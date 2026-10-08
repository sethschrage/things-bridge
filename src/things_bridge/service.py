"""High-level operations (§2.9, §3): reads, guarded writes and navigation.

* **Reads.** ``SCAN`` returns light rows; filtering, sorting and paging happen in Python; then ``ROWS``
  fetches full items for the page only.
* **Writes**, under ``runner.write_lock()``: validate -> resolve container titles to ids -> canonical tags ->
  snapshot the targets (``before``) -> guards -> build URLs with ``urlscheme``/``jsoncmd`` -> dispatch ->
  verify. Writes target ids only (N9). A write is never re-sent (N10): one that doesn't verify comes back
  with ``verified=False`` and the likely causes.
* ``Config.read_only`` refuses every write; navigation (``show``, ``open_search``) stays available.
"""

import difflib
import logging
import os
import re
import unicodedata
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Callable, Iterator, Literal, Mapping, Sequence

from . import applescript, jsoncmd, urlscheme
from .applescript import ScanRow
from .errors import Ambiguous, InvalidInput, NotFound, NotMacOS, ProtocolError, ReadFailed, Refused, \
    ThingsBridgeError
from .models import (Area, AreaList, Check, GetResult, Item, ItemKind, ListKey, NavResult, NotesMode, Page,
                     ProjectItemSpec, ProjectPatch, ProjectSpec, Status, TagList, TodoPatch, TodoSpec, WriteResult,
                     link_for)
from .runner import Dispatch, Runner
from .verify import (NOTHING_RESENT, Expect, Verifier, deadline_expect, dialog_reason, expectations_for_todo_patch,
                     not_verified_reason, read_failed_reason, when_expects)

log = logging.getLogger(__name__)

LIST_KEYS = ("inbox", "today", "anytime", "upcoming", "someday")
NOTES_MODES = ("none", "preview", "full")
STATUS_FILTERS = ("open", "all")
STATUSES = ("open", "completed", "canceled")
MAX_LIMIT = 200
MAX_BATCH_CAP = 100
MAX_PROJECT_ITEMS = jsoncmd.MAX_JSON_OBJECTS - 1        # the project itself counts too
MAX_QUERY_CHARS = 200
LOGBOOK_WINDOW = (10, 500)
MAX_WITHIN_DAYS = 365
ROWS_CHUNK = 200                                         # ROWS takes at most 200 ids
GUARD_FIELDS = frozenset({"when", "deadline", "completed", "canceled"})
GUARD_LISTS = ("inbox", "today", "anytime", "someday", "upcoming")
# SCAN errors the item-by-item slow path can't fix (environment, dialog, dictionary)
# -1708 is not here: Things can reject a form the bulk fast path uses while the item-by-item path still works.
_NO_SLOW_PATH = frozenset({-609, -1719, -128, -2740, -2741, -10814})
# Off macOS nothing can be looked up, and ID_RE also matches one-word titles ("Groceries"). Only values shaped like
# real Things ids (21/22-char base62, or a UUID) count as ids there; anything else may be a title (§5.5).
_OFFLINE_ID_RE = re.compile(r"^(?:[A-Za-z0-9]{20,24}|[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12})$")

ENV_NO_LAUNCH = "THINGS_BRIDGE_NO_LAUNCH"
ENV_READ_ONLY = "THINGS_BRIDGE_READ_ONLY"
ENV_MAX_BATCH = "THINGS_BRIDGE_MAX_BATCH"
ENV_VERIFY_TIMEOUT = "THINGS_BRIDGE_VERIFY_TIMEOUT"

REPEATING_REFUSAL = ("This item isn't visible in any Things list. It may be a repeating to-do's template, and "
                     "changing its schedule, deadline or status through automation is blocked by Things and can "
                     "crash it. Change it in Things; I can open it with things_show.")
UNKNOWN_TAG_WARNING = ("Tag '{}' doesn't exist in Things, so it was not applied (Things ignores unknown tags; this "
                       "tool can't create tags; create it in Things first)")
HEADING_WARNING = ("headings can't be read; if the heading doesn't exist Things files the to-do at the project root "
                   "(OFF-URL)")
REOPEN_WARNING = "Things will reopen the project '{}' (it is {}) when an open to-do goes into it"
OFFLINE_WARNING = "not validated against Things (not macOS)"
READ_ONLY_REFUSAL = "things-bridge is in read-only mode, so it won't change anything in Things."

# §3.6: what the policy makes impossible, with the message to give instead.
NOT_OFFERED: dict[str, str] = {
    "delete": ("I can't delete in Things (by design). I can cancel it (reversible; it goes to the Logbook), or open "
               "it with things_show so you can delete it yourself."),
    "create_tag": "Tag '{}' doesn't exist. Please create it in Things; then I can apply it.",
    "heading_in_existing_project": ("Headings can only be created with a new project. Add it in Things, then I can "
                                    "file to-dos under it."),
    "read_unreadable": "I can add checklist items but can't see existing ones.",
    "checklist_edit": "I can append or prepend checklist items only.",
    "move_to_inbox": ("Things' automation can't move items back to the Inbox. I can schedule it for Anytime or "
                      "Someday instead."),
    "repeating": "Repeating to-dos must be changed in Things. I can open it for you.",
    "app_only": "That's only available in the Things app.",
}


# ---- configuration --------------------------------------------------------------------------------

