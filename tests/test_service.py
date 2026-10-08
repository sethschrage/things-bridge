"""service.py (DESIGN §2.9, §3): reads, name resolution, tag canonicalisation, guards, URL routing, dry-run,
verification and the read-only switch — all against tests/fakes.py (FakeThings + FakeRunner)."""

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from fakes import FAKE_TOKEN, FakeRunner, FakeThings
from things_bridge import service as service_mod
from things_bridge import urlscheme as u
from things_bridge.errors import (Ambiguous, AutomationDenied, DispatchFailed, InvalidInput, NotFound, NotMacOS,
                                  ProtocolError, ReadFailed, Refused, ThingsNotRunning)
from things_bridge.models import ProjectItemSpec, ProjectPatch, ProjectSpec, TodoPatch, TodoSpec
from things_bridge.service import (HEADING_WARNING, NOT_OFFERED, OFFLINE_WARNING, REPEATING_REFUSAL,
                                   UNKNOWN_TAG_WARNING, Config, ThingsService, parse_since)
from things_bridge.verify import NOTHING_RESENT, dialog_reason, not_verified_reason

TODAY = date(2026, 9, 28)
GHOST = "GhostGhostGhostGhost01"


# ---- fixtures and helpers ----------------------------------------------------------------------------------

@pytest.fixture
def t(fake_things):
    return fake_things


@pytest.fixture
def r(fake_runner):
    return fake_runner


@pytest.fixture
def svc(r, clock):
    return ThingsService(r, Config(), today=clock.today)


@pytest.fixture
def w(t):
    """A small Things library."""
    ns = SimpleNamespace()
    ns.home = t.seed_tag("Home")
    ns.errand = t.seed_tag("Errand", parent="Home")
    ns.deep = t.seed_tag("Deep Work")
    ns.family = t.seed_area("Family")
    ns.work = t.seed_area("Work")
    ns.party = t.seed_project("Plan Birthday Party", area=ns.family, deadline="2026-10-20")
    ns.venue = t.seed_heading(ns.party, "Venue")
    ns.taxes = t.seed_project("Taxes", area=ns.work)
    ns.old = t.seed_project("Old Project", status="completed")
    ns.inbox = t.seed_todo("Inbox item")
    ns.milk = t.seed_todo("Buy milk", when="today", tags=["Errand"], deadline="2026-10-02", notes="2%",
                          project=ns.party)
    ns.book = t.seed_todo("Book venue", project=ns.party, heading=ns.venue)
    ns.evening = t.seed_todo("Collect dry cleaning", when="evening")
    ns.chore = t.seed_todo("Area chore", area=ns.work)
    ns.someday = t.seed_todo("Learn watercolor", when="someday")
    ns.later = t.seed_todo("Paint bedroom", when="2026-10-03")
    ns.done = t.seed_todo("Call Paolo", project=ns.party, status="completed")
    ns.tmpl = t.seed_todo("Water plants", repeating=True)       # a repeating template: in no list
    return ns


def _objects(objs: list) -> list[dict]:
    out = []
    for o in objs:
        out.append(o)
        out += _objects(o.get("attributes", {}).get("items", []))
    return out


@pytest.fixture(autouse=True)
def never_by_title(fake_runner):
    """N9 over every test here: no dispatched URL or json object names a list or area by title."""
    yield
    for turl in dispatched(fake_runner):
        assert not {"list", "area"} & set(params(turl)), turl.url
        if turl.command == "json":
            for obj in _objects(payload(turl)):
                assert not {"list", "area"} & set(obj["attributes"]), obj


def dispatched(r: FakeRunner) -> list[u.ThingsURL]:
    """Every URL handed to dispatch, dry runs included."""
    return [c.turl for c in r.dispatch_calls]


def params(turl: u.ThingsURL) -> dict[str, str]:
    return dict(turl.params)


def payload(turl: u.ThingsURL) -> list[dict]:
    assert turl.command == "json"
    return json.loads(params(turl)["data"])


def nothing_sent(r: FakeRunner, t: FakeThings) -> None:
    assert r.dispatch_calls == [] and t.log == []


def titles(page) -> list[str]:
    return [i.title for i in page.items]


# ---- Config ------------------------------------------------------------------------------------------------

def test_config_defaults():
    c = Config()
    assert (c.allow_launch, c.verify, c.max_batch, c.confirm_threshold, c.verify_timeout_s, c.read_only) == \
        (True, True, 50, 10, 6.0, False)
    assert Config.from_env({}) == c


def test_config_from_env():
    c = Config.from_env({"THINGS_BRIDGE_NO_LAUNCH": "1", "THINGS_BRIDGE_READ_ONLY": "true",
                         "THINGS_BRIDGE_MAX_BATCH": "20", "THINGS_BRIDGE_VERIFY_TIMEOUT": "9.5"})
    assert (c.allow_launch, c.read_only, c.max_batch, c.verify_timeout_s) == (False, True, 20, 9.5)
    assert Config.from_env({"THINGS_BRIDGE_NO_LAUNCH": "0", "THINGS_BRIDGE_READ_ONLY": ""}) == Config()


def test_config_from_os_environ(monkeypatch):
    monkeypatch.setenv("THINGS_BRIDGE_READ_ONLY", "1")
    assert Config.from_env().read_only is True


def test_config_max_batch_is_capped_at_100():
    assert Config.from_env({"THINGS_BRIDGE_MAX_BATCH": "500"}).max_batch == 100


@pytest.mark.parametrize("name, value", [
    ("THINGS_BRIDGE_NO_LAUNCH", "maybe"), ("THINGS_BRIDGE_READ_ONLY", "2"), ("THINGS_BRIDGE_MAX_BATCH", "0"),
    ("THINGS_BRIDGE_MAX_BATCH", "ten"), ("THINGS_BRIDGE_MAX_BATCH", "-3"), ("THINGS_BRIDGE_VERIFY_TIMEOUT", "0"),
    ("THINGS_BRIDGE_VERIFY_TIMEOUT", "soon"), ("THINGS_BRIDGE_VERIFY_TIMEOUT", "601"),
])
def test_config_from_env_rejects_bad_values(name, value):
    with pytest.raises(InvalidInput) as e:
        Config.from_env({name: value})
    assert e.value.details["field"] == name


@pytest.mark.parametrize("kwargs", [{"max_batch": 0}, {"max_batch": 101}, {"max_batch": True},
                                    {"confirm_threshold": -1}, {"verify_timeout_s": 0}])
def test_config_validates_programmatic_values(kwargs):
    with pytest.raises(InvalidInput):
        Config(**kwargs)


def test_service_defaults(r):
    s = ThingsService(r)
    assert s.config == Config() and s.today() == date.today()


# ---- reads -------------------------------------------------------------------------------------------------

def test_list_items_scans_the_list_then_reads_the_page(svc, r, w):
    page = svc.list_items("today")
    assert titles(page) == ["Buy milk", "Collect dry cleaning"]
    assert r.calls("SCAN")[0].args == ("list", "today", "0", "0")
    assert r.calls("ROWS")[0].args == ("1", w.milk, w.evening)
    assert (page.returned, page.total, page.offset, page.truncated, page.note) == (2, 2, 0, False, None)
    milk = page.items[0]
    assert milk.project.title == "Plan Birthday Party" and milk.area.title == "Family"
    assert milk.link == f"things:///show?id={w.milk}"
    assert "checklist items" in page.unreadable_fields
    assert r.dispatch_calls == []


def test_list_items_paging(svc, r, w):
    everything = svc.list_items("anytime", limit=200)
    assert everything.total >= 5
    page = svc.list_items("anytime", limit=2, offset=1)
    assert [i.id for i in page.items] == [i.id for i in everything.items[1:3]]
    assert (page.returned, page.total, page.offset, page.truncated) == (2, everything.total, 1, True)
    tail = svc.list_items("anytime", limit=2, offset=everything.total - 1)
    assert tail.returned == 1 and tail.truncated is False
    beyond = svc.list_items("anytime", offset=everything.total + 5)
    assert beyond.items == [] and beyond.total == everything.total


def test_list_items_status_filter(svc, t, w):
    t.log_immediately = False                   # completed but not yet logged: still in Today
    done = t.seed_todo("Done today", when="today", status="completed")
    assert done not in [i.id for i in svc.list_items("today").items]
    assert done in [i.id for i in svc.list_items("today", status="all").items]


def test_list_items_notes_modes(svc, r, t):
    long = "x" * 250
    t.seed_todo("Noted", notes=long)
    assert svc.list_items("inbox", notes="none").items[0].notes is None
    assert r.calls("ROWS")[-1].args[0] == "0"
    preview = svc.list_items("inbox").items[0]
    assert preview.notes == "x" * 200 and preview.notes_truncated is True
    full = svc.list_items("inbox", notes="full").items[0]
    assert full.notes == long and full.notes_truncated is False


@pytest.mark.parametrize("kwargs", [
    {"list_key": "logbook"}, {"list_key": "Today"}, {"limit": 0}, {"limit": 201}, {"limit": True},
    {"offset": -1}, {"notes": "all"}, {"status": "completed"},
])
def test_list_items_validates_arguments(svc, r, kwargs):
    args = {"list_key": "today", **kwargs}
    with pytest.raises(InvalidInput):
        svc.list_items(args.pop("list_key"), **args)
    assert r.template_calls == []


def test_reads_launch_things_hidden_when_allowed(svc, r, t, w):
    t.running = False
    svc.list_items("today")
    assert r.launches == 1


def test_reads_never_launch_under_no_launch(r, t, w, clock):
    t.running = False
    svc = ThingsService(r, Config(allow_launch=False), today=clock.today)
    with pytest.raises(ThingsNotRunning):
        svc.list_items("today")
    assert r.launches == 0 and r.template_calls == []


def test_reads_off_macos_raise_not_macos(t, clock):
    svc = ThingsService(FakeRunner(t, clock=clock, macos=False), today=clock.today)
    for call in (lambda: svc.list_items("today"), svc.areas, svc.tags, lambda: svc.search("x")):
        with pytest.raises(NotMacOS):
            call()


@pytest.mark.parametrize("exc", [ProtocolError("SCAN: truncated or malformed output"),
                                 ReadFailed("Things changed while reading; retry.", details={"osa_code": 9101}),
                                 ReadFailed("odd", details={"osa_code": -1700}),
                                 ReadFailed("Things didn't understand part of a read script (-1708): x",
                                            details={"osa_code": -1708})])
