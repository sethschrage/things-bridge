"""§1.4 AppleScript lint over ``LIBRARY`` and every template, plus tests of the linter itself.

The rules are applied after stripping ``--`` comments and ``"…"`` string literals (the Things bundle id
literal is kept, so tell targets stay checkable). The linter is conservative: syntax it doesn't model,
and that could hide code from it (block or ``#`` comments, ``|identifiers|``, raw ``«codes»``, ``¬``
continuations, anything non-ASCII outside a string), is itself a violation.

Additions beyond the letter of §1.4, all stricter: ``EXTRA_FORBIDDEN`` (file, clipboard, environment and
remote access that N1/N4 rule out), ``open``/``run`` only as the status enumerator and the run handler
(N3: nothing but get/count/exists reaches Things), every ``tell`` targets Things, and ``current date``,
``time to GMT`` and ``tb_parseDay`` never run inside a tell (§1.1: local work happens outside Things).
"""

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from things_bridge.applescript import (ARITY, HEADER_NAMES, LIBRARY, RECORD_TAGS, TEMPLATES, Template,
                                       script_source)

THINGS = "com.culturedcode.ThingsMac"
DESIGN = Path(__file__).resolve().parents[1] / "docs" / "DESIGN.md"

# Rule 1, verbatim from §1.4 (case-insensitive, whole word).
FORBIDDEN = ("make", "move", "delete", "duplicate", "schedule", "empty", "log", "show", "edit", "activate",
             "launch", "reopen", "quit", "close", "print", "save", "copy", "run script", "load script",
             "store script", "do shell script", "open location", "open for access", "display", "choose",
             "quicksilver", "contact", "_private", "quick entry", "filter by", "application responses",
             "System Events", "keystroke", "click")
EXTRA_FORBIDDEN = ("read", "write", "file", "alias", "POSIX", "path to", "info for", "list folder",
                   "list disks", "mount volume", "clipboard", "system attribute", "do JavaScript",
                   "using terms from", "machine", "say", "beep")

# Rule 2, verbatim from §1.4.
SET_OK = re.compile(r"^(v_\w+|g_\w+|end of v_\w+|AppleScript's text item delimiters|(time|day|month|year) of v_d)$")
DATE_PART = re.compile(r"^(time|day|month|year) of v_d$")
SET_RE = re.compile(r"set\s+(.+?)\s+to\b", re.I)          # shortest target: `set v_cand to to do id …`

# Rule 3.
TELL_BLOCK = re.compile(r'(?i:tell\s+application\s+id\s+)"com\.culturedcode\.ThingsMac"')
TELL_TO = re.compile(r'(?i:tell\s+application\s+id\s+)"com\.culturedcode\.ThingsMac"(?i:\s+to\s+)\S')
APP_BEFORE = re.compile(r"\b(tell|running\s+of|version\s+of)\s+$", re.I)
APP_AFTER = re.compile(r'(?i:\s+id\s+)"com\.culturedcode\.ThingsMac"(?!(?i:\s+of)\b)')
LOCAL_ONLY = re.compile(r"\bcurrent\s+date\b|\btime\s+to\s+GMT\b|\btb_parseDay\b", re.I)

# Rule 4.
BLOCK_KINDS = ("tell", "if", "repeat", "try", "timeout", "considering", "ignoring")

# Rule 5.
ARGV_OK = re.compile(r"\bmy\s+tb_arg\(argv,\s*(?:[0-9]+|[A-Za-z]\w*)\)|\bcount\s+of\s+argv\b")

# Rule 6: `repeat with x in {v_a, v_b}` over a literal list of variables. When those are lists, `contents of x`
# is "contents of item 1 of {{…}}", which Things rejects with -1708 (seen on Things 3.24.1 / macOS 26).
REPEAT_LITERAL = re.compile(r"\brepeat\s+with\s+\w+\s+in\s+\{[^}]*\b[vg]_\w+", re.I)


@dataclass(frozen=True)
class Violation:
    line: int
    rule: str
    detail: str


