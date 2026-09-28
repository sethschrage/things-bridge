"""The production stack end to end: ``MacRunner`` + ``ThingsService`` / CLI / MCP / doctor / selftest.

Only the operating system is simulated. ``subprocess.run`` is replaced by ``FakeMac``, which plays pgrep, open,
osascript and security on top of ``FakeThings``: osascript gets the real template source on stdin and the real
``=``-prefixed argv, and open gets the real URL with the token appended by the runner. Everything above
``subprocess.run`` is the shipped code, so a mismatch between layers (argv encoding, frame parsing, token
handling, error mapping) fails here even when each layer's unit tests pass against FakeRunner.
"""

import dataclasses
import io
import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field

import anyio
import pytest
from mcp import Client

from fakes import FAKE_TOKEN, FakeThings, OsaError
from things_bridge import applescript, authtoken, cli, runner, urlscheme
from things_bridge.doctor import run_doctor
from things_bridge.errors import AutomationDenied, NotFound, Refused, ThingsNotRunning, TokenMissing
from things_bridge.mcp_server import build_server
from things_bridge.models import TodoPatch, TodoSpec
from things_bridge.runner import OPEN, OSASCRIPT, PGREP, MacRunner
from things_bridge.selftest import run_selftest
from things_bridge.service import Config, ThingsService

SECURITY = authtoken.SECURITY
SOURCES = {applescript.script_source(name).encode("utf-8"): name for name in applescript.TEMPLATES}


def done(rc: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, stdout, stderr)


@dataclass
class FakeMac:
    """``subprocess.run`` on a Mac with Things 3, backed by FakeThings."""

    things: FakeThings
    keychain: str | None = None                     # the login Keychain item (None: no item)
    calls: list[tuple[list[str], dict]] = field(default_factory=list)
    osa_error: tuple[int, str] | None = None        # every osascript run fails with this (code, message)

    def __call__(self, argv, **kw):
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
        assert not kw.get("shell") and kw.get("capture_output") is True and kw.get("timeout", 0) > 0
        assert ("input" in kw) != ("stdin" in kw)
        assert authtoken.ENV not in kw["env"], "children never inherit the token"
        self.calls.append((list(argv), kw))
        program, rest = argv[0], argv[1:]
        if program == PGREP:
            assert rest == ["-x", runner.THINGS_PROCESS]
            return done(0 if self.things.running else 1)
        if program == OPEN:
            if rest == ["-g", "-b", runner.BUNDLE_ID]:
                if not self.things.running:
                    self.things.running = True
                    self.things.launches += 1
                return done()
            assert rest[:-1] in ([], ["-g"]) and rest[-1].startswith(urlscheme.SCHEME_PREFIX)
            self.things.open_url(rest[-1])
            return done()
        if program == OSASCRIPT:
            assert rest[0] == "-" and all(a.startswith("=") for a in rest[1:]), "values only as =-prefixed argv"
            name = SOURCES[kw["input"]]                 # the script arrives on stdin, verbatim from the registry
            if self.osa_error:
                code, message = self.osa_error
                return done(1, stderr=f"-: execution error: {message} ({code})\n".encode())
            try:
                return done(0, self.things.render(name, [a[1:] for a in rest[1:]]))
            except OsaError as e:
                return done(1, stderr=("-: " + e.stderr).encode())
        if program == SECURITY:
            assert rest[:1] == ["find-generic-password"], "tests only read the Keychain"
            return done(0, (self.keychain + "\n").encode()) if self.keychain else done(authtoken.NOT_FOUND_RC)
        raise AssertionError(f"unexpected program {program}")

    def of(self, program: str) -> list[list[str]]:
        return [argv for argv, _ in self.calls if argv[0] == program]

    def opened_urls(self) -> list[str]:
        return [argv[-1] for argv in self.of(OPEN) if argv[-1].startswith(urlscheme.SCHEME_PREFIX)]

    def templates(self) -> list[str]:
        return [SOURCES[kw["input"]] for argv, kw in self.calls if argv[0] == OSASCRIPT]


@pytest.fixture
def mac(monkeypatch, fake_things):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(runner, "_is_executable", lambda path: True)
    monkeypatch.setattr(runner, "_secrets", ())
    fake = FakeMac(fake_things)
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv(authtoken.ENV, FAKE_TOKEN)


