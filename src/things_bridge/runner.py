"""The only process launcher (§2.6): ``open``, ``osascript`` and ``pgrep`` run here and nowhere else.

* Writes leave as Things URLs opened with ``/usr/bin/open``, after the URL is re-validated against the
  command and parameter allowlists. The auth token is appended last, at dispatch time, only when the command
  needs it.
* Reads run registry templates through ``/usr/bin/osascript -``: the script goes on stdin, user values only in
  argv, each prefixed with ``=``.
* Every child gets an argv list (no shell), stdin from DEVNULL or ``input`` (never the MCP channel), captured
  output, a timeout, and an environment without the auth token. Subprocess exceptions are never chained into
  ours, because their text repeats argv. Everything that could carry the token passes ``redact()``.
"""

import errno
import fcntl
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ContextManager, Iterator, NamedTuple, Protocol, Sequence, runtime_checkable
from urllib.parse import parse_qsl, quote, urlsplit

from . import applescript, authtoken, urlscheme
from .errors import AutomationDenied, AutomationTimeout, DispatchFailed, InvalidInput, NotFound, NotMacOS, \
    ProtocolError, RateLimited, ReadFailed, Refused, SandboxBlocked, ThingsBridgeError, ThingsNotInstalled, \
    ThingsNotRunning, TokenMissing

log = logging.getLogger(__name__)

OPEN, OSASCRIPT, PGREP = "/usr/bin/open", "/usr/bin/osascript", "/usr/bin/pgrep"
THINGS_PROCESS = "Things3"                  # CONFIRMED: OFF-AS `tell application "Things3"`; TAPI pgrep -x Things3
BUNDLE_ID = applescript.BUNDLE_ID

OPEN_TIMEOUT_S = 15.0                       # `open` returns once LaunchServices queued the event (R-MEX §1.3)
PGREP_TIMEOUT_S = 5.0
LAUNCH_WAIT_S, LAUNCH_POLL_S, LAUNCH_SETTLE_S = 12.0, 0.2, 1.5    # R-MEX §1.2
SLOW_SCAN_TIMEOUT_S = 130.0                 # SCAN slow path waits up to 120 s inside AppleScript
OSA_LOCK_WAIT_S = 30.0
WRITE_LOCK_WAIT_S = 120.0                   # a writer holds it for dispatch + verify (6-15 s each)
LOCK_POLL_S = 0.05
RATE_BUDGET, RATE_WINDOW_S = urlscheme.RATE_BUDGET, urlscheme.RATE_WINDOW_S
STATE_ENV = "THINGS_BRIDGE_STATE_DIR"
DEFAULT_STATE_DIR = "~/Library/Caches/things-bridge"
COLUMN_MISMATCH = 9101                      # §4.1 custom error numbers
NOT_RUNNING = 9001
BAD_ARGUMENT = 9002
DICTIONARY_CODES = frozenset({-2740, -2741, -1708})    # terminology unavailable (§5.2)


@dataclass
class Dispatch:
    command: str
    url_redacted: str                       # never contains the token
    sent: bool
    dry_run: bool
    foreground: bool
    t0: float                               # wall() just before `open` ran; FIND_CREATED ages are measured from it


@runtime_checkable
class Runner(Protocol):
    """Everything above this layer does I/O only through these methods (FakeRunner implements them)."""

    def require_macos(self) -> None:
        """Raise ``NotMacOS`` unless on darwin with all required binaries present."""
        raise NotImplementedError

    def things_running(self) -> bool:
        """``pgrep -x Things3`` (no TCC, no Apple Event)."""
        raise NotImplementedError

    def ensure_running(self, *, allow_launch: bool) -> bool:
        """Launch Things hidden if needed; True if it launched Things. ``ThingsNotRunning`` if not allowed."""
        raise NotImplementedError

    def run_template(self, name: str, args: Sequence[str] = (), *,
                     timeout: float | None = None) -> "applescript.Frame":
        """Run the read-only template registered under ``name`` (a registry key, never script text)."""
        raise NotImplementedError

    def dispatch(self, turl: "urlscheme.ThingsURL", *, dry_run: bool = False) -> Dispatch:
        """Re-validate and open ``turl`` (token appended last when required). Never retried."""
        raise NotImplementedError

    def write_lock(self) -> ContextManager[None]:
        """Serialises dispatch + verify across processes."""
        raise NotImplementedError

    def wall(self) -> float:
        raise NotImplementedError

    def monotonic(self) -> float:
        raise NotImplementedError

    def sleep(self, s: float) -> None:
        raise NotImplementedError


