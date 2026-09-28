"""Typed builders for the ``json`` URL command (§2.4), following Cultured Code's ThingsJSONCoder (TJC). Pure.

TJC conventions: every object is ``{"type", "operation", "id"?, "attributes"}`` with ``operation`` always
emitted (even "create", also on headings and checklist items); attribute keys in TJC ``CodingKeys`` order;
dates as ``YYYY-MM-DDTHH:MM:SSZ``; ``add-tags`` as ``[String]``; checklist fields as checklist-item objects.
Things decodes a missing ``operation`` as create, so this form and OFF-URL's examples are equivalent.

``validate`` and ``from_obj`` raise ``InvalidInput`` whose message starts with a JSON pointer into the
``data`` array (``/0/attributes/title``); ``details["field"]`` holds it. ``urlscheme.build_json`` validates
and encodes.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, NoReturn, Sequence

from . import urlscheme
from .errors import InvalidInput


@dataclass
class ChecklistItem:
    title: str
    completed: bool | None = None
    canceled: bool | None = None


@dataclass
class Heading:
    title: str
    archived: bool | None = None


@dataclass
class Todo:
    operation: Literal["create", "update"] = "create"
    id: str | None = None
    title: str | None = None
    notes: str | None = None
    prepend_notes: str | None = None
    append_notes: str | None = None
    when: str | None = None
    deadline: str | None = None
    tags: list[str] | None = None
    add_tags: list[str] | None = None
    checklist_items: list[ChecklistItem] | None = None
    prepend_checklist_items: list[ChecklistItem] | None = None
    append_checklist_items: list[ChecklistItem] | None = None
    list_id: str | None = None
    list: str | None = None               # shadows the builtin inside this class body only (TJC key "list")
    heading_id: str | None = None
    heading: str | None = None
    completed: bool | None = None
    canceled: bool | None = None
    creation_date: datetime | None = None
    completion_date: datetime | None = None


@dataclass
class Project:
    operation: Literal["create", "update"] = "create"
    id: str | None = None
    title: str | None = None
    notes: str | None = None
    prepend_notes: str | None = None
    append_notes: str | None = None
    when: str | None = None
    deadline: str | None = None
    tags: list[str] | None = None
    add_tags: list[str] | None = None
    area_id: str | None = None
    area: str | None = None
    items: list[Todo | Heading] | None = None
    completed: bool | None = None
    canceled: bool | None = None
    creation_date: datetime | None = None
    completion_date: datetime | None = None


JsonItem = Todo | Project

TODO_KEYS = ("title", "notes", "prepend-notes", "append-notes", "when", "deadline", "tags", "add-tags",
             "checklist-items", "prepend-checklist-items", "append-checklist-items", "list-id", "list",
             "heading-id", "heading", "completed", "canceled", "creation-date",
             "completion-date")            # TJC order minus undocumented "tag-ids"
PROJECT_KEYS = ("title", "notes", "prepend-notes", "append-notes", "when", "deadline", "tags", "add-tags",
                "area-id", "area", "items", "completed", "canceled", "creation-date", "completion-date")
HEADING_KEYS = ("title", "archived")        # TJC also has creation/completion-date; unused
CHECKLIST_KEYS = ("title", "completed", "canceled")
UPDATE_ONLY = frozenset({"prepend-notes", "append-notes", "add-tags", "prepend-checklist-items",
                         "append-checklist-items"})               # OFF-URL
MAX_JSON_OBJECTS = 100        # to-dos+projects+headings at any depth per URL (LucasCoderT cap; R-MEX §1.4)

# Refused in strict mode: dates are never exposed (N13); the rest have the unresolved string-vs-array
# conflict between OFF-URL and TJC (D10: use the plain update URL instead).
STRICT_REFUSED = ("creation-date", "completion-date", "add-tags", "prepend-checklist-items",
                  "append-checklist-items")

_TYPE_NAME = {Todo: "to-do", Project: "project", Heading: "heading", ChecklistItem: "checklist-item"}
_CLASS = {name: cls for cls, name in _TYPE_NAME.items()}
_KEYS = {Todo: TODO_KEYS, Project: PROJECT_KEYS, Heading: HEADING_KEYS, ChecklistItem: CHECKLIST_KEYS}
_CHECKLIST_FIELDS = frozenset({"checklist-items", "prepend-checklist-items", "append-checklist-items"})
_DATE_FIELDS = frozenset({"creation-date", "completion-date"})
_STR_FIELDS = frozenset({"title", "notes", "prepend-notes", "append-notes", "when", "deadline", "list-id", "list",
                         "heading-id", "heading", "area-id", "area"})
_TAG_FIELDS = frozenset({"tags", "add-tags"})
_BOOL_FIELDS = frozenset({"completed", "canceled", "archived"})
_ENVELOPE = frozenset({"type", "operation", "id", "attributes"})
_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})")
_NESTED_FORBIDDEN = ("list-id", "list", "heading-id", "heading")   # OFF-URL: ignored inside project items


def _attr(key: str) -> str:
    return key.replace("-", "_")


def _fail(path: str, problem: str, hint: str | None = None) -> NoReturn:
    raise InvalidInput(f"{path}: {problem}", hint=hint, details={"field": path})


def _ptr(key: object) -> str:
    s = str(key)
    s = s if len(s) <= 40 else s[:40] + "…"
    return s.replace("~", "~0").replace("/", "~1")


def _kind(value: object) -> str:
    return _TYPE_NAME.get(type(value)) or type(value).__name__


# ---- encoding -----------------------------------------------------------------------------------

def _norm(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


def _encode(key: str, value: object) -> object:
    if key in _CHECKLIST_FIELDS or key == "items":
        return [to_obj(x) for x in value]
    if key in _DATE_FIELDS:
        return urlscheme.format_datetime(key, value)
    if key in _TAG_FIELDS:
        return [_norm(t) if isinstance(t, str) else t for t in value]
    return _norm(value) if isinstance(value, str) else value


def to_obj(item: "JsonItem | Heading | ChecklistItem") -> dict:
    """TJC shape: ``{"type", "operation", "id"?, "attributes"}``; None attributes omitted, key order fixed."""
    cls = type(item)
    if cls not in _TYPE_NAME:
        raise TypeError(f"not a jsoncmd model object: {cls.__name__}")
    attributes = {}
    for key in _KEYS[cls]:
        value = getattr(item, _attr(key))
        if value is not None:
            attributes[key] = _encode(key, value)
    obj = {"type": _TYPE_NAME[cls], "operation": getattr(item, "operation", "create")}
    if getattr(item, "id", None) is not None:
        obj["id"] = item.id
    obj["attributes"] = attributes
    return obj


def dumps(items: Sequence[JsonItem]) -> str:
    return json.dumps([to_obj(i) for i in items], separators=(",", ":"), ensure_ascii=False)


def weight(items: Sequence[object]) -> int:
    """Rate-limit weight: to-do, project and heading objects at any depth (checklist items not counted)."""
    return sum(1 + (weight(it.items) if isinstance(it, Project) and it.items else 0)
               for it in items if isinstance(it, (Todo, Project, Heading)))


def has_update(items: Sequence[object]) -> bool:
    """True if any operation is an update (the runner then appends the auth token)."""
    return any(getattr(it, "operation", None) == "update"
               or (isinstance(it, Project) and bool(it.items) and has_update(it.items)) for it in items)


# ---- validation ---------------------------------------------------------------------------------

def validate(items: Sequence[JsonItem], *, strict: bool = True) -> None:
    """The §2.4 rules. Raises ``InvalidInput`` naming the offending JSON pointer."""
    if not isinstance(items, (list, tuple)):
        _fail("data", f"expected a list of to-dos and projects, got {type(items).__name__}")
    if not items:
        _fail("data", "needs at least one to-do or project")
    for i, item in enumerate(items):
        if not isinstance(item, (Todo, Project)):
            _fail(f"/{i}", f"top-level objects must be to-dos or projects, not {_kind(item)}",
                  hint="headings only exist inside a new project's items")
    n = weight(items)
    if n > MAX_JSON_OBJECTS:
        _fail("data", f"has {n} to-do/project/heading objects; the limit per call is {MAX_JSON_OBJECTS}",
              hint="split it into several calls")
    for i, item in enumerate(items):
        _validate_item(item, f"/{i}", nested=False, strict=strict)


def _validate_item(item: JsonItem, path: str, *, nested: bool, strict: bool) -> None:
    op = item.operation
    attrs = f"{path}/attributes"
    if op not in ("create", "update"):
        _fail(f"{path}/operation", f"must be create or update, not {op!r}")
    if op == "update":
        if nested:
            _fail(f"{path}/operation", "only top-level to-dos and projects can be updated")
        if item.id is None:
            _fail(f"{path}/id", "an update operation needs the id of the item to update")
        urlscheme.check_id(f"{path}/id", item.id)
    elif item.id is not None:
        _fail(f"{path}/id", "a create operation must not carry an id")

    given = {key for key in _KEYS[type(item)] if getattr(item, _attr(key)) is not None}
    if op == "create":
        for key in sorted(given & UPDATE_ONLY):
            _fail(f"{attrs}/{key}", "is only allowed on update operations")
    elif not given:
        _fail(attrs, "nothing to update")
    if strict:
        for key in STRICT_REFUSED:
            if key in given:
                _fail(f"{attrs}/{key}", "is not supported by things-bridge",
                      hint="dates can't be set; add tags or checklist items with the update URL (things_tag, things_update_todo)")
    for key in ("completed", "canceled"):
        if key in given:
            urlscheme.check_bool(f"{attrs}/{key}", getattr(item, key))
    for a, b in urlscheme.EXCLUSIVE:
        if a in given and b in given:
            _fail(f"{attrs}/{b}", f"use either {a} or {b}, not both")
    if {"completed", "canceled"} <= given and (item.completed or item.canceled):
        _fail(f"{attrs}/canceled", "use either completed or canceled, not both")

    if item.title is not None:
        urlscheme.check_title(f"{attrs}/title", item.title)
    for key in ("notes", "prepend-notes", "append-notes"):
        if key in given:
            urlscheme.check_notes(f"{attrs}/{key}", getattr(item, _attr(key)))
    if item.when is not None:
        urlscheme.check_when(f"{attrs}/when", item.when, allow_anytime=True, strict=strict)   # D10
    if item.deadline is not None:
        urlscheme.check_deadline(f"{attrs}/deadline", item.deadline, strict=strict)
    for key in ("tags", "add-tags"):
        if key in given:
            urlscheme.check_tags(f"{attrs}/{key}", getattr(item, _attr(key)))
    for key in sorted(given & _DATE_FIELDS):
        urlscheme.format_datetime(f"{attrs}/{key}", getattr(item, _attr(key)))

    if isinstance(item, Todo):
        _validate_todo(item, attrs, given, op=op, nested=nested)
    else:
        _validate_project(item, attrs, given, op=op, strict=strict)


def _validate_todo(todo: Todo, attrs: str, given: set[str], *, op: str, nested: bool) -> None:
    if op == "update" and "checklist-items" in given:      # D11: replacing destroys unreadable items
        _fail(f"{attrs}/checklist-items",
              "replacing a checklist would delete its current (unreadable) items, which this tool never does",
              hint="append checklist items with the update URL instead")
    for key in sorted(given & _CHECKLIST_FIELDS):
        _validate_checklist(f"{attrs}/{key}", getattr(todo, _attr(key)))
    for key in ("list-id", "heading-id"):
        if key in given:
            urlscheme.check_id(f"{attrs}/{key}", getattr(todo, _attr(key)))
    for key in ("list", "heading"):
        if key in given:
            urlscheme.check_title(f"{attrs}/{key}", getattr(todo, key))
    if nested:
        for key in _NESTED_FORBIDDEN:
            if key in given:
                _fail(f"{attrs}/{key}", "is ignored by Things inside a project's items")
    elif op == "create" and given & {"heading", "heading-id"} and not given & {"list", "list-id"}:
        _fail(f"{attrs}/heading", "a heading needs its project in list-id (Things ignores it otherwise)")


def _validate_project(project: Project, attrs: str, given: set[str], *, op: str, strict: bool) -> None:
    if "area-id" in given:
        urlscheme.check_id(f"{attrs}/area-id", project.area_id)
    if "area" in given:
        urlscheme.check_title(f"{attrs}/area", project.area)
    if project.items is None:
        return
    if op != "create":
        _fail(f"{attrs}/items", "items can only be given when creating a project",
              hint="to add to-dos to an existing project, create to-dos with its id in list-id")
    items = urlscheme.check_seq(f"{attrs}/items", project.items)
    open_todos = False
    for j, child in enumerate(items):
        p = f"{attrs}/items/{j}"
        if isinstance(child, Todo):
            _validate_item(child, p, nested=True, strict=strict)
            open_todos = open_todos or not (child.completed or child.canceled)
        elif isinstance(child, Heading):
            urlscheme.check_title(f"{p}/attributes/title", child.title)
            if child.archived is not None:
                urlscheme.check_bool(f"{p}/attributes/archived", child.archived)
        else:
            _fail(p, f"a project's items must be to-dos or headings, not {_kind(child)}")
    if strict and open_todos and (project.completed or project.canceled):   # TAPI §5q: it lands open
        _fail(f"{attrs}/{'completed' if project.completed else 'canceled'}",
              "a new project with open to-dos can't be created completed or canceled",
              hint="complete or cancel its to-dos too, or leave the project open")


def _validate_checklist(path: str, value: object) -> None:
    items = urlscheme.check_seq(path, value)
    if len(items) > urlscheme.MAX_CHECKLIST:
        _fail(path, f"has {len(items)} items; Things keeps only the first {urlscheme.MAX_CHECKLIST}")
    for k, entry in enumerate(items):
        p = f"{path}/{k}"
        if not isinstance(entry, ChecklistItem):           # TAPI §22: strings void the whole batch
            _fail(p, f"checklist items must be checklist-item objects, not {_kind(entry)}")
        urlscheme.check_title(f"{p}/attributes/title", entry.title)
        for key in ("completed", "canceled"):
            if getattr(entry, key) is not None:
                urlscheme.check_bool(f"{p}/attributes/{key}", getattr(entry, key))
        if entry.completed is not None and entry.canceled is not None and (entry.completed or entry.canceled):
            _fail(f"{p}/attributes/canceled", "use either completed or canceled, not both")


# ---- parsing raw JSON (things_json tool) ---------------------------------------------------------

def from_obj(obj: object) -> JsonItem:
    """Parse one top-level to-do or project object. Unknown keys are refused at every level (Things
    ignores them silently: R-PAL P37), as are ``tag-ids`` and nested update operations. Run ``validate``
    (or ``urlscheme.build_json``) on the result for the semantic rules."""
    return _parse(obj, "", ("to-do", "project"), nested=False)


def from_objs(data: object) -> list[JsonItem]:
    """Parse a whole ``data`` array (the ``things_json`` payload)."""
    if not isinstance(data, list):
        _fail("data", f"expected a JSON array of to-do and project objects, got {type(data).__name__}")
    return [_parse(obj, f"/{i}", ("to-do", "project"), nested=False) for i, obj in enumerate(data)]


def _parse(obj: object, path: str, types: tuple[str, ...], *, nested: bool) -> object:
    here = path or "data"
    if not isinstance(obj, dict):
        hint = "write each as {\"type\":\"checklist-item\",\"attributes\":{\"title\":…}}" \
            if types == ("checklist-item",) else None
        _fail(here, f"expected a {' or '.join(types)} object, got {type(obj).__name__}", hint=hint)
    for key in obj:
        if key not in _ENVELOPE:
            _fail(f"{path}/{_ptr(key)}", "unknown key (Things would silently ignore it)")
    kind = obj.get("type")
    if kind not in types:
        _fail(f"{path}/type", f"must be {' or '.join(types)}, not {_clip_json(kind)}")
    op = obj.get("operation")
    op = "create" if op is None else op
    if op not in ("create", "update"):
        _fail(f"{path}/operation", f"must be create or update, not {_clip_json(op)}")
    ident = obj.get("id")
    if ident is not None and not isinstance(ident, str):
        _fail(f"{path}/id", f"expected a string, got {type(ident).__name__}")
    cls = _CLASS[kind]
    if cls in (Heading, ChecklistItem) or nested:
        if op != "create":
            _fail(f"{path}/operation", f"a nested {kind} can only be created")
        if ident is not None:
            _fail(f"{path}/id", f"a nested {kind} can't carry an id")
    if "attributes" not in obj:
        _fail(f"{path}/attributes", "is required (it may be {})")
    raw = obj["attributes"]
    if not isinstance(raw, dict):
        _fail(f"{path}/attributes", f"expected an object, got {type(raw).__name__}")

    allowed = _KEYS[cls]
    fields: dict[str, object] = {}
    for key, value in raw.items():
        p = f"{path}/attributes/{_ptr(key)}"
        if key == "tag-ids":
            _fail(p, "tag-ids is not documented by Cultured Code", hint="use tags (tag titles)")
        if key not in allowed:
            _fail(p, f"unknown {kind} attribute (Things would silently ignore it)")
        if value is None:                                   # JSON null == absent (TJC decodeIfPresent)
            continue
        if key in _STR_FIELDS:
            if not isinstance(value, str):
                _fail(p, f"expected a string, got {type(value).__name__}")
        elif key in _TAG_FIELDS:
            if not isinstance(value, list) or not all(isinstance(t, str) for t in value):
                _fail(p, "expected an array of strings")
        elif key in _BOOL_FIELDS:
            if not isinstance(value, bool):
                _fail(p, f"expected true or false, got {type(value).__name__}")
        elif key in _DATE_FIELDS:
            value = _parse_datetime(p, value)
        elif key in _CHECKLIST_FIELDS or key == "items":
            if not isinstance(value, list):
                _fail(p, f"expected an array, got {type(value).__name__}")
            child_types = ("checklist-item",) if key != "items" else ("to-do", "heading")
            value = [_parse(x, f"{p}/{j}", child_types, nested=True) for j, x in enumerate(value)]
        fields[_attr(key)] = value

    if cls in (Heading, ChecklistItem):
        if "title" not in fields:
            _fail(f"{path}/attributes/title", f"a {kind} needs a title")
        return cls(**fields)
    return cls(operation=op, id=ident, **fields)


def _parse_datetime(path: str, value: object) -> datetime:
    if not isinstance(value, str) or not _ISO_RE.fullmatch(value):
        _fail(path, "expected an ISO 8601 date-time like 2026-03-10T14:30:00Z (no fractional seconds)")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        _fail(path, f"{value} is not a real date-time")


def _clip_json(value: object) -> str:
    s = json.dumps(value, ensure_ascii=False) if isinstance(value, (str, int, float, bool, type(None))) \
        else type(value).__name__
    return s if len(s) <= 40 else s[:40] + "…"
