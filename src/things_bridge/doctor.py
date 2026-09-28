"""Environment diagnosis (§2.10): read-only checks, each one a ``DoctorCheck``.

Things is only ever asked read-only questions (``APP_VERSION``, ``AUTOMATION``, ``PROBE``, ``ROWS``,
``MEMBERSHIP``), and nothing is dispatched. Whether "Enable Things URLs" is on can't be checked without reading
Things' own files (N1), so that check is always a warning that points at ``things-bridge selftest``. The auth
token is only tested against ``TOKEN_RE``; its value never appears in a check, the report or a log line.

The individual checks are public so ``things-bridge setup`` can run them one at a time.
"""

import importlib.metadata
import logging
import os
import platform
import sys
from datetime import date
from pathlib import Path
from typing import Callable

from . import __version__, applescript, authtoken, errors, service, urlscheme
from .errors import AutomationDenied, AutomationTimeout, NotMacOS, Refused, SandboxBlocked
from .models import DoctorCheck, DoctorReport, Item
from .runner import Runner, binaries

log = logging.getLogger(__name__)

URL_SCHEME_VERSION = "2"                # OFF-URL: "The current version of the URL scheme is 2"
AUTOMATION_TIMEOUT_S = 20.0
AUTOMATION_PROMPT_TIMEOUT_S = 130.0     # AUTOMATION waits up to 120 s inside for the consent dialog
MEMBERSHIP_LISTS = ("inbox", "today", "anytime", "upcoming", "someday", "logbook")

URL_SCHEME_DETAIL = ("can't be checked safely (the setting lives in Things' own files); make sure Things → "
                     "Settings → General → Enable Things URLs is on. Run `things-bridge selftest` to confirm "
                     "end-to-end.")
INSTALL_REMEDY = "Install Things 3 for Mac (Mac App Store or culturedcode.com), then run things-bridge doctor again."
TOKEN_REMEDY = ("In Things: Settings → General → Enable Things URLs → Manage → copy the token, then run "
                "`things-bridge token set` in Terminal.")
AUTOMATION_PROMPT = "macOS will ask to let {} control Things; click OK"

# __CFBundleIdentifier (inherited from the host app) -> the name System Settings shows (R-MEX §5.1)
KNOWN_HOSTS = {
    "com.apple.Terminal": "Terminal",
    "com.googlecode.iterm2": "iTerm",
    "com.mitchellh.ghostty": "Ghostty",
    "com.microsoft.VSCode": "Visual Studio Code",
    "dev.warp.Warp-Stable": "Warp",
    "net.kovidgoyal.kitty": "kitty",
    "com.github.wez.wezterm": "WezTerm",
    "com.anthropic.claudefordesktop": "Claude",
}
# $TERM_PROGRAM -> app name, used when no bundle id was inherited
KNOWN_TERMINALS = {
    "Apple_Terminal": "Terminal",
    "iTerm.app": "iTerm",
    "ghostty": "Ghostty",
    "vscode": "Visual Studio Code",
    "WarpTerminal": "Warp",
    "WezTerm": "WezTerm",
}

Notify = Callable[[str], None]


def _check(name: str, status: str, detail: str, remedy: str | None = None) -> DoctorCheck:
    return DoctorCheck(name=name, status=status, detail=detail, remedy=remedy)


def _skip(name: str, why: str) -> DoctorCheck:
    return _check(name, "skip", f"skipped: {why}")


# ---- individual checks -----------------------------------------------------------------------------------

def check_platform(runner: Runner) -> DoctorCheck:
    """1. macOS with the four programs things-bridge runs (``runner.require_macos``)."""
    mac = platform.mac_ver()[0]
    where = f"macOS {mac}" if mac else sys.platform
    try:
        runner.require_macos()
    except NotMacOS as e:
        return _check("platform", "fail", e.message, "things-bridge only works on a Mac with Things 3 installed.")
    machine = platform.machine()
    return _check("platform", "ok", f"{where}{f' ({machine})' if machine else ''}; {', '.join(binaries())} present")


