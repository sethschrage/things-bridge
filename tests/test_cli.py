"""cli.py (DESIGN §2.12, §3, §3.4, §5.1, §5.3, §5.5): the parser for every command, --json shapes, exit codes,
stdin conventions, human output, setup/token/selftest/mcp flows, and that no token flag exists anywhere.

Everything runs in-process against tests/fakes.py (``main(argv, runner=FakeRunner, today=clock.today)``). The
real Keychain is never touched: ``authtoken._security`` fails the test if it is ever reached.
"""

import argparse
import dataclasses
import io
import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from fakes import FAKE_TOKEN, FakeRunner
from things_bridge import __version__, authtoken, cli
from things_bridge import runner as runner_mod
from things_bridge.errors import DispatchFailed, InvalidInput, NotMacOS, ProtocolError
from things_bridge.models import (AreaList, DoctorReport, GetResult, Item, NavResult, Page, SelftestReport,
                                  SelftestStep, TagList, WriteResult)

ID = "SyJEz273ceSkabUbciM73A"
ID2 = "Jvj7EW1fLoScPhaw2JomCT"
GHOST = "GhostGhostGhostGhost01"
SECRET = "SeCrEtToKeN0123456789x"


def keys(cls) -> set[str]:
    return {f.name for f in dataclasses.fields(cls)}


# ---- fixtures -----------------------------------------------------------------------------------------------

class FakeStdin(io.StringIO):
    def __init__(self, text: str = "", *, tty: bool = False):
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@dataclass
class Result:
    code: int
    out: str
    err: str

    def json(self):
        return json.loads(self.out)


@pytest.fixture(autouse=True)
def isolation(monkeypatch):
    """No real Keychain, no leftover secrets, and the package logger restored after each test."""
    def no_security(*a, **k):
        raise AssertionError("tests must never run /usr/bin/security")

    def no_keychain():
        raise NotMacOS("things-bridge keeps the auth token in the macOS Keychain; this system is linux")

    monkeypatch.setattr(authtoken, "_security", no_security)
    monkeypatch.setattr(authtoken, "_keychain_token", no_keychain)
    monkeypatch.setattr(runner_mod, "_secrets", ())
    logger = logging.getLogger("things_bridge")
    level, handlers = logger.level, list(logger.handlers)
    yield
    logger.setLevel(level)
    logger.handlers[:] = handlers


@pytest.fixture
def keychain(monkeypatch):
    """A login Keychain: ``store["token"]`` is the item (None: no item); ``set_token`` validates and stores."""
    store = {"token": None, "sets": []}
    monkeypatch.setattr(authtoken, "_keychain_token", lambda: store["token"])

    def set_token(token: str) -> None:
        token = token.strip()
        if not authtoken.is_valid_token(token):
            raise InvalidInput("That doesn't look like a Things URL auth token.", hint="copy it again",
                               details={"field": "token"})
        store["token"] = token
        store["sets"].append(token)

    monkeypatch.setattr(authtoken, "set_token", set_token)
    return store


@pytest.fixture
def run(fake_runner, clock, monkeypatch, capsys):
    def go(*argv, stdin: str = "", tty: bool = False, runner=None) -> Result:
        monkeypatch.setattr(sys, "stdin", FakeStdin(stdin, tty=tty))
        code = cli.main([str(a) for a in argv], runner=fake_runner if runner is None else runner, today=clock.today)
        out, err = capsys.readouterr()
        return Result(code, out, err)
    return go


@pytest.fixture
def w(fake_things):
    """A small Things library."""
    t = fake_things
    ns = SimpleNamespace()
    ns.home = t.seed_tag("Home")
    ns.errand = t.seed_tag("Errand", parent="Home")
    ns.deep = t.seed_tag("Deep Work", shortcut="d")
    ns.family = t.seed_area("Family", tags=["Home"])
    ns.work = t.seed_area("Work")
    ns.party = t.seed_project("Plan Birthday Party", area=ns.family, deadline="2026-10-20")
    ns.venue = t.seed_heading(ns.party, "Venue")
    ns.taxes = t.seed_project("Taxes", area=ns.work)
    ns.milk = t.seed_todo("Buy milk", when="today", tags=["Errand"], deadline="2026-10-02",
                          notes="2%\nlactose free", project=ns.party)
    ns.passport = t.seed_todo("Renew passport", when="today", deadline="2026-09-01")
    ns.inbox = t.seed_todo("Inbox item")
    ns.someday = t.seed_todo("Learn watercolor", when="someday")
    ns.done = t.seed_todo("Call Paolo", project=ns.party, status="completed")
    ns.tmpl = t.seed_todo("Water plants", repeating=True)
    return ns


def sent_params(fake_runner, i: int = -1) -> dict[str, str]:
    return dict(fake_runner.sent[i].params)


def payload(fake_runner, i: int = -1) -> list[dict]:
    return json.loads(sent_params(fake_runner, i)["data"])


# ---- parser: every command --------------------------------------------------------------------------------------

COMMANDS = {"list", "inbox", "today", "anytime", "upcoming", "someday", "logbook", "get", "projects", "areas", "tags",
            "search", "find", "deadlines", "doctor", "add", "add-many", "add-project", "update", "update-project",
            "complete", "cancel", "reopen", "schedule", "move", "tag", "json", "show", "open-search", "token",
            "setup", "selftest", "version", "mcp"}

