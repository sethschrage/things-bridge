"""In-memory Things 3 (``FakeThings``) and a ``Runner`` that drives it (``FakeRunner``), per DESIGN §7.

Nothing here spawns a process or touches Things.

* ``FakeThings.open_url`` interprets the documented URL commands (``add``, ``add-project``, ``update``,
  ``update-project``, ``json``; ``show``/``search``/``version`` only navigate) with the semantics in
  R/official/url-scheme.md: unknown tags are ignored, ``param=`` clears, ``titles`` lines create one
  to-do each, ``completed=false`` reopens, ``list-id``/``heading`` place to-dos, json creates projects
  with headings. Updates need the right ``auth-token`` (a wrong or missing one opens an error sheet).
* ``FakeThings.render`` returns the exact osascript stdout (bytes, one trailing newline) each template
  in ``applescript.TEMPLATES`` would print, or raises ``OsaError`` where the script would fail.
* ``FakeRunner`` implements ``runner.Runner`` on top of a ``FakeThings``, mirroring ``MacRunner``'s
  checks and error mapping: it records every call and can inject failures (osascript error numbers,
  timeouts, sheet mode, dropped or late URLs).

Items are dicts: id, kind ("to-do"|"project"), title, notes, status, tags, start ("inbox"|"anytime"|
"someday"|"YYYY-MM-DD"), evening, reminder, deadline, project, area, heading, created, modified,
completed, canceled (naive local datetimes), checklist, logged, trashed, repeating.

Where Things' behaviour is UNCERTAIN (DESIGN §6.1) the fake picks one reading and exposes it as an
instance attribute (see ``FakeThings.__init__``) so a test can flip it. Deliberate simplifications:
natural-language dates are ignored; to-dos with no start date of their own inside a someday or
future-dated project appear in no list (Q14); a deadline puts an item in Today once it is due, but
never in Upcoming; ``to dos of area`` holds only the area's loose to-dos (not its projects).

Clock: timestamps come from ``clock.wall()``, shifted by whole days so the date at construction equals
``clock.today()`` (conftest's FakeClock has a fixed ``today`` a week after its default wall time);
list membership uses ``clock.today()``, the same "today" the service is given.
"""

import json
import random
import string
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterator, Sequence
from urllib.parse import unquote

from things_bridge import authtoken, urlscheme
from things_bridge import runner as runner_mod
from things_bridge.applescript import RS, TEMPLATES, US, Frame, encode_args, parse_frame, parse_osa_error
from things_bridge.errors import (AutomationTimeout, InvalidInput, NotMacOS, Refused, ThingsBridgeError,
                                  ThingsNotInstalled, ThingsNotRunning, TokenMissing)
from things_bridge.runner import Dispatch, Runner

FAKE_TOKEN = "fAkEt0kEnThInGsBrIdGe1"       # matches authtoken.TOKEN_RE; never a real token
BUILTIN_LISTS = ("inbox", "today", "anytime", "upcoming", "someday", "logbook", "trash", "tomorrow")
LIST_IDS = {"inbox": "TMInboxListSource", "today": "TMTodayListSource", "anytime": "TMNextListSource",
            "upcoming": "TMCalendarListSource", "someday": "TMSomedayListSource",
            "logbook": "TMLogbookListSource", "trash": "TMTrashListSource", "tomorrow": "tomorrow"}
LIST_NAMES = {"inbox": "Inbox", "today": "Today", "anytime": "Anytime", "upcoming": "Upcoming",
              "someday": "Someday", "logbook": "Logbook", "trash": "Trash", "tomorrow": "Tomorrow"}

_ALNUM = string.ascii_letters + string.digits
_ITEM_KEYS = ("id", "kind", "title", "notes", "status", "tags", "start", "evening", "reminder", "deadline",
              "project", "area", "heading", "created", "modified", "completed", "canceled", "checklist",
              "logged", "trashed", "repeating")
_CHECKLIST_PARAMS = ("checklist-items", "prepend-checklist-items", "append-checklist-items")


class OsaError(Exception):
    """An AppleScript runtime/compile error, as osascript reports it on stderr (rc 1)."""

    def __init__(self, code: int, message: str):
        super().__init__(f"{message} ({code})")
        self.code = code
        self.message = message

    @property
    def stderr(self) -> str:
        return f"execution error: {self.message} ({self.code})\n"


class _Crash(Exception):
    """Things crashed while applying a URL (TAPI §1: `when` on a repeating template)."""


# ---- AppleScript library equivalents (§4.2) ------------------------------------------------------

def _t(value: object) -> str:
    """tb_text: missing value -> "", US/RS removed."""
    if value is None:
        return ""
    return str(value).replace(US, "").replace(RS, "")


def _day(value: "str | date | datetime | None") -> str:
    """tb_day."""
    if value is None or value in ("inbox", "anytime", "someday"):
        return ""
    if isinstance(value, datetime):
        value = value.date()
    return value if isinstance(value, str) else value.isoformat()


def _stamp(value: "datetime | None") -> str:
    """tb_stamp: local, no offset."""
    return "" if value is None else value.strftime("%Y-%m-%dT%H:%M:%S")


def _rec(*fields: str) -> str:
    """tb_rec."""
    return US.join(fields)


def _is(a: str, b: str) -> bool:
    """AppleScript `is` on text: ignoring case by default."""
    return a.casefold() == b.casefold()


def _in(value: str, values: list[str]) -> bool:
    return any(_is(value, v) for v in values)


def _add_unique(values: list[str], value: str) -> None:
    """tb_addUnique."""
    if value != "" and not _in(value, values):
        values.append(value)


def _as_integer(s: str) -> int:
    try:
        return int(s.strip())
    except ValueError:
        try:
            return round(float(s))
        except ValueError:
            raise OsaError(-1700, f"Can’t make \"{s}\" into type integer.") from None


def _date(s: str) -> date | None:
    try:
        return date.fromisoformat(s) if len(s) == 10 else None
    except ValueError:
        return None


def _bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return None


def _redact(url: str) -> str:
    head, sep, _ = url.partition("auth-token=")
    if not sep:
        return url
    rest = url[len(head) + len(sep):]
    tail = rest[rest.index("&"):] if "&" in rest else ""
    return f"{head}auth-token=***{tail}"


