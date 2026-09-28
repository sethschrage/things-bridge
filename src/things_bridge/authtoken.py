"""The Things URL auth token (§2.7): the login Keychain via ``/usr/bin/security``; ``THINGS_AUTH_TOKEN`` wins.

``security`` is used for reads and writes alike, so the item's ACL trusts a stable Apple binary and reads never
prompt (R-MEX §4.1). The token reaches ``security`` only on stdin (``security -i``) and comes back only on
stdout; it is never in any argv, message or log line. There is no Keychain deletion here (D11): ``set_token``
overwrites (``-U``).
"""

import os
import re
import subprocess
import sys
from typing import Literal

from .errors import InvalidInput, KeychainError, NotMacOS

SECURITY = "/usr/bin/security"
SERVICE = "things-bridge"
ACCOUNT = "things-url-auth-token"
LABEL = "Things URL auth token"
ENV = "THINGS_AUTH_TOKEN"
TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{8,256}$")   # observed tokens: 22 base62 chars (R-MEX §4.1, one sample)
NOT_FOUND_RC = 44                           # errSecItemNotFound (-25300) & 0xFF (R-MEX §4.1, Security sources)
READ_TIMEOUT_S, WRITE_TIMEOUT_S = 10.0, 15.0

_READ_HINT = "keychain locked? run from a logged-in GUI session"
# Without a value at the end, `-w` makes security prompt for the token itself, so it stays out of argv.
MANUAL_COMMAND = f'security add-generic-password -U -s {SERVICE} -a {ACCOUNT} -l "{LABEL}" -w'
_WRITE_HINT = ("Is the login keychain locked (SSH session)? Run it from a logged-in GUI session, or store the "
               f"token by hand with `{MANUAL_COMMAND}` (security prompts for it).")


def _redact(text: str, token: str | None = None) -> str:
    from .runner import redact             # runner imports this module at load time
    return redact(text, token)


def _excerpt(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise NotMacOS(f"things-bridge keeps the auth token in the macOS Keychain; this system is {sys.platform}")


def _security(args: list[str], *, timeout: float,
              input: bytes | None = None) -> "subprocess.CompletedProcess[bytes] | str":
    """Run ``security``; a failure to run comes back as a string, so no exception text (argv) is chained."""
    io: dict = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    env = {k: v for k, v in os.environ.items() if k != ENV}
    try:
        return subprocess.run([SECURITY, *args], capture_output=True, timeout=timeout, env=env, **io)
    except subprocess.TimeoutExpired:
        return f"timed out after {timeout:g} s (a Keychain dialog may be waiting)"
    except OSError as e:
        return f"couldn't run: {e.strerror or type(e).__name__}"


def _env_token() -> str | None:
    return os.environ.get(ENV, "").strip() or None


def _keychain_token() -> str | None:
    _require_macos()
    res = _security(["find-generic-password", "-s", SERVICE, "-a", ACCOUNT, "-w"], timeout=READ_TIMEOUT_S)
    if isinstance(res, str):
        raise KeychainError(f"Couldn't read the Keychain: security {res}.", hint=_READ_HINT)
    if res.returncode == 0:
        return res.stdout.decode("utf-8", "replace").rstrip("\n") or None
    if res.returncode == NOT_FOUND_RC:
        return None
    err = _excerpt(_redact(res.stderr.decode("utf-8", "replace")))     # never stdout: it could hold the secret
    raise KeychainError(f"Couldn't read the Keychain (locked? SSH session?): security exited {res.returncode}"
                        + (f": {err}" if err else "."), hint=_READ_HINT, details={"rc": res.returncode})


def is_valid_token(token: str) -> bool:
    return isinstance(token, str) and TOKEN_RE.fullmatch(token) is not None


def token_source() -> Literal["env", "keychain", "missing"]:
    """Where ``get_token()`` would find the token. Never returns or prints the token itself."""
    if _env_token():
        return "env"
    return "keychain" if _keychain_token() else "missing"


def get_token() -> str | None:
    """``THINGS_AUTH_TOKEN`` (stripped, non-empty), else the Keychain item; ``None`` if neither is set."""
    return _env_token() or _keychain_token()


def set_token(token: str) -> None:
    """Validate ``token`` and store it in the login Keychain (overwriting), then read it back to confirm."""
    token = token.strip() if isinstance(token, str) else token
    if not is_valid_token(token):
        raise InvalidInput("That doesn't look like a Things URL auth token.",
                           hint=("Copy it from Things → Settings → General → Enable Things URLs → Manage "
                                 "(8-256 characters: letters, digits and . _ ~ + / = -)."),
                           details={"field": "token"})
    _require_macos()
    from .runner import remember_secret
    remember_secret(token)
    # TOKEN_RE admits no quote, backslash or whitespace, so this line needs no escaping.
    line = f'add-generic-password -U -s "{SERVICE}" -a "{ACCOUNT}" -l "{LABEL}" -w "{token}"\n'
    res = _security(["-i"], input=line.encode("ascii"), timeout=WRITE_TIMEOUT_S)
    if isinstance(res, str):
        raise KeychainError(f"Couldn't write the Keychain: security {res}.", hint=_WRITE_HINT)
    err = _excerpt(_redact(res.stderr.decode("utf-8", "replace"), token))
    # interactive mode reports a failed command as "<command>: returned <status>" on stderr
    if res.returncode != 0 or re.search(r"returned -?\d+", err):
        raise KeychainError(f"Couldn't write the Keychain (security exited {res.returncode})"
                            + (f": {err}" if err else "."), hint=_WRITE_HINT, details={"rc": res.returncode})
    if _keychain_token() != token:
        raise KeychainError("The token was written to the Keychain but doesn't read back the same.",
                            hint=_WRITE_HINT)