PARSE_CASES = [
    (["list", "today"], {"func": cli._cmd_list, "list_key": "today", "status": "open", "limit": 50, "offset": 0,
                         "notes": "preview"}),
    (["inbox"], {"func": cli._cmd_list, "list_key": "inbox"}),
    (["today"], {"list_key": "today"}),
    (["anytime"], {"list_key": "anytime"}),
    (["upcoming", "--status", "all", "--limit", "5", "--offset", "10", "--notes", "none"],
     {"list_key": "upcoming", "status": "all", "limit": 5, "offset": 10, "notes": "none"}),
    (["someday"], {"list_key": "someday"}),
    (["logbook"], {"func": cli._cmd_logbook, "since": "7d", "limit": 50, "notes": "preview"}),
    (["logbook", "--since", "2w", "--limit", "5", "--notes", "full"], {"since": "2w", "limit": 5, "notes": "full"}),
    (["get", ID], {"func": cli._cmd_get, "id": ID, "children": False, "notes": "full"}),
    (["get", ID, "--children", "--notes", "none"], {"children": True, "notes": "none"}),
    (["projects"], {"func": cli._cmd_projects, "area": None, "status": "open", "limit": 100, "offset": 0,
                    "notes": "none"}),
    (["projects", "--area", "Work", "--status", "all"], {"area": "Work", "status": "all"}),
    (["areas"], {"func": cli._cmd_areas}),
    (["tags"], {"func": cli._cmd_tags}),
    (["search", "buy", "milk"], {"func": cli._cmd_search, "query": ["buy", "milk"], "include_completed": False,
                                 "logbook_window": 100, "limit": 50, "notes": "preview"}),
    (["search", "x", "--include-completed", "--logbook-window", "20"],
     {"include_completed": True, "logbook_window": 20}),
    (["find", "--tag", "a, b", "--tags", "c"], {"func": cli._cmd_find, "tags": ["a", "b", "c"], "project": None,
                                               "area": None, "status": "open"}),
    (["find", "--project", "P", "--offset", "3"], {"project": "P", "offset": 3}),
    (["deadlines"], {"func": cli._cmd_deadlines, "within_days": 14, "no_overdue": False, "no_projects": False,
                     "limit": 50}),
    (["deadlines", "--within-days", "all", "--no-overdue", "--no-projects"],
     {"within_days": None, "no_overdue": True, "no_projects": True}),
    (["deadlines", "--within-days", "30"], {"within_days": 30}),
    (["doctor"], {"func": cli._cmd_doctor, "prompt_automation": False, "probe": False, "check_repeating": None}),
    (["doctor", "--prompt-automation", "--probe", "--check-repeating", ID],
     {"prompt_automation": True, "probe": True, "check_repeating": ID}),
    (["add", "Buy", "milk"], {"func": cli._cmd_add, "title": ["Buy", "milk"], "notes": None, "when": None,
                              "reminder": None, "deadline": None, "tags": None, "checklist": None, "project": None,
                              "area": None, "heading": None, "reveal": False, "strict_tags": False,
                              "dry_run": False}),
    (["add", "x", "--notes", "-", "--when", "today", "--reminder-time", "9am", "--deadline", "tomorrow", "--tags",
      "A, B", "--tag", "C", "--checklist", "one, two", "--checklist", "three", "--project", "P", "--heading", "H",
      "--reveal", "--strict-tags", "--dry-run"],
     {"notes": "-", "when": "today", "reminder": "9am", "deadline": "tomorrow", "tags": ["A", "B", "C"],
      "checklist": ["one, two", "three"], "project": "P", "heading": "H", "reveal": True, "strict_tags": True,
      "dry_run": True}),
    (["add", "x", "--area", "Work", "--reminder", "18:00"], {"area": "Work", "reminder": "18:00"}),
    (["add-many", "-"], {"func": cli._cmd_add_many, "file": "-", "confirm": False, "strict_tags": False,
                         "dry_run": False}),
    (["add-many", "todos.json", "--confirm", "--strict-tags"], {"file": "todos.json", "confirm": True,
                                                                "strict_tags": True}),
    (["add-project", "Trip", "--todo", "a", "--heading", "H", "--todo", "b", "--area", "Work"],
     {"func": cli._cmd_add_project, "title": ["Trip"], "items": [("todo", "a"), ("heading", "H"), ("todo", "b")],
      "items_file": None, "area": "Work", "reveal": False}),
    (["add-project", "Trip", "--items", "-", "--when", "someday"], {"items": None, "items_file": "-",
                                                                   "when": "someday"}),
    (["update", ID], {"func": cli._cmd_update, "id": ID, "title": None, "notes": None, "clear_notes": False,
                      "prepend_notes": None, "append_notes": None, "when": None, "reminder": None,
                      "deadline": None, "clear_deadline": False, "tags": None, "clear_tags": False,
                      "add_tags": None, "checklist_append": None, "checklist_prepend": None, "project": None,
                      "area": None, "heading": None, "strict_tags": False, "dry_run": False}),
    (["update", ID, "--title", "T", "--clear-notes", "--prepend-notes", "p", "--append-notes", "a",
      "--clear-deadline", "--clear-tags", "--add-tags", "x,y", "--checklist-append", "c", "--checklist-prepend",
      "d", "--area", "A"],
     {"title": "T", "clear_notes": True, "prepend_notes": "p", "append_notes": "a", "clear_deadline": True,
      "clear_tags": True, "add_tags": ["x", "y"], "checklist_append": ["c"], "checklist_prepend": ["d"],
      "area": "A"}),
    (["update-project", ID, "--notes", "n", "--tags", "t", "--area", "A", "--when", "anytime"],
     {"func": cli._cmd_update_project, "notes": "n", "tags": ["t"], "area": "A", "when": "anytime"}),
    (["complete", ID, ID2], {"func": cli._cmd_status, "status": "completed", "ids": [ID, ID2], "confirm": False,
                             "dry_run": False}),
    (["cancel", ID, "--confirm"], {"func": cli._cmd_status, "status": "canceled", "confirm": True}),
    (["reopen", "-"], {"func": cli._cmd_status, "status": "open", "ids": ["-"]}),
    (["schedule", ID, "--when", "tomorrow", "--reminder", "6pm", "--deadline", "2026-12-01", "--confirm"],
     {"func": cli._cmd_schedule, "when": "tomorrow", "reminder": "6pm", "deadline": "2026-12-01",
      "clear_deadline": False, "confirm": True}),
    (["move", ID, "--project", "P", "--heading", "H"], {"func": cli._cmd_move, "project": "P", "area": None,
                                                        "heading": "H"}),
    (["move", ID, ID2, "--area", "A"], {"area": "A", "project": None, "ids": [ID, ID2]}),
    (["tag", ID, "--add", "a,b"], {"func": cli._cmd_tag, "add": ["a", "b"], "replace": None, "clear": False}),
    (["tag", ID, "--replace", "c"], {"replace": ["c"], "add": None}),
    (["tag", ID, "--clear"], {"clear": True, "add": None, "replace": None}),
    (["json", "-", "--reveal", "--confirm", "--dry-run"], {"func": cli._cmd_json, "file": "-", "reveal": True,
                                                           "confirm": True, "dry_run": True}),
    (["show", ID], {"func": cli._cmd_show, "id": ID, "list_key": None, "query": None, "filter": None}),
    (["show", "--list", "logbook"], {"id": None, "list_key": "logbook"}),
    (["show", "--query", "Work", "--filter", "a,b"], {"query": "Work", "filter": ["a", "b"]}),
    (["open-search"], {"func": cli._cmd_open_search, "query": []}),
    (["open-search", "a", "b"], {"query": ["a", "b"]}),
    (["token", "set"], {"func": cli._cmd_token_set, "stdin": False}),
    (["token", "set", "--stdin"], {"stdin": True}),
    (["token", "status"], {"func": cli._cmd_token_status}),
    (["setup"], {"func": cli._cmd_setup}),
    (["selftest"], {"func": cli._cmd_selftest, "yes": False, "probe_conflicts": False}),
    (["selftest", "--yes", "--probe-conflicts"], {"yes": True, "probe_conflicts": True}),
    (["version"], {"func": cli._cmd_version}),
    (["mcp"], {"func": cli._cmd_mcp, "read_only": False}),
    (["mcp", "--read-only"], {"read_only": True}),
]


@pytest.mark.parametrize("argv, expected", PARSE_CASES, ids=[" ".join(c[0])[:40] for c in PARSE_CASES])
def test_parser_cases(argv, expected):
    args = cli.build_parser().parse_args(argv)
    assert (args.json, args.no_launch, args.no_verify, args.verbose) == (False, False, False, 0)
    for k, v in expected.items():
        assert getattr(args, k) == v, k


def all_parsers(p: argparse.ArgumentParser):
    yield p
    for a in p._actions:
        if isinstance(a, argparse._SubParsersAction):
            for sp in a.choices.values():
                yield from all_parsers(sp)


def test_every_command_is_registered_and_covered():
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert set(sub.choices) == COMMANDS
    assert {c[0][0] for c in PARSE_CASES} == COMMANDS


def test_no_token_or_auth_flag_anywhere():
    """N8: the token is never a CLI flag or argument (only the `token` command's name mentions it)."""
    for p in all_parsers(cli.build_parser()):
        for a in p._actions:
            if isinstance(a, argparse._SubParsersAction):
                continue
            for s in [*a.option_strings, a.dest]:
                assert not any(w in s.lower() for w in ("token", "auth", "password", "secret")), (p.prog, s)