def test_scan_falls_back_to_the_slow_path(svc, r, w, exc):
    r.inject("SCAN", exc=exc)
    assert titles(svc.list_items("today")) == ["Buy milk", "Collect dry cleaning"]
    assert [c.args[3] for c in r.calls("SCAN")] == ["0", "1"]


@pytest.mark.parametrize("exc", [ReadFailed("Things returned an empty list", details={"osa_code": -1719}),
                                 ReadFailed("Things quit", details={"osa_code": -609}),
                                 ReadFailed("no code"), AutomationDenied("denied")])
def test_environment_errors_skip_the_slow_path(svc, r, w, exc):
    r.inject("SCAN", exc=exc)
    with pytest.raises(type(exc)):
        svc.list_items("today")
    assert len(r.calls("SCAN")) == 1


def test_items_that_vanish_between_scan_and_rows_are_noted(svc, r, t, w, monkeypatch):
    real = r.run_template

    def run_template(name, args=(), **kw):
        frame = real(name, args, **kw)
        if name == "SCAN":
            t.items.pop(w.evening)
        return frame
    monkeypatch.setattr(r, "run_template", run_template)
    page = svc.list_items("today")
    assert titles(page) == ["Buy milk"] and page.total == 2
    assert page.note == "1 item(s) changed or disappeared while reading and were left out."


def test_results_are_deduplicated_by_id(svc, t, w):
    t.app_todos_include_projects = True          # app-level `to dos` returns projects too (Q4)
    page = svc.search("birthday")
    assert [i.id for i in page.items] == [w.party]
    assert page.total == 1


def test_logbook(svc, r, t, w, clock):
    t.seed_todo("Dropped", status="canceled")
    page = svc.logbook()
    assert r.calls("LOGBOOK")[-1].args == ("50", "", "1")
    assert {i.title for i in page.items} == {"Call Paolo", "Dropped", "Old Project"}
    assert page.total == 3 and page.truncated is False
    small = svc.logbook(limit=1, notes="none")
    assert small.returned == 1 and small.truncated is True and small.items[0].notes is None
    assert r.calls("LOGBOOK")[-1].args == ("1", "", "0")
    svc.logbook(since="7d")
    assert r.calls("LOGBOOK")[-1].args == ("50", "2026-09-21", "1")
    since = svc.logbook(since=date(2026, 9, 1), limit=2)
    assert since.total is None and since.truncated is True
    with pytest.raises(InvalidInput):
        svc.logbook(since=12)
    with pytest.raises(InvalidInput):
        svc.logbook(since="yesterday")


def test_parse_since():
    assert parse_since("7d", today=TODAY) == date(2026, 9, 21)
    assert parse_since(" 2W ", today=TODAY) == date(2026, 9, 14)
    assert parse_since("2026-01-31", today=TODAY) == date(2026, 1, 31)
    for bad in ("", "7", "d7", "2026-02-30", "3m", "7 days"):
        with pytest.raises(InvalidInput):
            parse_since(bad, today=TODAY)


def test_get_todo(svc, r, w):
    res = svc.get(w.milk)
    assert res.kind == "to-do" and res.item.title == "Buy milk" and res.item.notes == "2%"
    assert res.area is None and res.children is None
    assert r.calls("ROWS")[-1].args == ("1", w.milk)
    assert svc.get(w.milk, children=True).children is None       # to-dos have no readable children
    assert svc.get(w.milk, notes="none").item.notes is None


def test_get_project_with_children(svc, r, w):
    res = svc.get(w.party, children=True)
    assert res.kind == "project" and res.item.deadline == "2026-10-20"
    assert set(titles(res.children)) == {"Buy milk", "Book venue", "Call Paolo"}    # every status
    assert ("project", w.party, "0", "0") in [c.args for c in r.calls("SCAN")]


def test_get_area_with_children(svc, t, w):
    res = svc.get(w.work, children=True)
    assert res.kind == "area" and res.area.title == "Work" and res.item is None
    assert titles(res.children) == ["Taxes", "Area chore"]            # its projects, then its loose to-dos
    assert svc.get(w.work).children is None


def test_get_errors(svc, r, w):
    with pytest.raises(NotFound) as e:
        svc.get(GHOST)
    assert GHOST in e.value.message
    with pytest.raises(InvalidInput):
        svc.get("not an id")
    with pytest.raises(InvalidInput):
        svc.get(w.milk + "\n")


def test_projects(svc, r, t, w):
    assert titles(svc.projects()) == ["Plan Birthday Party", "Taxes"]
    assert svc.projects().items[0].notes is None                 # notes default to none
    t.log_immediately = False
    t.seed_project("Finished", status="completed")
    assert "Finished" in titles(svc.projects(status="all")) and "Finished" not in titles(svc.projects())
    assert titles(svc.projects(area="work")) == ["Taxes"]
    assert titles(svc.projects(area=w.family)) == ["Plan Birthday Party"]
    assert r.calls("PROJECT_AREAS")


def test_areas_and_tags(svc, w):
    areas = svc.areas()
    assert [a.title for a in areas.items] == ["Family", "Work"] and areas.returned == 2
    tags = svc.tags()
    assert [g.title for g in tags.items] == ["Home", "Errand", "Deep Work"]
    assert tags.items[1].parent.title == "Home"


def test_search_is_case_and_diacritic_insensitive(svc, r, t):
    cafe = t.seed_todo("Café visit")
    noted = t.seed_todo("Errand", notes="pick up at the CAFE")
    t.seed_todo("Other")
    page = svc.search("cafe")
    assert [i.id for i in page.items] == [cafe, noted]            # title hits before notes hits
    assert [c.args for c in r.calls("SCAN")] == [("app", "", "1", "0"), ("projects", "", "1", "0")]
    assert [i.id for i in svc.search("CAFÉ").items] == [cafe, noted]


def test_search_open_only_unless_include_completed(svc, r, t, w):
    t.log_immediately = False
    open_one = t.seed_todo("Report draft")
    closed = t.seed_todo("Report final", status="completed")        # not logged yet: still in app to dos
    t.log_immediately = True
    logged = t.seed_todo("Report 2025", status="completed")
    assert [i.id for i in svc.search("report").items] == [open_one]
    page = svc.search("report", include_completed=True, logbook_window=10, notes="none")
    assert [i.id for i in page.items] == [open_one, closed, logged]
    assert r.calls("LOGBOOK")[-1].args == ("10", "", "1")
    assert page.items[2].notes is None


def test_search_limits(svc, t):
    for i in range(5):
        t.seed_todo(f"Match {i}")
    page = svc.search("match", limit=2)
    assert page.returned == 2 and page.total == 5 and page.truncated is True


@pytest.mark.parametrize("kwargs", [{"query": ""}, {"query": "   "}, {"query": "x" * 201}, {"query": "a\nb"},
                                    {"logbook_window": 5}, {"logbook_window": 501}, {"include_completed": "yes"}])
def test_search_validation(svc, r, kwargs):
    args = {"query": "ok", **kwargs}
    with pytest.raises(InvalidInput):
        svc.search(args.pop("query"), **args)
    assert r.template_calls == []


def test_find(svc, r, t, w):
    assert titles(svc.find(tags=["errand"])) == ["Buy milk"]
    assert r.calls("SCAN")[0].args[:2] == ("app", "") and r.calls("SCAN")[1].args[:2] == ("projects", "")
    assert set(titles(svc.find(project="plan birthday party"))) == {"Buy milk", "Book venue"}
    assert set(titles(svc.find(project=w.party, status="all"))) == {"Buy milk", "Book venue", "Call Paolo"}
    assert titles(svc.find(area="Work")) == ["Area chore"]
    assert titles(svc.find(project=w.party, tags=["ERRAND", "Deep Work"])) == ["Buy milk"]   # any-of
    t.seed_tag("Office")
    t.seed_project("Tagged project", tags=["Office"])
    assert titles(svc.find(tags=["office"])) == ["Tagged project"]
    with pytest.raises(InvalidInput):
        svc.find()
    with pytest.raises(InvalidInput):
        svc.find(project=w.party, area=w.work)
    with pytest.raises(InvalidInput):
        svc.find(tags="Home")                                         # a bare string, not a list


def test_deadlines(svc, t, w):
    overdue = t.seed_todo("Overdue", deadline="2026-09-20")
    t.seed_todo("Far", deadline="2027-06-01")
    t.seed_todo("Closed", deadline="2026-09-29", status="completed")
    page = svc.deadlines()                                          # 14 days: up to 2026-10-12
    assert titles(page) == ["Overdue", "Buy milk"] and page.items[0].id == overdue
    assert titles(svc.deadlines(within_days=30)) == ["Overdue", "Buy milk", "Plan Birthday Party"]   # by deadline
    assert titles(svc.deadlines(within_days=30, include_overdue=False)) == ["Buy milk", "Plan Birthday Party"]
    assert titles(svc.deadlines(within_days=30, include_projects=False)) == ["Overdue", "Buy milk"]
    assert titles(svc.deadlines(within_days=4)) == ["Overdue", "Buy milk"]
    assert titles(svc.deadlines(within_days=0)) == ["Overdue"]
    assert titles(svc.deadlines(within_days=None)) == ["Overdue", "Buy milk", "Plan Birthday Party", "Far"]
    assert svc.deadlines(limit=1).truncated is True
    with pytest.raises(InvalidInput):
        svc.deadlines(within_days=366)


# ---- name resolution (N9, D7) --------------------------------------------------------------------------------

def test_container_title_resolved_to_id(svc, r, w):
    res = svc.add_todo(TodoSpec(title="Order cake", project="plan birthday party"))
    turl = dispatched(r)[0]
    assert turl.command == "add"
    assert params(turl)["list-id"] == w.party and "list" not in params(turl)
    assert res.verified is True and res.checks[-1].field == "project_id"


def test_area_title_resolved_to_id(svc, r, t, w):
    res = svc.move([w.inbox], area="WORK")
    assert payload(dispatched(r)[0])[0]["attributes"] == {"list-id": w.work}
    assert t.get(w.inbox)["area"] == w.work and res.verified is True


def test_container_id_wins(svc, r, t, w):
    t.seed_project(w.taxes)                                 # another project whose *title* is Taxes' id
    svc.add_todo(TodoSpec(title="File return", project=w.taxes))
    assert params(dispatched(r)[0])["list-id"] == w.taxes
    assert r.calls("SCAN") == []                            # resolved by ROWS alone, no title matching