def _word(token: str) -> re.Pattern:
    if token.startswith("_"):                                    # `_private…` is a prefix
        return re.compile(re.escape(token), re.I)
    return re.compile(r"(?<!\w)" + r"\s+".join(map(re.escape, token.split())) + r"(?!\w)", re.I)


WORD_RES = [(t, _word(t)) for t in FORBIDDEN + EXTRA_FORBIDDEN]


def strip_line(line: str) -> tuple[str, list[str]]:
    """The code part of one line: ``--`` comment dropped, string literals replaced by ``""`` except the
    Things bundle id. Returns ``(code, lexical problems)``."""
    out: list[str] = []
    problems: list[str] = []
    i = 0
    while i < len(line):
        c = line[i]
        if c == '"':
            j, buf = i + 1, []
            while j < len(line) and line[j] != '"':
                step = 2 if line[j] == "\\" else 1
                buf.append(line[j:j + step])
                j += step
            if j >= len(line):
                problems.append("unterminated string literal")
                break
            out.append(f'"{THINGS}"' if "".join(buf) == THINGS else '""')
            i = j + 1
            continue
        if line.startswith("--", i):
            break
        if line.startswith("(*", i) or line.startswith("*)", i):
            problems.append("block comment")
        elif c in "#|\\":
            problems.append(f"{c!r} outside a string literal")
        elif not (c == "\t" or " " <= c <= "~"):
            problems.append(f"non-ASCII {c!r} outside a string literal")
        out.append(c)
        i += 1
    return "".join(out), problems


