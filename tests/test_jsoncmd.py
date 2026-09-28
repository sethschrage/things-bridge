import json
import os
import re
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

import pytest

from things_bridge import jsoncmd as jc
from things_bridge import urlscheme as us
from things_bridge.errors import InvalidInput
from things_bridge.jsoncmd import (
    ChecklistItem,
    Heading,
    Project,
    Todo,
    dumps,
    from_obj,
    from_objs,
    has_update,
    to_obj,
    validate,
    weight,
)

FIXTURE = Path(__file__).parent / "fixtures" / "official_json_example.json"
# OFF-URL "URL Encoded Example", verbatim (it leaves ':' and ',' bare and has no "operation").
OFFICIAL_ENCODED = ("things:///json?data=%5B%7B%22type%22:%22to-do%22,%22attributes%22:%7B%22title%22:"
                    "%22Buy%20milk%22%7D%7D%5D")
TID = "SyJEz273ceSkabUbciM73A"
PID = "Jvj7EW1fLoScPhaw2JomCT"
AREA = "Lg8UqVPXo2SbJNiBpDBBQ"
HID = "TryhwrjdiHEXfjgNtw81yt"
UTC = timezone.utc
DT = datetime(2026, 3, 10, 14, 30, tzinfo=UTC)


def drop_create(obj):
    """Remove ``"operation": "create"`` everywhere (Things decodes a missing operation as create)."""
    if isinstance(obj, list):
        return [drop_create(x) for x in obj]
    if isinstance(obj, dict):
        return {k: drop_create(v) for k, v in obj.items() if not (k == "operation" and v == "create")}
    return obj


def official_items():
    """The OFF-URL "JSON Example", written with the typed builders."""
    return [
        Project(title="Go Shopping", items=[Todo(title="Bread"), Todo(title="Milk")]),
        Project(title="Vacation in Rome", notes="Some time in August.", area="Family", items=[
            Todo(title="Ask Sarah for travel guide"),
            Todo(title="Add dates to calendar"),
            Heading(title="Sights"),
            Todo(title="Vatican City"),
            Todo(title="The Colosseum", notes="12€"),
            Heading(title="Planning"),
            Todo(title="Call Paolo", completed=True),
            Todo(title="Book flights", when="today"),
            Todo(title="Research", checklist_items=[ChecklistItem("Hotels", completed=True),
                                                    ChecklistItem("Transport from airport")]),
        ]),
        Todo(title="Pick up dry cleaning", when="evening", tags=["Errand"]),
        Todo(title="Submit report", deadline="2026-02-01", list="Work"),
    ]


def data_of(url: str):
    assert url.startswith("things:///json?data=")
    return json.loads(unquote(url.partition("data=")[2].partition("&")[0]))


# ---- the official examples ------------------------------------------------------------------------

def test_builders_reproduce_the_official_json_example():
    official = json.loads(FIXTURE.read_text(encoding="utf-8"))
    items = official_items()
    validate(items)                                     # strict: the official example is canonical
    assert drop_create(json.loads(dumps(items))) == official


def test_official_example_as_url():
    items = official_items()
    turl = us.build_json(items)
    assert turl.command == "json" and turl.params[0][0] == "data"
    assert data_of(turl.url) == json.loads(dumps(items))
    assert turl.weight == 15                            # 2 projects + 11 to-dos + 2 headings
    assert turl.requires_token is False
    assert "12%E2%82%AC" in turl.url                    # UTF-8, not \u escapes
    assert "+" not in turl.url and " " not in turl.url


def test_url_encoded_example_unquote_equality():
    ours = us.build_json([Todo(title="Buy milk")]).url
    assert ours == ("things:///json?data=%5B%7B%22type%22%3A%22to-do%22%2C%22operation%22%3A%22create%22%2C"
                    "%22attributes%22%3A%7B%22title%22%3A%22Buy%20milk%22%7D%7D%5D")
    payload = drop_create(data_of(ours))
    ours_without_operation = "things:///json?data=" + us.enc(json.dumps(payload, separators=(",", ":"),
                                                                        ensure_ascii=False))
    assert unquote(ours_without_operation) == unquote(OFFICIAL_ENCODED)
    assert data_of(OFFICIAL_ENCODED) == payload


def test_from_objs_parses_the_official_example_and_round_trips():
    official = json.loads(FIXTURE.read_text(encoding="utf-8"))
    parsed = from_objs(official)
    assert parsed == official_items()
    assert drop_create(json.loads(dumps(parsed))) == official