def check_installed(runner: Runner) -> tuple[DoctorCheck, str | None]:
    """2. ``APP_VERSION``: reads the version without launching Things or sending it an event (R-MEX §5.2).

    Returns the check and the Things version (None when it couldn't be read).
    """
    try:
        rows = applescript.probe_results(runner.run_template("APP_VERSION"))
    except errors.ThingsBridgeError as e:
        if isinstance(e, (SandboxBlocked, AutomationDenied, AutomationTimeout)):
            return _check("things_installed", "fail", f"couldn't read Things' version: {e.message}", e.hint), None
        return _check("things_installed", "fail", f"Things 3 doesn't seem to be installed ({e.message})",
                      INSTALL_REMEDY), None
    version = next((d[0] for f, ok, d in rows if f == "app-version" and ok and d and d[0]), None)
    if version is None:
        return _check("things_installed", "fail", "Things 3 doesn't seem to be installed (no version)",
                      INSTALL_REMEDY), None
    return _check("things_installed", "ok", f"Things {version}"), version


def check_running(runner: Runner, *, allow_launch: bool) -> DoctorCheck:
    """3. ``pgrep``; launches Things hidden when it's down and launching is allowed."""
    try:
        if runner.things_running():
            return _check("things_running", "ok", "Things 3 is running")
        if not allow_launch:
            return _check("things_running", "warn", "Things 3 is not running (auto-launch is disabled)",
                          "Open Things 3, then run things-bridge doctor again.")
        launched = runner.ensure_running(allow_launch=True)
    except errors.ThingsBridgeError as e:
        return _check("things_running", "fail", e.message, e.hint)
    return _check("things_running", "ok",
                  "Things 3 wasn't running; launched it in the background" if launched else "Things 3 is running")


def check_automation(runner: Runner, *, prompt: bool = False, grantee: str | None = None,
                     notify: Notify | None = None) -> DoctorCheck:
    """4. ``AUTOMATION`` (``count of areas``): granted, denied (-1743) or pending (-1712 / no answer).

    With ``prompt`` the wait is long enough for the macOS consent dialog, and ``notify`` tells the user first.
    """
    who = grantee or host_identity()["grantee"]
    if prompt:
        (notify or _log_notify)(AUTOMATION_PROMPT.format(who))
    timeout = AUTOMATION_PROMPT_TIMEOUT_S if prompt else AUTOMATION_TIMEOUT_S
    try:
        applescript.probe_results(runner.run_template("AUTOMATION", timeout=timeout))
    except AutomationDenied as e:
        return _check("automation", "fail", f"denied: {e.message}", e.hint)
    except AutomationTimeout:
        remedy = ("Click OK on the macOS dialog (it may be behind other windows), then run things-bridge doctor "
                  "again." if prompt else
                  "Run `things-bridge doctor --prompt-automation` (or `things-bridge setup`) and click OK on the "
                  "macOS dialog.")
        return _check("automation", "warn", f"no answer within {timeout:g} s: a macOS permission dialog may be "
                                            "waiting, or Things is busy", remedy)
    except errors.ThingsBridgeError as e:
        return _check("automation", "fail", e.message, e.hint)
    return _check("automation", "ok", f"granted: {who} may control Things (read-only use)")


def host_identity() -> dict[str, str]:
    """5. Who macOS asks about Automation (R-MEX §5.1). Informational; the Claude Desktop case is UNCERTAIN."""
    exe = sys.executable or ""
    try:
        resolved = str(Path(exe).resolve()) if exe else ""
    except OSError:
        resolved = exe
    bundle = os.environ.get("__CFBundleIdentifier", "")
    term = os.environ.get("TERM_PROGRAM", "")
    if bundle:
        grantee = KNOWN_HOSTS.get(bundle, bundle)
    elif term:
        grantee = KNOWN_TERMINALS.get(term, term)
    else:                                   # no host app: the binary itself is the responsible process
        grantee = Path(resolved).name or "python"
    return {"executable": exe, "executable_resolved": resolved, "bundle_id": bundle, "term_program": term,
            "parent_pid": str(os.getppid()), "grantee": grantee}


def check_host(host: dict[str, str]) -> DoctorCheck:
    parts = [f"likely Automation grantee: {host['grantee']}",
             f"host app: {host['bundle_id'] or 'none inherited'}",
             f"python: {host['executable_resolved'] or host['executable'] or 'unknown'}"]
    return _check("host", "ok", "; ".join(parts))


def check_token() -> DoctorCheck:
    """6. Where the token is and whether it has the expected format. The value is never shown."""
    try:
        source = authtoken.token_source()
        if source == "missing":
            return _check("token", "warn", "no auth token: creating items works, but updates, status changes, "
                                           "moves and tags need one", TOKEN_REMEDY)
        valid = authtoken.is_valid_token(authtoken.get_token() or "")
    except NotMacOS:
        return _skip("token", "the Keychain is only available on macOS")
    except errors.ThingsBridgeError as e:
        return _check("token", "fail", e.message, e.hint)
    where = authtoken.ENV if source == "env" else "the login Keychain"
    if not valid:
        extra = f" (or fix {authtoken.ENV})" if source == "env" else ""
        return _check("token", "fail", f"the token in {where} doesn't look like a Things URL auth token",
                      TOKEN_REMEDY + extra)
    return _check("token", "ok", f"found in {where}; format valid (the value is never shown)")