def lint(source: str, *, template: bool) -> list[Violation]:
    """All §1.4 violations in ``source`` (a template body/full script if ``template``, else the library)."""
    found: list[Violation] = []
    stack: list[tuple[str, str | None]] = []         # (block kind, handler name)
    runs = 0

    def bad(ln: int, rule: str, detail: str) -> None:
        found.append(Violation(ln, rule, detail))

    for ln, raw in enumerate(source.split("\n"), 1):
        code, problems = strip_line(raw)
        for p in problems:
            bad(ln, "lexical", p)
        s = code.strip()
        if not s:
            continue
        in_tell = any(kind == "tell" for kind, _ in stack)
        handler = next((name for kind, name in reversed(stack) if kind == "handler"), None)

        # 1. forbidden tokens; `open` / `run` only as the status enumerator / the run handler
        for token, rx in WORD_RES:
            if rx.search(s):
                bad(ln, "forbidden", token)
        for m in re.finditer(r"\bopen\b", s, re.I):
            if not re.search(r"\bis\s+(not\s+)?$", s[:m.start()], re.I):
                bad(ln, "forbidden", "open (only `is open` is allowed)")
        if re.search(r"\brun\b", s, re.I) and not re.fullmatch(r"(on\s+run\s+argv|end\s+run)", s, re.I):
            bad(ln, "forbidden", "run (only the run handler is allowed)")

        # 2. set targets
        for m in re.finditer(r"\bset\b", s, re.I):
            t = SET_RE.match(s, m.start())
            if not t:
                bad(ln, "set", "set without `<target> to`")
            elif not SET_OK.match(t.group(1)):
                bad(ln, "set", f"target {t.group(1)!r}")
            elif DATE_PART.match(t.group(1)) and handler != "tb_parseDay":
                bad(ln, "set", f"target {t.group(1)!r} outside tb_parseDay")

        # 3. tell targets and application specifiers
        for m in re.finditer(r"\btell\b", s, re.I):
            if re.fullmatch(r"end\s+tell", s, re.I):
                continue
            if m.start() != 0 or not (TELL_BLOCK.fullmatch(s) or TELL_TO.match(s)):
                bad(ln, "tell", "tell target must be application id \"com.culturedcode.ThingsMac\"")
        for m in re.finditer(r"\b(application|app)\b", s, re.I):
            if not (APP_BEFORE.search(s[:m.start()]) and APP_AFTER.match(s, m.end())):
                bad(ln, "application", "only tell/running of/version of application id Things")
        if (in_tell or TELL_TO.match(s)) and LOCAL_ONLY.search(s):
            bad(ln, "local", "current date / time to GMT / tb_parseDay inside a tell")

        # 4. structure
        end = re.fullmatch(r"end(?:\s+(\w+))?", s, re.I)
        if re.match(r"on\s+error\b", s, re.I):
            if not stack or stack[-1][0] != "try":
                bad(ln, "structure", "on error outside try")
        elif re.match(r"on\s+\w+", s, re.I):
            if handler is not None or stack:
                bad(ln, "structure", "nested handler")
            stack.append(("handler", re.match(r"on\s+(\w+)", s, re.I).group(1)))
        elif end:
            kind = end.group(1)
            if kind is None:
                bad(ln, "structure", "bare `end`")
            elif not stack:
                bad(ln, "structure", f"end {kind} without an opener")
            else:
                top_kind, top_name = stack.pop()
                want = kind.lower() if kind.lower() in BLOCK_KINDS else "handler"
                if top_kind != want or (want == "handler" and top_name != kind):
                    bad(ln, "structure", f"end {kind} closes {top_kind} {top_name or ''}".rstrip())
        elif re.match(r"tell\b", s, re.I):
            if not re.search(r"\sto\s", s, re.I):          # block form; a bad target is reported above
                stack.append(("tell", None))
        elif re.match(r"if\b", s, re.I):
            if re.search(r"\bthen$", s, re.I):
                stack.append(("if", None))
            elif not re.search(r"\bthen\b", s, re.I):
                bad(ln, "structure", "if without then")
        elif re.match(r"else\b", s, re.I):
            if not re.fullmatch(r"else(\s+if\b.*\bthen)?", s, re.I):
                bad(ln, "structure", "malformed else")
            elif not stack or stack[-1][0] != "if":
                bad(ln, "structure", "else outside if")
        elif re.match(r"repeat\b", s, re.I):
            stack.append(("repeat", None))
        elif re.fullmatch(r"try", s, re.I):
            stack.append(("try", None))
        elif re.fullmatch(r"with\s+timeout\s+(of\s+)?\S+\s+seconds?", s, re.I):
            stack.append(("timeout", None))
        elif re.match(r"considering\b", s, re.I):
            stack.append(("considering", None))
        elif re.match(r"ignoring\b", s, re.I):
            stack.append(("ignoring", None))
        elif re.match(r"(with|using|script|to)\b", s, re.I):
            bad(ln, "structure", f"unsupported block {s.split(' ', 1)[0]!r}")

        # 5. on run argv; argv read only through my tb_arg(argv, n)
        if re.match(r"on\s+run\b", s, re.I):
            runs += 1
            if s != "on run argv":
                bad(ln, "argv", "run handler must be exactly `on run argv`")
        rest = "" if s == "on run argv" else ARGV_OK.sub("", s)
        if re.search(r"\bargv\b", rest, re.I):
            bad(ln, "argv", "argv read other than through `my tb_arg(argv, n)` / `count of argv`")

        # 6. no repeat over a literal list of variables (nested lists break `contents of`, -1708)
        if REPEAT_LITERAL.search(s):
            bad(ln, "repeat", "repeat over a literal list of variables; iterate a named list or test each variable")

    for kind, name in stack:
        bad(0, "structure", f"unclosed {kind} {name or ''}".rstrip())
    if template and runs != 1:
        bad(0, "argv", f"{runs} run handlers (want exactly one `on run argv`)")
    if not template and runs:
        bad(0, "argv", "the library must not have a run handler")
    return found


def rules(source: str, *, template: bool = True) -> set[str]:
    return {v.rule for v in lint(source, template=template)}


def tokens(source: str, *, template: bool = True) -> set[str]:
    return {v.detail for v in lint(source, template=template) if v.rule == "forbidden"}


def run_wrap(*lines: str) -> str:
    return "\n".join(["on run argv", *lines, "end run"])


def tell_wrap(*lines: str) -> str:
    return run_wrap(f'\ttell application id "{THINGS}"', *("\t\t" + ln for ln in lines), "\tend tell")