def test_official_update_example():
    obj = {"type": "to-do", "operation": "update", "id": "Di9deEJeUkVZaDEdbnzQZw",
           "attributes": {"deadline": "today"}}
    item = from_obj(obj)
    assert item == Todo(operation="update", id="Di9deEJeUkVZaDEdbnzQZw", deadline="today")
    assert to_obj(item) == obj
    assert list(to_obj(item)) == ["type", "operation", "id", "attributes"]
    turl = us.build_json([item])
    assert turl.requires_token is True and has_update([item])


# ---- TJC conventions -------------------------------------------------------------------------------

def _full_todo():
    return Todo(operation="update", id=TID, title="t", notes="n", prepend_notes="p", append_notes="a", when="today",
                deadline="today", tags=["A"], add_tags=["B"], checklist_items=[ChecklistItem("c")],
                prepend_checklist_items=[ChecklistItem("p")], append_checklist_items=[ChecklistItem("a")],
                list_id=PID, list="L", heading_id=HID, heading="H", completed=False, canceled=False,
                creation_date=DT, completion_date=DT)


def _full_project():
    return Project(operation="create", title="t", notes="n", prepend_notes="p", append_notes="a", when="today",
                   deadline="today", tags=["A"], add_tags=["B"], area_id=AREA, area="Family",
                   items=[Todo(title="x"), Heading("h", archived=False)], completed=False, canceled=False,
                   creation_date=DT, completion_date=DT)


TJC_TODO = ["title", "notes", "prepend-notes", "append-notes", "when", "deadline", "tag-ids", "tags", "add-tags",
            "checklist-items", "prepend-checklist-items", "append-checklist-items", "list-id", "list", "heading-id",
            "heading", "completed", "canceled", "creation-date", "completion-date"]
TJC_PROJECT = ["title", "notes", "prepend-notes", "append-notes", "when", "deadline", "tag-ids", "tags", "add-tags",
               "area-id", "area", "items", "completed", "canceled", "creation-date", "completion-date"]


def test_key_constants_follow_tjc_order():
    assert list(jc.TODO_KEYS) == [k for k in TJC_TODO if k != "tag-ids"]
    assert list(jc.PROJECT_KEYS) == [k for k in TJC_PROJECT if k != "tag-ids"]
    assert jc.HEADING_KEYS == ("title", "archived")
    assert jc.CHECKLIST_KEYS == ("title", "completed", "canceled")
    assert jc.UPDATE_ONLY == {"prepend-notes", "append-notes", "add-tags", "prepend-checklist-items",
                              "append-checklist-items"}
    assert jc.MAX_JSON_OBJECTS == 100


def test_dataclass_fields_mirror_the_keys():
    assert [f.name for f in fields(Todo)] == ["operation", "id", *(k.replace("-", "_") for k in jc.TODO_KEYS)]
    assert [f.name for f in fields(Project)] == ["operation", "id", *(k.replace("-", "_") for k in jc.PROJECT_KEYS)]
    assert [f.name for f in fields(Heading)] == list(jc.HEADING_KEYS)
    assert [f.name for f in fields(ChecklistItem)] == list(jc.CHECKLIST_KEYS)
    assert Todo().operation == "create" and Project().operation == "create"


def test_to_obj_key_order_is_tjc():
    todo = to_obj(_full_todo())
    assert list(todo) == ["type", "operation", "id", "attributes"]
    assert list(todo["attributes"]) == list(jc.TODO_KEYS)
    project = to_obj(_full_project())
    assert list(project) == ["type", "operation", "attributes"]      # no id on a create
    assert list(project["attributes"]) == list(jc.PROJECT_KEYS)
    assert to_obj(Heading("Sights", archived=True)) == {
        "type": "heading", "operation": "create", "attributes": {"title": "Sights", "archived": True}}
    assert to_obj(ChecklistItem("Hotels", completed=True, canceled=False)) == {
        "type": "checklist-item", "operation": "create",
        "attributes": {"title": "Hotels", "completed": True, "canceled": False}}


def test_operation_always_emitted_at_every_level():
    def walk(obj):
        assert obj["operation"] in ("create", "update")
        for key in ("items", "checklist-items", "prepend-checklist-items", "append-checklist-items"):
            for child in obj["attributes"].get(key, []):
                walk(child)
    for obj in json.loads(dumps(official_items() + [_full_todo()])):
        walk(obj)


