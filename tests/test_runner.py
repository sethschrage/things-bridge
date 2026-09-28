"""MacRunner, RateLimiter, redaction, error mapping, state dir and locks (§2.6, §5.2, §5.8).

``subprocess.run`` is replaced by a recorder for every test that touches the runner, and the clock is fake, so
nothing here spawns a process, talks to Things or sleeps (except the one threading test).
"""

import fcntl
import json
import logging
import os
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from things_bridge import applescript, authtoken, jsoncmd, runner, urlscheme
from things_bridge.errors import AutomationDenied, AutomationTimeout, DispatchFailed, InvalidInput, NotFound, \
    NotMacOS, ProtocolError, RateLimited, ReadFailed, Refused, SandboxBlocked, ThingsBridgeError, \
    ThingsNotInstalled, ThingsNotRunning, TokenMissing
from things_bridge.runner import OPEN, OSASCRIPT, PGREP, Dispatch, MacRunner, RateLimiter, Runner, map_open_error, \
    map_osa_error, redact
from things_bridge.urlscheme import ThingsURL

FIXTURES = Path(__file__).parent / "fixtures"
TODO, PROJ = "SyJEz273ceSkabUbciM73A", "Jvj7EW1fLoScPhaw2JomCT"
TOKEN = "vKkylosuSuGwxrz7qcklOw"                    # the things.py test fixture token shape (22 base62)
ODD_TOKEN = "Ab+Cd/Ef=Gh~12.3_4-5"                  # every TOKEN_RE punctuation character


# ---- fake subprocess ------------------------------------------------------------------------------

def done(rc: int = 0, out: bytes | str = b"", err: bytes | str = b"") -> subprocess.CompletedProcess:
    enc = (lambda v: v.encode("utf-8") if isinstance(v, str) else v)
    return subprocess.CompletedProcess([], rc, enc(out), enc(err))


def times_out(argv: list[str], kw: dict) -> Any:
    raise subprocess.TimeoutExpired(argv, kw["timeout"], output=b"", stderr=b"")


def os_error(argv: list[str], kw: dict) -> Any:
    raise FileNotFoundError(2, "No such file or directory", argv[0])


@dataclass
class Call:
    argv: list[str]
    kw: dict


@dataclass
class FakeProc:
    """Records every ``subprocess.run`` call, checks its hygiene, and answers from per-program queues.

    A queued answer is a ``CompletedProcess``, or a callable ``(argv, kw)`` that returns one or raises.
    """
    calls: list[Call] = field(default_factory=list)
    queues: dict[str, list] = field(default_factory=lambda: {PGREP: [], OSASCRIPT: [], OPEN: []})
    running: bool = True                            # pgrep's answer when its queue is empty

    def __call__(self, argv, **kw):
        assert isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)
        assert not kw.get("shell"), "shell must never be used"
        assert kw.get("capture_output") is True, "output must be captured (stdout is the MCP channel)"
        assert ("input" in kw) != ("stdin" in kw), "stdin must be DEVNULL or input, never inherited"
        if "stdin" in kw:
            assert kw["stdin"] is subprocess.DEVNULL
        else:
            assert isinstance(kw["input"], bytes)
        assert not {"text", "encoding", "errors", "universal_newlines"} & set(kw), "bytes only"
        assert kw.get("timeout") and kw["timeout"] > 0
        assert "env" in kw and authtoken.ENV not in kw["env"]
        self.calls.append(Call(list(argv), kw))
        queue = self.queues[argv[0]]
        if queue:
            answer = queue.pop(0)
        elif argv[0] == PGREP:
            answer = done(0 if self.running else 1)
        elif argv[0] == OPEN:
            answer = done()
        else:
            raise AssertionError(f"no scripted answer for {argv[0]}")
        return answer(argv, kw) if callable(answer) else answer

    def of(self, program: str) -> list[Call]:
        return [c for c in self.calls if c.argv[0] == program]


@pytest.fixture
def proc(monkeypatch) -> FakeProc:
    fake = FakeProc()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


