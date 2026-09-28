"""Static package rules (DESIGN §1.3 enforcement map, §1.4 "tests/test_safety_static.py"), over the source of
``src/things_bridge/**/*.py``.

* Forbidden strings anywhere outside comments (case-insensitive).
* AST import allowlist: the §1.4 stdlib list; ``mcp``/``pydantic`` only in mcp_server.py; ``subprocess`` only in
  runner.py and authtoken.py. Two additions the design itself requires, each only in the module that needs it:
  ``types`` (``MappingProxyType`` for §2.3 ``PARAMS`` and §2.5 ``TEMPLATES``) and ``importlib.metadata`` (§2.10
  item 10, the mcp version in doctor.py).
* ``/usr/bin/osascript`` and ``/usr/bin/open`` only in runner.py, ``/usr/bin/security`` only in authtoken.py.
* The ``things:///`` literal only in urlscheme.py; ``models.link_for`` builds through ``urlscheme.build_show``.
* No ``shell=True``/``os.system``; no ``print`` outside cli.py; the URL command allowlist is frozen; service.py
  never uses the excluded parameters (N13).

Each rule is a checker over (module name, source); the checkers are tested against bad snippets too, so a rule
can't pass by being broken.
"""

import ast
import io
import tokenize
from pathlib import Path
from types import MappingProxyType

import pytest

import things_bridge
from things_bridge import applescript, models, urlscheme

PKG = Path(__file__).resolve().parents[1] / "src" / "things_bridge"
MODULES = {p.relative_to(PKG).as_posix(): p for p in sorted(PKG.rglob("*.py"))}

FORBIDDEN = ("sqlite", "Group Containers", "Library/Containers", "JLMPQHK86H", "ThingsData", ".thingsdatabase",
             "main.sqlite", "things.py", "import things", "delete-generic-password", "shell=True", "os.system",
             "os.popen", "x-success", "x-error", "x-cancel", "add-json", "webbrowser", "socket", "urllib.request",
             "http.client")
STDLIB = frozenset({"argparse", "contextlib", "dataclasses", "datetime", "difflib", "enum", "errno", "fcntl",
                    "functools", "getpass", "json", "logging", "os", "pathlib", "platform", "re", "subprocess", "sys",
                    "threading", "time", "typing", "unicodedata", "urllib.parse"})
ONLY_IN = {                                 # module -> the only files that may import it
    "subprocess": {"runner.py", "authtoken.py"},
    "mcp": {"mcp_server.py"},
    "pydantic": {"mcp_server.py"},
    "types": {"urlscheme.py", "applescript.py"},       # MappingProxyType (§2.3 PARAMS, §2.5 TEMPLATES)
    "importlib.metadata": {"doctor.py"},               # §2.10 item 10
}
THIRD_PARTY = ("mcp", "pydantic")
PATHS = {                                   # literal -> the only file that may contain it
    "/usr/bin/osascript": "runner.py",
    "/usr/bin/open": "runner.py",
    "/usr/bin/pgrep": "runner.py",          # §2.6: runner.py is the only launcher (not in §1.4's list)
    "/usr/bin/security": "authtoken.py",
}
URL_LITERAL = "things:///"
EXCLUDED_KEYWORDS = ("duplicate", "use_clipboard", "show_quick_entry", "creation_date", "completion_date")
BUILDERS = {"urlscheme.py", "jsoncmd.py"}   # they support N13's parameters only to match the doc
PROCESS_APIS = ("system", "popen", "spawn", "exec", "fork", "posix_spawn", "startfile")
OS_MODULES = frozenset({"os", "posix", "nt"})
TOKEN_ENV = "THINGS_AUTH_TOKEN"


# ---- source helpers ------------------------------------------------------------------------------------------

def read(name: str) -> str:
    return MODULES[name].read_text(encoding="utf-8")