def test_id_shaped_title_falls_back_to_title_match(svc, r, t):
    planning = t.seed_project("Planning")                   # "Planning" also matches ID_RE
    svc.add_todo(TodoSpec(title="x", project="Planning"))
    assert params(dispatched(r)[0])["list-id"] == planning
    assert r.calls("ROWS")[0].args == ("0", "Planning")


def test_ambiguous_title_errors(svc, r, t, w):
    a = t.seed_project("Errands", area=w.family)
    b = t.seed_project("errands")
    with pytest.raises(Ambiguous) as e:
        svc.add_todo(TodoSpec(title="x", project="ERRANDS"))
    cands = e.value.details["candidates"]
    assert [c["id"] for c in cands] == [a, b]
    assert cands[0]["area"] == {"id": w.family, "title": "Family"} and cands[1]["area"] is None
    assert a in e.value.hint and "in Family" in e.value.hint
    nothing_sent(r, t)


def test_ambiguous_area(svc, r, t, w):
    t.seed_area("family")
    with pytest.raises(Ambiguous) as e:
        svc.move([w.inbox], area="Family")
    assert len(e.value.details["candidates"]) == 2
    nothing_sent(r, t)


def test_not_found_suggests_close_titles(svc, r, t, w):
    with pytest.raises(NotFound) as e:
        svc.add_todo(TodoSpec(title="x", project="Plan Birthday Prty"))
    assert e.value.details["suggestions"] == ["Plan Birthday Party"]
    assert "Plan Birthday Party" in e.value.hint
    with pytest.raises(NotFound) as e:
        svc.add_todo(TodoSpec(title="x", area="Nowhere at all"))
    assert e.value.details["suggestions"] == [] and "things_areas" in e.value.hint
    nothing_sent(r, t)


def test_suggestions_are_capped_at_five(svc, t):
    for i in range(8):
        t.seed_project(f"Trip {i}")
    with pytest.raises(NotFound) as e:
        svc.add_todo(TodoSpec(title="x", project="Trip"))
    assert len(e.value.details["suggestions"]) == 5


def test_only_open_projects_match_titles(svc, r, t, w):
    with pytest.raises(NotFound):
        svc.add_todo(TodoSpec(title="x", project="Old Project"))      # completed and logged
    t.log_immediately = False
    t.seed_project("Wrapped", status="completed")                     # completed, still listed
    with pytest.raises(NotFound):
        svc.add_todo(TodoSpec(title="x", project="Wrapped"))
    nothing_sent(r, t)


@pytest.mark.parametrize("kind, target", [("project", "family"), ("project", "milk"), ("area", "party")])
def test_wrong_container_kind(svc, r, t, w, kind, target):
    with pytest.raises(InvalidInput):
        svc.add_todo(TodoSpec(title="x", **{kind: getattr(w, target)}))
    nothing_sent(r, t)


def test_inbox_is_not_a_destination(svc, r, t, w):
    with pytest.raises(Refused) as e:
        svc.move([w.milk], project="Inbox")
    assert e.value.message == NOT_OFFERED["move_to_inbox"]
    with pytest.raises(InvalidInput) as e:
        svc.move([w.milk])
    assert e.value.hint == NOT_OFFERED["move_to_inbox"]
    nothing_sent(r, t)


def test_areas_and_projects_are_read_once_per_call(svc, r, t, w):
    svc.add_todos([TodoSpec(title=f"t{i}", project="Taxes", area=None, tags=["home"]) for i in range(3)])
    assert len([c for c in r.calls("SCAN") if c.args[0] == "projects"]) == 1
    assert len(r.calls("TAGS")) == 1


# ---- tags ------------------------------------------------------------------------------------------------------

def test_unknown_tags_are_dropped_with_a_warning(svc, r, t, w):
    res = svc.add_todo(TodoSpec(title="Tagged", tags=["home", "Nope", "HOME"]))
    assert params(dispatched(r)[0])["tags"] == "Home"               # stored casing, deduped
    assert res.warnings == [UNKNOWN_TAG_WARNING.format("Nope")]
    assert "Tag 'Nope' doesn't exist in Things, so it was not applied" in res.warnings[0]
    assert res.verified is True and t.one("Tagged")["tags"] == ["Home"]


def test_all_tags_unknown_sends_none(svc, r, w):
    svc.add_todo(TodoSpec(title="x", tags=["Nope"]))
    assert "tags" not in params(dispatched(r)[0])


def test_strict_tags_errors(svc, r, t, w):
    with pytest.raises(InvalidInput) as e:
        svc.add_todo(TodoSpec(title="x", tags=["Nope"]), strict_tags=True)
    assert e.value.message == "Tag 'Nope' doesn't exist. Please create it in Things; then I can apply it."
    with pytest.raises(InvalidInput):
        svc.update_todo(w.milk, TodoPatch(add_tags=["Nope"]), strict_tags=True)
    nothing_sent(r, t)


def test_tag_tool_errors_on_unknown_tags(svc, r, t, w):
    with pytest.raises(InvalidInput) as e:
        svc.tag([w.milk], add=["Home", "Nope"])
    assert e.value.details["tag"] == "Nope"
    nothing_sent(r, t)


def test_tags_with_commas_are_refused_before_any_read(svc, r, w):
    with pytest.raises(InvalidInput):
        svc.add_todo(TodoSpec(title="x", tags=["a,b"]))
    assert r.template_calls == []


def test_replacing_with_only_unknown_tags_never_clears(svc, r, t, w):
    res = svc.update_todo(w.milk, TodoPatch(tags=["Nope"]))
    assert res.sent is False and res.verified is None
    assert "the tags were left unchanged" in " ".join(res.warnings)
    assert r.dispatch_calls == [] and t.get(w.milk)["tags"] == ["Errand"]
    res = svc.update_todo(w.milk, TodoPatch(title="Buy oat milk", tags=["Nope"]))
    assert "tags" not in params(dispatched(r)[0]) and t.get(w.milk)["tags"] == ["Errand"]
    assert res.verified is True


def test_explicit_empty_tags_clear(svc, r, t, w):
    res = svc.update_todo(w.milk, TodoPatch(tags=[]))
    assert params(dispatched(r)[0])["tags"] == ""
    assert t.get(w.milk)["tags"] == [] and res.verified is True


# ---- snapshots (before) ---------------------------------------------------------------------------------------

def test_before_snapshots_are_returned(svc, t, w):
    notes = "n" * 300
    tid = t.seed_todo("Old title", notes=notes, tags=["Home"])
    res = svc.update_todo(tid, TodoPatch(title="New title", notes="short"))
    assert [b.title for b in res.before] == ["Old title"]
    assert res.before[0].notes == notes and res.before[0].tags == ["Home"]        # full notes, for undo
    assert res.items[0].title == "New title" and res.items[0].notes == "short"
    res = svc.set_status([w.milk, w.evening], "completed")
    assert [b.status for b in res.before] == ["open", "open"]
    assert [i.status for i in res.items] == ["completed", "completed"]


def test_missing_target_is_not_found(svc, r, t, w):
    with pytest.raises(NotFound) as e:
        svc.update_todo(GHOST, TodoPatch(title="x"))
    assert e.value.details["ids"] == [GHOST]
    with pytest.raises(NotFound):
        svc.set_status([w.milk, GHOST], "completed")
    nothing_sent(r, t)


def test_kind_mismatch(svc, r, t, w):
    with pytest.raises(InvalidInput) as e:
        svc.update_todo(w.party, TodoPatch(title="x"))
    assert "use update_project" in e.value.message
    with pytest.raises(InvalidInput) as e:
        svc.update_project(w.milk, ProjectPatch(title="x"))
    assert "use update_todo" in e.value.message
    with pytest.raises(InvalidInput):
        svc.set_status([w.family], "completed")                      # areas can't be changed at all
    with pytest.raises(TypeError):
        svc.update_todo(w.milk, ProjectPatch(title="x"))
    nothing_sent(r, t)


# ---- guards ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("which", ["inbox", "milk", "chore", "someday", "later", "evening"])
def test_repeating_guard_passes_visible_items(svc, r, w, which):
    res = svc.schedule([getattr(w, which)], deadline="2026-12-01")
    assert res.sent is True and res.verified is True
    assert r.calls("MEMBERSHIP")[0].args == ("inbox", "today", "anytime", "someday", "upcoming")


def test_guard_refuses_unplaceable_id(svc, r, t, w):
    for call in (lambda: svc.schedule([w.tmpl], when="today"),
                 lambda: svc.schedule([w.milk, w.tmpl], deadline="2026-12-01"),
                 lambda: svc.schedule([w.tmpl], clear_deadline=True),
                 lambda: svc.update_todo(w.tmpl, TodoPatch(when="tomorrow")),
                 lambda: svc.update_todo(w.tmpl, TodoPatch(deadline="2026-12-01")),
                 lambda: svc.update_todo(w.tmpl, TodoPatch(clear_deadline=True)),
                 lambda: svc.set_status([w.tmpl], "completed"),
                 lambda: svc.set_status([w.tmpl], "canceled")):
        with pytest.raises(Refused) as e:
            call()
        assert e.value.message == REPEATING_REFUSAL
        assert e.value.details["ids"] == [w.tmpl] and "Water plants" in e.value.hint
    nothing_sent(r, t)
    assert t.crashes == 0


def test_guard_ignores_fields_that_are_safe_on_templates(svc, r, t, w):
    res = svc.update_todo(w.tmpl, TodoPatch(title="Water all plants", append_notes=" (weekly)"))
    assert res.sent is True and r.calls("MEMBERSHIP") == []
    assert t.get(w.tmpl)["title"] == "Water all plants"


def test_guard_upcoming_only_needs_a_future_start_date(svc, r, t, w, monkeypatch):
    future = t.seed_todo("Future template", when="2026-10-05", repeating=True)
    current = t.seed_todo("Current template", when="today", repeating=True)
    real = t._in_list
    monkeypatch.setattr(t, "_in_list", lambda it, key: True if key == "upcoming" and it["repeating"] else real(it, key))
    assert svc.schedule([future], deadline="2026-12-01").sent is True          # Upcoming + future start date
    for tid in (w.tmpl, current):                                                # no start date / not in the future
        with pytest.raises(Refused):
            svc.schedule([tid], deadline="2026-12-01")
    assert t.crashes == 0