@pytest.fixture
def mac(monkeypatch):
    """Pretend to be macOS with all four binaries present."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(runner, "_is_executable", lambda path: True)


@pytest.fixture
def mr(clock, mac, proc) -> MacRunner:
    return MacRunner(clock=clock)


@pytest.fixture(autouse=True)
def forget_secrets(monkeypatch):
    monkeypatch.setattr(runner, "_secrets", ())


@pytest.fixture
def token(monkeypatch):
    """A token from the Keychain (get_token patched), counting lookups."""
    looked_up = []

    def get_token():
        looked_up.append(1)
        return TOKEN
    monkeypatch.setattr(authtoken, "get_token", get_token)
    return looked_up


def osa_err(name: str) -> subprocess.CompletedProcess:
    return done(1, err=(FIXTURES / "osa_stderr" / f"osa_{name}.txt").read_bytes())


def tb1(name: str) -> bytes:
    return (FIXTURES / "tb1" / name).read_bytes()


def update_url(**kw) -> ThingsURL:
    return urlscheme.build_update(id=TODO, **kw)


def secrets_absent(exc: BaseException, *secrets: str) -> None:
    """No form of any secret in anything an error exposes."""
    texts = [str(exc), repr(exc)]
    if isinstance(exc, ThingsBridgeError):
        texts += [exc.message, str(exc.hint), json.dumps(exc.details, default=str)]
    for s in secrets:
        for form in runner._forms(s):
            assert all(form not in t for t in texts), (form, texts)
    assert exc.__cause__ is None and exc.__context__ is None, "subprocess exceptions (argv) must not be chained"


# ---- interface ----------------------------------------------------------------------------------

def test_macrunner_implements_runner():
    r = MacRunner()
    assert isinstance(r, Runner)
    for name in ("require_macos", "things_running", "ensure_running", "run_template", "dispatch", "write_lock",
                 "wall", "monotonic", "sleep"):
        assert getattr(MacRunner, name) is not getattr(Runner, name), name


def test_clock_methods_delegate(clock):
    r = MacRunner(clock=clock)
    assert r.wall() == clock.time() and r.monotonic() == clock.monotonic()
    r.sleep(1.5)
    assert clock.sleeps == [1.5]


def test_default_clock_is_real_time():
    r = MacRunner()
    assert abs(r.wall() - time.time()) < 5
    assert abs(r.monotonic() - time.monotonic()) < 5


def test_binaries_are_the_four_absolute_paths():
    assert runner.binaries() == (OPEN, OSASCRIPT, PGREP, authtoken.SECURITY)
    assert all(b.startswith("/usr/bin/") for b in runner.binaries())


# ---- require_macos (§5.5) ----------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "darwin", reason="exercises the non-macOS path")
def test_require_macos_on_this_os_raises_not_macos(proc):
    with pytest.raises(NotMacOS) as e:
        MacRunner().require_macos()
    assert e.value.message == f"things-bridge controls Things 3 on macOS; this system is {sys.platform}"
    assert e.value.exit_code == 3
    assert proc.calls == []


@pytest.mark.parametrize("call", [
    lambda r: r.things_running(),
    lambda r: r.ensure_running(allow_launch=True),
    lambda r: r.run_template("AREAS"),
    lambda r: r.dispatch(urlscheme.build_add(title="x")),
    lambda r: r.dispatch(urlscheme.build_show(id="today")),
])
def test_no_process_is_spawned_off_macos(monkeypatch, proc, call):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(runner, "_is_executable", lambda path: True)
    with pytest.raises(NotMacOS):
        call(MacRunner())
    assert proc.calls == []


def test_require_macos_needs_all_four_binaries(monkeypatch, proc):
    monkeypatch.setattr(sys, "platform", "darwin")
    checked = []

    def is_exec(path):
        checked.append(path)
        return path != PGREP
    monkeypatch.setattr(runner, "_is_executable", is_exec)
    with pytest.raises(NotMacOS) as e:
        MacRunner().require_macos()
    assert set(checked) == {OPEN, OSASCRIPT, PGREP, authtoken.SECURITY}
    assert e.value.details["missing"] == [PGREP]
    assert PGREP in e.value.message and "darwin" in e.value.message
    assert proc.calls == []


def test_require_macos_passes_and_caches(monkeypatch, mac):
    r = MacRunner()
    r.require_macos()
    monkeypatch.setattr(runner, "_is_executable", lambda path: pytest.fail("not re-checked"))
    r.require_macos()


def test_is_executable(tmp_path):
    exe, plain = tmp_path / "exe", tmp_path / "plain"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    plain.write_text("")
    plain.chmod(0o644)
    assert runner._is_executable(str(exe))
    assert not runner._is_executable(str(plain))
    assert not runner._is_executable(str(tmp_path / "missing"))
    assert not runner._is_executable(str(tmp_path))


# ---- things_running / ensure_running (§2.6, R-MEX §1.2) ----------------------------------------

def test_things_running_uses_pgrep_exact_name(mr, proc):
    proc.queues[PGREP] = [done(0), done(1)]
    assert mr.things_running() is True
    assert mr.things_running() is False
    assert [c.argv for c in proc.calls] == [[PGREP, "-x", "Things3"]] * 2


@pytest.mark.parametrize("answer", [done(2), done(3), times_out, os_error])
def test_things_running_pgrep_failure(mr, proc, answer):
    proc.queues[PGREP] = [answer]
    with pytest.raises(ReadFailed) as e:
        mr.things_running()
    assert e.value.__context__ is None


def test_ensure_running_when_already_running(mr, proc, clock):
    assert mr.ensure_running(allow_launch=True) is False
    assert [c.argv[0] for c in proc.calls] == [PGREP]
    assert clock.sleeps == []


def test_ensure_running_launch_disallowed(mr, proc):
    proc.running = False
    with pytest.raises(ThingsNotRunning) as e:
        mr.ensure_running(allow_launch=False)
    assert "auto-launch is disabled" in e.value.message
    assert proc.of(OPEN) == []


def test_ensure_running_launches_hidden_by_bundle_id(mr, proc, clock):
    proc.queues[PGREP] = [done(1), done(1), done(1), done(0)]
    assert mr.ensure_running(allow_launch=True) is True
    launch = proc.of(OPEN)
    assert [c.argv for c in launch] == [[OPEN, "-g", "-b", "com.culturedcode.ThingsMac"]]
    assert launch[0].kw["timeout"] == 15
    assert [c.argv[0] for c in proc.calls] == [PGREP, OPEN, PGREP, PGREP, PGREP]
    assert clock.sleeps == pytest.approx([0.2, 0.2, 1.5])        # poll, poll, settle
    assert proc.of(OSASCRIPT) == [], "an Apple Event must never be what launches Things"


def test_ensure_running_gives_up_after_12_s(mr, proc, clock):
    proc.running = False
    t = clock.monotonic()
    with pytest.raises(ThingsNotRunning) as e:
        mr.ensure_running(allow_launch=True)
    assert "12 s" in e.value.message
    assert clock.monotonic() - t == pytest.approx(12.0, abs=0.25)
    assert set(clock.sleeps) == {0.2}


def test_ensure_running_not_installed(mr, proc):
    proc.running = False
    proc.queues[OPEN] = [done(1, err=(FIXTURES / "osa_stderr" / "open_-10814.txt").read_bytes())]
    with pytest.raises(ThingsNotInstalled):
        mr.ensure_running(allow_launch=True)


@pytest.mark.parametrize("answer", [times_out, os_error])
def test_ensure_running_open_fails_to_run(mr, proc, answer):
    proc.running = False
    proc.queues[OPEN] = [answer]
    with pytest.raises(ThingsNotRunning) as e:
        mr.ensure_running(allow_launch=True)
    assert e.value.__context__ is None


# ---- run_template (§2.6) -------------------------------------------------------------------------

def test_osascript_argv_shape(mr, proc, monkeypatch):
    monkeypatch.setenv(authtoken.ENV, TOKEN)
    monkeypatch.setenv("LC_ALL", "C")
    proc.queues[OSASCRIPT] = [done(0, tb1("scan_list_today.bin"))]
    frame = mr.run_template("SCAN", ["list", "today", "1", "0"])
    [call] = proc.of(OSASCRIPT)
    assert call.argv == [OSASCRIPT, "-", "=list", "=today", "=1", "=0"]
    assert call.kw["input"] == applescript.script_source("SCAN").encode("utf-8")
    assert call.kw["timeout"] == applescript.TEMPLATES["SCAN"].timeout_s
    env = call.kw["env"]
    assert env["LANG"] == "en_US.UTF-8" and "LC_ALL" not in env and authtoken.ENV not in env
    assert env.get("PATH") == os.environ.get("PATH")
    assert frame.template == "SCAN" and len(frame.of("S")) == 3
    assert "\r\n" in frame.of("S")[0][6], "stdout must stay bytes: universal newlines would eat \\r"


def test_user_values_only_in_argv_with_prefix(mr, proc):
    title = '-tb ünïcødé 🙂 " & end tell -- '
    proc.queues[OSASCRIPT] = [done(0, tb1("find_created_diag.bin"))]
    mr.run_template("FIND_CREATED", ["to-do", title, "30", "list", "inbox", "1"])
    [call] = proc.of(OSASCRIPT)
    assert call.argv[2:] == ["=to-do", "=" + title, "=30", "=list", "=inbox", "=1"]
    assert title.encode("utf-8") not in call.kw["input"]
    assert "-e" not in call.argv


def test_zero_argument_template(mr, proc):
    proc.queues[OSASCRIPT] = [done(0, tb1("areas.bin"))]
    assert mr.run_template("AREAS").template == "AREAS"
    assert proc.of(OSASCRIPT)[0].argv == [OSASCRIPT, "-"]


def test_app_version_has_no_library(mr, proc):
    proc.queues[OSASCRIPT] = [done(0, tb1("app_version.bin"))]
    mr.run_template("APP_VERSION")
    assert proc.of(OSASCRIPT)[0].kw["input"] == applescript.TEMPLATES["APP_VERSION"].body.encode()


def test_only_registry_keys_run(mr, proc):
    with pytest.raises(KeyError):
        mr.run_template('tell application "Finder" to delete every file')
    with pytest.raises(KeyError):
        mr.run_template("scan")
    assert proc.calls == []


@pytest.mark.parametrize("args, exc", [
    (["list", "today", "0"], InvalidInput),         # SCAN takes 4
    ("list", TypeError),                            # a bare str is not a sequence of args
    ([1, 2, 3, 4], TypeError),
    (["a\x00b", "", "0", "0"], InvalidInput),
])
def test_bad_arguments_never_spawn(mr, proc, args, exc):
    with pytest.raises(exc):
        mr.run_template("SCAN", args)
    assert proc.calls == []


def test_accepts_any_sequence_of_args(mr, proc):
    proc.queues[OSASCRIPT] = [done(0, tb1("scan_list_today.bin"))]
    mr.run_template("SCAN", ("list", "today", "0", "0"))
    assert proc.of(OSASCRIPT)[0].argv[2:] == ["=list", "=today", "=0", "=0"]


def test_explicit_and_slow_scan_timeouts(mr, proc):
    proc.queues[OSASCRIPT] = [done(0, tb1("automation.bin")), done(0, tb1("scan_list_today.bin"))]
    mr.run_template("AUTOMATION", timeout=130)
    mr.run_template("SCAN", ["list", "today", "0", "1"])
    assert [c.kw["timeout"] for c in proc.of(OSASCRIPT)] == [130, runner.SLOW_SCAN_TIMEOUT_S]


def test_timeout_maps_to_automation_timeout_without_argv(mr, proc):
    secret_arg = "SECRET-TITLE-VALUE"
    proc.queues[OSASCRIPT] = [times_out]
    with pytest.raises(AutomationTimeout) as e:
        mr.run_template("FIND_CREATED", ["to-do", secret_arg, "30", "app", "", "0"])
    secrets_absent(e.value, secret_arg)
    assert "permission dialog" in e.value.hint
    assert len(proc.of(OSASCRIPT)) == 1, "a timeout is not retried"


def test_osascript_cannot_run(mr, proc):
    proc.queues[OSASCRIPT] = [os_error]
    with pytest.raises(ReadFailed) as e:
        mr.run_template("AREAS")
    assert e.value.__context__ is None


def test_protocol_error_retried_once(mr, proc):
    proc.queues[OSASCRIPT] = [done(0, tb1("truncated_scan.bin")), done(0, tb1("scan_list_today.bin"))]
    assert mr.run_template("SCAN", ["list", "today", "0", "0"]).template == "SCAN"
    assert len(proc.of(OSASCRIPT)) == 2


def test_protocol_error_twice_raises(mr, proc):
    proc.queues[OSASCRIPT] = [done(0, tb1("truncated_scan.bin")), done(0, b"")]
    with pytest.raises(ProtocolError):
        mr.run_template("SCAN", ["app", "", "0", "0"])
    assert len(proc.of(OSASCRIPT)) == 2


def test_column_mismatch_retried_once(mr, proc):
    proc.queues[OSASCRIPT] = [osa_err("9101"), done(0, tb1("areas.bin"))]
    assert mr.run_template("AREAS").template == "AREAS"
    assert len(proc.of(OSASCRIPT)) == 2


def test_column_mismatch_twice_is_read_failed(mr, proc):
    proc.queues[OSASCRIPT] = [osa_err("9101"), osa_err("9101")]
    with pytest.raises(ReadFailed) as e:
        mr.run_template("AREAS")
    assert e.value.message == "Things changed while reading; retry."
    assert e.value.details["osa_code"] == 9101
    assert len(proc.of(OSASCRIPT)) == 2


def test_other_errors_are_not_retried(mr, proc):
    proc.queues[OSASCRIPT] = [osa_err("-1743")]
    with pytest.raises(AutomationDenied):
        mr.run_template("AREAS")
    assert len(proc.of(OSASCRIPT)) == 1


# (fixture, template, args, running, expected exception, message fragment)
OSA_TABLE = [
    ("-1743", "AREAS", [], True, AutomationDenied, "Not allowed to control Things"),
    ("-1712", "AREAS", [], True, AutomationTimeout, "didn't answer"),
    ("-600", "AREAS", [], True, SandboxBlocked, "Apple Events are blocked"),
    ("-600", "AREAS", [], False, ThingsNotRunning, "not running"),
    ("-609", "AREAS", [], True, ReadFailed, "quit or crashed"),
    ("9001", "AREAS", [], False, ThingsNotRunning, "not running"),
    ("-10004", "AREAS", [], True, SandboxBlocked, "Apple Events are blocked"),
    ("-1728", "SCAN", ["project", PROJ, "0", "0"], True, NotFound, f"No item with id {PROJ}"),
    ("-1719", "SCAN", ["area", PROJ, "0", "0"], True, NotFound, f"No item with id {PROJ}"),
    ("-1728", "ROWS", ["0", TODO], True, NotFound, f"No item with id {TODO}"),
    ("-1728", "PROBE", [PROJ], True, NotFound, f"No item with id {PROJ}"),
    ("-1728", "FIND_CREATED", ["to-do", "t", "9", "project", PROJ, "0"], True, NotFound, PROJ),
    ("-1719", "SCAN", ["app", "", "0", "0"], True, ReadFailed, "a dialog may be open"),
    ("-1719", "MEMBERSHIP", ["today"], True, ReadFailed, "a dialog may be open"),
]


@pytest.mark.parametrize("fixture, name, args, running, exc, fragment", OSA_TABLE)
def test_osa_error_table(mr, proc, fixture, name, args, running, exc, fragment):
    proc.running = running
    proc.queues[OSASCRIPT] = [osa_err(fixture)]
    with pytest.raises(exc) as e:
        mr.run_template(name, args)
    assert fragment in e.value.message
    assert e.value.details["osa_code"] == int(fixture)
    assert e.value.__context__ is None


def test_dictionary_unavailable_but_app_present(mr, proc):
    proc.queues[OSASCRIPT] = [osa_err("-2741"), done(0, tb1("app_version.bin"))]
    with pytest.raises(ReadFailed) as e:
        mr.run_template("AREAS")
    assert "scripting dictionary is unavailable" in e.value.message
    probe = proc.of(OSASCRIPT)[1]
    assert probe.argv == [OSASCRIPT, "-"]
    assert probe.kw["input"] == applescript.script_source("APP_VERSION").encode()


@pytest.mark.parametrize("second", [osa_err("-1728"), done(0, b"garbage"), times_out])
def test_dictionary_unavailable_and_no_app_version(mr, proc, second):
    proc.queues[OSASCRIPT] = [osa_err("-2741"), second]
    with pytest.raises(ThingsNotInstalled):
        mr.run_template("TAGS")


def test_app_version_failure_is_not_probed_again(mr, proc):
    proc.queues[OSASCRIPT] = [osa_err("-2741")]
    with pytest.raises(ReadFailed):
        mr.run_template("APP_VERSION")
    assert len(proc.of(OSASCRIPT)) == 1


def test_unknown_osa_error(mr, proc):
    proc.queues[OSASCRIPT] = [done(1, err="12:30: execution error: Things3 got an error: Boom. (-10000)\n")]
    with pytest.raises(ReadFailed) as e:
        mr.run_template("AREAS")
    assert "(-10000)" in e.value.message and "Boom" in e.value.message
    proc.queues[OSASCRIPT] = [done(1, err="")]
    with pytest.raises(ReadFailed) as e:
        mr.run_template("AREAS")
    assert e.value.details["osa_code"] is None


# ---- osa.lock (single flight) --------------------------------------------------------------------

def hold_externally(path: Path) -> int:
    """Take the flock the way another process would (a separate open file description)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def is_free(path: Path) -> bool:
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