def without_comments(src: str) -> str:
    """The source with every ``#`` comment blanked out (strings and docstrings kept)."""
    lines = src.splitlines(keepends=True)
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            (row, col), (_, end) = tok.start, tok.end
            line = lines[row - 1]
            lines[row - 1] = line[:col] + " " * (end - col) + line[end:]
    return "".join(lines)


def lineno(src: str, needle: str) -> int:
    return src[:src.lower().index(needle.lower())].count("\n") + 1


def imports(tree: ast.AST) -> list[tuple[int, str, int]]:
    """(line, dotted module, relative level) for every import statement, lazy ones included."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(node.lineno, alias.name, 0) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            out.append((node.lineno, node.module or "", node.level))
    return out


def dotted(node: ast.AST) -> str | None:
    """``a.b.c`` for a Name/Attribute chain, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return ".".join([node.id, *reversed(parts)])
    return None


# ---- checkers: (module name, source) -> violations -----------------------------------------------------------

def forbidden_strings(name: str, src: str) -> list[str]:
    code = without_comments(src).lower()
    return [f"{name}:{lineno(code, s)}: {s!r}" for s in FORBIDDEN if s.lower() in code]


def import_violations(name: str, src: str) -> list[str]:
    bad = []
    for line, module, level in imports(ast.parse(src)):
        where = f"{name}:{line}: "
        if level:
            if level != 1:
                bad.append(where + f"relative import beyond the package ({'.' * level}{module})")
            continue
        root = module.split(".")[0]
        if root.lower() in ("things", "things3", "sqlite3"):
            bad.append(where + f"{module} (N1)")
        elif module in ONLY_IN or root in THIRD_PARTY:
            allowed = ONLY_IN.get(module, ONLY_IN.get(root, set()))
            if name not in allowed:
                bad.append(where + f"{module} may only be imported in {', '.join(sorted(allowed))}")
        elif module not in STDLIB:
            bad.append(where + f"{module} is not on the import allowlist")
    return bad