def test_guard_passes_closed_items(svc, r, t, w):
    res = svc.set_status([w.done], "open")                  # logged: in no list, but closed items can't be templates
    assert res.verified is True and t.get(w.done)["status"] == "open"
    assert r.calls("MEMBERSHIP") == []


def test_complete_project_with_open_children_refused(svc, r, t, w):
    for status in ("completed", "canceled"):
        with pytest.raises(Refused) as e:
            svc.set_status([w.party], status)
        assert {c["id"] for c in e.value.details["open_children"]} == {w.milk, w.book}
        assert e.value.hint == "complete or cancel these first (things_set_status), then the project"
    nothing_sent(r, t)


def test_children_closed_in_the_same_batch_count_as_closed(svc, r, t):
    p = t.seed_project("Small")
    a, b = t.seed_todo("a", project=p), t.seed_todo("b", project=p)
    res = svc.set_status([p, a, b], "completed")
    objs = payload(dispatched(r)[0])
    assert [o["id"] for o in objs] == [a, b, p]                 # children first
    assert res.verified is True and t.get(p)["status"] == "completed"


def test_reopening_a_project_needs_no_children_guard(svc, r, t, w):
    res = svc.set_status([w.old], "open")
    assert res.verified is True and r.calls("SCAN") == []


def test_batch_confirm_threshold(svc, r, t, clock):
    ids = [t.seed_todo(f"t{i}") for i in range(11)]
    with pytest.raises(Refused) as e:
        svc.set_status(ids, "completed")
    assert e.value.hint == "re-run with confirm=true after showing the user the list"
    nothing_sent(r, t)
    assert r.template_calls == []                              # refused before any read
    assert svc.set_status(ids[:10], "completed").sent is True
    assert svc.set_status(ids, "canceled", confirm=True).verified is True


def test_batch_limit(r, t, clock):
    ids = [t.seed_todo(f"t{i}") for i in range(6)]
    svc = ThingsService(r, Config(max_batch=5, confirm_threshold=2), today=clock.today)
    with pytest.raises(Refused) as e:
        svc.move(ids, area=t.seed_area("A"), confirm=True)
    assert e.value.details == {"count": 6, "max_batch": 5}
    with pytest.raises(Refused):
        svc.tag(ids[:3], add=["x"])
    with pytest.raises(InvalidInput):
        svc.set_status([], "completed")
    with pytest.raises(Refused):
        svc.add_todos([TodoSpec(title=f"n{i}") for i in range(3)])
    assert r.dispatch_calls == []


def test_duplicate_ids_count_once(svc, r, w):
    res = svc.set_status([w.milk, w.milk], "completed")
    assert res.ids == [w.milk] and len(payload(dispatched(r)[0])) == 1


def test_reopen_warning(svc, r, t, w):
    res = svc.add_todo(TodoSpec(title="Follow-up", project=w.old))
    assert "Things will reopen the project 'Old Project' (it is completed)" in res.warnings[0]
    assert t.get(w.old)["status"] == "open"                      # the side effect the warning is about
    t._set_status(t.get(w.old), "completed")
    res = svc.move([w.inbox], project=w.old)
    assert any("reopen the project 'Old Project'" in m for m in res.warnings)
    res = svc.move([w.done], project=w.old)                      # a closed to-do doesn't reopen anything
    assert not any("reopen" in m for m in res.warnings)
    res = svc.add_todo(TodoSpec(title="Plain", project=w.taxes))
    assert res.warnings == []


def test_heading_warning(svc, r, t, w):
    res = svc.add_todo(TodoSpec(title="Book DJ", project=w.party, heading="Venue"))
    assert HEADING_WARNING in res.warnings and "heading" in res.unverifiable
    assert params(dispatched(r)[0])["heading"] == "Venue"
    assert t.heading_title(t.one("Book DJ")) == "Venue"
    with pytest.raises(InvalidInput):
        svc.add_todo(TodoSpec(title="x", heading="Venue"))
    with pytest.raises(InvalidInput):
        svc.update_todo(w.inbox, TodoPatch(heading="Venue"))         # not in a project
    res = svc.update_todo(w.milk, TodoPatch(heading="Venue"))       # within its current project
    assert HEADING_WARNING in res.warnings and t.heading_title(t.get(w.milk)) == "Venue"


def test_past_when_refused(svc, r, t, w):
    past = "2026-09-27"
    calls = [lambda: svc.add_todo(TodoSpec(title="x", when=past)),
             lambda: svc.add_todos([TodoSpec(title="x", when=past)]),
             lambda: svc.add_project(ProjectSpec(title="x", when=past)),
             lambda: svc.add_project(ProjectSpec(title="x", items=[ProjectItemSpec(kind="todo", title="y",
                                                                                   when=past)])),
             lambda: svc.update_todo(w.milk, TodoPatch(when=past)),
             lambda: svc.update_project(w.party, ProjectPatch(when=past)),
             lambda: svc.schedule([w.milk], when=past),
             lambda: svc.raw_json([{"type": "to-do", "attributes": {"title": "x", "when": past}}]),
             lambda: svc.raw_json([{"type": "project", "attributes": {"title": "x", "items": [
                 {"type": "to-do", "attributes": {"title": "y", "when": past + "@9:00am"}}]}}])]
    for call in calls:
        with pytest.raises(InvalidInput) as e:
            call()
        assert "in the past" in e.value.message
    nothing_sent(r, t)
    assert svc.add_todo(TodoSpec(title="today by date", when="2026-09-28")).verified is True


# ---- routing -----------------------------------------------------------------------------------------------------

def test_add_todo_routes_to_add(svc, r, t, w):
    res = svc.add_todo(TodoSpec(title="Buy bread", notes="Wholemeal\r\nfresh", when="tomorrow", reminder_time="18:00",
                                deadline="2026-10-10", tags=["Errand"], checklist=["a", "b"], project=w.party),
                       reveal=True)
    turl = dispatched(r)[0]
    assert turl.command == "add" and turl.requires_token is False
    assert params(turl) == {"title": "Buy bread", "notes": "Wholemeal\nfresh", "when": "tomorrow@6:00pm",
                            "deadline": "2026-10-10", "tags": "Errand", "checklist-items": "a\nb",
                            "list-id": w.party, "reveal": "true"}
    new = t.one("Buy bread")
    assert res.action == "add_todo" and res.ids == [new["id"]] and res.links == [f"things:///show?id={new['id']}"]
    assert res.verified is True and res.sent is True and res.dry_run is False
    assert set(res.unverifiable) == {"reminder time", "checklist"}
    assert {c.field for c in res.checks} == {"title", "notes", "start_date", "deadline", "tags", "project_id"}
    assert res.items[0].id == new["id"] and res.before == []
    call = r.calls("FIND_CREATED")[0]
    assert call.args[:2] == ("to-do", "Buy bread") and call.args[3:5] == ("project", w.party)
    assert all(c.locked for c in r.dispatch_calls)


@pytest.mark.parametrize("when, scope", [(None, ("list", "inbox")), ("today", ("list", "today")),
                                         ("evening", ("list", "today")), ("2026-09-28", ("list", "today")),
                                         ("tomorrow", ("list", "upcoming")), ("2026-12-24", ("list", "upcoming")),
                                         ("anytime", ("list", "anytime")), ("someday", ("list", "someday"))])
def test_find_created_scope_follows_the_destination(svc, r, w, when, scope):
    res = svc.add_todo(TodoSpec(title="Scoped", when=when))
    assert r.calls("FIND_CREATED")[0].args[3:5] == scope
    assert res.verified is True


def test_add_todo_into_an_area(svc, r, t, w):
    res = svc.add_todo(TodoSpec(title="Area task", area="Work"))
    assert params(dispatched(r)[0])["list-id"] == w.work
    assert r.calls("FIND_CREATED")[0].args[3:5] == ("area", w.work)
    assert res.verified is True and any(c.field == "area_id" and c.ok for c in res.checks)


@pytest.mark.parametrize("spec", [
    TodoSpec(title=""), TodoSpec(title="a\nb"), TodoSpec(title="x", reminder_time="6pm"),
    TodoSpec(title="x", when="next week"), TodoSpec(title="x", when="today@6pm"),
    TodoSpec(title="x", when="someday", reminder_time="6pm"), TodoSpec(title="x", deadline="friday"),
    TodoSpec(title="x", project="P", area="A"), TodoSpec(title="x", checklist=["ok"] * 101),
    TodoSpec(title="x", reminder_time="9:05", when="today"), TodoSpec(title="x", notes="n" * 10_001),
])
def test_add_todo_validation(svc, r, spec):
    with pytest.raises(InvalidInput):
        svc.add_todo(spec)
    assert r.template_calls == [] and r.dispatch_calls == []


def test_titles_are_trimmed_so_read_back_matches(svc, r, t, w):
    res = svc.add_todo(TodoSpec(title="  Padded  "))
    assert params(dispatched(r)[0])["title"] == "Padded" and res.verified is True
    res = svc.update_todo(w.milk, TodoPatch(title=" Milk "))
    assert params(dispatched(r)[1])["title"] == "Milk" and res.checks[0].expected == "Milk"
    assert res.verified is True


def test_add_todos_is_one_json_create(svc, r, t, w):
    res = svc.add_todos([TodoSpec(title="One"),
                         TodoSpec(title="Two", when="today", project="Taxes", tags=["deep work"]),
                         TodoSpec(title="Three", checklist=["c1"], heading="Venue", project=w.party)])
    assert len(dispatched(r)) == 1
    turl = dispatched(r)[0]
    assert turl.requires_token is False and turl.weight == 3
    objs = payload(turl)
    assert [o["operation"] for o in objs] == ["create"] * 3
    assert objs[1]["attributes"] == {"title": "Two", "when": "today", "tags": ["Deep Work"], "list-id": w.taxes}
    assert objs[2]["attributes"]["checklist-items"] == [{"type": "checklist-item", "operation": "create",
                                                         "attributes": {"title": "c1"}}]
    assert objs[2]["attributes"]["heading"] == "Venue"
    assert res.action == "add_todos" and res.verified is True
    assert res.ids == [t.one("One")["id"], t.one("Two")["id"], t.one("Three")["id"]]
    assert {"checklist", "heading"} <= set(res.unverifiable)


def test_add_todos_needs_confirm_above_ten(svc, r, t):
    specs = [TodoSpec(title=f"Item {i}") for i in range(11)]
    with pytest.raises(Refused):
        svc.add_todos(specs)
    nothing_sent(r, t)
    res = svc.add_todos(specs, confirm=True)
    assert res.verified is True and len(res.ids) == 11


