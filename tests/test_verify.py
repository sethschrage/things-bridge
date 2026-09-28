"""verify.py (DESIGN §2.8): poll timing on a fake clock, expectation mapping per patch field, tag and notes
comparison rules, FIND_CREATED scoping and the sheet diagnosis. Reads only: nothing here may dispatch."""

from datetime import date

import pytest

from fakes import FakeRunner
from things_bridge import urlscheme as u
from things_bridge.errors import AutomationDenied
from things_bridge.models import Item, ProjectPatch, TodoPatch
from things_bridge.verify import (DIALOG_REASON, NOTHING_RESENT, Expect, Verifier, deadline_expect, dialog_reason,
                                  expectations_for_todo_patch, not_verified_reason, read_failed_reason, when_expects)

TODAY = date(2026, 9, 28)
TID = "SyJEz273ceSkabUbciM73A"


@pytest.fixture
def t(fake_things):
    return fake_things


@pytest.fixture
def r(fake_runner):
    return fake_runner


@pytest.fixture
def v(r):
    return Verifier(r)


def no_writes(r: FakeRunner) -> None:
    assert r.dispatch_calls == []


def item(**over) -> Item:
    base = dict(id=TID, kind="to-do", title="t", status="open", tags=[], deadline=None, start_date=None, project=None,
                area=None, notes="", notes_truncated=False, created=None, modified=None, completed_at=None,
                canceled_at=None, link=u.build_show(id=TID).url)
    base.update(over)
    return Item(**base)


# ---- poll timing ----------------------------------------------------------------------------------------

def test_check_polls_0_3_then_every_0_5_until_6_s(r, t, v, clock):
    tid = t.seed_todo("Milk")
    res = v.check([Expect(tid, "title", "eq", "Bread")], launched=False)
    assert res.verified is False
    assert clock.sleeps == pytest.approx([0.3] + [0.5] * 11 + [0.2])
    assert sum(clock.sleeps) == pytest.approx(6.0)
    assert len(r.calls("ROWS")) == 13            # one read per poll, the last one exactly at the deadline
    assert res.checks[0].observed == "Milk" and not res.checks[0].ok
    assert res.reason == not_verified_reason(6.0)
    no_writes(r)


def test_check_uses_15_s_after_a_launch(r, t, v, clock):
    tid = t.seed_todo("Milk")
    res = v.check([Expect(tid, "title", "eq", "Bread")], launched=True)
    assert res.verified is False
    assert sum(clock.sleeps) == pytest.approx(15.0)
    assert clock.sleeps[0] == 0.3 and set(clock.sleeps[1:-1]) == {0.5}
    assert "within 15 s" in res.reason


def test_custom_timings(r, t, clock):
    tid = t.seed_todo("Milk")
    v = Verifier(r, timeout_s=2.0, launched_timeout_s=3.0, initial_delay_s=0.1, interval_s=1.0)
    v.check([Expect(tid, "title", "eq", "x")], launched=False)
    assert clock.sleeps == pytest.approx([0.1, 1.0, 0.9])
    clock.sleeps.clear()
    v.check([Expect(tid, "title", "eq", "x")], launched=True)
    assert sum(clock.sleeps) == pytest.approx(3.0)
    assert Verifier(r, timeout_s=30.0).timeout(True) == 30.0        # a longer configured timeout still wins


def test_invalid_timings_are_rejected(r):
    with pytest.raises(ValueError):
        Verifier(r, interval_s=0)
    with pytest.raises(ValueError):
        Verifier(r, timeout_s=-1)


def test_check_stops_at_the_first_passing_poll(r, t, v, clock):
    tid = t.seed_todo("Milk")
    res = v.check([Expect(tid, "title", "eq", "Milk")], launched=False)
    assert res.verified is True and res.reason is None
    assert clock.sleeps == [0.3]
    assert [i.id for i in res.items] == [tid]
    assert len(r.calls("ROWS")) == 1


def test_check_sees_a_late_write(r, t, v, clock):
    tid = t.seed_todo("Milk")
    r.url_delay_s = 2.0
    r.dispatch(u.build_update(id=tid, title="Bread"))
    r.dispatch_calls.clear()
    res = v.check([Expect(tid, "title", "eq", "Bread")], launched=False)
    assert res.verified is True
    assert 2.0 <= sum(clock.sleeps) < 3.0
    no_writes(r)


