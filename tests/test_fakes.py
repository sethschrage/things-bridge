"""tests/fakes.py (DESIGN §7): URLs built by urlscheme/jsoncmd mutate FakeThings with the documented
semantics, every template's rendered frame parses with applescript.py, and FakeRunner implements
the Runner interface with recording and failure injection."""

import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from fakes import BUILTIN_LISTS, FAKE_TOKEN, FakeRunner, FakeThings, OsaError
from things_bridge import applescript as a
from things_bridge import jsoncmd as j
from things_bridge import urlscheme as u
from things_bridge.applescript import RS, TEMPLATES, US, parse_frame
from things_bridge.errors import (AutomationDenied, AutomationTimeout, DispatchFailed, InvalidInput, NotFound,
                                  NotMacOS, ReadFailed, Refused, SandboxBlocked, ThingsNotInstalled,
                                  ThingsNotRunning, TokenMissing)
from things_bridge.models import Item
from things_bridge.runner import Dispatch, RateLimiter, Runner

TODAY = date(2026, 9, 28)                    # conftest FakeClock's today
NOTES = 'Low fat.\r\nFrom the "good" shop – ünïcødé 🥛\r\n\ttabbed'
OFFICIAL_JSON = Path(__file__).parent / "fixtures" / "official_json_example.json"


@pytest.fixture
def t(fake_things):
    return fake_things


@pytest.fixture
def r(fake_runner):
    return fake_runner


@pytest.fixture
def w(t):
    """A small, varied Things library."""
    ns = SimpleNamespace()
    ns.home = t.seed_tag("Home", shortcut="h")
    ns.errand = t.seed_tag("Errand", parent="Home")
    ns.deep = t.seed_tag("Deep Work")
    ns.family = t.seed_area("Family", tags=["Home"])
    ns.work = t.seed_area("Work")
    ns.party = t.seed_project("Plan Birthday Party", area=ns.family, notes="Party notes", tags=["Home"],
                              deadline="2026-10-20")
    ns.venue = t.seed_heading(ns.party, "Venue")
    ns.inbox = t.seed_todo("Inbox item", age=3600)
    ns.milk = t.seed_todo("Buy milk 🥛", when="today", tags=["Errand", "Home"], deadline="2026-10-02",
                          notes=NOTES, project=ns.party, age=86400)
    ns.book = t.seed_todo("Book venue", project=ns.party, heading=ns.venue)
    ns.evening = t.seed_todo("Collect dry cleaning", when="evening")
    ns.chore = t.seed_todo("Area chore", area=ns.work)
    ns.someday = t.seed_todo("Learn watercolor", when="someday")
    ns.later = t.seed_todo("Paint bedroom", when="2026-10-03")
    ns.done = t.seed_todo("Call Paolo", project=ns.party, status="completed",
                          completed=datetime(2026, 9, 27, 18, 30))
    ns.dropped = t.seed_todo("Old idea", area=ns.family, status="canceled", tags=["Deep Work"],
                             canceled=datetime(2026, 9, 22, 7, 0))
    return ns


def send(r: FakeRunner, turl: u.ThingsURL) -> list[str]:
    """Dispatch through the runner; return what Things created (x-things-id)."""
    r.dispatch(turl)
    return list(r.things.last_created)


def members(t: FakeThings, key: str) -> set[str]:
    return set(t.list_ids(key))


# ---- FakeRunner: the Runner interface ---------------------------------------------------------------

RUNNER_METHODS = ("require_macos", "things_running", "ensure_running", "run_template", "dispatch", "write_lock",
                  "wall", "monotonic", "sleep")


def test_fake_runner_implements_every_runner_method(r):
    assert isinstance(r, Runner)
    for name in RUNNER_METHODS:
        assert getattr(FakeRunner, name) is not getattr(Runner, name), name


def test_fake_runner_standalone_construction():
    r = FakeRunner()
    assert r.things.today() == TODAY
    assert r.run_template("AREAS").template == "AREAS"


def test_clock_methods_delegate_to_the_shared_clock(r, clock):
    assert r.wall() == clock.wall() and r.monotonic() == clock.monotonic()
    wall, mono = clock.wall(), clock.monotonic()
    r.sleep(1.5)
    assert clock.sleeps == [1.5]
    assert (r.wall(), r.monotonic()) == (wall + 1.5, mono + 1.5)


def test_run_template_records_every_call(r, w):
    f = r.run_template("SCAN", ["list", "today", "0", "0"], timeout=130)
    assert f.template == "SCAN"
    r.run_template("AREAS")
    assert [(c.name, c.args, c.timeout) for c in r.template_calls] == [
        ("SCAN", ("list", "today", "0", "0"), 130), ("AREAS", (), None)]
    assert [c.name for c in r.calls("AREAS")] == ["AREAS"]


def test_run_template_validates_like_the_real_runner(r):
    with pytest.raises(KeyError):
        r.run_template("DELETE_EVERYTHING")
    with pytest.raises(TypeError):
        r.run_template("SCAN", "list")                           # a bare str is not an argv list
    with pytest.raises(InvalidInput):
        r.run_template("SCAN", ["app"])                          # arity (Template.check_args)
    with pytest.raises(InvalidInput):
        r.run_template("ROWS", ["0", "a\x00b"])                  # encode_args rejects NUL
    assert [c.name for c in r.template_calls] == ["SCAN", "ROWS"]


def test_argv_values_arrive_like_tb_arg(r, t):
    """The "=" prefix is stripped exactly once: leading dashes, "=" and empty values survive (Q7)."""
    tid = t.seed_todo("-tb =x, @ “q”", when="today")
    f = r.run_template("FIND_CREATED", ["to-do", "-tb =x, @ “q”", "60", "list", "today", "0"])
    assert a.created_from_frame(f) == [(tid, 0)]
    f = r.run_template("SCAN", ["app", "", "0", "0"])
    assert f.header[3:5] == ["app", ""]


def test_require_macos_and_running_state(r, t):
    r.require_macos()
    assert r.things_running() is True
    t.running = False
    assert r.things_running() is False


def test_not_macos_blocks_everything_but_dry_run(r, t):
    r.macos = False
    with pytest.raises(NotMacOS):
        r.require_macos()
    with pytest.raises(NotMacOS):
        r.things_running()
    with pytest.raises(NotMacOS):
        r.ensure_running(allow_launch=True)
    with pytest.raises(NotMacOS):
        r.run_template("AREAS")
    with pytest.raises(NotMacOS):
        r.dispatch(u.build_add(title="x"))
    r.token = None                                                # dry-run needs neither macOS nor a token
    d = r.dispatch(u.build_update(id="SyJEz273ceSkabUbciM73A", title="x"), dry_run=True)
    assert (d.sent, d.url_redacted) == (False, "things:///update?id=SyJEz273ceSkabUbciM73A&title=x&auth-token=***")
    assert t.log == []


def test_ensure_running(r, t):
    assert r.ensure_running(allow_launch=False) is False
    t.running = False
    with pytest.raises(ThingsNotRunning):
        r.ensure_running(allow_launch=False)
    assert r.ensure_running(allow_launch=True) is True
    assert t.running and r.launches == 1
    assert r.ensure_running(allow_launch=True) is False


def test_things_not_running(r, t):
    t.running = False
    for name in TEMPLATES:
        if TEMPLATES[name].uses_library:
            args = ["0"] * TEMPLATES[name].min_args
            with pytest.raises(ThingsNotRunning) as e:
                r.run_template(name, args)
            assert e.value.details == {"osa_code": 9001}
    f = r.run_template("APP_VERSION")                             # never launches, still answers
    assert a.probe_results(f) == [("app-version", True, ["3.22.1", "0"])]


def test_things_not_installed(r, t):
    t.installed = False
    t.running = False
    with pytest.raises(ReadFailed) as e:
        r.run_template("APP_VERSION")                             # doctor reads any failure as "not installed"
    assert e.value.details["osa_code"] == -10814
    with pytest.raises(ThingsNotInstalled) as e:
        r.run_template("AREAS")                                   # dictionary missing + APP_VERSION fails
    assert e.value.details["osa_code"] == -2741
    with pytest.raises(ThingsNotInstalled):
        r.ensure_running(allow_launch=True)
    with pytest.raises(ThingsNotInstalled):
        r.dispatch(u.build_add(title="x"))
    assert t.log == [] and r.dispatch_calls[-1].outcome == "failed"


