"""Consented live end-to-end test on the selftest's own items (§2.11, §6.2).

``run_selftest`` shows on the user's Mac, with the safe methods only, that Things URLs are enabled, Automation is
granted, the token is valid, encoding round-trips, the read templates parse and verification works. It also
answers the §6.1 questions it can, as ``Q…`` info steps. It creates a project named ``things-bridge selftest``
holding a few to-dos, changes only those, and at the end completes or cancels them, so everything ends up in the
Logbook. Nothing is ever deleted.

Hard invariants (§6.2):

1. Every write passes ``OwnedRunner.dispatch``, which raises ``Refused`` before anything is sent unless every id the
   URL or json payload targets is in ``created``. The selftest's ``ThingsService`` runs on that runner, so writes
   the service issues are covered as well.
2. ``created`` only receives ids that ``FIND_CREATED`` returned for items made since the run started, whose title
   carries the run token ``tb-<8 hex>``. The project's title is fixed, so its notes carry the token instead.
3. No area, no list or area title, nothing moved out of the selftest's project. The one existing tag it may use is
   applied only to the selftest's own item, under ``--probe-conflicts``.
4. Reads may see the user's lists, but checks only look at the selftest's own ids.
5. After a failure no later step runs. Close-out (ST-13) then completes or cancels only ``created`` items.
6. The repeating guard skips ``created`` ids, and only those. The URL scheme has no parameter that makes an item
   repeat (OFF-URL), so nothing this run created can be a repeating template. Every other id still gets the guard.
"""

import json
import logging
import os
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any, Callable, ContextManager, Sequence

from . import applescript, doctor, errors, jsoncmd, service, urlscheme
from .errors import InvalidInput, NotFound, Refused
from .models import Item, ProjectPatch, ProjectSpec, SelftestReport, SelftestStep, TodoPatch, TodoSpec, link_for
from .runner import Dispatch, Runner, redact

log = logging.getLogger(__name__)

SELFTEST_PROJECT_TITLE = "things-bridge selftest"
NOTES_PREFIX = "Created by things-bridge selftest"
RUN_PREFIX = "tb-"                         # + 8 hex digits: the run token in every title (ST-1: in the notes)
UNICODE_TAIL = " ünïcødé 🙂 & / + # = % , @ “q”"        # ST-8, after "-" + the run token
LIST_KEYS = ("inbox", "today", "anytime", "upcoming", "someday")    # MEMBERSHIP(all); trash/tomorrow are Q2
POLL_S = 0.5
AGE_SLACK_S = 2                            # FIND_CREATED ages are whole seconds
LAUNCHED_TIMEOUT_S = 15.0                  # §2.8: longer read-back right after a launch
CONFLICT_WAIT_S = 6.0                      # ST-12 (c)
MIN_BATCH = 10                             # the selftest's own batches hold at most 5 items

NO_TOKEN = "skip (no token)"
AFTER_FAILURE = "not run: an earlier step failed"
REFUSED_HINT = "the selftest only changes items it created in this run"

Progress = Callable[[SelftestStep], None]
_Result = tuple[str, str, list[str]]


# ---- OwnedRunner: the write gate ----------------------------------------------------------------------

_OWNED_KEYS = ("id", "list-id", "heading-id")
_NEVER_KEYS = {                            # key -> what it would use; refused outright (invariant 3, N13)
    "list": "a list title", "area": "an area title", "area-id": "an area", "duplicate": "duplicate",
    "use-clipboard": "the clipboard", "show-quick-entry": "Quick Entry", "creation-date": "a creation date",
    "completion-date": "a completion date",
}
_NESTED_TYPES = ("to-do", "project", "heading", "checklist-item")


def _owned(value: object, created: set[str]) -> bool:
    return isinstance(value, str) and value in created


def _show(value: object) -> str:
    s = repr(value)
    return s if len(s) <= 40 else s[:40] + "…"


def ownership_problem(turl: urlscheme.ThingsURL, created: set[str]) -> str | None:
    """Why the selftest may not send ``turl``; None when it only creates or only targets ids in ``created``."""
    command = turl.command
    if command not in urlscheme.MUTATING:          # navigation (show, search, version) is never needed
        return f"the selftest never sends {_show(command)} URLs"
    keys = [k for k, _ in turl.params]
    for key, value in turl.params:
        if key in _NEVER_KEYS:
            return f"the URL would use {_NEVER_KEYS[key]} ({key})"
        if key in _OWNED_KEYS and not _owned(value, created):
            return f"{key} {_show(value)} wasn't created by this run"
    if command in ("update", "update-project") and "id" not in keys:
        return f"an {command} URL without an id"
    if command == "json":
        data = dict(turl.params).get("data")
        try:
            payload = json.loads(data) if isinstance(data, str) else None
        except ValueError:
            payload = None
        if not isinstance(payload, list):
            return "json data that isn't an array"
        for i, obj in enumerate(payload):
            problem = _json_problem(obj, f"/{i}", created, top=True)
            if problem is not None:
                return problem
    return None