def check_url_scheme() -> DoctorCheck:
    """7. Can't be read without touching Things' files (N1): always a warning."""
    return _check("url_scheme", "warn", URL_SCHEME_DETAIL)


def _probe_detail(feature: str, ok: bool, d: list[str]) -> str:
    if feature == "date-columns-aligned" and len(d) == 3 and all(x.isdigit() for x in d):
        counts = f"ids {d[0]}, due dates {d[1]}, start dates {d[2]}"
        return counts if ok else f"NOT aligned: {counts}"
    if not ok:
        if len(d) >= 2 and d[-1].lstrip("-").isdigit():         # message, error number
            return f"failed: {' '.join(x for x in d[:-1] if x)} ({d[-1]})"
        return "failed: " + (" ".join(x for x in d if x) or "no detail")
    if feature == "version":
        return f"Things {d[0]}" if d else "ok"
    if feature.startswith("list:") and len(d) >= 2:
        return f"'{d[0]}' ({d[1]} to-dos)"
    if feature == "range":
        return "`to dos 1 thru 1` of the Logbook works"
    if feature == "list-projects" and d:
        return f"`projects of list` works ({d[0]} in Someday)"
    if feature == "app-todos-include-projects" and len(d) >= 2:
        return f"app `to dos`: {d[0]}, of which projects: {d[1]}"
    if feature == "todo-id-resolves-project" and d:
        return f"`to do id` of a project gives class {d[0]}"
    return " ".join(d) or "ok"


def probe_checks(runner: Runner, project_id: str | None = None) -> list[DoctorCheck]:
    """8. ``PROBE``: one check per ``K`` row. A failed row is a warning: the features are UNCERTAIN (§6.1)."""
    try:
        rows = applescript.probe_results(runner.run_template("PROBE", [project_id] if project_id else []))
    except errors.ThingsBridgeError as e:
        return [_check("probe", "fail", e.message, e.hint)]
    return [_check(f"probe:{feature}", "ok" if ok else "warn", _probe_detail(feature, ok, d))
            for feature, ok, d in rows]


def check_repeating(runner: Runner, id: str, *, allow_launch: bool = True,
                    today: Callable[[], date] | None = None) -> DoctorCheck:
    """9. Would the repeating-template guard (§2.9, N11) refuse schedule/status changes to ``id``?

    ``id`` is one of the user's repeating to-dos (Share → Copy Link); the expected answer is "refused".
    """
    name = "check_repeating"
    id = urlscheme.check_id("check_repeating_id", id)
    try:
        frame = runner.run_template("ROWS", ["0", id])
        items = {it.id: it for it in applescript.items_from_frame(frame, notes_mode="none")}
        if id in {a.id for a in applescript.areas_from_frame(frame)}:
            return _check(name, "fail", f"{id} is an area, not a to-do",
                          "Pass the id of a repeating to-do (in Things: Share → Copy Link).")
        if id not in items:
            return _check(name, "fail", f"No item with id {id} (wrong id, or it was deleted).",
                          "Pass the id of a repeating to-do (in Things: Share → Copy Link).")
        item = items[id]
        members = applescript.membership_from_frame(runner.run_template("MEMBERSHIP", list(MEMBERSHIP_LISTS)))
        lists = [k for k in MEMBERSHIP_LISTS if id in members.get(k, ())]
        refused = _guard_refuses(runner, item, allow_launch=allow_launch, today=today)
    except errors.ThingsBridgeError as e:
        return _check(name, "fail", e.message, e.hint)
    title = item.title if len(item.title) <= 60 else item.title[:60] + "…"
    shown = ", ".join(lists) or "no list"
    if item.status != "open":
        return _check(name, "warn", f"'{title}' is {item.status}; the guard only protects open items",
                      "Pass the id of an open repeating to-do (in Things: Share → Copy Link).")
    if refused:
        return _check(name, "ok", f"'{title}' shows in {shown}, so the guard refuses to change its schedule, "
                                  "deadline or status (as expected for a repeating to-do's template)")
    start = f", start date {item.start_date}" if item.start_date else ""
    return _check(name, "warn", f"the guard would NOT refuse '{title}': it shows in {shown}{start}",
                  "If this is a repeating to-do, the id may be one of its instances rather than the template. "
                  "Change repeating to-dos in Things, not through things-bridge.")