@pytest.mark.parametrize("code, running, exc", [
    (-1743, True, AutomationDenied), (-1744, True, AutomationDenied), (-1712, True, AutomationTimeout),
    (-600, True, SandboxBlocked), (-600, False, ThingsNotRunning), (-10004, True, SandboxBlocked),
    (9001, True, ThingsNotRunning), (-609, True, ReadFailed), (-1719, True, ReadFailed),
    (-1728, True, NotFound), (-2741, True, ReadFailed), (-1708, True, ReadFailed), (9101, True, ReadFailed),
    (9002, True, InvalidInput), (-1700, True, ReadFailed), (-128, True, ReadFailed),
])
def test_injected_osa_codes_map_like_the_real_runner(r, t, code, running, exc):
    t.running = running
    r.inject("AREAS", osa_code=code)
    with pytest.raises(exc) as e:
        r.run_template("AREAS")
    assert e.value.details == {"osa_code": code}
    assert r.things.running is running


def test_by_id_reads_map_empty_results_to_not_found(r, w):
    r.inject("ROWS", osa_code=-1719)
    with pytest.raises(NotFound) as e:
        r.run_template("ROWS", ["0", w.milk])
    assert w.milk in e.value.message


def test_injection_matching_and_exhaustion(r, w):
    bulk = ["app", "", "0", "0"]
    slow = ["app", "", "0", "1"]
    r.inject("SCAN", osa_code=9101, times=2, match=lambda args: args[3] == "0")
    r.run_template("AREAS")                                       # other templates unaffected
    r.run_template("SCAN", slow)                                  # the slow path is unaffected
    for _ in range(2):
        with pytest.raises(ReadFailed):
            r.run_template("SCAN", bulk)
    assert r.run_template("SCAN", bulk).rows                      # exhausted


def test_inject_timeout_and_exceptions(r):
    r.inject(timeout=True)
    with pytest.raises(AutomationTimeout) as e:
        r.run_template("FIND_CREATED", ["to-do", "secret title", "10", "app", "", "0"])
    assert "secret" not in str(e.value)                           # never echoes argv
    boom = ReadFailed("boom")
    r.inject(exc=boom)
    with pytest.raises(ReadFailed) as e:
        r.run_template("TAGS")
    assert e.value is boom
    with pytest.raises(ValueError):
        r.inject(osa_code=-1743, timeout=True)
    with pytest.raises(ValueError):
        r.inject()
    with pytest.raises(KeyError):
        r.inject("NOPE", osa_code=-1)


# ---- FakeRunner.dispatch --------------------------------------------------------------------------

def test_dispatch_appends_the_token_only_when_required(r, t, w, clock):
    d = r.dispatch(u.build_add(title="New"))
    assert d == Dispatch("add", "things:///add?title=New", sent=True, dry_run=False, foreground=False,
                         t0=clock.wall())
    assert t.log[-1] == "things:///add?title=New"
    d = r.dispatch(u.build_update(id=w.milk, title="Oat milk"))
    assert d.url_redacted == f"things:///update?id={w.milk}&title=Oat%20milk&auth-token=***"
    assert t.get(w.milk)["title"] == "Oat milk"                   # Things got the real token
    everything = repr(r.dispatch_calls) + repr(t.log) + repr(t.errors) + repr(r.sent_urls)
    assert FAKE_TOKEN not in everything


def test_dispatch_dry_run_sends_nothing(r, t, w):
    d = r.dispatch(u.build_update(id=w.milk, title="Oat milk"), dry_run=True)
    assert (d.sent, d.dry_run) == (False, True)
    assert d.url_redacted == f"things:///update?id={w.milk}&title=Oat%20milk&auth-token=***"
    assert t.log == [] and t.get(w.milk)["title"] == "Buy milk 🥛"
    assert r.sent == [] and r.dispatch_calls[0].outcome == "dry-run"
    assert r.dispatch(u.build_add(title="x"), dry_run=True).url_redacted == "things:///add?title=x"


def test_dispatch_without_a_usable_token(r, t, w):
    r.token = None
    with pytest.raises(TokenMissing):
        r.dispatch(u.build_update(id=w.milk, title="x"))
    with pytest.raises(TokenMissing):
        r.dispatch(u.build_json([j.Todo(operation="update", id=w.milk, when="anytime")]))
    send(r, u.build_add(title="No token needed"))
    assert t.one("No token needed")
    r.token = "not a token!"                                      # authtoken.TOKEN_RE
    with pytest.raises(TokenMissing):
        r.dispatch(u.build_update(id=w.milk, title="x"))
    assert t.get(w.milk)["title"] == "Buy milk 🥛"


@pytest.mark.parametrize("turl", [
    u.ThingsURL("delete", (("id", "SyJEz273ceSkabUbciM73A"),), 1, True),
    u.ThingsURL("add-json", (("data", "[]"),), 1, False),
    u.ThingsURL("add", (("title", "x"), ("x-success", "evil://")), 1, False),
    u.ThingsURL("add", (("title", "x"), ("X-Error", "evil://")), 1, False),
    u.ThingsURL("update", (("id", "SyJEz273ceSkabUbciM73A"), ("auth-token", "leak")), 1, True),
    u.ThingsURL("add", (("title", "x"), ("title", "y")), 1, False),
    u.ThingsURL("update", (("id", "SyJEz273ceSkabUbciM73A"), ("title", "x")), 1, False),    # token flag lies
    u.ThingsURL("json", (("data", '[{"type":"to-do","operation":"update","id":"SyJEz273ceSkabUbciM73A",'
                                  '"attributes":{"title":"x"}}]'),), 1, False),
    u.ThingsURL("json", (("data", "{}"),), 1, False),
])
def test_dispatch_refuses_what_the_real_runner_refuses(r, t, turl):
    for dry_run in (True, False):
        with pytest.raises(Refused):
            r.dispatch(turl, dry_run=dry_run)
    assert t.log == [] and r.dispatch_calls == []


def test_dispatch_rate_budget(r, t, clock, tmp_path):
    heavy = u.build_add(titles=[f"t{i}" for i in range(u.RATE_BUDGET + 1)])
    with pytest.raises(Refused):
        r.dispatch(heavy)
    assert t.log == []
    r.rate_limiter = RateLimiter(tmp_path / "ratelimit.json", clock=clock, sleep=clock.sleep)
    r.dispatch(u.build_add(titles=[f"a{i}" for i in range(150)]))
    r.dispatch(u.build_add(titles=[f"b{i}" for i in range(50)]))
    assert clock.sleeps == []
    r.dispatch(u.build_add(titles=[f"c{i}" for i in range(10)]))  # waits for the window to slide
    assert len(clock.sleeps) == 1 and 9.9 <= clock.sleeps[0] <= 10.1
    assert len(t.by_title("c9")) == 1


def test_dispatch_url_length_limit(r, t, monkeypatch):
    turl = u.build_add(title="x" * 50)
    monkeypatch.setattr(u, "MAX_URL_BYTES", 40)
    with pytest.raises(InvalidInput):
        r.dispatch(turl)
    assert t.log == []


def test_dispatch_navigation_is_foreground_and_changes_nothing(r, t, w):
    before = {k: dict(v) for k, v in t.items.items()}
    assert r.dispatch(u.build_show(id="today")).foreground is True
    r.dispatch(u.build_search(query="milk"))
    r.dispatch(u.build_version())
    assert t.shown == ["show?id=today", "search?query=milk", "version"]
    assert t.items == before


def test_dispatch_under_write_lock_is_recorded(r):
    r.dispatch(u.build_add(title="a"))
    with r.write_lock():
        r.dispatch(u.build_add(title="b"))
        with r.write_lock():
            pass
    assert [c.locked for c in r.dispatch_calls] == [False, True]
    assert r.write_locks == 2


def test_drop_next_url(r, t):
    r.drop_next_url()
    d = r.dispatch(u.build_add(title="Lost"))
    assert d.sent is True and t.by_title("Lost") == [] and t.log == []
    assert r.dispatch_calls[-1].outcome == "dropped"
    send(r, u.build_add(title="Kept"))
    assert t.one("Kept")


