"""doctor.py (DESIGN §2.10) against tests/fakes.py: every check, their order, skips, and that doctor is read-only.

The real Keychain is never touched: ``authtoken._keychain_token`` is replaced for every test.
"""

import dataclasses
import json
import os
import subprocess
import sys
from datetime import date
from importlib import metadata
from pathlib import Path

import pytest

from fakes import FAKE_TOKEN, FakeRunner
from things_bridge import authtoken, doctor
from things_bridge.errors import InvalidInput, KeychainError, NotMacOS, ReadFailed, SandboxBlocked
from things_bridge.runner import RateLimiter

BASE = ["platform", "things_installed", "things_running", "automation", "host", "token", "url_scheme", "versions",
        "rate_limiter"]
READ_TEMPLATES = {"APP_VERSION", "AUTOMATION", "PROBE", "ROWS", "MEMBERSHIP"}
GHOST = "GhostGhostGhostGhost01"
URL_SCHEME_TEXT = ("can't be checked safely (the setting lives in Things' own files); make sure Things → Settings → "
                   "General → Enable Things URLs is on. Run `things-bridge selftest` to confirm end-to-end.")


# ---- fixtures ------------------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def no_keychain(monkeypatch):
    """By default there is no Keychain (as on Linux), whatever OS runs the tests."""
    def refuse():
        raise NotMacOS("things-bridge keeps the auth token in the macOS Keychain; this system is linux")
    monkeypatch.setattr(authtoken, "_keychain_token", refuse)


@pytest.fixture
def keychain(monkeypatch):
    """A login Keychain holding ``store["token"]`` (None: no item)."""
    store = {"token": FAKE_TOKEN}
    monkeypatch.setattr(authtoken, "_keychain_token", lambda: store["token"])
    return store


@pytest.fixture(autouse=True)
def read_only(fake_runner, fake_things):
    """Doctor never dispatches a URL and only runs read templates."""
    yield
    assert fake_runner.dispatch_calls == []
    assert fake_things.log == []
    assert {c.name for c in fake_runner.template_calls} <= READ_TEMPLATES


def names(report) -> list[str]:
    return [c.name for c in report.checks]


def checks(report) -> dict:
    return {c.name: c for c in report.checks}


def calls(runner) -> list[str]:
    return [c.name for c in runner.template_calls]


# ---- the full report --------------------------------------------------------------------------------------------

def test_healthy_mac(fake_runner, fake_things, keychain):
    report = doctor.run_doctor(fake_runner)
    assert names(report) == BASE
    status = {c.name: c.status for c in report.checks}
    assert status == {"platform": "ok", "things_installed": "ok", "things_running": "ok", "automation": "ok",
                      "host": "ok", "token": "ok", "url_scheme": "warn", "versions": "ok", "rate_limiter": "skip"}
    assert report.ok is True
    assert checks(report)["things_installed"].detail == f"Things {fake_things.version}"
    assert calls(fake_runner) == ["APP_VERSION", "AUTOMATION"]
    assert fake_runner.calls("AUTOMATION")[0].timeout == doctor.AUTOMATION_TIMEOUT_S == 20.0
    assert fake_runner.launches == 0


def test_order_with_every_option(fake_runner, fake_things, keychain):
    tid = fake_things.seed_todo("Water plants", repeating=True)
    report = doctor.run_doctor(fake_runner, probe=True, check_repeating_id=tid)
    got = names(report)
    probes = [n for n in got if n.startswith("probe:")]
    assert got == BASE[:7] + probes + ["check_repeating"] + BASE[7:]
    assert probes[0] == "probe:version" and "probe:list:inbox" in probes


def test_report_is_json_serialisable(fake_runner, keychain):
    data = json.loads(json.dumps(dataclasses.asdict(doctor.run_doctor(fake_runner, probe=True))))
    assert set(data) == {"ok", "checks", "versions", "host"}
    assert set(data["checks"][0]) == {"name", "status", "detail", "remedy"}


def test_warnings_dont_fail_but_failures_do(fake_runner, keychain):
    keychain["token"] = None                                   # token: warn
    assert doctor.run_doctor(fake_runner).ok is True
    fake_runner.inject("AUTOMATION", osa_code=-1743)          # automation: fail
    assert doctor.run_doctor(fake_runner).ok is False


# ---- 1. platform ---------------------------------------------------------------------------------------------

