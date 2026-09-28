"""Pure builders for ``things:///`` URLs (§2.3): allowlists, validation, serialisation, encoding. No I/O.

A write leaves this module as a frozen ``ThingsURL``. Only ``runner.dispatch`` opens it, and the runner
appends the auth token itself, so the token never passes through here. Every value is checked against
the documented limits first, because Things silently truncates or ignores what it can't use.

The ``check_*`` helpers are shared with ``jsoncmd``. They raise ``InvalidInput`` whose message starts
with the field name (a parameter key, or a JSON pointer for ``json`` payloads) and whose
``details["field"]`` holds it. Match ``ID_RE``/``DATE_RE``/``TIME_RE`` with ``fullmatch``: ``$`` also
matches before a trailing newline.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Mapping, NoReturn, Sequence
from urllib.parse import quote

from .errors import InvalidInput

if TYPE_CHECKING:
    from . import jsoncmd

# ---- constants (OFF-URL unless marked) ---------------------------------------------------------

SCHEME_PREFIX = "things:///"
# Exactly the documented commands; the deprecated JSON-add command is deliberately left out.
ALLOWED_COMMANDS = ("add", "add-project", "update", "update-project", "show", "search", "version", "json")
MUTATING = frozenset({"add", "add-project", "update", "update-project", "json"})
FOREGROUND = frozenset({"show", "search"})            # R-MEX §1.1; OFF (shortcuts.md: show needs foreground)
PARAMS: Mapping[str, tuple[str, ...]] = MappingProxyType({   # doc order; auth-token handled by runner, last
    "add": ("title", "titles", "notes", "when", "deadline", "tags", "checklist-items", "use-clipboard",
            "list-id", "list", "heading-id", "heading", "completed", "canceled", "show-quick-entry", "reveal",
            "creation-date", "completion-date"),
    "add-project": ("title", "notes", "when", "deadline", "tags", "area-id", "area", "to-dos", "completed",
                    "canceled", "reveal", "creation-date", "completion-date"),
    "update": ("id", "title", "notes", "prepend-notes", "append-notes", "when", "deadline", "tags", "add-tags",
               "checklist-items", "prepend-checklist-items", "append-checklist-items", "list-id", "list",
               "heading-id", "heading", "completed", "canceled", "reveal", "duplicate", "creation-date",
               "completion-date"),
    "update-project": ("id", "title", "notes", "prepend-notes", "append-notes", "when", "deadline", "tags",
                       "add-tags", "area-id", "area", "completed", "canceled", "reveal", "duplicate",
                       "creation-date", "completion-date"),
    "show": ("id", "query", "filter"),
    "search": ("query",),
    "version": (),
    "json": ("data", "reveal"),
})
TOKEN_ALWAYS = frozenset({"update", "update-project"})     # json: iff any operation == "update"
SHOW_BUILTIN_IDS = ("inbox", "today", "anytime", "upcoming", "someday", "logbook", "tomorrow", "deadlines",
                    "repeating", "all-projects", "logged-projects")
# "Maximum un-encoded string length: 4,000 characters", counted in UTF-16 units (TAPI §2j, UNCERTAIN unit)
MAX_STRING_UTF16 = 4000
MAX_NOTES_CODEPOINTS = 10_000  # notes/prepend/append; code points >= graphemes, so conservative (TAPI §2j)
MAX_CHECKLIST = 100
MAX_URL_BYTES = 512 * 1024   # R-MEX §1.4 (ARG_MAX 1 MiB shared with env)
RATE_BUDGET, RATE_WINDOW_S = 200, 10.0   # official 250/10 s, 20 % margin (OFF-URL "add"; R-MEX §6)
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{7,63}$")     # 21/22-char base62 + legacy UUIDs (R-ASR §2; R-PAL P33)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>[aApP][mM])?$")

WHEN_KEYWORDS = ("today", "tomorrow", "evening", "anytime", "someday")
DEADLINE_KEYWORDS = ("today", "tomorrow")
CLEARABLE = frozenset({"notes", "deadline", "tags", "when"})   # CLEAR is accepted only for these, on updates
USE_CLIPBOARD_VALUES = ("replace-title", "replace-notes", "replace-checklist-items")
MAX_NATURAL_CHARS = 100      # non-strict when/deadline (natural language)
# Pairs where the doc says one side is "ignored" or "takes priority": refused instead (R-PAL P32).
EXCLUSIVE = (("title", "titles"), ("list", "list-id"), ("area", "area-id"), ("heading", "heading-id"),
             ("tags", "add-tags"), ("notes", "prepend-notes"), ("notes", "append-notes"),
             ("checklist-items", "prepend-checklist-items"), ("checklist-items", "append-checklist-items"))

_TEXT_KEYS = frozenset({"title", "list", "heading", "area", "query"})
_NOTES_KEYS = frozenset({"notes", "prepend-notes", "append-notes"})
_TAG_KEYS = frozenset({"tags", "add-tags", "filter"})                    # joined with ","
_LINE_KEYS = frozenset({"titles", "to-dos", "checklist-items", "prepend-checklist-items",
                        "append-checklist-items"})                      # joined with "\n"
_CHECKLIST_KEYS = frozenset({"checklist-items", "prepend-checklist-items", "append-checklist-items"})
_ID_KEYS = frozenset({"id", "list-id", "area-id", "heading-id"})
_BOOL_KEYS = frozenset({"completed", "canceled", "show-quick-entry", "reveal", "duplicate"})
_DATETIME_KEYS = frozenset({"creation-date", "completion-date"})
_SAFE = {"when": "@:"}       # OFF-URL shows `when=evening@6pm` bare; legal in a query (RFC 3986)
_WHEN_CHARS = re.compile(r"[a-z0-9@:-]+")


# ---- types --------------------------------------------------------------------------------------

class _Clear:
    """Sentinel for ``key=`` (empty value), which clears the field. update/update-project only."""
    __slots__ = ()

    def __repr__(self) -> str:
        return "CLEAR"

    def __reduce__(self) -> str:        # copy/pickle keep the singleton
        return "CLEAR"


CLEAR: Final = _Clear()
Value = str | bool | Sequence[str] | _Clear | None


@dataclass(frozen=True)
class ThingsURL:
    command: str
    params: tuple[tuple[str, str], ...]      # already-serialised (unencoded) values, doc order
    weight: int                              # rate-limit items (§2.3 "Rate-limit weight")
    requires_token: bool                     # the runner appends auth-token (last); never stored here

    @property
    def url(self) -> str:
        if not self.params:
            return SCHEME_PREFIX + self.command
        query = "&".join(f"{k}={enc(v, safe=_SAFE.get(k, ''))}" for k, v in self.params)
        return f"{SCHEME_PREFIX}{self.command}?{query}"

    @property
    def mutating(self) -> bool:
        return self.command in MUTATING

    @property
    def foreground(self) -> bool:
        return self.command in FOREGROUND


# ---- encoding and field checks (shared with jsoncmd) ---------------------------------------------

def _fail(field: str, problem: str, hint: str | None = None) -> NoReturn:
    raise InvalidInput(f"{field}: {problem}", hint=hint, details={"field": field})


def _clip(s: str, n: int = 40) -> str:
    return s if len(s) <= n else s[:n] + "…"


def _at(field: str, i: int) -> str:
    return f"{field}/{i}" if field.startswith("/") else f"{field}[{i}]"


def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le", "surrogatepass")) // 2


def check_str(field: str, value: object) -> str:
    """A str without NUL or unpaired surrogates, with CR/CRLF normalised to LF."""
    if not isinstance(value, str):
        _fail(field, f"expected a string, got {type(value).__name__}")
    if "\x00" in value:
        _fail(field, "NUL characters are not allowed")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _fail(field, "contains invalid Unicode (an unpaired surrogate)")
    return value.replace("\r\n", "\n").replace("\r", "\n")


def enc(value: str, *, safe: str = "") -> str:
    """RFC 3986 unreserved-only percent-encoding of UTF-8; space -> %20 (never '+'). CR/CRLF -> LF; NUL rejected."""
    return quote(check_str("value", value), safe=safe)


def check_title(field: str, value: object) -> str:
    """Titles, checklist/titles/to-dos lines, list/area/heading names, queries: one non-blank line of at most 4000
    UTF-16 units."""
    s = check_str(field, value)
    if not s.strip():
        _fail(field, "must not be blank")
    if "\n" in s:
        _fail(field, "must be a single line", hint="put longer text in the notes")
    n = utf16_len(s)
    if n > MAX_STRING_UTF16:
        _fail(field, f"is {n} UTF-16 units long; Things keeps only the first {MAX_STRING_UTF16} "
                     "and silently drops the rest")
    return s


def check_notes(field: str, value: object) -> str:
    s = check_str(field, value)
    if len(s) > MAX_NOTES_CODEPOINTS:
        _fail(field, f"is {len(s)} characters long; Things keeps only the first {MAX_NOTES_CODEPOINTS} "
                     "and silently drops the rest")
    return s


def check_seq(field: str, value: object) -> list:
    if not isinstance(value, (list, tuple)):
        _fail(field, f"expected a list, got {type(value).__name__}")
    return [*value]


def check_tag(field: str, value: object) -> str:
    s = check_title(field, value)
    if "," in s:
        _fail(field, f"tag {_clip(s)!r} contains a comma, which Things' tag lists can't express",
              hint="rename the tag in Things, or leave it out")
    return s


def check_tags(field: str, value: object) -> list[str]:
    return [check_tag(_at(field, i), t) for i, t in enumerate(check_seq(field, value))]


def check_lines(field: str, value: object, *, max_items: int | None = None) -> list[str]:
    lines = check_seq(field, value)
    if max_items is not None and len(lines) > max_items:
        _fail(field, f"has {len(lines)} items; Things keeps only the first {max_items}")
    return [check_title(_at(field, i), s) for i, s in enumerate(lines)]


def check_id(field: str, value: object, *, allow_builtin: bool = False) -> str:
    s = check_str(field, value)
    if allow_builtin and s in SHOW_BUILTIN_IDS:
        return s
    if not ID_RE.fullmatch(s):
        _fail(field, f"{_clip(s)!r} is not a Things id",
              hint="ids come from things-bridge reads, or from Share → Copy Link in Things")
    return s


def check_bool(field: str, value: object) -> str:
    if not isinstance(value, bool):
        _fail(field, f"expected true or false, got {type(value).__name__}")
    return "true" if value else "false"


def format_datetime(field: str, value: object) -> str:
    """Timezone-aware datetime -> ``YYYY-MM-DDTHH:MM:SSZ`` (UTC, no fraction: TAPI §2h; TJC ``.iso8601``)."""
    if not isinstance(value, datetime):
        _fail(field, f"expected a datetime, got {type(value).__name__}")
    if value.utcoffset() is None:
        _fail(field, "the datetime must be timezone-aware")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_date(field: str, s: str, vocabulary: str) -> None:
    if not DATE_RE.fullmatch(s):
        _fail(field, f"{_clip(s)!r} is not {vocabulary}",
              hint="natural-language dates are not accepted; use YYYY-MM-DD")
    try:
        date.fromisoformat(s)
    except ValueError:
        _fail(field, f"{s} is not a real calendar date")


def _check_time(field: str, s: str) -> None:
    m = TIME_RE.fullmatch(s)
    if m:
        h, minutes = int(m["h"]), int(m["m"] or 0)
        if (1 <= h <= 12 if m["ap"] else h <= 23) and minutes <= 59:
            return
    _fail(field, f"{_clip(s)!r} is not a valid time", hint='use e.g. "6pm", "6:30pm" or "18:30"')


def _check_natural(field: str, s: str) -> None:
    if not s.strip():
        _fail(field, "must not be blank")
    if len(s) > MAX_NATURAL_CHARS:
        _fail(field, f"is longer than {MAX_NATURAL_CHARS} characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in s):
        _fail(field, "must not contain control characters or newlines")


def check_when(field: str, value: object, *, allow_anytime: bool = True, strict: bool = True) -> str:
    """Strict: ``today|tomorrow|evening|anytime|someday|YYYY-MM-DD`` plus optional ``@<time>``.
    Non-strict: any single line ≤ 100 chars (natural language, for the official examples)."""
    s = check_str(field, value)
    if strict:
        vocabulary = "today|tomorrow|evening|anytime|someday|YYYY-MM-DD, optionally followed by @time"
        if not _WHEN_CHARS.fullmatch(s):
            _fail(field, f"{_clip(s)!r} is not {vocabulary}",
                  hint="natural-language dates are not accepted; use YYYY-MM-DD")
        day, at, time = s.partition("@")
        if day not in WHEN_KEYWORDS:
            _check_date(field, day, vocabulary)
        if at:
            _check_time(field, time)
    else:
        _check_natural(field, s)
        day, at, _ = s.partition("@")
        day = day.strip().lower()
    if day == "anytime" and not allow_anytime:
        _fail(field, "anytime isn't documented for update or update-project",
              hint="send when=anytime as a json update operation instead")
    if at and day in ("anytime", "someday"):
        _fail(field, f"a reminder time can't be combined with {day} (Things ignores it)")
    return s


def check_deadline(field: str, value: object, *, strict: bool = True) -> str:
    """Strict: ``today|tomorrow|YYYY-MM-DD`` (a real date). Non-strict: any single line ≤ 100 chars."""
    s = check_str(field, value)
    if not strict:
        _check_natural(field, s)
    elif s not in DEADLINE_KEYWORDS:
        _check_date(field, s, "today|tomorrow|YYYY-MM-DD")
    return s


# ---- builders -----------------------------------------------------------------------------------

def _check_joined(key: str, joined: str, what: str) -> str:
    """A newline- or comma-joined value is one ``string`` parameter, so the 4,000-character limit (url-scheme.md,
    Data Types) applies to the whole value, not just to each line or tag."""
    n = utf16_len(joined)
    if n > MAX_STRING_UTF16:
        _fail(key, f"is {n} UTF-16 units long once its {what} are joined; Things keeps only the first "
                   f"{MAX_STRING_UTF16} and silently drops the rest",
              hint="use fewer or shorter entries, or send them through the json command (one string each)")
    return joined


def _serialize(command: str, key: str, value: object, *, strict: bool) -> str | None:
    """One parameter value -> its unencoded string; None = omit."""
    update = command in TOKEN_ALWAYS
    if value is CLEAR:
        if not update:
            _fail(key, "clearing a field (key=) is only possible with update and update-project")
        if key not in CLEARABLE:
            _fail(key, "can't be cleared; only notes, deadline, tags and when can")
        return ""
    if key in _TEXT_KEYS:
        return check_title(key, value)
    if key in _NOTES_KEYS:
        s = check_notes(key, value)
        if s:
            return s
        return "" if key == "notes" and update else None        # notes="" clears on update
    if key == "when":
        return check_when(key, value, allow_anytime=not update, strict=strict)
    if key == "deadline":
        return check_deadline(key, value, strict=strict)
    if key in _TAG_KEYS:
        tags = check_tags(key, value)
        if tags:
            return _check_joined(key, ",".join(tags), "tags")
        return "" if key == "tags" and update else None          # tags=[] replaces with none on update
    if key in _LINE_KEYS:
        if key == "checklist-items" and update:                  # D11: replacing destroys unreadable items
            _fail(key, "replacing a checklist would delete its current (unreadable) items, which this tool never does",
                  hint="use append-checklist-items or prepend-checklist-items")
        lines = check_lines(key, value, max_items=MAX_CHECKLIST if key in _CHECKLIST_KEYS else None)
        if not lines and key == "titles":
            _fail(key, "needs at least one title")
        return _check_joined(key, "\n".join(lines), "lines") if lines else None
    if key in _ID_KEYS:
        return check_id(key, value, allow_builtin=command == "show")
    if key in _BOOL_KEYS:
        return check_bool(key, value)
    if key in _DATETIME_KEYS:
        return format_datetime(key, value)
    if key == "use-clipboard":
        if value not in USE_CLIPBOARD_VALUES:
            _fail(key, f"must be one of {', '.join(USE_CLIPBOARD_VALUES)}")
        return value
    raise AssertionError(f"no serialiser for {command} {key}")   # "data" comes only from build_json


def _checked(turl: ThingsURL) -> ThingsURL:
    n = len(turl.url)                                           # ASCII after percent-encoding
    if n > MAX_URL_BYTES:
        raise InvalidInput(f"the {turl.command} URL would be {n} bytes; the limit is {MAX_URL_BYTES}",
                           hint="split the request into smaller batches", details={"field": "url"})
    return turl


def _build(command: str, args: dict) -> ThingsURL:
    strict = args.pop("strict", True)
    given = {name.replace("_", "-"): value for name, value in args.items()}
    assert set(given) <= set(PARAMS[command]), set(given) - set(PARAMS[command])
    params = []
    for key in PARAMS[command]:
        value = given.get(key)
        if value is not None:
            s = _serialize(command, key, value, strict=strict)
            if s is not None:
                params.append((key, s))
    got = dict(params)

    for a, b in EXCLUSIVE:
        if a in got and b in got:
            _fail(b, f"use either {a} or {b}, not both")
    if "completed" in got and "canceled" in got and "true" in (got["completed"], got["canceled"]):
        _fail("canceled", "use either completed or canceled, not both")
    if command == "add" and ("heading" in got or "heading-id" in got) and not ("list" in got or "list-id" in got):
        _fail("heading", "a heading needs its project in list-id (Things ignores it otherwise)")
    if command in TOKEN_ALWAYS:
        if "id" not in got:
            _fail("id", f"{command} needs the id of the item to change")
        if not set(got) - {"id", "reveal"}:
            _fail(command, "nothing to update")
    if command == "show" and ("id" in got) == ("query" in got):
        _fail("show", "needs exactly one of id or query")

    if command == "add":
        weight = got["titles"].count("\n") + 1 if "titles" in got else 1
    elif command == "add-project":
        weight = 1 + (got["to-dos"].count("\n") + 1 if "to-dos" in got else 0)
    else:
        weight = 1 if command in TOKEN_ALWAYS else 0
    return _checked(ThingsURL(command, tuple(params), weight, command in TOKEN_ALWAYS))


# Each builder's first statement snapshots its keyword arguments (locals() holds nothing else yet).
# Parameter names are the kebab-case keys with "_"; `list`/`filter`/`id` shadow builtins on purpose.

def build_add(*, title: str | None = None, titles: Sequence[str] | None = None, notes: str | None = None,
              when: str | None = None, deadline: str | None = None, tags: Sequence[str] | None = None,
              checklist_items: Sequence[str] | None = None, use_clipboard: str | None = None,
              list_id: str | None = None, list: str | None = None, heading_id: str | None = None,
              heading: str | None = None, completed: bool | None = None, canceled: bool | None = None,
              show_quick_entry: bool | None = None, reveal: bool | None = None,
              creation_date: datetime | None = None, completion_date: datetime | None = None,
              strict: bool = True) -> ThingsURL:
    args = dict(locals())
    return _build("add", args)


def build_add_project(*, title: str | None = None, notes: str | None = None, when: str | None = None,
                      deadline: str | None = None, tags: Sequence[str] | None = None, area_id: str | None = None,
                      area: str | None = None, to_dos: Sequence[str] | None = None, completed: bool | None = None,
                      canceled: bool | None = None, reveal: bool | None = None,
                      creation_date: datetime | None = None, completion_date: datetime | None = None,
                      strict: bool = True) -> ThingsURL:
    args = dict(locals())
    return _build("add-project", args)


def build_update(*, id: str, title: str | None = None, notes: str | _Clear | None = None,
                 prepend_notes: str | None = None, append_notes: str | None = None,
                 when: str | _Clear | None = None, deadline: str | _Clear | None = None,
                 tags: Sequence[str] | _Clear | None = None, add_tags: Sequence[str] | None = None,
                 checklist_items: Sequence[str] | None = None, prepend_checklist_items: Sequence[str] | None = None,
                 append_checklist_items: Sequence[str] | None = None, list_id: str | None = None,
                 list: str | None = None, heading_id: str | None = None, heading: str | None = None,
                 completed: bool | None = None, canceled: bool | None = None, reveal: bool | None = None,
                 duplicate: bool | None = None, creation_date: datetime | None = None,
                 completion_date: datetime | None = None, strict: bool = True) -> ThingsURL:
    args = dict(locals())
    return _build("update", args)


def build_update_project(*, id: str, title: str | None = None, notes: str | _Clear | None = None,
                         prepend_notes: str | None = None, append_notes: str | None = None,
                         when: str | _Clear | None = None, deadline: str | _Clear | None = None,
                         tags: Sequence[str] | _Clear | None = None, add_tags: Sequence[str] | None = None,
                         area_id: str | None = None, area: str | None = None, completed: bool | None = None,
                         canceled: bool | None = None, reveal: bool | None = None, duplicate: bool | None = None,
                         creation_date: datetime | None = None, completion_date: datetime | None = None,
                         strict: bool = True) -> ThingsURL:
    args = dict(locals())
    return _build("update-project", args)


def build_show(*, id: str | None = None, query: str | None = None,
               filter: Sequence[str] | None = None) -> ThingsURL:
    args = dict(locals())
    return _build("show", args)


def build_search(*, query: str | None = None) -> ThingsURL:
    return _build("search", {"query": query})


def build_version() -> ThingsURL:
    """Built for completeness; never dispatched (it foregrounds Things and returns nothing: TAPI §5e)."""
    return ThingsURL("version", (), 0, False)


def build_json(items: Sequence["jsoncmd.JsonItem"], *, reveal: bool | None = None,
               strict: bool = True) -> ThingsURL:
    """Validate typed ``jsoncmd`` items and encode them as one ``json`` URL (token iff an update operation)."""
    from . import jsoncmd
    jsoncmd.validate(items, strict=strict)
    params = [("data", jsoncmd.dumps(items))]
    if reveal is not None:
        params.append(("reveal", check_bool("reveal", reveal)))
    return _checked(ThingsURL("json", tuple(params), jsoncmd.weight(items), jsoncmd.has_update(items)))


# ---- normalizers (service layer; the raw builders don't apply them) ------------------------------

def normalize_reminder(s: str) -> str:
    """``"18:00"|"6pm"|"6:30 PM"|"06:45"`` -> ``"h:mmam"``/``"h:mmpm"`` (e.g. ``"6:00pm"``, ``"6:45am"``).

    Always emits am/pm: Things reinterprets bare hours 1-11 (TAPI §2d, UNCERTAIN). An hour 1-12 without
    am/pm and without a leading zero (``"9:05"``) is ambiguous and refused.
    """
    field = "reminder_time"
    t = check_str(field, s).strip()
    m = TIME_RE.fullmatch(t)
    if not m:
        _fail(field, f"{_clip(t)!r} is not a time", hint='use e.g. "18:00", "6pm" or "6:30pm"')
    _check_time(field, t)
    h, minutes, ap = int(m["h"]), int(m["m"] or 0), m["ap"]
    mm = f"{minutes:02d}"
    if ap:
        pm = ap.lower() == "pm"
        h12 = h
    else:
        if 1 <= h <= 12 and not m["h"].startswith("0"):
            other = f"{h + 12}:{mm}" if h < 12 else f"{h}:{mm}pm"
            _fail(field, f"{t!r} is ambiguous; say {h}:{mm}am or {other}")
        pm = h >= 12
        h12 = h % 12 or 12
    return f"{h12}:{mm}{'pm' if pm else 'am'}"


def compose_when(when: str, reminder_time: str | None, *, today: date) -> str:
    """Canonical ``when`` (+ reminder) -> the value to send, e.g. ``"2026-10-02@6:00pm"``, ``"evening@6:00pm"``.

    Refuses past dates (R-PAL §4.7; also avoids the TAPI §1 crash branch), an ``@`` inside ``when``, and a
    reminder with anytime/someday.
    """
    field = "when"
    w = check_str(field, when).strip().lower()
    if "@" in w:
        _fail(field, "put the time in reminder_time, not in when")
    if w not in WHEN_KEYWORDS:
        _check_date(field, w, "today|tomorrow|evening|anytime|someday|YYYY-MM-DD")
        if date.fromisoformat(w) < today:
            _fail(field, f"{w} is in the past", hint="use today or a future date")
    if reminder_time is None:
        return w
    if w in ("anytime", "someday"):
        _fail("reminder_time", f"can't be combined with {w} (Things ignores the time)")
    return f"{w}@{normalize_reminder(reminder_time)}"


def normalize_deadline(d: str, *, today: date) -> str:
    """``"today"|"tomorrow"|"YYYY-MM-DD"`` (case/space-insensitive keywords). Past dates are allowed: overdue
    deadlines are legitimate, so ``today`` is only taken for symmetry with ``compose_when``."""
    s = check_str("deadline", d).strip().lower()
    if s not in DEADLINE_KEYWORDS:
        _check_date("deadline", s, "today|tomorrow|YYYY-MM-DD")
    return s


def resolve_day(value: str, *, today: date) -> str | None:
    """The calendar day a canonical when/deadline value means (for verification).

    today/evening -> today; tomorrow -> today + 1; ``YYYY-MM-DD[@time]`` -> that day; anytime/someday -> None.
    """
    day = check_str("when", value).partition("@")[0].strip().lower()
    if day in ("today", "evening"):
        return today.isoformat()
    if day == "tomorrow":
        return (today + timedelta(days=1)).isoformat()
    if day in ("anytime", "someday"):
        return None
    _check_date("when", day, "today|tomorrow|evening|anytime|someday|YYYY-MM-DD")
    return day