def test_osa_lock_file_and_state_dir_modes(mr, proc, state_dir):
    proc.queues[OSASCRIPT] = [done(0, tb1("areas.bin"))]
    mr.run_template("AREAS")
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((state_dir / "osa.lock").stat().st_mode) == 0o600
    assert is_free(state_dir / "osa.lock")


def test_osa_lock_held_during_osascript(mr, proc, state_dir):
    seen = []

    def check(argv, kw):
        seen.append(is_free(state_dir / "osa.lock"))
        return done(0, tb1("areas.bin"))
    proc.queues[OSASCRIPT] = [check]
    mr.run_template("AREAS")
    assert seen == [False]
    assert is_free(state_dir / "osa.lock")


def test_osa_lock_contention_times_out(mr, proc, clock, state_dir):
    fd = hold_externally(state_dir / "osa.lock")
    try:
        with pytest.raises(AutomationTimeout) as e:
            mr.run_template("AREAS")
    finally:
        os.close(fd)
    assert "Another things-bridge read" in e.value.message
    assert proc.of(OSASCRIPT) == []
    assert sum(clock.sleeps) == pytest.approx(runner.OSA_LOCK_WAIT_S, abs=0.1)
    proc.queues[OSASCRIPT] = [done(0, tb1("areas.bin"))]
    assert mr.run_template("AREAS").template == "AREAS"       # released: the next read goes through