@pytest.mark.parametrize("argv", [["add", "x", "--token", SECRET], ["update", ID, f"--auth-token={SECRET}"],
                                  ["token", "set", SECRET], ["--json", "token", "set", SECRET],
                                  ["token", SECRET]])
def test_token_like_arguments_are_refused_and_never_echoed(run, argv):
    r = run(*argv)
    assert r.code == 2
    assert SECRET not in r.out + r.err
    assert "***" in r.out + r.err


def test_global_options_before_or_after_the_command(run, w):
    a, b = run("--json", "today"), run("today", "--json")
    assert a.code == b.code == 0 and a.json() == b.json()
    args = cli.build_parser().parse_args(["--no-launch", "today", "--no-verify", "-v", "--json"])
    assert (args.no_launch, args.no_verify, args.verbose, args.json) == (True, True, 1, True)
    args = cli.build_parser().parse_args(["token", "status", "--json"])
    assert args.json is True


@pytest.mark.parametrize("argv", [
    ["bogus"], ["list", "trash"], ["today", "--limit", "x"], ["add"], ["move", ID],
    ["move", ID, "--project", "a", "--area", "b"], ["tag", ID], ["tag", ID, "--add", "a", "--clear"],
    ["update", ID, "--tags", "a", "--clear-tags"], ["update", ID, "--notes", "a", "--clear-notes"],
    ["schedule", ID, "--deadline", "2026-12-01", "--clear-deadline"], ["add", "x", "--tags", " , "],
    ["deadlines", "--within-days", "soon"], ["add", "x", "--dry"], ["token"], ["token", "delete"],
    ["show", "--list", "trash"], ["find", "--project", "a", "--area", "b"], ["complete"], ["get"],
    ["logbook", "--notes", "all"],
])
def test_usage_errors_exit_2(run, argv):
    r = run(*argv)
    assert r.code == 2 and r.out == ""
    assert "error:" in r.err and "usage:" in r.err
    j = run("--json", *argv)
    assert j.code == 2
    err = j.json()["error"]
    assert set(err) == {"code", "message", "hint", "details"} and err["code"] == "usage"


def test_version_flag(run):
    r = run("--version")
    assert r.code == 0 and r.out.strip() == f"things-bridge {__version__}"


def test_help(run):
    r = run("--help")
    assert r.code == 0 and "exit codes:" in r.out and "token set" in r.out
    r = run("add", "--help")
    assert r.code == 0 and "--dry-run" in r.out


def test_no_arguments_prints_help_and_exits_2(run):
    r = run()
    assert r.code == 2 and "COMMAND" in r.err and r.out == ""


# ---- reads: human output and --json shapes ------------------------------------------------------------------------

def test_list_human(run, w):
    r = run("today")
    assert r.code == 0
    lines = r.out.splitlines()
    assert lines[0] == "Today (2)"
    milk = next(ln for ln in lines if w.milk in ln)
    assert "[ ] Buy milk" in milk and "due 2026-10-02" in milk and "in Plan Birthday Party" in milk
    assert "#Errand" in milk
    assert lines[lines.index(milk) + 1].strip() == "2% …"               # notes preview, first line
    passport = next(ln for ln in lines if w.passport in ln)
    assert "overdue 2026-09-01" in passport


def test_list_json_shape(run, w):
    data = run("--json", "list", "today").json()
    assert set(data) == keys(Page)
    assert data["returned"] == 2 and data["total"] == 2 and data["truncated"] is False
    assert set(data["items"][0]) == keys(Item)
    assert "checklist items" in data["unreadable_fields"]
    assert run("--json", "today").json() == data


def test_list_paging_footer(run, fake_things):
    for i in range(3):
        fake_things.seed_todo(f"T{i}", when="today")
    r = run("today", "--limit", "2")
    assert r.out.splitlines()[0] == "Today (1–2 of 3)"
    assert "… more with --offset 2" in r.out
    assert run("today", "--limit", "2", "--offset", "2").out.splitlines()[0] == "Today (3–3 of 3)"


def test_empty_list(run):
    r = run("someday")
    assert r.code == 0 and r.out.splitlines() == ["Someday (0)", "  (nothing here)"]


def test_logbook(run, w):
    r = run("logbook")
    assert r.code == 0 and "Call Paolo" in r.out and "[x]" in r.out and "completed 2026-09-28" in r.out
    assert r.out.startswith("Logbook since 7d")
    data = run("--json", "logbook", "--since", "all").json()
    assert set(data) == keys(Page) and data["items"][0]["title"] == "Call Paolo"
    bad = run("logbook", "--since", "soon")
    assert bad.code == 2 and "since" in bad.err


def test_get_human_and_json(run, w):
    r = run("get", w.milk)
    assert r.code == 0
    lines = r.out.splitlines()
    assert lines[0] == "Buy milk"
    assert f"  id:        {w.milk}" in lines
    assert "  status:    open" in lines and "  deadline:  2026-10-02" in lines
    assert f"  project:   Plan Birthday Party ({w.party})" in lines
    assert lines[-3:] == ["  notes:", "    2%", "    lactose free"]
    data = run("--json", "get", w.milk).json()
    assert set(data) == keys(GetResult) and data["kind"] == "to-do" and data["item"]["notes"] == "2%\nlactose free"


def test_get_project_children_and_area(run, w):
    r = run("get", w.party, "--children")
    assert r.code == 0 and "Contents (" in r.out and "Buy milk" in r.out and "Call Paolo" in r.out
    r = run("get", w.family)
    assert r.code == 0 and r.out.splitlines()[0] == "Family" and "  kind:      area" in r.out
    assert "  tags:      Home" in r.out


def test_projects_areas_tags(run, w):
    r = run("projects")
    assert r.code == 0 and "Plan Birthday Party" in r.out and "(project" in r.out
    r = run("projects", "--area", "Work")
    assert "Taxes" in r.out and "Plan Birthday Party" not in r.out
    r = run("areas")
    assert r.out.splitlines()[0] == "Areas (2)" and f"  {w.family}  Family  (#Home)" in r.out.splitlines()
    assert set(run("--json", "areas").json()) == keys(AreaList)
    r = run("tags")
    lines = r.out.splitlines()
    assert lines[0] == "Tags (3)"
    assert f"  {w.home}  Home" in lines and f"  {w.errand}    Errand" in lines       # child indented
    assert lines.index(f"  {w.errand}    Errand") == lines.index(f"  {w.home}  Home") + 1
    assert f"  {w.deep}  Deep Work  [d]" in lines
    assert set(run("--json", "tags").json()) == keys(TagList)


def test_search_joins_words(run, w, fake_runner):
    r = run("search", "BUY", "mílk")
    assert r.code == 0 and "Search 'BUY mílk'" in r.out and "Buy milk" in r.out


def test_find_and_deadlines(run, w):
    r = run("find", "--tag", "errand")
    assert r.code == 0 and "Buy milk" in r.out and "Renew passport" not in r.out
    assert run("find").code == 2                            # at least one filter
    def listed(out: str) -> set[str]:
        return {ln.split()[0] for ln in out.splitlines()[1:] if ln.startswith("  ") and not ln.startswith("      ")}

    assert listed(run("deadlines").out) == {w.passport, w.milk}          # the party is 22 days out
    assert listed(run("deadlines", "--within-days", "30").out) == {w.passport, w.milk, w.party}
    assert listed(run("deadlines", "--within-days", "all", "--no-overdue", "--no-projects").out) == {w.milk}
    assert run("deadlines", "--within-days", "all").out.startswith("Deadlines (")