def test_no_expectations_means_nothing_to_verify(r, v, clock):
    res = v.check([], launched=False)
    assert res.verified is None and res.checks == [] and res.reason is None
    assert clock.sleeps == [] and r.template_calls == []


def test_read_failure_stops_polling(r, t, v, clock):
    tid = t.seed_todo("Milk")
    r.inject("ROWS", exc=AutomationDenied("Not allowed to control Things."))
    res = v.check([Expect(tid, "title", "eq", "Milk")], launched=False)
    assert res.verified is False
    assert res.reason == read_failed_reason(v.error)
    assert "[automation_denied]" in res.reason and NOTHING_RESENT in res.reason
    assert clock.sleeps == [0.3]
    no_writes(r)


def test_reads_are_batched_and_notes_only_when_needed(r, t, v):
    a, b = t.seed_todo("A", notes="n"), t.seed_todo("B")
    v.check([Expect(a, "title", "eq", "A"), Expect(b, "title", "eq", "B")], launched=False)
    assert r.calls("ROWS")[-1].args == ("0", a, b)
    v.check([Expect(a, "notes", "eq", "n")], launched=False)
    assert r.calls("ROWS")[-1].args == ("1", a)
    assert r.calls("MEMBERSHIP") == [] and r.calls("SCAN") == []


def test_more_than_200_targets_are_read_in_chunks(r, t, v):
    ids = [t.seed_todo(f"T{i}") for i in range(205)]
    res = v.check([Expect(i, "status", "eq", "open") for i in ids], launched=False)
    assert res.verified is True
    assert [len(c.args) - 1 for c in r.calls("ROWS")] == [200, 5]


# ---- evaluation rules ------------------------------------------------------------------------------------

def test_tags_superset_and_eq_are_case_insensitive(r, t, v):
    for name in ("Home", "Errand", "Deep Work"):
        t.seed_tag(name)
    tid = t.seed_todo("x", tags=["Home", "Errand"])
    res = v.check([Expect(tid, "tags", "superset", ["home"]),
                   Expect(tid, "tags", "superset", ["ERRAND", "Home"]),
                   Expect(tid, "tags", "eq", ["errand", "HOME"])], launched=False)
    assert res.verified is True
    assert res.checks[0].expected == "includes home"
    assert res.checks[2].observed == "Home, Errand"
    res = v.check([Expect(tid, "tags", "superset", ["Deep Work"])], launched=False)
    assert res.verified is False
    res = v.check([Expect(tid, "tags", "eq", ["Home"])], launched=False)      # eq means set-equal
    assert res.verified is False


def test_empty_tags_eq_and_none(r, t, v):
    tid = t.seed_todo("x")
    assert v.check([Expect(tid, "tags", "eq", [])], launched=False).verified is True
    assert v.check([Expect(tid, "tags", "none", None)], launched=False).verified is True


def test_notes_rules(r, t, v):
    tid = t.seed_todo("x", notes="P0 line1\nline2 Z9")
    res = v.check([Expect(tid, "notes", "eq", "P0 line1\r\nline2 Z9"),       # CRLF is sent as LF
                   Expect(tid, "notes", "startswith", "P0 "),
                   Expect(tid, "notes", "endswith", " Z9")], launched=False)
    assert res.verified is True
    assert [c.expected for c in res.checks[1:]] == ["P0 …", "… Z9"]
    empty = t.seed_todo("y")
    assert v.check([Expect(empty, "notes", "none", None)], launched=False).verified is True
    assert v.check([Expect(tid, "notes", "none", None)], launched=False).verified is False


def test_long_notes_are_clipped_in_checks(r, t, v):
    tid = t.seed_todo("x", notes="a" * 500)
    res = v.check([Expect(tid, "notes", "eq", "b")], launched=False)
    assert len(res.checks[0].observed) == 201 and res.checks[0].observed.endswith("…")


def test_missing_item_fails_every_check(r, t, v):
    ghost = "GhostGhostGhostGhost01"
    res = v.check([Expect(ghost, "deadline", "none", None), Expect(ghost, "title", "eq", "x")], launched=False)
    assert res.verified is False
    assert [c.observed for c in res.checks] == ["(not found)", "(not found)"]
    assert res.items == []