def test_delayed_urls_apply_once_the_clock_moves_on(r, t, clock):
    r.url_delay_s = 2.0
    r.dispatch(u.build_add(title="Late", when="today"))
    assert r.dispatch_calls[-1].outcome == "delayed"
    f = r.run_template("FIND_CREATED", ["to-do", "Late", "10", "list", "today", "0"])
    assert a.created_from_frame(f) == []
    r.sleep(2.0)
    f = r.run_template("FIND_CREATED", ["to-do", "Late", "10", "list", "today", "0"])
    assert [age for _, age in a.created_from_frame(f)] == [0]     # created when it finally applied
    r.dispatch(u.build_add(title="Later"))
    r.flush()
    assert t.one("Later")


def test_fail_next_dispatch(r, t):
    r.fail_next_dispatch(DispatchFailed("open failed"))
    with pytest.raises(DispatchFailed):
        r.dispatch(u.build_add(title="x"))
    assert t.log == [] and r.dispatch_calls[-1].outcome == "failed"
    send(r, u.build_add(title="x"))
    assert t.one("x")


def test_dispatch_launches_a_closed_things(r, t):
    t.running = False
    send(r, u.build_add(title="Wake up"))
    assert t.running and t.launches == 1 and t.one("Wake up")


# ---- sheets: wrong token, "Enable Things URLs", dialogs (R-MEX §3, MODALX1) ---------------------------

def test_wrong_token_opens_an_error_sheet_and_changes_nothing(r, t, w):
    r.token = "wrong-token-123"
    r.dispatch(u.build_update(id=w.milk, title="Hacked"))
    r.dispatch(u.build_json([j.Todo(title="Created"), j.Todo(operation="update", id=w.milk, title="Hacked")]))
    assert t.get(w.milk)["title"] == "Buy milk 🥛" and t.by_title("Created") == []
    assert t.sheet == "error" and len(t.errors) == 2
    assert r.run_template("SCAN", ["app", "", "0", "0"]).rows == []          # top-level reads empty
    assert r.run_template("SCAN", ["projects", "", "0", "0"]).rows == []
    assert len(r.run_template("SCAN", ["list", "today", "0", "0"]).rows) == 2   # list reads survive
    assert a.missing_ids(r.run_template("ROWS", ["0", w.milk])) == []           # by-id reads survive
    t.dismiss_sheet()
    assert len(r.run_template("SCAN", ["app", "", "0", "0"]).rows) == 7


def test_urls_park_behind_the_enable_sheet_until_enabled(r, t, w):
    t.urls_enabled = False
    r.dispatch(u.build_add(title="Parked", when="today"))
    assert t.parked == 1 and t.sheet == "enable-urls" and t.by_title("Parked") == []
    f = r.run_template("FIND_CREATED", ["to-do", "Parked", "10", "app", "", "1"])
    assert a.created_from_frame(f) == [] and a.diag_from_frame(f) == (0, 1)    # the sheet diagnosis
    created = t.enable_urls()
    assert created == [t.one("Parked")["id"]] and t.sheet is None and t.parked == 0


def test_dismissing_the_enable_sheet_drops_parked_urls(r, t):
    t.urls_enabled = False
    r.dispatch(u.build_add(title="Parked"))
    t.dismiss_sheet()
    assert t.parked == 0 and t.by_title("Parked") == []


def test_sheet_mode_empties_top_level_collections_only(r, t, w):
    r.sheet_mode = True
    assert r.sheet_mode and t.sheet == "dialog"
    assert r.run_template("SCAN", ["app", "", "0", "0"]).rows == []
    assert r.run_template("PROJECT_AREAS").rows == []
    assert a.created_from_frame(r.run_template("FIND_CREATED", ["project", "Plan Birthday Party", "99999",
                                                                "app", "", "0"])) == []
    assert len(a.areas_from_frame(r.run_template("AREAS"))) == 2
    assert a.probe_results(r.run_template("AUTOMATION")) == [("automation", True, ["2"])]
    assert a.scan_rows(r.run_template("SCAN", ["project", w.party, "0", "0"]))
    send(r, u.build_add(title="Written behind the sheet"))                   # writes still land
    r.sheet_mode = False
    assert t.one("Written behind the sheet")


# ---- URL interpreter: add ------------------------------------------------------------------------

def test_add_defaults_to_the_inbox(r, t):
    [tid] = send(r, u.build_add(title="Book flights"))
    it = t.get(tid)
    assert (it["kind"], it["title"], it["status"], it["start"], it["project"], it["area"]) == \
        ("to-do", "Book flights", "open", "inbox", None, None)
    assert members(t, "inbox") == {tid}
    assert it["created"] == t.now() and it["created"].date() == TODAY


def test_add_with_every_documented_field(r, t, w):
    [tid] = send(r, u.build_add(title="Buy bread", notes="Low fat.\nWholemeal", when="today", deadline="2026-10-01",
                                tags=["Errand", "Nope", "Home"], checklist_items=["a", "b"]))
    it = t.get(tid)
    assert it["notes"] == "Low fat.\nWholemeal"
    assert (it["start"], it["evening"], it["deadline"]) == ("2026-09-28", False, "2026-10-01")
    assert it["tags"] == ["Errand", "Home"]                        # unknown tags are ignored
    assert "tag 'Nope' doesn't exist" in t.ignored
    assert [c["title"] for c in it["checklist"]] == ["a", "b"]
    assert tid in members(t, "today") and tid in members(t, "anytime") and tid not in members(t, "inbox")


def test_add_titles_creates_one_todo_per_line(r, t, w):
    turl = u.build_add(titles=["Milk", "Beer", "Cheese"], list_id=w.party, tags=["Errand"])
    assert turl.weight == 3
    ids = send(r, turl)
    assert [t.get(i)["title"] for i in ids] == ["Milk", "Beer", "Cheese"]
    for i in ids:
        it = t.get(i)
        assert (it["project"], it["area"], it["start"], it["tags"]) == (w.party, None, "anytime", ["Errand"])


def test_add_into_an_area(r, t, w):
    [tid] = send(r, u.build_add(title="Call doctor", list_id=w.work))
    it = t.get(tid)
    assert (it["area"], it["project"], it["start"]) == (w.work, None, "anytime")
    assert tid in members(t, "anytime") and tid not in members(t, "inbox")


@pytest.mark.parametrize("when, start, evening, reminder, lists", [
    ("today", "2026-09-28", False, None, {"today", "anytime"}),
    ("evening", "2026-09-28", True, None, {"today", "anytime"}),
    ("evening@6pm", "2026-09-28", True, "6pm", {"today", "anytime"}),
    ("tomorrow", "2026-09-29", False, None, {"upcoming", "tomorrow"}),
    ("2026-10-02@9:30am", "2026-10-02", False, "9:30am", {"upcoming"}),
    ("anytime", "anytime", False, None, {"anytime"}),
    ("someday", "someday", False, None, {"someday"}),
])
def test_add_when_vocabulary(r, t, when, start, evening, reminder, lists):
    [tid] = send(r, u.build_add(title="x", when=when))
    it = t.get(tid)
    assert (it["start"], it["evening"], it["reminder"]) == (start, evening, reminder)
    assert {k for k in BUILTIN_LISTS if tid in t.list_ids(k)} == lists


def test_add_natural_language_is_not_modelled(r, t):
    [tid] = send(r, u.build_add(title="Call doctor", when="next monday", strict=False))
    assert t.get(tid)["start"] == "inbox" and "when 'next monday' not understood" in t.ignored


def test_add_under_a_heading(r, t, w):
    [a1] = send(r, u.build_add(title="Deposit", list_id=w.party, heading="Venue"))
    [a2] = send(r, u.build_add(title="Cake", list_id=w.party, heading_id=w.venue))
    [a3] = send(r, u.build_add(title="Balloons", list_id=w.party, heading="No such heading"))
    assert t.get(a1)["heading"] == t.get(a2)["heading"] == w.venue
    assert t.get(a3)["heading"] is None and t.get(a3)["project"] == w.party      # lands at the root
    assert t.ignored == ["heading not found in the project; the to-do is at its root"]


def test_add_by_list_title(r, t, w):
    [tid] = send(r, u.build_add(title="Shop", list="Plan Birthday Party"))
    assert t.get(tid)["project"] == w.party
    [tid] = send(r, u.build_add(title="File taxes", list="Work"))
    assert t.get(tid)["area"] == w.work
    assert send(r, u.build_add(title="Lost", list="No Such Area")) == []          # TAPI §2a: complete no-op
    assert t.by_title("Lost") == []


