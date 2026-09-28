"""Exception taxonomy (§2.1).

Every error carries a stable machine ``code`` (CLI ``--json`` output, MCP ``ToolError`` text as
``[code] message``) and a process ``exit_code`` (§2.12). Pure module: no I/O.
"""

from typing import Any


class ThingsBridgeError(Exception):
    code: str = "error"
    exit_code: int = 1

    def __init__(self, message: str, *, hint: str | None = None, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details: dict = dict(details) if details else {}

    def to_dict(self) -> dict[str, Any]:
        """The ``{"code", "message", "hint", "details"}`` error object (§2.12)."""
        return {"code": self.code, "message": self.message, "hint": self.hint, "details": self.details}

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.message!r})"


class InvalidInput(ThingsBridgeError):
    code, exit_code = "invalid_input", 2


class NotFound(ThingsBridgeError):
    code, exit_code = "not_found", 5


class Ambiguous(ThingsBridgeError):
    """``details["candidates"]`` lists the matches."""
    code, exit_code = "ambiguous", 5


class Refused(ThingsBridgeError):
    """A safety guard, the policy, or a missing ``confirm`` stopped the operation."""
    code, exit_code = "refused", 4


class NotMacOS(ThingsBridgeError):
    code, exit_code = "not_macos", 3


class ThingsNotInstalled(ThingsBridgeError):
    code, exit_code = "things_not_installed", 3


class ThingsNotRunning(ThingsBridgeError):
    code, exit_code = "things_not_running", 3


class AutomationDenied(ThingsBridgeError):
    """osascript -1743 / -1744."""
    code, exit_code = "automation_denied", 3


class AutomationTimeout(ThingsBridgeError):
    """osascript -1712 or a subprocess timeout."""
    code, exit_code = "automation_timeout", 3


class SandboxBlocked(ThingsBridgeError):
    """-600 / -10810 / -10004 while Things is running."""
    code, exit_code = "sandbox_blocked", 3


class TokenMissing(ThingsBridgeError):
    code, exit_code = "token_missing", 3


class KeychainError(ThingsBridgeError):
    code, exit_code = "keychain_error", 3


class RateLimited(ThingsBridgeError):
    code, exit_code = "rate_limited", 1


class DispatchFailed(ThingsBridgeError):
    code, exit_code = "dispatch_failed", 1


class ReadFailed(ThingsBridgeError):
    """``details["osa_code"]`` holds the osascript error number when known."""
    code, exit_code = "read_failed", 1


class ProtocolError(ThingsBridgeError):
    """Malformed or truncated tb1 frame."""
    code, exit_code = "protocol_error", 1


EXIT_NOT_VERIFIED = 6   # a write was sent but read-back did not confirm it (not an exception)