def _json_problem(obj: object, path: str, created: set[str], *, top: bool) -> str | None:
    if not isinstance(obj, dict):
        return f"{path} isn't an object"
    kind = obj.get("type")
    if kind not in (("to-do", "project") if top else _NESTED_TYPES):
        return f"{path} has type {_show(kind)}"
    op = obj.get("operation", "create")
    if op == "update":
        if not top:
            return f"{path} is a nested update"
        if not _owned(obj.get("id"), created):
            return f"{path} updates {_show(obj.get('id'))}, which wasn't created by this run"
    elif op != "create":
        return f"{path} has operation {_show(op)}"
    elif obj.get("id") is not None:
        return f"{path} creates an item with an id"
    attrs = obj.get("attributes", {})
    if not isinstance(attrs, dict):
        return f"{path}/attributes isn't an object"
    for key, value in attrs.items():
        if value is None:
            continue
        if key in _NEVER_KEYS:
            return f"{path} would use {_NEVER_KEYS[key]} ({key})"
        if key in _OWNED_KEYS and not _owned(value, created):
            return f"{path}/attributes/{key} {_show(value)} wasn't created by this run"
        if isinstance(value, list):         # items, checklist items (tags are strings: skipped)
            for j, child in enumerate(value):
                if isinstance(child, dict):
                    problem = _json_problem(child, f"{path}/attributes/{key}/{j}", created, top=False)
                    if problem is not None:
                        return problem
    return None


class OwnedRunner(Runner):
    """Wraps the real runner. The selftest builds ``ThingsService(OwnedRunner(runner, created))``, so every write,
    including those the service issues (guards and verification included), passes this check first.

    ``dispatch`` refuses (``Refused``, before anything is sent, dry runs included) unless every ``id``,
    ``list-id`` and ``heading-id`` parameter, and for ``json`` every update operation's ``id`` and every
    ``list-id``/``heading-id`` attribute at any depth, is in ``created``. ``area``/``list`` titles and areas are
    refused outright, as are commands other than add, add-project, update, update-project and json. Creates without
    container parameters are allowed. Reads and the rest delegate unchanged (reads are harmless).
    """

    def __init__(self, runner: Runner, created: set[str]):
        self.inner = runner
        self.created = created              # the live set: ids verified as created by this run

    def dispatch(self, turl: urlscheme.ThingsURL, *, dry_run: bool = False) -> Dispatch:
        problem = ownership_problem(turl, self.created)
        if problem is not None:
            log.warning("selftest refused a %s URL: %s", turl.command, problem)
            raise Refused(f"Selftest safety stop: {problem}. Nothing was sent.", hint=REFUSED_HINT,
                          details={"command": turl.command})
        return self.inner.dispatch(turl, dry_run=dry_run)

    def require_macos(self) -> None:
        self.inner.require_macos()

    def things_running(self) -> bool:
        return self.inner.things_running()

    def ensure_running(self, *, allow_launch: bool) -> bool:
        return self.inner.ensure_running(allow_launch=allow_launch)

    def run_template(self, name: str, args: Sequence[str] = (), *,
                     timeout: float | None = None) -> applescript.Frame:
        return self.inner.run_template(name, args, timeout=timeout)

    def write_lock(self) -> ContextManager[None]:
        return self.inner.write_lock()

    def wall(self) -> float:
        return self.inner.wall()

    def monotonic(self) -> float:
        return self.inner.monotonic()

    def sleep(self, s: float) -> None:
        self.inner.sleep(s)


# ---- the run ----------------------------------------------------------------------------------------

def run_selftest(runner: Runner, config: service.Config, *, yes: bool, probe_conflicts: bool = False,
                 progress: Progress | None = None, today: Callable[[], date] | None = None) -> SelftestReport:
    """Run ST-0..ST-14 (§6.2) and return the report; ``progress`` gets each step as it finishes.

    Consent is the caller's job (the CLI asks, or takes ``--yes``): without ``yes`` this raises ``Refused`` before
    touching anything, and it never prompts. Read-only mode refuses too. ``today`` is for tests.
    """
    if yes is not True:
        raise Refused("The selftest creates items in your Things, so it needs your consent.",
                      hint="run `things-bridge selftest` in Terminal to answer the prompt, or pass --yes")
    config = config if config is not None else service.Config()
    if config.read_only:
        raise Refused("The selftest creates and changes items, and things-bridge is in read-only mode.",
                      hint=f"unset {service.ENV_READ_ONLY} to run it")
    return _Selftest(runner, config, probe_conflicts=probe_conflicts, progress=progress,
                     today=today or date.today).execute()


def _one_line(text: str | None, limit: int = 300) -> str:
    s = " ".join((text or "").split())
    return s if len(s) <= limit else s[:limit] + "…"


def _norm(s: str | None) -> str:
    return (s or "").replace("\r\n", "\n").replace("\r", "\n")


def _ref(r: Any) -> str | None:
    return r.id if r is not None else None


def _check_failed(check: Any, prefix: str = "") -> _Result:
    remedy = f" ({check.remedy})" if check.remedy else ""
    return "fail", f"{prefix}{check.detail}{remedy}", []


class _Checks:
    """Expectations of one step; any failure makes the step fail."""

    def __init__(self) -> None:
        self.failures: list[str] = []

    def expect(self, what: str, ok: object, observed: object = None) -> bool:
        if not ok:
            self.failures.append(what if observed is None else f"{what} (got {observed})")
        return bool(ok)

    def result(self, summary: str, ids: Sequence[str], *, status: str = "pass") -> _Result:
        if self.failures:
            return "fail", f"{summary}; failed: {'; '.join(self.failures)}", list(ids)
        return status, summary, list(ids)