def test_container_fields(r, t, v):
    area = t.seed_area("Work")
    p = t.seed_project("P", area=area)
    in_p = t.seed_todo("in p", project=p)
    in_a = t.seed_todo("in a", area=area)
    res = v.check([Expect(in_p, "project_id", "eq", p), Expect(in_p, "area_id", "eq", area),   # via the project
                   Expect(in_a, "area_id", "eq", area), Expect(in_a, "project_id", "none", None),
                   Expect(p, "area_id", "eq", area)], launched=False)
    assert res.verified is True


def test_membership_checks(r, t, v):
    sd = t.seed_todo("later", when="someday")
    res = v.check([Expect(sd, "member", "member", "someday"), Expect(sd, "member", "not_member", "anytime"),
                   Expect(sd, "start_date", "none", None)], launched=False)
    assert res.verified is True
    assert r.calls("MEMBERSHIP")[-1].args == ("anytime", "someday")
    assert res.checks[0].expected == "in someday" and res.checks[0].observed == "someday"
    assert res.checks[1].expected == "not in anytime"
    res = v.check([Expect(sd, "member", "member", "anytime")], launched=False)
    assert res.verified is False and res.checks[0].observed == "not in anytime"
    hidden = t.seed_todo("tmpl", repeating=True)
    res = v.check([Expect(hidden, "member", "member", "today"), Expect(hidden, "member", "not_member", "someday")],
                  launched=False)
    assert [c.observed for c in res.checks] == ["not in today, someday"] * 2
    assert [c.ok for c in res.checks] == [False, True]


def test_children_superset(r, t, v):
    p = t.seed_project("P")
    t.seed_todo("a", project=p)
    t.seed_todo("b", project=p)
    assert v.check([Expect(p, "children", "superset", ["A", "b"])], launched=False).verified is True
    res = v.check([Expect(p, "children", "superset", ["a", "c"])], launched=False)
    assert res.verified is False and res.checks[0].observed == "a, b"
    assert r.calls("SCAN")[-1].args == ("project", p, "0", "0")


def test_status_eq(r, t, v):
    tid = t.seed_todo("x", status="completed")
    assert v.check([Expect(tid, "status", "eq", "completed")], launched=False).verified is True
    assert v.check([Expect(tid, "status", "eq", "open")], launched=False).verified is False


# ---- expectation mapping (§2.8 table) --------------------------------------------------------------------

def mapping(patch, resolved=None, before=None):
    return expectations_for_todo_patch(TID, patch, resolved or {}, before, TODAY)


def ops(expects):
    return [(e.field, e.op, e.value) for e in expects]


def test_mapping_title_and_notes():
    ex, unv = mapping(TodoPatch(title="New", notes="a\r\nb"))
    assert ops(ex) == [("title", "eq", "New"), ("notes", "eq", "a\nb")] and unv == []
    ex, _ = mapping(TodoPatch(notes=""))
    assert ops(ex) == [("notes", "none", None)]
    ex, _ = mapping(TodoPatch(prepend_notes="P0 ", append_notes=" Z9"))
    assert ops(ex) == [("notes", "startswith", "P0 "), ("notes", "endswith", " Z9")]


@pytest.mark.parametrize("when, expected, unverifiable", [
    ("today", [("start_date", "eq", "2026-09-28")], []),
    ("evening", [("start_date", "eq", "2026-09-28")], ["evening"]),
    ("evening@6:00pm", [("start_date", "eq", "2026-09-28")], ["reminder time", "evening"]),
    ("tomorrow", [("start_date", "eq", "2026-09-29")], []),
    ("2026-10-02@9:30am", [("start_date", "eq", "2026-10-02")], ["reminder time"]),
    ("someday", [("start_date", "none", None), ("member", "member", "someday")], []),
    ("anytime", [("start_date", "none", None), ("member", "member", "anytime")], []),
])
def test_mapping_when(when, expected, unverifiable):
    ex, unv = mapping(TodoPatch(when="x"), {"when": when})
    assert ops(ex) == expected and unv == unverifiable
    assert when_expects(TID, when, TODAY) == (ex, unverifiable)


