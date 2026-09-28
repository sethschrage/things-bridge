"""Command-line front end (§2.12; commands per §3 and §3.4; setup per §5.3).

* **Output.** Human-readable by default. ``--json`` prints the result dataclass (``dataclasses.asdict``) on
  stdout, and errors as ``{"error": {"code", "message", "hint", "details"}}``. Prompts and progress go to stderr.
* **Exit codes.** 0 ok, 2 invalid input or usage, 3 environment, 4 refused, 5 not found or ambiguous, 6 sent but
  not verified, 1 anything else.
* **Stdin.** ``add -`` (first line is the title, the rest the notes), ``--notes -`` (also ``--prepend-notes -``
  and ``--append-notes -``), ``json -``, ``add-many -``, ``add-project --items -``, and ``-`` as the only id of
  the batch commands. Only one argument per call can read stdin.
* **Token.** Never a flag. ``token set`` reads it with ``getpass`` (or ``--stdin``) and never prints it.
* **Laziness.** ``mcp`` and ``selftest`` import their modules only when run, so other commands never load
  mcp/pydantic.
"""

import argparse
import dataclasses
import getpass
import json
import logging
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Callable, Sequence

from . import __version__, applescript, authtoken, doctor, errors, service, urlscheme
from .errors import EXIT_NOT_VERIFIED, InvalidInput, KeychainError, Refused
from .models import (AreaList, DoctorCheck, DoctorReport, GetResult, Item, NavResult, Page, ProjectItemSpec,
                     ProjectPatch, ProjectSpec, Ref, SelftestReport, SelftestStep, TagList, TodoPatch, TodoSpec,
                     WriteResult)
from .runner import MacRunner, RedactFilter, Runner, redact

log = logging.getLogger(__name__)

PROG = "things-bridge"
EXIT_OK, EXIT_OTHER, EXIT_USAGE, EXIT_ENV, EXIT_INTERRUPTED = 0, 1, 2, 3, 130
LOG_ENV = "THINGS_BRIDGE_LOG"
MAX_INPUT_CHARS = 4 * 1024 * 1024
LIST_TITLES = {"inbox": "Inbox", "today": "Today", "anytime": "Anytime", "upcoming": "Upcoming", "someday": "Someday"}
NOTES_MODES = ("none", "preview", "full")
STATUS_FILTERS = ("open", "all")

SELFTEST_CONSENT = ("This creates a project named 'things-bridge selftest' containing ~8 to-dos in your Things, then "
                    "completes/cancels them so everything ends up in the Logbook. Nothing is deleted — you can "
                    "delete the project by hand afterwards. Continue?")
SELFTEST_OFFER = ("Run a live self-test now? It creates a project 'things-bridge selftest' with ~8 to-dos, "
                  "completes/cancels them (they end up in the Logbook), and never deletes anything.")
TOKEN_HINT = ("In Things: Settings → General → Enable Things URLs → Manage → copy the token, then run "
              "`things-bridge token set`.")

EPILOG = """\
exit codes: 0 ok, 2 invalid input or usage, 3 environment (not macOS, Things, permissions,
token), 4 refused by a safety guard, 5 not found or ambiguous, 6 sent but not verified, 1 other.

stdin: `add -` reads the title (first line) and notes (the rest); `--notes -`, `json -`,
`add-many -` and `add-project --items -` read stdin; `complete -` (and the other batch
commands) read whitespace-separated ids.

things-bridge never deletes anything and never touches the Things database. The auth token is
never a command-line flag: store it with `things-bridge token set`."""


# ---- output ------------------------------------------------------------------------------------------------

def _write_line(stream: Any, text: str) -> None:
    text = redact(text) + "\n"
    try:
        stream.write(text)
    except UnicodeEncodeError:
        enc = getattr(stream, "encoding", None) or "utf-8"
        stream.write(text.encode(enc, "backslashreplace").decode(enc, "replace"))


def _out(text: str = "") -> None:
    _write_line(sys.stdout, text)


def _err(text: str = "") -> None:
    _write_line(sys.stderr, text)


def _dump(obj: Any) -> str:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        obj = dataclasses.asdict(obj)
    return json.dumps(obj, indent=2, ensure_ascii=False, default=str)


# Terminal control characters and bidi overrides from Things content (titles and notes are user data).
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def _clean(s: str | None) -> str:
    """One line of user content, safe to print."""
    return _CONTROL.sub("", re.sub(r"[\r\n\t]+", " ", s or ""))


def _clean_block(s: str | None) -> str:
    """Multi-line user content, safe to print (keeps newlines and tabs)."""
    return _CONTROL.sub("", (s or "").replace("\r\n", "\n").replace("\r", "\n"))


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