def design_blocks() -> list[str]:
    return re.findall(r"```applescript\n(.*?)```", DESIGN.read_text(encoding="utf-8"), re.S)


# ---- the shipped scripts are clean ------------------------------------------------------------------

def test_library_is_clean():
    assert lint(LIBRARY, template=False) == []


@pytest.mark.parametrize("name", list(TEMPLATES))
def test_template_body_is_clean(name):
    assert lint(TEMPLATES[name].body, template=True) == []


@pytest.mark.parametrize("name", list(TEMPLATES))
def test_full_script_is_clean(name):
    """What osascript actually receives: library + body as one program."""
    assert lint(script_source(name), template=True) == []


def test_registry_is_the_designed_set():
    assert list(TEMPLATES) == ["SCAN", "ROWS", "LOGBOOK", "PROJECT_AREAS", "AREAS", "TAGS", "MEMBERSHIP",
                               "FIND_CREATED", "PROBE", "AUTOMATION", "APP_VERSION"]
    assert all(isinstance(t, Template) and t.name == key for key, t in TEMPLATES.items())


@pytest.mark.skipif(not DESIGN.exists(), reason="docs/DESIGN.md not shipped")
def test_scripts_are_verbatim_from_design():
    """§4.2/§4.3 were linted in the design; the code must be exactly those blocks, in order."""
    blocks = design_blocks()
    assert len(blocks) == 12
    assert blocks[0] == LIBRARY
    assert blocks[1:] == [t.body for t in TEMPLATES.values()]


def test_only_app_version_skips_the_library():
    assert [n for n, t in TEMPLATES.items() if not t.uses_library] == ["APP_VERSION"]
    body = TEMPLATES["APP_VERSION"].body
    assert "tell" not in body and "tb_" not in body     # no Things terminology: compiles without Things


@pytest.mark.parametrize("name", [n for n, t in TEMPLATES.items() if t.uses_library])
def test_templates_start_with_init_and_running_guard(name):
    """No Apple Event may reach a closed Things (it would launch frontmost, TAPI §5a)."""
    lines = [strip_line(ln)[0].strip() for ln in TEMPLATES[name].body.split("\n")]
    code = [ln for ln in lines if ln]
    assert code[:3] == ["on run argv", "my tb_init()", "my tb_assertRunning()"]


def test_every_handler_call_resolves_to_the_library():
    defined = set(re.findall(r"^on (tb_\w+)\(", LIBRARY, re.M))
    assert "tb_arg" in defined and "tb_assertRunning" in defined
    for name, t in TEMPLATES.items():
        called = set(re.findall(r"\bmy (\w+)\(", t.body))
        assert called <= defined, (name, called - defined)
    assert set(re.findall(r"\bmy (\w+)\(", LIBRARY)) <= defined


@pytest.mark.parametrize("name", list(TEMPLATES))
def test_argv_use_matches_arg_limits(name):
    t = TEMPLATES[name]
    body = t.body
    literal = [int(n) for n in re.findall(r"my tb_arg\(argv, ([0-9]+)\)", body)]
    if t.max_args == 0:
        assert not re.search(r"\bargv\b", body.replace("on run argv", "")), name
    else:
        assert literal or "count of argv" in body
        assert all(1 <= n <= (t.max_args if t.max_args is not None else n) for n in literal), (name, literal)


@pytest.mark.parametrize("name", list(TEMPLATES))
def test_header_name_and_record_tags_match_parser_tables(name):
    body = TEMPLATES[name].body
    assert f'"{HEADER_NAMES[name]}"' in body
    emitted = set(re.findall(r'tb_rec\(\{"([A-Z]+)"', body)) | set(re.findall(r'"([A-Z]+)" & v_us', body))
    if "tb_rowFor" in body:
        emitted.add("T")
    if "tb_containerRows" in body:
        emitted.add("R")
    assert {"H", "E"} <= emitted
    assert emitted - {"H", "E"} == RECORD_TAGS[name]