def test_user_content_cannot_drive_the_terminal(run, fake_things):
    evil = "Evil\x1b[31mred\x07" + chr(0x202E) + "txt.exe"
    tid = fake_things.seed_todo(evil, when="today", notes="line\x1b]0;title\x07")
    r = run("today")
    assert "\x1b" not in r.out and "\x07" not in r.out and chr(0x202E) not in r.out
    assert "Evil[31mred" in r.out
    r = run("get", tid)
    assert "\x1b" not in r.out
    assert run("--json", "get", tid).json()["item"]["title"] == evil       # --json keeps the data exact


# ---- writes ------------------------------------------------------------------------------------------------------

def test_add_human(run, fake_things):
    r = run("add", "Buy", "bread", "--when", "today")
    assert r.code == 0
    new = fake_things.one("Buy bread")
    assert r.out.splitlines()[0] == "add_todo: done, confirmed in Things."
    assert new["id"] in r.out


def test_add_every_option(run, w, fake_runner, fake_things):
    r = run("add", "Balloons", "--notes", "red ones", "--when", "tomorrow", "--reminder", "6pm", "--deadline",
            "2026-10-10", "--tags", "errand", "--checklist", "a", "--checklist", "b", "--project",
            "plan birthday party", "--heading", "Venue", "--reveal")
    assert r.code == 0, r.err
    p = sent_params(fake_runner)
    assert p["title"] == "Balloons" and p["notes"] == "red ones" and p["list-id"] == w.party
    assert p["when"] == "tomorrow@6:00pm" and p["deadline"] == "2026-10-10" and p["tags"] == "Errand"
    assert p["checklist-items"] == "a\nb" and p["heading"] == "Venue" and p["reveal"] == "true"
    assert "list" not in p                                  # N9: titles are resolved to ids
    assert "headings can't be read" in r.out                # service warning is shown
    assert "can't be checked:" in r.out


def test_add_unknown_tag_warns_but_strict_fails(run, fake_things):
    r = run("add", "x", "--tags", "Nope")
    assert r.code == 0 and "warning: Tag 'Nope' doesn't exist" in r.out
    r = run("add", "y", "--tags", "Nope", "--strict-tags")
    assert r.code == 2 and "Nope" in r.err


def test_add_json_shape(run):
    data = run("--json", "add", "Buy", "bread").json()
    assert set(data) == keys(WriteResult)
    assert data["action"] == "add_todo" and data["sent"] is True and data["verified"] is True
    assert len(data["ids"]) == 1 and data["links"] == [f"things:///show?id={data['ids'][0]}"]


def test_dry_run_sends_nothing(run, fake_runner, fake_things, w):
    r = run("add", "Nothing", "--dry-run")
    assert r.code == 0 and r.out.startswith("Dry run (add_todo): nothing was sent.")
    assert "would open: things:///add?title=Nothing" in r.out
    r = run("update", w.milk, "--title", "Oat milk", "--dry-run")
    assert r.code == 0 and f"{w.milk}  [ ] Buy milk" in r.out            # the target as it is now
    assert "&auth-token=***" in r.out
    assert fake_runner.sent == [] and fake_things.log == [] and fake_things.one("Buy milk")


def test_dry_run_off_macos(run, fake_things, clock):
    r = run("update", ID, "--title", "x", "--dry-run", runner=FakeRunner(fake_things, clock=clock, macos=False))
    assert r.code == 0 and "not validated against Things (not macOS)" in r.out
    assert f"things:///update?id={ID}&title=x&auth-token=***" in r.out


def test_add_from_stdin(run, fake_things):
    r = run("add", "-", stdin="Call Anna\nabout the trip\n\nbring maps\n")
    assert r.code == 0, r.err
    it = fake_things.one("Call Anna")
    assert it["notes"] == "about the trip\n\nbring maps"


def test_add_from_stdin_title_only_plus_notes_flag(run, fake_things):
    r = run("add", "-", "--notes", "flag notes", stdin="Title only\n")
    assert r.code == 0 and fake_things.one("Title only")["notes"] == "flag notes"


def test_notes_from_stdin(run, fake_things):
    r = run("add", "Letter", "--notes", "-", stdin="Dear X,\nhello\n")
    assert r.code == 0 and fake_things.one("Letter")["notes"] == "Dear X,\nhello"


@pytest.mark.parametrize("argv, stdin, field", [
    (["add", "-"], "", "title"),
    (["add", "-"], "\nnotes without a title", "title"),
    (["add", "-", "--notes", "x"], "Title\nstdin notes", "notes"),
    (["add", "-", "--notes", "-"], "Title\n", "title"),
])
def test_stdin_conflicts_and_empty_input(run, fake_runner, argv, stdin, field):
    r = run("--json", *argv, stdin=stdin)
    assert r.code == 2
    err = r.json()["error"]
    assert err["code"] == "invalid_input" and err["details"]["field"] == field
    assert fake_runner.dispatch_calls == []


def test_add_many_json_lines_and_file(run, fake_things, tmp_path):
    r = run("add-many", "-", stdin=json.dumps(["One", {"title": "Two", "when": "today", "tags": ["x"]}]))
    assert r.code == 0, r.err
    assert fake_things.one("One") and fake_things.one("Two")
    r = run("add-many", "-", stdin="Three\n\n  Four  \n")
    assert r.code == 0 and fake_things.one("Three") and fake_things.one("Four")
    f = tmp_path / "todos.json"
    f.write_text(json.dumps([{"title": "Five", "notes": "n"}]), encoding="utf-8")
    assert run("add-many", f).code == 0 and fake_things.one("Five")["notes"] == "n"


@pytest.mark.parametrize("stdin, message", [
    ('[{"title": "a", "colour": "red"}]', "unknown key 'colour'"),
    ('[{"notes": "no title"}]', "title is required"),
    ('[42]', "must be an object"),
    ('{"title": "an object"}', "must be a JSON array"),
    ('[{"title": ', "not valid JSON (line 1"),
    ("\n\n", "no entries"),
])
def test_add_many_bad_input(run, fake_runner, stdin, message):
    r = run("add-many", "-", stdin=stdin)
    assert r.code == 2 and message in r.err
    assert fake_runner.dispatch_calls == []


def test_add_many_needs_confirm_above_10(run, fake_runner):
    titles = "\n".join(f"T{i}" for i in range(11))
    r = run("--json", "add-many", "-", stdin=titles)
    assert r.code == 4 and r.json()["error"]["code"] == "refused"
    assert fake_runner.dispatch_calls == []
    assert run("add-many", "-", "--confirm", stdin=titles).code == 0


def test_add_project_items_in_order(run, fake_runner, fake_things):
    r = run("add-project", "Trip", "--todo", "Pack", "--heading", "Day 1", "--todo", "Fly", "--area", "Work")
    assert r.code == 5                                          # no area "Work" in an empty library
    fake_things.seed_area("Work")
    r = run("add-project", "Trip", "--todo", "Pack", "--heading", "Day 1", "--todo", "Fly", "--area", "Work")
    assert r.code == 0, r.err
    items = payload(fake_runner)[0]["attributes"]["items"]
    assert [(i["type"], i["attributes"]["title"]) for i in items] == [("to-do", "Pack"), ("heading", "Day 1"),
                                                                      ("to-do", "Fly")]
    r = run("add-project", "Plain", "--todo", "a", "--todo", "b")
    assert r.code == 0 and fake_runner.sent[-1].command == "add-project"
    assert sent_params(fake_runner)["to-dos"] == "a\nb"