def dynamic_code(name: str, src: str) -> list[str]:
    """Imports and code the AST allowlist can't see: ``__import__``, ``import_module``, ``exec``, ``eval``."""
    bad = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Name) and node.id in ("__import__", "exec", "eval"):
            bad.append(f"{name}:{node.lineno}: {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in ("import_module", "__import__"):
            bad.append(f"{name}:{node.lineno}: {node.attr}")
    return bad


def process_apis(name: str, src: str) -> list[str]:
    """``subprocess`` outside runner.py/authtoken.py (by name, even without an import), and os-level process
    calls anywhere, however they are spelled: ``os.system``, ``import os as o; o.system``,
    ``from os import system``/``*``, and ``getattr(os, <anything but a harmless constant>)``."""
    bad = []
    tree = ast.parse(src)
    os_names = {"os", "posix", "nt"}                    # module names and their aliases
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in OS_MODULES and alias.asname:
                    os_names.add(alias.asname)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module in OS_MODULES:
            for alias in node.names:
                if alias.name == "*" or alias.name.startswith(PROCESS_APIS):
                    bad.append(f"{name}:{node.lineno}: from {node.module} import {alias.name}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" \
                and node.args and isinstance(node.args[0], ast.Name) \
                and (node.args[0].id in os_names or node.args[0].id == "subprocess"):
            attr = node.args[1] if len(node.args) > 1 else None
            if not (isinstance(attr, ast.Constant) and isinstance(attr.value, str)
                    and not attr.value.startswith(PROCESS_APIS) and node.args[0].id != "subprocess"):
                bad.append(f"{name}:{node.lineno}: {ast.unparse(node)}")
            continue
        path = dotted(node) if isinstance(node, ast.Attribute) else None
        if path is None:
            continue
        head, _, attr = path.partition(".")
        if head == "subprocess" and name not in ONLY_IN["subprocess"]:
            bad.append(f"{name}:{node.lineno}: {path}")
        if head in os_names and attr.startswith(PROCESS_APIS):
            bad.append(f"{name}:{node.lineno}: {path}")
    return bad


def path_literals(name: str, src: str) -> list[str]:
    return [f"{name}:{lineno(src, lit)}: {lit} belongs in {home} only" for lit, home in PATHS.items()
            if lit in src and name != home]


def url_literals(name: str, src: str) -> list[str]:
    """``things:///`` anywhere (comments too), plus string constants that would assemble a Things URL."""
    if name == "urlscheme.py":
        return []
    bad = [f"{name}:{lineno(src, URL_LITERAL)}: {URL_LITERAL}"] if URL_LITERAL in src.lower() else []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "things:/" in node.value.lower():
            bad.append(f"{name}:{node.lineno}: a string constant holds a Things URL")
    return bad


def shell_true(name: str, src: str) -> list[str]:
    """Any ``shell=`` keyword other than a literal False (``shell = True``, ``shell=flag``, ``**{"shell": …}``)."""
    bad = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.keyword) and node.arg == "shell":
            if not (isinstance(node.value, ast.Constant) and node.value.value is False):
                bad.append(f"{name}:{node.value.lineno}: shell={ast.unparse(node.value)}")
        elif isinstance(node, ast.Dict):
            if any(isinstance(k, ast.Constant) and k.value == "shell" for k in node.keys):
                bad.append(f"{name}:{node.lineno}: a 'shell' key")
    return bad


def prints(name: str, src: str) -> list[str]:
    """``print`` (called or passed around) outside cli.py, and stdout writes/handlers outside cli.py (N14)."""
    if name == "cli.py":
        return []
    bad = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Name) and node.id == "print":
            bad.append(f"{name}:{node.lineno}: print")
        elif isinstance(node, ast.Attribute):
            path = dotted(node) or ""
            if path.startswith(("sys.stdout.", "sys.__stdout__.")) and \
                    path.split(".")[2] in ("write", "writelines", "buffer", "fileno"):
                bad.append(f"{name}:{node.lineno}: {path}")
        elif isinstance(node, ast.Call):
            args = [*node.args, *(k.value for k in node.keywords)]
            if any(dotted(a) in ("sys.stdout", "sys.__stdout__") for a in args):
                bad.append(f"{name}:{node.lineno}: stdout passed to {ast.unparse(node.func)}()")
    return bad


def excluded_params(name: str, src: str) -> list[str]:
    """N13: the builders' duplicate/use-clipboard/show-quick-entry/creation-date/completion-date never used as
    keywords above them. service.py is also grepped for ``<keyword>=`` (§1.3), comments included."""
    if name in BUILDERS:
        return []
    bad = [f"{name}:{node.lineno}: {node.arg}=" for node in ast.walk(ast.parse(src))
           if isinstance(node, ast.keyword) and node.arg in EXCLUDED_KEYWORDS]
    if name == "service.py":
        bad += [f"{name}:{lineno(src, k + '=')}: {k}=" for k in EXCLUDED_KEYWORDS if f"{k}=" in src]
    return bad


CHECKERS = {f.__name__: f for f in (forbidden_strings, import_violations, dynamic_code, process_apis, path_literals,
                                     url_literals, shell_true, prints, excluded_params)}


# ---- the package passes every rule ---------------------------------------------------------------------------

def test_the_walk_covers_the_package():
    expected = {"__init__.py", "__main__.py", "errors.py", "models.py", "urlscheme.py", "jsoncmd.py",
                "applescript.py", "runner.py", "authtoken.py", "verify.py", "service.py", "doctor.py", "selftest.py",
                "cli.py", "mcp_server.py"}
    assert expected <= set(MODULES), sorted(expected - set(MODULES))
    assert PKG.resolve() == Path(things_bridge.__file__).resolve().parent     # the code under test is this code


@pytest.mark.parametrize("name", sorted(MODULES))
def test_forbidden_strings(name):
    assert forbidden_strings(name, read(name)) == []


@pytest.mark.parametrize("name", sorted(MODULES))
def test_imports_allowlist(name):
    assert import_violations(name, read(name)) == []
    assert dynamic_code(name, read(name)) == []