def _blank(source: str) -> str:
    """Same-length copy with string contents and comments blanked, so offsets line up with ``source``."""
    out = []
    for ln in source.split("\n"):
        ln = re.sub(r'"(?:[^"\\]|\\.)*"', lambda m: '"' + "x" * (len(m.group(0)) - 2) + '"', ln)
        i = ln.find("--")
        out.append(ln if i < 0 else ln[:i] + " " * (len(ln) - i))
    return "\n".join(out)


def _record_literals(source: str) -> list[tuple[str, int]]:
    """``(tag, field count)`` for every ``tb_rec({"X", …})`` list literal in code."""
    blank = _blank(source)
    out = []
    for m in re.finditer(r'tb_rec\(\{"([A-Z]+)"', source):
        if not blank.startswith("tb_rec({", m.start()):
            continue                                    # inside a comment or string
        depth, i, fields = 2, m.start() + len("tb_rec({"), 1
        while depth > 1:
            c = blank[i]
            if c in "({":
                depth += 1
            elif c in ")}":
                depth -= 1
            elif c == "," and depth == 2:
                fields += 1
            i += 1
        out.append((m.group(1), fields))
    return out


def test_record_arities_match_parser_table():
    """Every fixed-arity record the AppleScript builds has exactly ``ARITY[tag]`` fields."""
    records = _record_literals(LIBRARY) + [r for t in TEMPLATES.values() for r in _record_literals(t.body)]
    checked = {tag for tag, _ in records if tag in ARITY}
    assert checked >= {"S", "T", "A", "G", "R", "PA", "N", "D", "X", "E"}
    for tag, n in records:
        if tag in ARITY:
            assert n == ARITY[tag], (tag, n)


# ---- the linter itself --------------------------------------------------------------------------------

def test_shortest_set_target_for_to_do():
    code = tell_wrap("set v_cand to to do id v_id")
    assert lint(code, template=True) == []
    assert SET_RE.match("set v_cand to to do id v_id").group(1) == "v_cand"


@pytest.mark.parametrize("source", [
    tell_wrap('set v_s to "do shell script, delete, make, tell application \\"Finder\\" -- x"'),
    tell_wrap("set v_x to 1 -- delete everything; do shell script; tell application \"Finder\""),
    tell_wrap("if v_s is open then return \"open\"", "if v_s is not open then return \"x\""),
    run_wrap("repeat with i from 1 to (count of argv)", "set v_k to my tb_arg(argv, i)", "end repeat"),
    run_wrap("set AppleScript's text item delimiters to {g_US, g_RS}", "set end of v_out to \"x\""),
    run_wrap(f'tell application id "{THINGS}" to set v_n to count of areas'),
    run_wrap(f'if not (running of application id "{THINGS}") then error "x" number 9001'),
    run_wrap(f'set v_v to version of application id "{THINGS}"'),
    run_wrap("considering case", "set v_same to (v_a is v_b)", "end considering"),
    run_wrap("try", "set v_x to 1", "on error v_msg number v_num", "set v_x to 2", "end try"),
    run_wrap("set v_d to current date", "set v_g to (time to GMT) as text"),
    run_wrap('repeat with v_try in {"to-do", "project", "area"}', "set v_t to contents of v_try", "end repeat"),
    run_wrap("if (count of v_names) is not v_n or (count of v_tags) is not v_n then error \"x\" number 9101"),
])
def test_good_snippets_pass(source):
    assert lint(source, template=True) == []


def test_date_parts_only_in_tb_parse_day():
    ok = "on tb_parseDay(v_s)\n\tset v_d to current date\n\tset time of v_d to 0\n\treturn v_d\nend tb_parseDay"
    assert lint(ok, template=False) == []
    assert rules(ok.replace("tb_parseDay", "tb_other"), template=False) == {"set"}