# ---- redaction (§5.8; R-MEX §4.2) ---------------------------------------------------------------

_TOKEN_PARAM = re.compile(r"(?i)(auth-token(?:=|%3D))[^&\s\"'#]*")
_secrets: tuple[str, ...] = ()              # tokens this process has handled; replaced atomically
_secrets_lock = threading.Lock()


def remember_secret(token: str) -> None:
    """Make every later ``redact()`` scrub ``token``, even when the caller doesn't pass it."""
    global _secrets
    if token:
        with _secrets_lock:
            if token not in _secrets:
                _secrets = (*_secrets, token)


def _forms(token: str) -> set[str]:
    forms = {token}
    try:
        once = quote(token, safe="")
        forms |= {once, quote(token, safe="/"), quote(once, safe="")}
    except UnicodeEncodeError:
        pass
    return forms


def redact(text: str, token: str | None = None) -> str:
    """Mask ``auth-token=…`` values, plus the literal token and its percent-encoded forms, as ``***``."""
    out = _TOKEN_PARAM.sub(r"\1***", text)
    forms: set[str] = set()
    for secret in (*_secrets, token) if token else _secrets:
        forms |= _forms(secret)
    for form in sorted(forms, key=len, reverse=True):
        out = out.replace(form, "***")
    return out