def test_add_project_items_from_stdin(run, fake_runner):
    items = [{"kind": "heading", "title": "H"}, "loose", {"kind": "todo", "title": "t", "when": "someday"}]
    r = run("add-project", "P", "--items", "-", stdin=json.dumps(items))
    assert r.code == 0, r.err
    got = payload(fake_runner)[0]["attributes"]["items"]
    assert [i["attributes"]["title"] for i in got] == ["H", "loose", "t"]
    r = run("add-project", "P", "--todo", "x", "--items", "-", stdin="[]")
    assert r.code == 2 and "not both" in r.err


def test_update(run, w, fake_things):
    r = run("update", w.milk, "--title", "Buy oat milk", "--append-notes", " (oat)", "--add-tags", "home")
    assert r.code == 0, r.err
    it = fake_things.get(w.milk)
    assert it["title"] == "Buy oat milk" and it["notes"].endswith(" (oat)") and "Home" in it["tags"]
    assert r.out.splitlines()[0] == "update_todo: done, confirmed in Things."


def test_update_clears_and_stdin_notes(run, w, fake_things):
    assert run("update", w.milk, "--clear-notes", "--clear-tags", "--clear-deadline").code == 0
    it = fake_things.get(w.milk)
    assert (it["notes"], it["tags"], it["deadline"]) == ("", [], None)
    assert run("update", w.milk, "--notes", "-", stdin="fresh notes\n").code == 0
    assert fake_things.get(w.milk)["notes"] == "fresh notes"


def test_update_with_only_unknown_tags_sends_nothing(run, w, fake_runner):
    r = run("update", w.milk, "--tags", "Nope")
    assert r.code == 0 and r.out.splitlines()[0] == "update_todo: nothing was sent."
    assert "warning: Tag 'Nope' doesn't exist" in r.out and fake_runner.sent == []


def test_update_that_cant_be_checked(run, w):
    r = run("update", w.milk, "--checklist-append", "cold")
    assert r.code == 0 and r.out.splitlines()[0] == "update_todo: sent."
    assert "  can't be checked: checklist" in r.out.splitlines()


def test_completing_a_project_lists_its_open_children(run, w, fake_runner):
    r = run("complete", w.party)
    assert r.code == 4 and "hint: complete or cancel these first" in r.err
    assert f"  {w.milk}  Buy milk" in r.err.splitlines() and fake_runner.sent == []
    data = run("--json", "complete", w.party).json()["error"]
    assert data["details"]["open_children"] == [{"id": w.milk, "title": "Buy milk"}]


def test_update_nothing_to_update(run, w):
    r = run("--json", "update", w.milk)
    assert r.code == 2 and r.json()["error"]["code"] == "invalid_input"


def test_update_project(run, w, fake_runner, fake_things):
    r = run("update-project", w.taxes, "--area", "Family", "--notes", "for 2026")
    assert r.code == 0, r.err
    assert fake_runner.sent[-1].command == "update-project"
    assert fake_things.get(w.taxes)["area"] == w.family


def test_status_commands(run, w, fake_things):
    assert run("complete", w.passport).code == 0 and fake_things.get(w.passport)["status"] == "completed"
    assert run("reopen", w.passport).code == 0 and fake_things.get(w.passport)["status"] == "open"
    r = run("cancel", "-", stdin=f"{w.passport}\n{w.inbox}\n")
    assert r.code == 0, r.err
    assert fake_things.get(w.passport)["status"] == fake_things.get(w.inbox)["status"] == "canceled"
    assert run("complete", "-", stdin="  \n").code == 2


def test_schedule_move_tag(run, w, fake_things):
    assert run("schedule", w.inbox, "--when", "tomorrow", "--deadline", "2026-10-30").code == 0
    it = fake_things.get(w.inbox)
    assert it["deadline"] == "2026-10-30"
    assert run("move", w.inbox, "--project", "Taxes").code == 0 and fake_things.get(w.inbox)["project"] == w.taxes
    assert run("tag", w.inbox, "--add", "Errand").code == 0 and fake_things.get(w.inbox)["tags"] == ["Errand"]
    assert run("tag", w.inbox, "--clear").code == 0 and fake_things.get(w.inbox)["tags"] == []
    r = run("tag", w.inbox, "--add", "Nope")                   # the tag command never drops a tag silently
    assert r.code == 2 and "doesn't exist" in r.err


def test_json_command(run, fake_runner, fake_things, tmp_path):
    data = [{"type": "to-do", "attributes": {"title": "From JSON"}}]
    assert run("json", "-", stdin=json.dumps(data)).code == 0 and fake_things.one("From JSON")
    f = tmp_path / "payload.json"
    f.write_text(json.dumps([{"type": "to-do", "attributes": {"title": "From file"}}]), encoding="utf-8")
    assert run("json", str(f)).code == 0 and fake_things.one("From file")


@pytest.mark.parametrize("stdin, message", [
    ('{"type": "to-do"}', "must be a JSON array"),
    ("[", "not valid JSON"),
    ('[{"type": "to-do", "attributes": {"title": "x", "list": "Work"}}]', "list titles are refused"),
])
def test_json_command_bad_input(run, fake_runner, stdin, message):
    r = run("json", "-", stdin=stdin)
    assert r.code == 2 and message in r.err
    assert fake_runner.dispatch_calls == []


def test_unreadable_file(run, tmp_path):
    r = run("--json", "json", tmp_path / "missing.json")
    assert r.code == 2 and "can't read" in r.json()["error"]["message"]
    bad = tmp_path / "latin1.json"
    bad.write_bytes(b"[\xff]")
    assert "isn't UTF-8" in run("json", bad).err


def test_things_own_files_are_never_read(run, tmp_path, monkeypatch):
    """N1: a FILE argument inside Things' data (directly or through a symlink) is refused unopened."""
    inside = tmp_path / "Group Containers" / "JLMPQHK86H.com.culturedcode.ThingsMac"
    inside.mkdir(parents=True)
    target = inside / "data.json"
    target.write_text("[]", encoding="utf-8")
    link = tmp_path / "innocent.json"
    link.symlink_to(target)
    real_read = Path.read_text

    def guarded(self, *a, **k):
        assert "culturedcode" not in str(self.resolve()).lower(), "read a Things file"
        return real_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", guarded)
    for path in (target, link):
        r = run("--json", "json", path)
        assert r.code == 4 and r.json()["error"]["code"] == "refused"


# ---- navigation ---------------------------------------------------------------------------------------------------

def test_show_and_open_search(run, w, fake_things):
    r = run("show", "--list", "today")
    assert r.code == 0 and r.out.strip() == "Opened things:///show?id=today"
    assert set(run("--json", "show", w.milk).json()) == keys(NavResult)
    r = run("open-search", "buy", "milk")
    assert r.code == 0 and r.out.strip() == "Opened things:///search?query=buy%20milk"
    assert run("open-search").out.strip() == "Opened things:///search"
    assert run("show").code == 2 and run("show", w.milk, "--list", "today").code == 2


# ---- exit codes -----------------------------------------------------------------------------------------------------

def test_exit_3_not_macos(run, fake_things, clock):
    r = run("--json", "today", runner=FakeRunner(fake_things, clock=clock, macos=False))
    assert r.code == 3 and r.json()["error"]["code"] == "not_macos"
    r = run("today", runner=FakeRunner(fake_things, clock=clock, macos=False))
    assert r.code == 3 and r.err.startswith("error: things-bridge controls Things 3 on macOS")