def test_none_attributes_omitted_and_attributes_always_present():
    assert to_obj(Todo()) == {"type": "to-do", "operation": "create", "attributes": {}}
    assert to_obj(Todo(title="x", notes=None, tags=[])) == {
        "type": "to-do", "operation": "create", "attributes": {"title": "x", "tags": []}}


def test_dates_are_utc_seconds():
    plus1 = timezone(timedelta(hours=1))
    todo = Todo(title="x", completed=True, creation_date=datetime(2026, 3, 10, 14, 30, 5, 123456, tzinfo=plus1),
                completion_date=datetime(2026, 3, 11, tzinfo=UTC))
    attrs = to_obj(todo)["attributes"]
    assert attrs["creation-date"] == "2026-03-10T13:30:05Z"
    assert attrs["completion-date"] == "2026-03-11T00:00:00Z"
    for value in attrs["creation-date"], attrs["completion-date"]:
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", value)


def test_dumps_is_compact_utf8_json():
    s = dumps([Todo(title="The Colosseum", notes="12€ \"quoted\"\nline")])
    assert s == ('[{"type":"to-do","operation":"create","attributes":{"title":"The Colosseum",'
                 '"notes":"12€ \\"quoted\\"\\nline"}}]')


def test_to_obj_normalises_newlines():
    attrs = to_obj(Todo(title="x", notes="a\r\nb\rc", tags=["t"]))["attributes"]
    assert attrs["notes"] == "a\nb\nc"


def test_to_obj_rejects_foreign_objects():
    with pytest.raises(TypeError):
        to_obj({"type": "to-do", "attributes": {}})  # type: ignore[arg-type]


def test_weight_and_has_update():
    assert weight(official_items()) == 15
    assert weight([Todo(title="a", checklist_items=[ChecklistItem("c")] * 5)]) == 1   # checklist items don't count
    assert weight([Project(title="p", items=[Heading("h"), Todo(title="t")]), Todo(title="u")]) == 4
    assert has_update(official_items()) is False
    assert has_update([Todo(title="a"), Project(operation="update", id=PID, title="b")]) is True


# ---- validation (§2.4), one failing case per rule --------------------------------------------------

def _p(n):
    return [Todo(title=f"t{i}") for i in range(n)]