def test_mapping_deadline():
    assert ops(mapping(TodoPatch(deadline="today"))[0]) == [("deadline", "eq", "2026-09-28")]
    assert ops(mapping(TodoPatch(deadline="x"), {"deadline": "tomorrow"})[0]) == [("deadline", "eq", "2026-09-29")]
    assert ops(mapping(TodoPatch(deadline="2025-01-01"))[0]) == [("deadline", "eq", "2025-01-01")]
    assert ops(mapping(TodoPatch(clear_deadline=True))[0]) == [("deadline", "none", None)]
    assert deadline_expect(TID, "2026-12-31", TODAY) == Expect(TID, "deadline", "eq", "2026-12-31")


def test_mapping_tags_use_the_resolved_canonical_values():
    assert ops(mapping(TodoPatch(tags=["home"]), {"tags": ["Home"]})[0]) == [("tags", "eq", ["Home"])]
    assert ops(mapping(TodoPatch(tags=[]), {"tags": []})[0]) == [("tags", "eq", [])]
    assert mapping(TodoPatch(tags=["nope"]), {"tags": None})[0] == []          # not sent: nothing to check
    assert ops(mapping(TodoPatch(add_tags=["x"]), {"add_tags": ["X"]})[0]) == [("tags", "superset", ["X"])]
    assert mapping(TodoPatch(add_tags=["x"]), {"add_tags": None})[0] == []


def test_mapping_unverifiable_fields():
    ex, unv = mapping(TodoPatch(checklist_append=["a"], checklist_prepend=["b"], heading="H",
                                project="P"), {"project_id": "Pr0jectId12345678901"})
    assert ops(ex) == [("project_id", "eq", "Pr0jectId12345678901")]
    assert unv == ["checklist", "heading"]


def test_mapping_moves():
    ex, _ = mapping(TodoPatch(area="A"), {"area_id": "AreaId12345678901234"}, item())
    assert ops(ex) == [("area_id", "eq", "AreaId12345678901234"), ("project_id", "none", None)]
    ex, _ = mapping(ProjectPatch(area="A"), {"area_id": "AreaId12345678901234"}, item(kind="project"))
    assert ops(ex) == [("area_id", "eq", "AreaId12345678901234")]


def test_mapping_project_patch_has_no_checklist():
    ex, unv = mapping(ProjectPatch(title="P", when="x", add_tags=["a"]), {"when": "someday", "add_tags": ["A"]})
    assert ops(ex) == [("title", "eq", "P"), ("start_date", "none", None), ("member", "member", "someday"),
                       ("tags", "superset", ["A"])]
    assert unv == []


def test_mapping_empty_patch():
    assert mapping(TodoPatch()) == ([], [])


# ---- created items -----------------------------------------------------------------------------------------

def test_find_created_scoped_poll_and_youngest_first(r, t, v, clock):
    p = t.seed_project("P")
    t0 = clock.wall()
    old = t.seed_todo("Call", project=p, age=3600)                 # too old to be ours
    new = t.seed_todo("Call", project=p, age=0)
    found = v.find_created(kind="to-do", titles=["Call"], scope=("project", p), t0=t0, launched=False)
    assert found == {"Call": [new]} and old not in found["Call"]
    call = r.calls("FIND_CREATED")[0]
    assert call.args == ("to-do", "Call", "5", "project", p, "0")
    assert clock.sleeps == [0.3] and v.warnings == [] and v.error is None
    no_writes(r)


def test_find_created_takes_the_youngest_and_warns(r, t, v, clock):
    t0 = clock.wall()
    older = t.seed_todo("Dup", age=2)
    younger = t.seed_todo("Dup", age=0)
    found = v.find_created(kind="to-do", titles=["Dup"], scope=("list", "inbox"), t0=t0 - 3, launched=False)
    assert found == {"Dup": [younger]}
    assert len(v.warnings) == 1 and "2 items titled 'Dup'" in v.warnings[0]
    both = v.find_created(kind="to-do", titles=["Dup", "Dup"], scope=("list", "inbox"), t0=t0 - 3, launched=False)
    assert both == {"Dup": [younger, older]} and v.warnings == []