class _Selftest:
    def __init__(self, runner: Runner, config: service.Config, *, probe_conflicts: bool,
                 progress: Progress | None, today: Callable[[], date]):
        self.config = config
        self.probe_conflicts = probe_conflicts
        self.progress = progress
        self.today_fn = today
        self.today = today()
        self.tomorrow = (self.today + timedelta(days=1)).isoformat()
        self.run = RUN_PREFIX + os.urandom(4).hex()
        self.created: set[str] = set()
        self.order: list[str] = []          # created ids, in creation order
        self.runner = OwnedRunner(runner, self.created)
        self._svc: service.ThingsService | None = None    # built by ST-0
        self.t_start = self.runner.wall()
        self.timeout_s = config.verify_timeout_s
        self.steps: list[SelftestStep] = []
        self.answers: list[tuple[str, str, list[str]]] = []     # §6.1 answers of the running step
        self.failed = False
        self.has_token = False
        self.launched = False
        self.ids: dict[str, str] = {}       # role (P, A, beta, G, g1, g2, U) -> id
        self.pending: dict[str, None] = {}  # titles sent but not identified (yet)
        self.left_open: list[str] = []

    # ---- driver --------------------------------------------------------------------------------

    def execute(self) -> SelftestReport:
        self._do("ST-0", self.st0)
        plan = [("ST-1", self.st1, False), ("ST-2", self.st2, False), ("ST-3", self.st3, False),
                ("ST-4", self.st4, True), ("ST-5", self.st5, True), ("ST-6", self.st6, True),
                ("ST-7", self.st7, True), ("ST-8", self.st8, False), ("ST-9", self.st9, False),
                ("ST-10", self.st10, True), ("ST-11", self.st11, True), ("ST-12", self.st12, True)]
        for name, step, needs_token in plan:
            if self.failed:
                self._record(name, "skip", AFTER_FAILURE)
            elif name == "ST-12" and not self.probe_conflicts:
                self._record(name, "skip", "not requested (--probe-conflicts)")
            elif needs_token and not self.has_token:
                self._record(name, "skip", NO_TOKEN)
            else:
                self._do(name, step)
        self._do("ST-13", self.close_out)
        return self.report()

    def _do(self, name: str, step: Callable[[], _Result]) -> None:
        try:
            status, detail, ids = step()
        except errors.ThingsBridgeError as e:
            status, detail, ids = "fail", f"[{e.code}] {e.message}", []
        except Exception as e:              # a bug must never skip the close-out
            log.exception("selftest %s crashed", name)
            status, detail, ids = "fail", f"internal error: {type(e).__name__}: {e}", []
        self._record(name, status, detail, ids)
        answers, self.answers = self.answers, []
        for q, text, q_ids in answers:     # a step's Q-answers print after the step itself
            self._record(q, "info", text, q_ids)
        if status == "fail":
            self.failed = True

    def _record(self, name: str, status: str, detail: str, ids: Sequence[str] = ()) -> None:
        step = SelftestStep(name=name, status=status, detail=redact(detail), ids=list(ids))
        self.steps.append(step)
        if self.progress is not None:
            try:
                self.progress(step)
            except Exception:               # a broken progress printer must not stop the run (or its close-out)
                log.warning("selftest progress callback failed", exc_info=True)

    def _answer(self, q: str, detail: str, ids: Sequence[str] = ()) -> None:
        self.answers.append((q, detail, list(ids)))

    # ---- reads -----------------------------------------------------------------------------------

    def _rows(self, ids: Sequence[str]) -> dict[str, Item]:
        if not ids:
            return {}
        frame = self.runner.run_template("ROWS", ["1", *ids])
        return {it.id: it for it in applescript.items_from_frame(frame, notes_mode="full")}

    def _one(self, id: str) -> Item:
        it = self._rows([id]).get(id)
        if it is None:
            raise NotFound(f"{id}, created by this run, can't be read any more.")
        return it

    def _membership(self, keys: Sequence[str]) -> dict[str, set[str]]:
        frame = self.runner.run_template("MEMBERSHIP", list(keys))
        return {k: set(v) for k, v in applescript.membership_from_frame(frame).items()}

    def _probe(self, args: Sequence[str]) -> dict[str, tuple[bool, list[str]]]:
        rows = applescript.probe_results(self.runner.run_template("PROBE", list(args)))
        return {feature: (ok, detail) for feature, ok, detail in rows}

    def _find(self, kind: str, title: str, scope: tuple[str, str]) -> list[str]:
        """``FIND_CREATED`` ids (youngest first) for items made since the run started, polled until one shows."""
        deadline = self.runner.monotonic() + self.timeout_s
        while True:
            max_age = int(self.runner.wall() - self.t_start) + AGE_SLACK_S
            frame = self.runner.run_template("FIND_CREATED", [kind, title, str(max_age), scope[0], scope[1], "0"])
            found = sorted(applescript.created_from_frame(frame), key=lambda pair: pair[1])
            if found or self.runner.monotonic() >= deadline:
                return list(dict.fromkeys(i for i, _ in found))
            self.runner.sleep(POLL_S)

    def _claim(self, role: str, kind: str, title: str, scope: tuple[str, str], *, in_notes: bool = False) -> list[str]:
        """Invariant 2: the only way an id enters ``created``. The item must come from ``FIND_CREATED`` (made since
        the run started), have exactly ``title``, and carry the run token in its title (``in_notes``: its notes)."""
        found = self._find(kind, title, scope)
        items = self._rows(found)
        mine = []
        for i in found:
            it = items.get(i)
            if it is None or it.kind != kind or it.title != title:
                continue
            if self.run in (_norm(it.notes) if in_notes else it.title):
                mine.append(i)
        for i in mine:
            if i not in self.created:
                self.created.add(i)
                self.order.append(i)
        if mine:
            self.ids[role] = mine[0]
            self.pending.pop(title, None)
        return mine

    def _wait(self, id: str, done: Callable[[Item], bool]) -> Item:
        """Poll ``ROWS([id])`` until ``done`` holds or the read-back timeout passes; the last read."""
        deadline = self.runner.monotonic() + self.timeout_s
        self.runner.sleep(0.3)
        while True:
            it = self._one(id)
            if done(it) or self.runner.monotonic() >= deadline:
                return it
            self.runner.sleep(POLL_S)

    # ---- writes ----------------------------------------------------------------------------------

    @property
    def svc(self) -> service.ThingsService:
        if self._svc is None:
            raise AssertionError("ST-0 builds the service")
        return self._svc

    def _sent(self, c: _Checks, res: Any, what: str, titles: Sequence[str] = ()) -> None:
        if res.sent:
            for t in titles:
                self.pending.setdefault(t, None)
        c.expect(f"the {what} URL was sent", res.sent)
        if res.verified is False:
            c.expect(f"the service verified the {what}", False, _one_line(res.reason or "not verified"))

    def _send(self, turl: urlscheme.ThingsURL) -> Dispatch:
        """ST-12's conflict probes: forms the service deliberately doesn't build, still through ``OwnedRunner``."""
        with self.runner.write_lock():
            return self.runner.dispatch(turl)

    # ---- steps -----------------------------------------------------------------------------------

    def st0(self) -> _Result:
        check = doctor.check_platform(self.runner)
        if check.status != "ok":
            return _check_failed(check)
        check, version = doctor.check_installed(self.runner)
        if check.status != "ok":
            return _check_failed(check)
        self.launched = self.runner.ensure_running(allow_launch=self.config.allow_launch)
        if self.launched:
            self.timeout_s = max(self.timeout_s, LAUNCHED_TIMEOUT_S)
        check = doctor.check_automation(self.runner)
        if check.status != "ok":
            return _check_failed(check, "Automation: ")
        token = doctor.check_token()
        if token.status == "fail":
            return _check_failed(token, "token: ")
        self.has_token = token.status == "ok"
        # its batches are a handful of its own items: the batch and confirm limits guard bulk changes, not these
        cfg = replace(self.config, verify=True, verify_timeout_s=self.timeout_s,
                      max_batch=max(self.config.max_batch, MIN_BATCH),
                      confirm_threshold=max(self.config.confirm_threshold, MIN_BATCH))
        # created_here: the repeating guard can't tell this run's Anytime project (or a Someday to-do inside it)
        # from a repeating template on Things 3.24.1, but nothing the URL scheme creates can repeat (OFF-URL).
        self._svc = service.ThingsService(self.runner, cfg, today=self.today_fn, created_here=self.created)
        parts = [f"Things {version} " + ("launched hidden by this run" if self.launched else "running"),
                 "Automation granted",
                 f"token {token.detail}" if self.has_token else
                 f"no usable token ({token.detail}); the steps that change items, and the close-out, are skipped"]
        if not self.config.verify:
            parts.append("--no-verify doesn't apply: the selftest always reads back")
        return "pass", "; ".join(parts) + f"; run token {self.run}", []

    def st1(self) -> _Result:
        stamp = datetime.fromtimestamp(self.runner.wall()).astimezone().isoformat(timespec="seconds")
        notes = (f"{NOTES_PREFIX} {self.run} at {stamp}. The tool never deletes; delete this project by hand if "
                 "you like.")
        res = self.svc.add_project(ProjectSpec(title=SELFTEST_PROJECT_TITLE, notes=notes))
        c = _Checks()
        self._sent(c, res, "add-project", [SELFTEST_PROJECT_TITLE])
        ids = self._claim("P", "project", SELFTEST_PROJECT_TITLE, ("app", ""), in_notes=True)
        if not c.expect("exactly one new selftest project", len(ids) == 1, f"{len(ids)} found"):
            return c.result("the new project wasn't identified by read-back", ids)
        p = self._one(ids[0])
        c.expect("it reads back as a project", p.kind == "project", p.kind)
        c.expect("its notes start with the selftest note", _norm(p.notes).startswith(NOTES_PREFIX), _show(p.notes))
        if not c.failures:
            self._answer("Q21", "Things was launched hidden by this run, and the first URL after the launch applied"
                         if self.launched else "not exercised: Things was already running", ids)
        return c.result(f"created project {ids[0]}: Things URLs are enabled", ids)

    def st2(self) -> _Result:
        pid = self.ids["P"]
        title, notes = f"{self.run} alpha", "line1\nline2 ✓"
        due = (self.today + timedelta(days=7)).isoformat()
        res = self.svc.add_todo(TodoSpec(title=title, notes=notes, when="today", deadline=due,
                                             checklist=["a", "b"], project=pid))
        c = _Checks()
        self._sent(c, res, "add", [title])
        ids = self._claim("A", "to-do", title, ("project", pid))
        if not c.expect("exactly one new to-do in the project", len(ids) == 1, f"{len(ids)} found"):
            return c.result("alpha wasn't identified by read-back", ids)
        a = self._one(ids[0])
        c.expect("title", a.title == title, _show(a.title))
        c.expect("notes", _norm(a.notes) == notes, _show(a.notes))
        c.expect("project", _ref(a.project) == pid, _ref(a.project))
        c.expect("deadline", a.deadline == due, a.deadline)
        c.expect("start date today", a.start_date == self.today.isoformat(), a.start_date)
        c.expect("status open", a.status == "open", a.status)
        if not c.failures:
            self._answer("Q5", "yes: ROWS read every field of the selftest's project (ST-1) and to-do (ST-2)", ids)
        return c.result(f"alpha {ids[0]}: title, notes, project, deadline and start date read back as sent; the "
                        "checklist can't be read back (unverifiable)", ids)

    def st3(self) -> _Result:
        pid, run = self.ids["P"], self.run
        beta, gamma, g1, g2 = f"{run} beta", f"{run} gamma-proj", f"{run} g1", f"{run} g2"
        checklist = [{"type": "checklist-item", "attributes": {"title": t}} for t in ("c1", "c2")]
        data = [
            {"type": "to-do", "attributes": {"title": beta, "list-id": pid, "when": "someday",
                                             "checklist-items": checklist}},
            {"type": "project", "attributes": {"title": gamma, "items": [
                {"type": "to-do", "attributes": {"title": g1}},
                {"type": "heading", "attributes": {"title": f"{run} H1", "archived": True}},
                {"type": "to-do", "attributes": {"title": g2, "completed": True}}]}},
        ]
        res = self.svc.raw_json(data)
        c = _Checks()
        self._sent(c, res, "json", [beta, gamma, g1, g2])
        b = self._claim("beta", "to-do", beta, ("project", pid))
        g = self._claim("G", "project", gamma, ("app", ""))
        kids: list[str] = []
        if len(g) == 1:
            kids = self._claim("g1", "to-do", g1, ("project", g[0])) + self._claim("g2", "to-do", g2, ("project", g[0]))
        ids = b + g + kids
        c.expect("beta found in the project", len(b) == 1, f"{len(b)} found")
        c.expect("gamma-proj found", len(g) == 1, f"{len(g)} found")
        c.expect("g1 and g2 found in gamma-proj", len(kids) == 2, f"{len(kids)} found")
        if c.failures:
            return c.result("the json create wasn't identified by read-back", ids)
        bid, gid, g2id = b[0], g[0], self.ids["g2"]
        rows = self._rows([bid, g2id])
        c.expect("beta is in the project", _ref(rows[bid].project) == pid, _ref(rows[bid].project))
        # Things 3.24.1 leaves a project's Someday to-dos out of Someday's list: no start date and in neither
        # Anytime nor the Inbox is what places beta in Someday.
        m = self._membership(["inbox", "anytime"])
        where = [k for k in ("inbox", "anytime") if bid in m.get(k, ())]
        c.expect("beta is in Someday (no start date; not in Inbox or Anytime)",
                 rows[bid].start_date is None and not where,
                 f"start {rows[bid].start_date}, in {', '.join(where) or 'no list'}")
        names = {r.title for r in applescript.scan_rows(self.runner.run_template("SCAN", ["project", gid, "0", "0"]))}
        c.expect("gamma-proj lists g1 and g2", {g1, g2} <= names, f"{len(names & {g1, g2})} of 2")
        if c.failures:
            return c.result("the json create read back differently", ids)
        under = _ref(rows[g2id].project) == gid
        self._answer("Q22", "yes: a to-do under a heading reports its project" if under else
                     f"no: g2 (under a heading) reports project {_ref(rows[g2id].project)}; reads attribute it by "
                     "the scanned container instead", [g2id])
        probe = self._probe([gid])
        ok, detail = probe.get("todo-id-resolves-project", (None, []))
        self._answer("Q23", "not reported by PROBE" if ok is None else
                     f"yes: `to do id` resolves a project (class {detail[0] if detail else '?'})" if ok else
                     "no: `to do id` doesn't resolve projects; ROWS falls back to `project id`", [gid])
        ok, detail = probe.get("list-projects", (None, []))
        self._answer("Q3", "not reported by PROBE" if ok is None else
                     "yes: `projects of list` works" if ok else
                     f"no: `projects of list` failed ({_one_line(' '.join(detail), 120)}); MEMBERSHIP lists to-dos "
                     "only")
        self._answer("Q26", "yes: a json create with checklist-item objects was accepted (the checklist itself "
                     "can't be read back)", [bid])
        return c.result("beta landed in the project (Someday); new project gamma-proj holds g1, heading H1 (archived) "
                        "and g2 (completed); headings and checklists are unverifiable", ids)

    def st4(self) -> _Result:
        aid = self.ids["A"]
        res = self.svc.update_todo(aid, TodoPatch(when=self.tomorrow, reminder_time="9:30am"))
        c = _Checks()
        self._sent(c, res, "update")
        a = self._one(aid)
        c.expect("start date tomorrow", a.start_date == self.tomorrow, a.start_date)
        if not c.failures:
            self._answer("Q8", f"yes: when={self.tomorrow}@9:30am (literal @ and :) set the start date; the reminder "
                         "time itself can't be read back", [aid])
        return c.result(f"update when={self.tomorrow}@9:30am applied, so the auth token works", [aid])

    def st5(self) -> _Result:
        aid = self.ids["A"]
        res = self.svc.update_todo(aid, TodoPatch(when="evening"))
        c = _Checks()
        self._sent(c, res, "update")
        a = self._one(aid)
        c.expect("start date today", a.start_date == self.today.isoformat(), a.start_date)
        if not c.failures:
            self._answer("Q25", "yes: when=evening sets the start date to today (This Evening itself can't be read)",
                         [aid])
        return c.result("update when=evening applied", [aid])

    def st6(self) -> _Result:
        aid = self.ids["A"]
        res = self.svc.update_todo(aid, TodoPatch(when="anytime"))
        c = _Checks()
        self._sent(c, res, "json update")
        a = self._one(aid)
        c.expect("no start date", a.start_date is None, a.start_date)
        c.expect("in Anytime", aid in self._membership(["anytime"]).get("anytime", ()), "not in anytime")
        if not c.failures:
            self._answer("Q10", "yes: a json update with when=anytime moved a scheduled to-do to Anytime", [aid])
        return c.result("json update when=anytime applied", [aid])

    def st7(self) -> _Result:
        aid = self.ids["A"]
        title = f"{self.run} alpha (renamed)"
        res = self.svc.update_todo(aid, TodoPatch(title=title, prepend_notes="P0 ", append_notes=" Z9"))
        c = _Checks()
        self._sent(c, res, "update")
        a = self._one(aid)
        c.expect("title", a.title == title, _show(a.title))
        c.expect("notes start with 'P0 '", _norm(a.notes).startswith("P0 "), _show(a.notes))
        c.expect("notes end with ' Z9'", _norm(a.notes).endswith(" Z9"), _show(a.notes))
        if c.failures:
            return c.result("update title, prepend-notes and append-notes", [aid])
        res = self.svc.update_todo(aid, TodoPatch(clear_deadline=True))
        self._sent(c, res, "update deadline=")
        a = self._one(aid)
        c.expect("deadline cleared", a.deadline is None, a.deadline)
        return c.result("title, prepend-notes and append-notes applied; deadline= cleared the deadline", [aid])

    def st8(self) -> _Result:
        pid = self.ids["P"]
        title = f"-{self.run}{UNICODE_TAIL}"
        res = self.svc.add_todo(TodoSpec(title=title, project=pid))
        c = _Checks()
        self._sent(c, res, "add", [title])
        ids = self._claim("U", "to-do", title, ("project", pid))
        if not c.expect("FIND_CREATED matched the exact title (leading '-', non-ASCII, '=' argv prefix)",
                        len(ids) == 1, f"{len(ids)} found"):
            return c.result("the unicode item wasn't identified by read-back", ids)
        u = self._one(ids[0])
        c.expect("the title round-trips exactly", u.title == title, _show(u.title))
        query = f"{self.run.upper()} ÜNÏ"
        hits = {it.id for it in self.svc.search(query, limit=service.MAX_LIMIT, notes="none").items}
        c.expect(f"search {query!r} (other case, no diacritics) finds it", ids[0] in hits, "not found")
        if not c.failures:
            self._answer("Q7", "yes: a leading '-', non-ASCII and an emoji survive `osascript -` argv with the '=' "
                         "prefix", ids)
        return c.result(f"unicode item {ids[0]} round-trips through the URL and the reads", ids)

    def st9(self) -> _Result:
        svc, pid = self.svc, self.ids["P"]
        own = [self.ids[k] for k in ("A", "beta", "U") if k in self.ids]
        c = _Checks()
        a = self._one(self.ids["A"])
        today = {it.id for it in svc.list_items("today", status="all", limit=service.MAX_LIMIT, notes="none").items}
        scheduled = a.start_date == self.today.isoformat()
        in_today = a.id in today
        seen = f"alpha is {'' if in_today else 'not '}in Today (start date {a.start_date or 'none'})"
        if in_today != scheduled:
            seen += ", which is unexpected"
        found = {it.id for it in svc.find(project=pid, status="all", limit=service.MAX_LIMIT, notes="none").items}
        c.expect("find(project) returns alpha, beta and the unicode item", set(own) <= found,
                 f"{len(set(own) & found)} of {len(own)}")
        got = svc.get(pid, children=True)
        kids = {it.id for it in got.children.items} if got.children else set()
        c.expect("get(project, children) reads the project", got.kind == "project", got.kind)
        c.expect("… and its to-dos", set(own) <= kids, f"{len(set(own) & kids)} of {len(own)}")
        logged = [it.id for it in svc.logbook(since=self.today, notes="none").items if it.id in self.created]
        probe = self._probe([])
        lists = [f[5:] for f, (ok, _) in probe.items() if f.startswith("list:") and ok]
        broken = [f[5:] for f, (ok, _) in probe.items() if f.startswith("list:") and not ok]
        self._answer("Q1", ("built-in list ids resolve from AppleScript: " + ", ".join(lists) if lists else
                            "no built-in list id resolved") + (f"; not: {', '.join(broken)}" if broken else ""))
        ok, detail = probe.get("app-todos-include-projects", (None, []))
        if ok is None:
            self._answer("Q4", "not reported by PROBE")
        elif not ok:
            self._answer("Q4", "the PROBE row failed: " + _one_line(" ".join(detail), 120))
        else:
            n = detail[1] if len(detail) > 1 else "?"
            self._answer("Q4", f"app-level `to dos` {'includes' if n not in ('0', '?') else 'does not include'} "
                         f"projects ({n} of {detail[0] if detail else '?'}); reads dedupe either way")
        return c.result(f"{seen}; find, get and logbook(since today) ran ({len(logged)} of this run's items in the "
                        "Logbook)", own)

    def st10(self) -> _Result:
        pid = self.ids["P"]
        c = _Checks()
        res = self.svc.update_project(pid, ProjectPatch(when=self.tomorrow))
        self._sent(c, res, "update-project")
        p = self._one(pid)
        c.expect("project start date tomorrow", p.start_date == self.tomorrow, p.start_date)
        if c.failures:
            return c.result(f"update-project when={self.tomorrow}", [pid])
        # Q14 is about to-dos with no start date of their own. beta is a Someday to-do, and Things 3.24.1 shows a
        # project's Someday to-dos in no list whatever the project's schedule, so it can't answer Q14.
        kids = [self.ids[k] for k in ("A", "U") if k in self.ids]
        rows = self._rows(kids)
        open_kids = [i for i in kids if i in rows and rows[i].status == "open"]
        members = self._membership(LIST_KEYS)
        where = {i: [k for k in LIST_KEYS if i in members.get(k, ())] for i in open_kids}
        hidden = [i for i, keys in where.items() if not keys]
        shown = "; ".join(f"{i}: {', '.join(keys) or 'no list'}" for i, keys in where.items())
        self._answer("Q14", (f"no: while its project is scheduled for tomorrow, {len(hidden)} open to-do(s) show in "
                             "no list, so the repeating guard would refuse to change them (a false refusal)"
                             if hidden else "yes: the open to-dos of a future-scheduled project still show in a list")
                     + (f" ({shown})" if shown else ""), open_kids)
        res = self.svc.update_project(pid, ProjectPatch(when="anytime"))
        self._sent(c, res, "json update (when anytime)")
        p = self._one(pid)
        c.expect("project start date cleared (Anytime)", p.start_date is None, p.start_date)
        return c.result(f"update-project when={self.tomorrow} applied; a json update put the project back in Anytime",
                        [pid])

    def st11(self) -> _Result:
        aid, bid = self.ids["A"], self.ids["beta"]
        data = [{"type": "to-do", "operation": "update", "id": aid, "attributes": {"completed": True}},
                {"type": "to-do", "operation": "update", "id": bid, "attributes": {"canceled": True}}]
        res = self.svc.raw_json(data)
        c = _Checks()
        self._sent(c, res, "json")
        rows = self._rows([aid, bid])
        c.expect("alpha completed", aid in rows and rows[aid].status == "completed", rows.get(aid) and rows[aid].status)
        c.expect("beta canceled", bid in rows and rows[bid].status == "canceled", rows.get(bid) and rows[bid].status)
        if c.failures:
            return c.result("json status updates", [aid, bid])
        logged = {it.id for it in self.svc.logbook(limit=20, notes="none").items}
        self._answer("Q24", "completed and canceled to-dos reach the Logbook right away" if {aid, bid} <= logged else
                     "they read as completed/canceled but aren't in the Logbook yet (logging set to daily or "
                     "manual?)", [aid, bid])
        return c.result("one json URL completed alpha and canceled beta", [aid, bid])

    def st12(self) -> _Result:
        uid, gid, g1 = self.ids.get("U"), self.ids.get("G"), self.ids.get("g1")
        c = _Checks()
        ids = [i for i in (uid, gid, g1) if i]
        # (a) Q9: plain update?when=anytime. The builders refuse it (D10), so the probe builds it by hand.
        if uid:
            res = self.svc.update_todo(uid, TodoPatch(when=self.tomorrow))
            self._sent(c, res, "update (Q9 set-up)")
            if c.failures:
                return c.result("conflict probes", ids)
            self._send(urlscheme.ThingsURL("update", (("id", uid), ("when", "anytime")), 1, True))
            u = self._wait(uid, lambda it: it.start_date is None)
            moved = u.start_date is None
            self._answer("Q9", "yes: plain update?when=anytime moved a scheduled to-do to Anytime" if moved else
                         f"no: plain update?when=anytime was ignored (start date still {u.start_date}); "
                         "things-bridge keeps using json for Anytime", [uid])
        # (b) Q11: json add-tags as an array, with one existing tag, on the selftest's own item
        tag = self._usable_tag()
        if uid and tag is not None:
            self._send(urlscheme.build_json([jsoncmd.Todo(operation="update", id=uid, add_tags=[tag])], strict=False))
            u = self._wait(uid, lambda it: tag.casefold() in {t.casefold() for t in it.tags})
            applied = tag.casefold() in {t.casefold() for t in u.tags}
            self._answer("Q11", "json add-tags as an array: applied" if applied else
                         "json add-tags as an array: ignored (things-bridge keeps using the update URL's comma string)",
                         [uid])
        elif uid:
            self._answer("Q11", "not answered: no usable tag exists in Things")
        # (c) Q12: update-project completed=true while one of its to-dos is open
        if gid and g1:
            self._send(urlscheme.build_update_project(id=gid, completed=True))
            self.runner.sleep(CONFLICT_WAIT_S)
            rows = self._rows([gid, g1])
            g_status = rows[gid].status if gid in rows else "unreadable"
            k_status = rows[g1].status if g1 in rows else "unreadable"
            if g_status == "open" and k_status == "open":
                answer = "ignored while a to-do is open (the official reading)"
            elif g_status != "open" and k_status != "open":
                answer = "completed the project and its open to-do (the cascade reading, TAPI §3)"
            else:
                answer = f"project {g_status}, its open to-do {k_status}"
            self._answer("Q12", f"update-project completed=true on a project with an open to-do: {answer}", [gid, g1])
        return c.result("conflict probes ran on the selftest's own items only", ids, status="info")

    def _usable_tag(self) -> str | None:
        for t in self.svc.tags().items:
            try:
                return urlscheme.check_tag("tag", t.title)
            except InvalidInput:
                continue
        return None

    # ---- ST-13: close-out ------------------------------------------------------------------------

    def close_out(self) -> _Result:
        """Complete (or, for the unicode item, cancel) every open created to-do, then the created projects. Only
        ``created`` ids; the service guards run first. Runs after failures too."""
        if not self.order:
            return "skip", "nothing was created, so there is nothing to close", []
        self.left_open = list(self.order)           # until a read says otherwise
        if not self.has_token:
            items = self._rows(self.order)
            self.left_open = [i for i in self.order if i in items and items[i].status == "open"]
            return "skip", f"{NO_TOKEN}: closing items needs the auth token", self.left_open
        items = self._rows(self.order)
        problems: list[tuple[str | None, str]] = []
        # A project left scheduled in the future (ST-10 cut short) goes back to Anytime first, so the run leaves it in a
        # normal state. The repeating guard skips the run's own ids (invariant 6), so this isn't needed to get past it.
        for i in self.order:
            it = items.get(i)
            if it and it.kind == "project" and it.status == "open" and it.start_date \
                    and it.start_date > self.today.isoformat():
                self._attempt(problems, i, "put back in Anytime",
                              lambda i=i: self.svc.update_project(i, ProjectPatch(when="anytime")))
        todos = [i for i in self.order if i in items and items[i].kind == "to-do" and items[i].status == "open"]
        if todos:
            self._close_todos(todos, problems)
        projects = [i for i in reversed(self.order)
                    if i in items and items[i].kind == "project" and items[i].status == "open"]
        for pid in projects:                        # gamma-proj first, then the selftest project
            self._attempt(problems, pid, "complete", lambda pid=pid: self.svc.set_status([pid], "completed"))
        final = self._rows(self.order)
        self.left_open = [i for i in self.order if i in final and final[i].status == "open"]
        closed = len(self.order) - len(self.left_open)
        if not self.left_open:
            note = f" (on the way: {'; '.join(m for _, m in problems)})" if problems else ""
            return "pass", f"completed or canceled all {closed} items this run created; Things files them in the " \
                           f"Logbook{note}", list(self.order)
        gid = self.ids.get("G")
        if self.left_open == [gid] and all(i == gid for i, _ in problems):
            return "info", (f"left open: gamma-proj {gid}; Things didn't archive its heading, so the project can't be "
                            "completed. Complete or delete it by hand."), [gid]
        why = "; ".join(m for _, m in problems) or "they didn't close"
        return "fail", f"closed {closed} of {len(self.order)}; left open: {', '.join(self.left_open)} ({why})", \
            list(self.left_open)

    def _attempt(self, problems: list, id: str | None, what: str, call: Callable[[], Any]) -> Any:
        label = f"{what} {id}" if id else what
        try:
            res = call()
        except errors.ThingsBridgeError as e:
            problems.append((id, f"{label}: [{e.code}] {_one_line(e.message, 200)}"))
            return None
        if res.verified is False:
            problems.append((id, f"{label}: not verified ({_one_line(res.reason, 200)})"))
        return res

    def _close_todos(self, todos: list[str], problems: list) -> None:
        uid = self.ids.get("U")

        def ops(ids: list[str]) -> list[dict]:
            return [{"type": "to-do", "operation": "update", "id": i,
                     "attributes": {"canceled" if i == uid else "completed": True}} for i in ids]
        try:
            res = self.svc.raw_json(ops(todos), confirm=True)
        except Refused as e:
            hidden = [i for i in e.details.get("ids", ()) if i in todos]
            if not hidden:
                problems.append((None, f"close to-dos: [{e.code}] {_one_line(e.message, 200)}"))
                return
            # Defensive only: the repeating guard skips the run's own ids (invariant 6). If a guard ever refuses some
            # ids before dispatch anyway, nothing was sent, so close the rest.
            problems.append((None, f"the repeating guard kept {', '.join(hidden)} open"))
            rest = [i for i in todos if i not in hidden]
            if rest:
                self._attempt(problems, None, "close to-dos", lambda: self.svc.raw_json(ops(rest), confirm=True))
            return
        except errors.ThingsBridgeError as e:
            problems.append((None, f"close to-dos: [{e.code}] {_one_line(e.message, 200)}"))
            return
        if res.verified is False:
            problems.append((None, f"close to-dos: not verified ({_one_line(res.reason, 200)})"))

    # ---- ST-14: report ---------------------------------------------------------------------------

    def report(self) -> SelftestReport:
        st = [s for s in self.steps if s.name.startswith("ST-")]
        counts = {k: sum(s.status == k for s in st) for k in ("pass", "fail", "skip", "info")}
        answered = [s.name for s in self.steps if s.name.startswith("Q")]
        detail = (f"{counts['pass']} passed, {counts['fail']} failed, {counts['skip']} skipped, {counts['info']} info; "
                  f"{len(self.order)} item(s) created"
                  + (f"; answered {', '.join(answered)}" if answered else ""))
        self._record("ST-14", "info", detail, self.order)
        return SelftestReport(ok=not any(s.status == "fail" for s in self.steps), project_id=self.ids.get("P"),
                              created_ids=list(self.order), steps=list(self.steps),
                              cleanup_note=redact(self._cleanup_note()))

    def _cleanup_note(self) -> str:
        pid = self.ids.get("P")
        if not self.order and not self.pending:
            return "Nothing was created, and nothing was deleted."
        parts = ["Nothing was deleted."]
        if pid:
            where = "still open" if pid in self.left_open else "now in the Logbook"
            parts.append(f"The selftest project is at {link_for(pid)} ({where}). Delete it by hand in Things if you "
                         "like; things-bridge never deletes.")
        others = [i for i in self.left_open if i != pid]
        if others:
            parts.append(f"Still open (complete or delete them by hand): {', '.join(others)}.")
        if self.pending:
            titles = ", ".join(repr(t) for t in self.pending)
            parts.append(f"Sent but never identified, so left alone: {titles}. If Things shows an 'Enable Things "
                         f"URLs?' prompt, held commands apply when you click Enable. Search Things for '{self.run}' "
                         "to find them.")
        return " ".join(parts)