def test_add_todos_errors_name_the_item(svc, r):
    with pytest.raises(InvalidInput) as e:
        svc.add_todos([TodoSpec(title="ok"), TodoSpec(title="x", when="yesterday")])
    assert e.value.message.startswith("todos[1].when:") and e.value.details["field"] == "todos[1].when"
    with pytest.raises(InvalidInput):
        svc.add_todos([])
    with pytest.raises(InvalidInput):
        svc.add_todos(TodoSpec(title="x"))
    assert r.template_calls == []


def test_add_todos_same_title_twice_checks_existence_only(svc, r, t):
    res = svc.add_todos([TodoSpec(title="Twin", when="today"), TodoSpec(title="Twin", when="someday")])
    assert res.verified is True and len(res.ids) == 2 and set(res.ids) == {i["id"] for i in t.by_title("Twin")}
    assert {c.field for c in res.checks} == {"title"}
    assert any("share the title 'Twin'" in m for m in res.warnings)


def test_add_project_plain_uses_add_project(svc, r, t, w):
    res = svc.add_project(ProjectSpec(title="Build treehouse", notes="Oak", when="today", deadline="2026-12-01",
                                      tags=["home"], area="family",
                                      items=[ProjectItemSpec(kind="todo", title="Wood"),
                                             ProjectItemSpec(kind="todo", title="Nails")]), reveal=True)
    turl = dispatched(r)[0]
    assert turl.command == "add-project" and turl.weight == 3
    assert params(turl) == {"title": "Build treehouse", "notes": "Oak", "when": "today", "deadline": "2026-12-01",
                            "tags": "Home", "area-id": w.family, "to-dos": "Wood\nNails", "reveal": "true"}
    pid = t.one("Build treehouse")["id"]
    assert res.ids == [pid] and res.verified is True
    assert {c.field for c in res.checks} >= {"title", "area_id", "children", "tags", "deadline", "start_date"}
    assert r.calls("FIND_CREATED")[0].args[:1] == ("project",)


def test_add_project_without_items(svc, r, t):
    res = svc.add_project(ProjectSpec(title="Solo"))
    assert dispatched(r)[0].command == "add-project" and "to-dos" not in params(dispatched(r)[0])
    assert res.verified is True


def test_add_project_with_headings_uses_json(svc, r, t, w):
    res = svc.add_project(ProjectSpec(title="Trip", area=w.work, items=[
        ProjectItemSpec(kind="todo", title="Passport", tags=["Home"]),
        ProjectItemSpec(kind="heading", title="Packing"),
        ProjectItemSpec(kind="todo", title="Socks", checklist=["wool"], when="someday", deadline="2026-11-01")]))
    turl = dispatched(r)[0]
    assert turl.command == "json" and turl.requires_token is False and turl.weight == 4
    (obj,) = payload(turl)
    assert obj["type"] == "project" and obj["attributes"]["area-id"] == w.work
    items = obj["attributes"]["items"]
    assert [i["type"] for i in items] == ["to-do", "heading", "to-do"]
    assert items[2]["attributes"] == {"title": "Socks", "when": "someday", "deadline": "2026-11-01",
                                      "checklist-items": [{"type": "checklist-item", "operation": "create",
                                                           "attributes": {"title": "wool"}}]}
    pid = t.one("Trip")["id"]
    assert res.verified is True and res.ids == [pid]
    assert {"heading", "checklist"} <= set(res.unverifiable)
    assert [c for c in res.checks if c.field == "children"][0].ok
    assert t.heading_title(t.one("Socks")) == "Packing"


def test_add_project_rich_todo_uses_json(svc, r, w):
    svc.add_project(ProjectSpec(title="P", items=[ProjectItemSpec(kind="todo", title="a", notes="n")]))
    assert dispatched(r)[0].command == "json"


def test_add_project_validation(svc, r, w):
    for spec in (ProjectSpec(title="x", items=[ProjectItemSpec(kind="heading", title="H", notes="n")]),
                 ProjectSpec(title="x", items=[ProjectItemSpec(kind="section", title="H")]),
                 ProjectSpec(title="x", items=[ProjectItemSpec(kind="todo", title="t")] * 100),
                 ProjectSpec(title="x", reminder_time="6pm")):
        with pytest.raises(InvalidInput):
            svc.add_project(spec)
    with pytest.raises(InvalidInput) as e:
        svc.add_project(ProjectSpec(title="x", items=[ProjectItemSpec(kind="todo", title="ok"),
                                                      ProjectItemSpec(kind="todo", title="")]))
    assert e.value.details["field"] == "items[1].title"
    assert r.template_calls == []


def test_update_todo_routes_to_update(svc, r, t, w):
    res = svc.update_todo(w.milk, TodoPatch(title="Buy oat milk", prepend_notes="P0 ", append_notes=" Z9",
                                            when="2026-10-05", reminder_time="7:30am", deadline="today",
                                            add_tags=["home"], checklist_append=["x"], checklist_prepend=["y"]))
    (turl,) = dispatched(r)
    assert turl.command == "update" and turl.requires_token is True
    assert params(turl) == {"id": w.milk, "title": "Buy oat milk", "prepend-notes": "P0 ", "append-notes": " Z9",
                            "when": "2026-10-05@7:30am", "deadline": "today", "add-tags": "Home",
                            "prepend-checklist-items": "y", "append-checklist-items": "x"}
    assert res.verified is True and res.ids == [w.milk] and res.links == [f"things:///show?id={w.milk}"]
    assert set(res.unverifiable) == {"reminder time", "checklist"}
    assert r.sent_urls[0].endswith("&auth-token=***") and FAKE_TOKEN not in json.dumps(res.urls)
    assert t.get(w.milk)["notes"] == "P0 2% Z9" and t.get(w.milk)["tags"] == ["Errand", "Home"]


def test_update_todo_move(svc, r, t, w):
    res = svc.update_todo(w.inbox, TodoPatch(project="Taxes"))
    assert params(dispatched(r)[0]) == {"id": w.inbox, "list-id": w.taxes}
    assert res.verified is True and t.get(w.inbox)["project"] == w.taxes
    res = svc.update_todo(w.milk, TodoPatch(area="Work"))
    assert res.verified is True and t.get(w.milk)["area"] == w.work
    assert {(c.field, c.expected) for c in res.checks} == {("area_id", w.work), ("project_id", None)}


def test_update_todo_anytime_routes_to_json(svc, r, t, w):
    res = svc.update_todo(w.milk, TodoPatch(title="Milk", when="anytime"))
    first, second = dispatched(r)
    assert first.command == "update" and params(first) == {"id": w.milk, "title": "Milk"}
    assert second.command == "json" and second.requires_token is True
    assert payload(second) == [{"type": "to-do", "operation": "update", "id": w.milk,
                                "attributes": {"when": "anytime"}}]
    assert res.verified is True and len(res.urls) == 2
    assert {c.field for c in res.checks} == {"title", "start_date", "member"}
    r.dispatch_calls.clear()
    svc.update_todo(w.evening, TodoPatch(when="anytime"))
    assert [u_.command for u_ in dispatched(r)] == ["json"]


def test_update_project_routes(svc, r, t, w):
    res = svc.update_project(w.party, ProjectPatch(title="Party!", tags=["home"], area="Work", when="anytime"))
    first, second = dispatched(r)
    assert first.command == "update-project"
    assert params(first) == {"id": w.party, "title": "Party!", "tags": "Home", "area-id": w.work}
    assert payload(second) == [{"type": "project", "operation": "update", "id": w.party,
                                "attributes": {"when": "anytime"}}]
    assert res.verified is True and t.get(w.party)["area"] == w.work and res.action == "update_project"


def test_deadline_clear_uses_plain_update(svc, r, t, w):
    res = svc.update_todo(w.milk, TodoPatch(clear_deadline=True))
    assert params(dispatched(r)[0]) == {"id": w.milk, "deadline": ""}
    assert res.verified is True and t.get(w.milk)["deadline"] is None
    r.dispatch_calls.clear()
    svc.update_project(w.party, ProjectPatch(clear_deadline=True))
    assert dispatched(r)[0].command == "update-project" and params(dispatched(r)[0])["deadline"] == ""


def test_notes_empty_string_clears(svc, r, t, w):
    res = svc.update_todo(w.milk, TodoPatch(notes=""))
    assert params(dispatched(r)[0]) == {"id": w.milk, "notes": ""}
    assert res.verified is True and t.get(w.milk)["notes"] == ""


@pytest.mark.parametrize("patch", [
    TodoPatch(), TodoPatch(notes="a", append_notes="b"), TodoPatch(deadline="today", clear_deadline=True),
    TodoPatch(tags=["a"], add_tags=["b"]), TodoPatch(project="P", area="A"), TodoPatch(reminder_time="6pm"),
    TodoPatch(heading="H", area="A"), TodoPatch(checklist_append=["x"] * 101), TodoPatch(title="   "),
    TodoPatch(clear_deadline="yes"),
])
def test_update_todo_validation(svc, r, w, patch):
    with pytest.raises(InvalidInput):
        svc.update_todo(w.milk, patch)
    assert r.template_calls == [] and r.dispatch_calls == []


def test_update_validates_the_id_first(svc, r):
    for bad in ("", "short", "has space in it", "x" * 70):
        with pytest.raises(InvalidInput):
            svc.update_todo(bad, TodoPatch(title="x"))
    assert r.template_calls == []


def test_schedule_uses_json_update_ops(svc, r, t, w):
    res = svc.schedule([w.milk, w.party], when="2026-10-10", reminder_time="6pm", deadline="2026-10-31")
    (turl,) = dispatched(r)
    assert turl.command == "json" and turl.requires_token is True and turl.weight == 2
    assert payload(turl) == [
        {"type": "to-do", "operation": "update", "id": w.milk,
         "attributes": {"when": "2026-10-10@6:00pm", "deadline": "2026-10-31"}},
        {"type": "project", "operation": "update", "id": w.party,
         "attributes": {"when": "2026-10-10@6:00pm", "deadline": "2026-10-31"}}]
    assert res.verified is True and res.unverifiable == ["reminder time"]
    assert t.get(w.party)["deadline"] == "2026-10-31"