@pytest.mark.parametrize("source, expected", [
    # the design's deliberately broken sample (§1.4 rule 2 note)
    (tell_wrap("set name of v_x to \"a\""), {"set"}),
    (tell_wrap("set name of to do id v_id to \"x\""), {"set"}),
    (run_wrap(f'tell application id "{THINGS}" to set name of to do id v_id to "x"'), {"set"}),
    (tell_wrap("delete to do id v_id"), {"forbidden"}),
    (run_wrap('do shell script "ls"'), {"forbidden"}),
    (run_wrap('tell application "Finder"', "end tell"), {"tell", "application"}),
    (run_wrap('tell application id "com.apple.finder"', "end tell"), {"tell", "application"}),
    (run_wrap('tell app "System Events" to keystroke "q"'), {"tell", "application", "forbidden"}),
    (run_wrap("set v_x to current application"), {"application"}),
    (run_wrap(f'tell application id "{THINGS}"'), {"structure"}),
    (run_wrap("if v_x then", "end repeat"), {"structure"}),
    (run_wrap("try", "set v_x to 1", "end tell"), {"structure"}),
    (run_wrap("on error v_m number v_n"), {"structure"}),
    (run_wrap("set v_a to item 1 of argv"), {"argv"}),
    (run_wrap("set v_a to tb_arg(argv, 1)"), {"argv"}),
    (run_wrap("set v_a to my tb_arg(argv, 1) & (argv as text)"), {"argv"}),
    (run_wrap("set time of v_d to 0"), {"set"}),
    (run_wrap("set v_x"), {"set"}),
    (run_wrap("set {v_a, v_b} to {1, 2}"), {"set"}),
    (run_wrap("repeat with v_col in {v_names, v_tags}", "set v_n to count of (contents of v_col)", "end repeat"),
     {"repeat"}),
])
def test_bad_snippets_are_flagged(source, expected):
    assert rules(source) == expected


@pytest.mark.parametrize("line, token", [
    ('make new to do with properties {name:"x"}', "make"),
    ('move to do id v_id to list id "x"', "move"),
    ("duplicate to do id v_id", "duplicate"),
    ("schedule to do id v_id for v_day", "schedule"),
    ("empty trash", "empty"),
    ("log completed now", "log"),
    ("show to do id v_id", "show"),
    ("edit to do id v_id", "edit"),
    ("activate", "activate"),
    ("launch", "launch"),
    ("reopen", "reopen"),
    ("quit", "quit"),
    ("close every window", "close"),
    ("print to do id v_id", "print"),
    ("save", "save"),
    ("copy v_a to v_b", "copy"),
    ("run script v_x", "run script"),
    ("load script v_x", "load script"),
    ("store script v_x in v_y", "store script"),
    ('do  shell   script "x"', "do shell script"),
    ('open location "x"', "open location"),
    ("open for access v_f", "open for access"),
    ('display dialog "hi"', "display"),
    ("set v_f to choose from list {1}", "choose"),
    ('set v_x to parse quicksilver input "x"', "quicksilver"),
    ('add contact named "x"', "contact"),
    ('set v_x to _private_experimental_json_import "x"', "_private"),
    ("show quick entry panel", "quick entry"),
    ('filter by tag "x"', "filter by"),
    ("keystroke v_k", "keystroke"),
    ("click v_b", "click"),
    ('set v_x to read POSIX file v_p', "read"),
    ('set v_x to read POSIX file v_p', "POSIX"),
    ('set v_x to read POSIX file v_p', "file"),
    ("write v_x to v_f", "write"),
    ("set v_x to alias v_p", "alias"),
    ("set v_x to path to desktop", "path to"),
    ("set v_x to info for v_p", "info for"),
    ("set v_x to list folder v_p", "list folder"),
    ("set v_x to list disks", "list disks"),
    ("mount volume v_x", "mount volume"),
    ("set v_c to the clipboard", "clipboard"),
    ('set v_t to system attribute "THINGS_AUTH_TOKEN"', "system attribute"),
    ('do JavaScript "x"', "do JavaScript"),
    ("say v_x", "say"),
    ("beep", "beep"),
])
def test_forbidden_tokens_are_flagged(line, token):
    assert token in tokens(tell_wrap(line))


def test_forbidden_tokens_are_case_insensitive_and_whole_word():
    assert "delete" in tokens(tell_wrap("DELETE to do id v_id"))
    assert "System Events" in tokens(run_wrap("set v_x to system   events"))
    assert tokens(tell_wrap("set v_x to id of every to do of list id \"TMLogbookListSource\"")) == set()
    assert tokens(run_wrap("set v_logbook to 1", "set v_showing to 2", "set v_made to 3")) == set()