def test_add_completed_or_canceled(r, t):
    [c] = send(r, u.build_add(title="Done already", completed=True))
    [x] = send(r, u.build_add(title="Never mind", canceled=True))
    assert (t.get(c)["status"], t.get(c)["completed"]) == ("completed", t.now())
    assert (t.get(x)["status"], t.get(x)["canceled"], t.get(x)["completed"]) == ("canceled", t.now(), None)
    assert members(t, "logbook") == {c, x} and members(t, "inbox") == set()


def test_add_into_a_closed_project_reopens_it(r, t, w):
    t.get(w.party).update(status="completed", completed=t.now(), logged=True)
    send(r, u.build_add(title="One more thing", list_id=w.party))
    assert t.get(w.party)["status"] == "open" and t.get(w.party)["logged"] is False


def test_add_creation_date(r, t):
    past = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    [tid] = send(r, u.build_add(title="Old", creation_date=past))
    assert t.get(tid)["created"] == past.astimezone().replace(tzinfo=None)
    [tid] = send(r, u.build_add(title="Future", creation_date=datetime(2030, 1, 1, tzinfo=timezone.utc)))
    assert t.get(tid)["created"] == t.now()                         # "Ignored if the date is in the future"


def test_add_reveal_and_quick_entry(r, t):
    [tid] = send(r, u.build_add(title="Show me", reveal=True))
    assert t.revealed == [tid]
    assert send(r, u.build_add(title="Quick", show_quick_entry=True)) == []
    assert t.by_title("Quick") == []


# ---- add-project -------------------------------------------------------------------------------

def test_add_project_with_todos(r, t, w):
    turl = u.build_add_project(title="Build treehouse", notes="Oak", when="someday", deadline="2026-12-31",
                               tags=["Home", "Nope"], area_id=w.family, to_dos=["Plans", "Wood"])
    assert turl.weight == 3
    [pid] = send(r, turl)
    p = t.get(pid)
    assert (p["kind"], p["notes"], p["start"], p["deadline"], p["tags"], p["area"]) == \
        ("project", "Oak", "someday", "2026-12-31", ["Home"], w.family)
    kids = t.children(pid)
    assert [c["title"] for c in kids] == ["Plans", "Wood"]
    assert all((c["start"], c["project"], c["area"]) == ("anytime", pid, None) for c in kids)
    assert pid in members(t, "someday")
    assert not {c["id"] for c in kids} & set().union(*(members(t, k) for k in BUILTIN_LISTS))   # dormant (Q14)


def test_add_project_by_area_title_and_default_start(r, t, w):
    [pid] = send(r, u.build_add_project(title="Plan party", area="Family"))
    assert (t.get(pid)["area"], t.get(pid)["start"]) == (w.family, "anytime")
    assert pid in members(t, "anytime")


def test_add_project_completed_completes_its_todos(r, t):
    [pid] = send(r, u.build_add_project(title="Archive", to_dos=["a", "b"], completed=True))
    assert [c["status"] for c in t.children(pid)] == ["completed", "completed"]
    assert t.get(pid)["status"] == "completed"


# ---- update / update-project -------------------------------------------------------------------------

def test_update_text_fields(r, t, w):
    send(r, u.build_update(id=w.inbox, title="Buy bread", notes="Wholemeal"))
    send(r, u.build_update(id=w.inbox, prepend_notes="P0 "))
    send(r, u.build_update(id=w.inbox, append_notes=" Z9"))
    it = t.get(w.inbox)
    assert (it["title"], it["notes"]) == ("Buy bread", "P0 Wholemeal Z9")
    assert it["modified"] == t.now()
    send(r, u.build_update(id=w.inbox, notes=""))                    # "param=" clears
    assert t.get(w.inbox)["notes"] == ""


def test_update_schedule_and_deadline(r, t, w):
    send(r, u.build_update(id=w.inbox, when="tomorrow", deadline="2026-10-09"))
    it = t.get(w.inbox)
    assert (it["start"], it["deadline"]) == ("2026-09-29", "2026-10-09")
    assert w.inbox in members(t, "upcoming") and w.inbox not in members(t, "inbox")
    send(r, u.build_update(id=w.inbox, when="evening"))
    assert (t.get(w.inbox)["start"], t.get(w.inbox)["evening"]) == ("2026-09-28", True)
    send(r, u.build_update(id=w.inbox, deadline=u.CLEAR))
    assert t.get(w.inbox)["deadline"] is None
    send(r, u.build_update(id=w.inbox, when="someday"))
    assert members(t, "someday") >= {w.inbox} and w.inbox not in members(t, "today")


def test_update_tags(r, t, w):
    send(r, u.build_update(id=w.inbox, tags=["Home", "Nope"]))
    assert t.get(w.inbox)["tags"] == ["Home"]
    send(r, u.build_update(id=w.inbox, add_tags=["Errand", "Home", "Nope"]))
    assert t.get(w.inbox)["tags"] == ["Home", "Errand"]
    send(r, u.build_update(id=w.inbox, tags=[]))                      # tags= replaces with none
    assert t.get(w.inbox)["tags"] == []


def test_update_checklist_prepend_append(r, t, w):
    send(r, u.build_add(title="Pack", checklist_items=["b"]))
    tid = t.one("Pack")["id"]
    send(r, u.build_update(id=tid, prepend_checklist_items=["a"]))
    send(r, u.build_update(id=tid, append_checklist_items=["c", "d"]))
    assert [c["title"] for c in t.get(tid)["checklist"]] == ["a", "b", "c", "d"]


def test_update_moves_between_containers(r, t, w):
    send(r, u.build_update(id=w.inbox, list_id=w.party, heading="Venue"))
    it = t.get(w.inbox)
    assert (it["project"], it["area"], it["heading"], it["start"]) == (w.party, None, w.venue, "anytime")
    assert w.inbox not in members(t, "inbox") and w.inbox in members(t, "anytime")
    send(r, u.build_update(id=w.inbox, list_id=w.work))
    it = t.get(w.inbox)
    assert (it["project"], it["area"], it["heading"]) == (None, w.work, None)
    send(r, u.build_update(id=w.inbox, list="No Such Project", title="Still renamed"))
    assert t.get(w.inbox)["area"] == w.work and t.get(w.inbox)["title"] == "Still renamed"
    send(r, u.build_update(id=w.chore, heading="Venue"))              # not in that project: ignored
    assert t.get(w.chore)["heading"] is None


def test_update_status_changes(r, t, w):
    send(r, u.build_update(id=w.inbox, completed=True))
    assert t.get(w.inbox)["status"] == "completed" and w.inbox in members(t, "logbook")
    send(r, u.build_update(id=w.inbox, completed=False))             # completed=false reopens
    it = t.get(w.inbox)
    assert (it["status"], it["completed"], it["logged"]) == ("open", None, False)
    assert w.inbox in members(t, "inbox")
    send(r, u.build_update(id=w.inbox, canceled=True))
    assert t.get(w.inbox)["status"] == "canceled"
    send(r, u.build_update(id=w.inbox, completed=False))             # "…on a canceled to-do will also mark it
    assert t.get(w.inbox)["status"] == "open"                        #  as incomplete"
    send(r, u.build_update(id=w.inbox, completed=True))
    send(r, u.build_update(id=w.inbox, canceled=False))
    assert t.get(w.inbox)["status"] == "open"


def test_update_requires_a_todo_id(r, t, w):
    send(r, u.build_update(id=w.party, title="Renamed"))
    send(r, u.build_update(id="SyJEz273ceSkabUbciM73A", title="Ghost"))
    assert t.get(w.party)["title"] == "Plan Birthday Party"
    assert len(t.errors) == 2 and t.sheet is None


def test_update_duplicate_leaves_the_original(r, t, w):
    [dup] = send(r, u.build_update(id=w.inbox, title="Copy", duplicate=True))
    assert dup != w.inbox
    assert (t.get(dup)["title"], t.get(w.inbox)["title"]) == ("Copy", "Inbox item")


def test_repeating_template_when_crashes_things(r, t, w):
    rep = t.seed_todo("Water plants", repeating=True, when="anytime")
    assert rep not in set().union(*(members(t, k) for k in BUILTIN_LISTS))   # invisible to lists
    r.dispatch(u.build_update(id=rep, when="today", title="Renamed"))
    assert t.crashes == 1 and t.running is False
    assert t.get(rep)["title"] == "Water plants"                      # nothing applied
    with pytest.raises(ThingsNotRunning):
        r.run_template("SCAN", ["app", "", "0", "0"])
    assert r.ensure_running(allow_launch=True) is True