def test_no_launch(run, fake_runner, fake_things):
    fake_things.running = False
    r = run("--json", "--no-launch", "today")
    assert r.code == 3 and r.json()["error"]["code"] == "things_not_running" and fake_runner.launches == 0
    assert run("today").code == 0 and fake_runner.launches == 1


def test_exit_4_refused(run, w, fake_things, monkeypatch):
    r = run("--json", "schedule", w.tmpl, "--when", "tomorrow")          # the repeating-template guard
    assert r.code == 4 and r.json()["error"]["code"] == "refused"
    assert fake_things.crashes == 0
    ids = [fake_things.seed_todo(f"Batch {i}", when="today") for i in range(11)]
    r = run("complete", *ids)
    assert r.code == 4 and "confirm" in r.err
    assert run("complete", *ids, "--confirm").code == 0
    monkeypatch.setenv("THINGS_BRIDGE_READ_ONLY", "1")
    r = run("add", "x")
    assert r.code == 4 and "read-only" in r.err


def test_exit_5_not_found_and_ambiguous(run, w, fake_things):
    r = run("--json", "get", GHOST)
    assert r.code == 5 and r.json()["error"]["code"] == "not_found"
    r = run("add", "x", "--project", "Plan Birthday Partyy")
    assert r.code == 5 and "hint: Did you mean 'Plan Birthday Party'?" in r.err
    other = fake_things.seed_project("Taxes", area=w.family)
    r = run("add", "x", "--project", "taxes")
    assert r.code == 5
    assert f"  {w.taxes}  Taxes  (in Work)" in r.err.splitlines()
    assert f"  {other}  Taxes  (in Family)" in r.err.splitlines()
    data = run("--json", "add", "x", "--project", "taxes").json()["error"]
    assert data["code"] == "ambiguous" and len(data["details"]["candidates"]) == 2


def test_exit_6_not_verified(run, fake_runner, fake_things, w):
    fake_runner.drop_next_url()
    r = run("add", "Lost")
    assert r.code == 6
    assert r.out.splitlines()[0] == "add_todo: sent, but Things hasn't confirmed it."
    assert "Things didn't apply the change within" in r.err and "Nothing was re-sent" in r.err
    assert len(fake_runner.sent) == 1                            # never re-sent (N10)
    fake_runner.drop_next_url()
    r = run("--json", "update", w.milk, "--title", "Never")
    assert r.code == 6
    data = r.json()
    assert data["verified"] is False and data["reason"] and any(not c["ok"] for c in data["checks"])
    fake_runner.drop_next_url()
    r = run("update", w.milk, "--title", "Never")
    assert "mismatch: " in r.out and "title: expected 'Never', found 'Buy milk'" in r.out


def test_exit_1_dispatch_failure_is_redacted(run, fake_runner, w):
    fake_runner.fail_next_dispatch(DispatchFailed(f"open failed for things:///update?id={w.milk}&auth-token={SECRET}"))
    r = run("update", w.milk, "--title", "x")
    assert r.code == 1 and "auth-token=***" in r.err and SECRET not in r.err
    fake_runner.fail_next_dispatch(DispatchFailed(f"open failed: auth-token={SECRET}"))
    r = run("--json", "update", w.milk, "--title", "x")
    assert r.code == 1 and SECRET not in r.out and r.json()["error"]["code"] == "dispatch_failed"


def test_exit_1_internal_error(run, monkeypatch):
    def boom(self, *a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(cli.service.ThingsService, "areas", boom)
    r = run("areas")
    assert r.code == 1 and r.err.startswith("error: internal error: RuntimeError: kaboom")
    assert "Traceback" not in r.err
    data = run("--json", "areas").json()["error"]
    assert data["code"] == "internal_error"
    r = run("-vv", "areas")
    assert "Traceback" in r.err                                  # -vv logs it (redacted) to stderr


def test_keyboard_interrupt(run, monkeypatch):
    def interrupted(self, *a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.service.ThingsService, "tags", interrupted)
    r = run("tags")
    assert r.code == 130 and "interrupted" in r.err


def test_bad_environment_variable(run, monkeypatch):
    monkeypatch.setenv("THINGS_BRIDGE_MAX_BATCH", "lots")
    r = run("today")
    assert r.code == 2 and "THINGS_BRIDGE_MAX_BATCH" in r.err


def test_error_json_shape(run):
    err = run("--json", "get", GHOST).json()
    assert set(err) == {"error"} and set(err["error"]) == {"code", "message", "hint", "details"}


# ---- global flags ---------------------------------------------------------------------------------------------------

def test_no_verify(run, fake_runner):
    data = run("--json", "--no-verify", "add", "Unchecked").json()
    assert data["sent"] is True and data["verified"] is None
    assert fake_runner.calls("FIND_CREATED") == []
    r = run("--no-verify", "add", "Unchecked 2")
    assert r.code == 0 and "(not verified: --no-verify)" in r.out.splitlines()[0]


def test_verbose_shows_urls_and_logs(run, w, fake_runner):
    r = run("-v", "update", w.milk, "--title", "Buy milk!")
    assert f"opened: things:///update?id={w.milk}" in r.out and "auth-token=***" in r.out
    assert "before:" in r.out and "Buy milk  (" in r.out


def test_logging_goes_to_stderr_with_level_control(run, w, fake_runner, monkeypatch):
    fake_runner.inject("SCAN", exc=ProtocolError("torn frame"))
    r = run("today")
    assert r.code == 0 and "things-bridge: WARNING: SCAN list failed (torn frame)" in r.err
    assert "WARNING" not in r.out
    monkeypatch.setenv("THINGS_BRIDGE_LOG", "ERROR")
    fake_runner.inject("SCAN", exc=ProtocolError("torn frame"))
    assert "SCAN list failed" not in run("today").err


def test_logging_setup_is_idempotent(run, w):
    run("today")
    run("today")
    ours = [h for h in logging.getLogger("things_bridge").handlers if getattr(h, cli._HANDLER_FLAG, False)]
    assert len(ours) == 1 and any(isinstance(f, runner_mod.RedactFilter) for f in ours[0].filters)


def test_log_level_parsing(monkeypatch):
    assert cli._log_level(2) == logging.DEBUG and cli._log_level(1) == logging.INFO
    assert cli._log_level(0) == logging.WARNING
    for raw, level in (("debug", logging.DEBUG), ("ERROR", logging.ERROR), ("15", 15), ("loud", logging.WARNING)):
        monkeypatch.setenv("THINGS_BRIDGE_LOG", raw)
        assert cli._log_level(0) == level


# ---- token (CLI only) ------------------------------------------------------------------------------------------------

def test_token_status(run, keychain, monkeypatch):
    r = run("token", "status")
    assert r.code == 3 and r.out.startswith("Token: missing.") and "token set" in r.out
    keychain["token"] = FAKE_TOKEN
    r = run("token", "status")
    assert r.code == 0 and r.out.strip() == "Token: login Keychain (format valid)"
    assert run("--json", "token", "status").json() == {"source": "keychain", "valid": True}
    monkeypatch.setenv(authtoken.ENV, "bad token!")
    r = run("--json", "token", "status")
    assert r.code == 3 and r.json() == {"source": "env", "valid": False}
    assert FAKE_TOKEN not in r.out and "bad token!" not in r.out


def test_token_status_without_keychain(run):
    r = run("--json", "token", "status")
    assert r.code == 3 and r.json()["error"]["code"] == "not_macos"


def test_token_set_from_stdin(run, keychain, monkeypatch):
    r = run("token", "set", "--stdin", stdin=f"  {FAKE_TOKEN}\n")
    assert r.code == 0 and keychain["sets"] == [FAKE_TOKEN]
    assert r.out.strip() == f"Saved the auth token to the login Keychain ({len(FAKE_TOKEN)} characters)."
    assert FAKE_TOKEN not in r.out + r.err
    monkeypatch.setenv(authtoken.ENV, FAKE_TOKEN)
    r = run("--json", "token", "set", "--stdin", stdin=FAKE_TOKEN)
    assert r.json() == {"stored": True, "length": len(FAKE_TOKEN), "overridden_by_env": True}
    assert FAKE_TOKEN not in r.out + r.err
    r = run("token", "set", "--stdin", stdin=FAKE_TOKEN)
    assert "note: THINGS_AUTH_TOKEN is set, and it overrides the Keychain." in r.err


def test_token_set_prompts_with_getpass(run, keychain, monkeypatch):
    prompts = []

    def fake_getpass(prompt):
        prompts.append(prompt)
        return FAKE_TOKEN

    monkeypatch.setattr(cli.getpass, "getpass", fake_getpass)
    r = run("token", "set", tty=True)
    assert r.code == 0 and keychain["token"] == FAKE_TOKEN and prompts and "hidden" in prompts[0]
    assert FAKE_TOKEN not in r.out + r.err


def test_token_set_refusals(run, keychain, monkeypatch):
    r = run("--json", "token", "set")                                   # no terminal, no --stdin
    assert r.code == 2 and "--stdin" in r.json()["error"]["hint"]
    assert run("token", "set", "--stdin", stdin="\n").code == 2
    r = run("token", "set", "--stdin", stdin="has spaces inside\n")
    assert r.code == 2 and "has spaces" not in r.out + r.err
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "")
    assert run("token", "set", tty=True).code == 2
    assert keychain["sets"] == []