BAD = [
    # 1. top level
    ("empty", [], "data", "at least one"),
    ("not-a-list", Todo(title="x"), "data", "expected a list"),
    ("heading-top", [Heading("h")], "/0", "top-level objects must be to-dos or projects"),
    ("checklist-top", [ChecklistItem("c")], "/0", "top-level"),
    ("dict-top", [{"type": "to-do", "attributes": {}}], "/0", "top-level"),
    ("too-many", _p(101), "data", "limit per call is 100"),
    ("too-many-nested", [Project(title="p", items=_p(100))], "data", "101"),
    # 2. operations and ids
    ("update-no-id", [Todo(operation="update", title="x")], "/0/id", "needs the id"),
    ("update-bad-id", [Todo(operation="update", id="not an id", title="x")], "/0/id", "not a Things id"),
    ("update-id-newline", [Todo(operation="update", id=TID + "\n", title="x")], "/0/id", "not a Things id"),
    ("create-with-id", [Todo(id=TID, title="x")], "/0/id", "must not carry an id"),
    ("bad-operation", [Todo(operation="delete", id=TID, title="x")], "/0/operation", "create or update"),
    ("nested-update", [Project(title="p", items=[Todo(operation="update", id=TID, title="x")])],
     "/0/attributes/items/0/operation", "only top-level"),
    ("empty-update", [Todo(operation="update", id=TID)], "/0/attributes", "nothing to update"),
    # 3. items
    ("items-on-update", [Project(operation="update", id=PID, items=[Todo(title="a")])], "/0/attributes/items",
     "only be given when creating"),
    ("nested-project", [Project(title="p", items=[Project(title="q")])], "/0/attributes/items/0",
     "to-dos or headings"),
    ("nested-checklist", [Project(title="p", items=[ChecklistItem("c")])], "/0/attributes/items/0", "to-dos or headings"),
    ("items-not-list", [Project(title="p", items="abc")], "/0/attributes/items", "expected a list"),
    ("nested-list-id", [Project(title="p", items=[Todo(title="a", list_id=PID)])],
     "/0/attributes/items/0/attributes/list-id", "ignored"),
    ("nested-list", [Project(title="p", items=[Todo(title="a", list="Work")])],
     "/0/attributes/items/0/attributes/list", "ignored"),
    ("nested-heading", [Project(title="p", items=[Todo(title="a", heading="H")])],
     "/0/attributes/items/0/attributes/heading", "ignored"),
    ("nested-heading-id", [Project(title="p", items=[Todo(title="a", heading_id=HID)])],
     "/0/attributes/items/0/attributes/heading-id", "ignored"),
    ("heading-blank", [Project(title="p", items=[Heading(" ")])], "/0/attributes/items/0/attributes/title", "blank"),
    ("heading-archived", [Project(title="p", items=[Heading("h", archived="no")])],
     "/0/attributes/items/0/attributes/archived", "true or false"),
    # 4. update-only keys on create
    ("create-prepend-notes", [Todo(title="x", prepend_notes="n")], "/0/attributes/prepend-notes", "only allowed on update"),
    ("create-append-notes", [Project(title="x", append_notes="n")], "/0/attributes/append-notes", "only allowed on update"),
    ("create-add-tags", [Todo(title="x", add_tags=["a"])], "/0/attributes/add-tags", "only allowed on update"),
    ("create-prepend-cl", [Todo(title="x", prepend_checklist_items=[ChecklistItem("a")])],
     "/0/attributes/prepend-checklist-items", "only allowed on update"),
    ("nested-append-cl", [Project(title="p", items=[Todo(title="x", append_checklist_items=[ChecklistItem("a")])])],
     "/0/attributes/items/0/attributes/append-checklist-items", "only allowed on update"),
    # 5. checklists
    ("checklist-101", [Todo(title="x", checklist_items=[ChecklistItem(str(i)) for i in range(101)])],
     "/0/attributes/checklist-items", "101 items"),
    ("checklist-blank", [Todo(title="x", checklist_items=[ChecklistItem(" ")])],
     "/0/attributes/checklist-items/0/attributes/title", "blank"),
    ("checklist-newline", [Todo(title="x", checklist_items=[ChecklistItem("a\nb")])],
     "/0/attributes/checklist-items/0/attributes/title", "single line"),
    ("checklist-strings", [Todo(title="x", checklist_items=["Hotels"])],
     "/0/attributes/checklist-items/0", "checklist-item objects"),
    ("checklist-str", [Todo(title="x", checklist_items="Hotels")], "/0/attributes/checklist-items", "expected a list"),
    ("checklist-both", [Todo(title="x", checklist_items=[ChecklistItem("a", completed=True, canceled=True)])],
     "/0/attributes/checklist-items/0/attributes/canceled", "not both"),
    ("checklist-bool", [Todo(title="x", checklist_items=[ChecklistItem("a", completed="yes")])],
     "/0/attributes/checklist-items/0/attributes/completed", "true or false"),
    ("update-checklist-replace", [Todo(operation="update", id=TID, checklist_items=[ChecklistItem("a")])],
     "/0/attributes/checklist-items", "never does"),
    # 6. field limits, tags, vocabulary
    ("title-long", [Todo(title="a" * 4001)], "/0/attributes/title", "UTF-16"),
    ("title-emoji-long", [Project(title="🙂" * 2001)], "/0/attributes/title", "UTF-16"),
    ("title-newline", [Todo(title="a\nb")], "/0/attributes/title", "single line"),
    ("title-type", [Todo(title=5)], "/0/attributes/title", "expected a string"),
    ("notes-long", [Todo(title="x", notes="n" * 10_001)], "/0/attributes/notes", "10001"),
    ("append-long", [Todo(operation="update", id=TID, append_notes="n" * 10_001)], "/0/attributes/append-notes", "10001"),
    ("notes-nul", [Todo(title="x", notes="a\x00b")], "/0/attributes/notes", "NUL"),
    ("tag-comma", [Todo(title="x", tags=["a,b"])], "/0/attributes/tags/0", "comma"),
    ("tags-str", [Todo(title="x", tags="Errand")], "/0/attributes/tags", "expected a list"),
    ("when-natural", [Todo(title="x", when="next monday")], "/0/attributes/when", "today|tomorrow"),
    ("when-someday-time", [Todo(title="x", when="someday@6pm")], "/0/attributes/when", "reminder time"),
    ("when-bad-date", [Project(title="x", when="2026-02-30")], "/0/attributes/when", "calendar"),
    ("deadline-natural", [Todo(title="x", deadline="December 31")], "/0/attributes/deadline", "YYYY-MM-DD"),
    ("list-id", [Todo(title="x", list_id="Work")], "/0/attributes/list-id", "not a Things id"),
    ("heading-id", [Todo(title="x", list_id=PID, heading_id="Sights")], "/0/attributes/heading-id", "not a Things id"),
    ("area-id", [Project(title="x", area_id="Family")], "/0/attributes/area-id", "not a Things id"),
    ("list-blank", [Todo(title="x", list="")], "/0/attributes/list", "blank"),
    ("area-newline", [Project(title="x", area="A\nB")], "/0/attributes/area", "single line"),
    ("heading-without-project", [Todo(title="x", heading="Sights")], "/0/attributes/heading", "list-id"),
    # 7. booleans
    ("completed-str", [Todo(title="x", completed="true")], "/0/attributes/completed", "true or false"),
    ("canceled-int", [Project(title="x", canceled=1)], "/0/attributes/canceled", "true or false"),
    # mutual exclusions (as §2.3)
    ("list-and-id", [Todo(title="x", list="Work", list_id=PID)], "/0/attributes/list-id", "not both"),
    ("heading-and-id", [Todo(operation="update", id=TID, heading="H", heading_id=HID)], "/0/attributes/heading-id",
     "not both"),
    ("area-and-id", [Project(title="x", area="Family", area_id=AREA)], "/0/attributes/area-id", "not both"),
    ("notes-and-append", [Todo(operation="update", id=TID, notes="a", append_notes="b")],
     "/0/attributes/append-notes", "not both"),
    ("completed-and-canceled", [Todo(title="x", completed=True, canceled=True)], "/0/attributes/canceled", "not both"),
    ("completed-and-uncanceled", [Todo(operation="update", id=TID, completed=True, canceled=False)],
     "/0/attributes/canceled", "not both"),
    # dates are checked even when not strict
    ("naive-date", [Todo(title="x", creation_date=datetime(2026, 1, 1))], "/0/attributes/creation-date", "timezone"),
    ("str-date", [Todo(title="x", creation_date="2026-01-01T00:00:00Z")], "/0/attributes/creation-date", "datetime"),
]