def test_not_macos_skips_everything_that_needs_things(fake_things, clock):
    r = FakeRunner(fake_things, clock=clock, macos=False)
    report = doctor.run_doctor(r, probe=True, check_repeating_id=GHOST)
    c = checks(report)
    assert c["platform"].status == "fail" and "macOS" in c["platform"].detail and c["platform"].remedy
    for name in ("things_installed", "things_running", "automation", "probe", "check_repeating", "rate_limiter"):
        assert c[name].status == "skip" and c[name].detail == "skipped: not macOS", name
    assert c["token"].status == "skip"
    assert c["versions"].status == "ok" and report.versions["things"] == "unknown"
    assert report.ok is False
    assert r.template_calls == [] and r.dispatch_calls == []


def test_platform_lists_the_four_programs(fake_runner):
    detail = checks(doctor.run_doctor(fake_runner))["platform"].detail
    for path in ("/usr/bin/open", "/usr/bin/osascript", "/usr/bin/pgrep", "/usr/bin/security"):
        assert path in detail


# ---- 2. installed --------------------------------------------------------------------------------------------

def test_things_not_installed(fake_runner, fake_things):
    fake_things.installed = False
    report = doctor.run_doctor(fake_runner, probe=True)
    c = checks(report)
    assert c["things_installed"].status == "fail"
    assert "doesn't seem to be installed" in c["things_installed"].detail
    assert c["things_installed"].remedy == doctor.INSTALL_REMEDY
    for name in ("things_running", "automation", "probe"):
        assert c[name].status == "skip" and "isn't installed" in c[name].detail
    assert report.ok is False and fake_runner.launches == 0
    assert calls(fake_runner) == ["APP_VERSION"]


def test_sandbox_is_not_reported_as_not_installed(fake_runner):
    fake_runner.inject("APP_VERSION", exc=SandboxBlocked("Apple Events are blocked", hint="allow Apple Events"))
    c = checks(doctor.run_doctor(fake_runner))["things_installed"]
    assert c.status == "fail" and "couldn't read Things' version" in c.detail and c.remedy == "allow Apple Events"


def test_things_version_flows_into_versions(fake_runner, fake_things):
    fake_things.version = "3.23.4"
    report = doctor.run_doctor(fake_runner)
    assert report.versions["things"] == "3.23.4"
    assert "Things 3.23.4" in checks(report)["versions"].detail


# ---- 3. running ----------------------------------------------------------------------------------------------

def test_launches_things_hidden_when_allowed(fake_runner, fake_things):
    fake_things.running = False
    c = checks(doctor.run_doctor(fake_runner))
    assert c["things_running"].status == "ok" and "launched it in the background" in c["things_running"].detail
    assert fake_runner.launches == 1 and c["automation"].status == "ok"


def test_no_launch_warns_and_skips_automation(fake_runner, fake_things):
    fake_things.running = False
    report = doctor.run_doctor(fake_runner, allow_launch=False, probe=True)
    c = checks(report)
    assert c["things_running"].status == "warn" and "auto-launch is disabled" in c["things_running"].detail
    assert c["automation"].status == "skip" and c["automation"].detail == "skipped: Things 3 isn't running"
    assert c["probe"].status == "skip"
    assert fake_runner.launches == 0 and "AUTOMATION" not in calls(fake_runner)


def test_running_check_error_is_a_failure(fake_runner, monkeypatch):
    def broken():
        raise ReadFailed("Couldn't check whether Things is running: pgrep exited 3.")
    monkeypatch.setattr(fake_runner, "things_running", broken)
    c = checks(doctor.run_doctor(fake_runner))
    assert c["things_running"].status == "fail" and "pgrep" in c["things_running"].detail
    assert c["automation"].status == "skip"


# ---- 4. automation -------------------------------------------------------------------------------------------

def test_automation_denied(fake_runner):
    fake_runner.inject("AUTOMATION", osa_code=-1743)
    report = doctor.run_doctor(fake_runner, probe=True, check_repeating_id=GHOST)
    c = checks(report)
    assert c["automation"].status == "fail" and c["automation"].detail.startswith("denied:")
    assert "Privacy & Security" in c["automation"].remedy
    for name in ("probe", "check_repeating"):
        assert c[name].status == "skip" and "Automation access isn't confirmed" in c[name].detail
    assert report.ok is False


def test_automation_pending(fake_runner):
    fake_runner.inject("AUTOMATION", timeout=True)
    c = checks(doctor.run_doctor(fake_runner))["automation"]
    assert c.status == "warn" and "no answer within 20 s" in c.detail
    assert "--prompt-automation" in c.remedy