@pytest.mark.skipif(sys.platform == "darwin", reason="exercises the real set_token's non-macOS path")
def test_token_set_off_macos_uses_the_real_validation(run):
    r = run("token", "set", "--stdin", stdin=FAKE_TOKEN)
    assert r.code == 3 and "Keychain" in r.err and FAKE_TOKEN not in r.out + r.err
    r = run("token", "set", "--stdin", stdin="x")
    assert r.code == 2                                                  # validated before the platform check


# ---- version and doctor ----------------------------------------------------------------------------------------------

def test_version(run, fake_things, clock):
    r = run("version")
    assert r.code == 0
    assert r.out.splitlines()[:3] == [f"things-bridge  {__version__}", "URL scheme     2",
                                      f"Things         {fake_things.version}"]
    data = run("--json", "version").json()
    assert data["things"] == fake_things.version and data["things_error"] is None and data["url_scheme"] == "2"
    off = run("--json", "version", runner=FakeRunner(fake_things, clock=clock, macos=False))
    assert off.code == 0 and off.json()["things"] is None and "macOS" in off.json()["things_error"]


def test_doctor(run, fake_things, fake_runner, clock, monkeypatch):
    monkeypatch.setenv(authtoken.ENV, FAKE_TOKEN)
    r = run("doctor")
    assert r.code == 0 and "  WARN  url_scheme" in r.out and r.out.rstrip().endswith("with warnings: url_scheme.")
    data = run("--json", "doctor").json()
    assert set(data) == keys(DoctorReport) and data["ok"] is True
    r = run("--json", "doctor", runner=FakeRunner(fake_things, clock=clock, macos=False))
    assert r.code == 3 and r.json()["ok"] is False
    assert run("doctor", "--check-repeating", "not an id").code == 2
    assert FAKE_TOKEN not in r.out


def test_doctor_prompt_goes_to_stderr_and_no_launch_is_passed(run, fake_runner, fake_things, monkeypatch):
    monkeypatch.setenv("__CFBundleIdentifier", "com.apple.Terminal")
    r = run("--json", "doctor", "--prompt-automation")
    assert "macOS will ask to let Terminal control Things; click OK" in r.err
    json.loads(r.out)                                                   # stdout stays pure JSON
    assert fake_runner.calls("AUTOMATION")[-1].timeout == 130.0
    fake_things.running = False
    r = run("--json", "--no-launch", "doctor")
    assert {c["name"]: c["status"] for c in r.json()["checks"]}["things_running"] == "warn"
    assert fake_runner.launches == 0


# ---- selftest --------------------------------------------------------------------------------------------------------

@pytest.fixture
def fake_selftest(monkeypatch):
    calls = []
    outcome = {"ok": True}

    def run_selftest(runner, config, *, yes, probe_conflicts=False, progress=None):
        calls.append(SimpleNamespace(runner=runner, config=config, yes=yes, probe_conflicts=probe_conflicts))
        steps = [SelftestStep("ST-0", "pass", "doctor basics", []), SelftestStep("ST-1", "pass", "project", [ID])]
        for s in steps[:1]:                         # the last step is only in the report, not via progress
            progress(s)
        return SelftestReport(ok=outcome["ok"], project_id=ID, created_ids=[ID], steps=steps,
                              cleanup_note="Nothing was deleted.")

    module = SimpleNamespace(run_selftest=run_selftest, calls=calls, outcome=outcome)
    monkeypatch.setattr(cli, "_load_selftest", lambda: module)
    return module


def test_selftest_needs_consent(run, fake_selftest, fake_runner):
    r = run("selftest")
    assert r.code == 4 and "consent" in r.err and fake_selftest.calls == []
    r = run("selftest", tty=True, stdin="n\n")
    assert r.code == 0 and "Cancelled; nothing was created." in r.err and fake_selftest.calls == []
    assert "things-bridge selftest" in r.err and "Nothing is deleted" in r.err       # the consent text
    r = run("selftest", tty=True, stdin="y\n")
    assert r.code == 0 and fake_selftest.calls[-1].yes is True
    assert fake_runner.dispatch_calls == []


def test_selftest_with_yes(run, fake_selftest, fake_runner):
    r = run("selftest", "--yes", "--probe-conflicts")
    assert r.code == 0
    call = fake_selftest.calls[-1]
    assert call.yes is True and call.probe_conflicts is True and call.runner is fake_runner
    assert r.out.count("ST-0") == 1 and "ST-1" in r.out                 # progress, plus steps not yet shown
    assert "Selftest passed." in r.out and "Nothing was deleted." in r.out
    fake_selftest.outcome["ok"] = False
    r = run("--json", "selftest", "--yes")
    assert r.code == 1 and set(r.json()) == keys(SelftestReport) and "ST-0" in r.err


def test_selftest_config_carries_global_flags(run, fake_selftest):
    run("--no-launch", "--no-verify", "selftest", "--yes")
    cfg = fake_selftest.calls[-1].config
    assert cfg.allow_launch is False and cfg.verify is False


def test_missing_selftest_module_is_a_clean_error(run, monkeypatch):
    import things_bridge
    monkeypatch.delattr(things_bridge, "selftest", raising=False)
    monkeypatch.setitem(sys.modules, "things_bridge.selftest", None)
    cli.build_parser()                                                  # the parser never needs it
    r = run("selftest", "--yes")
    assert r.code == 1 and "selftest isn't part of this installation" in r.err and "Traceback" not in r.err