def _parse_query(query: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for part in query.split("&") if query else ():
        key, _, value = part.partition("=")
        params[unquote(key)] = unquote(value)       # never unquote_plus: "+" is literal
    return params


# ---- FakeThings -------------------------------------------------------------------------------

class FakeThings:
    """In-memory Things 3: the URL-scheme writer and the AppleScript reader see the same state."""

    def __init__(self, clock: Any = None, *, seed: int = 1):
        if clock is None:
            from conftest import FakeClock
            clock = FakeClock()
        self.clock = clock
        wall_day = datetime.fromtimestamp(clock.wall()).date()
        self._shift = timedelta(days=(self.today() - wall_day).days)
        self._rng = random.Random(seed)
        self._ids: set[str] = set()
        self._parked: list[str] = []
        self.items: dict[str, dict] = {}         # to-dos and projects, in creation order
        self.areas: dict[str, dict] = {}         # id -> {id, title, tags}
        self.tags: dict[str, dict] = {}          # id -> {id, title, parent, shortcut}
        self.headings: dict[str, dict] = {}      # id -> {id, title, project, archived}
        # app state
        self.installed = True
        self.running = True
        self.version = "3.22.1"
        self.urls_enabled = True                 # False: the first URL opens the "Enable" sheet and parks
        self.auth_token = FAKE_TOKEN
        self.sheet: str | None = None            # None | "enable-urls" | "error" | "dialog"
        # UNCERTAIN behaviours (DESIGN §6.1), one reading each
        self.log_immediately = True              # Q24: completed items leave their lists at once
        self.list_todos_include_projects = True  # R-ASR §3: `to dos of list` returns projects too
        self.app_todos_include_projects = False  # Q4
        self.todo_id_resolves_projects = True    # Q23
        self.project_complete_mode = "ignore"    # Q12: "ignore" (OFF-URL) or "cascade" (TAPI §3)
        self.reopen_project_on_add = True        # TAPI §5b
        self.crash_on_repeating_when = True      # TAPI §1
        self.missing_lists: set[str] = set()     # Q2: list ids that don't resolve
        # what happened
        self.log: list[str] = []                 # every URL received, token redacted
        self.errors: list[str] = []              # error sheets and failed commands
        self.ignored: list[str] = []             # parts Things silently ignored
        self.shown: list[str] = []               # show/search/version (navigation)
        self.revealed: list[str] = []
        self.last_created: list[str] = []        # x-things-id(s) of the last applied URL
        self.crashes = 0
        self.launches = 0

    # ---- time -----------------------------------------------------------------------------------

    def now(self) -> datetime:
        """AppleScript `current date` (local, whole seconds)."""
        return (datetime.fromtimestamp(self.clock.wall()) + self._shift).replace(microsecond=0)

    def today(self) -> date:
        return self.clock.today() if hasattr(self.clock, "today") else datetime.fromtimestamp(
            self.clock.wall()).date()

    def time_to_gmt(self) -> int:
        offset = self.now().astimezone().utcoffset()
        return int(offset.total_seconds()) if offset is not None else 0

    # ---- seeding (test setup; not something Things' automation could do) -------------------------

    def new_id(self) -> str:
        while True:
            s = "".join(self._rng.choice(_ALNUM) for _ in range(22))
            if s not in self._ids:
                self._ids.add(s)
                return s

    def _claim(self, id: str | None) -> str:
        if id is None:
            return self.new_id()
        if id in self._ids:
            raise ValueError(f"duplicate id {id!r}")
        self._ids.add(id)
        return id

    def seed_area(self, title: str, *, tags: Sequence[str] = (), id: str | None = None) -> str:
        a = {"id": self._claim(id), "title": title, "tags": list(tags)}
        self.areas[a["id"]] = a
        return a["id"]

    def seed_tag(self, title: str, *, parent: str | None = None, shortcut: str | None = None,
                 id: str | None = None) -> str:
        if parent is not None and parent not in self.tags:
            parent = self._tag_by_title(parent)["id"]
        t = {"id": self._claim(id), "title": title, "parent": parent, "shortcut": shortcut}
        self.tags[t["id"]] = t
        return t["id"]

    def seed_heading(self, project: str, title: str, *, archived: bool = False, id: str | None = None) -> str:
        if self.items.get(project, {}).get("kind") != "project":
            raise ValueError(f"no project {project!r}")
        h = {"id": self._claim(id), "title": title, "project": project, "archived": archived}
        self.headings[h["id"]] = h
        return h["id"]

    def seed_todo(self, title: str, **fields: Any) -> str:
        """A to-do. Extra keywords: any item key, plus ``when`` (URL vocabulary or "inbox") and ``age``
        (seconds since creation)."""
        return self._seed("to-do", title, fields)

    def seed_project(self, title: str, **fields: Any) -> str:
        return self._seed("project", title, fields)

    def _seed(self, kind: str, title: str, fields: dict) -> str:
        it = self._new_item(kind, title, id=self._claim(fields.pop("id", None)))
        age = fields.pop("age", None)
        if age is not None:
            it["created"] = it["modified"] = self.now() - timedelta(seconds=age)
        project, area, heading = fields.pop("project", None), fields.pop("area", None), fields.pop("heading", None)
        if project is not None and area is not None and kind == "to-do":
            raise ValueError("a to-do in a project has no area of its own (R-ASR §1.2)")
        if project is not None:
            if kind != "to-do" or self.items.get(project, {}).get("kind") != "project":
                raise ValueError(f"no project {project!r}")
            self._place(it, "project", project)
        if area is not None:
            if area not in self.areas:
                raise ValueError(f"no area {area!r}")
            self._place(it, "area", area)
        if heading is not None:
            hid = heading if heading in self.headings else self._heading_by_title(project, heading)
            if hid is None or self.headings[hid]["project"] != project:
                raise ValueError(f"no heading {heading!r} in project {project!r}")
            it["heading"] = hid
        when = fields.pop("when", None)
        if when == "inbox":
            it["start"] = "inbox"
        elif when is not None and not self._apply_when(it, when):
            raise ValueError(f"unsupported when {when!r}")
        status = fields.pop("status", "open")
        explicit = {k: fields.pop(k) for k in ("completed", "canceled", "logged") if k in fields}
        unknown = set(fields) - set(_ITEM_KEYS)
        if unknown:
            raise TypeError(f"unknown item fields {sorted(unknown)}")
        it.update(fields)
        if status != "open":
            self._set_status(it, status)
        it.update(explicit)
        self.items[it["id"]] = it
        return it["id"]

    def _new_item(self, kind: str, title: str, *, id: str | None = None) -> dict:
        now = self.now()
        return {"id": id or self.new_id(), "kind": kind, "title": title, "notes": "", "status": "open",
                "tags": [], "start": "inbox" if kind == "to-do" else "anytime", "evening": False,
                "reminder": None, "deadline": None, "project": None, "area": None, "heading": None,
                "created": now, "modified": now, "completed": None, "canceled": None, "checklist": [],
                "logged": False, "trashed": False, "repeating": False}

    # ---- inspection -----------------------------------------------------------------------------

    def get(self, id: str) -> dict:
        return self.items[id]

    def by_title(self, title: str) -> list[dict]:
        return [it for it in self.items.values() if it["title"] == title]

    def one(self, title: str) -> dict:
        found = self.by_title(title)
        if len(found) != 1:
            raise AssertionError(f"{len(found)} items titled {title!r}")
        return found[0]

    def list_ids(self, key: str) -> list[str]:
        """Ids of ``every to do of list id <key>`` (projects included, per the knob)."""
        return [it["id"] for it in self._list_todos(key)]

    def children(self, project: str) -> list[dict]:
        return self._project_children(project)

    def heading_title(self, it: dict) -> str | None:
        return self.headings[it["heading"]]["title"] if it["heading"] else None

    @property
    def parked(self) -> int:
        """Number of URLs held behind the "Enable Things URLs" sheet."""
        return len(self._parked)

    # ---- the user, at the Mac -------------------------------------------------------------------

    def enable_urls(self) -> list[str]:
        """The user clicks Enable: parked commands apply now, in order (R-MEX §3)."""
        self.urls_enabled = True
        if self.sheet == "enable-urls":
            self.sheet = None
        created: list[str] = []
        parked, self._parked = self._parked, []
        for url in parked:
            created += self._execute(url)
        return created

    def dismiss_sheet(self) -> None:
        """Cancel/OK on whatever sheet is open; parked URLs are dropped."""
        self.sheet = None
        self._parked.clear()

    def log_completed_now(self) -> None:
        for it in self.items.values():
            if it["status"] != "open":
                it["logged"] = True

    # ---- URL scheme -----------------------------------------------------------------------------

    def open_url(self, url: str) -> list[str]:
        """LaunchServices hands Things a ``things:///`` URL. Returns the ids it created (x-things-id)."""
        self.log.append(_redact(url))
        self.last_created = []
        if not self.running:
            self.running = True
            self.launches += 1
        if not url.startswith(urlscheme.SCHEME_PREFIX):
            self._error(f"not a Things URL: {_redact(url)}")
            return []
        if not self.urls_enabled:
            self._parked.append(url)
            self.sheet = "enable-urls"
            return []
        return self._execute(url)

    def _execute(self, url: str) -> list[str]:
        command, _, query = url[len(urlscheme.SCHEME_PREFIX):].partition("?")
        params = _parse_query(query)
        token = params.pop("auth-token", None)
        for key in [k for k in params if k.startswith("x-")]:
            del params[key]
        handler = {"add": self._cmd_add, "add-project": self._cmd_add_project, "update": self._cmd_update,
                   "update-project": self._cmd_update_project, "json": self._cmd_json,
                   "show": self._cmd_nav, "search": self._cmd_nav, "version": self._cmd_nav}.get(command)
        if handler is None:
            self._error(f"unsupported command {command!r}")
            return []
        if command in urlscheme.TOKEN_ALWAYS and token != self.auth_token:
            self._error(f"{command}: auth-token missing or wrong; nothing changed")
            return []
        try:
            created = handler(command, params, token)
        except _Crash:
            return []
        self.last_created = created
        return created

    def _error(self, message: str) -> None:
        self.errors.append(message)
        self.sheet = "error"

    def _crash(self, why: str) -> None:
        self.errors.append(f"crash: {why}")
        self.crashes += 1
        self.running = False
        self.sheet = None
        raise _Crash(why)

    def _cmd_nav(self, command: str, params: dict, token: str | None) -> list[str]:
        self.shown.append(command + ("?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else ""))
        return []

    def _cmd_add(self, command: str, params: dict, token: str | None) -> list[str]:
        if "titles" in params:
            titles = [line for line in params["titles"].split("\n") if line.strip()]
        else:
            if _bool(params.get("show-quick-entry")):
                self.ignored.append("add: show-quick-entry opens Quick Entry; nothing was added")
                return []
            titles = [params.get("title", "")]
        if "use-clipboard" in params:
            self.ignored.append("add: use-clipboard (the fake has no clipboard)")
        target = self._resolve_list(params)
        if target is False:                  # TAPI §2a: unknown list -> complete no-op
            self.ignored.append("add: unknown list; nothing was added")
            return []
        ids = []
        for title in titles:
            it = self._new_item("to-do", title)
            self._fill_todo(it, params, target)
            ids.append(it["id"])
        if ids and _bool(params.get("reveal")):
            self.revealed.append(ids[0])
        return ids

    def _fill_todo(self, it: dict, params: dict, target: "tuple[str, str] | None") -> None:
        """A new to-do from add/json-create parameters; inserted into the model."""
        if "notes" in params:
            it["notes"] = params["notes"]
        if target is not None:
            self._place(it, *target)
            hid = self._resolve_heading(target[1], params) if target[0] == "project" else None
            if hid:
                it["heading"] = hid
            elif "heading" in params or "heading-id" in params:
                self.ignored.append("heading not found in the project; the to-do is at its root")
        elif "heading" in params or "heading-id" in params:
            self.ignored.append("heading without a project")
        if "when" in params and not self._apply_when(it, params["when"]):
            self.ignored.append(f"when {params['when']!r} not understood")
        if "deadline" in params:
            self._apply_deadline(it, params["deadline"])
        if "tags" in params:
            it["tags"] = self._existing_tags(params["tags"])
        if "checklist-items" in params:
            it["checklist"] = self._checklist(params["checklist-items"])
        self.items[it["id"]] = it
        self._apply_status(it, params)
        self._apply_dates(it, params)
        self._maybe_reopen_project(it)

    def _cmd_add_project(self, command: str, params: dict, token: str | None) -> list[str]:
        area = self._resolve_area(params)
        if area is False:
            self.ignored.append("add-project: unknown area; nothing was added")
            return []
        p = self._new_item("project", params.get("title", ""))
        if "notes" in params:
            p["notes"] = params["notes"]
        if area:
            p["area"] = area
        if "when" in params and not self._apply_when(p, params["when"]):
            self.ignored.append(f"when {params['when']!r} not understood")
        if "deadline" in params:
            self._apply_deadline(p, params["deadline"])
        if "tags" in params:
            p["tags"] = self._existing_tags(params["tags"])
        self.items[p["id"]] = p
        children = []
        for line in params.get("to-dos", "").split("\n"):
            if line.strip():
                c = self._new_item("to-do", line)
                self._place(c, "project", p["id"])
                self.items[c["id"]] = c
                children.append(c)
        status = self._status_from(params)
        if status not in (None, "open"):        # OFF-URL: "Will set all child to-dos to be completed"
            for c in children:
                self._set_status(c, status)
            self._set_status(p, status)
        for it in (p, *children):
            self._apply_dates(it, params)
        if _bool(params.get("reveal")):
            self.revealed.append(p["id"])
        return [p["id"]]

    def _cmd_update(self, command: str, params: dict, token: str | None) -> list[str]:
        it = self._target(params.get("id"), "to-do", command)
        if it is None:
            return []
        if _bool(params.get("duplicate")):
            if it["repeating"]:
                self.ignored.append("update: repeating to-dos can't be duplicated")
                return []
            it = self._duplicate(it)
        self._apply_update(it, params)
        if _bool(params.get("reveal")):
            self.revealed.append(it["id"])
        return [it["id"]]

    def _cmd_update_project(self, command: str, params: dict, token: str | None) -> list[str]:
        it = self._target(params.get("id"), "project", command)
        if it is None:
            return []
        if _bool(params.get("duplicate")):
            if it["repeating"]:
                self.ignored.append("update-project: repeating projects can't be duplicated")
                return []
            it = self._duplicate(it)
        self._apply_update(it, params)
        if _bool(params.get("reveal")):
            self.revealed.append(it["id"])
        return [it["id"]]

    def _target(self, id: str | None, kind: str, command: str) -> dict | None:
        it = self.items.get(id or "")
        if it is None or it["kind"] != kind:
            self.errors.append(f"{command}: no {kind} with id {id!r}")
            return None
        return it

    def _duplicate(self, it: dict) -> dict:
        now = self.now()
        dup = dict(it, id=self.new_id(), tags=list(it["tags"]), checklist=[dict(c) for c in it["checklist"]],
                   created=now, modified=now)
        self.items[dup["id"]] = dup
        return dup

    def _apply_update(self, it: dict, params: dict) -> None:
        """update / update-project / json update semantics (OFF-URL)."""
        params = dict(params)
        if it["repeating"]:          # "This field cannot be updated on repeating to-dos"
            if "when" in params and self.crash_on_repeating_when:
                self._crash(f"when= on repeating {it['kind']} {it['id']}")
            for key in ("when", "deadline", "completed", "canceled", "completion-date"):
                if key in params:
                    del params[key]
                    self.ignored.append(f"{key} on a repeating {it['kind']}")
        if "title" in params:
            it["title"] = params["title"]
        if "notes" in params:
            it["notes"] = params["notes"]
        if "prepend-notes" in params:
            it["notes"] = params["prepend-notes"] + it["notes"]
        if "append-notes" in params:
            it["notes"] = it["notes"] + params["append-notes"]
        if "when" in params:
            if params["when"] == "":
                it.update(start="inbox" if it["kind"] == "to-do" and not (it["project"] or it["area"])
                          else "anytime", evening=False, reminder=None)
            elif not self._apply_when(it, params["when"]):
                self.ignored.append(f"when {params['when']!r} not understood")
        if "deadline" in params:
            self._apply_deadline(it, params["deadline"])
        if "tags" in params:
            it["tags"] = self._existing_tags(params["tags"])
        if "add-tags" in params:
            for tag in self._existing_tags(params["add-tags"]):
                if tag not in it["tags"]:
                    it["tags"].append(tag)
        if it["kind"] == "to-do":
            if "checklist-items" in params:
                it["checklist"] = self._checklist(params["checklist-items"])
            if "prepend-checklist-items" in params:
                it["checklist"] = self._checklist(params["prepend-checklist-items"]) + it["checklist"]
            if "append-checklist-items" in params:
                it["checklist"] = it["checklist"] + self._checklist(params["append-checklist-items"])
            it["checklist"] = it["checklist"][:urlscheme.MAX_CHECKLIST]
            target = self._resolve_list(params)
            if target is False:
                self.ignored.append("unknown list; the to-do was not moved")
            elif target is not None:
                self._place(it, *target)
            if "heading" in params or "heading-id" in params:
                hid = self._resolve_heading(it["project"], params) if it["project"] else None
                if hid:
                    it["heading"] = hid
                else:
                    self.ignored.append("heading not found in the to-do's project")
        else:
            area = self._resolve_area(params)
            if area is False:
                self.ignored.append("unknown area; the project was not moved")
            elif area:
                it["area"] = area
        self._apply_status(it, params)
        self._apply_dates(it, params)
        it["modified"] = self.now()
        self._maybe_reopen_project(it)

    def _status_from(self, params: dict) -> str | None:
        """canceled=true beats completed; completed=false or canceled=false reopen."""
        completed, canceled = _bool(params.get("completed")), _bool(params.get("canceled"))
        if canceled is True:
            return "canceled"
        if completed is True:
            return "completed"
        if completed is False or canceled is False:
            return "open"
        return None

    def _apply_status(self, it: dict, params: dict, *, json_create: bool = False) -> None:
        status = self._status_from(params)
        if status is None:
            return
        if it["kind"] == "project" and status != "open":
            self._close_project(it, status, json_create=json_create)
        else:
            self._set_status(it, status)

    def _close_project(self, p: dict, status: str, *, json_create: bool) -> None:
        """OFF-URL: ignored unless every child is closed (and, for updates, every heading archived).

        A json create with open to-dos lands open either way (TAPI §5q); updates cascade instead when
        ``project_complete_mode == "cascade"`` (TAPI §3).
        """
        open_children = [c for c in self._project_children(p["id"]) if c["status"] == "open"]
        unarchived = [] if json_create else \
            [h for h in self.headings.values() if h["project"] == p["id"] and not h["archived"]]
        if open_children or unarchived:
            if json_create or self.project_complete_mode != "cascade":
                self.ignored.append(f"{status}: project {p['id']} still has open to-dos or unarchived headings")
                return
            for c in open_children:
                self._set_status(c, status)
            for h in unarchived:
                h["archived"] = True
        self._set_status(p, status)

    def _set_status(self, it: dict, status: str) -> None:
        if status == it["status"]:
            return
        now = self.now()
        if status == "open":
            it.update(status="open", completed=None, canceled=None, logged=False)
        elif status == "completed":
            it.update(status="completed", completed=now, canceled=None, logged=self.log_immediately)
        elif status == "canceled":
            it.update(status="canceled", canceled=now, completed=None, logged=self.log_immediately)
        else:
            raise ValueError(f"unknown status {status!r}")
        it["modified"] = now

    def _maybe_reopen_project(self, it: dict) -> None:
        """TAPI §5b: an open to-do added to (or moved into) a closed project reopens it."""
        if it["kind"] != "to-do" or it["status"] != "open" or not it["project"] or not self.reopen_project_on_add:
            return
        p = self.items[it["project"]]
        if p["status"] != "open":
            self._set_status(p, "open")
            self.ignored.append(f"project {p['id']} was reopened by an open to-do")

    def _apply_dates(self, it: dict, params: dict) -> None:
        now = self.now()
        for key in ("creation-date", "completion-date"):
            if key not in params:
                continue
            try:
                value = datetime.fromisoformat(params[key].replace("Z", "+00:00"))
                local = value.astimezone().replace(tzinfo=None) if value.tzinfo else value
            except (ValueError, AttributeError):
                self.errors.append(f"{key}: {params[key]!r} is not ISO 8601")
                continue
            if local > now:
                self.ignored.append(f"{key} in the future")
            elif key == "creation-date":
                it["created"] = local
            elif it["status"] != "open":
                it[it["status"]] = local
            else:
                self.ignored.append("completion-date on an open item")

    def _apply_when(self, it: dict, value: str) -> bool:
        day, at, time_ = value.partition("@")
        day = day.strip().lower()
        today = self.today()
        evening = False
        if day in ("today", "evening"):
            start, evening = today.isoformat(), day == "evening"
        elif day == "tomorrow":
            start = (today + timedelta(days=1)).isoformat()
        elif day in ("anytime", "someday"):
            start = day
        elif _date(day) is not None:
            start = day
        else:
            return False                     # natural language: not modelled
        it.update(start=start, evening=evening,
                  reminder=time_.strip() if at and day not in ("anytime", "someday") else None)
        return True

    def _apply_deadline(self, it: dict, value: str) -> None:
        v = value.strip().lower()
        if v == "":
            it["deadline"] = None
        elif v in ("today", "tomorrow"):
            it["deadline"] = (self.today() + timedelta(days=v == "tomorrow")).isoformat()
        elif _date(v) is not None:
            it["deadline"] = v
        else:
            self.ignored.append(f"deadline {value!r} not understood")

    def _existing_tags(self, value: "str | list") -> list[str]:
        """Tag titles that exist (exact match); unknown ones are dropped, as Things does."""
        names = value.split(",") if isinstance(value, str) else list(value)
        known = {t["title"] for t in self.tags.values()}
        out: list[str] = []
        for name in (n.strip() for n in names if isinstance(n, str)):
            if not name:
                continue
            if name not in known:
                self.ignored.append(f"tag {name!r} doesn't exist")
            elif name not in out:
                out.append(name)
        return out

    def _checklist(self, value: "str | list") -> list[dict]:
        if isinstance(value, str):
            rows = [{"title": line, "completed": False, "canceled": False}
                    for line in value.split("\n") if line.strip()]
        else:
            rows = []
            for obj in value:
                attrs = obj.get("attributes", obj) if isinstance(obj, dict) else {}
                rows.append({"title": attrs.get("title", ""), "completed": bool(attrs.get("completed")),
                             "canceled": bool(attrs.get("canceled"))})
        return rows[:urlscheme.MAX_CHECKLIST]

    def _resolve_list(self, params: dict) -> "tuple[str, str] | None | bool":
        """list-id (wins) or list title -> ("project"|"area", id); None if absent; False if unknown."""
        if "list-id" in params:
            cid = params["list-id"]
            it = self.items.get(cid)
            if it is not None and it["kind"] == "project" and not it["trashed"]:
                return ("project", cid)
            return ("area", cid) if cid in self.areas else False
        if "list" in params:
            title = params["list"]
            for it in self.items.values():
                if it["kind"] == "project" and it["title"] == title and not (it["logged"] or it["trashed"]):
                    return ("project", it["id"])
            for a in self.areas.values():
                if a["title"] == title:
                    return ("area", a["id"])
            return False
        return None

    def _resolve_area(self, params: dict) -> "str | None | bool":
        if "area-id" in params:
            return params["area-id"] if params["area-id"] in self.areas else False
        if "area" in params:
            return next((a["id"] for a in self.areas.values() if a["title"] == params["area"]), False)
        return None

    def _resolve_heading(self, project: str | None, params: dict) -> str | None:
        """heading-id (wins) or heading title within ``project``; None when absent or unknown."""
        if "heading-id" in params:
            h = self.headings.get(params["heading-id"])
            return h["id"] if h is not None and h["project"] == project else None
        if "heading" in params:
            return self._heading_by_title(project, params["heading"])
        return None

    def _heading_by_title(self, project: str | None, title: str) -> str | None:
        return next((h["id"] for h in self.headings.values()
                     if h["project"] == project and h["title"] == title), None)

    def _tag_by_title(self, title: str) -> dict:
        tag = next((t for t in self.tags.values() if t["title"] == title), None)
        if tag is None:
            raise ValueError(f"no tag {title!r}")
        return tag

    def _place(self, it: dict, kind: str, cid: str) -> None:
        if it["kind"] == "project":
            it["area"] = cid
            return
        if kind == "project":
            it.update(project=cid, area=None)
        else:
            it.update(area=cid, project=None)
        it["heading"] = None
        if it["start"] == "inbox":
            it["start"] = "anytime"

    # ---- json -------------------------------------------------------------------------------------

    def _cmd_json(self, command: str, params: dict, token: str | None) -> list[str]:
        try:
            data = json.loads(params.get("data", ""))
        except ValueError:
            self._error("json: data is not valid JSON")
            return []
        if not isinstance(data, list):
            self._error("json: data must be an array")
            return []
        if any(isinstance(o, dict) and o.get("operation") == "update" for o in data) \
                and token != self.auth_token:
            self._error("json: an update operation needs the auth-token; nothing changed")
            return []
        if self._json_voids(data):
            self.ignored.append("json: a malformed element voided the whole batch (TAPI §22)")
            return []
        created: list[str] = []
        for obj in data:
            kind, op = obj.get("type"), obj.get("operation", "create")
            attrs = self._json_params(obj.get("attributes") or {})
            if op == "update":
                it = self._target(obj.get("id"), kind, "json update")
                if it is not None:
                    self._apply_update(it, attrs)
            elif kind == "to-do":
                target = self._resolve_list(attrs)
                if target is False:
                    self.ignored.append("json: unknown list; the to-do went to the Inbox")
                    target = None
                it = self._new_item("to-do", attrs.get("title", ""))
                self._fill_todo(it, attrs, target)
                created.append(it["id"])
            else:
                created.append(self._json_project(attrs, obj.get("attributes") or {}))
        if created and _bool(params.get("reveal")):
            self.revealed.append(created[0])
        return created

    @staticmethod
    def _json_voids(data: list) -> bool:
        """One malformed element voids the whole batch, silently (TAPI §22, §22a): a top-level type other
        than to-do/project, a missing attributes object, or checklist items that aren't objects."""
        def bad(obj: object, types: tuple[str, ...]) -> bool:
            if not isinstance(obj, dict) or obj.get("type") not in types:
                return True
            attrs = obj.get("attributes")
            if not isinstance(attrs, dict):
                return True
            checklist = attrs.get("checklist-items")
            if checklist is not None and not (isinstance(checklist, list)
                                              and all(isinstance(c, dict) for c in checklist)):
                return True
            items = attrs.get("items") if obj["type"] == "project" else None
            if items is not None and (not isinstance(items, list)
                                      or any(bad(c, ("to-do", "heading")) for c in items)):
                return True
            return False
        return any(bad(obj, ("to-do", "project")) for obj in data)

    @staticmethod
    def _json_params(attrs: dict) -> dict:
        """json attributes -> the URL parameter shape (bools as "true"/"false"; lists kept)."""
        out: dict = {}
        for key, value in attrs.items():
            if value is None or key == "items":
                continue
            out[key] = ("true" if value else "false") if isinstance(value, bool) else value
        return out

    def _json_project(self, attrs: dict, raw: dict) -> str:
        area = self._resolve_area(attrs)
        if area is False:
            self.ignored.append("json: unknown area; the project has no area")
            area = None
        p = self._new_item("project", attrs.get("title", ""))
        if "notes" in attrs:
            p["notes"] = attrs["notes"]
        if area:
            p["area"] = area
        if "when" in attrs and not self._apply_when(p, attrs["when"]):
            self.ignored.append(f"when {attrs['when']!r} not understood")
        if "deadline" in attrs:
            self._apply_deadline(p, attrs["deadline"])
        if "tags" in attrs:
            p["tags"] = self._existing_tags(attrs["tags"])
        self.items[p["id"]] = p
        heading: dict | None = None
        under: dict[str, list[dict]] = {}
        for child in raw.get("items") or []:
            cattrs = self._json_params(child.get("attributes") or {})
            if child["type"] == "heading":
                heading = {"id": self.new_id(), "title": cattrs.get("title", ""), "project": p["id"],
                           "archived": False, "_want": _bool(cattrs.get("archived")) is True}
                self.headings[heading["id"]] = heading
                under[heading["id"]] = []
                continue
            for key in ("list", "list-id", "heading", "heading-id"):      # ignored inside items (OFF-URL)
                if cattrs.pop(key, None) is not None:
                    self.ignored.append(f"json: {key} inside a project's items")
            c = self._new_item("to-do", cattrs.get("title", ""))
            self._fill_todo(c, cattrs, ("project", p["id"]))
            if heading is not None:
                c["heading"] = heading["id"]
                under[heading["id"]].append(c)
        for hid, todos in under.items():    # archived: ignored unless all its to-dos are closed
            h = self.headings[hid]
            if h.pop("_want") and all(c["status"] != "open" for c in todos):
                h["archived"] = True
        self._apply_status(p, attrs, json_create=True)
        self._apply_dates(p, attrs)
        return p["id"]

    # ---- AppleScript collections ------------------------------------------------------------------

    def _app_todos(self) -> list[dict]:
        """App-level ``to dos``: not logged, not trashed (R-ASR §5.3); empty behind a sheet (MODALX1)."""
        if self.sheet:
            return []
        kinds = ("to-do", "project") if self.app_todos_include_projects else ("to-do",)
        return [it for it in self.items.values()
                if it["kind"] in kinds and not it["logged"] and not it["trashed"]]

    def _app_projects(self) -> list[dict]:
        if self.sheet:
            return []
        return [it for it in self.items.values()
                if it["kind"] == "project" and not it["logged"] and not it["trashed"]]

    def _list_key(self, key: str) -> str:
        """tb_listId (case-insensitive); list ids that don't resolve raise -1728."""
        canonical = next((k for k in BUILTIN_LISTS if _is(key, k)), None)
        if canonical is None:
            raise OsaError(9002, f"unknown list key: {key}")
        if canonical in self.missing_lists:
            raise OsaError(-1728, f"Things3 got an error: Can’t get list id \"{LIST_IDS[canonical]}\".")
        return canonical

    def _list_todos(self, key: str) -> list[dict]:
        key = self._list_key(key)
        kinds = ("to-do", "project") if self.list_todos_include_projects else ("to-do",)
        return self._order(key, [it for it in self.items.values() if it["kind"] in kinds and self._in_list(it, key)])

    def _list_projects(self, key: str) -> list[dict]:
        key = self._list_key(key)
        return self._order(key, [it for it in self.items.values()
                                 if it["kind"] == "project" and self._in_list(it, key)])

    def _in_list(self, it: dict, key: str) -> bool:
        if key == "trash":
            return it["trashed"]
        if it["trashed"]:
            return False
        if key == "logbook":
            return it["status"] != "open" and it["logged"]
        if it["logged"] or it["repeating"]:          # templates are invisible to list reads (TAPI §5e)
            return False
        today = self.today()
        start = it["start"]
        start_day = _date(start)
        dormant = False
        if it["kind"] == "to-do" and it["project"] and start == "anytime":
            p = self.items[it["project"]]
            p_day = _date(p["start"])
            dormant = p["start"] == "someday" or (p_day is not None and p_day > today)
        active = not dormant and (start == "anytime" or (start_day is not None and start_day <= today))
        deadline_day = _date(it["deadline"] or "")
        deadline_due = deadline_day is not None and deadline_day <= today
        if key == "inbox":
            return it["kind"] == "to-do" and start == "inbox"
        if key == "today":
            return (start_day is not None and start_day <= today) or deadline_due
        if key == "anytime":
            return active
        if key == "upcoming":
            return start_day is not None and start_day > today
        if key == "someday":
            return start == "someday"
        if key == "tomorrow":
            return start_day == today + timedelta(days=1)
        raise AssertionError(key)

    def _order(self, key: str, items: list[dict]) -> list[dict]:
        if key == "today":                   # This Evening at the bottom (OFF-LISTS)
            return sorted(items, key=lambda it: it["evening"])
        if key == "upcoming":
            return sorted(items, key=lambda it: it["start"])
        if key == "logbook":                 # newest first (OFF-CMD UI order)
            return sorted(items, key=lambda it: it["completed"] or it["canceled"] or datetime.min, reverse=True)
        return items

    def _project_children(self, pid: str) -> list[dict]:
        """``to dos of project``: every status, heading children flattened after the loose ones (LAB A11)."""
        kids = [it for it in self.items.values()
                if it["kind"] == "to-do" and it["project"] == pid and not it["trashed"]]
        order = [None] + [h["id"] for h in self.headings.values() if h["project"] == pid]
        return sorted(kids, key=lambda it: order.index(it["heading"]) if it["heading"] in order else 0)

    def _container(self, scope: str, key: str) -> list[dict]:
        """``every to do of (tb_container(scope, key))``."""
        if _is(scope, "list"):
            return self._list_todos(key)
        if _is(scope, "project"):
            if self.items.get(key, {}).get("kind") != "project":
                raise OsaError(-1728, f"Things3 got an error: Can’t get project id \"{key}\".")
            return self._project_children(key)
        if _is(scope, "area"):
            if key not in self.areas:
                raise OsaError(-1728, f"Things3 got an error: Can’t get area id \"{key}\".")
            return [it for it in self.items.values()
                    if it["kind"] == "to-do" and it["area"] == key and not it["trashed"]]
        raise OsaError(9002, f"unknown scope: {scope}")

    def _tag_names(self, obj: dict) -> str:
        return ", ".join(obj["tags"])

    def _container_ids(self, it: dict) -> tuple[str, str]:
        """tb_containerIds: a to-do in a project has no area of its own; a project has no project."""
        if it["kind"] == "project":
            return "", it["area"] or ""
        return it["project"] or "", "" if it["project"] else (it["area"] or "")

    # ---- AppleScript templates (§4.3) -------------------------------------------------------------

    def render(self, name: str, args: Sequence[str] = ()) -> bytes:
        """osascript's stdout for template ``name`` run with ``args`` (values *before* the "=" prefix).

        Raises ``OsaError`` where the script would fail (rc 1), ``KeyError`` for an unknown template.
        """
        t = TEMPLATES[name]
        args = [str(a) for a in args]
        if not self.installed:
            if t.uses_library:           # Things terminology doesn't compile without the app
                raise OsaError(-2741, "Expected end of line but found identifier.")
            raise OsaError(-10814, f"Can’t get application id \"{_BUNDLE}\".")    # kLSApplicationNotFoundErr
        if t.uses_library and not self.running:
            raise OsaError(9001, "Things 3 is not running")
        records = getattr(self, f"_render_{name.lower()}")(args)
        return (RS.join(records) + "\n").encode("utf-8")

    def frame(self, name: str, args: Sequence[str] = ()) -> Frame:
        return parse_frame(self.render(name, args), name)

    @staticmethod
    def _arg(args: list[str], i: int) -> str:
        if i > len(args):
            raise OsaError(-1728, f"Can’t get item {i} of argv.")
        return args[i - 1]

    def _gmt(self) -> str:
        return str(self.time_to_gmt())

    def _s_row(self, it: dict, want_notes: bool) -> str:
        return _rec("S", _t(it["id"]), _t(it["title"]), it["status"], _t(self._tag_names(it)),
                    _day(it["deadline"]), _t(it["notes"]) if want_notes else "")

    def _row_for(self, it: dict, want_notes: bool) -> tuple[str, str, str]:
        """tb_rowFor: (T record, project id, area id)."""
        pid, aid = self._container_ids(it)
        start = _day(it["start"])
        row = _rec("T", _t(it["id"]), it["kind"], _t(it["title"]), it["status"], _t(self._tag_names(it)),
                   _day(it["deadline"]), start, f"{start}T00:00:00" if start else "", _stamp(it["created"]),
                   _stamp(it["modified"]), _stamp(it["completed"]), _stamp(it["canceled"]), _t(pid), _t(aid),
                   _t(it["notes"]) if want_notes else "")
        return row, pid, aid

    def _container_rows(self, pids: list[str], aids: list[str]) -> list[str]:
        """tb_containerRows: R rows for projects (appending their areas), then for areas."""
        rows = []
        for pid in pids:
            p = self.items.get(pid)
            name, paid = (p["title"], p["area"] or "") if p is not None and p["kind"] == "project" else ("", "")
            if paid != "" and not _in(paid, aids):
                aids.append(paid)
            rows.append(_rec("R", "project", _t(pid), _t(name), _t(paid)))
        for aid in aids:
            name = self.areas[aid]["title"] if aid in self.areas else ""
            rows.append(_rec("R", "area", _t(aid), _t(name), ""))
        return rows

    def _render_scan(self, args: list[str]) -> list[str]:
        scope, key = self._arg(args, 1), self._arg(args, 2)
        want_notes = _is(self._arg(args, 3), "1")
        self._arg(args, 4)                   # slow: same output, per-item instead of column fetch
        out = [_rec("H", "tb1", "scan", scope, key, self._gmt())]
        if _is(scope, "app"):
            refs = self._app_todos()
        elif _is(scope, "projects"):
            refs = self._app_projects()
        else:
            refs = self._container(scope, key)
        out += [self._s_row(it, want_notes) for it in refs]
        out.append(_rec("E", str(len(refs))))
        return out

    def _render_rows(self, args: list[str]) -> list[str]:
        want_notes = _is(self._arg(args, 1), "1")
        out = [_rec("H", "tb1", "rows", self._gmt())]
        pids: list[str] = []
        aids: list[str] = []
        count = 0
        for id_ in args[1:]:
            it = self.items.get(id_)             # `to do id` or else `project id`: same T row (Q23)
            if it is not None:
                row, pid, aid = self._row_for(it, want_notes)
                out.append(row)
                _add_unique(pids, pid)
                _add_unique(aids, aid)
            elif id_ in self.areas:
                a = self.areas[id_]
                out.append(_rec("A", _t(id_), _t(a["title"]), _t(self._tag_names(a))))
            else:
                out.append(_rec("X", _t(id_)))
            count += 1
        out += self._container_rows(pids, aids)
        out.append(_rec("E", str(count)))
        return out

    def _render_logbook(self, args: list[str]) -> list[str]:
        limit = _as_integer(self._arg(args, 1))
        since_text = self._arg(args, 2)
        want_notes = _is(self._arg(args, 3), "1")
        since = None
        if since_text != "":
            day = _date(since_text)
            if day is None:
                raise OsaError(-1700, f"Can’t make \"{since_text[5:7]}\" into type integer.")
            since = datetime.combine(day, datetime.min.time())
        refs = self._list_todos("logbook")
        out = [_rec("H", "tb1", "logbook", str(len(refs)), since_text, self._gmt())]
        pids: list[str] = []
        aids: list[str] = []
        emitted = 0
        for it in refs:
            if emitted >= limit:
                break
            stop = it["completed"] or it["canceled"]
            if since is not None and stop is not None and stop < since:
                break
            row, pid, aid = self._row_for(it, want_notes)
            out.append(row)
            _add_unique(pids, pid)
            _add_unique(aids, aid)
            emitted += 1
        out += self._container_rows(pids, aids)
        out.append(_rec("E", str(emitted)))
        return out

    def _render_project_areas(self, args: list[str]) -> list[str]:
        refs = self._app_projects()
        out = [_rec("H", "tb1", "project-areas", self._gmt())]
        out += [_rec("PA", _t(p["id"]), _t(p["area"] or "")) for p in refs]
        out.append(_rec("E", str(len(refs))))
        return out

    def _render_areas(self, args: list[str]) -> list[str]:
        out = [_rec("H", "tb1", "areas", self._gmt())]
        out += [_rec("A", _t(a["id"]), _t(a["title"]), _t(self._tag_names(a))) for a in self.areas.values()]
        out.append(_rec("E", str(len(self.areas))))
        return out

    def _render_tags(self, args: list[str]) -> list[str]:
        out = [_rec("H", "tb1", "tags", self._gmt())]
        out += [_rec("G", _t(t["id"]), _t(t["title"]), _t(t["parent"] or ""), _t(t["shortcut"] or ""))
                for t in self.tags.values()]
        out.append(_rec("E", str(len(self.tags))))
        return out

    def _render_membership(self, args: list[str]) -> list[str]:
        out = [_rec("H", "tb1", "membership", self._gmt())]
        for key in args:
            ids = [it["id"] for it in self._list_todos(key)]
            pids = [it["id"] for it in self._list_projects(key)]
            out.append(_rec("M", key, *ids, *pids))
        out.append(_rec("E", str(len(args))))
        return out

    def _render_find_created(self, args: list[str]) -> list[str]:
        kind, title = self._arg(args, 1), self._arg(args, 2)
        max_age = _as_integer(self._arg(args, 3))
        scope, key = self._arg(args, 4), self._arg(args, 5)
        diag = _is(self._arg(args, 6), "1")
        now = self.now()
        out = [_rec("H", "tb1", "find-created", kind, scope, key, self._gmt())]
        if _is(kind, "project"):
            refs = [p for p in self._app_projects() if _is(p["title"], title)]
        elif _is(scope, "app"):
            refs = [it for it in self._app_todos() if _is(it["title"], title)]
        else:
            refs = [it for it in self._container(scope, key) if _is(it["title"], title)]
        hits = 0
        for it in refs:
            age = int((now - it["created"]).total_seconds())      # `div 1` truncates toward zero
            if it["title"] == title and -5 <= age <= max_age:    # `considering case`
                out.append(_rec("N", _t(it["id"]), str(age)))
                hits += 1
        if diag:
            out.append(_rec("D", str(len(self._app_todos())), str(len(self._list_todos("inbox")))))
        out.append(_rec("E", str(hits)))
        return out

    def _render_probe(self, args: list[str]) -> list[str]:
        out = [_rec("H", "tb1", "probe", self._gmt()), _rec("K", "version", "1", _t(self.version))]
        for key in BUILTIN_LISTS:
            try:
                out.append(_rec("K", "list:" + key, "1", _t(LIST_NAMES[key]), str(len(self._list_todos(key)))))
            except OsaError as e:
                out.append(_rec("K", "list:" + key, "0", _t(e.message), str(e.code)))
        if self._list_todos("logbook"):
            out.append(_rec("K", "range", "1", "1"))
        else:
            out.append(_rec("K", "range", "0", "Things3 got an error: Can’t get to dos 1 thru 1 of list id "
                                               "\"TMLogbookListSource\".", "-1719"))
        out.append(_rec("K", "list-projects", "1", str(len(self._list_projects("someday")))))
        n = str(len(self._list_todos("today")))
        out.append(_rec("K", "date-columns-aligned", "1", n, n, n))
        app = self._app_todos()
        out.append(_rec("K", "app-todos-include-projects", "1", str(len(app)),
                        str(sum(it["kind"] == "project" for it in app))))
        if len(args) >= 1:
            pid = args[0]
            it = self.items.get(pid)
            if it is not None and (it["kind"] == "to-do" or self.todo_id_resolves_projects):
                out.append(_rec("K", "todo-id-resolves-project", "1", "project" if it["kind"] == "project"
                                else "to do"))
            else:
                out.append(_rec("K", "todo-id-resolves-project", "0",
                                _t(f"Things3 got an error: Can’t get to do id \"{pid}\"."), "-1728"))
        k = sum(r.startswith("K" + US) for r in out)
        out.append(_rec("E", str(k)))
        return out

    def _render_automation(self, args: list[str]) -> list[str]:
        return [_rec("H", "tb1", "automation", self._gmt()), _rec("K", "automation", "1", str(len(self.areas))),
                _rec("E", "1")]

    def _render_app_version(self, args: list[str]) -> list[str]:
        return [_rec("H", "tb1", "app-version"),
                _rec("K", "app-version", "1", self.version, "1" if self.running else "0"), _rec("E", "1")]


_BUNDLE = "com.culturedcode.ThingsMac"


# ---- FakeRunner -------------------------------------------------------------------------------

@dataclass
class TemplateCall:
    name: str
    args: tuple[str, ...]
    timeout: float | None


@dataclass
class DispatchCall:
    turl: urlscheme.ThingsURL
    dry_run: bool
    outcome: str                    # "applied" | "dry-run" | "dropped" | "delayed" | "failed"
    locked: bool                    # write_lock() was held
    result: Dispatch | None


@dataclass
class _Injection:
    template: str | None
    match: Callable[[tuple[str, ...]], bool] | None
    exc: BaseException | None
    osa_code: int | None
    timeout: bool
    times: int


class FakeRunner(Runner):
    """``Runner`` over a ``FakeThings``. Records every call; never spawns a process.

    Mirrors ``MacRunner``'s observable behaviour: the same argument checks and URL re-validation
    (``runner._checked_url``/``_needs_token``), the token appended last and shown as ``***``, osascript
    failures turned into exceptions by ``runner.map_osa_error``, and ``NotMacOS`` when ``macos=False``.
    It doesn't keep a rate window unless given a ``runner.RateLimiter``; without one it only refuses a
    single URL heavier than the budget, as ``RateLimiter.acquire`` does.

    Failure injection: ``inject()`` for reads (osascript error numbers, timeouts, any exception; each is
    what ``run_template`` raises *after* MacRunner's own single retry), ``fail_next_dispatch()`` for
    writes, ``drop_next_url()`` (handed to LaunchServices but never applied), ``url_delay_s`` (applied
    once the clock has moved on that far) and ``sheet_mode`` (top-level app collections read empty).
    """

    def __init__(self, things: FakeThings | None = None, *, clock: Any = None, token: str | None = FAKE_TOKEN,
                 macos: bool = True, rate_limiter: Any = None):
        self.things = things if things is not None else FakeThings(clock)
        self.clock = clock if clock is not None else self.things.clock
        self.token = token                      # what authtoken.get_token() would return
        self.macos = macos
        self.rate_limiter = rate_limiter
        self.template_calls: list[TemplateCall] = []
        self.dispatch_calls: list[DispatchCall] = []
        self.launches = 0
        self.write_locks = 0
        self.url_delay_s = 0.0
        self._lock_depth = 0
        self._drop = 0
        self._pending: list[tuple[float, str]] = []
        self._injections: list[_Injection] = []
        self._dispatch_failures: list[BaseException] = []

    # ---- configuration --------------------------------------------------------------------------

    def inject(self, template: str | None = None, *, osa_code: int | None = None,
               exc: BaseException | None = None, timeout: bool = False, times: int = 1,
               match: Callable[[tuple[str, ...]], bool] | None = None) -> None:
        """Make the next ``times`` matching ``run_template`` calls fail (``template=None``: any)."""
        if (osa_code is not None) + (exc is not None) + bool(timeout) != 1:
            raise ValueError("give exactly one of osa_code, exc, timeout")
        if template is not None and template not in TEMPLATES:
            raise KeyError(template)
        self._injections.append(_Injection(template, match, exc, osa_code, timeout, times))

    def fail_next_dispatch(self, exc: BaseException) -> None:
        self._dispatch_failures.append(exc)

    def drop_next_url(self, n: int = 1) -> None:
        """The next ``n`` URLs are handed to LaunchServices (rc 0) but never reach Things."""
        self._drop += n

    @property
    def sheet_mode(self) -> bool:
        return self.things.sheet is not None

    @sheet_mode.setter
    def sheet_mode(self, on: bool) -> None:
        self.things.sheet = "dialog" if on else None

    def flush(self) -> None:
        """Apply every delayed URL now."""
        pending, self._pending = self._pending, []
        for _, url in pending:
            self.things.open_url(url)

    # ---- inspection -----------------------------------------------------------------------------

    @property
    def sent(self) -> list[urlscheme.ThingsURL]:
        return [c.turl for c in self.dispatch_calls if c.result is not None and c.result.sent]

    @property
    def sent_urls(self) -> list[str]:
        """Redacted URLs actually sent (``…&auth-token=***``)."""
        return [c.result.url_redacted for c in self.dispatch_calls if c.result is not None and c.result.sent]

    def calls(self, name: str | None = None) -> list[TemplateCall]:
        return [c for c in self.template_calls if name is None or c.name == name]

    # ---- Runner ---------------------------------------------------------------------------------

    def require_macos(self) -> None:
        if not self.macos:
            raise NotMacOS("things-bridge controls Things 3 on macOS; this system is linux",
                           details={"platform": "linux", "missing": []})

    def things_running(self) -> bool:
        self.require_macos()
        return self.things.running

    def ensure_running(self, *, allow_launch: bool) -> bool:
        if self.things_running():
            return False
        if not allow_launch:
            raise ThingsNotRunning("Things 3 is not running (auto-launch is disabled).")
        if not self.things.installed:
            raise runner_mod.map_open_error(1, "LSOpenURLsWithRole() failed with error -10814", running=False)
        self.things.running = True
        self.launches += 1
        return True

    def run_template(self, name: str, args: Sequence[str] = (), *, timeout: float | None = None) -> Frame:
        t = TEMPLATES[name]                     # KeyError: a programming error
        if isinstance(args, (str, bytes)):
            raise TypeError("args must be a sequence of str, not a single string")
        args = list(args)
        self.template_calls.append(TemplateCall(name, tuple(args), timeout))
        argv = encode_args(args)                # same validation, same order as MacRunner
        t.check_args(args)
        if timeout is None:
            slow = name == "SCAN" and len(args) == 4 and args[3] == "1"
            timeout = runner_mod.SLOW_SCAN_TIMEOUT_S if slow else t.timeout_s
        self.require_macos()
        self._apply_due()
        injection = self._take_injection(name, tuple(args))
        if injection is not None:
            if injection.exc is not None:
                raise injection.exc
            if injection.timeout:
                raise AutomationTimeout(f"Things didn't answer within {timeout:g} s ({name}).",
                                        hint="A macOS permission dialog may be waiting on screen, or Things is "
                                             "busy. Answer it and retry.")
            raise self._read_error(name, args, OsaError(injection.osa_code, "injected error").stderr)
        try:
            raw = self.things.render(name, [a[1:] for a in argv])     # tb_arg strips the "="
        except OsaError as e:
            raise self._read_error(name, args, e.stderr) from None
        return parse_frame(raw, name)

    def dispatch(self, turl: urlscheme.ThingsURL, *, dry_run: bool = False) -> Dispatch:
        base = runner_mod._checked_url(turl)    # Refused: foreign command, x-/undocumented/repeated params
        needs = runner_mod._needs_token(turl)
        if bool(turl.requires_token) != needs:
            raise Refused(f"Refusing to send a {turl.command} URL whose token requirement doesn't match its command.")
        sep = "&" if "?" in base else "?"
        shown = runner_mod.redact(base) + (f"{sep}auth-token=***" if needs else "")
        locked = self._lock_depth > 0
        if dry_run:
            result = Dispatch(turl.command, shown, sent=False, dry_run=True, foreground=turl.foreground,
                              t0=self.wall())
            self.dispatch_calls.append(DispatchCall(turl, True, "dry-run", locked, result))
            return result
        self.require_macos()
        url = base
        if needs:
            if not self.token:
                raise TokenMissing(f"No auth token is set up; Things requires one for {turl.command}.",
                                   hint="run `things-bridge token set` in Terminal")
            if not authtoken.TOKEN_RE.fullmatch(self.token):
                raise TokenMissing("The auth token isn't a valid Things URL token.")
            url = f"{base}{sep}auth-token={urlscheme.enc(self.token)}"      # always last
        if len(url.encode("utf-8")) > urlscheme.MAX_URL_BYTES:
            raise InvalidInput(f"The {turl.command} URL would be longer than {urlscheme.MAX_URL_BYTES} bytes.",
                               hint="split the request into smaller batches", details={"field": "url"})
        if turl.mutating:
            if self.rate_limiter is not None:
                self.rate_limiter.acquire(max(1, turl.weight))
            elif turl.weight > urlscheme.RATE_BUDGET:
                raise Refused(f"This request sends {turl.weight} items at once; things-bridge budgets "
                              f"{urlscheme.RATE_BUDGET} per {urlscheme.RATE_WINDOW_S:g} s.")
        if self._dispatch_failures:
            self.dispatch_calls.append(DispatchCall(turl, False, "failed", locked, None))
            raise self._dispatch_failures.pop(0)
        if not self.things.installed:
            self.dispatch_calls.append(DispatchCall(turl, False, "failed", locked, None))
            raise runner_mod.map_open_error(1, "LSOpenURLsWithRole() failed with error -10814", running=False)
        t0 = self.wall()
        result = Dispatch(turl.command, shown, sent=True, dry_run=False, foreground=turl.foreground, t0=t0)
        if self._drop:
            self._drop -= 1
            outcome = "dropped"
        elif self.url_delay_s > 0:
            self._pending.append((t0 + self.url_delay_s, url))
            outcome = "delayed"
        else:
            self.things.open_url(url)
            outcome = "applied"
        self.dispatch_calls.append(DispatchCall(turl, False, outcome, locked, result))
        return result

    @contextmanager
    def write_lock(self) -> Iterator[None]:
        """Re-entrant, like MacRunner's; dispatches record whether it was held."""
        self.write_locks += 1
        self._lock_depth += 1
        try:
            yield
        finally:
            self._lock_depth -= 1

    def wall(self) -> float:
        return self.clock.wall()

    def monotonic(self) -> float:
        return self.clock.monotonic()

    def sleep(self, s: float) -> None:
        self.clock.sleep(s)

    # ---- internals ------------------------------------------------------------------------------

    def _read_error(self, name: str, args: list[str], stderr: str) -> ThingsBridgeError:
        """What MacRunner raises for osascript rc != 0 (after its one retry)."""
        code, message = parse_osa_error(stderr)
        if code in runner_mod.DICTIONARY_CODES and name != "APP_VERSION" and not self.things.installed:
            return ThingsNotInstalled("Things 3 doesn't seem to be installed: its scripting dictionary is "
                                      "unavailable and its version can't be read.", details={"osa_code": code})
        return runner_mod.map_osa_error(code, message, running=self.things.running,
                                        target_id=runner_mod._target_id(name, args))

    def _apply_due(self) -> None:
        now = self.wall()
        due = [p for p in self._pending if p[0] <= now]
        self._pending = [p for p in self._pending if p[0] > now]
        for _, url in due:
            self.things.open_url(url)

    def _take_injection(self, name: str, args: tuple[str, ...]) -> _Injection | None:
        for inj in self._injections:
            if (inj.template is None or inj.template == name) and (inj.match is None or inj.match(args)):
                inj.times -= 1
                if inj.times <= 0:
                    self._injections.remove(inj)
                return inj
        return None