def test_schedule_someday_and_anytime(svc, t, w):
    assert svc.schedule([w.milk], when="someday").verified is True
    assert t.get(w.milk)["start"] == "someday"
    assert svc.schedule([w.milk], when="anytime").verified is True


def test_schedule_clear_deadline_uses_plain_update(svc, r, t, w):
    res = svc.schedule([w.milk, w.party], when="today", clear_deadline=True)
    cmds = [x.command for x in dispatched(r)]
    assert cmds == ["json", "update", "update-project"]
    assert params(dispatched(r)[1]) == {"id": w.milk, "deadline": ""}
    assert params(dispatched(r)[2]) == {"id": w.party, "deadline": ""}
    assert res.verified is True and t.get(w.milk)["deadline"] is None
    r.dispatch_calls.clear()
    svc.schedule([w.milk], clear_deadline=True)
    assert [x.command for x in dispatched(r)] == ["update"]


def test_schedule_validation(svc, r, w):
    with pytest.raises(InvalidInput):
        svc.schedule([w.milk])
    with pytest.raises(InvalidInput):
        svc.schedule([w.milk], deadline="today", clear_deadline=True)
    with pytest.raises(InvalidInput):
        svc.schedule([w.milk], reminder_time="6pm")
    with pytest.raises(InvalidInput):
        svc.schedule(w.milk, when="today")                         # ids must be a list
    assert r.dispatch_calls == []


@pytest.mark.parametrize("status, attrs", [("completed", {"completed": True}), ("canceled", {"canceled": True})])
def test_set_status_uses_json(svc, r, t, w, status, attrs):
    res = svc.set_status([w.milk, w.evening], status)
    (turl,) = dispatched(r)
    assert turl.requires_token is True and turl.weight == 2
    assert payload(turl) == [{"type": "to-do", "operation": "update", "id": i, "attributes": attrs}
                             for i in (w.milk, w.evening)]
    assert res.verified is True and t.get(w.milk)["status"] == status and res.action == "set_status"


def test_set_status_open_reopens_canceled_items(svc, r, t, w):
    tid = t.seed_todo("Dropped", status="canceled")
    res = svc.set_status([tid], "open")
    assert payload(dispatched(r)[0])[0]["attributes"] == {"completed": False}
    assert res.verified is True and t.get(tid)["status"] == "open"


def test_set_status_warns_when_already_there(svc, w):
    res = svc.set_status([w.done], "completed")
    assert res.warnings == ["'Call Paolo' is already completed"] and res.verified is True
    with pytest.raises(InvalidInput):
        svc.set_status([w.milk], "done")


def test_move_todos_via_json_and_projects_via_update_project(svc, r, t, w):
    res = svc.move([w.inbox, w.chore], project="Plan Birthday Party", heading="Venue")
    (turl,) = dispatched(r)
    assert payload(turl) == [{"type": "to-do", "operation": "update", "id": i,
                              "attributes": {"list-id": w.party, "heading": "Venue"}} for i in (w.inbox, w.chore)]
    assert res.verified is True and res.unverifiable == ["heading"]
    assert t.get(w.chore)["project"] == w.party and t.heading_title(t.get(w.chore)) == "Venue"
    r.dispatch_calls.clear()
    res = svc.move([w.party, w.milk], area="Work")
    assert [x.command for x in dispatched(r)] == ["json", "update-project"]
    assert params(dispatched(r)[1]) == {"id": w.party, "area-id": w.work}
    assert res.verified is True and t.get(w.party)["area"] == w.work and t.get(w.milk)["area"] == w.work


def test_move_refuses_a_project_into_a_project(svc, r, t, w):
    with pytest.raises(InvalidInput):
        svc.move([w.taxes], project=w.party)
    with pytest.raises(InvalidInput):
        svc.move([w.milk], area=w.work, heading="Venue")
    nothing_sent(r, t)


def test_tag_routes(svc, r, t, w):
    res = svc.tag([w.milk, w.party], add=["home", "deep work"])
    first, second = dispatched(r)
    assert (first.command, params(first)) == ("update", {"id": w.milk, "add-tags": "Home,Deep Work"})
    assert (second.command, params(second)) == ("update-project", {"id": w.party, "add-tags": "Home,Deep Work"})
    assert res.verified is True and t.get(w.milk)["tags"] == ["Errand", "Home", "Deep Work"]
    assert {c.expected for c in res.checks} == {"includes Home, Deep Work"}
    r.dispatch_calls.clear()
    res = svc.tag([w.milk], replace=["Deep Work"])
    assert params(dispatched(r)[0]) == {"id": w.milk, "tags": "Deep Work"}
    assert res.verified is True and t.get(w.milk)["tags"] == ["Deep Work"]
    r.dispatch_calls.clear()
    res = svc.tag([w.milk], replace=[])
    assert params(dispatched(r)[0]) == {"id": w.milk, "tags": ""}
    assert res.verified is True and t.get(w.milk)["tags"] == []


def test_tag_validation(svc, r, w):
    for kwargs in ({}, {"add": ["a"], "replace": ["b"]}, {"add": []}, {"add": "Home"}):
        with pytest.raises(InvalidInput):
            svc.tag([w.milk], **kwargs)
    assert r.dispatch_calls == []


# ---- things_json ----------------------------------------------------------------------------------------------

def test_raw_json_creates_need_no_token(svc, r, t, w):
    data = [{"type": "to-do", "attributes": {"title": "From JSON", "list-id": w.party, "tags": ["home", "nope"]}},
            {"type": "project", "attributes": {"title": "JSON project", "area-id": w.work, "items": [
                {"type": "heading", "attributes": {"title": "H"}},
                {"type": "to-do", "attributes": {"title": "child"}}]}}]
    res = svc.raw_json(data, reveal=True)
    (turl,) = dispatched(r)
    assert turl.requires_token is False and params(turl)["reveal"] == "true" and turl.weight == 4
    assert payload(turl)[0]["attributes"]["tags"] == ["Home"]
    assert res.warnings[0] == UNKNOWN_TAG_WARNING.format("nope") and HEADING_WARNING not in res.warnings
    assert res.verified is True and res.action == "json"
    assert res.ids == [t.one("From JSON")["id"], t.one("JSON project")["id"]]


def test_raw_json_updates(svc, r, t, w):
    res = svc.raw_json([{"type": "to-do", "operation": "update", "id": w.inbox,
                         "attributes": {"title": "Renamed", "list-id": w.taxes, "when": "someday"}},
                        {"type": "project", "operation": "update", "id": w.taxes, "attributes": {"notes": "N"}}])
    (turl,) = dispatched(r)
    assert turl.requires_token is True and r.sent_urls[0].endswith("&auth-token=***")
    assert res.verified is True and res.ids == [w.inbox, w.taxes]
    assert [b.id for b in res.before] == [w.inbox, w.taxes]
    assert {(c.target_id, c.field) for c in res.checks} >= {(w.inbox, "title"), (w.inbox, "project_id"),
                                                             (w.inbox, "member"), (w.taxes, "notes")}


def test_raw_json_status_ops_are_verified_and_guarded(svc, r, t, w):
    res = svc.raw_json([{"type": "to-do", "operation": "update", "id": w.milk, "attributes": {"completed": True}}])
    assert res.verified is True and res.checks[0].expected == "completed"
    with pytest.raises(Refused):
        svc.raw_json([{"type": "to-do", "operation": "update", "id": w.tmpl, "attributes": {"deadline": "today"}}])
    with pytest.raises(Refused) as e:
        svc.raw_json([{"type": "project", "operation": "update", "id": w.party, "attributes": {"canceled": True}}])
    assert e.value.details["open_children"] == [{"id": w.book, "title": "Book venue"}]
    assert t.crashes == 0


def test_raw_json_children_closed_earlier_in_the_payload(svc, r, t):
    p = t.seed_project("P")
    a = t.seed_todo("a", project=p)
    close_p = {"type": "project", "operation": "update", "id": p, "attributes": {"completed": True}}
    close_a = {"type": "to-do", "operation": "update", "id": a, "attributes": {"completed": True}}
    with pytest.raises(Refused):
        svc.raw_json([close_p, close_a])                      # the project would be processed first
    assert svc.raw_json([close_a, close_p]).verified is True


@pytest.mark.parametrize("data, exc, where", [
    ([{"type": "to-do", "attributes": {"title": "x", "list": "Taxes"}}], InvalidInput, "/0/attributes/list"),
    ([{"type": "project", "attributes": {"title": "x", "area": "Work"}}], InvalidInput, "/0/attributes/area"),
    ([{"type": "to-do", "attributes": {"title": "x", "list-id": GHOST}}], NotFound, "/0/attributes/list-id"),
    ([{"type": "to-do", "attributes": {"title": "x", "bogus": 1}}], InvalidInput, "/0/attributes/bogus"),
    ([{"type": "to-do", "attributes": {"title": "x", "add-tags": ["a"]}}], InvalidInput, None),
    ([{"type": "to-do", "operation": "update", "id": GHOST, "attributes": {"title": "x"}}], NotFound, None),
    ([], InvalidInput, "data"),
])
def test_raw_json_refusals(svc, r, t, w, data, exc, where):
    with pytest.raises(exc) as e:
        svc.raw_json(data)
    if where:
        assert e.value.details.get("field") == where
    nothing_sent(r, t)


def test_raw_json_container_kinds(svc, r, t, w):
    with pytest.raises(InvalidInput) as e:
        svc.raw_json([{"type": "to-do", "attributes": {"title": "x", "list-id": w.milk}}])
    assert "is a to-do" in e.value.message
    with pytest.raises(InvalidInput) as e:
        svc.raw_json([{"type": "project", "attributes": {"title": "x", "area-id": w.party}}])
    assert "is a project, not an area" in e.value.message
    with pytest.raises(InvalidInput) as e:
        svc.raw_json([{"type": "project", "operation": "update", "id": w.milk, "attributes": {"title": "x"}}])
    assert "is a to-do, not a project" in e.value.message
    nothing_sent(r, t)


def test_raw_json_confirm_counts_update_operations(svc, r, t):
    ids = [t.seed_todo(f"t{i}") for i in range(11)]
    ops = [{"type": "to-do", "operation": "update", "id": i, "attributes": {"notes": "n"}} for i in ids]
    with pytest.raises(Refused):
        svc.raw_json(ops)
    nothing_sent(r, t)
    creates = [{"type": "to-do", "attributes": {"title": f"c{i}"}} for i in range(12)]
    assert svc.raw_json(creates).verified is True                   # creates need no confirm
    assert svc.raw_json(ops, confirm=True).verified is True