def _isatty(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def _ask(prompt: str) -> str:
    """Prompt on stderr (stdout stays clean for --json) and read one line; "" at end of input."""
    sys.stderr.write(prompt)
    sys.stderr.flush()
    return sys.stdin.readline().strip()


def _confirm(question: str) -> bool:
    return _ask(f"{question} [y/N] ").lower() in ("y", "yes")


# ---- human-readable rendering -------------------------------------------------------------------------------

_GLYPHS = {"open": "[ ]", "completed": "[x]", "canceled": "[-]"}


def _item_line(it: Item, today: str) -> str:
    meta = []
    if it.kind == "project":
        meta.append("project")
    if it.start_date and it.start_date != today:
        meta.append(f"start {it.start_date}")
    if it.deadline:
        meta.append(f"{'overdue' if it.status == 'open' and it.deadline < today else 'due'} {it.deadline}")
    if it.status == "completed" and it.completed_at:
        meta.append(f"completed {it.completed_at[:10]}")
    if it.status == "canceled" and it.canceled_at:
        meta.append(f"canceled {it.canceled_at[:10]}")
    where = it.project or it.area
    if where is not None and where.title:
        meta.append(f"in {_clip(_clean(where.title), 40)}")
    if it.tags:
        meta.append(" ".join("#" + _clean(t) for t in it.tags))
    line = f"{it.id}  {_GLYPHS.get(it.status, '[?]')} {_clean(it.title) or '(untitled)'}"
    return line + (f"  ({', '.join(meta)})" if meta else "")


def _notes_preview(it: Item) -> str | None:
    lines = [ln.strip() for ln in (it.notes or "").splitlines() if ln.strip()]
    if not lines:
        return None
    text = _clip(_clean(lines[0]), 100)
    return text + (" …" if (len(lines) > 1 or it.notes_truncated) and not text.endswith("…") else "")


def _print_page(ctx: "_Ctx", page: Page, title: str, *, more: str = "offset") -> None:
    n = page.returned
    if page.total is None:
        count = f"{n} shown"
    elif page.offset or page.truncated:
        count = f"{page.offset + 1}–{page.offset + n} of {page.total}" if n else f"0 of {page.total}"
    else:
        count = str(page.total)
    _out(f"{title} ({count})")
    today = ctx.today().isoformat()
    if not page.items:
        _out("  (nothing here)")
    for it in page.items:
        _out("  " + _item_line(it, today))
        preview = _notes_preview(it)
        if preview:
            _out("      " + preview)
    if page.truncated:
        _out(f"  … more with --offset {page.offset + n}" if more == "offset" else "  … more with a higher --limit")
    if page.note:
        _out(f"  note: {page.note}")


def _ref(ref: Ref | None) -> str | None:
    return f"{_clean(ref.title)} ({ref.id})" if ref is not None else None


def _print_get(ctx: "_Ctx", r: GetResult) -> None:
    notes, truncated = None, False
    if r.area is not None:
        a = r.area
        _out(_clean(a.title) or "(untitled)")
        rows = [("id", a.id), ("kind", "area"), ("tags", ", ".join(_clean(t) for t in a.tags)), ("link", a.link)]
    else:
        it = r.item
        assert it is not None
        _out(_clean(it.title) or "(untitled)")
        rows = [("id", it.id), ("kind", it.kind), ("status", it.status), ("project", _ref(it.project)),
                ("area", _ref(it.area)), ("tags", ", ".join(_clean(t) for t in it.tags)), ("start", it.start_date),
                ("deadline", it.deadline), ("created", it.created), ("modified", it.modified),
                ("completed", it.completed_at), ("canceled", it.canceled_at), ("link", it.link)]
        notes, truncated = it.notes, it.notes_truncated
    for key, value in rows:
        if value:
            _out(f"  {key + ':':<10} {value}")
    if notes:
        _out("  notes:")
        for line in _clean_block(notes).split("\n"):
            _out("    " + line)
        if truncated:
            _out("    … (truncated; use --notes full)")
    if r.children is not None:
        _out()
        _print_page(ctx, r.children, "Contents")


def _print_areas(r: AreaList) -> None:
    _out(f"Areas ({r.returned})")
    if not r.items:
        _out("  (nothing here)")
    for a in r.items:
        tags = f"  ({' '.join('#' + _clean(t) for t in a.tags)})" if a.tags else ""
        _out(f"  {a.id}  {_clean(a.title)}{tags}")


def _print_tags(r: TagList) -> None:
    _out(f"Tags ({r.returned})")
    if not r.items:
        _out("  (nothing here)")
    ids = {t.id for t in r.items}
    children: dict[str | None, list] = {}
    for t in r.items:
        children.setdefault(t.parent.id if t.parent is not None and t.parent.id in ids else None, []).append(t)
    seen: set[str] = set()

    def walk(parent: str | None, depth: int) -> None:
        for t in children.get(parent, ()):
            if t.id in seen:
                continue
            seen.add(t.id)
            shortcut = f"  [{_clean(t.shortcut)}]" if t.shortcut else ""
            _out(f"  {t.id}  {'  ' * depth}{_clean(t.title)}{shortcut}")
            walk(t.id, depth + 1)

    walk(None, 0)
    for t in r.items:                   # a parent cycle can't happen in Things, but never drop a tag
        if t.id not in seen:
            seen.add(t.id)
            _out(f"  {t.id}  {_clean(t.title)}")


def _fmt(v: str | None) -> str:
    return "nothing" if v is None else repr(_clip(_clean(v), 60))


def _print_write(ctx: "_Ctx", r: WriteResult) -> None:
    if r.dry_run:
        head = f"Dry run ({r.action}): nothing was sent."
    elif not r.sent:
        head = f"{r.action}: nothing was sent."
    elif r.verified is True:
        head = f"{r.action}: done, confirmed in Things."
    elif r.verified is None:
        head = f"{r.action}: sent" + ("" if ctx.config.verify else " (not verified: --no-verify)") + "."
    else:
        head = f"{r.action}: sent, but Things hasn't confirmed it."
    _out(head)
    today = ctx.today().isoformat()
    shown = {it.id: it for it in [*r.before, *r.items]}      # the read-back wins; a dry run shows the target
    links = dict(zip(r.ids, r.links, strict=True)) if len(r.links) == len(r.ids) else {}
    for i in r.ids:
        _out("  " + _item_line(shown[i], today) if i in shown else f"  {i}  {links.get(i, '')}".rstrip())
    for c in r.checks:
        if not c.ok:
            _out(f"  mismatch: {c.target_id} {c.field}: expected {_fmt(c.expected)}, found {_fmt(c.observed)}")
    if r.unverifiable:
        _out("  can't be checked: " + ", ".join(r.unverifiable))
    for w in r.warnings:
        _out(f"  warning: {_clean(w)}")
    if r.dry_run or ctx.verbose:
        for u in r.urls:
            _out(f"  {'would open' if r.dry_run else 'opened'}: {u}")
    if ctx.verbose and r.before:
        _out("  before:")
        for it in r.before:
            _out("    " + _item_line(it, today))
    if r.verified is False and r.reason:
        _err(r.reason)


def _print_nav(r: NavResult) -> None:
    _out(f"Opened {r.url}" if r.sent else f"Not opened: {r.url}")


_LABELS = {"ok": "ok", "warn": "WARN", "fail": "FAIL", "skip": "skip"}


def _print_check(c: DoctorCheck, width: int = 0) -> None:
    _out(f"  {_LABELS.get(c.status, c.status):<4}  {c.name:<{width}}  {c.detail}")
    if c.remedy and c.status in ("warn", "fail"):
        _out(f"  {'':<4}  {'':<{width}}  → {c.remedy}")


def _print_doctor(r: DoctorReport) -> None:
    width = max((len(c.name) for c in r.checks), default=0)
    for c in r.checks:
        _print_check(c, width)
    fails = [c.name for c in r.checks if c.status == "fail"]
    warns = [c.name for c in r.checks if c.status == "warn"]
    _out()
    if fails:
        _out(f"Problems: {', '.join(fails)} (see FAIL above).")
    else:
        _out("OK" + (f", with warnings: {', '.join(warns)}." if warns else "."))


def _step_line(s: SelftestStep) -> str:
    ids = f"  [{', '.join(s.ids)}]" if s.ids else ""
    return f"  {s.status.upper():<4}  {s.name}  {_clean(s.detail)}{ids}"


def _detail_lines(details: dict) -> list[str]:
    lines = []
    for c in details.get("candidates") or ():
        if isinstance(c, dict):
            area = c.get("area")
            where = f"  (in {_clean(area.get('title'))})" if isinstance(area, dict) and area.get("title") else ""
            lines.append(f"{c.get('id')}  {_clean(c.get('title'))}{where}")
    for c in details.get("open_children") or ():
        if isinstance(c, dict):
            lines.append(f"{c.get('id')}  {_clean(c.get('title'))}")
    return lines


# ---- per-call context --------------------------------------------------------------------------------------

class _Ctx:
    """Parsed arguments plus lazily built config, runner and service (so parse-only paths never touch them)."""

    def __init__(self, args: argparse.Namespace, runner: Runner | None, today: Callable[[], date] | None):
        self.args = args
        self.json = bool(getattr(args, "json", False))
        self.verbose = int(getattr(args, "verbose", 0) or 0)
        self.today = today or date.today
        self._runner = runner
        self._config: service.Config | None = None
        self._service: service.ThingsService | None = None
        self._stdin_used: str | None = None

    @property
    def config(self) -> service.Config:
        if self._config is None:
            cfg = service.Config.from_env()
            if getattr(self.args, "no_launch", False):
                cfg = dataclasses.replace(cfg, allow_launch=False)
            if getattr(self.args, "no_verify", False):
                cfg = dataclasses.replace(cfg, verify=False)
            self._config = cfg
        return self._config

    @property
    def runner(self) -> Runner:
        if self._runner is None:
            self._runner = MacRunner()
        return self._runner

    @property
    def service(self) -> service.ThingsService:
        if self._service is None:
            self._service = service.ThingsService(self.runner, self.config, today=self.today)
        return self._service

    def stdin(self, field: str) -> str:
        """All of stdin, for one argument per call."""
        if self._stdin_used is not None:
            raise InvalidInput(f"{field}: stdin is already used for {self._stdin_used}; only one argument can be '-'",
                               details={"field": field})
        self._stdin_used = field
        try:
            data = sys.stdin.read(MAX_INPUT_CHARS + 1)
        except UnicodeDecodeError:
            raise InvalidInput(f"{field}: stdin isn't valid {getattr(sys.stdin, 'encoding', None) or 'UTF-8'} "
                               "text", details={"field": field}) from None
        if len(data) > MAX_INPUT_CHARS:
            raise InvalidInput(f"{field}: stdin is larger than {MAX_INPUT_CHARS} characters", details={"field": field})
        return data

    def emit(self, result: Any, human: Callable[[Any], None]) -> None:
        if self.json:
            _out(_dump(result))
        else:
            human(result)


# ---- input helpers -----------------------------------------------------------------------------------------

def _text(ctx: _Ctx, value: str | None, field: str) -> str | None:
    """An option value, or stdin when it is ``-`` (trailing newlines dropped)."""
    if value == "-":
        return ctx.stdin(field).rstrip("\r\n")
    return value


def _refuse_things_data(path: Path, field: str) -> None:
    if "culturedcode" in str(path).lower():
        raise Refused("That file is inside Things' own data; things-bridge never reads Things' files.",
                      details={"field": field})


def _read_input(ctx: _Ctx, source: str, field: str) -> str:
    """A FILE argument or ``-`` (stdin). Paths into Things' own data are refused unopened (N1)."""
    if source == "-":
        return ctx.stdin(field)
    path = Path(source).expanduser()
    _refuse_things_data(path.absolute(), field)             # before touching the filesystem at all
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        resolved = path
    _refuse_things_data(resolved, field)                    # a symlink into Things' data
    try:
        if path.stat().st_size > 4 * MAX_INPUT_CHARS:
            raise InvalidInput(f"{field}: {source} is too large", details={"field": field})
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise InvalidInput(f"{field}: {source} isn't UTF-8 text", details={"field": field}) from None
    except OSError as e:
        raise InvalidInput(f"{field}: can't read {source} ({e.strerror or type(e).__name__})",
                           details={"field": field}) from None


def _parse_json(text: str, field: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise InvalidInput(f"{field}: not valid JSON (line {e.lineno}, column {e.colno}: {e.msg})",
                           details={"field": field}) from None


def _from_obj(cls: type, obj: Any, where: str) -> Any:
    """A spec dataclass from a JSON object whose keys are exactly its field names."""
    if not isinstance(obj, dict):
        raise InvalidInput(f"{where}: must be an object or a title string", details={"field": where})
    fields = dataclasses.fields(cls)
    names = [f.name for f in fields]
    unknown = [k for k in obj if k not in names]
    if unknown:
        raise InvalidInput(f"{where}: unknown key {_clip(str(unknown[0]), 40)!r}", hint="allowed: " + ", ".join(names),
                           details={"field": f"{where}.{unknown[0]}"})
    for f in fields:
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING and f.name not in obj:
            raise InvalidInput(f"{where}: {f.name} is required", details={"field": f"{where}.{f.name}"})
    return cls(**obj)


def _specs(text: str, field: str, make: Callable[[str], Any], cls: type) -> list:
    """A JSON array (title strings or objects) or, if the input isn't JSON, one title per line."""
    if text.lstrip().startswith(("[", "{")):
        data = _parse_json(text, field)
        if not isinstance(data, list):
            raise InvalidInput(f"{field}: must be a JSON array", details={"field": field})
        out = [make(x) if isinstance(x, str) else _from_obj(cls, x, f"{field}[{i}]") for i, x in enumerate(data)]
    else:
        out = [make(line.strip()) for line in text.splitlines() if line.strip()]
    if not out:
        raise InvalidInput(f"{field}: the input has no entries", details={"field": field})
    return out


def _title_and_notes(text: str) -> tuple[str, str | None]:
    """``add -``: the first line is the title, the rest (if any) the notes (itspriddle convention, R-PAL §3.8)."""
    lines = text.splitlines()
    if not lines or not lines[0].strip():
        raise InvalidInput("title: the first line of stdin must be the title", details={"field": "title"})
    rest = "\n".join(lines[1:]).strip("\n")
    return lines[0].strip(), (rest if rest.strip() else None)


def _ids(ctx: _Ctx, values: Sequence[str]) -> list[str]:
    if list(values) == ["-"]:
        ids = ctx.stdin("ids").split()
        if not ids:
            raise InvalidInput("ids: stdin has no ids", details={"field": "ids"})
        return ids
    return list(values)


def _write(ctx: _Ctx, r: WriteResult) -> int:
    ctx.emit(r, lambda x: _print_write(ctx, x))
    return EXIT_NOT_VERIFIED if r.verified is False else EXIT_OK


# ---- commands: reads ---------------------------------------------------------------------------------------

def _cmd_list(ctx: _Ctx) -> int:
    a = ctx.args
    page = ctx.service.list_items(a.list_key, status=a.status, limit=a.limit, offset=a.offset, notes=a.notes)
    ctx.emit(page, lambda p: _print_page(ctx, p, LIST_TITLES[a.list_key]))
    return EXIT_OK


def _cmd_logbook(ctx: _Ctx) -> int:
    a = ctx.args
    since = None if a.since.strip().lower() in ("all", "none") else a.since
    page = ctx.service.logbook(since=since, limit=a.limit, notes=a.notes)
    title = "Logbook" + (f" since {a.since}" if since is not None else "")
    ctx.emit(page, lambda p: _print_page(ctx, p, title, more="limit"))
    return EXIT_OK


def _cmd_get(ctx: _Ctx) -> int:
    a = ctx.args
    ctx.emit(ctx.service.get(a.id, children=a.children, notes=a.notes), lambda r: _print_get(ctx, r))
    return EXIT_OK


def _cmd_projects(ctx: _Ctx) -> int:
    a = ctx.args
    page = ctx.service.projects(area=a.area, status=a.status, limit=a.limit, offset=a.offset, notes=a.notes)
    ctx.emit(page, lambda p: _print_page(ctx, p, "Projects"))
    return EXIT_OK


def _cmd_areas(ctx: _Ctx) -> int:
    ctx.emit(ctx.service.areas(), _print_areas)
    return EXIT_OK


def _cmd_tags(ctx: _Ctx) -> int:
    ctx.emit(ctx.service.tags(), _print_tags)
    return EXIT_OK


def _cmd_search(ctx: _Ctx) -> int:
    a = ctx.args
    query = " ".join(a.query)
    page = ctx.service.search(query, include_completed=a.include_completed, logbook_window=a.logbook_window,
                              limit=a.limit, notes=a.notes)
    ctx.emit(page, lambda p: _print_page(ctx, p, f"Search '{_clip(_clean(query), 40)}'", more="limit"))
    return EXIT_OK


def _cmd_find(ctx: _Ctx) -> int:
    a = ctx.args
    page = ctx.service.find(tags=a.tags or (), project=a.project, area=a.area, status=a.status, limit=a.limit,
                            offset=a.offset, notes=a.notes)
    ctx.emit(page, lambda p: _print_page(ctx, p, "Found"))
    return EXIT_OK


def _cmd_deadlines(ctx: _Ctx) -> int:
    a = ctx.args
    page = ctx.service.deadlines(within_days=a.within_days, include_overdue=not a.no_overdue,
                                 include_projects=not a.no_projects, limit=a.limit)
    title = "Deadlines" + (f" within {a.within_days} days" if a.within_days is not None else "")
    ctx.emit(page, lambda p: _print_page(ctx, p, title, more="limit"))
    return EXIT_OK


def _cmd_doctor(ctx: _Ctx) -> int:
    a = ctx.args
    report = doctor.run_doctor(ctx.runner, allow_launch=ctx.config.allow_launch,
                               prompt_automation=a.prompt_automation, probe=a.probe,
                               check_repeating_id=a.check_repeating, notify=_err, today=ctx.today)
    ctx.emit(report, _print_doctor)
    return EXIT_OK if report.ok else EXIT_ENV


# ---- commands: writes --------------------------------------------------------------------------------------

def _cmd_add(ctx: _Ctx) -> int:
    a = ctx.args
    notes = _text(ctx, a.notes, "notes")
    title = " ".join(a.title)
    if a.title == ["-"]:
        title, stdin_notes = _title_and_notes(ctx.stdin("title"))
        if stdin_notes is not None:
            if notes is not None:
                raise InvalidInput("notes: given both on stdin and with --notes", details={"field": "notes"})
            notes = stdin_notes
    spec = TodoSpec(title=title, notes=notes, when=a.when, reminder_time=a.reminder, deadline=a.deadline,
                    tags=a.tags, checklist=a.checklist, project=a.project, area=a.area, heading=a.heading)
    return _write(ctx, ctx.service.add_todo(spec, reveal=a.reveal, dry_run=a.dry_run, strict_tags=a.strict_tags))


def _cmd_add_many(ctx: _Ctx) -> int:
    a = ctx.args
    specs = _specs(_read_input(ctx, a.file, "todos"), "todos", lambda t: TodoSpec(title=t), TodoSpec)
    return _write(ctx, ctx.service.add_todos(specs, dry_run=a.dry_run, strict_tags=a.strict_tags, confirm=a.confirm))


def _cmd_add_project(ctx: _Ctx) -> int:
    a = ctx.args
    items = [ProjectItemSpec(kind=kind, title=title) for kind, title in a.items or ()]
    if a.items_file is not None:
        if items:
            raise InvalidInput("items: give --todo/--heading or --items, not both", details={"field": "items"})
        items = _specs(_read_input(ctx, a.items_file, "items"), "items",
                       lambda t: ProjectItemSpec(kind="todo", title=t), ProjectItemSpec)
    spec = ProjectSpec(title=" ".join(a.title), notes=_text(ctx, a.notes, "notes"), when=a.when,
                       reminder_time=a.reminder, deadline=a.deadline, tags=a.tags, area=a.area, items=items or None)
    return _write(ctx, ctx.service.add_project(spec, reveal=a.reveal, dry_run=a.dry_run, strict_tags=a.strict_tags))


def _patch_fields(ctx: _Ctx) -> dict:
    """The fields ``TodoPatch`` and ``ProjectPatch`` share."""
    a = ctx.args
    return {"title": a.title, "notes": "" if a.clear_notes else _text(ctx, a.notes, "notes"),
            "prepend_notes": _text(ctx, a.prepend_notes, "prepend_notes"),
            "append_notes": _text(ctx, a.append_notes, "append_notes"), "when": a.when, "reminder_time": a.reminder,
            "deadline": a.deadline, "clear_deadline": a.clear_deadline, "tags": [] if a.clear_tags else a.tags,
            "add_tags": a.add_tags}


def _cmd_update(ctx: _Ctx) -> int:
    a = ctx.args
    patch = TodoPatch(**_patch_fields(ctx), checklist_append=a.checklist_append,
                      checklist_prepend=a.checklist_prepend, project=a.project, area=a.area, heading=a.heading)
    return _write(ctx, ctx.service.update_todo(a.id, patch, dry_run=a.dry_run, strict_tags=a.strict_tags))


def _cmd_update_project(ctx: _Ctx) -> int:
    a = ctx.args
    patch = ProjectPatch(**_patch_fields(ctx), area=a.area)
    return _write(ctx, ctx.service.update_project(a.id, patch, dry_run=a.dry_run, strict_tags=a.strict_tags))


def _cmd_status(ctx: _Ctx) -> int:
    a = ctx.args
    return _write(ctx, ctx.service.set_status(_ids(ctx, a.ids), a.status, dry_run=a.dry_run, confirm=a.confirm))


def _cmd_schedule(ctx: _Ctx) -> int:
    a = ctx.args
    return _write(ctx, ctx.service.schedule(_ids(ctx, a.ids), when=a.when, reminder_time=a.reminder,
                                            deadline=a.deadline, clear_deadline=a.clear_deadline,
                                            dry_run=a.dry_run, confirm=a.confirm))


def _cmd_move(ctx: _Ctx) -> int:
    a = ctx.args
    return _write(ctx, ctx.service.move(_ids(ctx, a.ids), project=a.project, area=a.area, heading=a.heading,
                                        dry_run=a.dry_run, confirm=a.confirm))


def _cmd_tag(ctx: _Ctx) -> int:
    a = ctx.args
    replace = [] if a.clear else a.replace
    return _write(ctx, ctx.service.tag(_ids(ctx, a.ids), add=a.add, replace=replace, dry_run=a.dry_run,
                                       confirm=a.confirm))


def _cmd_json(ctx: _Ctx) -> int:
    a = ctx.args
    data = _parse_json(_read_input(ctx, a.file, "data"), "data")
    if not isinstance(data, list):
        raise InvalidInput("data: the payload must be a JSON array (Things' JSON format)", details={"field": "data"})
    return _write(ctx, ctx.service.raw_json(data, reveal=a.reveal, dry_run=a.dry_run, confirm=a.confirm))


# ---- commands: navigation ----------------------------------------------------------------------------------

def _cmd_show(ctx: _Ctx) -> int:
    a = ctx.args
    ctx.emit(ctx.service.show(id=a.id, list_key=a.list_key, query=a.query, filter_tags=a.filter or ()), _print_nav)
    return EXIT_OK


def _cmd_open_search(ctx: _Ctx) -> int:
    ctx.emit(ctx.service.open_search(" ".join(ctx.args.query) or None), _print_nav)
    return EXIT_OK


# ---- commands: CLI only (§3.4) -----------------------------------------------------------------------------

def _cmd_token_set(ctx: _Ctx) -> int:
    if ctx.args.stdin:
        raw = ctx.stdin("token")
    else:
        if not _isatty(sys.stdin):
            raise InvalidInput("token set reads the token from the terminal, and there is none here.",
                               hint="pipe it in with `things-bridge token set --stdin`", details={"field": "token"})
        try:
            raw = getpass.getpass("Paste the Things URL auth token (input is hidden): ")
        except EOFError:
            raw = ""
    if not raw.strip():
        raise InvalidInput("No token was given.", hint=TOKEN_HINT, details={"field": "token"})
    authtoken.set_token(raw)
    length = len(raw.strip())
    del raw
    overridden = bool(os.environ.get(authtoken.ENV, "").strip())
    result = {"stored": True, "length": length, "overridden_by_env": overridden}

    def human(_: dict) -> None:
        _out(f"Saved the auth token to the login Keychain ({length} characters).")
        if overridden:
            _err(f"note: {authtoken.ENV} is set, and it overrides the Keychain.")

    ctx.emit(result, human)
    return EXIT_OK


def _cmd_token_status(ctx: _Ctx) -> int:
    source = authtoken.token_source()
    valid = None if source == "missing" else authtoken.is_valid_token(authtoken.get_token() or "")

    def human(_: dict) -> None:
        if source == "missing":
            _out(f"Token: missing. {TOKEN_HINT}")
            return
        where = authtoken.ENV if source == "env" else "login Keychain"
        _out(f"Token: {where} ({'format valid' if valid else 'INVALID format; run `things-bridge token set` again'})")

    ctx.emit({"source": source, "valid": valid}, human)
    return EXIT_OK if valid else EXIT_ENV


def _cmd_version(ctx: _Ctx) -> int:
    things, problem = None, None
    try:
        rows = applescript.probe_results(ctx.runner.run_template("APP_VERSION"))
        things = next((d[0] for f, ok, d in rows if f == "app-version" and ok and d and d[0]), None)
    except errors.ThingsBridgeError as e:
        problem = e.message
    data = {**doctor.versions(things), "things": things, "things_error": problem}

    def human(d: dict) -> None:
        _out(f"things-bridge  {d['things_bridge']}")
        _out(f"URL scheme     {d['url_scheme']}")
        _out(f"Things         {d['things'] or 'unavailable' + (f' ({problem})' if problem else '')}")
        _out(f"Python         {d['python']}")
        _out(f"mcp            {d['mcp']}")

    ctx.emit(data, human)
    return EXIT_OK


def _load_selftest() -> Any:
    try:
        from . import selftest
    except ModuleNotFoundError as e:
        if e.name == f"{__package__}.selftest":
            raise errors.ThingsBridgeError("The selftest isn't part of this installation.") from None
        raise
    return selftest


def _load_mcp_server() -> Any:
    try:
        from . import mcp_server
    except ModuleNotFoundError as e:
        if (e.name or "").split(".")[0] == "mcp":
            raise errors.ThingsBridgeError("The MCP server needs the `mcp` package, which isn't installed.",
                                           hint="reinstall things-bridge with its dependencies (mcp>=2.2,<3)") from None
        if e.name == f"{__package__}.mcp_server":
            raise errors.ThingsBridgeError("The MCP server isn't part of this installation.") from None
        raise
    return mcp_server


def _run_selftest(ctx: _Ctx, st: Any, *, probe_conflicts: bool) -> SelftestReport:
    printed: set[tuple] = set()

    def progress(step: SelftestStep) -> None:
        printed.add((step.name, step.status, step.detail))
        (_err if ctx.json else _out)(_step_line(step))

    report = st.run_selftest(ctx.runner, ctx.config, yes=True, probe_conflicts=probe_conflicts, progress=progress)
    if ctx.json:
        _out(_dump(report))
        return report
    for step in report.steps:
        if (step.name, step.status, step.detail) not in printed:
            _out(_step_line(step))
    _out()
    _out("Selftest passed." if report.ok else "Selftest FAILED.")
    if report.created_ids:
        _out(f"Created by this run: {', '.join(report.created_ids)}")
    if report.cleanup_note:
        _out(report.cleanup_note)
    return report


def _cmd_selftest(ctx: _Ctx) -> int:
    st = _load_selftest()
    if not ctx.args.yes:
        if not _isatty(sys.stdin):
            raise Refused("The selftest creates items in your Things, so it needs your consent.",
                          hint="run it in Terminal to answer the prompt, or pass --yes")
        if not _confirm(SELFTEST_CONSENT):
            _err("Cancelled; nothing was created.")
            return EXIT_OK
    report = _run_selftest(ctx, st, probe_conflicts=ctx.args.probe_conflicts)
    return EXIT_OK if report.ok else EXIT_OTHER


def _setup_token() -> bool:
    """Setup step 5: store the token unless one is already there. True if a valid token is set up."""
    try:
        source = authtoken.token_source()
    except errors.ThingsBridgeError as e:
        _report_human(e)
        return False
    if source == "env":
        valid = authtoken.is_valid_token(authtoken.get_token() or "")
        _out(f"{authtoken.ENV} is set and overrides the Keychain" + ("." if valid else ", but it isn't a valid token."))
        return valid
    if source == "keychain" and not _confirm("A token is already stored in the login Keychain. Replace it?"):
        _out("Kept the stored token.")
        return True
    _out("Click Manage next to Enable Things URLs and copy the token.")
    for _ in range(3):
        try:
            raw = getpass.getpass("Paste token (hidden; leave empty to skip): ")
        except EOFError:
            raw = ""
        if not raw.strip():
            _out("Skipped. Updates need the token; run `things-bridge token set` later.")
            return False
        try:
            authtoken.set_token(raw)
        except InvalidInput as e:
            _report_human(e)
            continue
        except KeychainError as e:
            _report_human(e)
            return False
        _out(f"Saved to the login Keychain ({len(raw.strip())} characters).")
        return True
    return False


def _cmd_setup(ctx: _Ctx) -> int:
    if ctx.json:
        raise InvalidInput("setup is interactive; run it without --json",
                           hint="`things-bridge doctor --json` checks the setup without asking anything",
                           details={"field": "json"})
    if not _isatty(sys.stdin):
        raise InvalidInput("setup is interactive; run it in Terminal",
                           hint="`things-bridge doctor` checks the setup without asking anything",
                           details={"field": "stdin"})
    runner, cfg = ctx.runner, ctx.config
    _out("things-bridge setup. It reads Things with read-only AppleScript and writes with Things URLs; it never "
         "touches the Things database and never deletes anything.")
    _out()
    _out("1. This Mac")
    first = doctor.check_platform(runner)
    _print_check(first)
    if first.status == "ok":
        first, version = doctor.check_installed(runner)
        _print_check(first)
    if first.status != "ok":
        return EXIT_ENV
    _print_check(doctor.check_versions(doctor.versions(version)))
    _out()
    _out("2. Things")
    running = doctor.check_running(runner, allow_launch=cfg.allow_launch)
    _print_check(running)
    if running.status != "ok":
        return EXIT_ENV
    _out()
    _out("3. Automation permission")
    grantee = doctor.host_identity()["grantee"]
    _out(f"macOS will now ask whether {grantee} may control Things. Click OK.")
    automation = doctor.check_automation(runner, prompt=True, grantee=grantee, notify=lambda _: None)
    _print_check(automation)
    _out()
    _out("4. Things URLs")
    _out("In Things → Settings → General, turn on Enable Things URLs.")
    _ask("Press Return when it's on. ")
    _out()
    _out("5. Auth token")
    token_ok = _setup_token()
    _out()
    _out("6. Self-test")
    if automation.status != "ok":
        _out("Skipped until Automation access works. Then run `things-bridge selftest`.")
    elif _confirm(SELFTEST_OFFER):
        try:
            _run_selftest(ctx, _load_selftest(), probe_conflicts=False)
        except errors.ThingsBridgeError as e:
            _report_human(e)
    else:
        _out("Skipped. Run `things-bridge selftest` any time.")
    problems = ([] if automation.status == "ok" else ["automation"]) + ([] if token_ok else ["token"])
    _out()
    _out("Setup finished." if not problems else f"Setup finished; still to fix: {', '.join(problems)}.")
    return EXIT_OK if not problems else EXIT_ENV


def _cmd_mcp(ctx: _Ctx) -> int:
    mcp_server = _load_mcp_server()
    a = ctx.args
    # The server builds its own Config from the environment; pass the global flags on that way.
    if a.no_launch:
        os.environ[service.ENV_NO_LAUNCH] = "1"
    if a.verbose:
        os.environ[LOG_ENV] = "DEBUG" if a.verbose > 1 else "INFO"
    if a.no_verify:
        _err("things-bridge: --no-verify doesn't apply to the MCP server; every write is verified.")
    try:
        mcp_server.main(["--read-only"] if a.read_only else [])
    except SystemExit as e:                     # a startup error, already logged to stderr
        return e.code if isinstance(e.code, int) else EXIT_OTHER
    return EXIT_OK


# ---- the parser ----------------------------------------------------------------------------------------------

class _UsageError(Exception):
    def __init__(self, parser: argparse.ArgumentParser, message: str):
        super().__init__(message)
        self.parser, self.message = parser, message


class _Parser(argparse.ArgumentParser):
    """Usage errors become exceptions, so ``main`` can render them (as JSON with ``--json``) and return 2."""

    def error(self, message: str) -> Any:            # type: ignore[override]
        raise _UsageError(self, message)


def _tags_arg(value: str) -> list[str]:
    tags = [t.strip() for t in value.split(",") if t.strip()]
    if not tags:
        raise argparse.ArgumentTypeError("give at least one tag name")
    return tags


def _within_days_arg(value: str) -> int | None:
    if value.strip().lower() in ("all", "any", "none"):
        return None
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number of days, or 'all'") from None


def _todo_item(value: str) -> tuple[str, str]:
    return "todo", value


def _heading_item(value: str) -> tuple[str, str]:
    return "heading", value


def _add_globals(p: argparse.ArgumentParser, *, sub: bool) -> None:
    """Global options. Subcommands accept them too; there they default to SUPPRESS so the top level's value
    stands unless the option is repeated after the command."""
    def default(v: Any) -> Any:
        return argparse.SUPPRESS if sub else v

    g = p.add_argument_group("global options")
    g.add_argument("--json", action="store_true", default=default(False),
                   help='print the result as JSON (errors as {"error": {...}})')
    g.add_argument("--no-launch", action="store_true", default=default(False),
                   help="never launch Things (same as THINGS_BRIDGE_NO_LAUNCH=1)")
    g.add_argument("--no-verify", action="store_true", default=default(False),
                   help="don't read Things back after a write")
    g.add_argument("-v", "--verbose", action="count", default=default(0),
                   help="log to stderr (-vv for debug); also shows the URLs sent")


def _page_opts(p: argparse.ArgumentParser, *, limit: int = 50, notes: str = "preview", status: bool = True,
               offset: bool = True) -> None:
    if status:
        p.add_argument("--status", choices=STATUS_FILTERS, default="open", help="open (default) or all")
    p.add_argument("--limit", type=int, default=limit, metavar="N", help=f"at most N items, 1-200 (default {limit})")
    if offset:
        p.add_argument("--offset", type=int, default=0, metavar="N", help="skip the first N items")
    p.add_argument("--notes", choices=NOTES_MODES, default=notes,
                   help=f"notes: none, preview (200 characters) or full (default {notes})")


def _when_opts(p: argparse.ArgumentParser, *, deadline_clear: bool) -> None:
    p.add_argument("--when", metavar="WHEN", help="today, tomorrow, evening, anytime, someday or YYYY-MM-DD")
    p.add_argument("--reminder", "--reminder-time", dest="reminder", metavar="TIME",
                   help="reminder time with --when, e.g. 9:30am or 21:30")
    if deadline_clear:
        g = p.add_mutually_exclusive_group()
        g.add_argument("--deadline", metavar="DATE", help="today, tomorrow or YYYY-MM-DD")
        g.add_argument("--clear-deadline", action="store_true", help="remove the deadline")
    else:
        p.add_argument("--deadline", metavar="DATE", help="today, tomorrow or YYYY-MM-DD")


def _tags_opt(p: argparse.ArgumentParser, help: str, *, dest: str = "tags") -> None:
    p.add_argument("--tags", "--tag", dest=dest, action="extend", type=_tags_arg, metavar="TAGS", help=help)


def _write_opts(p: argparse.ArgumentParser, *, confirm: bool = False, strict_tags: bool = False,
                reveal: bool = False) -> None:
    p.add_argument("--dry-run", action="store_true", help="show the URL that would be sent; send nothing")
    if confirm:
        p.add_argument("--confirm", action="store_true", help="required for more than 10 items at once")
    if strict_tags:
        p.add_argument("--strict-tags", action="store_true", help="fail on unknown tags instead of dropping them")
    if reveal:
        p.add_argument("--reveal", action="store_true", help="show the result in Things")


def _patch_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("id", metavar="ID")
    p.add_argument("--title", metavar="TITLE", help="new title")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--notes", metavar="TEXT|-", help="replace the notes ('-' reads stdin)")
    g.add_argument("--clear-notes", action="store_true", help="remove the notes")
    p.add_argument("--prepend-notes", metavar="TEXT|-", help="add text before the notes")
    p.add_argument("--append-notes", metavar="TEXT|-", help="add text after the notes")
    _when_opts(p, deadline_clear=True)
    g = p.add_mutually_exclusive_group()
    _tags_opt(g, "replace all tags (comma-separated; existing tags only)")
    g.add_argument("--clear-tags", action="store_true", help="remove all tags")
    p.add_argument("--add-tags", dest="add_tags", action="extend", type=_tags_arg, metavar="TAGS",
                   help="add tags, keeping the others")


def build_parser() -> argparse.ArgumentParser:
    common = _Parser(add_help=False, allow_abbrev=False)
    _add_globals(common, sub=True)
    parser = _Parser(prog=PROG, allow_abbrev=False, formatter_class=argparse.RawDescriptionHelpFormatter,
                     description=("Safe command line for Things 3: reads with read-only AppleScript and writes\n"
                                  "with Things URLs. It never touches the database and never deletes anything."),
                     epilog=EPILOG)
    _add_globals(parser, sub=False)
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", title="commands", required=True)

    def cmd(name: str, func: Callable[[_Ctx], int] | None, help: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, parents=[common], help=help, description=help, allow_abbrev=False)
        if func is not None:
            p.set_defaults(func=func)
        return p

    # ---- reads
    p = cmd("list", _cmd_list, "list the to-dos in a Things list")
    p.add_argument("list_key", choices=service.LIST_KEYS, metavar="LIST", help=", ".join(service.LIST_KEYS))
    _page_opts(p)
    for key in service.LIST_KEYS:
        p = cmd(key, _cmd_list, f"same as `list {key}`")
        p.set_defaults(list_key=key)
        _page_opts(p)
    p = cmd("logbook", _cmd_logbook, "recently completed and canceled items")
    p.add_argument("--since", default="7d", metavar="WHEN",
                   help="YYYY-MM-DD, Nd or Nw (default 7d), or 'all' for no date limit")
    _page_opts(p, status=False, offset=False)
    p = cmd("get", _cmd_get, "one to-do, project or area, with its full notes")
    p.add_argument("id", metavar="ID")
    p.add_argument("--children", action="store_true", help="also list a project's or area's contents")
    p.add_argument("--notes", choices=NOTES_MODES, default="full", help="notes: none, preview or full (default)")
    p = cmd("projects", _cmd_projects, "list projects")
    p.add_argument("--area", metavar="ID|TITLE", help="only projects in this area")
    _page_opts(p, limit=100, notes="none")
    cmd("areas", _cmd_areas, "list areas")
    cmd("tags", _cmd_tags, "list tags")
    p = cmd("search", _cmd_search, "search titles and notes (case- and accent-insensitive)")
    p.add_argument("query", nargs="+", metavar="QUERY")
    p.add_argument("--include-completed", action="store_true", help="also search the recent Logbook")
    p.add_argument("--logbook-window", type=int, default=100, metavar="N",
                   help="how many Logbook items to search, 10-500 (default 100)")
    _page_opts(p, status=False, offset=False)
    p = cmd("find", _cmd_find, "filter by tags, project or area (at least one)")
    _tags_opt(p, "any of these tags (comma-separated or repeated)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--project", metavar="ID|TITLE")
    g.add_argument("--area", metavar="ID|TITLE")
    _page_opts(p)
    p = cmd("deadlines", _cmd_deadlines, "open items with deadlines, soonest first")
    p.add_argument("--within-days", type=_within_days_arg, default=14, metavar="N",
                   help="deadlines up to N days ahead, 0-365 (default 14), or 'all'")
    p.add_argument("--no-overdue", action="store_true", help="leave out overdue items")
    p.add_argument("--no-projects", action="store_true", help="leave out projects")
    p.add_argument("--limit", type=int, default=50, metavar="N", help="at most N items, 1-200 (default 50)")
    p = cmd("doctor", _cmd_doctor, "check the setup (read-only)")
    p.add_argument("--prompt-automation", action="store_true",
                   help="wait up to 130 s for the macOS Automation dialog, so you can click OK")
    p.add_argument("--probe", action="store_true", help="also probe Things' AppleScript features")
    p.add_argument("--check-repeating", metavar="ID",
                   help="would the repeating-to-do guard protect this to-do? (Share → Copy Link in Things)")

    # ---- writes
    p = cmd("add", _cmd_add, "add a to-do")
    p.add_argument("title", nargs="+", metavar="TITLE", help="the title ('-' reads the title and notes from stdin)")
    p.add_argument("--notes", metavar="TEXT|-", help="notes ('-' reads stdin)")
    _when_opts(p, deadline_clear=False)
    _tags_opt(p, "tags (comma-separated or repeated; existing tags only)")
    p.add_argument("--checklist", action="append", metavar="ITEM", help="a checklist item (repeat for more)")
    p.add_argument("--project", metavar="ID|TITLE", help="put it in this project")
    p.add_argument("--area", metavar="ID|TITLE", help="put it in this area")
    p.add_argument("--heading", metavar="TITLE", help="under this heading of --project")
    _write_opts(p, strict_tags=True, reveal=True)
    p = cmd("add-many", _cmd_add_many, "add several to-dos at once (one JSON URL)")
    p.add_argument("file", metavar="FILE|-",
                   help="a JSON array of titles or to-do objects, or one title per line ('-' reads stdin)")
    _write_opts(p, confirm=True, strict_tags=True)
    p = cmd("add-project", _cmd_add_project, "add a project, optionally with to-dos and headings")
    p.add_argument("title", nargs="+", metavar="TITLE")
    p.add_argument("--notes", metavar="TEXT|-", help="notes ('-' reads stdin)")
    _when_opts(p, deadline_clear=False)
    _tags_opt(p, "tags (comma-separated or repeated; existing tags only)")
    p.add_argument("--area", metavar="ID|TITLE", help="put it in this area")
    p.add_argument("--todo", dest="items", action="append", type=_todo_item, metavar="TITLE",
                   help="a to-do in the project (repeat; order is kept)")
    p.add_argument("--heading", dest="items", action="append", type=_heading_item, metavar="TITLE",
                   help="a heading in the project (repeat; order is kept)")
    p.add_argument("--items", dest="items_file", metavar="FILE|-",
                   help='a JSON array of titles or {"kind": "todo"|"heading", "title": ...} objects')
    _write_opts(p, strict_tags=True, reveal=True)
    p = cmd("update", _cmd_update, "change a to-do")
    _patch_opts(p)
    p.add_argument("--checklist-append", action="append", metavar="ITEM", help="add a checklist item at the end")
    p.add_argument("--checklist-prepend", action="append", metavar="ITEM", help="add a checklist item at the top")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--project", metavar="ID|TITLE", help="move it to this project")
    g.add_argument("--area", metavar="ID|TITLE", help="move it to this area")
    p.add_argument("--heading", metavar="TITLE", help="file it under this heading of its project")
    _write_opts(p, strict_tags=True)
    p = cmd("update-project", _cmd_update_project, "change a project")
    _patch_opts(p)
    p.add_argument("--area", metavar="ID|TITLE", help="move it to this area")
    _write_opts(p, strict_tags=True)
    for name, status, help in (("complete", "completed", "mark items completed"),
                               ("cancel", "canceled", "mark items canceled (reversible; they go to the Logbook)"),
                               ("reopen", "open", "mark completed or canceled items open again")):
        p = cmd(name, _cmd_status, help)
        p.set_defaults(status=status)
        p.add_argument("ids", nargs="+", metavar="ID", help="item ids ('-' reads them from stdin)")
        _write_opts(p, confirm=True)
    p = cmd("schedule", _cmd_schedule, "set when and/or the deadline of items")
    p.add_argument("ids", nargs="+", metavar="ID", help="item ids ('-' reads them from stdin)")
    _when_opts(p, deadline_clear=True)
    _write_opts(p, confirm=True)
    p = cmd("move", _cmd_move, "move items to a project or an area")
    p.add_argument("ids", nargs="+", metavar="ID", help="item ids ('-' reads them from stdin)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--project", metavar="ID|TITLE")
    g.add_argument("--area", metavar="ID|TITLE")
    p.add_argument("--heading", metavar="TITLE", help="under this heading of --project")
    _write_opts(p, confirm=True)
    p = cmd("tag", _cmd_tag, "add or replace the tags of items (existing tags only)")
    p.add_argument("ids", nargs="+", metavar="ID", help="item ids ('-' reads them from stdin)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--add", action="extend", type=_tags_arg, metavar="TAGS", help="add these tags")
    g.add_argument("--replace", action="extend", type=_tags_arg, metavar="TAGS", help="replace all tags")
    g.add_argument("--clear", action="store_true", help="remove all tags")
    _write_opts(p, confirm=True)
    p = cmd("json", _cmd_json, "send a Things JSON payload (list and area by id only)")
    p.add_argument("file", metavar="FILE|-", help="the JSON array ('-' reads stdin)")
    _write_opts(p, confirm=True, reveal=True)

    # ---- navigation
    p = cmd("show", _cmd_show, "show an item, list or area in Things (brings Things to the front)")
    p.add_argument("id", nargs="?", metavar="ID", help="an item or area id")
    p.add_argument("--list", dest="list_key", choices=urlscheme.SHOW_BUILTIN_IDS, metavar="LIST",
                   help=", ".join(urlscheme.SHOW_BUILTIN_IDS))
    p.add_argument("--query", metavar="NAME", help="the name of an area, project, tag or list")
    p.add_argument("--filter", action="extend", type=_tags_arg, metavar="TAGS", help="only items with these tags")
    p = cmd("open-search", _cmd_open_search, "open Things' search window")
    p.add_argument("query", nargs="*", metavar="QUERY")

    # ---- CLI only (§3.4)
    p = cmd("token", None, "store or check the Things URL auth token (never shown)")
    tsub = p.add_subparsers(dest="token_command", metavar="ACTION", required=True)
    tp = tsub.add_parser("set", parents=[common], allow_abbrev=False,
                         help="store the token in the login Keychain (you paste it; it isn't echoed)")
    tp.add_argument("--stdin", action="store_true", help="read the token from stdin instead of prompting")
    tp.set_defaults(func=_cmd_token_set)
    tp = tsub.add_parser("status", parents=[common], allow_abbrev=False,
                         help="where the token comes from and whether its format is valid (exit 3 if not)")
    tp.set_defaults(func=_cmd_token_status)
    cmd("setup", _cmd_setup, "interactive first-run setup")
    p = cmd("selftest", _cmd_selftest, "live end-to-end test on its own items (asks for consent)")
    p.add_argument("--yes", action="store_true", help="consent without the prompt")
    p.add_argument("--probe-conflicts", action="store_true", help="also probe documented-vs-observed conflicts")
    cmd("version", _cmd_version, "versions of things-bridge, the URL scheme and Things")
    p = cmd("mcp", _cmd_mcp, "run the MCP server on stdio")
    p.add_argument("--read-only", action="store_true", help="register only the read and navigation tools")
    return parser


# ---- errors and logging --------------------------------------------------------------------------------------

class _InternalError(errors.ThingsBridgeError):
    code, exit_code = "internal_error", EXIT_OTHER


def _report_human(e: errors.ThingsBridgeError) -> None:
    _err(f"error: {e.message}")
    if e.hint:
        _err(f"hint: {e.hint}")
    for line in _detail_lines(e.details):
        _err("  " + line)


def _report(ctx: _Ctx, e: errors.ThingsBridgeError) -> int:
    if ctx.json:
        _out(_dump({"error": e.to_dict()}))
    else:
        _report_human(e)
    return e.exit_code


def _wants_json(argv: Sequence[str]) -> bool:
    head = list(argv[:list(argv).index("--")]) if "--" in argv else list(argv)
    return "--json" in head


def _secretish(argv: Sequence[str]) -> list[str]:
    """Values a user may have typed as a token (``--token X``, ``token set X``), to keep out of usage errors."""
    found = []
    for i, arg in enumerate(argv):
        if re.match(r"(?i)--?[\w-]*(token|auth)", arg):
            if "=" in arg:
                found.append(arg.split("=", 1)[1])
            elif i + 1 < len(argv):
                found.append(argv[i + 1])
    if "token" in argv:
        found += [a for a in argv[list(argv).index("token") + 1:] if a not in ("set", "status") and
                  not a.startswith("-")]
    return sorted({s for s in found if s}, key=len, reverse=True)


def _usage_error(e: _UsageError, argv: Sequence[str], json_mode: bool) -> int:
    message = e.message
    for secret in _secretish(argv):
        message = message.replace(secret, "***")
    if json_mode:
        _out(_dump({"error": {"code": "usage", "message": message, "hint": f"see `{e.parser.prog} --help`",
                              "details": {}}}))
    else:
        _err(redact(e.parser.format_usage()).rstrip())
        _err(f"{e.parser.prog}: error: {message}")
    return EXIT_USAGE


_HANDLER_FLAG = "_things_bridge_cli"


def _log_level(verbosity: int) -> int:
    if verbosity >= 2:
        return logging.DEBUG
    if verbosity == 1:
        return logging.INFO
    raw = os.environ.get(LOG_ENV, "").strip()
    if raw.isdigit():
        return int(raw)
    level = logging.getLevelName(raw.upper()) if raw else logging.WARNING
    return level if isinstance(level, int) else logging.WARNING


def _setup_logging(verbosity: int) -> None:
    """Package logs to stderr through ``RedactFilter`` on the handler (§5.8), replacing an earlier CLI handler."""
    logger = logging.getLogger(__package__)
    for h in [h for h in logger.handlers if getattr(h, _HANDLER_FLAG, False)]:
        logger.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(RedactFilter())
    handler.setFormatter(logging.Formatter(f"{PROG}: %(levelname)s: %(message)s"))
    setattr(handler, _HANDLER_FLAG, True)
    logger.addHandler(handler)
    logger.setLevel(_log_level(verbosity))


def _silence_stdout() -> None:
    """After a broken pipe (``things-bridge today | head``), keep the interpreter's final flush quiet."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        pass


# ---- entry point -------------------------------------------------------------------------------------------

def main(argv: Sequence[str] | None = None, *, runner: Runner | None = None,
         today: Callable[[], date] | None = None) -> int:
    """Run one command and return its exit code. ``runner`` and ``today`` are for tests (default: ``MacRunner``
    and ``date.today``)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not argv:
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    try:
        args = parser.parse_args(argv)
    except _UsageError as e:
        return _usage_error(e, argv, _wants_json(argv))
    except SystemExit as e:                     # --help, --version
        return e.code if isinstance(e.code, int) else (EXIT_OK if e.code is None else EXIT_USAGE)
    ctx = _Ctx(args, runner, today)
    if args.command != "mcp":                   # the MCP server sets up its own stderr logging
        _setup_logging(ctx.verbose)
    try:
        return args.func(ctx)
    except errors.ThingsBridgeError as e:
        return _report(ctx, e)
    except KeyboardInterrupt:
        _err("interrupted")
        return EXIT_INTERRUPTED
    except BrokenPipeError:
        _silence_stdout()
        return EXIT_OTHER
    except Exception as e:                      # a bug: report it without a traceback unless -vv
        log.debug("unexpected error", exc_info=True)
        return _report(ctx, _InternalError(f"internal error: {type(e).__name__}: {_clip(str(e), 300)}",
                                           hint="re-run with -vv for the traceback"))