def test_repeating_template_drops_deadline_and_status(r, t):
    rep = t.seed_todo("Water plants", repeating=True)
    send(r, u.build_update(id=rep, deadline="2026-10-01", title="Water all plants"))
    send(r, u.build_update(id=rep, completed=True))
    it = t.get(rep)
    assert (it["title"], it["deadline"], it["status"]) == ("Water all plants", None, "open")
    assert "deadline on a repeating to-do" in t.ignored and t.crashes == 0


def test_update_project(r, t, w):
    send(r, u.build_update_project(id=w.party, title="Party!", prepend_notes="P0 ", add_tags=["Errand"],
                                   area_id=w.work))
    p = t.get(w.party)
    assert (p["title"], p["notes"], p["tags"], p["area"]) == ("Party!", "P0 Party notes", ["Home", "Errand"], w.work)
    send(r, u.build_update_project(id=w.party, deadline=u.CLEAR, when="2026-10-10"))
    assert (t.get(w.party)["deadline"], t.get(w.party)["start"]) == (None, "2026-10-10")
    assert w.party in members(t, "upcoming")
    assert w.book not in set().union(*(members(t, k) for k in BUILTIN_LISTS))   # dormant child (Q14)
    assert w.milk in members(t, "today")                              # has its own start date
    send(r, u.build_update_project(id=w.inbox, title="not a project"))
    assert t.get(w.inbox)["title"] == "Inbox item"


def test_completing_a_project_with_open_children_is_ignored(r, t, w):
    send(r, u.build_update_project(id=w.party, completed=True))
    assert t.get(w.party)["status"] == "open"
    assert any("still has open to-dos" in s for s in t.ignored)


def test_completing_a_project_cascades_in_tapi_mode(r, t, w):
    t.project_complete_mode = "cascade"
    send(r, u.build_update_project(id=w.party, canceled=True))
    assert t.get(w.party)["status"] == "canceled"
    assert {c["id"]: c["status"] for c in t.children(w.party)} == {
        w.milk: "canceled", w.book: "canceled", w.done: "completed"}
    assert t.headings[w.venue]["archived"] is True


def test_project_completion_needs_archived_headings(r, t, w):
    for tid in (w.milk, w.book):
        send(r, u.build_update(id=tid, completed=True))
    send(r, u.build_update_project(id=w.party, completed=True))
    assert t.get(w.party)["status"] == "open"                        # heading "Venue" isn't archived
    t.headings[w.venue]["archived"] = True
    send(r, u.build_update_project(id=w.party, completed=True))
    assert t.get(w.party)["status"] == "completed" and w.party in members(t, "logbook")
    send(r, u.build_update_project(id=w.party, completed=False))
    assert t.get(w.party)["status"] == "open"


def test_moving_an_open_todo_into_a_closed_project_reopens_it(r, t, w):
    [pid] = send(r, u.build_add_project(title="Closed", canceled=True))
    send(r, u.build_update(id=w.inbox, list_id=pid))
    assert t.get(pid)["status"] == "open"
    t.reopen_project_on_add = False
    [pid2] = send(r, u.build_add_project(title="Closed 2", completed=True))
    send(r, u.build_update(id=w.chore, list_id=pid2))
    assert t.get(pid2)["status"] == "completed"


# ---- json -------------------------------------------------------------------------------------------

def test_json_create_todo_in_a_project_under_a_heading(r, t, w):
    turl = u.build_json([j.Todo(title="Deposit", notes="Cash", when="tomorrow", deadline="2026-10-05",
                                tags=["Errand"], list_id=w.party, heading_id=w.venue,
                                checklist_items=[j.ChecklistItem("Hotels", completed=True),
                                                 j.ChecklistItem("Transport")])])
    assert turl.requires_token is False
    [tid] = send(r, turl)
    it = t.get(tid)
    assert (it["title"], it["notes"], it["start"], it["deadline"], it["tags"], it["project"], it["heading"]) == \
        ("Deposit", "Cash", "2026-09-29", "2026-10-05", ["Errand"], w.party, w.venue)
    assert it["checklist"] == [{"title": "Hotels", "completed": True, "canceled": False},
                               {"title": "Transport", "completed": False, "canceled": False}]


def test_json_create_project_with_headings(r, t):
    turl = u.build_json([j.Project(title="Gamma", items=[
        j.Todo(title="g1"), j.Heading("H1", archived=True), j.Todo(title="g2", completed=True),
        j.Heading("H2", archived=True), j.Todo(title="g3")])])
    assert turl.weight == 6
    [pid] = send(r, turl)
    kids = t.children(pid)
    assert [(c["title"], t.heading_title(c), c["status"]) for c in kids] == [
        ("g1", None, "open"), ("g2", "H1", "completed"), ("g3", "H2", "open")]
    archived = {h["title"]: h["archived"] for h in t.headings.values() if h["project"] == pid}
    assert archived == {"H1": True, "H2": False}                     # ignored unless its to-dos are closed


def test_json_create_completed_project_with_open_todos_lands_open(r, t):
    t.project_complete_mode = "cascade"                               # json create never cascades (TAPI §5q)
    [pid] = send(r, u.build_json([j.Project(title="P", completed=True, items=[j.Todo(title="open")])],
                                 strict=False))
    assert t.get(pid)["status"] == "open"
    [pid] = send(r, u.build_json([j.Project(title="Q", completed=True,
                                            items=[j.Todo(title="done", completed=True)])]))
    assert t.get(pid)["status"] == "completed"


def test_json_update_operations(r, t, w):
    turl = u.build_json([j.Todo(operation="update", id=w.milk, when="anytime"),
                         j.Todo(operation="update", id=w.inbox, completed=True),
                         j.Project(operation="update", id=w.party, title="Party", deadline="2026-11-01")])
    assert turl.requires_token is True
    assert send(r, turl) == []                                        # updates create nothing
    assert t.get(w.milk)["start"] == "anytime"                        # Q10: anytime via json
    assert w.milk not in members(t, "today") and w.milk in members(t, "anytime")
    assert t.get(w.inbox)["status"] == "completed"
    assert (t.get(w.party)["title"], t.get(w.party)["deadline"]) == ("Party", "2026-11-01")
    send(r, u.build_json([j.Todo(operation="update", id=w.inbox, completed=False)]))
    assert t.get(w.inbox)["status"] == "open"


def test_json_update_non_strict_fields(r, t, w):
    send(r, u.build_json([j.Todo(operation="update", id=w.inbox, add_tags=["Errand"], append_notes=" !",
                                 append_checklist_items=[j.ChecklistItem("x")])], strict=False))
    it = t.get(w.inbox)
    assert (it["tags"], it["notes"], [c["title"] for c in it["checklist"]]) == (["Errand"], " !", ["x"])


def test_official_json_example(r, t):
    family, work = t.seed_area("Family"), t.seed_area("Work")
    t.seed_tag("Errand")
    items = j.from_objs(json.loads(OFFICIAL_JSON.read_text()))
    created = send(r, u.build_json(items))
    shopping, rome, cleaning, report = (t.get(i) for i in created)
    assert [c["title"] for c in t.children(shopping["id"])] == ["Bread", "Milk"]
    assert (rome["title"], rome["notes"], rome["area"]) == ("Vacation in Rome", "Some time in August.", family)
    kids = {c["title"]: c for c in t.children(rome["id"])}
    assert [(c["title"], t.heading_title(c)) for c in t.children(rome["id"])] == [
        ("Ask Sarah for travel guide", None), ("Add dates to calendar", None), ("Vatican City", "Sights"),
        ("The Colosseum", "Sights"), ("Call Paolo", "Planning"), ("Book flights", "Planning"),
        ("Research", "Planning")]
    assert kids["The Colosseum"]["notes"] == "12€"
    assert kids["Call Paolo"]["status"] == "completed"
    assert kids["Book flights"]["start"] == TODAY.isoformat()
    assert kids["Research"]["checklist"] == [{"title": "Hotels", "completed": True, "canceled": False},
                                             {"title": "Transport from airport", "completed": False,
                                              "canceled": False}]
    assert (cleaning["evening"], cleaning["tags"], cleaning["project"]) == (True, ["Errand"], None)
    assert (report["deadline"], report["area"]) == ("2026-02-01", work)