def test_prompt_automation_notifies_then_waits_130s(fake_runner, monkeypatch):
    monkeypatch.setenv("__CFBundleIdentifier", "com.apple.Terminal")
    said = []
    report = doctor.run_doctor(fake_runner, prompt_automation=True, notify=said.append)
    assert said == ["macOS will ask to let Terminal control Things; click OK"]
    assert fake_runner.calls("AUTOMATION")[0].timeout == doctor.AUTOMATION_PROMPT_TIMEOUT_S == 130.0
    assert checks(report)["automation"].detail == "granted: Terminal may control Things (read-only use)"


def test_prompt_timeout_remedy(fake_runner):
    fake_runner.inject("AUTOMATION", timeout=True)
    c = checks(doctor.run_doctor(fake_runner, prompt_automation=True, notify=lambda m: None))["automation"]
    assert c.status == "warn" and "no answer within 130 s" in c.detail and "Click OK" in c.remedy


def test_default_notify_goes_to_the_log_not_stdout(fake_runner, capsys, caplog):
    caplog.set_level("WARNING", logger="things_bridge.doctor")
    doctor.run_doctor(fake_runner, prompt_automation=True)
    assert capsys.readouterr().out == ""
    assert any("click OK" in r.getMessage() for r in caplog.records)


def test_no_notice_without_prompt(fake_runner):
    said = []
    doctor.run_doctor(fake_runner, notify=said.append)
    assert said == []


def test_automation_other_error(fake_runner):
    fake_runner.inject("AUTOMATION", osa_code=-600)            # Things running: a sandbox
    c = checks(doctor.run_doctor(fake_runner))["automation"]
    assert c.status == "fail" and "sandbox" in (c.remedy or "").lower()