STRICT_VOCABULARY = {"when-natural", "when-bad-date", "deadline-natural"}   # natural language passes when lax
REFUSED_KEY_FIRST = {"naive-date", "str-date"}         # strict refuses the key before looking at the value


@pytest.mark.parametrize("name,items,path,problem", BAD, ids=[b[0] for b in BAD])
def test_validate_rejects(name, items, path, problem):
    for strict in (True,) if name in STRICT_VOCABULARY else (True, False):
        with pytest.raises(InvalidInput) as err:
            validate(items, strict=strict)
        assert err.value.message.startswith(path + ": "), err.value.message
        assert err.value.details == {"field": path}
        if not (strict and name in REFUSED_KEY_FIRST):
            assert problem in err.value.message, err.value.message
    if name in STRICT_VOCABULARY:
        validate(items, strict=False)


STRICT_ONLY = [
    ("creation-date", [Todo(title="x", creation_date=DT)], "/0/attributes/creation-date"),
    ("completion-date", [Todo(title="x", completed=True, completion_date=DT)], "/0/attributes/completion-date"),
    ("add-tags", [Todo(operation="update", id=TID, add_tags=["a"])], "/0/attributes/add-tags"),
    ("project-add-tags", [Project(operation="update", id=PID, add_tags=["a"])], "/0/attributes/add-tags"),
    ("prepend-checklist", [Todo(operation="update", id=TID, prepend_checklist_items=[ChecklistItem("a")])],
     "/0/attributes/prepend-checklist-items"),
    ("append-checklist", [Todo(operation="update", id=TID, append_checklist_items=[ChecklistItem("a")])],
     "/0/attributes/append-checklist-items"),
    ("completed-project-open-todos", [Project(title="p", completed=True, items=[Todo(title="open")])],
     "/0/attributes/completed"),
    ("canceled-project-open-todos", [Project(title="p", canceled=True, items=[
        Todo(title="done", completed=True), Heading("h"), Todo(title="open")])], "/0/attributes/canceled"),
    ("natural-when", [Todo(title="x", when="next monday")], "/0/attributes/when"),
    ("natural-deadline", [Project(title="x", deadline="December 31")], "/0/attributes/deadline"),
]