@pytest.fixture
def mr(mac, clock):
    return MacRunner(clock=clock)


@pytest.fixture
def svc(mr, clock):
    return ThingsService(mr, Config(), today=clock.today)


@pytest.fixture(autouse=True)
def restore_logger():
    logger = logging.getLogger("things_bridge")
    level, handlers = logger.level, list(logger.handlers)
    yield
    logger.setLevel(level)
    logger.handlers[:] = handlers


@pytest.fixture
def lib(fake_things):
    t = fake_things
    t.seed_tag("Errand")
    area = t.seed_area("Family")
    party = t.seed_project("Plan Birthday Party", area=area)
    return {
        "area": area, "party": party,
        "milk": t.seed_todo("Buy milk", when="today", tags=["Errand"], notes="2%\nlactose free", project=party),
        "cafe": t.seed_todo("Café visit", when="today"),
        "someday": t.seed_todo("Learn watercolor", when="someday"),
        "tmpl": t.seed_todo("Water plants", repeating=True),
    }


def leaks(obj) -> bool:
    text = json.dumps(dataclasses.asdict(obj) if dataclasses.is_dataclass(obj) else obj, default=str)
    return FAKE_TOKEN in text


# ---- reads -------------------------------------------------------------------------------------------------------

def test_list_get_and_search_through_osascript(svc, mac, lib, fake_things):
    page = svc.list_items("today")
    assert [i.title for i in page.items] == ["Buy milk", "Café visit"]
    milk = page.items[0]
    assert milk.id == lib["milk"] and milk.tags == ["Errand"] and milk.project.title == "Plan Birthday Party"
    assert milk.area.title == "Family" and milk.link == urlscheme.build_show(id=lib["milk"]).url
    assert milk.notes == "2%\nlactose free"
    got = svc.get(lib["party"], children=True)
    assert got.kind == "project" and [i.id for i in got.children.items] == [lib["milk"]]
    assert [i.title for i in svc.search("CAFE").items] == ["Café visit"]      # case- and accent-insensitive
    assert {"SCAN", "ROWS"} <= set(mac.templates())
    assert mac.of(OPEN) == [] and fake_things.log == []                        # reads never open a URL


def test_unknown_id_is_not_found(svc, lib):
    with pytest.raises(NotFound):
        svc.get("AAAAAAAAAAAAAAAAAAAAAA")


def test_automation_denied_maps_through_the_stack(svc, mac, lib):
    mac.osa_error = (-1743, "Not authorized to send Apple events to Things3.")
    with pytest.raises(AutomationDenied) as e:
        svc.list_items("inbox")
    assert "Privacy & Security" in e.value.hint and e.value.details["osa_code"] == -1743


def test_closed_things_is_launched_hidden(svc, mac, lib, fake_things, clock):
    fake_things.running = False
    assert [i.title for i in svc.list_items("someday").items] == ["Learn watercolor"]
    assert [OPEN, "-g", "-b", runner.BUNDLE_ID] in mac.of(OPEN) and fake_things.launches == 1
    assert runner.LAUNCH_SETTLE_S in clock.sleeps


def test_no_launch_never_starts_things(mr, mac, lib, fake_things, clock):
    fake_things.running = False
    svc = ThingsService(mr, Config(allow_launch=False), today=clock.today)
    with pytest.raises(ThingsNotRunning):
        svc.list_items("today")
    assert mac.of(OPEN) == [] and mac.of(OSASCRIPT) == [] and fake_things.running is False


# ---- writes ------------------------------------------------------------------------------------------------------

def test_add_todo_is_sent_without_token_and_verified(svc, mac, lib, fake_things):
    res = svc.add_todo(TodoSpec(title="Call Paolo", when="today", tags=["errand"], project="plan birthday party"))
    assert res.sent is True and res.verified is True, res.reason
    new = fake_things.get(res.ids[0])
    assert new["title"] == "Call Paolo" and new["tags"] == ["Errand"] and new["project"] == lib["party"]
    [argv] = mac.of(OPEN)
    assert argv[:2] == [OPEN, "-g"] and "auth-token" not in argv[-1]          # add needs no token (D8)
    assert f"list-id={lib['party']}" in argv[-1] and "list=" not in argv[-1]   # resolved to an id (N9)
    assert res.urls == [argv[-1]]