# ---- mcp ----------------------------------------------------------------------------------------------------------

@pytest.fixture
def fake_mcp(monkeypatch):
    calls = []

    def main(argv=None):
        calls.append(list(argv))

    module = SimpleNamespace(main=main, calls=calls)
    monkeypatch.setattr(cli, "_load_mcp_server", lambda: module)
    return module


def test_mcp_passes_read_only_and_writes_nothing_to_stdout(run, fake_mcp, monkeypatch):
    monkeypatch.setenv("THINGS_BRIDGE_NO_LAUNCH", "0")                  # restored after the test
    monkeypatch.setenv("THINGS_BRIDGE_LOG", "WARNING")
    r = run("mcp")
    assert r.code == 0 and r.out == "" and fake_mcp.calls == [[]]
    r = run("--no-launch", "-v", "mcp", "--read-only")
    assert r.code == 0 and r.out == "" and fake_mcp.calls[-1] == ["--read-only"]
    import os
    assert os.environ["THINGS_BRIDGE_NO_LAUNCH"] == "1" and os.environ["THINGS_BRIDGE_LOG"] == "INFO"
    r = run("--no-verify", "mcp")
    assert r.out == "" and "--no-verify doesn't apply" in r.err


def test_mcp_startup_error_becomes_the_exit_code(run, monkeypatch):
    def main(argv=None):
        raise SystemExit(3)

    monkeypatch.setattr(cli, "_load_mcp_server", lambda: SimpleNamespace(main=main))
    assert run("mcp").code == 3


def test_missing_mcp_is_a_clean_error(run, monkeypatch):
    import things_bridge
    monkeypatch.delattr(things_bridge, "mcp_server", raising=False)
    monkeypatch.delitem(sys.modules, "things_bridge.mcp_server", raising=False)
    for name in [m for m in sys.modules if m.startswith("mcp.")]:     # loaded by other tests: evict them too
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "mcp", None)                     # as if the dependency were missing
    r = run("mcp")
    assert r.code == 1 and "Traceback" not in r.err
    assert "MCP server" in r.err


# ---- setup (interactive) ---------------------------------------------------------------------------------------------

@pytest.fixture
def pasted(monkeypatch):
    """getpass returns these values in turn."""
    values: list[str] = []
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: values.pop(0))
    return values


def test_setup_happy_path(run, keychain, pasted, fake_runner, fake_selftest, monkeypatch):
    monkeypatch.setenv("__CFBundleIdentifier", "com.apple.Terminal")
    pasted.append(FAKE_TOKEN)
    r = run("setup", tty=True, stdin="\nn\n")                           # Return at step 4, "n" to the selftest
    assert r.code == 0, r.out + r.err
    for step in ("1. This Mac", "2. Things", "3. Automation permission", "4. Things URLs", "5. Auth token",
                 "6. Self-test"):
        assert step in r.out
    assert "macOS will now ask whether Terminal may control Things. Click OK." in r.out
    assert "  ok    versions  things-bridge " in r.out and ", URL scheme 2" in r.out
    assert "turn on Enable Things URLs" in r.out and "Click Manage next to Enable Things URLs" in r.out
    assert f"Saved to the login Keychain ({len(FAKE_TOKEN)} characters)." in r.out
    assert FAKE_TOKEN not in r.out + r.err and keychain["token"] == FAKE_TOKEN
    assert fake_runner.calls("AUTOMATION")[0].timeout == 130.0
    assert "Run a live self-test now?" in r.err and fake_selftest.calls == []
    assert r.out.rstrip().endswith("Setup finished.")
    assert fake_runner.dispatch_calls == []


def test_setup_runs_the_selftest_when_asked(run, keychain, pasted, fake_selftest):
    keychain["token"] = FAKE_TOKEN
    r = run("setup", tty=True, stdin="\nn\ny\n")                        # keep the token, then yes to the selftest
    assert r.code == 0 and "Kept the stored token." in r.out
    assert fake_selftest.calls and fake_selftest.calls[-1].yes is True and "Selftest passed." in r.out


def test_setup_retries_a_bad_token_then_skips(run, keychain, pasted):
    pasted += ["not a token!", ""]
    r = run("setup", tty=True, stdin="\nn\n")
    assert r.code == 3 and "doesn't look like a Things URL auth token" in r.err
    assert "Skipped. Updates need the token" in r.out and "still to fix: token" in r.out
    assert "not a token!" not in r.out + r.err and keychain["sets"] == []


def test_setup_stops_early_off_macos(run, fake_things, clock):
    r = FakeRunner(fake_things, clock=clock, macos=False)
    res = run("setup", tty=True, runner=r)
    assert res.code == 3 and "FAIL" in res.out and "2. Things" not in res.out and r.template_calls == []


def test_setup_with_denied_automation(run, keychain, pasted, fake_runner, fake_selftest):
    fake_runner.inject("AUTOMATION", osa_code=-1743)
    pasted.append(FAKE_TOKEN)
    r = run("setup", tty=True, stdin="\n")
    assert r.code == 3 and "Privacy & Security" in r.out and "still to fix: automation" in r.out
    assert "Skipped until Automation access works" in r.out and fake_selftest.calls == []


def test_setup_is_interactive_only(run):
    assert run("setup").code == 2
    r = run("--json", "setup", tty=True)
    assert r.code == 2 and r.json()["error"]["details"]["field"] == "json"


# ---- helpers ---------------------------------------------------------------------------------------------------------

def test_wants_json_ignores_values_after_double_dash():
    assert cli._wants_json(["--json", "today"]) and cli._wants_json(["today", "--json"])
    assert not cli._wants_json(["add", "--", "--json"])


def test_secretish():
    assert cli._secretish(["add", "x", "--token", "abc123"]) == ["abc123"]
    assert cli._secretish(["--auth-token=zzz"]) == ["zzz"]
    assert cli._secretish(["token", "set", "--stdin", "leaked"]) == ["leaked"]
    assert cli._secretish(["today", "--json"]) == []


def test_title_and_notes():
    assert cli._title_and_notes("T\r\nline 1\r\nline 2\r\n") == ("T", "line 1\nline 2")
    assert cli._title_and_notes("  T  \n\n") == ("T", None)


# ---- process-level checks --------------------------------------------------------------------------------------------

def test_cli_import_is_lazy():
    code = ("import sys, things_bridge.cli as c; c.build_parser(); "
            "print(sorted(m for m in ('mcp', 'pydantic', 'things_bridge.mcp_server', 'things_bridge.selftest') "
            "if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"


@pytest.mark.skipif(sys.platform == "darwin", reason="on a Mac this would talk to the real Things")
def test_python_dash_m_off_macos(state_dir):
    import os
    env = {k: v for k, v in os.environ.items() if k != authtoken.ENV}
    proc = subprocess.run([sys.executable, "-m", "things_bridge", "--json", "version"], capture_output=True,
                          text=True, timeout=60, env=env)
    assert proc.returncode == 0 and json.loads(proc.stdout)["things"] is None
    proc = subprocess.run([sys.executable, "-m", "things_bridge", "--json", "today"], capture_output=True,
                          text=True, timeout=60, env=env)
    assert proc.returncode == 3 and json.loads(proc.stdout)["error"]["code"] == "not_macos"
    proc = subprocess.run([sys.executable, "-m", "things_bridge", "add", "x", "--dry-run"], capture_output=True,
                          text=True, timeout=60, env=env)
    assert proc.returncode == 0 and "would open: things:///add?title=x" in proc.stdout