class RedactFilter(logging.Filter):
    """Applies ``redact()`` to a record's message, arguments and traceback. Attach it to handlers (§5.8)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string must not break logging
            message = f"{record.msg!r} {record.args!r}"
        record.msg, record.args = redact(message), None
        if record.exc_info:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


# ---- error mapping (§5.2) -----------------------------------------------------------------------

_SANDBOX_HINT = ("If this runs inside Claude Code's sandbox, enable `sandbox.allowAppleEvents` or exclude the "
                 "command from the sandbox.")
_NOT_RUNNING_HINT = "Open Things 3, or allow auto-launch (no --no-launch, THINGS_BRIDGE_NO_LAUNCH unset)."


def _grantee() -> str:
    bundle = os.environ.get("__CFBundleIdentifier")     # inherited from the host app (R-MEX §5.1)
    return bundle or "the app that runs things-bridge (your terminal, or the MCP host)"


def _excerpt(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


_ECHOED_URL = re.compile(re.escape(urlscheme.SCHEME_PREFIX) + r"[^\s\"']*")


def _has_code(text: str, *codes: str) -> bool:
    return any(re.search(rf"(?<![\d-]){re.escape(c)}(?!\d)", text) for c in codes)


def map_osa_error(code: int | None, stderr: str, *, running: bool | None,
                  target_id: str | None = None) -> ThingsBridgeError:
    """The exception for a failed read.

    ``target_id`` is set (``""`` if it isn't id-shaped) when the read addressed one object by id.
    """
    details: dict = {"osa_code": code}
    by_id = target_id is not None
    if code == -1743:
        return AutomationDenied(
            "Not allowed to control Things.",
            hint=(f"System Settings → Privacy & Security → Automation → enable Things under {_grantee()}. If you "
                  "clicked Don't Allow, macOS won't ask again until you enable it there (or `tccutil reset "
                  "AppleEvents <bundle-id>`)."), details=details)
    if code == -1744:
        return AutomationDenied("macOS needs your consent before things-bridge can control Things.",
                                hint="Run `things-bridge setup` at the Mac to grant access.", details=details)
    if code == -1712:
        return AutomationTimeout("Things didn't answer.",
                                 hint=("A macOS permission dialog may be waiting on screen, or Things is busy. "
                                       "Answer it and retry."), details=details)
    if code == -600 and running:
        return SandboxBlocked("Apple Events are blocked for this process.", hint=_SANDBOX_HINT, details=details)
    if code in (-600, NOT_RUNNING):
        return ThingsNotRunning("Things 3 is not running.", hint=_NOT_RUNNING_HINT, details=details)
    if code == -609:
        return ReadFailed("Things quit or crashed mid-read.", hint="Reopen it and retry.", details=details)
    if code == -10004:
        return SandboxBlocked("Apple Events are blocked for this process.", hint=_SANDBOX_HINT, details=details)
    if code == -1728 or (code == -1719 and by_id):
        what = f"No item with id {target_id}" if target_id else "No such item in Things"
        return NotFound(f"{what} (wrong id, or it was deleted).", details=details)
    if code == -1719:
        return ReadFailed("Things returned an empty list; a dialog may be open in Things.",
                          hint="Close any dialog or sheet in the Things window and retry.", details=details)
    if code == -1708:
        # errAEEventNotHandled: Things compiled the script but rejected a term or a form in it. The message
        # carries Things' own text so a template bug can't hide behind "dictionary unavailable".
        return ReadFailed(f"Things didn't understand part of a read script (-1708): "
                          f"{_excerpt(redact(stderr)) or 'no error message'}",
                          hint="Things may be out of date, or this Things version rejects a term things-bridge uses.",
                          details=details)
    if code in DICTIONARY_CODES:
        return ReadFailed("Things' scripting dictionary is unavailable (is Things 3 installed and up to date?).",
                          details=details)
    if code == COLUMN_MISMATCH:
        return ReadFailed("Things changed while reading; retry.", details=details)
    if code == BAD_ARGUMENT:
        return InvalidInput(f"Things rejected a read argument: {_excerpt(redact(stderr))}", details=details)
    if code == -128:
        return ReadFailed("The read was cancelled.", details=details)
    label = f" ({code})" if code is not None else ""
    return ReadFailed(f"Reading from Things failed{label}: {_excerpt(redact(stderr)) or 'no error message'}",
                      details=details)


def map_open_error(rc: int, stderr: str, *, running: bool | None) -> ThingsBridgeError:
    """The exception for ``open`` exiting non-zero.

    LaunchServices echoes the URL in stderr. Callers pass stderr through ``redact(stderr, token)`` first; here the
    URL itself is replaced by ``<url>``, so user content can neither leak into the message nor look like an
    error code.
    """
    text = _ECHOED_URL.sub("<url>", redact(stderr))
    details: dict = {"rc": rc}
    if _has_code(text, "-10814") or "kLSApplicationNotFoundErr" in text or "Unable to find application" in text:
        return ThingsNotInstalled("Things 3 is not installed (no app handles Things URLs).", details=details)
    if _has_code(text, "-10810", "-600", "-10004") or "procNotFound" in text:
        return SandboxBlocked("macOS refused the hand-off to Things.", hint=_SANDBOX_HINT, details=details)
    hint = "The command probably didn't reach Things; check Things before retrying."
    if running is False:
        hint = "Things 3 isn't running. " + hint
    return DispatchFailed(f"`open` failed (exit {rc}): {_excerpt(text) or 'no output'}", hint=hint, details=details)


# ---- state directory, file locks, rate limiter ---------------------------------------------------

def _ensure_dir(path: Path) -> Path:
    for p in (path, path.resolve()):
        if "culturedcode" in str(p).lower():        # never inside Things' own folders (N1)
            raise InvalidInput(f"{STATE_ENV} must not point into Things' own folders",
                               details={"field": STATE_ENV})
    path.mkdir(mode=0o700, parents=True, exist_ok=True)   # only a newly created leaf gets 0700
    return path


def state_dir() -> Path:
    """``$THINGS_BRIDGE_STATE_DIR`` or ``~/Library/Caches/things-bridge``, created with mode 0700."""
    raw = os.environ.get(STATE_ENV, "").strip()
    return _ensure_dir(Path(os.path.expanduser(raw or DEFAULT_STATE_DIR)))


def _open_private(path: Path) -> int:
    return os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)


def _flock(path: Path, *, wait_s: float, monotonic: Callable[[], float], sleep: Callable[[float], None],
           busy: Callable[[], ThingsBridgeError]) -> int:
    """Open ``path`` and take an exclusive ``flock``, polling with LOCK_NB for up to ``wait_s``."""
    fd = _open_private(path)
    deadline = monotonic() + wait_s
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError as e:
            if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                os.close(fd)
                raise
        if monotonic() >= deadline:
            os.close(fd)
            raise busy()
        sleep(LOCK_POLL_S)


class _ProcessFileLock:
    """Held by one thread in this process and one process on this Mac (``flock``).

    Re-entrant for the owning thread; only the outermost hold takes the file lock.
    """

    def __init__(self, filename: str):
        self.filename = filename
        self._thread = threading.RLock()
        self._depth = 0
        self._fd: int | None = None

    @contextmanager
    def hold(self, directory: Path, *, wait_s: float, monotonic: Callable[[], float],
             sleep: Callable[[float], None], busy: Callable[[], ThingsBridgeError]) -> Iterator[None]:
        if not self._thread.acquire(timeout=wait_s):
            raise busy()
        try:
            if self._depth == 0:
                self._fd = _flock(directory / self.filename, wait_s=wait_s, monotonic=monotonic, sleep=sleep,
                                  busy=busy)
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0 and self._fd is not None:
                    fd, self._fd = self._fd, None
                    os.close(fd)            # closing the only descriptor releases the flock
        finally:
            self._thread.release()


_OSA_LOCK = _ProcessFileLock("osa.lock")
_WRITE_LOCK = _ProcessFileLock("write.lock")


class RateLimiter:
    """Sliding-window budget shared by every things-bridge process (R-MEX §6).

    The log is a JSON list of ``[wall_time, weight]`` pairs, read and rewritten under ``flock``. Things
    documents 250 items per 10 s; the budget keeps a 20 % margin.
    """

    def __init__(self, path: Path, budget: int = RATE_BUDGET, window_s: float = RATE_WINDOW_S, clock: Any = time,
                 sleep: Callable[[float], None] = time.sleep):
        self.path, self.budget, self.window_s = Path(path), budget, window_s
        self.clock, self.sleep = clock, sleep

    def acquire(self, weight: int, *, max_wait_s: float = 30.0) -> None:
        """Record ``weight`` items, first waiting (up to ``max_wait_s``) until the window has room."""
        if weight > self.budget:
            raise Refused(f"This request sends {weight} items at once; Things accepts at most 250 per 10 s and "
                          f"things-bridge budgets {self.budget}.",
                          hint=f"split the request into batches of at most {self.budget} items")
        if weight <= 0:
            return
        waited = 0.0
        while True:
            with self._log() as (events, save):
                now = self.clock.time()
                live = self._live(events, now)
                wait = self._wait_for(live, weight, now)
                if wait <= 0:
                    save([*live, [now, weight]])
                    return
                if live != events:
                    save(live)              # persist pruning, and clamped future stamps so they expire
                used = sum(n for _, n in live)
            if waited + wait > max_wait_s:
                raise RateLimited(f"Rate limit: {used} of {self.budget} items were sent in the last "
                                  f"{self.window_s:g} s; this request would have to wait {waited + wait:.1f} s.",
                                  hint="wait a few seconds and retry, or send fewer items")
            log.info("rate limit: waiting %.1f s for %d items", wait, weight)
            self.sleep(wait)
            waited += wait

    def usage(self) -> int:
        """Items recorded in the current window."""
        with self._log() as (events, save):
            live = self._live(events, self.clock.time())
            if live != events:
                save(live)
            return sum(n for _, n in live)

    def _live(self, events: list, now: float) -> list[list[float]]:
        out = []
        for e in events:
            if isinstance(e, list) and len(e) == 2 and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                                           for v in e) and e[1] > 0:
                t = min(float(e[0]), now)   # a future stamp (clock step back) counts as now, never as expired
                if now - t < self.window_s:
                    out.append([t, e[1]])
        return sorted(out)

    def _wait_for(self, live: list[list[float]], weight: int, now: float) -> float:
        excess = sum(n for _, n in live) + weight - self.budget
        if excess <= 0:
            return 0.0
        freed = 0
        for t, n in live:
            freed += n
            if freed >= excess:
                return max(t + self.window_s - now, 0.01)
        return self.window_s                # unreachable while weight <= budget

    @contextmanager
    def _log(self) -> Iterator[tuple[list, Callable[[list], None]]]:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = _open_private(self.path)
        with os.fdopen(fd, "r+", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)  # held for one read-modify-write only
            try:
                events = json.loads(fh.read() or "[]")
            except ValueError:
                events = []

            def save(new: list) -> None:
                fh.seek(0)
                fh.truncate()
                fh.write(json.dumps(new))
                fh.flush()

            yield (events if isinstance(events, list) else []), save


# ---- subprocess plumbing ------------------------------------------------------------------------

class _Failed(NamedTuple):
    kind: str                               # "timeout" | "oserror"
    detail: str                             # never contains argv


def _child_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != authtoken.ENV}
    env.update(extra)
    return env


def _osa_env() -> dict[str, str]:
    env = _child_env(LANG="en_US.UTF-8")
    for name in ("LC_ALL", "LC_CTYPE"):     # would override LANG
        env.pop(name, None)
    return env


def _spawn(argv: list[str], *, timeout: float, input: bytes | None = None,
           env: dict[str, str] | None = None) -> "subprocess.CompletedProcess[bytes] | _Failed":
    """``subprocess.run`` with captured bytes output. Failures come back as values, so no exception whose text
    repeats argv is ever chained to the one the caller raises."""
    io: dict[str, Any] = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    try:
        return subprocess.run(argv, capture_output=True, timeout=timeout, env=_child_env() if env is None else env,
                              **io)
    except subprocess.TimeoutExpired:
        return _Failed("timeout", f"timed out after {timeout:g} s")
    except OSError as e:
        return _Failed("oserror", e.strerror or type(e).__name__)


def _text(data: bytes | str | None) -> str:
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return data or ""


def _is_executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def binaries() -> tuple[str, ...]:
    """The four programs things-bridge runs."""
    return OPEN, OSASCRIPT, PGREP, authtoken.SECURITY


# ---- URL re-validation (D3, N8) ---------------------------------------------------------------

def _has_update_op(obj: object) -> bool:
    if isinstance(obj, dict):
        return obj.get("operation") == "update" or any(_has_update_op(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_update_op(v) for v in obj)
    return False


def _checked_url(turl: "urlscheme.ThingsURL") -> str:
    """Re-check a ``ThingsURL`` before it leaves the process; returns its URL (without token)."""
    command = turl.command
    if not isinstance(command, str) or command not in urlscheme.ALLOWED_COMMANDS:
        raise Refused(f"Refusing to open a Things URL with command {command!r}: it isn't on the allowlist.")
    keys = [k for k, _ in turl.params]
    for key in keys:
        if not isinstance(key, str) or key.lower().startswith("x-"):
            raise Refused("Refusing to send x-callback parameters.")
        if key not in urlscheme.PARAMS[command]:
            raise Refused(f"Refusing to send parameter {key!r}: it isn't documented for {command}.")
    if len(set(keys)) != len(keys):
        raise Refused(f"Refusing to send a {command} URL with a repeated parameter.")
    url = turl.url
    prefix = urlscheme.SCHEME_PREFIX + command
    if not (url == prefix or url.startswith(prefix + "?")) or not url.isascii() or \
            any(c.isspace() or not c.isprintable() for c in url):
        raise Refused(f"Refusing to open a malformed {command} URL.")
    parts = urlsplit(url)
    back = [k for k, _ in parse_qsl(parts.query, keep_blank_values=True)]
    if parts.netloc or parts.path != "/" + command or parts.fragment or back != keys:
        raise Refused(f"Refusing to open a malformed {command} URL.")
    return url


def _needs_token(turl: "urlscheme.ThingsURL") -> bool:
    if turl.command in urlscheme.TOKEN_ALWAYS:
        return True
    if turl.command != "json":
        return False
    data = dict(turl.params).get("data")
    try:
        payload = json.loads(data) if isinstance(data, str) else None
    except ValueError:
        payload = None
    if not isinstance(payload, list):
        raise Refused("Refusing to send a json URL whose data isn't a JSON array.")
    return _has_update_op(payload)


def _target_id(name: str, args: Sequence[str]) -> str | None:
    """The id a template addresses directly (for -1728/-1719 mapping); ``""`` if it isn't id-shaped."""
    value = None
    if name == "SCAN" and len(args) >= 2 and args[0] in ("project", "area"):
        value = args[1]
    elif name == "FIND_CREATED" and len(args) >= 5 and args[3] in ("project", "area"):
        value = args[4]
    elif name == "PROBE" and args:
        value = args[0]
    elif name == "ROWS" and len(args) == 2:
        value = args[1]
    if value is None:
        return None
    return value if urlscheme.ID_RE.fullmatch(value) else ""


# ---- the macOS runner ---------------------------------------------------------------------------

class MacRunner(Runner):
    """The real runner. ``clock`` quacks like the ``time`` module (``time``, ``monotonic``, ``sleep``)."""

    def __init__(self, *, clock: Any = time, state: Path | None = None):
        self._clock = clock
        self._state = Path(state) if state is not None else None
        self._limiter: RateLimiter | None = None
        self._macos = False

    # -- clock and state --

    def wall(self) -> float:
        return self._clock.time()

    def monotonic(self) -> float:
        return self._clock.monotonic()

    def sleep(self, s: float) -> None:
        self._clock.sleep(s)

    def state_dir(self) -> Path:
        return _ensure_dir(self._state) if self._state is not None else state_dir()

    @property
    def rate_limiter(self) -> RateLimiter:
        if self._limiter is None:
            self._limiter = RateLimiter(self.state_dir() / "ratelimit.json", clock=self._clock, sleep=self.sleep)
        return self._limiter

    # -- environment --

    def require_macos(self) -> None:
        if self._macos:
            return
        missing = [b for b in binaries() if not _is_executable(b)] if sys.platform == "darwin" else []
        if sys.platform != "darwin" or missing:
            where = sys.platform + (f" (missing {', '.join(missing)})" if missing else "")
            raise NotMacOS(f"things-bridge controls Things 3 on macOS; this system is {where}",
                           details={"platform": sys.platform, "missing": missing})
        self._macos = True

    def things_running(self) -> bool:
        self.require_macos()
        res = _spawn([PGREP, "-x", THINGS_PROCESS], timeout=PGREP_TIMEOUT_S)
        if isinstance(res, _Failed):
            raise ReadFailed(f"Couldn't check whether Things is running: pgrep {res.detail}.")
        if res.returncode not in (0, 1):
            raise ReadFailed(f"Couldn't check whether Things is running: pgrep exited {res.returncode}.")
        return res.returncode == 0

    def _running_or_none(self) -> bool | None:
        try:
            return self.things_running()
        except ThingsBridgeError:
            return None

    def ensure_running(self, *, allow_launch: bool) -> bool:
        if self.things_running():
            return False
        if not allow_launch:
            raise ThingsNotRunning("Things 3 is not running (auto-launch is disabled).", hint=_NOT_RUNNING_HINT)
        # -g: don't activate; -b: by bundle id. Never let an Apple Event launch Things (it comes up frontmost).
        res = _spawn([OPEN, "-g", "-b", BUNDLE_ID], timeout=OPEN_TIMEOUT_S)
        if isinstance(res, _Failed):
            raise ThingsNotRunning(f"Couldn't launch Things 3: open {res.detail}.",
                                   hint="Open Things by hand and retry.")
        if res.returncode != 0:
            raise map_open_error(res.returncode, _text(res.stderr) or _text(res.stdout), running=False)
        deadline = self.monotonic() + LAUNCH_WAIT_S
        while not self.things_running():
            if self.monotonic() >= deadline:
                raise ThingsNotRunning(f"Things 3 didn't start within {LAUNCH_WAIT_S:g} s.",
                                       hint="Open Things by hand and retry.")
            self.sleep(LAUNCH_POLL_S)
        self.sleep(LAUNCH_SETTLE_S)         # a URL sent during start-up has been reported dropped (R-MEX §1.2)
        log.info("launched Things 3 in the background")
        return True

    # -- reads --

    @contextmanager
    def _osa_lock(self) -> Iterator[None]:
        def busy() -> ThingsBridgeError:
            return AutomationTimeout(f"Another things-bridge read has been waiting on Things for over "
                                     f"{OSA_LOCK_WAIT_S:g} s.",
                                     hint="Things may be busy or showing a permission dialog; answer it and retry.")
        with _OSA_LOCK.hold(self.state_dir(), wait_s=OSA_LOCK_WAIT_S, monotonic=self.monotonic, sleep=self.sleep,
                            busy=busy):
            yield

    def run_template(self, name: str, args: Sequence[str] = (), *,
                     timeout: float | None = None) -> "applescript.Frame":
        template = applescript.TEMPLATES[name]              # KeyError: a programming error
        if isinstance(args, (str, bytes)):
            raise TypeError("args must be a sequence of str, not a single string")
        args = list(args)
        encoded = applescript.encode_args(args)
        template.check_args(args)
        if timeout is None:
            slow = name == "SCAN" and len(args) == 4 and args[3] == "1"
            timeout = SLOW_SCAN_TIMEOUT_S if slow else template.timeout_s
        argv = [OSASCRIPT, "-", *encoded]
        source = applescript.script_source(name).encode("utf-8")
        self.require_macos()
        with self._osa_lock():
            for attempt in (1, 2):          # reads only: one retry on a torn frame or a column mismatch
                res = _spawn(argv, timeout=timeout, input=source, env=_osa_env())
                if isinstance(res, _Failed):
                    if res.kind == "timeout":
                        raise AutomationTimeout(f"Things didn't answer within {timeout:g} s ({name}).",
                                                hint=("A macOS permission dialog may be waiting on screen, or "
                                                      "Things is busy. Answer it and retry."))
                    raise ReadFailed(f"Couldn't run osascript: {res.detail}.")
                if res.returncode == 0:
                    try:
                        return applescript.parse_frame(res.stdout, name)
                    except ProtocolError:
                        if attempt == 2:
                            raise
                        log.warning("%s: malformed output, retrying once", name)
                        continue
                code, message = applescript.parse_osa_error(_text(res.stderr))
                if code == COLUMN_MISMATCH and attempt == 1:
                    log.warning("%s: column length mismatch, retrying once", name)
                    continue
                if code in DICTIONARY_CODES and name != "APP_VERSION" and not self._app_version_ok():
                    raise ThingsNotInstalled("Things 3 doesn't seem to be installed: its scripting dictionary is "
                                             "unavailable and its version can't be read.",
                                             details={"osa_code": code})
                raise map_osa_error(code, message, running=self._running_or_none(),
                                    target_id=_target_id(name, args))
        raise AssertionError("unreachable")

    def _app_version_ok(self) -> bool:
        """APP_VERSION, run while the osa lock is already held."""
        res = _spawn([OSASCRIPT, "-"], timeout=applescript.TEMPLATES["APP_VERSION"].timeout_s,
                     input=applescript.script_source("APP_VERSION").encode("utf-8"), env=_osa_env())
        if isinstance(res, _Failed) or res.returncode != 0:
            return False
        try:
            applescript.parse_frame(res.stdout, "APP_VERSION")
        except ProtocolError:
            return False
        return True

    # -- writes and navigation --

    def dispatch(self, turl: "urlscheme.ThingsURL", *, dry_run: bool = False) -> Dispatch:
        base = _checked_url(turl)
        needs = _needs_token(turl)
        if bool(turl.requires_token) != needs:
            raise Refused(f"Refusing to send a {turl.command} URL whose token requirement doesn't match its "
                          "command.")
        sep = "&" if "?" in base else "?"
        shown = redact(base) + (f"{sep}auth-token=***" if needs else "")
        if dry_run:                         # no process, no Keychain: works on any OS (§5.5)
            return Dispatch(turl.command, shown, sent=False, dry_run=True, foreground=turl.foreground,
                            t0=self.wall())
        self.require_macos()
        url, token = base, None
        if needs:
            token = authtoken.get_token()
            if not token:
                raise TokenMissing(f"No auth token is set up; Things requires one for {turl.command}.",
                                   hint=("In Things: Settings → General → Enable Things URLs → Manage → copy the "
                                         "token, then run `things-bridge token set` in Terminal."))
            if not authtoken.TOKEN_RE.fullmatch(token):
                source = authtoken.ENV if os.environ.get(authtoken.ENV, "").strip() else "the Keychain"
                raise TokenMissing(f"The auth token from {source} isn't a valid Things URL token.",
                                   hint="Copy it again from Things and run `things-bridge token set`.")
            remember_secret(token)
            url = f"{base}{sep}auth-token={urlscheme.enc(token)}"      # always last
        if len(url.encode("utf-8")) > urlscheme.MAX_URL_BYTES:
            raise InvalidInput(f"The {turl.command} URL would be longer than {urlscheme.MAX_URL_BYTES} bytes.",
                               hint="split the request into smaller batches", details={"field": "url"})
        if turl.mutating:
            self.rate_limiter.acquire(max(1, turl.weight))
        t0 = self.wall()
        argv = [OPEN, url] if turl.foreground else [OPEN, "-g", url]
        res = _spawn(argv, timeout=OPEN_TIMEOUT_S)
        if isinstance(res, _Failed):
            if res.kind == "timeout":
                raise DispatchFailed(f"`open` didn't return within {OPEN_TIMEOUT_S:g} s; the {turl.command} "
                                     "command may or may not reach Things.",
                                     hint="Check Things before retrying; nothing is re-sent automatically.")
            raise DispatchFailed(f"Couldn't run open: {redact(res.detail, token)}.")
        if res.returncode != 0:
            raise map_open_error(res.returncode, redact(_text(res.stderr) or _text(res.stdout), token),
                                 running=self._running_or_none())
        log.debug("opened %s", shown)
        return Dispatch(turl.command, shown, sent=True, dry_run=False, foreground=turl.foreground, t0=t0)

    @contextmanager
    def write_lock(self) -> Iterator[None]:
        def busy() -> ThingsBridgeError:
            return RateLimited(f"Another things-bridge write has been in progress for over "
                               f"{WRITE_LOCK_WAIT_S:g} s.", hint="wait for it to finish, then retry")
        with _WRITE_LOCK.hold(self.state_dir(), wait_s=WRITE_LOCK_WAIT_S, monotonic=self.monotonic,
                              sleep=self.sleep, busy=busy):
            yield