def test_osa_lock_released_after_error(mr, proc, state_dir):
    proc.queues[OSASCRIPT] = [osa_err("-1743")]
    with pytest.raises(AutomationDenied):
        mr.run_template("AREAS")
    assert is_free(state_dir / "osa.lock")


def test_osa_lock_waits_then_proceeds(mr, proc, clock, state_dir, monkeypatch):
    fd = hold_externally(state_dir / "osa.lock")
    real_sleep = clock.sleep

    def sleep_then_release(s):
        real_sleep(s)
        if len(clock.sleeps) == 3:
            os.close(fd)
    monkeypatch.setattr(clock, "sleep", sleep_then_release)
    proc.queues[OSASCRIPT] = [done(0, tb1("areas.bin"))]
    assert mr.run_template("AREAS").template == "AREAS"
    assert clock.sleeps == [runner.LOCK_POLL_S] * 3


# ---- dispatch (§2.6) ---------------------------------------------------------------------------

def test_write_uses_open_g(mr, proc, clock):
    turl = urlscheme.build_add(title="Buy milk", notes="High fat")
    d = mr.dispatch(turl)
    [call] = proc.calls
    assert call.argv == [OPEN, "-g", "things:///add?title=Buy%20milk&notes=High%20fat"]
    assert call.kw["stdin"] is subprocess.DEVNULL and call.kw["timeout"] == 15
    assert d == Dispatch(command="add", url_redacted=turl.url, sent=True, dry_run=False, foreground=False,
                         t0=clock.time())


@pytest.mark.parametrize("turl", [urlscheme.build_show(id="today"), urlscheme.build_show(query="vacation"),
                                  urlscheme.build_search(query="vacation"), urlscheme.build_search()])
def test_navigation_uses_plain_open(mr, proc, state_dir, turl):
    d = mr.dispatch(turl)
    assert proc.calls[0].argv == [OPEN, turl.url]
    assert d.foreground is True and d.sent is True
    assert not (state_dir / "ratelimit.json").exists(), "navigation is not rate-limited"


@pytest.mark.parametrize("turl", [
    urlscheme.build_add(title="a"),
    urlscheme.build_add_project(title="p", to_dos=["a", "b"]),
    urlscheme.build_show(id=TODO),
    urlscheme.build_search(query="x"),
    urlscheme.build_json([jsoncmd.Todo(title="new")]),
    urlscheme.build_json([jsoncmd.Project(title="p", items=[jsoncmd.Todo(title="a")])]),
])
def test_token_never_sent_when_not_needed(mr, proc, token, turl):
    d = mr.dispatch(turl)
    assert token == [], "the Keychain must not even be read"
    assert "auth-token" not in proc.calls[0].argv[-1] and "auth-token" not in d.url_redacted


@pytest.mark.parametrize("turl", [
    urlscheme.build_update(id=TODO, title="Buy bread"),
    urlscheme.build_update_project(id=PROJ, deadline=urlscheme.CLEAR),
    urlscheme.build_json([jsoncmd.Todo(operation="update", id=TODO, when="anytime")]),
    urlscheme.build_json([jsoncmd.Todo(title="new"), jsoncmd.Project(operation="update", id=PROJ, completed=True)]),
])
def test_token_appended_last_and_redacted(mr, proc, token, turl):
    d = mr.dispatch(turl)
    url = proc.calls[0].argv[-1]
    assert url == turl.url + "&auth-token=" + TOKEN
    assert url.rsplit("&", 1)[1] == "auth-token=" + TOKEN
    assert d.url_redacted == turl.url + "&auth-token=***"
    assert TOKEN not in d.url_redacted and TOKEN not in turl.url
    assert token == [1]


def test_token_with_reserved_characters_is_encoded_and_redacted(mr, proc, monkeypatch, caplog):
    monkeypatch.setenv(authtoken.ENV, ODD_TOKEN)
    caplog.set_level(logging.DEBUG, logger="things_bridge")
    d = mr.dispatch(update_url(title="x"))
    url = proc.calls[0].argv[-1]
    assert url.endswith("&auth-token=" + urlscheme.enc(ODD_TOKEN))
    assert "&auth-token=Ab%2BCd%2FEf%3DGh~12.3_4-5" in url
    assert ODD_TOKEN not in d.url_redacted
    assert all(f not in caplog.text for f in runner._forms(ODD_TOKEN))
    assert authtoken.ENV not in proc.calls[0].kw["env"]
    assert redact(f"echo {ODD_TOKEN} and {urlscheme.enc(ODD_TOKEN)}") == "echo *** and ***"   # remembered