def _guard_refuses(runner: Runner, item: Item, *, allow_launch: bool, today: Callable[[], date] | None) -> bool:
    """Run the service's own guard (not a copy of its rules), as a ``when`` change would."""
    svc = service.ThingsService(runner, service.Config(allow_launch=allow_launch), today=today or date.today)
    try:
        svc._guard_repeating(service._Call(svc), [item], {"when"})
    except Refused:
        return True
    return False


def versions(things_version: str | None = None) -> dict[str, str]:
    """10. things-bridge, Python, mcp (installed metadata only; mcp itself is never imported here), Things and
    the URL scheme."""
    try:
        mcp_version = importlib.metadata.version("mcp")
    except importlib.metadata.PackageNotFoundError:
        mcp_version = "not installed"
    return {"things_bridge": __version__, "python": platform.python_version(), "mcp": mcp_version,
            "things": things_version or "unknown", "url_scheme": URL_SCHEME_VERSION}


def check_versions(v: dict[str, str]) -> DoctorCheck:
    return _check("versions", "ok", f"things-bridge {v['things_bridge']}, Python {v['python']}, mcp {v['mcp']}, "
                                    f"Things {v['things']}, URL scheme {v['url_scheme']}")


def check_rate(runner: Runner) -> DoctorCheck:
    """11. Items sent in the current rate window (shared by every things-bridge process)."""
    limiter = getattr(runner, "rate_limiter", None)
    if limiter is None or not hasattr(limiter, "usage"):
        return _skip("rate_limiter", "this runner keeps no rate log")
    try:
        used = limiter.usage()
    except (OSError, ValueError, errors.ThingsBridgeError) as e:
        return _check("rate_limiter", "warn", f"couldn't read the rate log: {type(e).__name__}")
    return _check("rate_limiter", "ok", f"{used} of {limiter.budget} items sent in the last {limiter.window_s:g} s "
                                        "(Things accepts 250 per 10 s)")


def _log_notify(message: str) -> None:
    log.warning("%s", message)


# ---- the report ------------------------------------------------------------------------------------------

def run_doctor(runner: Runner, *, allow_launch: bool = True, prompt_automation: bool = False, probe: bool = False,
               check_repeating_id: str | None = None, notify: Notify | None = None,
               today: Callable[[], date] | None = None) -> DoctorReport:
    """Run §2.10's checks in order. ``ok`` is False when any check failed.

    ``notify`` receives the "click OK" instruction before a prompting Automation check (default: a log warning
    on stderr, never stdout). A malformed ``check_repeating_id`` raises ``InvalidInput`` before anything runs.
    """
    if check_repeating_id is not None:
        check_repeating_id = urlscheme.check_id("check_repeating_id", check_repeating_id)
    host = host_identity()
    plat = check_platform(runner)
    checks: list[DoctorCheck] = [plat]
    mac = plat.status == "ok"
    why = None if mac else "not macOS"         # why the checks that talk to Things are skipped
    things_version = None

    if why is None:
        installed, things_version = check_installed(runner)
        checks.append(installed)
        why = None if installed.status == "ok" else "Things 3 isn't installed or can't be reached"
    else:
        checks.append(_skip("things_installed", why))
    if why is None:
        running = check_running(runner, allow_launch=allow_launch)
        checks.append(running)
        why = None if running.status == "ok" else "Things 3 isn't running"
    else:
        checks.append(_skip("things_running", why))
    if why is None:
        automation = check_automation(runner, prompt=prompt_automation, grantee=host["grantee"], notify=notify)
        checks.append(automation)
        why = None if automation.status == "ok" else "Automation access isn't confirmed"
    else:
        checks.append(_skip("automation", why))

    checks += [check_host(host), check_token(), check_url_scheme()]
    if probe:
        checks += probe_checks(runner) if why is None else [_skip("probe", why)]
    if check_repeating_id is not None:
        checks.append(check_repeating(runner, check_repeating_id, allow_launch=allow_launch, today=today)
                      if why is None else _skip("check_repeating", why))
    v = versions(things_version)
    checks.append(check_versions(v))
    checks.append(check_rate(runner) if mac else _skip("rate_limiter", "not macOS"))
    return DoctorReport(ok=all(c.status != "fail" for c in checks), checks=checks, versions=v, host=host)