@pytest.mark.parametrize("line", [
    "open v_x",                         # an Apple Event other than get/count/exists (N3)
    "set v_x to open",
    "run",
    "launch",
])
def test_open_and_run_only_as_enumerator_and_handler(line):
    assert "forbidden" in rules(tell_wrap(line))


@pytest.mark.parametrize("source", [
    run_wrap('«event sysoexec» "ls"'),
    run_wrap('(* do shell script "x" *)'),
    run_wrap("# comment"),
    run_wrap("set |v_x| to 1"),
    run_wrap('set v_a to |"| & do shell script "x" & |"|'),    # a quoted identifier hiding code
    run_wrap("set v_x to 1 ¬"),
    run_wrap("set v_x to “q”"),
    run_wrap('set v_x to "unterminated'),
    run_wrap('set v_x to "a\\'),
])
def test_unmodelled_syntax_is_flagged(source):
    assert "lexical" in rules(source)


@pytest.mark.parametrize("source", [
    run_wrap(f'tell application id "{THINGS}" of machine "eppc://h"', "end tell"),
    run_wrap(f'tell application id "{THINGS.lower()}"', "end tell"),
    run_wrap("tell v_ref", "end tell"),
    run_wrap(f'if v_x then tell application id "{THINGS}" to set v_y to 1'),
    run_wrap(f'set v_x to name of application id "{THINGS}"'),
])
def test_tell_and_application_targets(source):
    assert rules(source) & {"tell", "application"}


@pytest.mark.parametrize("source", [
    run_wrap(f'tell application id "{THINGS}" to set v_d to current date'),
    tell_wrap("set v_d to current date"),
    tell_wrap("set v_g to (time to GMT) as text"),
    tell_wrap('set v_d to my tb_parseDay("2026-01-01")'),
])
def test_local_work_outside_tell(source):
    assert "local" in rules(source)


@pytest.mark.parametrize("source", [
    run_wrap("set v_x to 1", "end"),
    run_wrap("if v_x", "end if"),
    run_wrap("with transaction", "end transaction"),
    run_wrap("else"),
    run_wrap("repeat", "end try"),
    run_wrap("on tb_inner()", "end tb_inner"),
    "on run argv\nend tb_other",
])
def test_structure_errors(source):
    assert "structure" in rules(source)


def test_run_handler_count():
    assert rules("set v_x to 1") == {"argv"}                                  # no run handler
    two = run_wrap("set v_x to 1") + "\n" + run_wrap("set v_y to 1")
    assert "argv" in rules(two)
    assert "argv" in rules("on run\nend run")
    assert rules(run_wrap("set v_x to 1"), template=False) == {"argv"}      # library: none allowed
    assert lint(LIBRARY + "\n" + TEMPLATES["AREAS"].body, template=False) != []


def test_linter_rejects_a_mutated_template():
    """Sanity: small edits to a real template are caught."""
    body = TEMPLATES["ROWS"].body
    assert rules(body.replace("get id of v_cand", "delete v_cand")) == {"forbidden"}
    assert rules(body.replace("set v_ref to v_cand", "set name of v_cand to v_id")) == {"set"}
    assert rules(body.replace("my tb_arg(argv, i)", "item i of argv")) == {"argv"}
    assert rules(body.replace("\t\tend tell\n\t\tif v_kind", "\t\tif v_kind", 1)) == {"structure"}
    assert "application" in rules(body.replace(THINGS, "com.apple.finder", 1))


def test_row_kind_does_not_come_from_class_of():
    """On Things 3.24.1 a project fetched with `to do id` has class `selected to do`, so `class of … is project`
    called every project a to-do (ST-1 then couldn't find its own project). tb_rowFor asks `exists project id`."""
    assert re.search(r"\(class of v_ref\) is project", LIBRARY) is None
    assert "if (exists project id v_id) then set v_kind to \"project\"" in LIBRARY