@pytest.mark.parametrize("name", sorted(MODULES))
def test_subprocess_only_in_runner_and_authtoken(name):
    assert process_apis(name, read(name)) == []
    uses = any(m == "subprocess" for _, m, _ in imports(ast.parse(read(name))))
    assert uses == (name in ONLY_IN["subprocess"])


@pytest.mark.parametrize("name", sorted(MODULES))
def test_osascript_only_in_runner(name):
    assert path_literals(name, read(name)) == []


def test_binaries_are_where_they_belong():
    for lit, home in PATHS.items():
        assert lit in read(home), f"{lit} missing from {home}"


@pytest.mark.parametrize("name", sorted(MODULES))
def test_url_literal_only_in_urlscheme(name):
    assert url_literals(name, read(name)) == []


def test_url_literal_is_defined_in_urlscheme():
    assert urlscheme.SCHEME_PREFIX == URL_LITERAL and URL_LITERAL in read("urlscheme.py")


def test_link_for_builds_through_urlscheme():
    tree = ast.parse(read("models.py"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "link_for")
    calls = {dotted(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call)}
    assert "urlscheme.build_show" in calls
    id_ = "SyJEz273ceSkabUbciM73A"
    assert models.link_for(id_) == urlscheme.build_show(id=id_).url == f"{URL_LITERAL}show?id={id_}"


@pytest.mark.parametrize("name", sorted(MODULES))
def test_no_shell_true(name):
    assert shell_true(name, read(name)) == []


@pytest.mark.parametrize("name", sorted(MODULES))
def test_no_print_outside_cli(name):
    assert prints(name, read(name)) == []


@pytest.mark.parametrize("name", sorted(MODULES))
def test_service_never_uses_excluded_params(name):
    assert excluded_params(name, read(name)) == []


def test_service_text_never_mentions_excluded_params():
    src = read("service.py")
    for needle in ("duplicate=", "use_clipboard=", "show_quick_entry=", "creation_date=", "completion_date="):
        assert needle not in src


def test_token_env_is_read_only_by_authtoken_and_scrubbed_by_runner():
    """N8: THINGS_AUTH_TOKEN is read in authtoken.py; runner.py only strips it from child environments; the MCP
    server only registers it for redaction; no other module touches it."""
    readers = {name for name in MODULES if TOKEN_ENV in read(name) or "authtoken.ENV" in read(name)}
    assert readers <= {"authtoken.py", "runner.py", "mcp_server.py", "cli.py", "doctor.py"}, readers
    assert TOKEN_ENV in read("authtoken.py")


# ---- the URL command allowlist and the script registry are frozen ---------------------------------------------

def test_url_command_allowlist_is_frozen():
    assert urlscheme.ALLOWED_COMMANDS == ("add", "add-project", "update", "update-project", "show", "search",
                                          "version", "json")
    assert isinstance(urlscheme.ALLOWED_COMMANDS, tuple)
    assert isinstance(urlscheme.PARAMS, MappingProxyType)
    assert tuple(urlscheme.PARAMS) == urlscheme.ALLOWED_COMMANDS
    with pytest.raises(TypeError):
        urlscheme.PARAMS["add-json"] = ("data",)            # type: ignore[index]
    with pytest.raises(TypeError):
        del urlscheme.PARAMS["add"]                          # type: ignore[attr-defined]
    for command, keys in urlscheme.PARAMS.items():
        assert isinstance(keys, tuple) and len(set(keys)) == len(keys), command
        assert not [k for k in keys if k.lower().startswith("x-")], command
        assert "auth-token" not in keys, command             # the runner appends it, last
    assert not {"add-json", "delete", "trash", "empty-trash"} & set(urlscheme.ALLOWED_COMMANDS)
    assert urlscheme.MUTATING == frozenset({"add", "add-project", "update", "update-project", "json"})
    assert urlscheme.FOREGROUND == frozenset({"show", "search"})
    assert urlscheme.TOKEN_ALWAYS == frozenset({"update", "update-project"})
    for frozen in (urlscheme.MUTATING, urlscheme.FOREGROUND, urlscheme.TOKEN_ALWAYS):
        assert isinstance(frozen, frozenset) and frozen <= set(urlscheme.ALLOWED_COMMANDS)


def test_the_allowlist_has_one_definition():
    """No other module keeps its own copy of the command list (the runner re-checks against urlscheme's)."""
    for name in MODULES:
        if name == "urlscheme.py":
            continue
        for node in ast.walk(ast.parse(read(name))):
            if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
                values = {e.value for e in node.elts if isinstance(e, ast.Constant)}
                assert not {"add-project", "update-project", "json"} <= values, f"{name}:{node.lineno}"
    assert "urlscheme.ALLOWED_COMMANDS" in read("runner.py") and "urlscheme.PARAMS" in read("runner.py")


def test_script_registry_is_frozen():
    assert isinstance(applescript.TEMPLATES, MappingProxyType)
    assert set(applescript.TEMPLATES) == {"SCAN", "ROWS", "LOGBOOK", "PROJECT_AREAS", "AREAS", "TAGS", "MEMBERSHIP",
                                          "FIND_CREATED", "PROBE", "AUTOMATION", "APP_VERSION"}
    with pytest.raises(TypeError):
        applescript.TEMPLATES["RUN"] = applescript.TEMPLATES["AREAS"]      # type: ignore[index]


# ---- the checkers catch what they are for --------------------------------------------------------------------

BAD = [
    ("forbidden_strings", "x.py", "DB = '~/Library/Group Containers/JLMPQHK86H.com.culturedcode.ThingsMac'\n"),
    ("forbidden_strings", "x.py", 'import sqlite3\n'),
    ("forbidden_strings", "x.py", '"""Uses things.py."""\n'),
    ("forbidden_strings", "x.py", "from .service import ThingsService\n"),
    ("forbidden_strings", "x.py", "URL = 'things:///add?x-success=foo'\n"),
    ("forbidden_strings", "x.py", "os.system('ls')\n"),
    ("forbidden_strings", "x.py", "import socket\n"),
    ("import_violations", "x.py", "import sqlite3\n"),
    ("import_violations", "x.py", "import things\n"),
    ("import_violations", "x.py", "from urllib import request\n"),
    ("import_violations", "x.py", "import urllib.request\n"),
    ("import_violations", "x.py", "import shutil\n"),
    ("import_violations", "x.py", "import glob\n"),
    ("import_violations", "x.py", "import types\n"),
    ("import_violations", "service.py", "import subprocess\n"),
    ("import_violations", "cli.py", "import mcp\n"),
    ("import_violations", "service.py", "from pydantic import BaseModel\n"),
    ("import_violations", "cli.py", "import importlib.metadata\n"),
    ("import_violations", "x.py", "def f():\n    import webbrowser\n"),
    ("import_violations", "x.py", "from .. import outside\n"),
    ("dynamic_code", "x.py", "m = __import__('sqlite3')\n"),
    ("dynamic_code", "x.py", "import importlib\nm = importlib.import_module('x')\n"),
    ("dynamic_code", "x.py", "eval('1')\n"),
    ("process_apis", "service.py", "import os\nos.execv('/bin/sh', [])\n"),
    ("process_apis", "runner.py", "os.posix_spawn('/bin/sh', [], {})\n"),
    ("process_apis", "cli.py", "subprocess.run(['x'])\n"),
    ("process_apis", "service.py", "from os import system\nsystem('osascript -e x')\n"),
    ("process_apis", "service.py", "from os import execv, posix_spawn\nexecv('/bin/sh', [])\n"),
    ("process_apis", "service.py", "from posix import *\n"),
    ("process_apis", "service.py", "import os as o\no.system('x')\n"),
    ("process_apis", "service.py", "import os\ngetattr(os, 'sys' + 'tem')('x')\n"),
    ("process_apis", "service.py", "import os\ngetattr(os, 'popen')('x')\n"),
    ("process_apis", "cli.py", "import subprocess\ngetattr(subprocess, 'run')(['x'])\n"),
    ("path_literals", "service.py", "OSA = '/usr/bin/osascript'\n"),
    ("path_literals", "runner.py", "SEC = '/usr/bin/security'\n"),
    ("path_literals", "cli.py", "# runs /usr/bin/open\n"),
    ("url_literals", "service.py", "URL = 'things:///add'\n"),
    ("url_literals", "cli.py", "# e.g. things:///show?id=x\n"),
    ("url_literals", "models.py", "PREFIX = 'things:' '//' '/'\n"),
    ("shell_true", "runner.py", "subprocess.run(argv, shell = True)\n"),
    ("shell_true", "runner.py", "subprocess.run(argv, shell=flag)\n"),
    ("shell_true", "runner.py", "subprocess.run(argv, **{'shell': True})\n"),
    ("prints", "service.py", "print('hi')\n"),
    ("prints", "mcp_server.py", "out = print\n"),
    ("prints", "verify.py", "import sys\nsys.stdout.write('x')\n"),
    ("prints", "runner.py", "import sys\nsys.stdout.buffer.write(b'x')\n"),
    ("prints", "mcp_server.py", "import logging, sys\nlogging.basicConfig(stream=sys.stdout)\n"),
    ("prints", "mcp_server.py", "import logging, sys\nlogging.StreamHandler(sys.stdout)\n"),
    ("excluded_params", "service.py", "urlscheme.build_update(id=i, duplicate=True)\n"),
    ("excluded_params", "cli.py", "urlscheme.build_add(title=t, use_clipboard='replace-title')\n"),
    ("excluded_params", "mcp_server.py", "jsoncmd.Todo(title=t, creation_date=d)\n"),
    ("excluded_params", "service.py", "# never: show_quick_entry=True\n"),
]

GOOD = [
    ("forbidden_strings", "x.py", "# the deprecated add-json command is excluded on purpose\n"),
    ("import_violations", "urlscheme.py", "from types import MappingProxyType\n"),
    ("import_violations", "doctor.py", "import importlib.metadata\n"),
    ("import_violations", "runner.py", "import subprocess\nfrom urllib.parse import quote\n"),
    ("import_violations", "mcp_server.py", "from mcp.server.mcpserver import MCPServer\nimport pydantic\n"),
    ("import_violations", "x.py", "from . import urlscheme\nfrom .errors import Refused\n"),
    ("process_apis", "runner.py", "subprocess.run(['x'])\n"),
    ("process_apis", "service.py", "from os import environ, path\nimport os\ngetattr(os, 'sep')\n"),
    ("path_literals", "runner.py", "OPEN, OSASCRIPT = '/usr/bin/open', '/usr/bin/osascript'\n"),
    ("url_literals", "urlscheme.py", "SCHEME_PREFIX = 'things:///'\n"),
    ("url_literals", "cli.py", "HINT = 'In Things: Settings → General'\n"),
    ("shell_true", "runner.py", "subprocess.run(argv, shell=False)\n"),
    ("prints", "cli.py", "print('hi')\n"),
    ("prints", "mcp_server.py", "import sys\nstdouts = (sys.stdout, sys.__stdout__)\n"),
    ("excluded_params", "urlscheme.py", "def build_update(*, duplicate=None): ...\nbuild_update(duplicate=True)\n"),
]


@pytest.mark.parametrize("checker, name, src", BAD, ids=[f"{c}-{i}" for i, (c, _, _) in enumerate(BAD)])
def test_checkers_catch_violations(checker, name, src):
    assert CHECKERS[checker](name, src), src


@pytest.mark.parametrize("checker, name, src", GOOD, ids=[f"{c}-{i}" for i, (c, _, _) in enumerate(GOOD)])
def test_checkers_allow_legitimate_code(checker, name, src):
    assert CHECKERS[checker](name, src) == []