def test_json_payloads_things_rejects(r, t, w):
    def raw(data: str) -> None:
        t.open_url(u.ThingsURL("json", (("data", data),), 1, False).url)

    raw(json.dumps([{"type": "to-do", "attributes": {"title": "ok"}},
                    {"type": "to-do", "attributes": {"title": "bad", "checklist-items": ["a", "b"]}}]))
    raw(json.dumps([{"type": "to-do", "attributes": {"title": "ok2"}}, {"type": "area", "attributes": {}}]))
    raw(json.dumps([{"type": "project", "attributes": {"title": "p", "items": [{"type": "project"}]}}]))
    assert t.by_title("ok") == t.by_title("ok2") == t.by_title("p") == []   # one bad element voids the batch
    assert sum("voided" in s for s in t.ignored) == 3
    raw("[not json")
    assert t.sheet == "error" and t.errors == ["json: data is not valid JSON"]


def test_raw_urls_things_ignores_or_rejects(t):
    t.open_url("things:///add?title=Hello%2Bworld&x-success=evil%3A%2F%2F&bogus=1")
    assert t.one("Hello+world")                                       # "+" is literal, x- and unknown ignored
    t.open_url("things:///delete?id=abc")
    assert t.sheet == "error" and "unsupported command 'delete'" in t.errors
    t.dismiss_sheet()
    t.open_url("things:///update?id=abc&title=x")                     # no token at all
    assert t.sheet == "error"


# ---- render: every template parses ---------------------------------------------------------------

def template_args(w) -> dict[str, list[str]]:
    return {
        "SCAN": ["list", "today", "1", "0"],
        "ROWS": ["1", w.milk, w.party, w.family, w.done, "GJJVZHE7SNu7xcVuH2xDDh"],
        "LOGBOOK": ["50", "", "1"],
        "PROJECT_AREAS": [],
        "AREAS": [],
        "TAGS": [],
        "MEMBERSHIP": ["inbox", "today", "anytime", "upcoming", "someday", "logbook", "trash", "tomorrow"],
        "FIND_CREATED": ["to-do", "Inbox item", "7200", "list", "inbox", "1"],
        "PROBE": [w.party],
        "AUTOMATION": [],
        "APP_VERSION": [],
    }


CONVERTERS = {
    "SCAN": [a.scan_rows],
    "ROWS": [lambda f: a.items_from_frame(f, notes_mode="full"), a.areas_from_frame, a.missing_ids],
    "LOGBOOK": [lambda f: a.items_from_frame(f, notes_mode="preview")],
    "PROJECT_AREAS": [a.project_areas_from_frame],
    "AREAS": [a.areas_from_frame],
    "TAGS": [a.tags_from_frame],
    "MEMBERSHIP": [a.membership_from_frame],
    "FIND_CREATED": [a.created_from_frame, a.diag_from_frame],
    "PROBE": [a.probe_results],
    "AUTOMATION": [a.probe_results],
    "APP_VERSION": [a.probe_results],
}


def test_arg_table_covers_every_template(w):
    assert set(template_args(w)) == set(TEMPLATES) == set(CONVERTERS)


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_every_template_renders_a_frame_the_parser_accepts(r, t, w, name):
    args = template_args(w)[name]
    raw = t.render(name, args)
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")          # osascript's single newline
    records = raw[:-1].decode("utf-8").split(RS)
    assert records[0].startswith("H" + US + "tb1" + US + a.HEADER_NAMES[name])
    assert records[-1].split(US)[0] == "E"
    f = parse_frame(raw, name)
    assert f.header[2] == a.HEADER_NAMES[name]
    assert f.rows and {row[0] for row in f.rows} <= a.RECORD_TAGS[name]
    for convert in CONVERTERS[name]:
        convert(f)
    assert r.run_template(name, args) == f                            # the runner returns the same frame


def _header_params(name: str) -> list[str] | None:
    """The expressions after the template name in the template's own H record (None: APP_VERSION form)."""
    m = re.search(r'\{"H", "tb1", "[a-z-]+"((?:, [^{}]+?)*)\}', TEMPLATES[name].body)
    if m is None:
        return None
    return [p for p in m.group(1).split(", ") if p]


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_header_matches_the_template_source(t, w, name):
    """Each H field comes from the AppleScript H record: argv values pass through raw, the last field is
    `time to GMT`."""
    args = template_args(w)[name]
    header = t.frame(name, args).header
    params = _header_params(name)
    if params is None:
        assert name == "APP_VERSION" and header == ["H", "tb1", "app-version"]
        return
    assert len(header) == 3 + len(params)
    assert params[-1] == "(time to GMT) as text" and header[-1] == str(t.time_to_gmt())
    argv_vars = {v: int(n) for v, n in re.findall(r"set (v_\w+) to my tb_arg\(argv, (\d)\)", TEMPLATES[name].body)}
    for expr, value in zip(params[:-1], header[3:-1], strict=True):
        if expr in argv_vars:
            assert value == args[argv_vars[expr] - 1], expr
    assert sum(p in argv_vars for p in params) == {"SCAN": 2, "LOGBOOK": 1, "FIND_CREATED": 3}.get(name, 0)


@pytest.fixture
def kolkata():
    """Process time zone +05:30 (no DST) for one test, then the previous zone again."""
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is Unix-only")
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Kolkata"
    time.tzset()
    try:
        if time.localtime(1_790_000_000).tm_gmtoff != 19800:
            pytest.skip("Asia/Kolkata zone data not available")
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def test_time_to_gmt_is_the_local_offset(kolkata, t, w):
    assert t.time_to_gmt() == 19800
    assert t.frame("AREAS").header == ["H", "tb1", "areas", "19800"]
    [milk] = a.items_from_frame(t.frame("ROWS", ["0", w.milk]), notes_mode="none")
    assert milk.created.endswith("+05:30")


# ---- render: SCAN -------------------------------------------------------------------------------

def test_scan_list_today(r, t, w):
    f = r.run_template("SCAN", ["list", "today", "1", "0"])
    assert f.header == ["H", "tb1", "scan", "list", "today", str(t.time_to_gmt())]
    assert f.of("S") == [
        ["S", w.milk, "Buy milk 🥛", "open", "Errand, Home", "2026-10-02", NOTES],
        ["S", w.evening, "Collect dry cleaning", "open", "", "", ""],          # This Evening at the bottom
    ]
    no_notes = r.run_template("SCAN", ["list", "today", "0", "0"])
    assert [row[6] for row in no_notes.of("S")] == ["", ""]
    assert t.render("SCAN", ["list", "today", "1", "1"]) == t.render("SCAN", ["list", "today", "1", "0"])
    assert t.render("SCAN", ["LIST", "Today", "1", "0"]).split(RS.encode())[1:] == \
        t.render("SCAN", ["list", "today", "1", "0"]).split(RS.encode())[1:]  # AppleScript `is` ignores case


def test_scan_app_and_projects(r, t, w):
    app = {row.id for row in a.scan_rows(r.run_template("SCAN", ["app", "", "0", "0"]))}
    assert app == {w.inbox, w.milk, w.book, w.evening, w.chore, w.someday, w.later}   # not logged, no projects
    t.app_todos_include_projects = True
    assert w.party in {row.id for row in a.scan_rows(r.run_template("SCAN", ["app", "", "0", "0"]))}
    t.log_immediately = False
    send(r, u.build_update(id=w.inbox, completed=True))
    rows = {row.id: row.status for row in a.scan_rows(r.run_template("SCAN", ["app", "", "0", "0"]))}
    assert rows[w.inbox] == "completed"                               # completed but not yet logged
    t.log_completed_now()
    assert w.inbox not in {row.id for row in a.scan_rows(r.run_template("SCAN", ["app", "", "0", "0"]))}
    projects = a.scan_rows(r.run_template("SCAN", ["projects", "", "1", "0"]))
    assert [(p.id, p.title, p.tags, p.deadline, p.notes) for p in projects] == [
        (w.party, "Plan Birthday Party", ["Home"], date(2026, 10, 20).isoformat(), "Party notes")]