def test_env_token_read_at_dispatch_time(mr, proc, monkeypatch):
    monkeypatch.setenv(authtoken.ENV, f"  {TOKEN}\n")
    mr.dispatch(update_url(title="x"))
    assert proc.calls[0].argv[-1].endswith("&auth-token=" + TOKEN)


def test_token_missing(mr, proc, monkeypatch):
    monkeypatch.setattr(authtoken, "get_token", lambda: None)
    with pytest.raises(TokenMissing) as e:
        mr.dispatch(update_url(title="x"))
    assert "things-bridge token set" in e.value.hint
    assert proc.calls == []


@pytest.mark.parametrize("bad", ["short", "has space inside", "quote\"inside12", "x" * 257, "ünïcødé-token"])
def test_invalid_token_is_never_sent(mr, proc, monkeypatch, bad):
    monkeypatch.setenv(authtoken.ENV, bad)
    with pytest.raises(TokenMissing) as e:
        mr.dispatch(update_url(title="x"))
    assert authtoken.ENV in e.value.message
    secrets_absent(e.value, bad)
    assert proc.calls == []
    assert runner._secrets == ()


def test_keychain_error_propagates(mr, proc, monkeypatch):
    def fail():
        raise authtoken.KeychainError("Couldn't read the Keychain")
    monkeypatch.setattr(authtoken, "get_token", fail)
    with pytest.raises(authtoken.KeychainError):
        mr.dispatch(update_url(title="x"))
    assert proc.calls == []


def test_dry_run_spawns_nothing_on_any_os(monkeypatch, proc, clock):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(authtoken, "get_token", lambda: pytest.fail("dry-run must not read the Keychain"))
    r = MacRunner(clock=clock)
    turl = update_url(title="Buy bread")
    d = r.dispatch(turl, dry_run=True)
    assert d == Dispatch(command="update", url_redacted=turl.url + "&auth-token=***", sent=False, dry_run=True,
                         foreground=False, t0=clock.time())
    add = r.dispatch(urlscheme.build_add(title="a"), dry_run=True)
    assert add.url_redacted == "things:///add?title=a" and add.sent is False
    assert proc.calls == []


def test_dry_run_still_validates(mr, proc):
    with pytest.raises(Refused):
        mr.dispatch(ThingsURL("add", (("title", "a"), ("x-success", "evil://")), 1, False), dry_run=True)


def test_dry_run_is_not_rate_limited(mr, proc, state_dir):
    for _ in range(3):
        mr.dispatch(urlscheme.build_add(titles=["a"] * 150), dry_run=True)
    assert not (state_dir / "ratelimit.json").exists()


# -- re-validation (D3, N2, N8) --

@pytest.mark.parametrize("turl", [
    ThingsURL("delete", (("id", TODO),), 1, False),
    ThingsURL("add-json", (("data", "[]"),), 1, False),
    ThingsURL("empty-trash", (), 0, False),
    ThingsURL("Add", (("title", "a"),), 1, False),
    ThingsURL("add?title=a&x-success=evil://x#", (), 1, False),
    ThingsURL("", (), 0, False),
])
def test_dispatch_rejects_foreign_command(mr, proc, token, turl):
    with pytest.raises(Refused) as e:
        mr.dispatch(turl)
    assert "allowlist" in e.value.message
    assert proc.calls == [] and token == []


@pytest.mark.parametrize("params", [
    (("title", "a"), ("x-success", "evil://done")),
    (("title", "a"), ("X-Error", "evil://")),
    (("title", "a"), ("x-cancel", "")),
    (("x-source", "me"),),
])
def test_dispatch_rejects_x_callback_params(mr, proc, params):
    with pytest.raises(Refused) as e:
        mr.dispatch(ThingsURL("add", params, 1, False))
    assert "x-callback" in e.value.message
    assert proc.calls == []


@pytest.mark.parametrize("command, params", [
    ("add", (("title", "a"), ("auth-token", "smuggled12345"))),      # only the runner appends the token
    ("add", (("title", "a"), ("delete", "true"))),
    ("add", (("title&x-success=evil://x&t", "a"),)),                    # keys are not encoded: injection
    ("add", (("title=a&when", "today"),)),
    ("show", (("id", "today"), ("title", "x"))),
    ("json", (("data", "[]"), ("reveal", "true"), ("id", TODO))),
    ("add", (("title", "a"), ("title", "b"))),
])
def test_dispatch_rejects_undocumented_or_repeated_params(mr, proc, command, params):
    with pytest.raises(Refused):
        mr.dispatch(ThingsURL(command, params, 1, False))
    assert proc.calls == []


@pytest.mark.parametrize("turl", [
    ThingsURL("add", (("title", "a"),), 1, True),                   # would leak the token to add
    ThingsURL("show", (("id", "today"),), 0, True),
    ThingsURL("update", (("id", TODO), ("title", "b")), 1, False),
    ThingsURL("json", (("data", '[{"type":"to-do","operation":"update","id":"%s","attributes":{}}]' % TODO),), 1,
              False),
    ThingsURL("json", (("data", '[{"type":"project","attributes":{"items":[{"operation":"update"}]}}]'),), 1,
              False),
    ThingsURL("json", (("data", '[{"type":"to-do","operation":"create","attributes":{"title":"a"}}]'),), 1, True),
])
def test_dispatch_rejects_token_requirement_mismatch(mr, proc, token, turl):
    with pytest.raises(Refused):
        mr.dispatch(turl)
    assert proc.calls == [] and token == []


@pytest.mark.parametrize("data", ["not json", "{}", '"x"'])
def test_dispatch_rejects_bad_json_payload(mr, proc, data):
    with pytest.raises(Refused):
        mr.dispatch(ThingsURL("json", (("data", data),), 1, False))
    with pytest.raises(Refused):
        mr.dispatch(ThingsURL("json", (), 1, False))
    assert proc.calls == []


def test_all_documented_builders_pass_revalidation(mr, proc, token):
    for turl in [urlscheme.build_add(title="a", when="evening@6pm", tags=["Errand"], checklist_items=["x", "y"]),
                 urlscheme.build_add(titles=["Milk", "Beer"], list_id=PROJ, heading="H"),
                 urlscheme.build_add_project(title="p", area_id=PROJ, deadline="2030-12-31"),
                 urlscheme.build_update(id=TODO, append_checklist_items=["a"], add_tags=["x"], deadline=urlscheme.CLEAR),
                 urlscheme.build_update_project(id=PROJ, prepend_notes="SFO to JFK.", completed=True),
                 urlscheme.build_show(query="vacation", filter=["errand"]),
                 urlscheme.build_version()]:
        mr.dispatch(turl)
    assert len(proc.of(OPEN)) == 7


