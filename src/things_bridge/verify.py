"""Post-write read-back (§2.8): poll Things with read-only templates until a change shows, or time out.

URL writes are fire-and-forget (D6), so every write is checked by reading Things back. This module only
reads: a write that doesn't show up is reported (``verified=False`` plus a reason), never re-sent (N10).

* Created items are found with ``FIND_CREATED`` (title + creation age, scoped to where the item should
  land; the final poll is app-wide with the ``D`` diagnostic row).
* Updates are checked field by field (``Expect``) against ``ROWS``, plus ``MEMBERSHIP`` for Someday /
  Anytime and ``SCAN(project)`` for a new project's to-dos.
"""

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Sequence

from . import applescript, errors, urlscheme
from .models import Check, Item, ItemKind, ProjectPatch, TodoPatch
from .runner import Runner

log = logging.getLogger(__name__)

ExpectOp = Literal["eq", "startswith", "endswith", "superset", "none", "member", "not_member"]
FIELDS = ("title", "notes", "deadline", "start_date", "status", "tags", "project_id", "area_id", "member",
          "children")
MEMBER_KEYS = ("inbox", "today", "anytime", "upcoming", "someday")
ROWS_CHUNK = 200                            # ROWS takes at most 200 ids
_EPS = 1e-6                                 # float slack when comparing clock readings
_CLIP = 200                                 # longest notes text echoed in a Check

_CAUSES = {
    "urls": ("'Enable Things URLs' is off. Things shows an Enable prompt and holds the command, which will still "
             "apply if you click Enable."),
    "token": ("The auth token is wrong or stale; Things shows an error in its window. Re-run "
              "`things-bridge token set`."),
    "dialog": "A dialog is open in Things.",
    "repeating": "It's a repeating item.",
}
NOTHING_RESENT = "Nothing was re-sent; check Things before retrying."
DIALOG_REASON = "Things is showing a dialog (possibly 'Enable Things URLs?'); answer it in Things"


def not_verified_reason(seconds: float, *, update: bool = True) -> str:
    """The §5.1 not-verified text. Creates need no token and can't hit a repeating template, so ``update=False``
    leaves those two causes out."""
    keys = ("urls", "token", "dialog", "repeating") if update else ("urls", "dialog")
    causes = "\n".join(f"({i}) {_CAUSES[k]}" for i, k in enumerate(keys, 1))
    return f"Things didn't apply the change within {seconds:g} s. Likely causes:\n{causes}\n\n{NOTHING_RESENT}"


def dialog_reason() -> str:
    return (f"{DIALOG_REASON}. If it is the Enable prompt, the held command still applies when you click Enable. "
            f"{NOTHING_RESENT}")


def read_failed_reason(e: errors.ThingsBridgeError) -> str:
    return f"The change was sent, but reading Things back failed ([{e.code}] {e.message}). {NOTHING_RESENT}"


@dataclass
class Expect:
    target_id: str
    field: str                              # one of FIELDS
    op: ExpectOp
    value: str | list[str] | None           # member/not_member: a MEMBER_KEYS list key


@dataclass
class Verification:
    verified: bool | None                   # None: nothing to check
    checks: list[Check]
    unverifiable: list[str]
    reason: str | None
    items: list[Item] = field(default_factory=list)     # the last read-back, in target order