def test_raw_json_update_with_only_unknown_tags_is_dropped(svc, r, t, w):
    res = svc.raw_json([{"type": "to-do", "operation": "update", "id": w.milk, "attributes": {"tags": ["Nope"]}}])
    assert res.sent is False and r.dispatch_calls == [] and t.get(w.milk)["tags"] == ["Errand"]
    assert any("Nothing left to update" in m for m in res.warnings)


def test_raw_json_reopen_warning(svc, w):
    res = svc.raw_json([{"type": "to-do", "attributes": {"title": "x", "list-id": w.old}}])
    assert any("reopen the project 'Old Project'" in m for m in res.warnings)


# ---- dry run -----------------------------------------------------------------------------------------------------

DRY_CALLS = {
    "add_todo": lambda s, w: s.add_todo(TodoSpec(title="x", project="Taxes", tags=["home"]), dry_run=True),
    "add_todos": lambda s, w: s.add_todos([TodoSpec(title="x"), TodoSpec(title="y")], dry_run=True),
    "add_project": lambda s, w: s.add_project(ProjectSpec(title="P", items=[ProjectItemSpec("heading", "H")]),
                                              dry_run=True),
    "update_todo": lambda s, w: s.update_todo(w.milk, TodoPatch(title="x", when="anytime"), dry_run=True),
    "update_project": lambda s, w: s.update_project(w.party, ProjectPatch(title="x"), dry_run=True),
    "set_status": lambda s, w: s.set_status([w.milk], "completed", dry_run=True),
    "schedule": lambda s, w: s.schedule([w.milk], when="today", clear_deadline=True, dry_run=True),
    "move": lambda s, w: s.move([w.milk], area="Work", dry_run=True),
    "tag": lambda s, w: s.tag([w.milk], add=["Home"], dry_run=True),
    "json": lambda s, w: s.raw_json([{"type": "to-do", "operation": "update", "id": w.milk,
                                      "attributes": {"title": "x"}}], dry_run=True),
}


@pytest.mark.parametrize("name", sorted(DRY_CALLS))
def test_dry_run_dispatches_nothing(svc, r, t, w, name):
    res = DRY_CALLS[name](svc, w)
    assert res.action == name and res.sent is False and res.dry_run is True and res.verified is None
    assert res.urls and all(u_.startswith("things:///") for u_ in res.urls)
    assert all(c.outcome == "dry-run" for c in r.dispatch_calls) and r.sent == []
    assert t.log == [] and r.write_locks == 0
    assert r.calls("FIND_CREATED") == []                         # no verification reads
    assert FAKE_TOKEN not in json.dumps(res.urls)
    if dispatched(r)[0].requires_token:
        assert res.urls[0].endswith("&auth-token=***")


def test_dry_run_still_validates_on_macos(svc, r, t, w):
    with pytest.raises(Refused):
        svc.set_status([w.tmpl], "completed", dry_run=True)
    with pytest.raises(NotFound):
        svc.add_todo(TodoSpec(title="x", project="Nope"), dry_run=True)
    res = svc.update_todo(w.milk, TodoPatch(title="x"), dry_run=True)
    assert res.before[0].title == "Buy milk" and res.ids == [w.milk]


def test_offline_dry_run(t, w, clock):
    r = FakeRunner(t, clock=clock, macos=False)
    svc = ThingsService(r, today=clock.today)
    res = svc.update_todo(w.milk, TodoPatch(title="x", tags=["anything"]), dry_run=True)
    assert res.warnings == [OFFLINE_WARNING] and res.before == [] and res.dry_run is True
    assert params(dispatched(r)[0]) == {"id": w.milk, "title": "x", "tags": "anything"}
    assert r.template_calls == []
    assert svc.add_todo(TodoSpec(title="x", project=w.taxes), dry_run=True).urls
    assert svc.raw_json([{"type": "to-do", "operation": "update", "id": w.milk, "attributes": {"title": "y"}}],
                        dry_run=True).urls
    assert "list-id=0f8fad5b-d9cb-469f-a165-70867728950e" in svc.add_todo(
        TodoSpec(title="x", area="0f8fad5b-d9cb-469f-a165-70867728950e"), dry_run=True).urls[0]    # legacy UUID
    with pytest.raises(NotMacOS):
        svc.add_todo(TodoSpec(title="x", project="Taxes"), dry_run=True)      # a title needs a read
    for title in ("Groceries", "Shopping1", "Q3-Planning"):                  # id-shaped titles too
        with pytest.raises(NotMacOS):
            svc.add_todo(TodoSpec(title="x", project=title), dry_run=True)
        with pytest.raises(NotMacOS):
            svc.add_project(ProjectSpec(title="x", area=title), dry_run=True)
    with pytest.raises(NotMacOS):
        svc.set_status([w.milk], "completed", dry_run=True)                   # kinds need a read
    with pytest.raises(NotMacOS):
        svc.update_todo(w.milk, TodoPatch(title="x"))                         # real writes need macOS
    with pytest.raises(NotMacOS):
        svc.add_todo(TodoSpec(title="x"))
    assert r.template_calls == [] and t.log == []
    assert r.write_locks == 0                   # checked before the lock: no state file off macOS


# ---- verification ------------------------------------------------------------------------------------------------

def test_no_redispatch_on_verify_timeout(svc, r, t, w, clock):
    r.drop_next_url()
    res = svc.update_todo(w.milk, TodoPatch(title="Never arrives"))
    assert res.sent is True and res.verified is False
    assert res.reason == not_verified_reason(6.0) and NOTHING_RESENT in res.reason
    assert len(r.dispatch_calls) == 1 and len(r.sent) == 1               # never re-sent (N10)
    assert sum(clock.sleeps) == pytest.approx(6.0)
    assert [c.ok for c in res.checks] == [False] and res.checks[0].observed == "Buy milk"
    assert res.items[0].title == "Buy milk"


def test_add_not_verified_reason(svc, r, t, w, clock):
    r.drop_next_url()
    res = svc.add_todo(TodoSpec(title="Lost"))
    assert res.verified is False and res.ids == [] and res.links == []
    assert res.reason == not_verified_reason(6.0, update=False)
    assert [(c.field, c.expected, c.observed, c.ok) for c in res.checks] == [("created", "Lost", None, False)]
    assert len(r.dispatch_calls) == 1 and r.calls("ROWS") == []         # nothing found, nothing to re-read


def test_add_behind_the_enable_sheet_is_diagnosed(svc, r, t, w):
    t.urls_enabled = False                                      # the first URL opens "Enable Things URLs?"
    res = svc.add_todo(TodoSpec(title="Parked"))
    assert res.verified is False and res.reason == dialog_reason()
    assert t.parked == 1 and len(r.dispatch_calls) == 1
    assert r.calls("FIND_CREATED")[-1].args[3:] == ("app", "", "1")
    t.enable_urls()                                             # the user clicks Enable: it applies late
    assert t.one("Parked")


def test_late_write_is_verified(svc, r, t, w, clock):
    r.url_delay_s = 2.0
    res = svc.update_todo(w.milk, TodoPatch(title="Late"))
    assert res.verified is True and 2.0 <= sum(clock.sleeps) < 3.0
    assert len(r.sent) == 1


def test_wrong_token_is_not_verified(svc, r, t, w):
    r.token = "WrongTokenWrongToken12"
    res = svc.update_todo(w.milk, TodoPatch(title="x"))
    assert res.verified is False and "auth token is wrong" in res.reason
    assert t.errors and t.get(w.milk)["title"] == "Buy milk"
    assert "WrongTokenWrongToken12" not in json.dumps(res.urls)


def test_verification_off(r, t, w, clock):
    svc = ThingsService(r, Config(verify=False), today=clock.today)
    res = svc.update_todo(w.milk, TodoPatch(title="x"))
    assert res.sent is True and res.verified is None and res.checks == [] and res.items == []
    assert "not checked" in res.warnings[-1]
    assert clock.sleeps == []
    res = svc.add_todo(TodoSpec(title="unchecked"))
    assert res.verified is None and res.ids == [] and r.calls("FIND_CREATED") == []


def test_verify_timeout_from_config(r, t, w, clock):
    svc = ThingsService(r, Config(verify_timeout_s=2.0), today=clock.today)
    r.drop_next_url()
    res = svc.update_todo(w.milk, TodoPatch(title="x"))
    assert sum(clock.sleeps) == pytest.approx(2.0) and "within 2 s" in res.reason


def test_launch_is_reported_and_extends_verification(svc, r, t, w, clock):
    t.running = False
    r.drop_next_url()
    res = svc.update_todo(w.milk, TodoPatch(title="x"))
    assert res.launched_things is True and r.launches == 1
    assert sum(clock.sleeps) == pytest.approx(15.0)


def test_create_with_mismatched_fields(svc, r, t, w, monkeypatch):
    monkeypatch.setattr(t, "_existing_tags", lambda value: [])       # Things drops the tag after all
    res = svc.add_todo(TodoSpec(title="Tagless", tags=["Home"]))
    assert res.verified is False and res.ids == [t.one("Tagless")["id"]]
    assert res.reason.startswith("Things created it, but these fields don't match what was sent: tags.")
    assert len(r.dispatch_calls) == 1


def test_read_failure_during_verification(svc, r, t, w):
    r.inject("ROWS", exc=AutomationDenied("Not allowed to control Things."), match=lambda args: args[0] == "0")
    res = svc.update_todo(w.milk, TodoPatch(title="x"))             # the snapshot read (notes "1") still works
    assert res.sent is True and res.verified is False
    assert res.reason.startswith("The change was sent, but reading Things back failed ([automation_denied]")
    assert t.get(w.milk)["title"] == "x" and len(r.dispatch_calls) == 1


def test_partial_dispatch_failure(svc, r, t, w, monkeypatch):
    real = r.dispatch
    count = {"n": 0}

    def dispatch(turl, *, dry_run=False):
        count["n"] += 1
        if count["n"] == 2:
            raise DispatchFailed("`open` failed (exit 1): boom")
        return real(turl, dry_run=dry_run)
    monkeypatch.setattr(r, "dispatch", dispatch)
    res = svc.update_todo(w.milk, TodoPatch(title="Half", when="anytime"))
    assert res.sent is True and res.verified is False and len(res.urls) == 1
    assert res.reason.startswith("Only 1 of 2 URLs were sent; the next one failed ([dispatch_failed]")
    assert res.warnings[-1].startswith("Only 1 of 2 URLs were sent")
    assert t.get(w.milk)["title"] == "Half" and count["n"] == 2


