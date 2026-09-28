import dataclasses
import json
import subprocess
import sys
import types
import typing

import pytest

import things_bridge
from things_bridge import models
from things_bridge.models import (
    NOTES_PREVIEW_CHARS,
    UNREADABLE_FIELDS,
    Area,
    AreaList,
    Check,
    DoctorCheck,
    DoctorReport,
    GetResult,
    Item,
    NavResult,
    Page,
    ProjectItemSpec,
    ProjectPatch,
    ProjectSpec,
    Ref,
    SelftestReport,
    SelftestStep,
    Tag,
    TagList,
    TodoPatch,
    TodoSpec,
    WriteResult,
    link_for,
)

ID = "SyJEz273ceSkabUbciM73A"
PID = "Jvj7EW1fLoScPhaw2JomCT"
AID = "Lg8UqVPXo2SbJNiBpDBBQ"

# §2.2 field order, verbatim. Other stages construct these positionally and by keyword.
FIELDS = {
    Ref: ["id", "title"],
    Item: ["id", "kind", "title", "status", "tags", "deadline", "start_date", "project", "area", "notes",
           "notes_truncated", "created", "modified", "completed_at", "canceled_at", "link"],
    Area: ["id", "title", "tags", "link"],
    Tag: ["id", "title", "parent", "shortcut"],
    Page: ["items", "returned", "total", "offset", "truncated", "note", "unreadable_fields"],
    GetResult: ["kind", "item", "area", "children"],
    AreaList: ["items", "returned"],
    TagList: ["items", "returned"],
    Check: ["target_id", "field", "expected", "observed", "ok"],
    WriteResult: ["action", "sent", "dry_run", "verified", "ids", "items", "before", "checks", "unverifiable",
                  "warnings", "urls", "links", "launched_things", "reason"],
    NavResult: ["sent", "url"],
    DoctorCheck: ["name", "status", "detail", "remedy"],
    DoctorReport: ["ok", "checks", "versions", "host"],
    SelftestStep: ["name", "status", "detail", "ids"],
    SelftestReport: ["ok", "project_id", "created_ids", "steps", "cleanup_note"],
    TodoSpec: ["title", "notes", "when", "reminder_time", "deadline", "tags", "checklist", "project", "area",
               "heading"],
    ProjectItemSpec: ["kind", "title", "notes", "when", "deadline", "tags", "checklist"],
    ProjectSpec: ["title", "notes", "when", "reminder_time", "deadline", "tags", "area", "items"],
    TodoPatch: ["title", "notes", "prepend_notes", "append_notes", "when", "reminder_time", "deadline",
                "clear_deadline", "tags", "add_tags", "checklist_append", "checklist_prepend", "project", "area",
                "heading"],
    ProjectPatch: ["title", "notes", "prepend_notes", "append_notes", "when", "reminder_time", "deadline",
                   "clear_deadline", "tags", "add_tags", "area"],
}

# Fields that have defaults (everything else is required).
DEFAULTED = {
    Page: {"note", "unreadable_fields"},
    WriteResult: {"reason"},
    TodoSpec: set(FIELDS[TodoSpec]) - {"title"},
    ProjectItemSpec: set(FIELDS[ProjectItemSpec]) - {"kind", "title"},
    ProjectSpec: set(FIELDS[ProjectSpec]) - {"title"},
    TodoPatch: set(FIELDS[TodoPatch]),
    ProjectPatch: set(FIELDS[ProjectPatch]),
}

RESULT_TYPES = [Ref, Item, Area, Tag, Page, GetResult, AreaList, TagList, Check, WriteResult, NavResult,
                DoctorCheck, DoctorReport, SelftestStep, SelftestReport]


def make_item(**kw) -> Item:
    base = dict(
        id=ID, kind="to-do", title="Buy milk ✓", status="open", tags=["Errand", "Home"],
        deadline="2026-10-02", start_date="2026-09-28",
        project=Ref(PID, "Groceries"), area=Ref(AID, "Family"),
        notes="line1\r\nline2 🙂", notes_truncated=False,
        created="2026-09-27T10:00:00+02:00", modified="2026-09-28T08:30:00+02:00",
        completed_at=None, canceled_at=None, link="link:" + ID,
    )
    base.update(kw)
    return Item(**base)