def test_url_too_long_once_token_appended(mr, proc, token, monkeypatch):
    turl = update_url(title="x" * 50)
    monkeypatch.setattr(urlscheme, "MAX_URL_BYTES", len(turl.url) + 5)
    with pytest.raises(InvalidInput) as e:
        mr.dispatch(turl)
    assert "split" in e.value.hint
    secrets_absent(e.value, TOKEN)
    assert proc.calls == []


def test_open_error_redacts_token(mr, proc, token, monkeypatch):
    def echo_url(argv, kw):
        return done(1, err=f"LSOpenURLsWithRole() failed with error -54 for the URL {argv[-1]}.\n")
    proc.queues[OPEN] = [echo_url]
    with pytest.raises(DispatchFailed) as e:
        mr.dispatch(update_url(title="Buy bread"))
    secrets_absent(e.value, TOKEN)
    assert e.value.message == "`open` failed (exit 1): LSOpenURLsWithRole() failed with error -54 for the URL <url>"


def test_open_error_redacts_token_everywhere(mr, proc, monkeypatch):
    monkeypatch.setenv(authtoken.ENV, ODD_TOKEN)
    proc.queues[OPEN] = [lambda argv, kw: done(1, out=argv[-1], err=f"raw {ODD_TOKEN} {argv}")]
    with pytest.raises(DispatchFailed) as e:
        mr.dispatch(update_url(title="Buy bread"))
    secrets_absent(e.value, ODD_TOKEN)


def test_open_timeout_has_no_argv_or_token(mr, proc, token):
    proc.queues[OPEN] = [times_out]
    with pytest.raises(DispatchFailed) as e:
        mr.dispatch(update_url(title="Buy bread"))
    secrets_absent(e.value, TOKEN)
    assert "things:///" not in str(e.value) and "Buy" not in str(e.value)
    assert "may or may not" in e.value.message
    assert len(proc.of(OPEN)) == 1, "never retried (N10)"


def test_open_cannot_run(mr, proc, token):
    proc.queues[OPEN] = [os_error]
    with pytest.raises(DispatchFailed) as e:
        mr.dispatch(update_url(title="x"))
    secrets_absent(e.value, TOKEN)


@pytest.mark.parametrize("fixture, exc", [("open_-10814", ThingsNotInstalled), ("open_-10810", SandboxBlocked)])
def test_open_error_mapping_from_fixtures(mr, proc, fixture, exc):
    proc.queues[OPEN] = [done(1, err=(FIXTURES / "osa_stderr" / f"{fixture}.txt").read_bytes())]
    with pytest.raises(exc):
        mr.dispatch(urlscheme.build_add(title="Buy milk"))
    assert len(proc.of(OPEN)) == 1


def test_dispatch_is_never_retried(mr, proc):
    proc.queues[OPEN] = [done(1, err="LSOpenURLsWithRole() failed with error -1 for the URL x")]
    with pytest.raises(DispatchFailed):
        mr.dispatch(urlscheme.build_add(title="a"))
    assert len(proc.of(OPEN)) == 1


def test_dispatch_logs_only_redacted(mr, proc, token, caplog):
    caplog.set_level(logging.DEBUG, logger="things_bridge")
    mr.dispatch(update_url(title="x"))
    assert "auth-token=***" in caplog.text and TOKEN not in caplog.text


# -- rate limiting at dispatch --

def test_mutating_dispatch_consumes_weight(mr, proc, state_dir):
    mr.dispatch(urlscheme.build_add(titles=["a", "b", "c"]))
    mr.dispatch(urlscheme.build_add_project(title="p", to_dos=["x", "y"]))
    mr.dispatch(ThingsURL("add", (("title", "a"),), 0, False))       # weight 0 still counts as 1
    events = json.loads((state_dir / "ratelimit.json").read_text())
    assert [n for _, n in events] == [3, 3, 1]
    assert mr.rate_limiter.usage() == 7


def test_dispatch_waits_for_the_window_and_t0_follows(mr, proc, clock):
    start = clock.time()
    mr.dispatch(urlscheme.build_add(titles=["x"] * 150))
    mr.dispatch(urlscheme.build_add(titles=["x"] * 50))
    assert clock.sleeps == []
    d = mr.dispatch(urlscheme.build_add(titles=["x"] * 10))
    assert clock.sleeps == pytest.approx([10.0])
    assert d.t0 == pytest.approx(start + 10.0), "t0 is taken after the wait, just before open"


def test_dispatch_over_budget_refused_before_open(mr, proc):
    with pytest.raises(Refused) as e:
        mr.dispatch(urlscheme.build_add(titles=["x"] * 201))
    assert "split" in e.value.hint
    assert proc.calls == []


# ---- redact (§5.8) -------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("things:///update?id=X&auth-token=abc123&title=y", "things:///update?id=X&auth-token=***&title=y"),
    ("things:///update?id=X&auth-token=abc123", "things:///update?id=X&auth-token=***"),
    ("a AUTH-TOKEN=Secret1 b", "a AUTH-TOKEN=*** b"),
    ("x auth-token%3Dabc%2Bdef&y", "x auth-token%3D***&y"),
    ("x auth-token%3dabc y", "x auth-token%3d*** y"),
    ("'auth-token=abc' \"auth-token=def\"", "'auth-token=***' \"auth-token=***\""),
    ("auth-token=a&auth-token=b", "auth-token=***&auth-token=***"),
    ("auth-token=abc#frag", "auth-token=***#frag"),
    ("no token here", "no token here"),
])
def test_redact_pattern(text, expected):
    assert redact(text) == expected


def test_redact_literal_and_encoded_forms():
    enc = urlscheme.enc(ODD_TOKEN)
    slash_kept = enc.replace("%2F", "/")                     # quote()'s default safe="/"
    text = f"raw={ODD_TOKEN} enc={enc} double={urlscheme.enc(enc)} slash={slash_kept}"
    assert redact(text, ODD_TOKEN) == "raw=*** enc=*** double=*** slash=***"
    assert redact(f"[{TOKEN}]", TOKEN) == "[***]"
    assert redact("nothing", None) == "nothing"


def test_redact_remembers_handled_tokens():
    assert redact(f"x {TOKEN}") == f"x {TOKEN}"
    runner.remember_secret(TOKEN)
    runner.remember_secret(TOKEN)
    assert runner._secrets == (TOKEN,)
    assert redact(f"x {TOKEN} {urlscheme.enc(TOKEN)}") == "x *** ***"
    runner.remember_secret("")
    assert runner._secrets == (TOKEN,)


def test_redact_filter_scrubs_messages_args_and_tracebacks():
    runner.remember_secret(TOKEN)
    stream = __import__("io").StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(runner.RedactFilter())
    logger = logging.getLogger("things_bridge.test_redact")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.warning("url %s", "things:///update?id=X&auth-token=zzzsecret")
        logger.warning("token %s", TOKEN)
        try:
            raise RuntimeError(f"boom auth-token={TOKEN}")
        except RuntimeError:
            logger.exception("failed")
        logger.warning("bad format %s %s", "one")
    finally:
        logger.removeHandler(handler)
    out = stream.getvalue()
    assert "zzzsecret" not in out and TOKEN not in out
    assert "auth-token=***" in out and "token ***" in out and "RuntimeError" in out
    assert "bad format" in out