def test_find_created_is_case_exact(r, t, v, clock):
    t.seed_todo("call mom", age=0)
    found = v.find_created(kind="to-do", titles=["Call Mom"], scope=("list", "inbox"), t0=clock.wall(), launched=False)
    assert found == {"Call Mom": []}


def test_find_created_final_poll_is_app_wide_with_diag(r, t, v, clock):
    found = v.find_created(kind="to-do", titles=["Nope"], scope=("list", "today"), t0=clock.wall(), launched=False)
    assert found == {"Nope": []}
    calls = r.calls("FIND_CREATED")
    assert sum(clock.sleeps) == pytest.approx(6.0)
    assert [c.args[3:] for c in calls[:-1]] == [("list", "today", "0")] * (len(calls) - 1)
    assert calls[-1].args[3:] == ("app", "", "1")
    assert v.diagnosis is None                      # no dialog: count to dos isn't 0 with an Inbox > 0


def test_find_created_max_age_grows_from_t0(r, t, v, clock):
    t0 = clock.wall() - 10                           # dispatched 10 s ago: budget already down to 0
    v.find_created(kind="to-do", titles=["Nope"], scope=("list", "inbox"), t0=t0, launched=False)
    calls = r.calls("FIND_CREATED")
    assert len(calls) == 1 and calls[0].args[2] == "15" and calls[0].args[3:] == ("app", "", "1")
    assert clock.sleeps == [0.3]


def test_find_created_budget_is_shared_from_t0(r, t, v, clock):
    t0 = clock.wall() - 4.0
    v.find_created(kind="to-do", titles=["Nope"], scope=("list", "inbox"), t0=t0, launched=False)
    assert sum(clock.sleeps) == pytest.approx(2.0)


def test_find_created_sheet_diagnosis(r, t, v, clock):
    t.seed_todo("waiting in inbox")
    r.sheet_mode = True                              # top-level `to dos` read empty behind a sheet (MODALX1)
    found = v.find_created(kind="to-do", titles=["New"], scope=("list", "inbox"), t0=clock.wall(), launched=False)
    assert found == {"New": []}
    assert v.diagnosis == DIALOG_REASON
    assert dialog_reason().startswith(DIALOG_REASON) and NOTHING_RESENT in dialog_reason()


def test_find_created_projects_are_app_level(r, t, v, clock):
    pid = t.seed_project("Build", age=0)
    found = v.find_created(kind="project", titles=["Build"], scope=("app", ""), t0=clock.wall(), launched=False)
    assert found == {"Build": [pid]}


def test_find_created_multiple_titles_poll_only_whats_missing(r, t, v, clock):
    a = t.seed_todo("A", age=0)
    r.url_delay_s = 1.0
    r.dispatch(u.build_add(title="B"))
    found = v.find_created(kind="to-do", titles=["A", "B"], scope=("list", "inbox"), t0=clock.wall(), launched=False)
    assert found["A"] == [a] and len(found["B"]) == 1
    titles = [c.args[1] for c in r.calls("FIND_CREATED")]
    assert titles[:2] == ["A", "B"] and set(titles[2:]) == {"B"}


def test_find_created_read_error(r, t, v, clock):
    r.inject("FIND_CREATED", exc=AutomationDenied("Not allowed to control Things."))
    found = v.find_created(kind="to-do", titles=["A"], scope=("list", "inbox"), t0=clock.wall(), launched=False)
    assert found == {"A": []}
    assert isinstance(v.error, AutomationDenied)
    assert len(r.calls("FIND_CREATED")) == 1


# ---- reasons ----------------------------------------------------------------------------------------------

def test_not_verified_reason_text():
    text = not_verified_reason(6)
    assert text.startswith("Things didn't apply the change within 6 s. Likely causes:\n")
    assert "(1) 'Enable Things URLs' is off. Things shows an Enable prompt and holds the command, which will " \
           "still apply if you click Enable." in text
    assert "(2) The auth token is wrong or stale; Things shows an error in its window. Re-run " \
           "`things-bridge token set`." in text
    assert "(3) A dialog is open in Things." in text
    assert "(4) It's a repeating item." in text
    assert text.endswith("\n\nNothing was re-sent; check Things before retrying.")
    creates = not_verified_reason(15, update=False)
    assert "auth token" not in creates and "repeating" not in creates
    assert "(2) A dialog is open in Things." in creates