def make_write_result(**kw) -> WriteResult:
    item = make_item()
    base = dict(
        action="update_todo", sent=True, dry_run=False, verified=True, ids=[ID], items=[item],
        before=[make_item(title="Buy mlk")],
        checks=[Check(ID, "title", "Buy milk ✓", "Buy milk ✓", True)],
        unverifiable=["reminder time"], warnings=["Tag 'X' doesn't exist in Things"],
        urls=["redacted-url&auth-token=***"], links=["link:" + ID], launched_things=False,
    )
    base.update(kw)
    return WriteResult(**base)


# ---- constants and aliases ---------------------------------------------------------------------

def test_constants():
    assert NOTES_PREVIEW_CHARS == 200
    assert UNREADABLE_FIELDS == ("checklist items", "headings", "This Evening vs Today", "reminder time",
                                 "repeat rule", "inherited tags")
    assert isinstance(UNREADABLE_FIELDS, tuple)


def test_literal_aliases():
    assert typing.get_args(models.ItemKind) == ("to-do", "project")
    assert typing.get_args(models.Status) == ("open", "completed", "canceled")
    assert typing.get_args(models.ListKey) == ("inbox", "today", "anytime", "upcoming", "someday")
    assert typing.get_args(models.NotesMode) == ("none", "preview", "full")


# ---- shape -------------------------------------------------------------------------------------

@pytest.mark.parametrize("cls", list(FIELDS), ids=lambda c: c.__name__)
def test_field_names_and_order(cls):
    assert dataclasses.is_dataclass(cls)
    assert [f.name for f in dataclasses.fields(cls)] == FIELDS[cls]


@pytest.mark.parametrize("cls", list(FIELDS), ids=lambda c: c.__name__)
def test_required_vs_defaulted(cls):
    defaulted = {f.name for f in dataclasses.fields(cls)
                 if f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING}
    assert defaulted == DEFAULTED.get(cls, set())


@pytest.mark.parametrize("cls", list(FIELDS), ids=lambda c: c.__name__)
def test_type_hints_resolve(cls):
    # pydantic (mcp outputSchema) and the CLI both rely on resolvable runtime annotations.
    hints = typing.get_type_hints(cls)
    assert list(hints) == FIELDS[cls]


def test_key_type_hints():
    h = typing.get_type_hints(Item)
    assert h["kind"] == models.ItemKind
    assert h["status"] == models.Status
    assert h["tags"] == list[str]
    assert h["project"] == typing.Optional[Ref]
    assert h["deadline"] == typing.Optional[str]
    assert typing.get_type_hints(GetResult)["kind"] == typing.Literal["to-do", "project", "area"]
    assert typing.get_type_hints(DoctorCheck)["status"] == typing.Literal["ok", "warn", "fail", "skip"]
    assert typing.get_type_hints(SelftestStep)["status"] == typing.Literal["pass", "fail", "skip", "info"]
    assert typing.get_type_hints(ProjectItemSpec)["kind"] == typing.Literal["todo", "heading"]
    assert typing.get_type_hints(ProjectSpec)["items"] == typing.Optional[list[ProjectItemSpec]]
    assert typing.get_type_hints(WriteResult)["verified"] == typing.Optional[bool]
    assert typing.get_type_hints(TodoPatch)["clear_deadline"] is bool


def test_project_patch_is_todo_patch_minus_todo_only_fields():
    todo_only = {"checklist_append", "checklist_prepend", "project", "heading"}
    assert FIELDS[ProjectPatch] == [f for f in FIELDS[TodoPatch] if f not in todo_only]
    th, ph = typing.get_type_hints(TodoPatch), typing.get_type_hints(ProjectPatch)
    for name in FIELDS[ProjectPatch]:
        assert ph[name] == th[name], name


# ---- construction and defaults -----------------------------------------------------------------

def test_page_defaults():
    p = Page(items=[], returned=0, total=0, offset=0, truncated=False)
    assert p.note is None
    assert p.unreadable_fields == list(UNREADABLE_FIELDS)
    assert isinstance(p.unreadable_fields, list)