# ---- map_osa_error / map_open_error (§5.2) --------------------------------------------------------

@pytest.mark.parametrize("code, running, target, exc", [
    (-1743, True, None, AutomationDenied),
    (-1744, True, None, AutomationDenied),
    (-1712, True, None, AutomationTimeout),
    (-600, True, None, SandboxBlocked),
    (-600, False, None, ThingsNotRunning),
    (-600, None, None, ThingsNotRunning),
    (9001, True, None, ThingsNotRunning),
    (-609, True, None, ReadFailed),
    (-10004, False, None, SandboxBlocked),
    (-1728, True, TODO, NotFound),
    (-1728, True, None, NotFound),
    (-1719, True, TODO, NotFound),
    (-1719, True, "", NotFound),
    (-1719, True, None, ReadFailed),
    (-2740, True, None, ReadFailed),
    (-2741, True, None, ReadFailed),
    (-1708, True, None, ReadFailed),
    (9101, True, None, ReadFailed),
    (9002, True, None, InvalidInput),
    (-128, True, None, ReadFailed),
    (-10000, True, None, ReadFailed),
    (None, None, None, ReadFailed),
])
def test_map_osa_error_table(code, running, target, exc):
    e = map_osa_error(code, "Things3 got an error: whatever", running=running, target_id=target)
    assert type(e) is exc
    assert e.details["osa_code"] == code


def test_map_osa_error_messages(monkeypatch):
    monkeypatch.delenv("__CFBundleIdentifier", raising=False)
    denied = map_osa_error(-1743, "", running=True)
    assert "Privacy & Security → Automation" in denied.hint and "tccutil reset AppleEvents" in denied.hint
    monkeypatch.setenv("__CFBundleIdentifier", "com.mitchellh.ghostty")
    assert "com.mitchellh.ghostty" in map_osa_error(-1743, "", running=True).hint
    assert "things-bridge setup" in map_osa_error(-1744, "", running=True).hint
    assert "sandbox.allowAppleEvents" in map_osa_error(-600, "", running=True).hint
    assert map_osa_error(-1728, "", running=True, target_id=TODO).message == \
        f"No item with id {TODO} (wrong id, or it was deleted)."
    assert TODO not in map_osa_error(-1728, "", running=True, target_id="").message
    assert map_osa_error(-1728, "x", running=True).exit_code == 5


def test_map_osa_error_redacts_and_truncates():
    e = map_osa_error(None, "auth-token=abc " + "x" * 1000, running=None)
    assert "abc" not in e.message and len(e.message) < 400


@pytest.mark.parametrize("stderr, exc", [
    ("LSOpenURLsWithRole() failed with error -10814 for the URL things:///add?title=a.", ThingsNotInstalled),
    ("kLSApplicationNotFoundErr", ThingsNotInstalled),
    ("Unable to find application with bundle identifier com.culturedcode.ThingsMac", ThingsNotInstalled),
    ("LSOpenURLsWithRole() failed for the application /Applications/Things3.app with error -10810.",
     SandboxBlocked),
    ("procNotFound: no eligible process with specified descriptor", SandboxBlocked),
    ("_LSOpenURLsWithCompletionHandler() failed with error -600.", SandboxBlocked),
    ("error -10004", SandboxBlocked),
    ("LSOpenURLsWithRole() failed with error -108148 for the URL x", DispatchFailed),
    ("something odd", DispatchFailed),
    ("", DispatchFailed),
])
def test_map_open_error_table(stderr, exc):
    e = map_open_error(1, stderr, running=True)
    assert type(e) is exc
    assert e.details["rc"] == 1


def test_map_open_error_ignores_codes_inside_the_echoed_url():
    stderr = "LSOpenURLsWithRole() failed with error -54 for the URL things:///add?title=-600&notes=-10814."
    e = map_open_error(1, stderr, running=True)
    assert type(e) is DispatchFailed
    assert "title" not in e.message and "<url>" in e.message


def test_map_open_error_generic():
    e = map_open_error(3, "things:///update?id=X&auth-token=zzz", running=False)
    assert "(exit 3)" in e.message and "zzz" not in e.message
    assert e.hint.startswith("Things 3 isn't running.")
    assert "check Things before retrying" in map_open_error(1, "", running=True).hint


# ---- RateLimiter (R-MEX §6) ---------------------------------------------------------------------

@pytest.fixture
def limiter(tmp_path, clock) -> RateLimiter:
    return RateLimiter(tmp_path / "rl" / "ratelimit.json", clock=clock, sleep=clock.sleep)


def test_rate_limiter_defaults():
    rl = RateLimiter(Path("x"))
    assert (rl.budget, rl.window_s) == (200, 10.0)
    assert (runner.RATE_BUDGET, runner.RATE_WINDOW_S) == (200, 10.0)


def test_rate_limiter_150_plus_50_then_10_waits(limiter, clock):
    t0 = clock.time()
    limiter.acquire(150)
    limiter.acquire(50)
    assert clock.sleeps == []
    limiter.acquire(10)
    assert clock.sleeps == pytest.approx([10.0])
    events = json.loads(limiter.path.read_text())
    assert events == [[pytest.approx(t0 + 10), 10]]


def test_rate_limiter_refuses_over_budget(limiter, clock):
    with pytest.raises(Refused) as e:
        limiter.acquire(201)
    assert "split" in e.value.hint and "200" in e.value.hint
    assert clock.sleeps == []
    limiter.acquire(200)                            # exactly the budget is fine


def test_rate_limiter_waits_only_as_long_as_needed(limiter, clock):
    limiter.acquire(100)
    clock.advance(4)
    limiter.acquire(100)
    clock.advance(1)
    limiter.acquire(50)                             # the first 100 expire 5 s later
    assert clock.sleeps == pytest.approx([5.0])
    assert limiter.usage() == 150


def test_rate_limiter_gives_up_after_max_wait(limiter, clock):
    limiter.acquire(200)
    with pytest.raises(RateLimited) as e:
        limiter.acquire(1, max_wait_s=5)
    assert "10.0 s" in e.value.message and "200 of 200" in e.value.message
    assert clock.sleeps == [], "it doesn't sleep when the wait would exceed max_wait_s"
    limiter.acquire(1, max_wait_s=10)
    assert clock.sleeps == pytest.approx([10.0])


def test_rate_limiter_is_shared_through_the_file(tmp_path, clock):
    path = tmp_path / "ratelimit.json"
    a = RateLimiter(path, clock=clock, sleep=clock.sleep)
    b = RateLimiter(path, clock=clock, sleep=clock.sleep)
    a.acquire(190)
    with pytest.raises(RateLimited):
        b.acquire(11, max_wait_s=0)
    b.acquire(10)
    assert a.usage() == b.usage() == 200