@pytest.mark.parametrize("items,path", [s[1:] for s in STRICT_ONLY], ids=[s[0] for s in STRICT_ONLY])
def test_strict_refusals(items, path):
    with pytest.raises(InvalidInput) as err:
        validate(items)                                  # strict is the default
    assert err.value.message.startswith(path + ": ")
    validate(items, strict=False)


def test_validate_accepts():
    validate([Todo(operation="update", id=TID, when="anytime")])          # D10: anytime via json update
    validate([Project(operation="update", id=PID, when="anytime", deadline="2026-12-31")])
    validate([Todo(title="x", when="2026-10-02@6:00pm", deadline="tomorrow", tags=["A", "B"])])
    validate([Todo(operation="update", id=TID, list_id=PID, heading="Planning")])       # the service's move
    validate([Todo(operation="update", id=TID, completed=False)])                       # reopen
    validate([Todo(operation="update", id=TID, completed=False, canceled=False)])
    validate([Project(operation="update", id=PID, area_id=AREA)])
    validate([Todo(title="x", list_id=PID, heading_id=HID)])
    validate([Project(title="p", completed=True, items=[Todo(title="a", completed=True),
                                                        Todo(title="b", canceled=True), Heading("h")])])
    validate([Project(title="p", items=[])])
    validate(_p(100))
    validate([Project(title="p", items=_p(99))])
    validate((Todo(title="tuple payloads are fine"),))


def test_validate_first_error_names_its_index():
    with pytest.raises(InvalidInput) as err:
        validate([Todo(title="ok"), Todo(title="ok"), Todo(title="bad", when="soon")])
    assert err.value.message.startswith("/2/attributes/when: ")


# ---- from_obj ------------------------------------------------------------------------------------