def test_page_unreadable_fields_not_shared():
    a = Page(items=[], returned=0, total=None, offset=0, truncated=False)
    b = Page(items=[], returned=0, total=None, offset=0, truncated=False)
    a.unreadable_fields.append("x")
    assert b.unreadable_fields == list(UNREADABLE_FIELDS)
    assert "x" not in UNREADABLE_FIELDS


def test_write_result_reason_default():
    assert make_write_result().reason is None
    r = make_write_result(verified=False, reason="Things didn't apply the change within 6 s.")
    assert r.verified is False and r.reason.startswith("Things didn't")


def test_patch_defaults_mean_unchanged():
    for patch in (TodoPatch(), ProjectPatch()):
        for f in dataclasses.fields(patch):
            expected = False if f.name == "clear_deadline" else None
            assert getattr(patch, f.name) is expected, f.name


def test_specs_require_title():
    with pytest.raises(TypeError):
        TodoSpec()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        ProjectSpec()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        ProjectItemSpec(kind="todo")  # type: ignore[call-arg]
    s = TodoSpec("Call mum")
    assert s.title == "Call mum" and s.tags is None and s.checklist is None


def test_result_types_require_all_fields():
    with pytest.raises(TypeError):
        Item(id=ID, kind="to-do", title="x", status="open")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        WriteResult(action="add_todo", sent=True, dry_run=False)  # type: ignore[call-arg]


def test_positional_construction():
    assert Ref(ID, "t") == Ref(id=ID, title="t")
    assert Check(ID, "status", "completed", "open", False).field == "status"
    assert NavResult(True, "u") == NavResult(sent=True, url="u")


def test_project_spec_with_items():
    spec = ProjectSpec(
        title="Build treehouse", when="today", area=AID, tags=["Weekend"],
        items=[ProjectItemSpec("todo", "Buy wood", checklist=["planks", "nails"]),
               ProjectItemSpec("heading", "Phase 2"),
               ProjectItemSpec("todo", "Paint", deadline="2026-10-10")],
    )
    assert [i.kind for i in spec.items] == ["todo", "heading", "todo"]
    assert spec.items[0].checklist == ["planks", "nails"]
    assert spec.reminder_time is None


def test_equality_and_mutability():
    a, b = make_item(), make_item()
    assert a == b
    b.tags.append("Extra")
    assert a != b
    assert a.tags == ["Errand", "Home"]
    a.title = "changed"          # plain (non-frozen) dataclasses
    assert a.title == "changed"


# ---- serialisation -----------------------------------------------------------------------------

def test_item_asdict_nested_refs():
    d = dataclasses.asdict(make_item())
    assert d["project"] == {"id": PID, "title": "Groceries"}
    assert d["area"] == {"id": AID, "title": "Family"}
    assert list(d) == FIELDS[Item]


def test_item_asdict_manual_round_trip():
    item = make_item(project=None)
    d = dataclasses.asdict(item)
    rebuilt = Item(**{**d, "project": None, "area": Ref(**d["area"])})
    assert rebuilt == item


def _json_round_trip(obj):
    d = dataclasses.asdict(obj)
    return d, json.loads(json.dumps(d, ensure_ascii=False))


def test_write_result_json_round_trip():
    d, back = _json_round_trip(make_write_result())
    assert back == d
    assert back["items"][0]["notes"] == "line1\r\nline2 🙂"      # \r preserved
    assert back["checks"][0] == {"target_id": ID, "field": "title", "expected": "Buy milk ✓",
                                 "observed": "Buy milk ✓", "ok": True}