def test_update_appends_the_token_last_and_never_returns_it(svc, mac, lib, fake_things, token):
    res = svc.update_todo(lib["milk"], TodoPatch(title="Buy oat milk", when="anytime"))
    assert res.sent is True and res.verified is True, res.reason
    assert fake_things.get(lib["milk"])["title"] == "Buy oat milk"
    urls = mac.opened_urls()
    assert [u.split("?")[0] for u in urls] == ["things:///update", "things:///json"]      # anytime via json (D10)
    assert all(u.endswith("&auth-token=" + FAKE_TOKEN) for u in urls)
    assert all(u.endswith("&auth-token=***") for u in res.urls)
    assert not leaks(res) and res.before[0].title == "Buy milk"


def test_token_from_the_keychain(svc, mac, lib, fake_things):
    mac.keychain = FAKE_TOKEN
    res = svc.set_status([lib["cafe"]], "completed")
    assert res.verified is True and fake_things.get(lib["cafe"])["status"] == "completed"
    assert mac.of(SECURITY) and mac.opened_urls()[-1].endswith("&auth-token=" + FAKE_TOKEN)
    assert not leaks(res)


def test_missing_token_stops_before_open(svc, mac, lib, fake_things):
    with pytest.raises(TokenMissing):
        svc.update_todo(lib["milk"], TodoPatch(title="x"))
    assert mac.of(SECURITY) and mac.of(OPEN) == [] and fake_things.log == []


def test_repeating_guard_refuses_before_anything_is_sent(svc, mac, lib, fake_things, token):
    with pytest.raises(Refused):
        svc.schedule([lib["tmpl"]], when="today")
    assert mac.of(OPEN) == [] and fake_things.crashes == 0


def test_unverified_write_is_reported_and_never_resent(svc, mac, lib, fake_things, token, clock):
    fake_things.urls_enabled = False                  # "Enable Things URLs?" sheet: the URL is parked
    res = svc.update_todo(lib["milk"], TodoPatch(title="Later"))
    assert res.sent is True and res.verified is False and "Enable Things URLs" in res.reason
    assert len(mac.opened_urls()) == 1                # N10
    fake_things.enable_urls()                         # the user clicks Enable: it applies late (§1.5)
    assert fake_things.get(lib["milk"])["title"] == "Later"


def test_dry_run_opens_nothing(svc, mac, lib, fake_things, token):
    res = svc.update_todo(lib["milk"], TodoPatch(title="x"), dry_run=True)
    assert res.dry_run is True and res.sent is False and res.urls[0].endswith("&auth-token=***")
    assert mac.opened_urls() == [] and mac.of(SECURITY) == []


def test_raw_json_update_carries_the_token(svc, mac, lib, fake_things, token):
    res = svc.raw_json([{"type": "to-do", "operation": "update", "id": lib["cafe"],
                         "attributes": {"when": "someday", "append-notes": "latte"}}])
    assert res.verified is True, res.reason
    assert fake_things.get(lib["cafe"])["notes"] == "latte" and lib["cafe"] in fake_things.list_ids("someday")
    [url] = mac.opened_urls()
    assert url.startswith("things:///json?data=") and url.endswith("&auth-token=" + FAKE_TOKEN)


def test_navigation_opens_in_the_foreground(svc, mac, lib, fake_things):
    svc.show(id=lib["party"])
    svc.show(list_key="today")
    svc.open_search("milk")
    opens = [argv for argv in mac.of(OPEN) if argv[-1].startswith(urlscheme.SCHEME_PREFIX)]
    assert [argv[1:] for argv in opens] == [[f"things:///show?id={lib['party']}"], ["things:///show?id=today"],
                                            ["things:///search?query=milk"]]     # no -g: Things comes forward
    assert len(fake_things.shown) == 3 and fake_things.errors == []


def test_rate_log_and_locks_live_in_the_state_dir(svc, lib, token, state_dir):
    svc.add_todo(TodoSpec(title="One"))
    assert {p.name for p in state_dir.iterdir()} >= {"ratelimit.json", "write.lock", "osa.lock"}
    assert state_dir.stat().st_mode & 0o777 == 0o700
    assert json.loads((state_dir / "ratelimit.json").read_text())[0][1] == 1