class Verifier:
    """Polls reads after a dispatch: first after ``initial_delay_s``, then every ``interval_s``, until
    ``timeout_s`` (``launched_timeout_s`` when Things was just launched). Never dispatches.

    Diagnostics of the last ``find_created`` call are left in ``warnings`` (several new items share a title)
    and ``diagnosis`` (the ``D`` row says a dialog is open); a read failure stops polling and is kept in
    ``error``.
    """

    def __init__(self, runner: Runner, *, timeout_s: float = 6.0, launched_timeout_s: float = 15.0,
                 initial_delay_s: float = 0.3, interval_s: float = 0.5):
        if timeout_s < 0 or launched_timeout_s < 0 or initial_delay_s < 0 or interval_s <= 0:
            raise ValueError("verification timings must be non-negative (interval positive)")
        self.runner = runner
        self.timeout_s = timeout_s
        self.launched_timeout_s = launched_timeout_s
        self.initial_delay_s = initial_delay_s
        self.interval_s = interval_s
        self.warnings: list[str] = []
        self.diagnosis: str | None = None
        self.error: errors.ThingsBridgeError | None = None

    def timeout(self, launched: bool) -> float:
        return max(self.timeout_s, self.launched_timeout_s) if launched else self.timeout_s

    # ---- created items ----------------------------------------------------------------------------

    def find_created(self, *, kind: ItemKind, titles: Sequence[str], scope: tuple[str, str], t0: float,
                     launched: bool) -> dict[str, list[str]]:
        """``title -> new ids`` (youngest first; at most as many as ``titles`` lists that title).

        ``t0`` is the dispatch's wall time: the poll deadline and ``maxAge`` are measured from it, so several
        calls for one dispatch (one per destination scope) share one time budget.
        """
        self.warnings, self.diagnosis, self.error = [], None, None
        want: dict[str, int] = {}
        for t in titles:
            want[t] = want.get(t, 0) + 1
        found: dict[str, dict[str, int]] = {t: {} for t in want}      # title -> {id: age}
        budget = self.timeout(launched) - max(0.0, self.runner.wall() - t0)
        deadline = self.runner.monotonic() + max(0.0, budget)
        self.runner.sleep(self.initial_delay_s)
        while True:
            remaining = deadline - self.runner.monotonic()
            final = remaining <= _EPS
            pending = [t for t in want if len(found[t]) < want[t]]
            poll_scope = ("app", "") if final else scope      # list scopes survive a sheet; app is the last resort
            for i, title in enumerate(pending):
                diag = final and i == 0
                max_age = int(self.runner.wall() - t0) + 5
                try:
                    frame = self.runner.run_template(
                        "FIND_CREATED", [kind, title, str(max_age), poll_scope[0], poll_scope[1], "1" if diag else "0"])
                except errors.ThingsBridgeError as e:
                    log.warning("verification read failed: %s", e.message)
                    self.error = e
                    return self._created(want, found)
                for id_, age in applescript.created_from_frame(frame):
                    found[title][id_] = min(age, found[title].get(id_, age))
                if diag:
                    d = applescript.diag_from_frame(frame)
                    if d is not None and d[0] == 0 and d[1] > 0:
                        self.diagnosis = DIALOG_REASON
            if final or all(len(found[t]) >= want[t] for t in want):
                return self._created(want, found)
            self.runner.sleep(min(self.interval_s, remaining))

    def _created(self, want: dict[str, int], found: dict[str, dict[str, int]]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for title, n in want.items():
            ids = [i for i, _ in sorted(found[title].items(), key=lambda kv: kv[1])]
            if len(ids) > n:
                self.warnings.append(f"{len(ids)} items titled {_clip(title, 60)!r} were created just now; "
                                     f"assuming the youngest {'is' if n == 1 else 'are'} the new one{'s' * (n > 1)}.")
            out[title] = ids[:n]
        return out

    # ---- field checks -----------------------------------------------------------------------------

    def check(self, expects: Sequence[Expect], *, launched: bool) -> Verification:
        """Poll until every expectation holds (True) or the timeout passes (False); None without expectations."""
        self.error = None
        if not expects:
            return Verification(None, [], [], None)
        ids = list(dict.fromkeys(e.target_id for e in expects))
        want_notes = any(e.field == "notes" for e in expects)
        keys = [k for k in MEMBER_KEYS if any(e.field == "member" and e.value == k for e in expects)]
        projects = list(dict.fromkeys(e.target_id for e in expects if e.field == "children"))
        timeout = self.timeout(launched)
        deadline = self.runner.monotonic() + timeout
        self.runner.sleep(self.initial_delay_s)
        checks: list[Check] = []
        items: dict[str, Item] = {}
        while True:
            try:
                items = self._items(ids, want_notes)
                members = self._membership(keys)
                children = {pid: self._children(pid) for pid in projects}
            except errors.ThingsBridgeError as e:
                log.warning("verification read failed: %s", e.message)
                self.error = e
                return Verification(False, checks, [], read_failed_reason(e), [items[i] for i in ids if i in items])
            checks = [_evaluate(e, items, members, children) for e in expects]
            ordered = [items[i] for i in ids if i in items]
            if all(c.ok for c in checks):
                return Verification(True, checks, [], None, ordered)
            remaining = deadline - self.runner.monotonic()
            if remaining <= _EPS:
                return Verification(False, checks, [], not_verified_reason(timeout), ordered)
            self.runner.sleep(min(self.interval_s, remaining))

    def _items(self, ids: list[str], want_notes: bool) -> dict[str, Item]:
        out: dict[str, Item] = {}
        for i in range(0, len(ids), ROWS_CHUNK):
            frame = self.runner.run_template("ROWS", ["1" if want_notes else "0", *ids[i:i + ROWS_CHUNK]])
            for it in applescript.items_from_frame(frame, notes_mode="full" if want_notes else "none"):
                out.setdefault(it.id, it)
        return out

    def _membership(self, keys: list[str]) -> dict[str, set[str]]:
        if not keys:
            return {}
        frame = self.runner.run_template("MEMBERSHIP", keys)
        return {k: set(v) for k, v in applescript.membership_from_frame(frame).items()}

    def _children(self, pid: str) -> list[str]:
        frame = self.runner.run_template("SCAN", ["project", pid, "0", "0"])
        return [r.title for r in applescript.scan_rows(frame)]


# ---- evaluation -------------------------------------------------------------------------------------

def _norm(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


def _clip(s: str, n: int = _CLIP) -> str:
    return s if len(s) <= n else s[:n] + "…"


def _fold_set(values: Sequence[str]) -> set[str]:
    return {v.strip().casefold() for v in values}


def _observe(e: Expect, items: dict[str, Item], members: dict[str, set[str]],
             children: dict[str, list[str]]) -> tuple[bool, object]:
    """(found, observed value) for one expectation."""
    if e.field == "member":
        return True, [k for k in MEMBER_KEYS if e.target_id in members.get(k, ())]
    if e.field == "children":
        return e.target_id in children, children.get(e.target_id)
    it = items.get(e.target_id)
    if it is None:
        return False, None
    if e.field == "project_id":
        return True, it.project.id if it.project else None
    if e.field == "area_id":
        return True, it.area.id if it.area else None
    if e.field not in FIELDS:
        raise ValueError(f"unknown field {e.field!r}")
    return True, getattr(it, e.field)


def _matches(e: Expect, observed: object) -> bool:
    op, value = e.op, e.value
    if op == "none":
        return observed is None or observed == "" or observed == []
    if op in ("member", "not_member"):
        return (value in observed) == (op == "member")
    if observed is None:
        return False
    if isinstance(observed, list):
        if op == "eq":
            return _fold_set(observed) == _fold_set(value or [])
        if op == "superset":
            return _fold_set(observed) >= _fold_set(value or [])
        return False
    if not isinstance(value, str):
        return False
    obs = _norm(observed) if e.field == "notes" else observed
    want = _norm(value) if e.field == "notes" else value
    if op == "eq":
        return obs == want
    if op == "startswith":
        return obs.startswith(want)
    if op == "endswith":
        return obs.endswith(want)
    return False


def _show(value: object, field: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        return ", ".join(value)
    s = str(value)
    return _clip(s) if field == "notes" else s


def _expected(e: Expect) -> str | None:
    v = _show(e.value, e.field)
    return {"eq": v, "startswith": f"{v}…", "endswith": f"…{v}", "superset": f"includes {v}", "none": None,
            "member": f"in {v}", "not_member": f"not in {v}"}[e.op]


def _evaluate(e: Expect, items: dict[str, Item], members: dict[str, set[str]],
              children: dict[str, list[str]]) -> Check:
    found, observed = _observe(e, items, members, children)
    ok = found and _matches(e, observed)
    shown = _show(observed, e.field) if found else "(not found)"
    if e.field == "member":                 # only the queried lists are known
        shown = ", ".join(observed) or "not in " + ", ".join(k for k in MEMBER_KEYS if k in members)
    return Check(target_id=e.target_id, field=e.field, expected=_expected(e), observed=shown, ok=ok)


# ---- expectations -------------------------------------------------------------------------------------

def when_expects(id: str, when: str, today: date) -> tuple[list[Expect], list[str]]:
    """Checks for a canonical ``when`` value (optionally ``@time``), and what can't be checked."""
    day = when.partition("@")[0].strip().lower()
    unverifiable = ["reminder time"] if "@" in when else []
    if day == "evening":
        unverifiable.append("evening")
    if day in ("someday", "anytime"):
        return [Expect(id, "start_date", "none", None), Expect(id, "member", "member", day)], unverifiable
    return [Expect(id, "start_date", "eq", urlscheme.resolve_day(when, today=today))], unverifiable


def deadline_expect(id: str, deadline: str, today: date) -> Expect:
    return Expect(id, "deadline", "eq", urlscheme.resolve_day(deadline, today=today))


def expectations_for_todo_patch(id: str, patch: TodoPatch | ProjectPatch, resolved: dict, before: Item | None,
                                today: date) -> tuple[list[Expect], list[str]]:
    """``(expects, unverifiable)`` for an update (§2.8 table). Also takes a ``ProjectPatch``.

    ``resolved`` overrides patch values with what was actually sent: ``title`` (trimmed), ``when`` (composed,
    with ``@time``), ``deadline`` (normalised), ``tags`` / ``add_tags`` (canonical existing tags; None = not
    sent) and ``project_id`` / ``area_id`` (resolved container ids). ``before`` is the pre-write snapshot; a
    to-do moved into an area leaves its project, which is checked too.
    """
    ex: list[Expect] = []
    unverifiable: list[str] = []

    def pick(key: str) -> object:
        return resolved[key] if key in resolved else getattr(patch, key, None)

    title = pick("title")
    if title is not None:
        ex.append(Expect(id, "title", "eq", _norm(title)))
    if patch.notes is not None:
        ex.append(Expect(id, "notes", "eq", _norm(patch.notes)) if patch.notes else Expect(id, "notes", "none", None))
    if patch.prepend_notes:
        ex.append(Expect(id, "notes", "startswith", _norm(patch.prepend_notes)))
    if patch.append_notes:
        ex.append(Expect(id, "notes", "endswith", _norm(patch.append_notes)))
    when = pick("when")
    if when is not None:
        more, unv = when_expects(id, when, today)
        ex += more
        unverifiable += unv
    if patch.clear_deadline:
        ex.append(Expect(id, "deadline", "none", None))
    else:
        deadline = pick("deadline")
        if deadline is not None:
            ex.append(deadline_expect(id, deadline, today))
    tags = pick("tags")
    if tags is not None:
        ex.append(Expect(id, "tags", "eq", list(tags)))
    add_tags = pick("add_tags")
    if add_tags:
        ex.append(Expect(id, "tags", "superset", list(add_tags)))
    if getattr(patch, "checklist_append", None) or getattr(patch, "checklist_prepend", None):
        unverifiable.append("checklist")
    project_id = resolved.get("project_id")
    if project_id:
        ex.append(Expect(id, "project_id", "eq", project_id))
    area_id = resolved.get("area_id")
    if area_id:
        ex.append(Expect(id, "area_id", "eq", area_id))
        if (before is None or before.kind == "to-do") and isinstance(patch, TodoPatch):
            ex.append(Expect(id, "project_id", "none", None))
    if getattr(patch, "heading", None):
        unverifiable.append("heading")
    return ex, list(dict.fromkeys(unverifiable))