FROM_BAD = [
    ("not-dict", "a string", "data", "expected a to-do or project object"),
    ("extra-envelope-key", {"type": "to-do", "attributes": {}, "x-callback": 1}, "/x-callback", "unknown key"),
    ("no-attributes", {"type": "to-do"}, "/attributes", "required"),
    ("attributes-list", {"type": "to-do", "attributes": []}, "/attributes", "expected an object"),
    ("area-type", {"type": "area", "attributes": {"title": "x"}}, "/type", "must be to-do or project"),
    ("heading-top", {"type": "heading", "attributes": {"title": "x"}}, "/type", "must be to-do or project"),
    ("no-type", {"attributes": {}}, "/type", "must be to-do or project, not null"),
    ("bad-op", {"type": "to-do", "operation": "delete", "attributes": {}}, "/operation", "create or update"),
    ("id-int", {"type": "to-do", "id": 123, "attributes": {}}, "/id", "expected a string"),
    ("tag-ids", {"type": "to-do", "attributes": {"tag-ids": ["x"]}}, "/attributes/tag-ids", "not documented"),
    ("todo-project-key", {"type": "to-do", "attributes": {"project": "Work"}}, "/attributes/project",
     "unknown to-do attribute"),
    ("todo-area-key", {"type": "to-do", "attributes": {"area": "Work"}}, "/attributes/area", "unknown to-do attribute"),
    ("project-list-key", {"type": "project", "attributes": {"list": "Work"}}, "/attributes/list",
     "unknown project attribute"),
    ("project-checklist", {"type": "project", "attributes": {"checklist-items": []}}, "/attributes/checklist-items",
     "unknown project attribute"),
    ("slash-key", {"type": "to-do", "attributes": {"a/b~c": 1}}, "/attributes/a~1b~0c", "unknown"),
    ("nested-update", {"type": "project", "attributes": {"items": [
        {"type": "to-do", "operation": "update", "id": TID, "attributes": {"title": "x"}}]}},
     "/attributes/items/0/operation", "only be created"),
    ("nested-id", {"type": "project", "attributes": {"items": [
        {"type": "to-do", "id": TID, "attributes": {"title": "x"}}]}}, "/attributes/items/0/id", "can't carry an id"),
    ("nested-project", {"type": "project", "attributes": {"items": [{"type": "project", "attributes": {}}]}},
     "/attributes/items/0/type", "must be to-do or heading"),
    ("heading-id", {"type": "project", "attributes": {"items": [
        {"type": "heading", "id": TID, "attributes": {"title": "h"}}]}}, "/attributes/items/0/id", "can't carry an id"),
    ("heading-date", {"type": "project", "attributes": {"items": [
        {"type": "heading", "attributes": {"title": "h", "creation-date": "2026-01-01T00:00:00Z"}}]}},
     "/attributes/items/0/attributes/creation-date", "unknown heading attribute"),
    ("heading-untitled", {"type": "project", "attributes": {"items": [{"type": "heading", "attributes": {}}]}},
     "/attributes/items/0/attributes/title", "needs a title"),
    ("checklist-strings", {"type": "to-do", "attributes": {"checklist-items": ["Hotels"]}},
     "/attributes/checklist-items/0", "expected a checklist-item object"),
    ("checklist-string", {"type": "to-do", "attributes": {"checklist-items": "Hotels\nFlights"}},
     "/attributes/checklist-items", "expected an array"),
    ("checklist-update", {"type": "to-do", "attributes": {"checklist-items": [
        {"type": "checklist-item", "operation": "update", "attributes": {"title": "a"}}]}},
     "/attributes/checklist-items/0/operation", "only be created"),
    ("checklist-when", {"type": "to-do", "attributes": {"checklist-items": [
        {"type": "checklist-item", "attributes": {"title": "a", "when": "today"}}]}},
     "/attributes/checklist-items/0/attributes/when", "unknown checklist-item attribute"),
    ("checklist-to-do", {"type": "to-do", "attributes": {"checklist-items": [{"type": "to-do", "attributes": {}}]}},
     "/attributes/checklist-items/0/type", "must be checklist-item"),
    ("title-number", {"type": "to-do", "attributes": {"title": 5}}, "/attributes/title", "expected a string"),
    ("tags-string", {"type": "to-do", "attributes": {"tags": "Errand"}}, "/attributes/tags", "array of strings"),
    ("tags-mixed", {"type": "to-do", "attributes": {"tags": ["a", 1]}}, "/attributes/tags", "array of strings"),
    ("add-tags-string", {"type": "to-do", "operation": "update", "id": TID, "attributes": {"add-tags": "a,b"}},
     "/attributes/add-tags", "array of strings"),
    ("completed-string", {"type": "to-do", "attributes": {"completed": "true"}}, "/attributes/completed",
     "true or false"),
    ("completed-int", {"type": "to-do", "attributes": {"completed": 1}}, "/attributes/completed", "true or false"),
    ("items-object", {"type": "project", "attributes": {"items": {}}}, "/attributes/items", "expected an array"),
    ("date-fraction", {"type": "to-do", "attributes": {"creation-date": "2026-03-10T14:30:00.000Z"}},
     "/attributes/creation-date", "no fractional seconds"),
    ("date-only", {"type": "to-do", "attributes": {"creation-date": "2026-03-10"}}, "/attributes/creation-date",
     "ISO 8601"),
    ("date-impossible", {"type": "to-do", "attributes": {"creation-date": "2026-02-30T14:30:00Z"}},
     "/attributes/creation-date", "not a real date-time"),
    ("date-number", {"type": "to-do", "attributes": {"completion-date": 1700000000}}, "/attributes/completion-date",
     "ISO 8601"),
]


@pytest.mark.parametrize("obj,path,problem", [b[1:] for b in FROM_BAD], ids=[b[0] for b in FROM_BAD])
def test_from_obj_rejects(obj, path, problem):
    with pytest.raises(InvalidInput) as err:
        from_obj(obj)
    assert err.value.message.startswith(path + ": "), err.value.message
    assert problem in err.value.message
    assert err.value.details == {"field": path}


def test_from_obj_checklist_string_hint():
    with pytest.raises(InvalidInput) as err:
        from_obj({"type": "to-do", "attributes": {"checklist-items": ["Hotels"]}})
    assert "checklist-item" in err.value.hint


def test_from_objs_paths_and_type():
    with pytest.raises(InvalidInput) as err:
        from_objs([{"type": "to-do", "attributes": {"title": "ok"}}, {"type": "to-do", "attributes": {"titel": "x"}}])
    assert err.value.message.startswith("/1/attributes/titel: ")
    for bad in ({"type": "to-do", "attributes": {}}, "[]", None):
        with pytest.raises(InvalidInput, match="^data: expected a JSON array"):
            from_objs(bad)
    assert from_objs([]) == []                           # emptiness is validate()'s job