# ---- 5. host identity ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("env, grantee", [
    ({"__CFBundleIdentifier": "com.apple.Terminal"}, "Terminal"),
    ({"__CFBundleIdentifier": "com.googlecode.iterm2", "TERM_PROGRAM": "vscode"}, "iTerm"),
    ({"__CFBundleIdentifier": "com.example.Unknown"}, "com.example.Unknown"),
    ({"TERM_PROGRAM": "ghostty"}, "Ghostty"),
    ({"TERM_PROGRAM": "SomeTerm"}, "SomeTerm"),
])
def test_host_identity_grantee(monkeypatch, env, grantee):
    monkeypatch.delenv("__CFBundleIdentifier", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    host = doctor.host_identity()
    assert host["grantee"] == grantee
    assert host["bundle_id"] == env.get("__CFBundleIdentifier", "")


def test_host_identity_without_host_app_names_the_interpreter(monkeypatch):
    monkeypatch.delenv("__CFBundleIdentifier", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    host = doctor.host_identity()
    assert host["grantee"] == Path(sys.executable).resolve().name
    assert host["executable"] == sys.executable
    assert host["executable_resolved"] == str(Path(sys.executable).resolve())
    assert host["parent_pid"] == str(os.getppid())
    assert all(isinstance(v, str) for v in host.values())


def test_report_carries_host(fake_runner, monkeypatch):
    monkeypatch.setenv("__CFBundleIdentifier", "com.mitchellh.ghostty")
    report = doctor.run_doctor(fake_runner)
    assert report.host == doctor.host_identity()
    detail = checks(report)["host"].detail
    assert "likely Automation grantee: Ghostty" in detail and "com.mitchellh.ghostty" in detail


# ---- 6. token ------------------------------------------------------------------------------------------------

def _no_token_anywhere(report, token):
    assert token not in json.dumps(dataclasses.asdict(report))


def test_token_from_env(fake_runner, monkeypatch):
    monkeypatch.setenv(authtoken.ENV, FAKE_TOKEN)
    report = doctor.run_doctor(fake_runner)
    c = checks(report)["token"]
    assert c.status == "ok" and authtoken.ENV in c.detail and "never shown" in c.detail
    _no_token_anywhere(report, FAKE_TOKEN)


def test_token_from_keychain(fake_runner, keychain):
    report = doctor.run_doctor(fake_runner)
    c = checks(report)["token"]
    assert c.status == "ok" and "login Keychain" in c.detail
    _no_token_anywhere(report, FAKE_TOKEN)


def test_token_missing_is_a_warning(fake_runner, keychain):
    keychain["token"] = None
    c = checks(doctor.run_doctor(fake_runner))["token"]
    assert c.status == "warn" and c.remedy == doctor.TOKEN_REMEDY and "token set" in c.remedy


def test_invalid_env_token(fake_runner, monkeypatch):
    bad = "not a token!"
    monkeypatch.setenv(authtoken.ENV, bad)
    report = doctor.run_doctor(fake_runner)
    c = checks(report)["token"]
    assert c.status == "fail" and "doesn't look like" in c.detail and authtoken.ENV in c.remedy
    _no_token_anywhere(report, bad)
    assert report.ok is False


def test_keychain_error(fake_runner, monkeypatch):
    def locked():
        raise KeychainError("Couldn't read the Keychain (locked? SSH session?)", hint="unlock it")
    monkeypatch.setattr(authtoken, "_keychain_token", locked)
    c = checks(doctor.run_doctor(fake_runner))["token"]
    assert c.status == "fail" and "locked" in c.detail and c.remedy == "unlock it"


def test_token_skipped_without_keychain(fake_runner):
    c = checks(doctor.run_doctor(fake_runner))["token"]
    assert c.status == "skip" and "only available on macOS" in c.detail


# ---- 7. url scheme ---------------------------------------------------------------------------------------------

def test_url_scheme_is_always_a_warning_with_the_design_text(fake_runner):
    c = checks(doctor.run_doctor(fake_runner))["url_scheme"]
    assert (c.status, c.detail, c.remedy) == ("warn", URL_SCHEME_TEXT, None)


# ---- 8. probe --------------------------------------------------------------------------------------------------

def test_probe_rows_become_checks(fake_runner, fake_things):
    fake_things.seed_todo("A", when="today")
    fake_things.missing_lists = {"trash"}
    report = doctor.run_doctor(fake_runner, probe=True)
    c = checks(report)
    assert fake_runner.calls("PROBE")[0].args == ()                 # no project id from doctor
    assert c["probe:version"].detail == f"Things {fake_things.version}"
    assert c["probe:list:today"].status == "ok" and c["probe:list:today"].detail == "'Today' (1 to-dos)"
    assert c["probe:list:trash"].status == "warn" and c["probe:list:trash"].detail.endswith("(-1728)")
    assert c["probe:range"].status == "warn"                        # empty Logbook
    assert c["probe:date-columns-aligned"].detail == "ids 1, due dates 1, start dates 1"
    assert c["probe:app-todos-include-projects"].detail == "app `to dos`: 1, of which projects: 0"
    assert report.ok is True                                        # probe rows only warn


def test_probe_range_ok_with_a_logged_item(fake_runner, fake_things):
    fake_things.seed_todo("Done", status="completed")
    c = checks(doctor.run_doctor(fake_runner, probe=True))
    assert c["probe:range"].status == "ok" and c["probe:list:logbook"].detail == "'Logbook' (1 to-dos)"


def test_probe_failure_is_one_failed_check(fake_runner):
    fake_runner.inject("PROBE", osa_code=-1712)
    report = doctor.run_doctor(fake_runner, probe=True)
    c = checks(report)
    assert c["probe"].status == "fail" and not any(n.startswith("probe:") for n in c)


def test_probe_checks_with_project_id(fake_runner, fake_things):
    pid = fake_things.seed_project("P")
    got = {c.name: c for c in doctor.probe_checks(fake_runner, pid)}
    assert got["probe:todo-id-resolves-project"].detail == "`to do id` of a project gives class project"
    assert fake_runner.calls("PROBE")[0].args == (pid,)


@pytest.mark.parametrize("feature, ok, fields, expected", [
    ("date-columns-aligned", False, ["3", "2", "3"], "NOT aligned: ids 3, due dates 2, start dates 3"),
    ("date-columns-aligned", False, ["Can't get due date", "-1728"], "failed: Can't get due date (-1728)"),
    ("list-projects", True, ["4"], "`projects of list` works (4 in Someday)"),
    ("list:trash", False, ["boom"], "failed: boom"),
    ("range", True, ["1"], "`to dos 1 thru 1` of the Logbook works"),
    ("something-new", True, ["a", "b"], "a b"),
    ("something-new", False, [], "failed: no detail"),
])
def test_probe_detail(feature, ok, fields, expected):
    assert doctor._probe_detail(feature, ok, fields) == expected


# ---- 9. check_repeating ------------------------------------------------------------------------------------------

def test_repeating_template_is_refused(fake_runner, fake_things):
    tid = fake_things.seed_todo("Water plants", repeating=True)
    c = checks(doctor.run_doctor(fake_runner, check_repeating_id=tid))["check_repeating"]
    assert c.status == "ok" and "shows in no list" in c.detail and "refuses" in c.detail
    assert fake_runner.calls("ROWS")[0].args == ("0", tid)
    assert fake_runner.calls("MEMBERSHIP")[0].args == doctor.MEMBERSHIP_LISTS


def test_visible_item_would_not_be_refused(fake_runner, fake_things):
    tid = fake_things.seed_todo("Stretch", when="today")
    c = checks(doctor.run_doctor(fake_runner, check_repeating_id=tid))["check_repeating"]
    assert c.status == "warn" and "would NOT refuse 'Stretch'" in c.detail and "today" in c.detail
    assert "instances" in c.remedy


def test_upcoming_item_uses_the_services_today(fake_runner, fake_things):
    tid = fake_things.seed_todo("Dentist", when="2026-10-03")
    c = doctor.check_repeating(fake_runner, tid, today=lambda: date(2026, 9, 28))
    assert c.status == "warn" and "upcoming" in c.detail and "start date 2026-10-03" in c.detail
    # the same item, judged on a later day: Upcoming only with a start date that isn't in the future -> refused
    c = doctor.check_repeating(fake_runner, tid, today=lambda: date(2026, 10, 5))
    assert c.status == "ok"


def test_closed_item_is_explained(fake_runner, fake_things):
    tid = fake_things.seed_todo("Old", status="completed")
    c = checks(doctor.run_doctor(fake_runner, check_repeating_id=tid))["check_repeating"]
    assert c.status == "warn" and "is completed" in c.detail


def test_unknown_id(fake_runner):
    c = checks(doctor.run_doctor(fake_runner, check_repeating_id=GHOST))["check_repeating"]
    assert c.status == "fail" and f"No item with id {GHOST}" in c.detail and "Copy Link" in c.remedy


def test_area_id(fake_runner, fake_things):
    aid = fake_things.seed_area("Home")
    c = checks(doctor.run_doctor(fake_runner, check_repeating_id=aid))["check_repeating"]
    assert c.status == "fail" and "is an area" in c.detail


def test_malformed_id_is_refused_before_anything_runs(fake_runner):
    with pytest.raises(InvalidInput) as e:
        doctor.run_doctor(fake_runner, check_repeating_id="not an id")
    assert e.value.details["field"] == "check_repeating_id"
    assert fake_runner.template_calls == []


def test_read_failure_during_check(fake_runner, fake_things):
    tid = fake_things.seed_todo("X", when="today")
    fake_runner.inject("MEMBERSHIP", osa_code=-609)
    c = checks(doctor.run_doctor(fake_runner, check_repeating_id=tid))["check_repeating"]
    assert c.status == "fail"


# ---- 10. versions / 11. rate limiter -----------------------------------------------------------------------------

def test_versions(fake_runner):
    import things_bridge
    v = doctor.run_doctor(fake_runner).versions
    assert set(v) == {"things_bridge", "python", "mcp", "things", "url_scheme"}
    assert v["things_bridge"] == things_bridge.__version__
    assert v["url_scheme"] == doctor.URL_SCHEME_VERSION == "2"
    assert v["mcp"] == metadata.version("mcp")
    assert v["python"] == ".".join(map(str, sys.version_info[:3]))


def test_versions_without_mcp(monkeypatch):
    def missing(name):
        raise metadata.PackageNotFoundError(name)
    monkeypatch.setattr(doctor.importlib.metadata, "version", missing)
    assert doctor.versions()["mcp"] == "not installed"
    assert doctor.versions()["things"] == "unknown"


def test_rate_limiter_usage(fake_things, clock, tmp_path):
    limiter = RateLimiter(tmp_path / "ratelimit.json", clock=clock, sleep=clock.sleep)
    limiter.acquire(30)
    r = FakeRunner(fake_things, clock=clock, rate_limiter=limiter)
    c = checks(doctor.run_doctor(r))["rate_limiter"]
    assert c.status == "ok" and c.detail.startswith("30 of 200 items sent in the last 10 s")
    assert r.dispatch_calls == []


def test_rate_limiter_unreadable(fake_things, clock):
    class Broken:
        budget, window_s = 200, 10.0

        def usage(self):
            raise OSError("disk on fire")

    c = checks(doctor.run_doctor(FakeRunner(fake_things, clock=clock, rate_limiter=Broken())))["rate_limiter"]
    assert c.status == "warn" and "OSError" in c.detail


# ---- hygiene -----------------------------------------------------------------------------------------------------

def test_doctor_import_is_light():
    code = ("import sys, things_bridge.doctor; "
            "print(sorted(m for m in ('mcp', 'pydantic', 'things_bridge.cli') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"