# ---- front ends --------------------------------------------------------------------------------------------------

def run_cli(monkeypatch, capsys, mr, clock, *argv, stdin: str = ""):
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = cli.main(list(argv), runner=mr, today=clock.today)
    out, err = capsys.readouterr()
    return code, out, err


def test_cli_add_list_and_update(monkeypatch, capsys, mr, mac, clock, lib, fake_things, token):
    code, out, err = run_cli(monkeypatch, capsys, mr, clock, "--json", "add", "Pick up cake", "--when", "today")
    assert code == 0, err
    added = json.loads(out)
    assert added["verified"] is True and fake_things.get(added["ids"][0])["title"] == "Pick up cake"
    code, out, _ = run_cli(monkeypatch, capsys, mr, clock, "--json", "today")
    assert code == 0 and "Pick up cake" in [i["title"] for i in json.loads(out)["items"]]
    code, out, err = run_cli(monkeypatch, capsys, mr, clock, "-v", "update", added["ids"][0], "--notes", "-",
                             stdin="with candles\n")
    assert code == 0 and fake_things.get(added["ids"][0])["notes"] == "with candles"
    assert FAKE_TOKEN not in out + err


def test_cli_exit_codes_through_the_stack(monkeypatch, capsys, mr, mac, clock, lib, token):
    code, out, _ = run_cli(monkeypatch, capsys, mr, clock, "--json", "complete", lib["tmpl"])
    assert code == 4 and json.loads(out)["error"]["code"] == "refused"
    code, out, _ = run_cli(monkeypatch, capsys, mr, clock, "--json", "get", "AAAAAAAAAAAAAAAAAAAAAA")
    assert code == 5 and json.loads(out)["error"]["code"] == "not_found"
    mac.osa_error = (-1743, "Not authorized to send Apple events to Things3.")
    code, out, _ = run_cli(monkeypatch, capsys, mr, clock, "--json", "inbox")
    assert code == 3 and json.loads(out)["error"]["code"] == "automation_denied"


def test_mcp_tools_over_the_real_runner(svc, mac, lib, fake_things, token):
    server = build_server(svc)

    async def main():
        async with Client(server) as c:
            listed = await c.call_tool("things_list", {"list": "today"})
            added = await c.call_tool("things_add_todo", {"title": "Book venue", "project": lib["party"]})
            refused = await c.call_tool("things_set_status", {"ids": [lib["tmpl"]], "status": "completed"})
            return listed, added, refused

    listed, added, refused = anyio.run(main)
    assert not listed.is_error and [i["title"] for i in listed.structured_content["items"]] == ["Buy milk",
                                                                                                "Café visit"]
    assert not added.is_error and added.structured_content["verified"] is True
    assert fake_things.one("Book venue")["project"] == lib["party"]
    assert refused.is_error and "[refused]" in refused.content[0].text
    assert FAKE_TOKEN not in json.dumps([r.model_dump(mode="json") for r in (listed, added, refused)])


def test_doctor_on_a_working_mac(mr, mac, lib, token):
    report = run_doctor(mr, allow_launch=True, probe=True, check_repeating_id=lib["tmpl"])
    status = {c.name: c.status for c in report.checks}
    assert status["platform"] == status["things_installed"] == status["things_running"] == "ok"
    assert status["automation"] == status["token"] == status["check_repeating"] == "ok"
    assert report.versions["things"] == "3.22.1" and mac.opened_urls() == []    # doctor never writes


def test_selftest_end_to_end(mr, mac, lib, fake_things, clock, token):
    before = {i: dict(it) for i, it in fake_things.items.items()}
    report = run_selftest(mr, Config(), yes=True, today=clock.today)
    assert report.ok, [(s.name, s.detail) for s in report.steps if s.status == "fail"]
    assert all(fake_things.get(i)["status"] != "open" for i in report.created_ids)
    for id_, old in before.items():                   # the user's own items are untouched
        assert fake_things.items[id_] == old
    urls = mac.opened_urls()
    assert len(urls) >= 10 and any("auth-token" in u for u in urls)
    assert all(u.endswith("&auth-token=" + FAKE_TOKEN) == ("auth-token" in u) for u in urls)
    assert all(s.status != "skip" for s in report.steps if s.name not in ("ST-12",))    # ST-12: --probe-conflicts