def test_first_dispatch_failure_raises(svc, r, t, w):
    r.fail_next_dispatch(DispatchFailed("`open` failed (exit 1): boom"))
    with pytest.raises(DispatchFailed):
        svc.update_todo(w.milk, TodoPatch(title="x"))
    assert r.sent == [] and t.log == []


def test_writes_hold_the_write_lock(svc, r, w):
    svc.update_todo(w.milk, TodoPatch(title="x"))
    svc.add_todo(TodoSpec(title="y"))
    assert r.write_locks == 2 and all(c.locked for c in r.dispatch_calls)


# ---- read-only ---------------------------------------------------------------------------------------------------

WRITES = {
    "add_todo": lambda s, w: s.add_todo(TodoSpec(title="x")),
    "add_todos": lambda s, w: s.add_todos([TodoSpec(title="x")]),
    "add_project": lambda s, w: s.add_project(ProjectSpec(title="x")),
    "update_todo": lambda s, w: s.update_todo(w.milk, TodoPatch(title="x")),
    "update_project": lambda s, w: s.update_project(w.party, ProjectPatch(title="x")),
    "set_status": lambda s, w: s.set_status([w.milk], "canceled"),
    "schedule": lambda s, w: s.schedule([w.milk], when="today"),
    "move": lambda s, w: s.move([w.milk], area=w.work),
    "tag": lambda s, w: s.tag([w.milk], add=["Home"]),
    "json": lambda s, w: s.raw_json([{"type": "to-do", "attributes": {"title": "x"}}]),
    "dry_run": lambda s, w: s.update_todo(w.milk, TodoPatch(title="x"), dry_run=True),
}


@pytest.mark.parametrize("name", sorted(WRITES))
def test_read_only_refuses_every_write(r, t, w, clock, name):
    svc = ThingsService(r, Config(read_only=True), today=clock.today)
    with pytest.raises(Refused) as e:
        WRITES[name](svc, w)
    assert "read-only" in e.value.message and "THINGS_BRIDGE_READ_ONLY" in e.value.hint
    assert r.template_calls == [] and r.dispatch_calls == [] and r.write_locks == 0


def test_read_only_still_reads_and_navigates(r, t, w, clock):
    svc = ThingsService(r, Config(read_only=True), today=clock.today)
    assert svc.list_items("today").returned == 2
    assert svc.show(id=w.milk).sent is True


# ---- navigation ---------------------------------------------------------------------------------------------------

def test_show(svc, r, t, w):
    res = svc.show(id=w.milk)
    assert res.sent is True and res.url == f"things:///show?id={w.milk}"
    turl = dispatched(r)[0]
    assert turl.foreground is True and turl.mutating is False
    assert svc.show(list_key="deadlines").url == "things:///show?id=deadlines"
    assert svc.show(query="Taxes", filter_tags=["Home", "Deep Work"]).url == \
        "things:///show?query=Taxes&filter=Home%2CDeep%20Work"
    assert svc.show(list_key="today", filter_tags=["Errand"]).url == "things:///show?id=today&filter=Errand"
    assert len(t.shown) == 4 and t.log[0] == f"things:///show?id={w.milk}"


@pytest.mark.parametrize("kwargs", [{}, {"id": "x" * 22, "query": "q"}, {"list_key": "trash"},
                                    {"list_key": "Today"}, {"id": "today"}, {"query": ""},
                                    {"query": "q", "filter_tags": ["a,b"]}])
def test_show_validation(svc, r, kwargs):
    with pytest.raises(InvalidInput):
        svc.show(**kwargs)
    assert r.dispatch_calls == []


def test_open_search(svc, r, t):
    assert svc.open_search("vacation").url == "things:///search?query=vacation"
    assert svc.open_search().url == "things:///search"
    assert all(c.turl.foreground for c in r.dispatch_calls)


def test_navigation_launch_rules(r, t, w, clock):
    t.running = False
    ThingsService(r, today=clock.today).show(id=w.milk)
    assert r.launches == 1
    t.running = False
    with pytest.raises(ThingsNotRunning):
        ThingsService(r, Config(allow_launch=False), today=clock.today).open_search("x")


# ---- policy invariants in the source ---------------------------------------------------------------------------

def test_service_never_uses_excluded_params():
    source = Path(service_mod.__file__).read_text(encoding="utf-8")
    for needle in ("duplicate=", "use_clipboard=", "show_quick_entry=", "creation_date=", "completion_date=",
                   "build_version", "print(", "subprocess", "while True"):
        assert needle not in source, needle


def test_not_offered_messages_are_verbatim():
    assert NOT_OFFERED["delete"] == ("I can't delete in Things (by design). I can cancel it (reversible; it goes to "
                                     "the Logbook), or open it with things_show so you can delete it yourself.")
    assert NOT_OFFERED["checklist_edit"] == "I can append or prepend checklist items only."
    assert NOT_OFFERED["repeating"] == "Repeating to-dos must be changed in Things. I can open it for you."


# ---- adversarial-review regressions ------------------------------------------------------------------------------

def _close_project(pid: str) -> dict:
    return {"type": "project", "operation": "update", "id": pid, "attributes": {"completed": True}}


def test_raw_json_project_close_sees_moves_into_it(svc, r, t):
    p, q = t.seed_project("P"), t.seed_project("Q")
    x = t.seed_todo("x", project=q)
    move = {"type": "to-do", "operation": "update", "id": x, "attributes": {"list-id": p}}
    with pytest.raises(Refused) as e:
        svc.raw_json([move, _close_project(p)])
    assert e.value.details["open_children"] == [{"id": x, "title": "x"}]
    nothing_sent(r, t)


def test_raw_json_project_close_sees_moves_out_of_it(svc, r, t):
    p, q = t.seed_project("P"), t.seed_project("Q")
    x = t.seed_todo("x", project=p)
    move = {"type": "to-do", "operation": "update", "id": x, "attributes": {"list-id": q}}
    assert svc.raw_json([move, _close_project(p)]).sent is True


def test_raw_json_project_close_sees_creates_into_it(svc, r, t):
    p = t.seed_project("P")
    create = {"type": "to-do", "attributes": {"title": "new", "list-id": p}}
    with pytest.raises(Refused) as e:
        svc.raw_json([create, _close_project(p)])
    assert e.value.details["open_children"] == [{"id": None, "title": "new"}]
    nothing_sent(r, t)
    done = {"type": "to-do", "attributes": {"title": "done", "list-id": p, "completed": True}}
    assert svc.raw_json([done, _close_project(p)]).sent is True


def test_raw_json_project_close_sees_reopens(svc, r, t):
    p = t.seed_project("P")
    a = t.seed_todo("a", project=p)
    close_a = {"type": "to-do", "operation": "update", "id": a, "attributes": {"completed": True}}
    reopen_a = {"type": "to-do", "operation": "update", "id": a, "attributes": {"completed": False}}
    with pytest.raises(Refused) as e:
        svc.raw_json([close_a, reopen_a, _close_project(p)])
    assert e.value.details["open_children"] == [{"id": a, "title": "a"}]
    nothing_sent(r, t)


def test_raw_json_project_close_sees_a_closed_todo_reopened_and_moved_in(svc, r, t):
    p, q = t.seed_project("P"), t.seed_project("Q")
    x = t.seed_todo("x", project=q, status="completed")
    move = {"type": "to-do", "operation": "update", "id": x, "attributes": {"list-id": p}}
    reopen = {"type": "to-do", "operation": "update", "id": x, "attributes": {"completed": False}}
    assert svc.raw_json([move, _close_project(p)]).sent is True           # still completed: not an open child
    with pytest.raises(Refused):
        svc.raw_json([move, reopen, _close_project(p)])


@pytest.mark.parametrize("when", ["someday", "2026-12-01"])
def test_guard_passes_children_of_a_dormant_project(svc, r, t, when):
    p = t.seed_project("Dormant", when=when)
    c = t.seed_todo("Child", project=p)
    assert svc.update_todo(c, TodoPatch(deadline="2026-12-20")).sent is True
    assert svc.set_status([c], "completed").sent is True
    d = t.seed_todo("Child 2", project=p)
    with pytest.raises(Refused) as e:                  # `when` can crash Things on a template: still refused
        svc.schedule([d], when="today")
    assert e.value.message == REPEATING_REFUSAL and "Someday or future-scheduled project" in e.value.hint
    assert t.crashes == 0


def test_guard_still_refuses_hidden_items_in_an_active_project(svc, r, t):
    p = t.seed_project("Active")
    tmpl = t.seed_todo("Template", project=p, repeating=True)
    with pytest.raises(Refused):
        svc.set_status([tmpl], "completed")
    with pytest.raises(Refused):
        svc.raw_json([{"type": "to-do", "operation": "update", "id": tmpl, "attributes": {"deadline": "2026-12-01"}}])
    nothing_sent(r, t)


def test_add_todo_with_a_long_checklist_uses_json(svc, r, t, w):
    lines = [f"{i:03d}" + "c" * 57 for i in range(100)]            # 6,099 characters once joined
    res = svc.add_todo(TodoSpec(title="Pack", checklist=lines, project=w.party))
    turl = dispatched(r)[0]
    assert turl.command == "json"
    (obj,) = payload(turl)
    assert [i["attributes"]["title"] for i in obj["attributes"]["checklist-items"]] == lines
    assert obj["attributes"]["list-id"] == w.party
    assert res.sent is True and res.ids == [t.one("Pack")["id"]]


def test_add_project_with_many_plain_todos_uses_json(svc, r, t):
    names = [f"{i:02d}" + "c" * 58 for i in range(99)]             # 6,038 characters once joined
    res = svc.add_project(ProjectSpec(title="Big", items=[ProjectItemSpec(kind="todo", title=n) for n in names]))
    turl = dispatched(r)[0]
    assert turl.command == "json"
    assert [i["attributes"]["title"] for i in payload(turl)[0]["attributes"]["items"]] == names
    assert res.sent is True


def test_update_todo_refuses_an_overlong_checklist_append(svc, r, t, w):
    with pytest.raises(InvalidInput):
        svc.update_todo(w.inbox, TodoPatch(checklist_append=["c" * 60] * 100))
    nothing_sent(r, t)
