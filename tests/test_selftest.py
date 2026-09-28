"""selftest.py (DESIGN §2.11, §6.2, §7): the full run against FakeThings, the OwnedRunner write gate (direct URLs
and writes issued through ThingsService), close-out after failures, and the consent rules."""

import copy
import dataclasses
import inspect
import io
import json
import logging
import re
import subprocess
import sys
from datetime import date
from types import SimpleNamespace

import pytest

from conftest import FakeClock
from fakes import FAKE_TOKEN, FakeRunner, FakeThings
from things_bridge import authtoken, cli, jsoncmd, selftest
from things_bridge import runner as runner_mod
from things_bridge import urlscheme as u
from things_bridge.errors import NotMacOS, Refused
from things_bridge.models import ProjectPatch, ProjectSpec, TodoPatch, TodoSpec, link_for
from things_bridge.runner import Runner
from things_bridge.selftest import AFTER_FAILURE, NO_TOKEN, SELFTEST_PROJECT_TITLE, OwnedRunner, run_selftest
from things_bridge.service import Config, ThingsService

ALL_STEPS = [f"ST-{i}" for i in range(15)]
TOKEN_STEPS = (4, 5, 6, 7, 10, 11)


# ---- fixtures and helpers --------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolation(monkeypatch):
    """No real Keychain (no item stored), no leftover secrets, the package logger restored."""
    def no_security(*a, **k):
        raise AssertionError("tests must never run /usr/bin/security")

    monkeypatch.setattr(authtoken, "_security", no_security)
    monkeypatch.setattr(authtoken, "_keychain_token", lambda: None)
    monkeypatch.setattr(runner_mod, "_secrets", ())
    logger = logging.getLogger("things_bridge")
    level, handlers = logger.level, list(logger.handlers)
    yield
    logger.setLevel(level)
    logger.handlers[:] = handlers


@pytest.fixture
def token(monkeypatch):
    """The auth token, as THINGS_AUTH_TOKEN (what FakeRunner appends and FakeThings expects)."""
    monkeypatch.setenv("THINGS_AUTH_TOKEN", FAKE_TOKEN)


def snapshot(t: FakeThings) -> dict:
    return {"items": copy.deepcopy(t.items), "areas": copy.deepcopy(t.areas), "tags": copy.deepcopy(t.tags),
            "headings": copy.deepcopy(t.headings)}


@pytest.fixture
def lib(fake_things):
    """The user's own library, including leftovers of earlier selftest runs. The selftest must never change it."""
    t = fake_things
    ns = SimpleNamespace()
    ns.home = t.seed_tag("Home")
    ns.errand = t.seed_tag("Errand", parent="Home")
    ns.family = t.seed_area("Family")
    ns.work = t.seed_area("Work")
    ns.taxes = t.seed_project("Taxes", area=ns.work, age=86_400)
    ns.receipts_heading = t.seed_heading(ns.taxes, "Receipts")
    ns.milk = t.seed_todo("Buy milk", when="today", tags=["Errand"], deadline="2026-10-02", age=3_600)
    ns.receipts = t.seed_todo("File receipts", project=ns.taxes, heading=ns.receipts_heading, age=3_600)
    ns.someday = t.seed_todo("Learn watercolor", when="someday", age=3_600)
    ns.inbox = t.seed_todo("Inbox item", age=60)
    ns.done = t.seed_todo("Call Paolo", status="completed", age=7_200)
    ns.tmpl = t.seed_todo("Water plants", repeating=True, age=7_200)
    ns.old_run = t.seed_project(SELFTEST_PROJECT_TITLE, notes="Created by things-bridge selftest tb-00000000 at …",
                                age=86_400)
    ns.old_alpha = t.seed_todo("tb-00000000 alpha", project=ns.old_run, age=86_400)
    ns.old_logged = t.seed_project(SELFTEST_PROJECT_TITLE, status="completed", logged=True, age=172_800)
    ns.before = snapshot(t)
    return ns


def assert_untouched(t: FakeThings, lib) -> None:
    before = lib.before
    for kind in ("items", "areas", "tags", "headings"):
        now = getattr(t, kind)
        for id_, old in before[kind].items():
            assert id_ in now, f"{kind} {id_} disappeared"
            assert now[id_] == old, f"{kind} {id_} changed"


def go(r: FakeRunner, clock, *, config: Config | None = None, **kw):
    seen = []
    report = run_selftest(r, config or Config(), yes=True, progress=seen.append, today=clock.today, **kw)
    return report, seen


def by_name(report) -> dict:
    return {s.name: s for s in report.steps}


def failures(report) -> list:
    return [(s.name, s.detail) for s in report.steps if s.status == "fail"]


def objects(objs: list) -> list[dict]:
    """Every json object at any depth (items and checklist items included)."""
    out = []
    for o in objs:
        if isinstance(o, dict):
            out.append(o)
            for v in (o.get("attributes") or {}).values():
                if isinstance(v, list):
                    out += objects(v)
    return out


def payload(turl: u.ThingsURL) -> list:
    return json.loads(dict(turl.params)["data"])


def targets(turl: u.ThingsURL) -> set[str]:
    """Every id a URL points at: parameters, and json ids / container attributes at any depth."""
    params = dict(turl.params)
    out = {params[k] for k in ("id", "list-id", "area-id", "heading-id") if k in params}
    if turl.command == "json":
        for o in objects(payload(turl)):
            if o.get("id"):
                out.add(o["id"])
            attrs = o.get("attributes") or {}
            out |= {attrs[k] for k in ("list-id", "area-id", "heading-id") if attrs.get(k)}
    return out


