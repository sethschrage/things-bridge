"""The only process launcher (§2.6): ``open``, ``osascript`` and ``pgrep`` run here and nowhere else.

This stage defines the interface only: the ``Runner`` protocol that the service, verifier, doctor,
selftest and test fakes program against, plus the ``Dispatch`` result.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, ContextManager, Protocol, Sequence, runtime_checkable

if TYPE_CHECKING:
    from . import applescript, urlscheme

OPEN, OSASCRIPT, PGREP = "/usr/bin/open", "/usr/bin/osascript", "/usr/bin/pgrep"
THINGS_PROCESS = "Things3"                  # CONFIRMED: OFF-AS `tell application "Things3"`; TAPI pgrep -x Things3


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

    def run_template(self, name: str, args: Sequence[str] = (), *, timeout: float | None = None) -> "applescript.Frame":
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


# MacRunner implementation added in stage C (together with RateLimiter, redact, map_osa_error,
# map_open_error and state_dir, per §2.6).