def test_scan_containers(r, t, w):
    rows = a.scan_rows(r.run_template("SCAN", ["project", w.party, "0", "0"]))
    assert [(row.title, row.status) for row in rows] == [
        ("Buy milk 🥛", "open"), ("Call Paolo", "completed"), ("Book venue", "open")]   # heading children last
    rows = a.scan_rows(r.run_template("SCAN", ["area", w.family, "0", "0"]))
    assert [(row.title, row.status, row.tags) for row in rows] == [("Old idea", "canceled", ["Deep Work"])]
    with pytest.raises(NotFound):
        r.run_template("SCAN", ["project", w.inbox, "0", "0"])        # a to-do id is not a project
    with pytest.raises(NotFound):
        r.run_template("SCAN", ["area", "GJJVZHE7SNu7xcVuH2xDDh", "0", "0"])
    with pytest.raises(InvalidInput) as e:
        r.run_template("SCAN", ["list", "evening", "0", "0"])    # "unknown list key" (9002)
    assert e.value.details["osa_code"] == 9002
    with pytest.raises(InvalidInput):
        r.run_template("SCAN", ["galaxy", "", "0", "0"])


def test_separators_in_user_text_are_stripped(r, t):
    tid = t.seed_todo(f"a{US}b{RS}c", notes=f"n{RS}o{US}", tags=[], when="today")
    [row] = a.scan_rows(r.run_template("SCAN", ["list", "today", "1", "0"]))
    assert (row.id, row.title, row.notes) == (tid, "abc", "no")
    [item] = a.items_from_frame(r.run_template("ROWS", ["1", tid]), notes_mode="full")
    assert (item.title, item.notes) == ("abc", "no")


# ---- render: ROWS / LOGBOOK ---------------------------------------------------------------------------

def test_rows_frame(r, t, w):
    f = r.run_template("ROWS", ["1", w.milk, w.party, w.family, "GJJVZHE7SNu7xcVuH2xDDh", w.chore])
    assert [row[0] for row in f.rows] == ["T", "T", "A", "X", "T", "R", "R", "R"]
    created = t.get(w.milk)["created"].strftime("%Y-%m-%dT%H:%M:%S")
    assert f.of("T")[0] == ["T", w.milk, "to-do", "Buy milk 🥛", "open", "Errand, Home", "2026-10-02",
                            "2026-09-28", "2026-09-28T00:00:00", created, created, "", "", w.party, "", NOTES]
    assert f.of("R") == [["R", "project", w.party, "Plan Birthday Party", w.family],
                         ["R", "area", w.family, "Family", ""], ["R", "area", w.work, "Work", ""]]
    assert f.of("A") == [["A", w.family, "Family", "Home"]]
    assert a.missing_ids(f) == ["GJJVZHE7SNu7xcVuH2xDDh"]
    milk, party, chore = a.items_from_frame(f, notes_mode="full")
    assert isinstance(milk, Item)
    assert (milk.project.id, milk.project.title, milk.area.id, milk.area.title) == \
        (w.party, "Plan Birthday Party", w.family, "Family")         # area via the project
    assert (milk.notes, milk.start_date, milk.deadline, milk.tags) == (NOTES, "2026-09-28", "2026-10-02",
                                                                       ["Errand", "Home"])
    assert milk.created == t.get(w.milk)["created"].astimezone().isoformat(timespec="seconds")
    assert (party.kind, party.project, party.area.id, party.notes) == ("project", None, w.family, "Party notes")
    assert (chore.project, chore.area.id, chore.area.title) == (None, w.work, "Work")
    assert a.items_from_frame(r.run_template("ROWS", ["0", w.milk]), notes_mode="full")[0].notes == ""


def test_rows_status_dates(r, t, w):
    done, dropped = a.items_from_frame(r.run_template("ROWS", ["0", w.done, w.dropped]), notes_mode="none")
    assert (done.status, done.completed_at, done.canceled_at) == \
        ("completed", datetime(2026, 9, 27, 18, 30).astimezone().isoformat(), None)
    assert (dropped.status, dropped.completed_at, dropped.canceled_at) == \
        ("canceled", None, datetime(2026, 9, 22, 7, 0).astimezone().isoformat())


def test_rows_project_id_whether_or_not_to_do_id_resolves_it(t, w):
    before = t.render("ROWS", ["0", w.party])
    t.todo_id_resolves_projects = False
    assert t.render("ROWS", ["0", w.party]) == before


def test_logbook(r, t, w):
    [pid] = send(r, u.build_add_project(title="Shipped", completed=True))
    f = r.run_template("LOGBOOK", ["50", "", "1"])
    assert f.header[:5] == ["H", "tb1", "logbook", "3", ""]
    items = a.items_from_frame(f, notes_mode="full")
    assert [(i.id, i.kind) for i in items] == [(pid, "project"), (w.done, "to-do"), (w.dropped, "to-do")]
    assert [row[1:3] for row in f.of("R")] == [["project", w.party], ["area", w.family]]
    assert items[1].project.title == "Plan Birthday Party" and items[2].area.title == "Family"
    limited = r.run_template("LOGBOOK", ["2", "", "0"])
    assert [row[1] for row in limited.of("T")] == [pid, w.done] and limited.header[3] == "3"
    since = r.run_template("LOGBOOK", ["50", "2026-09-23", "0"])      # stops at the first older stop date
    assert [row[1] for row in since.of("T")] == [pid, w.done] and since.header[4] == "2026-09-23"
    assert r.run_template("LOGBOOK", ["0", "", "0"]).of("T") == []
    with pytest.raises(ReadFailed) as e:
        r.run_template("LOGBOOK", ["many", "", "0"])
    assert e.value.details["osa_code"] == -1700


# ---- render: small templates ---------------------------------------------------------------------

def test_small_frames_byte_exact(t, w):
    gmt = str(t.time_to_gmt()).encode()
    assert t.render("AREAS") == (b"H\x1ftb1\x1fareas\x1f" + gmt + b"\x1eA\x1f" + w.family.encode()
                                 + b"\x1fFamily\x1fHome\x1eA\x1f" + w.work.encode() + b"\x1fWork\x1f\x1eE\x1f2\n")
    assert t.render("AUTOMATION") == b"H\x1ftb1\x1fautomation\x1f" + gmt + b"\x1eK\x1fautomation\x1f1\x1f2\x1eE\x1f1\n"
    assert t.render("APP_VERSION") == b"H\x1ftb1\x1fapp-version\x1eK\x1fapp-version\x1f1\x1f3.22.1\x1f1\x1eE\x1f1\n"
    assert t.render("PROJECT_AREAS") == (b"H\x1ftb1\x1fproject-areas\x1f" + gmt + b"\x1ePA\x1f" + w.party.encode()
                                         + b"\x1f" + w.family.encode() + b"\x1eE\x1f1\n")


def test_tags_and_areas_convert(r, w):
    tags = a.tags_from_frame(r.run_template("TAGS"))
    assert [(g.id, g.title, g.parent.title if g.parent else None, g.shortcut) for g in tags] == [
        (w.home, "Home", None, "h"), (w.errand, "Errand", "Home", None), (w.deep, "Deep Work", None, None)]
    areas = a.areas_from_frame(r.run_template("AREAS"))
    assert [(x.title, x.tags) for x in areas] == [("Family", ["Home"]), ("Work", [])]
    assert a.project_areas_from_frame(r.run_template("PROJECT_AREAS")) == {w.party: w.family}


# ---- render: MEMBERSHIP ------------------------------------------------------------------------------

@pytest.fixture
def lists(t):
    """One item per list rule (today is 2026-09-28)."""
    ns = SimpleNamespace()
    ns.inbox = t.seed_todo("I")
    ns.due = t.seed_todo("D", deadline="2026-09-28")                  # Inbox, and Today by its deadline
    ns.today = t.seed_todo("T", when="today")
    ns.evening = t.seed_todo("E", when="evening")
    ns.overdue = t.seed_todo("O", when="anytime", deadline="2026-09-01")
    ns.anytime = t.seed_todo("A", when="anytime")
    ns.future = t.seed_todo("U", when="2026-10-05")
    ns.tomorrow = t.seed_todo("M", when="tomorrow")
    ns.someday = t.seed_todo("S", when="someday")
    ns.done = t.seed_todo("C", status="completed")
    ns.trashed = t.seed_todo("X", when="today", trashed=True)
    ns.template = t.seed_todo("R", when="anytime", repeating=True)
    ns.active = t.seed_project("P1")
    ns.active_child = t.seed_todo("P1c", project=ns.active)
    ns.sleeping = t.seed_project("PS", when="someday")
    ns.sleeping_child = t.seed_todo("PSc", project=ns.sleeping)
    ns.future_project = t.seed_project("PF", when="2026-10-10")
    ns.future_child = t.seed_todo("PFc", project=ns.future_project)
    ns.future_child_today = t.seed_todo("PFt", project=ns.future_project, when="today")
    return ns