def test_every_result_type_is_json_serialisable():
    samples = [
        Ref(ID, "t"),
        make_item(),
        Area(AID, "Family", ["Home"], "link:" + AID),
        Tag("tagid001", "Errand", Ref("tagid000", "Parent"), "e"),
        Page(items=[make_item()], returned=1, total=3, offset=0, truncated=True, note="more"),
        GetResult("project", make_item(kind="project"), None,
                  Page(items=[], returned=0, total=0, offset=0, truncated=False)),
        AreaList([Area(AID, "Family", [], "l")], 1),
        TagList([Tag("tagid001", "Errand", None, None)], 1),
        Check(ID, "deadline", None, None, True),
        make_write_result(),
        NavResult(False, "u"),
        DoctorCheck("platform", "fail", "linux", "use a Mac"),
        DoctorReport(False, [DoctorCheck("platform", "fail", "linux", None)], {"python": "3.11"},
                     {"executable": "/usr/bin/python3"}),
        SelftestStep("ST-1", "pass", "created", [PID]),
        SelftestReport(True, PID, [PID, ID], [SelftestStep("ST-0", "info", "", [])], "delete by hand"),
    ]
    assert {type(s) for s in samples} == set(RESULT_TYPES)
    for s in samples:
        d, back = _json_round_trip(s)
        assert back == d, type(s).__name__


def test_pydantic_round_trip_and_schema():
    # mcp 2.x derives outputSchema/structuredContent from these dataclasses via pydantic.
    pydantic = pytest.importorskip("pydantic")
    for obj in (make_item(), make_write_result(),
                Page(items=[make_item()], returned=1, total=None, offset=0, truncated=False),
                GetResult("area", None, Area(AID, "Family", [], "l"), None),
                DoctorReport(True, [], {}, {}),
                ProjectSpec("P", items=[ProjectItemSpec("heading", "H")]),
                TodoPatch(tags=[], clear_deadline=True)):
        ta = pydantic.TypeAdapter(type(obj))
        assert ta.validate_python(dataclasses.asdict(obj)) == obj
        assert ta.validate_json(json.dumps(dataclasses.asdict(obj))) == obj
    for cls in RESULT_TYPES:
        schema = pydantic.TypeAdapter(cls).json_schema()
        assert schema["type"] == "object"
        assert set(schema["properties"]) == set(FIELDS[cls])
    item_schema = pydantic.TypeAdapter(Item).json_schema()
    assert item_schema["properties"]["kind"]["enum"] == ["to-do", "project"]
    assert item_schema["properties"]["status"]["enum"] == ["open", "completed", "canceled"]


def test_pydantic_rejects_bad_literal():
    pydantic = pytest.importorskip("pydantic")
    d = dataclasses.asdict(make_item())
    d["status"] = "deleted"
    with pytest.raises(pydantic.ValidationError):
        pydantic.TypeAdapter(Item).validate_python(d)


# ---- link_for ----------------------------------------------------------------------------------

def test_models_import_does_not_import_urlscheme():
    code = ("import sys, things_bridge.models as m; "
            "print(int('things_bridge.urlscheme' in sys.modules), int(callable(m.link_for)))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.split() == ["0", "1"]


def test_link_for_defers_to_urlscheme_build_show(monkeypatch):
    calls = []

    class _URL:
        def __init__(self, url):
            self.url = url

    def build_show(**kw):
        calls.append(kw)
        return _URL("fake-show-url?id=" + kw["id"])

    fake = types.ModuleType("things_bridge.urlscheme")
    fake.build_show = build_show
    monkeypatch.setitem(sys.modules, "things_bridge.urlscheme", fake)
    monkeypatch.setattr(things_bridge, "urlscheme", fake, raising=False)

    assert link_for(ID) == "fake-show-url?id=" + ID
    assert calls == [{"id": ID}]


def test_link_for_propagates_builder_errors(monkeypatch):
    from things_bridge.errors import InvalidInput

    def build_show(**kw):
        raise InvalidInput("bad id")

    fake = types.ModuleType("things_bridge.urlscheme")
    fake.build_show = build_show
    monkeypatch.setitem(sys.modules, "things_bridge.urlscheme", fake)
    monkeypatch.setattr(things_bridge, "urlscheme", fake, raising=False)
    with pytest.raises(InvalidInput):
        link_for("not an id")


def test_link_for_real_urlscheme():
    try:
        from things_bridge import urlscheme
    except Exception:  # urlscheme is written in a later stage
        pytest.skip("urlscheme not available yet")
    if not hasattr(urlscheme, "build_show"):
        pytest.skip("urlscheme.build_show not available yet")
    assert link_for(ID) == urlscheme.SCHEME_PREFIX + "show?id=" + ID
    assert link_for(ID) == urlscheme.build_show(id=ID).url