def test_from_obj_nulls_are_absent_and_explicit_create_ok():
    obj = {"type": "to-do", "operation": None, "id": None,
           "attributes": {"title": "x", "notes": None, "checklist-items": [
               {"type": "checklist-item", "operation": "create", "attributes": {"title": "c", "completed": None}}]}}
    assert from_obj(obj) == Todo(title="x", checklist_items=[ChecklistItem("c")])
    assert from_obj({"type": "project", "operation": "create", "attributes": {"items": [
        {"type": "heading", "operation": "create", "attributes": {"title": "h", "archived": False}}]}}) == \
        Project(items=[Heading("h", archived=False)])


def test_from_obj_parses_dates():
    item = from_obj({"type": "to-do", "attributes": {"title": "x", "creation-date": "2026-03-10T14:30:00Z",
                                                     "completion-date": "2026-03-10T14:30:00+01:00"}})
    assert item.creation_date == DT
    assert item.completion_date == datetime(2026, 3, 10, 13, 30, tzinfo=UTC)
    attrs = to_obj(item)["attributes"]
    assert (attrs["creation-date"], attrs["completion-date"]) == ("2026-03-10T14:30:00Z", "2026-03-10T13:30:00Z")


def test_from_obj_leaves_semantics_to_validate():
    item = from_obj({"type": "to-do", "id": TID, "attributes": {"when": "next monday"}})
    assert item == Todo(id=TID, when="next monday")
    with pytest.raises(InvalidInput, match="must not carry an id"):
        validate([item])


def test_to_obj_from_obj_round_trip():
    todo = _full_todo()
    todo.checklist_items = None                          # (not allowed together with prepend/append on update)
    assert from_obj(to_obj(todo)) == todo
    assert from_obj(to_obj(_full_project())) == _full_project()


# ---- build_json -----------------------------------------------------------------------------------

def test_build_json_reveal_and_order():
    turl = us.build_json([Todo(title="x")], reveal=True)
    assert [k for k, _ in turl.params] == ["data", "reveal"]
    assert turl.url.endswith("&reveal=true")
    assert us.build_json([Todo(title="x")], reveal=False).url.endswith("&reveal=false")
    with pytest.raises(InvalidInput, match="reveal"):
        us.build_json([Todo(title="x")], reveal="yes")  # type: ignore[arg-type]


def test_build_json_validates_strict_by_default():
    with pytest.raises(InvalidInput, match="creation-date"):
        us.build_json([Todo(title="x", creation_date=DT)])
    turl = us.build_json([Todo(title="x", creation_date=DT)], strict=False)
    assert data_of(turl.url)[0]["attributes"]["creation-date"] == "2026-03-10T14:30:00Z"
    with pytest.raises(InvalidInput):
        us.build_json([])
    with pytest.raises(InvalidInput, match="^/0: "):
        us.build_json([{"type": "to-do", "attributes": {"title": "x"}}])  # type: ignore[list-item]


def test_build_json_token_iff_update():
    assert us.build_json([Todo(title="a"), Project(title="b")]).requires_token is False
    mixed = us.build_json([Todo(title="a"), Todo(operation="update", id=TID, completed=True)])
    assert mixed.requires_token is True
    assert mixed.weight == 2 and mixed.mutating and not mixed.foreground
    assert "auth-token" not in mixed.url


def test_build_json_url_size_limit():
    items = [Todo(title=f"t{i}", notes="é" * 10_000) for i in range(100)]     # ~6 MB encoded
    with pytest.raises(InvalidInput, match="limit is 524288"):
        us.build_json(items)


# ---- optional cross-check against the TJC source ------------------------------------------------

@pytest.mark.skipif(not os.environ.get("TB_TJC_SWIFT"),
                    reason="set TB_TJC_SWIFT to ThingsJSON.swift from github.com/culturedcode/ThingsJSONCoder")
def test_key_order_matches_tjc_source():
    src = Path(os.environ["TB_TJC_SWIFT"]).read_text(encoding="utf-8")
    blocks = re.findall(r"private enum CodingKeys: String, CodingKey \{(.*?)\}", src, re.S)
    orders = [[q or n for n, q in re.findall(r'case (\w+)(?: = "([^"]+)")?', b)] for b in blocks]
    envelope, todo, project, heading, checklist = orders
    assert envelope == ["type", "operation", "id", "attributes"]
    assert todo == TJC_TODO and project == TJC_PROJECT
    assert [k for k in heading if not k.endswith("-date")] == list(jc.HEADING_KEYS)
    assert [k for k in checklist if not k.endswith("-date")] == list(jc.CHECKLIST_KEYS)