def test_membership_follows_the_list_rules(r, t, lists):
    m = a.membership_from_frame(r.run_template("MEMBERSHIP", list(BUILTIN_LISTS)))
    n = lists
    assert set(m["inbox"]) == {n.inbox, n.due}
    assert set(m["today"]) == {n.due, n.today, n.evening, n.overdue, n.future_child_today}
    assert m["today"][-1] == n.evening
    assert set(m["anytime"]) == {n.today, n.evening, n.overdue, n.anytime, n.active, n.active_child,
                                 n.future_child_today}
    assert set(m["upcoming"]) == {n.future, n.tomorrow, n.future_project}
    assert m["upcoming"].index(n.tomorrow) < m["upcoming"].index(n.future) < m["upcoming"].index(n.future_project)
    assert set(m["someday"]) == {n.someday, n.sleeping}
    assert m["logbook"] == [n.done] and m["trash"] == [n.trashed] and m["tomorrow"] == [n.tomorrow]
    everywhere = set().union(*m.values())
    assert not {n.template, n.sleeping_child, n.future_child} & everywhere
    assert m["anytime"].count(n.active) == 2                          # from `to dos` and from `projects`


def test_membership_knobs_and_errors(r, t, lists):
    t.list_todos_include_projects = False
    m = a.membership_from_frame(r.run_template("MEMBERSHIP", ["anytime", "Someday"]))
    assert m["anytime"].count(lists.active) == 1 and set(m["Someday"]) == {lists.someday, lists.sleeping}
    with pytest.raises(InvalidInput) as e:
        r.run_template("MEMBERSHIP", ["evening"])
    assert e.value.details["osa_code"] == 9002
    t.missing_lists = {"tomorrow"}
    with pytest.raises(NotFound):
        r.run_template("MEMBERSHIP", ["tomorrow"])


# ---- render: FIND_CREATED ------------------------------------------------------------------------------

def test_find_created_ages_and_matching(r, t, w, clock):
    [new] = send(r, u.build_add(title="Buy milk 🥛", list_id=w.party, when="today"))
    clock.advance(4)
    f = r.run_template("FIND_CREATED", ["to-do", "Buy milk 🥛", "10", "project", w.party, "0"])
    assert f.header[3:6] == ["to-do", "project", w.party]
    assert a.created_from_frame(f) == [(new, 4)]                      # the seeded twin is too old
    wide = a.created_from_frame(r.run_template("FIND_CREATED", ["to-do", "Buy milk 🥛", "1000000", "app", "",
                                                                "0"]))
    assert sorted(wide, key=lambda x: x[1]) == [(new, 4), (w.milk, 86404)]
    assert a.created_from_frame(r.run_template("FIND_CREATED", ["to-do", "BUY MILK 🥛", "10", "app", "", "0"])) == []
    assert a.created_from_frame(r.run_template("FIND_CREATED", ["to-do", "Buy milk 🥛", "10", "list", "today",
                                                                "0"])) == [(new, 4)]
    f = r.run_template("FIND_CREATED", ["to-do", "Area chore", "3", "area", w.work, "1"])
    assert a.created_from_frame(f) == [] and a.diag_from_frame(f) == (8, 1)   # 4 s old > maxAge 3
    f = r.run_template("FIND_CREATED", ["to-do", "Area chore", "4", "area", w.work, "0"])
    assert a.created_from_frame(f) == [(w.chore, 4)] and a.diag_from_frame(f) is None


def test_find_created_projects_and_clock_skew(r, t, w):
    [pid] = send(r, u.build_add_project(title="New project", area_id=w.work))
    f = r.run_template("FIND_CREATED", ["project", "New project", "5", "area", w.family, "0"])  # scope ignored
    assert a.created_from_frame(f) == [(pid, 0)]
    t.seed_todo("Skewed", age=-5)
    t.seed_todo("Skewed", age=-6)
    ages = [age for _, age in a.created_from_frame(r.run_template("FIND_CREATED", ["to-do", "Skewed", "5", "app", "",
                                                                                   "0"]))]
    assert ages == [-5]                                               # `v_age >= -5`


# ---- render: PROBE ------------------------------------------------------------------------------------

def test_probe(r, t, w):
    results = {k: (ok, d) for k, ok, d in a.probe_results(r.run_template("PROBE", [w.party]))}
    assert results["version"] == (True, ["3.22.1"])
    assert results["list:today"] == (True, ["Today", "2"])
    assert results["list:logbook"] == (True, ["Logbook", "2"])
    assert results["range"] == (True, ["1"])
    assert results["list-projects"] == (True, ["0"])
    assert results["date-columns-aligned"] == (True, ["2", "2", "2"])
    assert results["app-todos-include-projects"] == (True, ["7", "0"])
    assert results["todo-id-resolves-project"] == (True, ["project"])
    assert set(results) == {"version", "range", "list-projects", "date-columns-aligned",
                            "app-todos-include-projects", "todo-id-resolves-project"} | \
        {f"list:{k}" for k in BUILTIN_LISTS}


def test_probe_failures_become_k_rows(r, t, w):
    t.missing_lists = {"trash", "tomorrow"}
    t.todo_id_resolves_projects = False
    f = r.run_template("PROBE", [w.party])
    results = {k: (ok, d) for k, ok, d in a.probe_results(f)}
    assert results["list:trash"] == (False, ["Things3 got an error: Can’t get list id \"TMTrashListSource\".",
                                             "-1728"])
    assert results["todo-id-resolves-project"][0] is False
    assert f.header[:3] == ["H", "tb1", "probe"] and len(f.of("K")) == 14
    empty = FakeThings(t.clock)
    results = {k: (ok, d) for k, ok, d in a.probe_results(empty.frame("PROBE"))}
    assert results["range"][0] is False and "todo-id-resolves-project" not in results
    assert a.probe_results(r.run_template("PROBE", [w.inbox]))[-1] == ("todo-id-resolves-project", True, ["to do"])


# ---- FakeThings: seeding and clock -------------------------------------------------------------------

def test_clock_alignment(t, clock):
    assert t.now().date() == t.today() == clock.today() == TODAY
    before = t.now()
    clock.advance(90)
    assert (t.now() - before).total_seconds() == 90


def test_ids_are_things_shaped_and_unique(t):
    ids = [t.seed_todo(str(i)) for i in range(200)]
    assert len(set(ids)) == 200 and all(u.ID_RE.fullmatch(i) and len(i) == 22 for i in ids)
    assert FakeThings(t.clock).new_id() == FakeThings(t.clock).new_id()          # deterministic per seed


def test_seeding_validates(t, w):
    with pytest.raises(ValueError):
        t.seed_todo("x", project=w.party, area=w.family)
    with pytest.raises(ValueError):
        t.seed_todo("x", project=w.inbox)
    with pytest.raises(ValueError):
        t.seed_todo("x", heading=w.venue)
    with pytest.raises(ValueError):
        t.seed_todo("x", id=w.inbox)
    with pytest.raises(ValueError):
        t.seed_todo("x", when="next week")
    with pytest.raises(ValueError):
        t.seed_tag("x", parent="No such tag")
    with pytest.raises(TypeError):
        t.seed_todo("x", colour="red")
    assert t.seed_todo("x", id="SyJEz273ceSkabUbciM73A") == "SyJEz273ceSkabUbciM73A"


def test_osa_error_stderr_shape():
    e = OsaError(-1728, "Things3 got an error: Can’t get project id \"X\".")
    assert a.parse_osa_error(e.stderr) == (-1728, "Things3 got an error: Can’t get project id \"X\".")


def test_things_state_is_only_reachable_through_urls_and_templates(r, t, w):
    """The fake's public write path is ``open_url``; reads go through ``render``."""
    before = t.render("ROWS", ["1", w.milk, w.party])
    r.run_template("SCAN", ["app", "", "1", "0"])
    r.run_template("MEMBERSHIP", ["today"])
    r.run_template("PROBE", [w.party])
    assert t.render("ROWS", ["1", w.milk, w.party]) == before and t.log == []
    assert timedelta(0) == t.now() - t.now()