def test_rate_limiter_window_slides(limiter, clock):
    limiter.acquire(200)
    clock.advance(9.99)
    assert limiter.usage() == 200
    clock.advance(0.01)
    assert limiter.usage() == 0
    limiter.acquire(200)
    assert clock.sleeps == []


@pytest.mark.parametrize("content", ["not json", "{}", '{"a": 1}', "", "[1, 2, 3]", '[["x", 5], [1, true]]'])
def test_rate_limiter_tolerates_garbage(limiter, clock, content):
    limiter.path.parent.mkdir(parents=True)
    limiter.path.write_text(content)
    limiter.acquire(200)
    assert clock.sleeps == []


def test_rate_limiter_future_stamps_count_as_now(limiter, clock):
    limiter.path.parent.mkdir(parents=True)
    limiter.path.write_text(json.dumps([[clock.time() + 10_000, 150], [clock.time() - 60, 200]]))
    assert limiter.usage() == 150
    limiter.acquire(100)
    assert clock.sleeps == pytest.approx([10.0])


def test_rate_limiter_zero_weight_is_free(limiter):
    limiter.acquire(0)
    assert not limiter.path.exists()


def test_rate_limiter_file_is_private(limiter):
    limiter.acquire(1)
    assert stat.S_IMODE(limiter.path.stat().st_mode) == 0o600


def test_rate_limiter_real_clock_default(tmp_path):
    rl = RateLimiter(tmp_path / "r.json")
    rl.acquire(5)
    [[t, n]] = json.loads((tmp_path / "r.json").read_text())
    assert n == 5 and abs(t - time.time()) < 5


# ---- state_dir ----------------------------------------------------------------------------------

def test_state_dir_from_env(state_dir):
    assert runner.state_dir() == state_dir
    assert state_dir.is_dir() and stat.S_IMODE(state_dir.stat().st_mode) == 0o700


def test_state_dir_default(monkeypatch, tmp_path):
    monkeypatch.delenv(runner.STATE_ENV)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert runner.state_dir() == tmp_path / "Library" / "Caches" / "things-bridge"
    assert (tmp_path / "Library" / "Caches" / "things-bridge").is_dir()


def test_state_dir_expands_user(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(runner.STATE_ENV, "~/tb-state")
    assert runner.state_dir() == tmp_path / "tb-state"


def test_state_dir_keeps_existing_permissions(monkeypatch, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    monkeypatch.setenv(runner.STATE_ENV, str(shared))
    runner.state_dir()
    assert stat.S_IMODE(shared.stat().st_mode) == 0o755


@pytest.mark.parametrize("sub", ["Group Containers/JLMPQHK86H.com.culturedcode.ThingsMac/x",
                                 "Containers/com.culturedcode.ThingsMac/Data/tmp"])
def test_state_dir_never_inside_things_folders(monkeypatch, tmp_path, sub):
    target = tmp_path / "Library" / sub
    monkeypatch.setenv(runner.STATE_ENV, str(target))
    with pytest.raises(InvalidInput):
        runner.state_dir()
    assert not target.exists()


def test_state_dir_symlink_into_things_folder_refused(monkeypatch, tmp_path):
    real = tmp_path / "com.culturedcode.ThingsMac"
    real.mkdir()
    link = tmp_path / "innocent"
    link.symlink_to(real)
    monkeypatch.setenv(runner.STATE_ENV, str(link / "state"))
    with pytest.raises(InvalidInput):
        runner.state_dir()
    assert list(real.iterdir()) == []


def test_macrunner_explicit_state(tmp_path, clock, mac, proc):
    r = MacRunner(clock=clock, state=tmp_path / "explicit")
    r.dispatch(urlscheme.build_add(title="a"))
    assert (tmp_path / "explicit" / "ratelimit.json").exists()
    assert r.state_dir() == tmp_path / "explicit"


# ---- write_lock ---------------------------------------------------------------------------------

def test_write_lock_is_cross_process_and_reentrant(clock, state_dir):
    r = MacRunner(clock=clock)
    with r.write_lock():
        lock = state_dir / "write.lock"
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600
        assert not is_free(lock)
        with r.write_lock():                        # nested use in one thread must not deadlock
            assert not is_free(lock)
        assert not is_free(lock)
        with MacRunner(clock=clock).write_lock():   # another runner in the same thread: same process lock
            pass
    assert is_free(lock)


def test_write_lock_waits_then_gives_up(clock, state_dir):
    fd = hold_externally(state_dir / "write.lock")
    try:
        with pytest.raises(RateLimited) as e:
            with MacRunner(clock=clock).write_lock():
                pytest.fail("must not enter")
    finally:
        os.close(fd)
    assert "in progress" in e.value.message
    assert sum(clock.sleeps) == pytest.approx(runner.WRITE_LOCK_WAIT_S, abs=0.1)
    with MacRunner(clock=clock).write_lock():
        pass


def test_write_lock_released_on_exception(clock, state_dir):
    with pytest.raises(ZeroDivisionError):
        with MacRunner(clock=clock).write_lock():
            raise ZeroDivisionError
    assert is_free(state_dir / "write.lock")


def test_write_lock_serialises_threads(state_dir):
    order = []

    def other():
        with MacRunner().write_lock():
            order.append("other")

    with MacRunner().write_lock():
        t = threading.Thread(target=other)
        t.start()
        t.join(0.2)
        assert t.is_alive(), "the second thread must wait"
        order.append("first")
    t.join(5)
    assert order == ["first", "other"]


def test_write_lock_does_not_need_macos(clock, monkeypatch, proc):
    monkeypatch.setattr(sys, "platform", "linux")
    with MacRunner(clock=clock).write_lock():
        pass
    assert proc.calls == []


# ---- hygiene ------------------------------------------------------------------------------------

def test_every_child_process_is_hygienic(mr, proc, token, monkeypatch):
    """The FakeProc assertions run on every call: exercise each path once more under one roof."""
    monkeypatch.setenv(authtoken.ENV, TOKEN)
    proc.queues[PGREP] = [done(1), done(0)]
    proc.queues[OSASCRIPT] = [done(0, tb1("areas.bin"))]
    mr.ensure_running(allow_launch=True)
    mr.run_template("AREAS")
    mr.dispatch(update_url(title="x"))
    mr.dispatch(urlscheme.build_show(id="today"))
    assert [c.argv[0] for c in proc.calls] == [PGREP, OPEN, PGREP, OSASCRIPT, OPEN, OPEN]
    with_token = [c.argv for c in proc.calls if any(TOKEN in a for a in c.argv)]
    assert with_token == [[OPEN, "-g", update_url(title="x").url + "&auth-token=" + TOKEN]]
    assert all(TOKEN.encode() not in c.kw.get("input", b"") for c in proc.calls)