def _env_flag(env: Mapping[str, str], name: str) -> bool:
    raw = env.get(name, "").strip().lower()
    if raw in ("", "0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    raise InvalidInput(f"{name} must be 1 or 0, not {raw[:20]!r}", details={"field": name})


@dataclass
class Config:
    allow_launch: bool = True            # THINGS_BRIDGE_NO_LAUNCH=1 -> False
    verify: bool = True
    max_batch: int = 50                  # THINGS_BRIDGE_MAX_BATCH (<= 100)
    confirm_threshold: int = 10          # batches above this need confirm=True (R-PAL §4.12)
    verify_timeout_s: float = 6.0        # THINGS_BRIDGE_VERIFY_TIMEOUT
    read_only: bool = False              # THINGS_BRIDGE_READ_ONLY=1 / `mcp --read-only`

    def __post_init__(self) -> None:
        if not _is_int(self.max_batch) or not 1 <= self.max_batch <= MAX_BATCH_CAP:
            raise InvalidInput(f"max_batch must be 1..{MAX_BATCH_CAP}", details={"field": "max_batch"})
        if not _is_int(self.confirm_threshold) or self.confirm_threshold < 0:
            raise InvalidInput("confirm_threshold must be >= 0", details={"field": "confirm_threshold"})
        if isinstance(self.verify_timeout_s, bool) or not isinstance(self.verify_timeout_s, (int, float)) \
                or not 0 < self.verify_timeout_s <= 600:
            raise InvalidInput("verify_timeout_s must be > 0 and <= 600 seconds", details={"field": "verify_timeout_s"})

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        """Read §2.14's variables (``os.environ`` by default). Bad values raise ``InvalidInput``."""
        env = os.environ if env is None else env
        kwargs: dict = {"allow_launch": not _env_flag(env, ENV_NO_LAUNCH), "read_only": _env_flag(env, ENV_READ_ONLY)}
        raw = env.get(ENV_MAX_BATCH, "").strip()
        if raw:
            if not re.fullmatch(r"[0-9]{1,6}", raw) or int(raw) < 1:
                raise InvalidInput(f"{ENV_MAX_BATCH} must be a whole number >= 1", details={"field": ENV_MAX_BATCH})
            kwargs["max_batch"] = min(int(raw), MAX_BATCH_CAP)
        raw = env.get(ENV_VERIFY_TIMEOUT, "").strip()
        if raw:
            try:
                seconds = float(raw)
            except ValueError:
                seconds = -1.0
            if not 0 < seconds <= 600:
                raise InvalidInput(f"{ENV_VERIFY_TIMEOUT} must be seconds between 0 and 600",
                                   details={"field": ENV_VERIFY_TIMEOUT})
            kwargs["verify_timeout_s"] = seconds
        return cls(**kwargs)


# ---- small helpers ----------------------------------------------------------------------------------

def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _fail(field: str, problem: str, hint: str | None = None, **details: object) -> InvalidInput:
    return InvalidInput(f"{field}: {problem}", hint=hint, details={"field": field, **details})


def _choice(field: str, value: object, allowed: Sequence[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise _fail(field, f"must be one of {', '.join(allowed)}")
    return value


def _int_range(field: str, value: object, lo: int, hi: int) -> int:
    if not _is_int(value) or not lo <= value <= hi:
        raise _fail(field, f"must be a whole number from {lo} to {hi}")
    return value


def _flag(field: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise _fail(field, "must be true or false")
    return value


def _title(field: str, value: object) -> str:
    """A title to send: validated, surrounding whitespace dropped (so read-back can match it exactly)."""
    return urlscheme.check_title(field, value).strip()


def _joined_too_long(lines: Sequence[str]) -> bool:
    """True if newline-joined ``lines`` would pass the URL scheme's 4,000-character string limit."""
    return urlscheme.utf16_len("\n".join(lines)) > urlscheme.MAX_STRING_UTF16


def _clip(s: str, n: int = 60) -> str:
    return s if len(s) <= n else s[:n] + "…"


def _fold(s: str) -> str:
    """Case- and diacritic-insensitive form for search (casefold + NFKD without combining marks)."""
    return "".join(c for c in unicodedata.normalize("NFKD", s.casefold()) if not unicodedata.combining(c))


def _unique(rows: Sequence[ScanRow]) -> list[ScanRow]:
    """Dedupe by id, keeping the first occurrence (app-level ``to dos`` may include projects: R-ASR S10)."""
    seen: set[str] = set()
    out = []
    for r in rows:
        if r.id not in seen:
            seen.add(r.id)
            out.append(r)
    return out


def _by_status(rows: list[ScanRow], status: str) -> list[ScanRow]:
    return rows if status == "all" else [r for r in rows if r.status == "open"]


def _prefixed(prefix: str, fn: Callable[[], object]) -> object:
    """Run a validator; prefix the field of any ``InvalidInput`` (``todos[3].``) so batch errors say where."""
    if not prefix:
        return fn()
    try:
        return fn()
    except InvalidInput as e:
        where = prefix + str(e.details.get("field", ""))
        raise InvalidInput(f"{prefix}{e.message}", hint=e.hint, details={**e.details, "field": where}) from None


def _not_past(field: str, when: str | None, today: date) -> None:
    """Refuse a ``when`` day before today (R-PAL §4.7; also avoids the TAPI §1 crash branch)."""
    if when is None:
        return
    day = when.partition("@")[0].strip().lower()
    if urlscheme.DATE_RE.fullmatch(day):
        try:
            past = date.fromisoformat(day) < today
        except ValueError:
            return                              # not a real date: the builders refuse it
        if past:
            raise _fail(field, f"{day} is in the past", hint="use today or a future date")


def parse_since(value: str, *, today: date) -> date:
    """``YYYY-MM-DD`` | ``Nd`` | ``Nw`` -> a date (the logbook ``since`` parameter of the CLI and MCP tools)."""
    s = urlscheme.check_str("since", value).strip().lower()
    m = re.fullmatch(r"([0-9]{1,4})([dw])", s)
    if m:
        return today - timedelta(days=int(m[1]) * (7 if m[2] == "w" else 1))
    if urlscheme.DATE_RE.fullmatch(s):
        try:
            return date.fromisoformat(s)
        except ValueError:
            pass
    raise _fail("since", f"{_clip(s, 20)!r} is not YYYY-MM-DD, Nd or Nw (e.g. 7d, 2w)")


def _scope_for(container: "_Container | None", when: str | None, today: date) -> tuple[str, str]:
    """Where a new to-do should show up, for ``FIND_CREATED`` (§2.8 scope table)."""
    if container is not None:
        return container.kind, container.id
    if when is None:
        return "list", "inbox"
    day = urlscheme.resolve_day(when, today=today)
    if day is None:
        return "list", when.partition("@")[0].strip().lower()        # anytime | someday
    return ("list", "today") if day <= today.isoformat() else ("list", "upcoming")


# ---- per-call state -----------------------------------------------------------------------------------

@dataclass
class _Container:
    kind: Literal["project", "area"]
    id: str
    title: str
    status: str | None                  # a project's status; None for areas or when unknown


@dataclass
class _Create:
    """A to-do or project a URL creates, and what to check once ``FIND_CREATED`` finds it."""
    kind: ItemKind
    title: str
    scope: tuple[str, str]
    notes: str | None = None
    when: str | None = None
    deadline: str | None = None
    tags: list[str] | None = None
    project_id: str | None = None
    area_id: str | None = None
    children: list[str] | None = None   # a new project's to-do titles
    unverifiable: list[str] = field(default_factory=list)


class _Call:
    """One service call: its warnings, launch state and read caches. The service itself holds no mutable state,
    because MCP tool calls run concurrently in worker threads."""

    def __init__(self, svc: "ThingsService", *, offline: bool = False):
        self.svc = svc
        self.offline = offline           # dry run off macOS: validation reads are skipped
        self.launched = False
        self.warnings: list[str] = []
        self._projects: list[ScanRow] | None = None
        self._areas: list[Area] | None = None
        self._tags: list | None = None
        self._project_areas: dict[str, str | None] | None = None

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def projects(self) -> list[ScanRow]:
        if self._projects is None:
            self._projects = self.svc._scan("projects")
        return self._projects

    def areas(self) -> list[Area]:
        if self._areas is None:
            self._areas = applescript.areas_from_frame(self.svc.runner.run_template("AREAS"))
        return self._areas

    def tags(self) -> list:
        if self._tags is None:
            self._tags = applescript.tags_from_frame(self.svc.runner.run_template("TAGS"))
        return self._tags

    def project_areas(self) -> dict[str, str | None]:
        if self._project_areas is None:
            self._project_areas = applescript.project_areas_from_frame(self.svc.runner.run_template("PROJECT_AREAS"))
        return self._project_areas


# ---- the service ----------------------------------------------------------------------------------------

class ThingsService:
    def __init__(self, runner: Runner, config: Config | None = None, *, today: Callable[[], date] = date.today):
        self.runner = runner
        self.config = config if config is not None else Config()
        self._today = today

    def today(self) -> date:
        return self._today()

    # ==== reads (AppleScript) ============================================================================

    def list_items(self, list_key: ListKey, *, status: Literal["open", "all"] = "open", limit: int = 50,
                   offset: int = 0, notes: NotesMode = "preview") -> Page:
        key = _choice("list", list_key, LIST_KEYS)
        self._page_args(status=status, limit=limit, offset=offset, notes=notes)
        self._ready()
        rows = _by_status(self._scan("list", key), status)
        return self._page(rows, limit=limit, offset=offset, notes=notes)

    def logbook(self, *, since: date | str | None = None, limit: int = 50, notes: NotesMode = "preview") -> Page:
        if isinstance(since, str):
            since = parse_since(since, today=self.today())
        elif since is not None and not isinstance(since, date):
            raise _fail("since", "must be a date")
        self._page_args(limit=limit, notes=notes)
        self._ready()
        frame = self.runner.run_template("LOGBOOK", [str(limit), since.isoformat() if since else "",
                                                     "0" if notes == "none" else "1"])
        items = self._unique_items(applescript.items_from_frame(frame, notes_mode=notes))
        if since is None:
            total = int(frame.header[3]) if frame.header[3].isdigit() else None
            truncated = total is not None and total > len(items)
        else:
            total, truncated = None, len(items) >= limit
        return Page(items=items, returned=len(items), total=total, offset=0, truncated=truncated)

    def get(self, id: str, *, children: bool = False, notes: NotesMode = "full") -> GetResult:
        id = urlscheme.check_id("id", id)
        _flag("children", children)
        _choice("notes", notes, NOTES_MODES)
        self._ready()
        items, areas, _ = self._rows([id], notes=notes)
        if id in areas:
            page = self._area_children(id) if children else None
            return GetResult(kind="area", item=None, area=areas[id], children=page)
        if id in items:
            it = items[id]
            page = None
            if children and it.kind == "project":
                page = self._page(self._scan("project", id), limit=MAX_LIMIT, offset=0, notes="preview")
            return GetResult(kind=it.kind, item=it, area=None, children=page)
        raise NotFound(f"No item with id {id} (wrong id, or it was deleted).", details={"id": id})

    def projects(self, *, area: str | None = None, status: Literal["open", "all"] = "open", limit: int = 100,
                 offset: int = 0, notes: NotesMode = "none") -> Page:
        self._page_args(status=status, limit=limit, offset=offset, notes=notes)
        self._ready()
        ctx = _Call(self)
        rows = ctx.projects()
        if area is not None:
            aid = self._resolve_container(ctx, area=area).id
            pa = ctx.project_areas()
            rows = [r for r in rows if pa.get(r.id) == aid]
        return self._page(_by_status(rows, status), limit=limit, offset=offset, notes=notes)

    def areas(self) -> AreaList:
        self._ready()
        items = applescript.areas_from_frame(self.runner.run_template("AREAS"))
        return AreaList(items=items, returned=len(items))

    def tags(self) -> TagList:
        self._ready()
        items = applescript.tags_from_frame(self.runner.run_template("TAGS"))
        return TagList(items=items, returned=len(items))

    def search(self, query: str, *, include_completed: bool = False, logbook_window: int = 100, limit: int = 50,
               notes: NotesMode = "preview") -> Page:
        q = urlscheme.check_title("query", query).strip()
        if len(q) > MAX_QUERY_CHARS:
            raise _fail("query", f"must be at most {MAX_QUERY_CHARS} characters")
        _flag("include_completed", include_completed)
        _int_range("logbook_window", logbook_window, *LOGBOOK_WINDOW)
        self._page_args(limit=limit, notes=notes)
        self._ready()
        needle = _fold(q)
        rows = _unique(self._scan("app", notes=True) + self._scan("projects", notes=True))
        if not include_completed:
            rows = [r for r in rows if r.status == "open"]
        by_title = [r.id for r in rows if needle in _fold(r.title)]
        seen = set(by_title)
        order = by_title + [r.id for r in rows if r.id not in seen and needle in _fold(r.notes)]
        logged: dict[str, Item] = {}
        if include_completed:
            frame = self.runner.run_template("LOGBOOK", [str(logbook_window), "", "1"])
            seen = set(order)
            for it in applescript.items_from_frame(frame, notes_mode="full"):
                if it.id not in seen and it.id not in logged and \
                        (needle in _fold(it.title) or needle in _fold(it.notes or "")):
                    logged[it.id] = it
            order += list(logged)
        window = order[:limit]
        fetched, _, missing = self._rows([i for i in window if i not in logged], notes=notes)
        items = []
        for i in window:
            if i in logged:
                text, cut = applescript.notes_view(logged[i].notes or "", notes)
                items.append(replace(logged[i], notes=text, notes_truncated=cut))
            elif i in fetched:
                items.append(fetched[i])
        return Page(items=items, returned=len(items), total=len(order), offset=0, truncated=len(order) > len(window),
                    note=self._missing_note(missing))

    def find(self, *, tags: Sequence[str] = (), project: str | None = None, area: str | None = None,
             status: Literal["open", "all"] = "open", limit: int = 50, offset: int = 0,
             notes: NotesMode = "preview") -> Page:
        wanted = {t.strip().casefold() for t in urlscheme.check_tags("tags", tags)}
        if not wanted and project is None and area is None:
            raise InvalidInput("find needs at least one filter: tags, project or area", details={"field": "tags"})
        if project is not None and area is not None:
            raise _fail("area", "give project or area, not both")
        self._page_args(status=status, limit=limit, offset=offset, notes=notes)
        self._ready()
        ctx = _Call(self)
        if project is not None:
            rows = self._scan("project", self._resolve_container(ctx, project=project).id)
        elif area is not None:
            rows = self._scan("area", self._resolve_container(ctx, area=area).id)
        else:
            rows = _unique(self._scan("app") + ctx.projects())
        if wanted:
            rows = [r for r in rows if wanted & {t.casefold() for t in r.tags}]
        return self._page(_by_status(rows, status), limit=limit, offset=offset, notes=notes)

    def deadlines(self, *, within_days: int | None = 14, include_overdue: bool = True, include_projects: bool = True,
                  limit: int = 50) -> Page:
        if within_days is not None:
            _int_range("within_days", within_days, 0, MAX_WITHIN_DAYS)
        _flag("include_overdue", include_overdue)
        _flag("include_projects", include_projects)
        self._page_args(limit=limit)
        self._ready()
        rows = self._scan("app") + (self._scan("projects") if include_projects else [])
        today = self.today()
        last = (today + timedelta(days=within_days)).isoformat() if within_days is not None else None
        keep = []
        for r in _unique(rows):
            if r.status != "open" or r.deadline is None:
                continue
            if r.deadline < today.isoformat() and not include_overdue:
                continue
            if last is not None and r.deadline > last:
                continue
            keep.append(r)
        keep.sort(key=lambda r: (r.deadline, r.title.casefold()))
        return self._page(keep, limit=limit, offset=0, notes="preview")

    # ---- read plumbing --------------------------------------------------------------------------------

    def _ready(self) -> bool:
        """macOS check, then launch Things hidden if allowed. True if it launched Things."""
        self.runner.require_macos()
        return self.runner.ensure_running(allow_launch=self.config.allow_launch)

    @staticmethod
    def _page_args(*, status: str = "open", limit: int = 50, offset: int = 0, notes: str = "none") -> None:
        _choice("status", status, STATUS_FILTERS)
        _int_range("limit", limit, 1, MAX_LIMIT)
        _int_range("offset", offset, 0, 10**6)
        _choice("notes", notes, NOTES_MODES)

    def _scan(self, scope: str, key: str = "", *, notes: bool = False) -> list[ScanRow]:
        """``SCAN`` rows; the slow item-by-item path runs when the bulk column fetch fails twice (§4.3)."""
        args = [scope, key, "1" if notes else "0"]
        try:
            frame = self.runner.run_template("SCAN", [*args, "0"])
        except (ReadFailed, ProtocolError) as e:
            code = e.details.get("osa_code")
            if isinstance(e, ReadFailed) and (code is None or code in _NO_SLOW_PATH):
                raise
            log.warning("SCAN %s failed (%s); retrying item by item", scope, e.message)
            frame = self.runner.run_template("SCAN", [*args, "1"])
        return _unique(applescript.scan_rows(frame))

    def _rows(self, ids: Sequence[str], *, notes: NotesMode) -> tuple[dict[str, Item], dict[str, Area], list[str]]:
        """``ROWS`` in chunks: items and areas by id, and the ids Things doesn't know."""
        items: dict[str, Item] = {}
        areas: dict[str, Area] = {}
        missing: list[str] = []
        ids = list(dict.fromkeys(ids))
        for i in range(0, len(ids), ROWS_CHUNK):
            frame = self.runner.run_template("ROWS", ["0" if notes == "none" else "1", *ids[i:i + ROWS_CHUNK]])
            for it in applescript.items_from_frame(frame, notes_mode=notes):
                items.setdefault(it.id, it)
            for a in applescript.areas_from_frame(frame):
                areas.setdefault(a.id, a)
            missing += applescript.missing_ids(frame)
        return items, areas, missing

    def _page(self, rows: list[ScanRow], *, limit: int, offset: int, notes: NotesMode) -> Page:
        window = rows[offset:offset + limit]
        fetched, _, missing = self._rows([r.id for r in window], notes=notes) if window else ({}, {}, [])
        items = [fetched[r.id] for r in window if r.id in fetched]
        return Page(items=items, returned=len(items), total=len(rows), offset=offset,
                    truncated=offset + len(window) < len(rows), note=self._missing_note(missing))

    @staticmethod
    def _missing_note(missing: list[str]) -> str | None:
        if not missing:
            return None
        return f"{len(missing)} item(s) changed or disappeared while reading and were left out."

    @staticmethod
    def _unique_items(items: list[Item]) -> list[Item]:
        seen: set[str] = set()
        return [it for it in items if not (it.id in seen or seen.add(it.id))]

    def _area_children(self, aid: str) -> Page:
        """An area's projects, then its loose to-dos (``to dos of area`` holds only the latter)."""
        pa = applescript.project_areas_from_frame(self.runner.run_template("PROJECT_AREAS"))
        projects = [r for r in self._scan("projects") if pa.get(r.id) == aid]
        return self._page(_unique(projects + self._scan("area", aid)), limit=MAX_LIMIT, offset=0, notes="preview")

    # ==== writes (URL scheme) ============================================================================

    def add_todo(self, spec: TodoSpec, *, reveal: bool = False, dry_run: bool = False,
                 strict_tags: bool = False) -> WriteResult:
        self._check_writable()
        _flag("reveal", reveal)
        v = self._todo_values(spec)
        with self._writing(dry_run) as ctx:
            container = self._todo_container(ctx, v)
            tags = self._canonical_tags(ctx, v["tags"], strict=strict_tags) if v["tags"] else None
            if v["checklist"] and _joined_too_long(v["checklist"]):
                # one `checklist-items` string would pass 4,000 characters: json sends each item on its own
                turl = urlscheme.build_json([jsoncmd.Todo(
                    title=v["title"], notes=v["notes"], when=v["when"], deadline=v["deadline"], tags=tags or None,
                    checklist_items=[jsoncmd.ChecklistItem(t) for t in v["checklist"]],
                    list_id=container.id if container else None, heading=v["heading"])],
                    reveal=True if reveal else None)
            else:
                turl = urlscheme.build_add(title=v["title"], notes=v["notes"], when=v["when"],
                                           deadline=v["deadline"], tags=tags or None, checklist_items=v["checklist"],
                                           list_id=container.id if container else None, heading=v["heading"],
                                           reveal=True if reveal else None)
            dispatches, failure = self._dispatch(ctx, [turl], dry_run)
            create = self._todo_create(v, container, tags)
            return self._finish(ctx, "add_todo", dispatches, failure, creates=[create], update=False)

    def add_todos(self, specs: Sequence[TodoSpec], *, dry_run: bool = False, strict_tags: bool = False,
                  confirm: bool = False) -> WriteResult:
        self._check_writable()
        specs = urlscheme.check_seq("todos", specs)
        self._require_batch(len(specs), confirm, what="to-dos")
        values = [self._todo_values(s, prefix=f"todos[{i}].") for i, s in enumerate(specs)]
        with self._writing(dry_run) as ctx:
            todos, creates = [], []
            for v in values:
                container = self._todo_container(ctx, v)
                tags = self._canonical_tags(ctx, v["tags"], strict=strict_tags) if v["tags"] else None
                todos.append(jsoncmd.Todo(
                    title=v["title"], notes=v["notes"], when=v["when"], deadline=v["deadline"], tags=tags or None,
                    checklist_items=[jsoncmd.ChecklistItem(t) for t in v["checklist"]] if v["checklist"] else None,
                    list_id=container.id if container else None, heading=v["heading"]))
                creates.append(self._todo_create(v, container, tags))
            turl = urlscheme.build_json(todos)
            dispatches, failure = self._dispatch(ctx, [turl], dry_run)
            return self._finish(ctx, "add_todos", dispatches, failure, creates=creates, update=False)

    def add_project(self, spec: ProjectSpec, *, reveal: bool = False, dry_run: bool = False,
                    strict_tags: bool = False) -> WriteResult:
        self._check_writable()
        _flag("reveal", reveal)
        if not isinstance(spec, ProjectSpec):
            raise TypeError("spec must be a ProjectSpec")
        today = self.today()
        title = _title("title", spec.title)
        notes = (urlscheme.check_notes("notes", spec.notes) or None) if spec.notes is not None else None
        when = self._when(spec.when, spec.reminder_time)
        deadline = urlscheme.normalize_deadline(spec.deadline, today=today) if spec.deadline is not None else None
        tags = urlscheme.check_tags("tags", spec.tags) if spec.tags is not None else None
        items = self._project_items(spec.items)
        with self._writing(dry_run) as ctx:
            area = self._resolve_container(ctx, area=spec.area) if spec.area is not None else None
            if tags:
                tags = self._canonical_tags(ctx, tags, strict=strict_tags)
            for it in items:
                if it.get("tags"):
                    it["tags"] = self._canonical_tags(ctx, it["tags"], strict=strict_tags)
            plain = all(it["kind"] == "todo" and set(it) == {"kind", "title"} for it in items) \
                and not _joined_too_long([it["title"] for it in items])
            if plain:
                turl = urlscheme.build_add_project(title=title, notes=notes, when=when, deadline=deadline,
                                                   tags=tags or None, area_id=area.id if area else None,
                                                   to_dos=[it["title"] for it in items] or None,
                                                   reveal=True if reveal else None)
            else:
                children: list = []
                for it in items:
                    if it["kind"] == "heading":
                        children.append(jsoncmd.Heading(it["title"]))
                    else:
                        children.append(jsoncmd.Todo(
                            title=it["title"], notes=it.get("notes"), when=it.get("when"), deadline=it.get("deadline"),
                            tags=it.get("tags") or None,
                            checklist_items=[jsoncmd.ChecklistItem(t) for t in it.get("checklist", ())] or None))
                turl = urlscheme.build_json([jsoncmd.Project(
                    title=title, notes=notes, when=when, deadline=deadline, tags=tags or None,
                    area_id=area.id if area else None, items=children)], reveal=True if reveal else None)
            unverifiable = []
            if any(it["kind"] == "heading" for it in items):
                unverifiable.append("heading")
            if any(it.get("checklist") for it in items):
                unverifiable.append("checklist")
            create = _Create("project", title, ("app", ""), notes=notes, when=when, deadline=deadline,
                             tags=tags or None, area_id=area.id if area else None,
                             children=[it["title"] for it in items if it["kind"] == "todo"] or None,
                             unverifiable=unverifiable + self._when_unverifiable(when))
            dispatches, failure = self._dispatch(ctx, [turl], dry_run)
            return self._finish(ctx, "add_project", dispatches, failure, creates=[create], update=False)

    def update_todo(self, id: str, patch: TodoPatch, *, dry_run: bool = False,
                    strict_tags: bool = False) -> WriteResult:
        self._check_writable()
        if not isinstance(patch, TodoPatch):
            raise TypeError("patch must be a TodoPatch (use update_project for projects)")
        id = urlscheme.check_id("id", id)
        v = self._patch_values(patch)
        heading = urlscheme.check_title("heading", patch.heading) if patch.heading is not None else None
        if patch.project is not None and patch.area is not None:
            raise _fail("area", "move to a project or an area, not both")
        if heading is not None and patch.area is not None:
            raise _fail("heading", "headings only exist in projects; give project instead of area")
        if not v and patch.project is None and patch.area is None and heading is None:
            raise _fail("patch", "nothing to update")
        with self._writing(dry_run) as ctx:
            before = self._snapshot(ctx, [id], kind="to-do")
            container = None
            if patch.project is not None or patch.area is not None:
                container = self._resolve_container(ctx, project=patch.project, area=patch.area)
            if heading is not None:
                if container is None and before and before[0].project is None:
                    raise _fail("heading", "this to-do isn't in a project; give the project too")
                ctx.warn(HEADING_WARNING)
            if container is not None and before and before[0].status == "open":
                self._warn_reopen(ctx, container)
            self._guard_repeating(ctx, before, self._guarded_fields(v))
            tags, add_tags = self._patch_tags(ctx, patch, v, strict_tags)
            kw = self._update_kwargs(v, tags, add_tags)
            if container is not None:
                kw["list_id"] = container.id
            if heading is not None:
                kw["heading"] = heading
            urls = [urlscheme.build_update(id=id, **kw)] if kw else []
            if v.get("when") == "anytime":         # D10: undocumented on plain update; documented for json
                urls.append(urlscheme.build_json([jsoncmd.Todo(operation="update", id=id, when="anytime")]))
            if not urls:
                return self._nothing_sent(ctx, "update_todo", [id], before, dry_run)
            dispatches, failure = self._dispatch(ctx, urls, dry_run)
            resolved = self._resolved(v, tags, add_tags)
            if container is not None:
                resolved[f"{container.kind}_id"] = container.id
            expects, unverifiable = expectations_for_todo_patch(id, patch, resolved, before[0] if before else None,
                                                                self.today())
            return self._finish(ctx, "update_todo", dispatches, failure, ids=[id], before=before, expects=expects,
                                unverifiable=unverifiable)

    def update_project(self, id: str, patch: ProjectPatch, *, dry_run: bool = False,
                       strict_tags: bool = False) -> WriteResult:
        self._check_writable()
        if not isinstance(patch, ProjectPatch):
            raise TypeError("patch must be a ProjectPatch (use update_todo for to-dos)")
        id = urlscheme.check_id("id", id)
        v = self._patch_values(patch)
        if not v and patch.area is None:
            raise _fail("patch", "nothing to update")
        with self._writing(dry_run) as ctx:
            before = self._snapshot(ctx, [id], kind="project")
            area = self._resolve_container(ctx, area=patch.area) if patch.area is not None else None
            self._guard_repeating(ctx, before, self._guarded_fields(v))
            tags, add_tags = self._patch_tags(ctx, patch, v, strict_tags)
            kw = self._update_kwargs(v, tags, add_tags)
            if area is not None:
                kw["area_id"] = area.id
            urls = [urlscheme.build_update_project(id=id, **kw)] if kw else []
            if v.get("when") == "anytime":
                urls.append(urlscheme.build_json([jsoncmd.Project(operation="update", id=id, when="anytime")]))
            if not urls:
                return self._nothing_sent(ctx, "update_project", [id], before, dry_run)
            dispatches, failure = self._dispatch(ctx, urls, dry_run)
            resolved = self._resolved(v, tags, add_tags)
            if area is not None:
                resolved["area_id"] = area.id
            expects, unverifiable = expectations_for_todo_patch(id, patch, resolved, before[0] if before else None,
                                                                self.today())
            return self._finish(ctx, "update_project", dispatches, failure, ids=[id], before=before, expects=expects,
                                unverifiable=unverifiable)

    def set_status(self, ids: Sequence[str], status: Status, *, dry_run: bool = False,
                   confirm: bool = False) -> WriteResult:
        self._check_writable()
        status = _choice("status", status, STATUSES)
        ids = self._ids(ids)
        self._require_batch(len(ids), confirm)
        with self._writing(dry_run) as ctx:
            before = self._snapshot(ctx, ids, need_kind=True)
            self._guard_repeating(ctx, before, {"canceled" if status == "canceled" else "completed"})
            if status != "open":
                closing = {it.id for it in before if it.kind == "to-do"}     # sent before the projects, below
                self._guard_project_children(ctx, [it.id for it in before if it.kind == "project"], status,
                                             closing=closing)
            for it in before:
                if it.status == status:
                    ctx.warn(f"'{_clip(it.title)}' is already {status}")
            attrs = {"completed": {"completed": True}, "canceled": {"canceled": True},
                     "open": {"completed": False}}[status]          # completed=false also reopens canceled items
            ordered = sorted(before, key=lambda it: it.kind == "project")       # children first, then projects
            turl = urlscheme.build_json([self._json_update(it, **attrs) for it in ordered])
            dispatches, failure = self._dispatch(ctx, [turl], dry_run)
            expects = [Expect(it.id, "status", "eq", status) for it in before]
            return self._finish(ctx, "set_status", dispatches, failure, ids=ids, before=before, expects=expects)

    def schedule(self, ids: Sequence[str], *, when: str | None = None, reminder_time: str | None = None,
                 deadline: str | None = None, clear_deadline: bool = False, dry_run: bool = False,
                 confirm: bool = False) -> WriteResult:
        self._check_writable()
        _flag("clear_deadline", clear_deadline)
        if when is None and deadline is None and not clear_deadline:
            raise _fail("when", "schedule needs when, deadline or clear_deadline")
        if deadline is not None and clear_deadline:
            raise _fail("clear_deadline", "give deadline or clear_deadline, not both")
        today = self.today()
        w = self._when(when, reminder_time)
        d = urlscheme.normalize_deadline(deadline, today=today) if deadline is not None else None
        ids = self._ids(ids)
        self._require_batch(len(ids), confirm)
        with self._writing(dry_run) as ctx:
            before = self._snapshot(ctx, ids, need_kind=True)
            fields = ({"when"} if w else set()) | ({"deadline"} if d or clear_deadline else set())
            self._guard_repeating(ctx, before, fields)
            urls = []
            if w or d:
                urls.append(urlscheme.build_json([self._json_update(it, when=w, deadline=d) for it in before]))
            if clear_deadline:          # clearing is undocumented in json; the plain `deadline=` form is documented
                for it in before:
                    build = urlscheme.build_update if it.kind == "to-do" else urlscheme.build_update_project
                    urls.append(build(id=it.id, deadline=urlscheme.CLEAR))
            dispatches, failure = self._dispatch(ctx, urls, dry_run)
            expects: list[Expect] = []
            unverifiable = self._when_unverifiable(w)
            for it in before:
                if w:
                    expects += when_expects(it.id, w, today)[0]
                if d:
                    expects.append(deadline_expect(it.id, d, today))
                if clear_deadline:
                    expects.append(Expect(it.id, "deadline", "none", None))
            return self._finish(ctx, "schedule", dispatches, failure, ids=ids, before=before, expects=expects,
                                unverifiable=unverifiable)

    def move(self, ids: Sequence[str], *, project: str | None = None, area: str | None = None,
             heading: str | None = None, dry_run: bool = False, confirm: bool = False) -> WriteResult:
        self._check_writable()
        if project is None and area is None:
            raise _fail("project", "move needs a project or an area", hint=NOT_OFFERED["move_to_inbox"])
        if project is not None and area is not None:
            raise _fail("area", "move to a project or an area, not both")
        if heading is not None:
            heading = urlscheme.check_title("heading", heading)
            if area is not None:
                raise _fail("heading", "headings only exist in projects; give project instead of area")
        ids = self._ids(ids)
        self._require_batch(len(ids), confirm)
        with self._writing(dry_run) as ctx:
            before = self._snapshot(ctx, ids, need_kind=True)
            target = self._resolve_container(ctx, project=project, area=area)
            todos = [it for it in before if it.kind == "to-do"]
            projects = [it for it in before if it.kind == "project"]
            if projects and target.kind == "project":
                raise _fail("project", "a project can't go into another project; move it to an area",
                            ids=[it.id for it in projects])
            if heading is not None:
                ctx.warn(HEADING_WARNING)
            if any(it.status == "open" for it in todos):
                self._warn_reopen(ctx, target)
            urls = []
            if todos:
                urls.append(urlscheme.build_json([jsoncmd.Todo(operation="update", id=it.id, list_id=target.id,
                                                               heading=heading) for it in todos]))
            for it in projects:
                urls.append(urlscheme.build_update_project(id=it.id, area_id=target.id))
            dispatches, failure = self._dispatch(ctx, urls, dry_run)
            expects: list[Expect] = []
            for it in before:
                expects.append(Expect(it.id, f"{target.kind}_id", "eq", target.id))
                if it.kind == "to-do" and target.kind == "area":
                    expects.append(Expect(it.id, "project_id", "none", None))
            return self._finish(ctx, "move", dispatches, failure, ids=ids, before=before, expects=expects,
                                unverifiable=["heading"] if heading is not None else [])

    def tag(self, ids: Sequence[str], *, add: Sequence[str] | None = None, replace: Sequence[str] | None = None,
            dry_run: bool = False, confirm: bool = False) -> WriteResult:
        self._check_writable()
        if (add is None) == (replace is None):
            raise _fail("add", "give exactly one of add or replace")
        name = "add" if add is not None else "replace"
        wanted = urlscheme.check_tags(name, add if add is not None else replace)
        if add is not None and not wanted:
            raise _fail("add", "needs at least one tag")
        ids = self._ids(ids)
        self._require_batch(len(ids), confirm)
        with self._writing(dry_run) as ctx:
            before = self._snapshot(ctx, ids, need_kind=True)
            tags = self._canonical_tags(ctx, wanted, strict=True, field=name)     # the tag tool never drops
            urls = []
            for it in before:
                build = urlscheme.build_update if it.kind == "to-do" else urlscheme.build_update_project
                if add is not None:
                    urls.append(build(id=it.id, add_tags=tags))
                else:
                    urls.append(build(id=it.id, tags=tags or urlscheme.CLEAR))      # [] clears (documented)
            dispatches, failure = self._dispatch(ctx, urls, dry_run)
            op = "superset" if add is not None else "eq"
            expects = [Expect(it.id, "tags", op, list(tags)) for it in before]
            return self._finish(ctx, "tag", dispatches, failure, ids=ids, before=before, expects=expects)

    def raw_json(self, data: list[dict], *, reveal: bool = False, dry_run: bool = False,
                 confirm: bool = False) -> WriteResult:
        self._check_writable()
        _flag("reveal", reveal)
        items = jsoncmd.from_objs(data)
        for i, it in enumerate(items):          # titles are silently ignored when unknown (P28): ids only
            if isinstance(it, jsoncmd.Todo) and it.list is not None:
                raise _fail(f"/{i}/attributes/list", "list titles are refused; use list-id",
                            hint="get ids from things_projects or things_areas")
            if isinstance(it, jsoncmd.Project) and it.area is not None:
                raise _fail(f"/{i}/attributes/area", "area titles are refused; use area-id",
                            hint="get ids from things_areas")
        jsoncmd.validate(items, strict=True)
        today = self.today()
        for i, it in enumerate(items):
            _not_past(f"/{i}/attributes/when", it.when, today)
            for j, child in enumerate(getattr(it, "items", None) or ()):
                if isinstance(child, jsoncmd.Todo):
                    _not_past(f"/{i}/attributes/items/{j}/attributes/when", child.when, today)
        update_ids = list(dict.fromkeys(it.id for it in items if it.operation == "update"))
        if update_ids:
            self._require_batch(len(update_ids), confirm, what="update operations")
        with self._writing(dry_run) as ctx:
            before = self._snapshot(ctx, update_ids, need_kind=False) if update_ids else []
            kinds = {it.id: it.kind for it in before}
            for i, it in enumerate(items):
                want = "to-do" if isinstance(it, jsoncmd.Todo) else "project"
                if it.operation == "update" and it.id in kinds and kinds[it.id] != want:
                    raise _fail(f"/{i}/type", f"{it.id} is a {kinds[it.id]}, not a {want}")
            containers = self._json_containers(ctx, items)
            items = self._json_tags(ctx, items)
            if not items:
                return self._nothing_sent(ctx, "json", update_ids, before, dry_run)
            self._json_guards(ctx, items, before, containers)
            turl = urlscheme.build_json(items, reveal=True if reveal else None)
            dispatches, failure = self._dispatch(ctx, [turl], dry_run)
            creates, expects, unverifiable = self._json_checks(items, before, containers, today)
            targets = list(dict.fromkeys(it.id for it in items if it.operation == "update"))
            return self._finish(ctx, "json", dispatches, failure, ids=targets, before=before, creates=creates,
                                expects=expects, unverifiable=unverifiable)

    # ==== navigation (foreground) ========================================================================

    def show(self, *, id: str | None = None, list_key: str | None = None, query: str | None = None,
             filter_tags: Sequence[str] = ()) -> NavResult:
        if sum(x is not None for x in (id, list_key, query)) != 1:
            raise _fail("id", "show needs exactly one of id, list or query")
        tags = urlscheme.check_tags("filter_tags", filter_tags) or None
        if id is not None:
            turl = urlscheme.build_show(id=urlscheme.check_id("id", id), filter=tags)
        elif list_key is not None:
            key = _choice("list", list_key, urlscheme.SHOW_BUILTIN_IDS)
            turl = urlscheme.build_show(id=key, filter=tags)
        else:
            turl = urlscheme.build_show(query=urlscheme.check_title("query", query), filter=tags)
        return self._navigate(turl)

    def open_search(self, query: str | None = None) -> NavResult:
        q = urlscheme.check_title("query", query) if query is not None else None
        return self._navigate(urlscheme.build_search(query=q))

    def _navigate(self, turl: urlscheme.ThingsURL) -> NavResult:
        self._ready()                   # launch hidden first (or refuse under no-launch); `show` then foregrounds
        d = self.runner.dispatch(turl)
        return NavResult(sent=d.sent, url=d.url_redacted)

    # ==== write plumbing =================================================================================

    def _check_writable(self) -> None:
        if self.config.read_only:
            raise Refused(READ_ONLY_REFUSAL, hint=f"unset {ENV_READ_ONLY}, or start the MCP server without --read-only")

    @contextmanager
    def _writing(self, dry_run: bool) -> Iterator[_Call]:
        """Hold the write lock (not for dry runs) for validation reads, guards, dispatch and verification, so the
        checks still hold when the URL goes out. A dry run off macOS skips every validation read (§5.5)."""
        _flag("dry_run", dry_run)
        offline = False
        try:
            self.runner.require_macos()             # before the lock: off macOS no state file is created
        except NotMacOS:
            if not dry_run:
                raise
            offline = True
        with nullcontext() if dry_run else self.runner.write_lock():
            ctx = _Call(self)
            if offline:
                ctx.offline = True
                ctx.warn(OFFLINE_WARNING)
            else:
                ctx.launched = self.runner.ensure_running(allow_launch=self.config.allow_launch)
            yield ctx

    def _ids(self, ids: Sequence[str]) -> list[str]:
        values = urlscheme.check_seq("ids", ids)
        return list(dict.fromkeys(urlscheme.check_id(f"ids[{i}]", x) for i, x in enumerate(values)))

    def _require_batch(self, n: int, confirm: bool, *, what: str = "items") -> None:
        _flag("confirm", confirm)
        if n == 0:
            raise _fail("ids", f"needs at least one of the {what}")
        if n > self.config.max_batch:
            raise Refused(f"{n} {what} is more than the batch limit of {self.config.max_batch}.",
                          hint="split it into smaller batches",
                          details={"count": n, "max_batch": self.config.max_batch})
        if n > self.config.confirm_threshold and not confirm:
            raise Refused(f"This changes {n} {what} at once, which needs confirm=true.",
                          hint="re-run with confirm=true after showing the user the list",
                          details={"count": n, "confirm_threshold": self.config.confirm_threshold})

    def _when(self, when: str | None, reminder_time: str | None) -> str | None:
        """Canonical ``when`` (+ reminder) to send; past dates and a reminder without a day are refused."""
        if when is None:
            if reminder_time is not None:
                raise _fail("reminder_time", "needs a when (e.g. today or a date)")
            return None
        return urlscheme.compose_when(when, reminder_time, today=self.today())

    @staticmethod
    def _when_unverifiable(when: str | None) -> list[str]:
        return when_expects("", when, date.min)[1] if when else []

    def _todo_values(self, spec: TodoSpec, *, prefix: str = "") -> dict:
        """Validated, normalised ``TodoSpec`` fields (no reads yet)."""
        if not isinstance(spec, TodoSpec):
            raise TypeError("each to-do must be a TodoSpec")
        today = self.today()

        def run() -> dict:
            v = {"title": _title("title", spec.title),
                 "notes": (urlscheme.check_notes("notes", spec.notes) or None) if spec.notes is not None else None,
                 "when": self._when(spec.when, spec.reminder_time),
                 "deadline": urlscheme.normalize_deadline(spec.deadline, today=today)
                 if spec.deadline is not None else None,
                 "tags": urlscheme.check_tags("tags", spec.tags) if spec.tags is not None else None,
                 "checklist": urlscheme.check_lines("checklist", spec.checklist, max_items=urlscheme.MAX_CHECKLIST)
                 if spec.checklist else None,
                 "project": spec.project, "area": spec.area,
                 "heading": urlscheme.check_title("heading", spec.heading) if spec.heading is not None else None}
            if spec.project is not None and spec.area is not None:
                raise _fail("area", "give project or area, not both")
            if v["heading"] is not None and spec.project is None:
                raise _fail("heading", "a heading needs its project")
            return v
        return _prefixed(prefix, run)

    def _todo_container(self, ctx: _Call, v: dict) -> "_Container | None":
        if v["project"] is None and v["area"] is None:
            return None
        container = self._resolve_container(ctx, project=v["project"], area=v["area"])
        self._warn_reopen(ctx, container)
        if v["heading"] is not None:
            ctx.warn(HEADING_WARNING)
        return container

    def _todo_create(self, v: dict, container: "_Container | None", tags: list[str] | None) -> _Create:
        unverifiable = self._when_unverifiable(v["when"])
        if v["checklist"]:
            unverifiable.append("checklist")
        if v["heading"]:
            unverifiable.append("heading")
        return _Create("to-do", v["title"], _scope_for(container, v["when"], self.today()), notes=v["notes"],
                       when=v["when"], deadline=v["deadline"], tags=tags or None,
                       project_id=container.id if container and container.kind == "project" else None,
                       area_id=container.id if container and container.kind == "area" else None,
                       unverifiable=unverifiable)

    def _project_items(self, items: Sequence[ProjectItemSpec] | None) -> list[dict]:
        if items is None:
            return []
        items = urlscheme.check_seq("items", items)
        if len(items) > MAX_PROJECT_ITEMS:
            raise _fail("items", f"has {len(items)} entries; the limit is {MAX_PROJECT_ITEMS}",
                        hint="create the project, then add the rest with things_add_todos")
        today = self.today()
        out = []
        for i, spec in enumerate(items):
            if not isinstance(spec, ProjectItemSpec):
                raise TypeError("each project item must be a ProjectItemSpec")

            def run(spec: ProjectItemSpec = spec) -> dict:
                kind = _choice("kind", spec.kind, ("todo", "heading"))
                v: dict = {"kind": kind, "title": _title("title", spec.title)}
                extra = {k: getattr(spec, k) for k in ("notes", "when", "deadline", "tags", "checklist")
                         if getattr(spec, k) is not None}
                if kind == "heading":
                    if extra:
                        raise _fail(min(extra), "a heading only has a title")
                    return v
                if spec.notes:
                    v["notes"] = urlscheme.check_notes("notes", spec.notes)
                if spec.when is not None:
                    v["when"] = urlscheme.compose_when(spec.when, None, today=today)
                if spec.deadline is not None:
                    v["deadline"] = urlscheme.normalize_deadline(spec.deadline, today=today)
                if spec.tags:
                    v["tags"] = urlscheme.check_tags("tags", spec.tags)
                if spec.checklist:
                    v["checklist"] = urlscheme.check_lines("checklist", spec.checklist,
                                                           max_items=urlscheme.MAX_CHECKLIST)
                return v
            out.append(_prefixed(f"items[{i}].", run))
        return out

    def _patch_values(self, patch: TodoPatch | ProjectPatch) -> dict:
        """Validated, normalised patch fields that were given (no reads yet); moves are handled by the caller."""
        today = self.today()
        v: dict = {}
        if patch.title is not None:
            v["title"] = _title("title", patch.title)
        if patch.notes is not None:
            v["notes"] = urlscheme.check_notes("notes", patch.notes)       # "" clears
        for key in ("prepend_notes", "append_notes"):
            value = getattr(patch, key)
            if value:
                v[key] = urlscheme.check_notes(key, value)
        if "notes" in v and ("prepend_notes" in v or "append_notes" in v):
            raise _fail("notes", "replace the notes or prepend/append to them, not both")
        when = self._when(patch.when, patch.reminder_time)
        if when is not None:
            v["when"] = when
        _flag("clear_deadline", patch.clear_deadline)
        if patch.deadline is not None and patch.clear_deadline:
            raise _fail("clear_deadline", "give deadline or clear_deadline, not both")
        if patch.deadline is not None:
            v["deadline"] = urlscheme.normalize_deadline(patch.deadline, today=today)
        if patch.clear_deadline:
            v["clear_deadline"] = True
        if patch.tags is not None and patch.add_tags is not None:
            raise _fail("add_tags", "replace the tags or add to them, not both")
        if patch.tags is not None:
            v["tags"] = urlscheme.check_tags("tags", patch.tags)            # [] clears
        if patch.add_tags:
            v["add_tags"] = urlscheme.check_tags("add_tags", patch.add_tags)
        for key in ("checklist_append", "checklist_prepend"):
            value = getattr(patch, key, None)
            if value:
                v[key] = urlscheme.check_lines(key, value, max_items=urlscheme.MAX_CHECKLIST)
        return v

    @staticmethod
    def _guarded_fields(v: dict) -> set[str]:
        return ({"when"} if "when" in v else set()) | \
            ({"deadline"} if "deadline" in v or "clear_deadline" in v else set())

    def _patch_tags(self, ctx: _Call, patch: TodoPatch | ProjectPatch, v: dict,
                    strict: bool) -> tuple[list[str] | None, list[str] | None]:
        """Canonical (tags, add_tags) to send; None = don't send. Only an explicit ``tags=[]`` clears: a replacement
        whose tags are all unknown is skipped, never turned into a clear."""
        tags = add_tags = None
        if "tags" in v:
            tags = self._canonical_tags(ctx, v["tags"], strict=strict, field="tags") if v["tags"] else []
            if v["tags"] and not tags:
                ctx.warn("None of the requested tags exist, so the tags were left unchanged.")
                tags = None
        if "add_tags" in v:
            add_tags = self._canonical_tags(ctx, v["add_tags"], strict=strict, field="add_tags") or None
        return tags, add_tags

    @staticmethod
    def _resolved(v: dict, tags: list[str] | None, add_tags: list[str] | None) -> dict:
        """The values an update actually sends, for ``expectations_for_todo_patch``."""
        return {"title": v.get("title"), "when": v.get("when"), "deadline": v.get("deadline"), "tags": tags,
                "add_tags": add_tags}

    @staticmethod
    def _update_kwargs(v: dict, tags: list[str] | None, add_tags: list[str] | None) -> dict:
        kw: dict = {k: v[k] for k in ("title", "notes", "prepend_notes", "append_notes", "deadline") if k in v}
        if v.get("when") not in (None, "anytime"):
            kw["when"] = v["when"]
        if v.get("clear_deadline"):
            kw["deadline"] = urlscheme.CLEAR
        if tags is not None:
            kw["tags"] = tags or urlscheme.CLEAR
        if add_tags:
            kw["add_tags"] = add_tags
        if v.get("checklist_append"):
            kw["append_checklist_items"] = v["checklist_append"]
        if v.get("checklist_prepend"):
            kw["prepend_checklist_items"] = v["checklist_prepend"]
        return kw

    @staticmethod
    def _json_update(it: Item, **attrs: object) -> jsoncmd.JsonItem:
        cls = jsoncmd.Todo if it.kind == "to-do" else jsoncmd.Project
        return cls(operation="update", id=it.id, **attrs)

    # ---- resolution, tags, snapshots, guards ---------------------------------------------------------

    def _resolve_container(self, ctx: _Call, *, project: str | None = None, area: str | None = None) -> _Container:
        """A project/area given by id or exact title -> its id (N9, D7).

        An id-shaped value that ``ROWS`` resolves wins; otherwise titles match exactly, case-insensitively,
        against open projects (``SCAN(projects)``) or areas (``AREAS``). None -> ``NotFound`` with up to five
        ``difflib`` suggestions; several -> ``Ambiguous`` with the candidates.
        """
        if (project is None) == (area is None):
            raise _fail("project", "give either a project or an area")
        kind: Literal["project", "area"] = "project" if project is not None else "area"
        value = urlscheme.check_title(kind, project if project is not None else area).strip()
        if ctx.offline:
            if _OFFLINE_ID_RE.fullmatch(value):
                return _Container(kind, value, "", None)
            self.runner.require_macos()             # a title needs a read: NotMacOS (§5.5)
        if urlscheme.ID_RE.fullmatch(value):
            items, areas, _ = self._rows([value], notes="none")
            if value in items:
                it = items[value]
                if kind == "project" and it.kind == "project":
                    return _Container("project", it.id, it.title, it.status)
                raise _fail(kind, f"{value} is a {it.kind}, not {'a project' if kind == 'project' else 'an area'}")
            if value in areas:
                if kind == "area":
                    return _Container("area", value, areas[value].title, None)
                raise _fail("project", f"{value} is an area; pass it as area")
        if kind == "project":
            candidates = [(r.id, r.title) for r in ctx.projects() if r.status == "open"]
        else:
            candidates = [(a.id, a.title) for a in ctx.areas()]
        folded = value.casefold()
        matches = [c for c in candidates if c[1].strip().casefold() == folded]
        if len(matches) == 1:
            cid, title = matches[0]
            return _Container(kind, cid, title, "open" if kind == "project" else None)
        if matches:
            raise self._ambiguous(ctx, kind, value, matches)
        if folded == "inbox":
            raise Refused(NOT_OFFERED["move_to_inbox"], details={"field": kind},
                          hint="to add a new to-do to the Inbox, leave project and area empty")
        titles = list(dict.fromkeys(t for _, t in candidates))
        by_fold = {t.casefold(): t for t in titles}
        close = difflib.get_close_matches(folded, list(by_fold), n=5, cutoff=0.6)
        near = [by_fold[c] for c in close] + [t for t in titles if folded in t.casefold()]
        suggestions = list(dict.fromkeys(near))[:5]
        what = "open project" if kind == "project" else "area"
        hint = ("Did you mean " + ", ".join(repr(s) for s in suggestions) + "?") if suggestions else \
            f"list them with things_{'projects' if kind == 'project' else 'areas'}"
        raise NotFound(f"No {what} named '{_clip(value)}'.", hint=hint,
                       details={"field": kind, "suggestions": suggestions})

    def _ambiguous(self, ctx: _Call, kind: str, value: str, matches: list[tuple[str, str]]) -> Ambiguous:
        candidates = []
        if kind == "project":
            pa = ctx.project_areas()
            area_titles = {a.id: a.title for a in ctx.areas()}
            for cid, title in matches:
                aid = pa.get(cid)
                candidates.append({"id": cid, "title": title,
                                   "area": {"id": aid, "title": area_titles.get(aid, "")} if aid else None})
        else:
            candidates = [{"id": cid, "title": title, "area": None} for cid, title in matches]
        listing = "; ".join(f"{c['id']}" + (f" (in {c['area']['title']})" if c["area"] else "") for c in candidates)
        what = "open projects" if kind == "project" else "areas"
        return Ambiguous(f"{len(candidates)} {what} are named '{_clip(value)}'.",
                         hint=f"pass the id instead: {listing}", details={"field": kind, "candidates": candidates})

    def _canonical_tags(self, ctx: _Call, tags: Sequence[str], *, strict: bool, field: str = "tags") -> list[str]:
        """Requested tags -> the stored titles of existing tags (case-insensitive). Unknown ones are dropped with
        a warning, or refused with ``strict`` (Things ignores unknown tags silently: OFF-URL, P6)."""
        wanted = [t.strip() for t in urlscheme.check_tags(field, tags)]
        if ctx.offline:
            return list(dict.fromkeys(wanted))
        titles = [t.title for t in ctx.tags()]
        exact = set(titles)
        by_fold: dict[str, str] = {}
        for t in titles:
            by_fold.setdefault(t.casefold(), t)
        out: list[str] = []
        for t in wanted:
            title = t if t in exact else by_fold.get(t.casefold())
            if title is None:
                if strict:
                    raise InvalidInput(NOT_OFFERED["create_tag"].format(_clip(t)),
                                       hint="things_tags lists the existing tags", details={"field": field, "tag": t})
                ctx.warn(UNKNOWN_TAG_WARNING.format(_clip(t)))
                continue
            if title not in out:
                out.append(title)
        return out

    def _snapshot(self, ctx: _Call, ids: Sequence[str], *, kind: ItemKind | None = None,
                  need_kind: bool = False) -> list[Item]:
        """``before``: the targets as they are now (full notes, for undo). Missing ids -> ``NotFound``; areas and
        the wrong kind -> ``InvalidInput``. Off macOS a dry run skips this, unless the kinds are needed to build
        the URLs (then ``NotMacOS``)."""
        if ctx.offline:
            if need_kind:
                self.runner.require_macos()
            return []
        items, areas, missing = self._rows(ids, notes="full")
        if missing:
            raise NotFound(f"No item with id {', '.join(missing[:5])} (wrong id, or it was deleted).",
                           details={"ids": missing})
        for i in ids:
            if i in areas:
                raise _fail("ids", f"{i} is an area; areas can't be changed through Things' URL scheme",
                            hint=NOT_OFFERED["app_only"])
            if kind is not None and items[i].kind != kind:
                other = "update_project" if items[i].kind == "project" else "update_todo"
                raise _fail("id", f"{i} is a {items[i].kind}; use {other}")
        return [items[i] for i in ids]

    def _guard_repeating(self, ctx: _Call, items: Sequence[Item], fields: set[str], *,
                         per_item: dict[str, set[str]] | None = None) -> None:
        """N11: refuse when/deadline/status writes to an open item that no list shows (a possible repeating
        template: TAPI §1 crash). Upcoming alone passes only with a future start date (templates rest with none).
        ``per_item`` (id -> fields) overrides ``fields`` for the ids it names.

        Children of a *dormant* project (in Someday, or in Upcoming with a future start date, and in neither
        Today nor Anytime) are hidden from every list too, even when they are ordinary to-dos (lists-in-depth:
        "While they slumber in Someday, neither to-dos nor projects will show up in Anytime or Upcoming"). They
        can't be told apart from a template there, so they pass for deadline/completed/canceled (on a template
        Things ignores those, and verification reports it) but ``when`` (the TAPI §1 crash) is still refused."""
        per_item = per_item or {}
        wanted = {it.id: per_item.get(it.id, fields) & GUARD_FIELDS for it in items}
        if ctx.offline or not any(wanted.values()):
            return
        open_items = [it for it in items if it.status == "open" and wanted[it.id]]
        if not open_items:
            return
        frame = self.runner.run_template("MEMBERSHIP", list(GUARD_LISTS))
        members = {k: set(v) for k, v in applescript.membership_from_frame(frame).items()}
        visible = set().union(*(members.get(k, set()) for k in ("inbox", "today", "anytime", "someday")))
        today = self.today().isoformat()
        hidden = [it for it in open_items
                  if it.id not in visible
                  and not (it.id in members.get("upcoming", set()) and it.start_date and it.start_date > today)]
        dormant_candidates = {it.project.id for it in hidden if it.project is not None and "when" not in wanted[it.id]}
        active = members.get("today", set()) | members.get("anytime", set())
        dormant = {pid for pid in dormant_candidates if pid in members.get("someday", set()) and pid not in active}
        upcoming = [pid for pid in dormant_candidates - dormant
                    if pid in members.get("upcoming", set()) and pid not in active]
        if upcoming:
            found, _, _ = self._rows(upcoming, notes="none")
            dormant |= {pid for pid, p in found.items()
                        if p.kind == "project" and p.start_date and p.start_date > today}
        hidden = [it for it in hidden
                  if not (it.project is not None and it.project.id in dormant and "when" not in wanted[it.id])]
        if hidden:
            listing = ", ".join(f"'{_clip(it.title, 40)}' ({it.id})" for it in hidden[:10])
            hint = f"{NOT_OFFERED['repeating']} Affected: {listing}"
            if any(it.project is not None for it in hidden):
                hint += (" (A to-do inside a Someday or future-scheduled project is hidden from every list as "
                         "well, so its start date can't be changed through automation either.)")
            raise Refused(REPEATING_REFUSAL, hint=hint, details={"ids": [it.id for it in hidden]})

    def _guard_project_children(self, ctx: _Call, pids: Sequence[str], status: str, *,
                                closing: set[str] = frozenset(), is_open: dict[str, bool] | None = None,
                                moved: dict[str, str] | None = None,
                                incoming: Sequence[dict] = ()) -> None:
        """N12: completing/canceling a project with open to-dos is ignored (OFF-URL) or cascades (TAPI §3); refuse.
        To-dos in ``closing`` are closed earlier in the same URL, so they don't count. For a json payload the
        state the earlier operations leave behind is passed in: ``is_open`` (to-do id -> open after them),
        ``moved`` (to-do id -> its new ``list-id``) and ``incoming`` (open to-dos created in or moved into the
        project, as ``{"id", "title", "project"}``)."""
        if ctx.offline or status not in ("completed", "canceled") or not pids:
            return
        is_open = is_open or {}
        moved = moved or {}
        open_children = [{"id": r.id, "title": r.title, "project": pid}
                         for pid in pids for r in self._scan("project", pid)
                         if r.id not in closing and is_open.get(r.id, r.status == "open")
                         and moved.get(r.id, pid) == pid]
        seen = {c["id"] for c in open_children}
        open_children += [c for c in incoming if c["project"] in pids and (c["id"] is None or c["id"] not in seen)]
        if open_children:
            raise Refused(f"The project still has {len(open_children)} open to-do(s), so it can't be {status} yet.",
                          hint="complete or cancel these first (things_set_status), then the project",
                          details={"open_children": [{"id": c["id"], "title": c["title"]} for c in open_children]})

    def _warn_reopen(self, ctx: _Call, container: _Container) -> None:
        """TAPI §5b (UNCERTAIN): an open to-do added to or moved into a closed project reopens it."""
        if container.kind == "project" and container.status not in (None, "open"):
            ctx.warn(REOPEN_WARNING.format(_clip(container.title), container.status))

    # ---- json payloads (things_json) ---------------------------------------------------------------------

    def _json_containers(self, ctx: _Call, items: list) -> dict[str, _Container]:
        """``list-id``/``area-id`` targets must exist and be the right kind (Things files the item in the Inbox
        or ignores the move otherwise)."""
        refs: list[tuple[str, str, tuple[str, ...]]] = []          # (id, JSON pointer, allowed kinds)
        for i, it in enumerate(items):
            if isinstance(it, jsoncmd.Todo) and it.list_id is not None:
                refs.append((it.list_id, f"/{i}/attributes/list-id", ("project", "area")))
            if isinstance(it, jsoncmd.Project) and it.area_id is not None:
                refs.append((it.area_id, f"/{i}/attributes/area-id", ("area",)))
        if not refs or ctx.offline:
            return {}
        found, areas, _ = self._rows([cid for cid, _, _ in refs], notes="none")
        out: dict[str, _Container] = {}
        for cid, where, kinds in refs:
            if cid in areas:
                out[cid] = _Container("area", cid, areas[cid].title, None)
            elif cid in found and found[cid].kind == "project":
                out[cid] = _Container("project", cid, found[cid].title, found[cid].status)
            elif cid in found:
                raise _fail(where, f"{cid} is a to-do, not a project or area")
            else:
                raise NotFound(f"{where}: no project or area with id {cid}.", details={"field": where})
            if out[cid].kind not in kinds:
                raise _fail(where, f"{cid} is a project, not an area")
        return out

    def _json_tags(self, ctx: _Call, items: list) -> list:
        """Canonical tags everywhere in the payload (unknown ones dropped with a warning). An update whose only
        change was unknown tags is dropped instead of being sent as a clear."""
        out = []
        for it in items:
            if it.tags:
                tags = self._canonical_tags(ctx, it.tags, strict=False)
                it.tags = tags or None
                if not tags and it.operation == "update":
                    keys = jsoncmd.TODO_KEYS if isinstance(it, jsoncmd.Todo) else jsoncmd.PROJECT_KEYS
                    if all(getattr(it, k.replace("-", "_")) is None for k in keys):
                        ctx.warn(f"Nothing left to update for {it.id}: none of its tags exist.")
                        continue
            for child in getattr(it, "items", None) or ():
                if isinstance(child, jsoncmd.Todo) and child.tags:
                    child.tags = self._canonical_tags(ctx, child.tags, strict=False) or None
            out.append(it)
        return out

    def _json_guards(self, ctx: _Call, items: list, before: list[Item], containers: dict[str, _Container]) -> None:
        by_id = {it.id: it for it in before}
        guarded: list[Item] = []
        guarded_fields: dict[str, set[str]] = {}
        # N12 over the whole payload: what the operations before a project close leave behind.
        is_open: dict[str, bool] = {}          # to-do id -> open after the earlier operations
        moved: dict[str, str] = {}             # to-do id -> list-id it was moved to
        created: list[dict] = []               # open to-dos created into a project
        for it in items:
            if it.operation == "update":
                fields = {k for k in ("when", "deadline") if getattr(it, k) is not None}
                fields |= {k for k in ("completed", "canceled") if getattr(it, k) is not None}
                if fields and it.id in by_id:
                    if it.id not in guarded_fields:
                        guarded.append(by_id[it.id])
                    guarded_fields.setdefault(it.id, set()).update(fields)
                if isinstance(it, jsoncmd.Project) and (it.completed or it.canceled):
                    incoming = created + [
                        {"id": tid, "title": by_id[tid].title if tid in by_id else tid, "project": pid}
                        for tid, pid in moved.items()
                        if is_open.get(tid, tid not in by_id or by_id[tid].status == "open")]
                    self._guard_project_children(ctx, [it.id], "completed" if it.completed else "canceled",
                                                 is_open=dict(is_open), moved=dict(moved), incoming=incoming)
                elif isinstance(it, jsoncmd.Todo):
                    if it.completed or it.canceled:
                        is_open[it.id] = False
                    elif it.completed is False or it.canceled is False:
                        is_open[it.id] = True
                    if it.list_id is not None:
                        moved[it.id] = it.list_id
            elif isinstance(it, jsoncmd.Todo) and it.list_id is not None and not (it.completed or it.canceled):
                created.append({"id": None, "title": it.title or "", "project": it.list_id})
            if isinstance(it, jsoncmd.Todo) and (it.heading is not None or it.heading_id is not None):
                ctx.warn(HEADING_WARNING)
            if isinstance(it, jsoncmd.Todo) and it.list_id in containers and not (it.completed or it.canceled) \
                    and (it.operation == "create" or by_id.get(it.id) is None or by_id[it.id].status == "open"):
                self._warn_reopen(ctx, containers[it.list_id])
        self._guard_repeating(ctx, guarded, set(GUARD_FIELDS), per_item=guarded_fields)

    def _json_checks(self, items: list, before: list[Item], containers: dict[str, _Container],
                     today: date) -> tuple[list[_Create], list[Expect], list[str]]:
        creates: list[_Create] = []
        expects: list[Expect] = []
        unverifiable: list[str] = []
        by_id = {it.id: it for it in before}
        for it in items:
            container = containers.get(getattr(it, "list_id", None) or getattr(it, "area_id", None) or "")
            if it.operation == "create":
                unv = self._when_unverifiable(it.when)
                if isinstance(it, jsoncmd.Todo):
                    if it.checklist_items:
                        unv.append("checklist")
                    if it.heading is not None or it.heading_id is not None:
                        unv.append("heading")
                    creates.append(_Create(
                        "to-do", it.title or "", _scope_for(container, it.when, today), notes=it.notes or None,
                        when=it.when, deadline=it.deadline, tags=it.tags, unverifiable=unv,
                        project_id=container.id if container and container.kind == "project" else None,
                        area_id=container.id if container and container.kind == "area" else None))
                else:
                    kids = it.items or []
                    if any(isinstance(k, jsoncmd.Heading) for k in kids):
                        unv.append("heading")
                    if any(isinstance(k, jsoncmd.Todo) and k.checklist_items for k in kids):
                        unv.append("checklist")
                    titles = [k.title for k in kids if isinstance(k, jsoncmd.Todo) and k.title]
                    creates.append(_Create("project", it.title or "", ("app", ""), notes=it.notes or None,
                                           when=it.when, deadline=it.deadline, tags=it.tags,
                                           area_id=it.area_id, children=titles or None, unverifiable=unv))
                continue
            patch_cls = TodoPatch if isinstance(it, jsoncmd.Todo) else ProjectPatch
            patch = patch_cls(title=it.title, notes=it.notes, prepend_notes=it.prepend_notes,
                              append_notes=it.append_notes, when=it.when, deadline=it.deadline, tags=it.tags)
            if isinstance(it, jsoncmd.Todo):
                patch.heading = it.heading or it.heading_id
            resolved: dict = {}
            if container is not None:
                resolved[f"{container.kind}_id"] = container.id
            more, unv = expectations_for_todo_patch(it.id, patch, resolved, by_id.get(it.id), today)
            expects += more
            unverifiable += unv
            if it.canceled is True:
                expects.append(Expect(it.id, "status", "eq", "canceled"))
            elif it.completed is True:
                expects.append(Expect(it.id, "status", "eq", "completed"))
            elif it.completed is False or it.canceled is False:
                expects.append(Expect(it.id, "status", "eq", "open"))
        return creates, expects, unverifiable

    # ---- dispatch and verification -------------------------------------------------------------------------

    def _dispatch(self, ctx: _Call, urls: list[urlscheme.ThingsURL],
                  dry_run: bool) -> tuple[list[Dispatch], str | None]:
        """Send each URL once, in order. If one fails after an earlier one went out, stop and report it (the
        earlier ones can't be taken back, and nothing is retried: N10)."""
        out: list[Dispatch] = []
        for turl in urls:
            try:
                out.append(self.runner.dispatch(turl, dry_run=dry_run))
            except ThingsBridgeError as e:
                if not any(d.sent for d in out):
                    raise
                failure = (f"Only {len(out)} of {len(urls)} URLs were sent; the next one failed "
                           f"([{e.code}] {e.message}) and the rest were not sent.")
                ctx.warn(failure)
                return out, failure
        return out, None

    def _nothing_sent(self, ctx: _Call, action: str, ids: list[str], before: list[Item], dry_run: bool) -> WriteResult:
        ctx.warn("Nothing was sent: none of the requested changes can be applied.")
        return WriteResult(action=action, sent=False, dry_run=dry_run, verified=None, ids=list(ids), items=[],
                           before=before, checks=[], unverifiable=[], warnings=ctx.warnings, urls=[],
                           links=[link_for(i) for i in ids], launched_things=ctx.launched)

    def _finish(self, ctx: _Call, action: str, dispatches: list[Dispatch], failure: str | None, *,
                ids: Sequence[str] = (), before: Sequence[Item] = (), creates: Sequence[_Create] = (),
                expects: Sequence[Expect] = (), unverifiable: Sequence[str] = (), update: bool = True) -> WriteResult:
        sent = any(d.sent for d in dispatches)
        dry_run = any(d.dry_run for d in dispatches)
        ids = list(ids)
        unverifiable = list(dict.fromkeys([*unverifiable, *(u for c in creates for u in c.unverifiable)]))
        checks: list[Check] = []
        items: list[Item] = []
        verified: bool | None = None
        reason: str | None = None
        if sent and not self.config.verify:
            ctx.warn("Verification is off, so the change was sent but not checked.")
        elif sent:
            verified, reason, checks, items, created = self._verify(ctx, dispatches, creates, list(expects), update)
            ids = created + ids
        if failure:
            verified = False
            reason = f"{failure} {reason or NOTHING_RESENT}"
        return WriteResult(action=action, sent=sent, dry_run=dry_run, verified=verified, ids=ids, items=items,
                           before=list(before), checks=checks, unverifiable=unverifiable, warnings=ctx.warnings,
                           urls=[d.url_redacted for d in dispatches], links=[link_for(i) for i in ids],
                           launched_things=ctx.launched, reason=reason)

    def _verify(self, ctx: _Call, dispatches: list[Dispatch], creates: Sequence[_Create], expects: list[Expect],
                update: bool) -> tuple[bool | None, str | None, list[Check], list[Item], list[str]]:
        """Read Things back: find created items, then poll every expectation. Never dispatches (N10)."""
        verifier = Verifier(self.runner, timeout_s=self.config.verify_timeout_s)
        today = self.today()
        t0 = min(d.t0 for d in dispatches if d.sent)
        created: list[str] = []
        missing: list[Check] = []
        diagnosis = None
        error: ThingsBridgeError | None = None
        if creates:
            counts: dict[str, int] = {}
            for c in creates:
                counts[c.title] = counts.get(c.title, 0) + 1
            groups: dict[tuple, list[int]] = {}
            for i, c in enumerate(creates):
                groups.setdefault((c.kind, c.scope), []).append(i)
            assigned: dict[int, str] = {}
            for (kind, scope), members in groups.items():
                found = verifier.find_created(kind=kind, titles=[creates[i].title for i in members], scope=scope,
                                              t0=t0, launched=ctx.launched)
                for w in verifier.warnings:
                    ctx.warn(w)
                diagnosis = diagnosis or verifier.diagnosis
                for i in members:
                    queue = found.get(creates[i].title) or []
                    if queue:
                        assigned[i] = queue.pop(0)
                if verifier.error is not None:
                    error = verifier.error
                    break
            for i, c in enumerate(creates):
                nid = assigned.get(i)
                if nid is None:
                    missing.append(Check(target_id="", field="created", expected=_clip(c.title), observed=None,
                                         ok=False))
                    continue
                created.append(nid)
                if counts[c.title] > 1:          # same title twice in one call: ids can't be told apart
                    expects.append(Expect(nid, "title", "eq", c.title))
                    ctx.warn(f"Several new items share the title {_clip(c.title)!r}; only their existence was "
                             "checked.")
                else:
                    expects += self._create_expects(nid, c, today)
        checks: list[Check] = list(missing)
        items: list[Item] = []
        result = None
        if error is None and expects:
            result = verifier.check(expects, launched=ctx.launched)
            checks += result.checks
            items = result.items
            error = verifier.error
        if error is not None:
            return False, read_failed_reason(error), checks, items, created
        if missing:
            reason = dialog_reason() if diagnosis else not_verified_reason(verifier.timeout(ctx.launched),
                                                                             update=update)
            return False, reason, checks, items, created
        if result is None:
            return (True if created else None), None, checks, items, created
        if result.verified is False and not update:
            bad = sorted({c.field for c in result.checks if not c.ok})
            return False, (f"Things created it, but these fields don't match what was sent: {', '.join(bad)}. "
                           f"{NOTHING_RESENT}"), checks, items, created
        return result.verified, result.reason, checks, items, created

    @staticmethod
    def _create_expects(nid: str, c: _Create, today: date) -> list[Expect]:
        ex = [Expect(nid, "title", "eq", c.title)]
        if c.notes:
            ex.append(Expect(nid, "notes", "eq", c.notes))
        if c.when:
            ex += when_expects(nid, c.when, today)[0]
        if c.deadline:
            ex.append(deadline_expect(nid, c.deadline, today))
        if c.tags:
            ex.append(Expect(nid, "tags", "eq", list(c.tags)))
        if c.project_id:
            ex.append(Expect(nid, "project_id", "eq", c.project_id))
        if c.area_id:
            ex.append(Expect(nid, "area_id", "eq", c.area_id))
        if c.children:
            ex.append(Expect(nid, "children", "superset", list(c.children)))
        return ex