def names_used(turl: u.ThingsURL) -> set[str]:
    """Parameter and json attribute keys, at any depth."""
    keys = {k for k, _ in turl.params}
    if turl.command == "json":
        for o in objects(payload(turl)):
            keys |= set(o.get("attributes") or {})
    return keys


def drop_first(monkeypatch, r: FakeRunner, match) -> list:
    """The first URL that ``match``es is handed to LaunchServices but never reaches Things."""
    dropped: list = []
    original = r.dispatch

    def dispatch(turl, *, dry_run=False):
        if not dropped and not dry_run and match(turl):
            dropped.append(turl)
            r.drop_next_url()
        return original(turl, dry_run=dry_run)

    monkeypatch.setattr(r, "dispatch", dispatch)
    return dropped


def run_token(t: FakeThings, report) -> str:
    notes = t.get(report.project_id)["notes"]
    m = re.search(r"\btb-[0-9a-f]{8}\b", notes)
    assert m, notes
    return m.group(0)


# ---- the full run ----------------------------------------------------------------------------------------------

def test_signature_and_constants():
    sig = inspect.signature(run_selftest)
    assert list(sig.parameters)[:5] == ["runner", "config", "yes", "probe_conflicts", "progress"]
    assert sig.parameters["yes"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["probe_conflicts"].default is False and sig.parameters["progress"].default is None
    assert SELFTEST_PROJECT_TITLE == "things-bridge selftest"
    assert issubclass(OwnedRunner, Runner)


def test_full_run_passes(fake_runner, fake_things, clock, lib, token):
    t, r = fake_things, fake_runner
    report, seen = go(r, clock)
    s = by_name(report)
    assert report.ok, failures(report)
    assert [x.name for x in report.steps if x.name.startswith("ST-")] == ALL_STEPS
    assert seen == report.steps                               # progress saw every step, in order
    for name in ALL_STEPS:
        want = {"ST-12": "skip", "ST-14": "info"}.get(name, "pass")
        assert s[name].status == want, (name, s[name].detail)
    assert "--probe-conflicts" in s["ST-12"].detail
    assert {"Q1", "Q3", "Q4", "Q5", "Q7", "Q8", "Q10", "Q14", "Q21", "Q22", "Q23", "Q24", "Q25", "Q26"} <= set(s)
    assert not {"Q9", "Q11", "Q12"} & set(s)
    assert all(x.status == "info" for x in report.steps if x.name.startswith("Q"))
    names = [x.name for x in report.steps]
    assert names.index("Q21") == names.index("ST-1") + 1      # answers follow the step that produced them

    # what it made: the project, alpha, beta, gamma-proj, g1, g2 and the unicode item, all closed and logged
    ids = report.created_ids
    assert len(ids) == len(set(ids)) == 7 and ids[0] == report.project_id
    p = t.get(report.project_id)
    assert (p["kind"], p["title"], p["status"]) == ("project", SELFTEST_PROJECT_TITLE, "completed")
    assert p["notes"].startswith("Created by things-bridge selftest tb-") and "never deletes" in p["notes"]
    tok = run_token(t, report)
    assert tok in s["ST-0"].detail
    for i in ids:
        it = t.get(i)
        assert it["status"] != "open" and it["logged"], it
        assert tok in (it["notes"] if i == report.project_id else it["title"])
    kinds = [t.get(i)["kind"] for i in ids]
    assert kinds.count("project") == 2 and kinds.count("to-do") == 5
    unicode_item = next(t.get(i) for i in ids if t.get(i)["title"].startswith("-tb-"))
    assert unicode_item["title"] == f"-{tok}{selftest.UNICODE_TAIL}" and unicode_item["status"] == "canceled"

    # nothing else changed, nothing deleted, no crash, no error sheet
    assert_untouched(t, lib)
    assert t.crashes == 0 and t.errors == [] and t.sheet is None

    # every write stayed inside the run's own items, within the documented budget, never as a dry run
    for call in r.dispatch_calls:
        assert targets(call.turl) <= set(ids), call.turl.url
        assert not call.dry_run and call.outcome == "applied"
        assert not names_used(call.turl) & {"list", "area", "area-id", "heading-id", "duplicate", "use-clipboard",
                                            "show-quick-entry", "creation-date", "completion-date"}
    assert {c.turl.command for c in r.dispatch_calls} == {"add-project", "add", "json", "update", "update-project"}
    assert len(r.dispatch_calls) < 30 and sum(c.turl.weight for c in r.dispatch_calls) < 60

    assert report.cleanup_note.startswith("Nothing was deleted.")
    assert f"{link_for(report.project_id)} (now in the Logbook)" in report.cleanup_note
    assert "things-bridge never deletes" in report.cleanup_note
    assert FAKE_TOKEN not in json.dumps(dataclasses.asdict(report), ensure_ascii=False)


def test_full_run_step_evidence(fake_runner, fake_things, clock, token):
    report, _ = go(fake_runner, clock)
    s = by_name(report)
    assert report.ok, failures(report)
    assert "Things URLs are enabled" in s["ST-1"].detail
    assert "auth token works" in s["ST-4"].detail
    assert s["Q8"].detail.startswith("yes") and "@9:30am" in s["Q8"].detail
    assert s["Q22"].detail.startswith("yes") and s["Q23"].detail.startswith("yes")
    assert s["Q14"].detail.startswith("no:")               # the fake's reading: dormant to-dos show nowhere
    assert s["Q24"].detail.startswith("completed and canceled to-dos reach the Logbook")
    # ST-8: the title went to FIND_CREATED as argv with its leading '-' and was matched exactly
    found = [c.args for c in fake_runner.calls("FIND_CREATED") if c.args[1].startswith("-tb-")]
    assert found and found[0][1].endswith(selftest.UNICODE_TAIL)
    # ST-9: the run reads the user's lists but only reports on its own ids
    assert "alpha is not in Today" in s["ST-9"].detail
    assert set(s["ST-9"].ids) <= set(report.created_ids)


def test_find_created_is_bounded_by_the_run_start(fake_runner, clock, token):
    t0 = clock.wall()
    go(fake_runner, clock)
    elapsed = clock.wall() - t0
    ages = [int(c.args[2]) for c in fake_runner.calls("FIND_CREATED")]
    assert ages and max(ages) <= int(elapsed) + selftest.AGE_SLACK_S + 5


def test_progress_that_raises_does_not_stop_the_run(fake_runner, clock, token):
    def broken(step):
        raise BrokenPipeError("stdout closed")

    report = run_selftest(fake_runner, Config(), yes=True, progress=broken, today=clock.today)
    assert report.ok, failures(report)


def test_no_verify_and_a_tiny_batch_limit_do_not_apply(fake_runner, fake_things, clock, token):
    report, _ = go(fake_runner, clock, config=Config(verify=False, max_batch=1, confirm_threshold=0))
    assert report.ok, failures(report)
    assert "--no-verify doesn't apply" in by_name(report)["ST-0"].detail
    assert all(fake_things.get(i)["status"] != "open" for i in report.created_ids)


def test_launch_is_recorded(fake_runner, fake_things, clock, token):
    fake_things.running = False
    report, _ = go(fake_runner, clock)
    s = by_name(report)
    assert report.ok, failures(report)
    assert fake_runner.launches == 1 and "launched hidden by this run" in s["ST-0"].detail
    assert s["Q21"].detail.startswith("Things was launched hidden by this run")


# ---- --probe-conflicts ------------------------------------------------------------------------------------------

def test_probe_conflicts_answers_q9_q11_q12(fake_runner, fake_things, clock, lib, token):
    t, r = fake_things, fake_runner
    report, _ = go(r, clock, probe_conflicts=True)
    s = by_name(report)
    assert report.ok, failures(report)
    assert s["ST-12"].status == "info" and s["ST-13"].status == "pass"
    assert s["Q9"].detail.startswith("yes")
    assert s["Q11"].detail == "json add-tags as an array: applied"
    assert "ignored while a to-do is open" in s["Q12"].detail
    # the plain update?when=anytime (built by hand; the builders refuse it) still went through the gate
    plain = [c.turl for c in r.dispatch_calls
             if c.turl.command == "update" and dict(c.turl.params).get("when") == "anytime"]
    assert len(plain) == 1 and targets(plain[0]) <= set(report.created_ids)
    # one existing tag, applied to the selftest's own item only
    tagged = [i for i in report.created_ids if t.get(i)["tags"]]
    assert len(tagged) == 1 and t.get(tagged[0])["tags"] == ["Home"] and t.get(tagged[0])["title"].startswith("-tb-")
    assert_untouched(t, lib)
    for call in r.dispatch_calls:
        assert targets(call.turl) <= set(report.created_ids)
    assert all(t.get(i)["status"] != "open" for i in report.created_ids)


def test_probe_conflicts_cascade_reading(fake_runner, fake_things, clock, token):
    fake_things.project_complete_mode = "cascade"
    fake_things.seed_tag("Home")
    report, _ = go(fake_runner, clock, probe_conflicts=True)
    s = by_name(report)
    assert report.ok, failures(report)
    assert "cascade" in s["Q12"].detail and s["ST-13"].status == "pass"


def test_probe_conflicts_without_tags(fake_runner, clock, token):
    report, _ = go(fake_runner, clock, probe_conflicts=True)
    s = by_name(report)
    assert report.ok, failures(report)
    assert s["Q11"].detail == "not answered: no usable tag exists in Things"
    assert not any("add-tags" in names_used(c.turl) for c in fake_runner.dispatch_calls)


# ---- token, environment ----------------------------------------------------------------------------------------

def test_without_a_token_the_token_steps_are_skipped(fake_things, clock, lib):
    r = FakeRunner(fake_things, clock=clock, token=None)
    report, _ = go(r, clock)
    s = by_name(report)
    assert report.ok, failures(report)
    assert "no usable token" in s["ST-0"].detail
    for n in TOKEN_STEPS:
        assert (s[f"ST-{n}"].status, s[f"ST-{n}"].detail) == ("skip", NO_TOKEN)
    assert s["ST-13"].status == "skip" and s["ST-13"].detail.startswith(NO_TOKEN)
    assert all(s[f"ST-{n}"].status == "pass" for n in (1, 2, 3, 8, 9))
    # only creates went out; nothing needed (or got) the token
    assert {c.turl.command for c in r.dispatch_calls} == {"add-project", "add", "json"}
    assert not any(c.turl.requires_token for c in r.dispatch_calls)
    # what stays open is listed for the user
    still_open = [i for i in report.created_ids if fake_things.get(i)["status"] == "open"]
    assert len(still_open) == 6                              # all but g2, created completed
    assert set(s["ST-13"].ids) == set(still_open)
    assert "(still open)" in report.cleanup_note
    assert all(i in report.cleanup_note for i in still_open)
    assert_untouched(fake_things, lib)


def test_an_invalid_token_stops_before_anything_is_created(monkeypatch, fake_runner, clock):
    monkeypatch.setenv("THINGS_AUTH_TOKEN", "not a token!")
    report, _ = go(fake_runner, clock)
    s = by_name(report)
    assert not report.ok and s["ST-0"].status == "fail" and s["ST-0"].detail.startswith("token: ")
    assert fake_runner.dispatch_calls == [] and report.created_ids == [] and report.project_id is None


@pytest.mark.parametrize("setup, expect", [
    (lambda r, t: setattr(r, "macos", False), "macOS"),
    (lambda r, t: r.inject("AUTOMATION", osa_code=-1743), "Automation: denied"),
    (lambda r, t: setattr(t, "installed", False), "installed"),
], ids=["not-macos", "automation-denied", "not-installed"])
def test_environment_failures_stop_at_st0(fake_runner, fake_things, clock, token, setup, expect):
    setup(fake_runner, fake_things)
    report, seen = go(fake_runner, clock)
    s = by_name(report)
    assert not report.ok and s["ST-0"].status == "fail" and expect in s["ST-0"].detail
    for n in range(1, 13):
        assert (s[f"ST-{n}"].status, s[f"ST-{n}"].detail) == ("skip", AFTER_FAILURE)
    assert s["ST-13"].status == "skip" and s["ST-14"].status == "info"
    assert fake_runner.dispatch_calls == []
    assert (report.created_ids, report.project_id) == ([], None)
    assert report.cleanup_note == "Nothing was created, and nothing was deleted."
    assert seen == report.steps


def test_things_not_running_and_no_launch(fake_runner, fake_things, clock, token):
    fake_things.running = False
    report, _ = go(fake_runner, clock, config=Config(allow_launch=False))
    s = by_name(report)
    assert not report.ok and "[things_not_running]" in s["ST-0"].detail
    assert fake_runner.launches == 0 and fake_runner.dispatch_calls == []


# ---- consent ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("yes", [False, None, 1, "yes"])
def test_no_consent_no_run(fake_runner, clock, yes):
    with pytest.raises(Refused, match="consent"):
        run_selftest(fake_runner, Config(), yes=yes, today=clock.today)
    assert fake_runner.template_calls == [] and fake_runner.dispatch_calls == []


def test_read_only_mode_refuses(fake_runner, clock, token):
    with pytest.raises(Refused, match="read-only"):
        run_selftest(fake_runner, Config(read_only=True), yes=True, today=clock.today)
    assert fake_runner.template_calls == [] and fake_runner.dispatch_calls == []


class _Stdin(io.StringIO):
    def __init__(self, text: str = "", *, tty: bool = False):
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def cli_run(monkeypatch, capsys, r, *argv, stdin="", tty=False, today=None):
    monkeypatch.setattr(sys, "stdin", _Stdin(stdin, tty=tty))
    code = cli.main(list(argv), runner=r, today=today)
    out, err = capsys.readouterr()
    return code, out, err


def test_cli_without_yes_and_without_a_tty_refuses(monkeypatch, capsys, fake_runner, token):
    assert cli._load_selftest() is selftest                      # the real module, not a stand-in
    code, out, err = cli_run(monkeypatch, capsys, fake_runner, "selftest")
    assert code == 4 and "consent" in err
    code, out, err = cli_run(monkeypatch, capsys, fake_runner, "--json", "selftest")
    assert code == 4 and json.loads(out)["error"]["code"] == "refused"
    assert fake_runner.template_calls == [] and fake_runner.dispatch_calls == []


def test_cli_prompt_declined_does_nothing(monkeypatch, capsys, fake_runner, token):
    code, _out, err = cli_run(monkeypatch, capsys, fake_runner, "selftest", stdin="n\n", tty=True)
    assert code == 0 and "Cancelled; nothing was created." in err and "Nothing is deleted" in err
    assert fake_runner.template_calls == [] and fake_runner.dispatch_calls == []


def test_cli_runs_the_real_selftest(monkeypatch, capsys, token):
    clock = FakeClock(today=date.today())                        # the CLI gives the selftest the real today
    t = FakeThings(clock)
    r = FakeRunner(t, clock=clock)
    code, out, err = cli_run(monkeypatch, capsys, r, "--json", "selftest", "--yes")
    assert code == 0, err
    report = json.loads(out)
    assert report["ok"] is True and len(report["created_ids"]) == 7
    assert "ST-0" in err and "ST-13" in err                     # progress goes to stderr under --json
    assert all(t.get(i)["status"] != "open" for i in report["created_ids"])
    code, out, err = cli_run(monkeypatch, capsys, r, "selftest", stdin="y\n", tty=True)
    assert code == 0 and "Selftest passed." in out and "Nothing was deleted." in out


# ---- OwnedRunner: direct URLs -------------------------------------------------------------------------------

@pytest.fixture
def gate(fake_runner, fake_things, lib):
    """An OwnedRunner whose ``created`` holds one project and one to-do of 'this run'; ``lib`` is the user's."""
    t = fake_things
    own_p = t.seed_project("tb-12345678 mine")
    own_t = t.seed_todo("tb-12345678 todo", project=own_p)
    created = {own_p, own_t}
    lib.before = snapshot(t)
    return SimpleNamespace(runner=OwnedRunner(fake_runner, created), inner=fake_runner, created=created,
                           own_p=own_p, own_t=own_t, lib=lib, t=t)


def hand_json(objs) -> u.ThingsURL:
    data = objs if isinstance(objs, str) else json.dumps(objs)
    return u.ThingsURL("json", (("data", data),), 1, False)


REFUSED_URLS = {
    "update a user to-do": lambda g: u.build_update(id=g.lib.milk, title="x"),
    "complete a user project": lambda g: u.build_update_project(id=g.lib.taxes, completed=True),
    "add into a user project": lambda g: u.build_add(title="x", list_id=g.lib.taxes),
    "add into a user area": lambda g: u.build_add(title="x", list_id=g.lib.family),
    "add under a user heading": lambda g: u.build_add(title="x", list_id=g.own_p, heading_id=g.lib.receipts_heading),
    "add by list title": lambda g: u.build_add(title="x", list="Taxes"),
    "add-project into a user area": lambda g: u.build_add_project(title="x", area_id=g.lib.work),
    "add-project by area title": lambda g: u.build_add_project(title="x", area="Work"),
    "move an own to-do into a user project": lambda g: u.build_update(id=g.own_t, list_id=g.lib.taxes),
    "move an own project into a user area": lambda g: u.build_update_project(id=g.own_p, area_id=g.lib.work),
    "duplicate an own to-do": lambda g: u.build_update(id=g.own_t, duplicate=True),
    "json update of a user to-do": lambda g: u.build_json([jsoncmd.Todo(operation="update", id=g.lib.milk,
                                                                       completed=True)]),
    "json update of a user project": lambda g: u.build_json([jsoncmd.Project(operation="update", id=g.lib.taxes,
                                                                            canceled=True)]),
    "json batch with one user item": lambda g: u.build_json([
        jsoncmd.Todo(operation="update", id=g.own_t, completed=True),
        jsoncmd.Todo(operation="update", id=g.lib.milk, completed=True)]),
    "json move of an own to-do into a user project": lambda g: u.build_json([
        jsoncmd.Todo(operation="update", id=g.own_t, list_id=g.lib.taxes)]),
    "json create in a user project": lambda g: u.build_json([jsoncmd.Todo(title="x", list_id=g.lib.taxes)]),
    "json create by list title": lambda g: u.build_json([jsoncmd.Todo(title="x", list="Taxes")]),
    "json create under a user heading": lambda g: u.build_json([
        jsoncmd.Todo(title="x", list_id=g.own_p, heading_id=g.lib.receipts_heading)]),
    "json project in a user area": lambda g: u.build_json([jsoncmd.Project(title="x", area_id=g.lib.work)]),
    "json project by area title": lambda g: u.build_json([jsoncmd.Project(title="x", area="Work")]),
    "hand-built nested update": lambda g: hand_json([{"type": "project", "attributes": {"title": "x", "items": [
        {"type": "to-do", "operation": "update", "id": g.own_t, "attributes": {"title": "y"}}]}}]),
    "hand-built nested id": lambda g: hand_json([{"type": "project", "attributes": {"title": "x", "items": [
        {"type": "to-do", "id": g.lib.milk, "attributes": {"title": "y"}}]}}]),
    "hand-built nested list-id": lambda g: hand_json([{"type": "project", "attributes": {"title": "x", "items": [
        {"type": "to-do", "attributes": {"title": "y", "list-id": g.lib.taxes}}]}}]),
    "hand-built create carrying an id": lambda g: hand_json([{"type": "to-do", "id": g.lib.milk,
                                                              "attributes": {"title": "x"}}]),
    "hand-built unknown type": lambda g: hand_json([{"type": "area", "attributes": {"title": "x"}}]),
    "hand-built unknown operation": lambda g: hand_json([{"type": "to-do", "operation": "delete", "id": g.own_t,
                                                          "attributes": {}}]),
    "hand-built creation date": lambda g: hand_json([{"type": "to-do", "attributes": {
        "title": "x", "creation-date": "2020-01-01T00:00:00Z"}}]),
    "hand-built data that isn't an array": lambda g: hand_json('{"type": "to-do", "attributes": {}}'),
    "hand-built data that isn't JSON": lambda g: hand_json("[{"),
    "hand-built update without an id": lambda g: u.ThingsURL("update", (("title", "x"),), 1, True),
    "show": lambda g: u.build_show(id=g.own_t),
    "search": lambda g: u.build_search(query="x"),
    "version": lambda g: u.build_version(),
}

ALLOWED_URLS = {
    "add to the Inbox": lambda g: u.build_add(title="tb-12345678 new"),
    "add-project with to-dos": lambda g: u.build_add_project(title="tb-12345678 p", to_dos=["a", "b"]),
    "add into an own project": lambda g: u.build_add(title="tb-12345678 new", list_id=g.own_p, heading="H"),
    "update an own to-do": lambda g: u.build_update(id=g.own_t, title="tb-12345678 renamed", list_id=g.own_p),
    "update an own project": lambda g: u.build_update_project(id=g.own_p, notes="n"),
    "json create in an own project": lambda g: u.build_json([jsoncmd.Todo(
        title="tb-12345678 c", list_id=g.own_p, checklist_items=[jsoncmd.ChecklistItem("c1")])]),
    "json update of an own to-do": lambda g: u.build_json([jsoncmd.Todo(operation="update", id=g.own_t,
                                                                        completed=True)]),
    "json new project with items": lambda g: u.build_json([jsoncmd.Project(title="tb-12345678 q", items=[
        jsoncmd.Todo(title="a"), jsoncmd.Heading("h", archived=True), jsoncmd.Todo(title="b", completed=True)])]),
    "json add-tags on an own to-do": lambda g: u.build_json([jsoncmd.Todo(operation="update", id=g.own_t,
                                                                         add_tags=["Home"])], strict=False),
}


@pytest.mark.parametrize("dry_run", [False, True], ids=["send", "dry-run"])
@pytest.mark.parametrize("case", sorted(REFUSED_URLS))
def test_owned_runner_refuses_before_dispatch(gate, token, case, dry_run):
    turl = REFUSED_URLS[case](gate)
    with pytest.raises(Refused, match="Selftest safety stop") as e:
        gate.runner.dispatch(turl, dry_run=dry_run)
    assert e.value.hint == selftest.REFUSED_HINT and "Nothing was sent" in e.value.message
    assert gate.inner.dispatch_calls == [] and gate.t.log == []
    assert_untouched(gate.t, gate.lib)


@pytest.mark.parametrize("case", sorted(ALLOWED_URLS))
def test_owned_runner_passes_creates_and_own_targets(gate, token, case):
    turl = ALLOWED_URLS[case](gate)
    d = gate.runner.dispatch(turl)
    assert d.sent and len(gate.inner.dispatch_calls) == 1 and gate.inner.dispatch_calls[0].turl is turl
    assert gate.t.errors == []
    for id_, old in gate.lib.before["items"].items():             # the user's items are as they were
        if id_ not in gate.created:
            assert gate.t.items[id_] == old


def test_owned_runner_reads_the_live_created_set(gate, token):
    other = gate.t.seed_todo("tb-12345678 later")
    turl = u.build_update(id=other, title="tb-12345678 later!")
    with pytest.raises(Refused):
        gate.runner.dispatch(turl)
    gate.created.add(other)                                      # claimed by the run
    assert gate.runner.dispatch(turl).sent
    assert gate.t.get(other)["title"] == "tb-12345678 later!"


def test_ownership_problem_names_the_offender(gate):
    problem = selftest.ownership_problem(u.build_update(id=gate.lib.milk, title="x"), gate.created)
    assert gate.lib.milk in problem and "wasn't created by this run" in problem
    assert selftest.ownership_problem(u.build_add(title="x"), gate.created) is None


def test_owned_runner_delegates_everything_else(gate, clock):
    g, inner = gate, gate.inner
    assert isinstance(g.runner, Runner)
    assert g.runner.run_template("AREAS") == inner.run_template("AREAS")
    assert [c.name for c in inner.calls()] == ["AREAS", "AREAS"]
    with g.runner.write_lock():
        assert inner._lock_depth == 1
    assert inner.write_locks == 1
    assert g.runner.wall() == clock.wall() and g.runner.monotonic() == clock.monotonic()
    t0 = clock.monotonic()
    g.runner.sleep(2.5)
    assert clock.monotonic() == t0 + 2.5
    assert g.runner.things_running() is True and g.runner.ensure_running(allow_launch=False) is False
    g.runner.require_macos()
    inner.macos = False
    with pytest.raises(NotMacOS):
        g.runner.require_macos()


# ---- OwnedRunner under ThingsService: service-issued writes ---------------------------------------------------

SERVICE_CALLS = {
    "update_todo on a user to-do": lambda s, g, **kw: s.update_todo(g.lib.milk, TodoPatch(title="x"), **kw),
    "update_todo moving an own to-do into a user project": lambda s, g, **kw: s.update_todo(
        g.own_t, TodoPatch(project=g.lib.taxes), **kw),
    "update_todo moving an own to-do into a user area": lambda s, g, **kw: s.update_todo(
        g.own_t, TodoPatch(area="Family"), **kw),
    "update_project on a user project": lambda s, g, **kw: s.update_project(g.lib.taxes, ProjectPatch(notes="x"),
                                                                            **kw),
    "set_status on a user to-do": lambda s, g, **kw: s.set_status([g.lib.milk], "completed", **kw),
    "set_status on own + user": lambda s, g, **kw: s.set_status([g.own_t, g.lib.milk], "canceled", **kw),
    "schedule a user to-do": lambda s, g, **kw: s.schedule([g.lib.milk], when="tomorrow", **kw),
    "clear a user deadline": lambda s, g, **kw: s.schedule([g.lib.milk], clear_deadline=True, **kw),
    "move an own to-do into a user project": lambda s, g, **kw: s.move([g.own_t], project=g.lib.taxes, **kw),
    "move a user to-do into an own project": lambda s, g, **kw: s.move([g.lib.milk], project=g.own_p, **kw),
    "move an own project into a user area": lambda s, g, **kw: s.move([g.own_p], area=g.lib.work, **kw),
    "tag a user to-do": lambda s, g, **kw: s.tag([g.lib.milk], add=["Home"], **kw),
    "add_todo into a user project by id": lambda s, g, **kw: s.add_todo(TodoSpec(title="x", project=g.lib.taxes),
                                                                       **kw),
    "add_todo into a user project by title": lambda s, g, **kw: s.add_todo(TodoSpec(title="x", project="Taxes"),
                                                                          **kw),
    "add_todo into a user area": lambda s, g, **kw: s.add_todo(TodoSpec(title="x", area="Family"), **kw),
    "add_todos with one in a user project": lambda s, g, **kw: s.add_todos(
        [TodoSpec(title="x"), TodoSpec(title="y", project=g.lib.taxes)], **kw),
    "add_project into a user area": lambda s, g, **kw: s.add_project(ProjectSpec(title="x", area=g.lib.work), **kw),
    "raw_json update of a user to-do": lambda s, g, **kw: s.raw_json(
        [{"type": "to-do", "operation": "update", "id": g.lib.milk, "attributes": {"title": "x"}}], **kw),
    "raw_json create in a user project": lambda s, g, **kw: s.raw_json(
        [{"type": "to-do", "attributes": {"title": "x", "list-id": g.lib.taxes}}], **kw),
    "raw_json project in a user area": lambda s, g, **kw: s.raw_json(
        [{"type": "project", "attributes": {"title": "x", "area-id": g.lib.work}}], **kw),
}


@pytest.fixture
def owned_service(gate, clock):
    return ThingsService(gate.runner, Config(), today=clock.today)


@pytest.mark.parametrize("dry_run", [False, True], ids=["send", "dry-run"])
@pytest.mark.parametrize("case", sorted(SERVICE_CALLS))
def test_service_writes_on_foreign_ids_are_refused_before_dispatch(gate, owned_service, token, case, dry_run):
    with pytest.raises(Refused, match="Selftest safety stop"):
        SERVICE_CALLS[case](owned_service, gate, dry_run=dry_run)
    assert gate.inner.dispatch_calls == [] and gate.t.log == []
    assert_untouched(gate.t, gate.lib)


def test_service_writes_on_own_items_go_through(gate, owned_service, token):
    s, g = owned_service, gate
    res = s.update_todo(g.own_t, TodoPatch(title="tb-12345678 renamed", when="tomorrow"))
    assert res.sent and res.verified is True
    res = s.add_todo(TodoSpec(title="tb-12345678 new", project=g.own_p))
    assert res.sent and res.verified is True
    res = s.set_status([g.own_t], "completed")
    assert res.sent and res.verified is True and g.t.get(g.own_t)["status"] == "completed"
    for id_, old in g.lib.before["items"].items():
        if id_ not in g.created:
            assert g.t.items[id_] == old


# ---- invariants during a run ---------------------------------------------------------------------------------

def test_a_service_write_retargeted_at_a_user_item_is_refused_mid_run(monkeypatch, fake_runner, fake_things, clock,
                                                                      lib, token):
    original = ThingsService.update_todo

    def retarget(self, id, patch, **kw):            # a bug sends the selftest's change to the user's to-do
        return original(self, lib.milk, patch, **kw)

    monkeypatch.setattr(ThingsService, "update_todo", retarget)
    report, _ = go(fake_runner, clock)
    s = by_name(report)
    assert not report.ok
    assert s["ST-4"].status == "fail" and "[refused] Selftest safety stop" in s["ST-4"].detail
    assert lib.milk in s["ST-4"].detail
    for n in range(5, 13):
        assert (s[f"ST-{n}"].status, s[f"ST-{n}"].detail) == ("skip", AFTER_FAILURE)
    assert s["ST-13"].status == "pass"
    assert_untouched(fake_things, lib)
    assert all(lib.milk not in targets(c.turl) for c in fake_runner.dispatch_calls)
    assert len(report.created_ids) == 6 and all(fake_things.get(i)["status"] != "open" for i in report.created_ids)


def test_a_json_payload_with_a_user_item_is_refused_mid_run(monkeypatch, fake_runner, fake_things, clock, lib,
                                                            token):
    original = ThingsService.raw_json
    calls = []

    def inject(self, data, **kw):                   # the first json (ST-3) also carries an update of a user item
        calls.append(data)
        if len(calls) == 1:
            data = [*data, {"type": "to-do", "operation": "update", "id": lib.milk, "attributes": {"completed": True}}]
        return original(self, data, **kw)

    monkeypatch.setattr(ThingsService, "raw_json", inject)
    report, _ = go(fake_runner, clock)
    s = by_name(report)
    assert not report.ok and s["ST-3"].status == "fail" and "Selftest safety stop" in s["ST-3"].detail
    assert_untouched(fake_things, lib)
    assert not any(t["title"].endswith(" beta") for t in fake_things.items.values())    # ST-3 sent nothing
    assert len(report.created_ids) == 2 and s["ST-13"].status == "pass"
    assert all(fake_things.get(i)["status"] != "open" for i in report.created_ids)


def test_a_same_title_project_made_during_the_run_is_never_adopted(monkeypatch, fake_runner, fake_things, clock,
                                                                   token):
    t = fake_things
    foreign: dict = {}
    original = fake_runner.dispatch

    def dispatch(turl, *, dry_run=False):
        if turl.command == "add-project" and not foreign:
            foreign["id"] = t.seed_project(SELFTEST_PROJECT_TITLE, notes="someone else's project")
            clock.advance(1.0)
        return original(turl, dry_run=dry_run)

    monkeypatch.setattr(fake_runner, "dispatch", dispatch)
    report, _ = go(fake_runner, clock)
    assert report.ok, failures(report)
    fid = foreign["id"]
    assert fid not in report.created_ids and report.project_id != fid
    assert t.get(fid)["status"] == "open" and t.get(fid)["notes"] == "someone else's project"
    assert all(fid not in targets(c.turl) for c in fake_runner.dispatch_calls)


# ---- close-out after failures ----------------------------------------------------------------------------------

def test_close_out_runs_after_a_failed_step(monkeypatch, fake_runner, fake_things, clock, lib, token):
    dropped = drop_first(monkeypatch, fake_runner,
                         lambda turl: turl.command == "update" and dict(turl.params).get("when") == "evening")
    report, _ = go(fake_runner, clock)
    s = by_name(report)
    assert dropped and not report.ok
    assert s["ST-5"].status == "fail" and "verified" in s["ST-5"].detail
    for n in range(6, 13):
        assert (s[f"ST-{n}"].status, s[f"ST-{n}"].detail) == ("skip", AFTER_FAILURE)
    assert s["ST-13"].status == "pass" and s["ST-14"].status == "info"
    assert all(fake_things.get(i)["status"] != "open" for i in report.created_ids)
    # after the failure, the only writes are close-out status changes on the run's own items
    calls = fake_runner.dispatch_calls
    later = calls[[c.turl for c in calls].index(dropped[0]) + 1:]
    assert later
    for c in later:
        assert c.turl.command == "json" and targets(c.turl) <= set(report.created_ids)
        for obj in payload(c.turl):
            assert obj["operation"] == "update" and set(obj["attributes"]) <= {"completed", "canceled"}
    assert_untouched(fake_things, lib)


def test_close_out_puts_a_future_scheduled_project_back_first(monkeypatch, fake_runner, fake_things, clock, token):
    def restore(turl):
        if turl.command != "json":
            return False
        obj = payload(turl)[0]
        return obj["type"] == "project" and obj["attributes"].get("when") == "anytime"

    dropped = drop_first(monkeypatch, fake_runner, restore)       # ST-10's restore never arrives
    report, _ = go(fake_runner, clock)
    s = by_name(report)
    assert dropped and s["ST-10"].status == "fail" and s["ST-13"].status == "pass", failures(report)
    assert all(fake_things.get(i)["status"] != "open" for i in report.created_ids)
    first_close = fake_runner.dispatch_calls[[c.turl for c in fake_runner.dispatch_calls].index(dropped[0]) + 1]
    assert restore(first_close.turl) and targets(first_close.turl) == {report.project_id}


def test_close_out_runs_after_an_unexpected_exception(monkeypatch, fake_runner, fake_things, clock, token):
    def boom(self, *a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(ThingsService, "search", boom)
    report, _ = go(fake_runner, clock)
    s = by_name(report)
    assert s["ST-8"].status == "fail" and s["ST-8"].detail == "internal error: RuntimeError: boom"
    assert s["ST-13"].status == "pass" and len(report.created_ids) == 7
    assert all(fake_things.get(i)["status"] != "open" for i in report.created_ids)


def test_items_that_were_never_identified_are_left_alone(fake_runner, fake_things, clock, lib, token):
    t = fake_things
    fake_runner.inject("FIND_CREATED", timeout=True, times=100, match=lambda args: args[1].endswith(" beta"))
    report, _ = go(fake_runner, clock)
    s = by_name(report)
    assert not report.ok and s["ST-3"].status == "fail" and "[automation_timeout]" in s["ST-3"].detail
    beta = next(it for it in t.items.values() if it["title"].endswith(" beta"))
    gamma = next(it for it in t.items.values() if it["title"].endswith(" gamma-proj"))
    assert beta["id"] not in report.created_ids and gamma["id"] not in report.created_ids
    assert beta["status"] == "open" and gamma["status"] == "open"               # never touched
    assert all(not targets(c.turl) & {beta["id"], gamma["id"]} for c in fake_runner.dispatch_calls)
    # alpha was closed; the project keeps an open to-do it can't claim, so the guard keeps it open
    assert s["ST-13"].status == "fail" and report.project_id in s["ST-13"].detail
    assert t.get(report.project_id)["status"] == "open"
    alpha = [i for i in report.created_ids if i != report.project_id]
    assert len(alpha) == 1 and t.get(alpha[0])["status"] == "completed"
    tok = run_token(t, report)
    assert "(still open)" in report.cleanup_note and "never identified" in report.cleanup_note
    assert repr(f"{tok} beta") in report.cleanup_note and f"Search Things for '{tok}'" in report.cleanup_note
    assert_untouched(t, lib)


def test_a_project_whose_heading_stays_unarchived_is_reported_not_failed(monkeypatch, fake_runner, fake_things,
                                                                         clock, token):
    t = fake_things
    original = fake_runner.dispatch

    def dispatch(turl, *, dry_run=False):
        d = original(turl, dry_run=dry_run)
        if turl.command == "json" and any(o.get("type") == "heading" for o in objects(payload(turl))):
            for h in t.headings.values():                 # Things left H1 unarchived (§6.2 ST-3)
                h["archived"] = False
        return d

    monkeypatch.setattr(fake_runner, "dispatch", dispatch)
    report, _ = go(fake_runner, clock)
    s = by_name(report)
    gid = next(i for i in report.created_ids if t.get(i)["title"].endswith(" gamma-proj"))
    assert report.ok, failures(report)
    assert s["ST-13"].status == "info" and s["ST-13"].ids == [gid] and "complete or delete it by hand" in \
        s["ST-13"].detail.lower()
    assert t.get(gid)["status"] == "open" and t.get(report.project_id)["status"] == "completed"
    assert f"Still open (complete or delete them by hand): {gid}." in report.cleanup_note


def test_selftest_module_stays_light():
    code = ("import sys, things_bridge.selftest; "
            "print(sorted(m for m in ('mcp', 'pydantic') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"
