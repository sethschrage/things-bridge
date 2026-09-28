"""Foundation checks: packaging, import hygiene, the Runner interface, and the shared fixtures."""

import dataclasses
import importlib.metadata
import os
import subprocess
import sys
import typing
from contextlib import contextmanager
from pathlib import Path

import pytest

import things_bridge
from things_bridge import runner
from things_bridge.runner import Dispatch, Runner

ROOT = Path(__file__).resolve().parents[1]


def test_version_matches_distribution():
    assert things_bridge.__version__ == "0.1.0"
    assert importlib.metadata.version("things-bridge") == things_bridge.__version__


def test_console_scripts_declared():
    eps = {ep.name: ep.value for ep in importlib.metadata.entry_points(group="console_scripts")
           if ep.value.startswith("things_bridge.")}
    assert eps == {"things-bridge": "things_bridge.cli:main", "things-bridge-mcp": "things_bridge.mcp_server:main"}


def test_pure_modules_are_stdlib_only():
    # models/errors/runner must never pull in mcp or pydantic (the CLI imports them eagerly).
    code = ("import sys, things_bridge, things_bridge.errors, things_bridge.models, things_bridge.runner; "
            "print(sorted(m for m in ('mcp', 'pydantic', 'pydantic_core') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"


def test_python_dash_m_runs_cli_main():
    proc = subprocess.run([sys.executable, "-m", "things_bridge", "--version"], capture_output=True,
                          text=True, timeout=60, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr


# ---- runner interface --------------------------------------------------------------------------

def test_runner_constants():
    assert (runner.OPEN, runner.OSASCRIPT, runner.PGREP) == ("/usr/bin/open", "/usr/bin/osascript", "/usr/bin/pgrep")
    assert runner.THINGS_PROCESS == "Things3"


def test_dispatch_fields():
    assert [f.name for f in dataclasses.fields(Dispatch)] == ["command", "url_redacted", "sent", "dry_run",
                                                              "foreground", "t0"]
    d = Dispatch(command="show", url_redacted="u", sent=False, dry_run=True, foreground=True, t0=0.0)
    assert dataclasses.asdict(d)["dry_run"] is True


RUNNER_METHODS = {
    "require_macos": ["self"],
    "things_running": ["self"],
    "ensure_running": ["self", "allow_launch"],
    "run_template": ["self", "name", "args", "timeout"],
    "dispatch": ["self", "turl", "dry_run"],
    "write_lock": ["self"],
    "wall": ["self"],
    "monotonic": ["self"],
    "sleep": ["self", "s"],
}


def test_runner_protocol_methods():
    import inspect
    members = {n for n, v in vars(Runner).items() if callable(v) and not n.startswith("_")}
    assert members == set(RUNNER_METHODS)
    for name, params in RUNNER_METHODS.items():
        sig = inspect.signature(getattr(Runner, name))
        assert list(sig.parameters) == params, name
    kw_only = inspect.Parameter.KEYWORD_ONLY
    assert inspect.signature(Runner.ensure_running).parameters["allow_launch"].kind == kw_only
    assert inspect.signature(Runner.run_template).parameters["timeout"].kind == kw_only
    assert inspect.signature(Runner.run_template).parameters["timeout"].default is None
    assert inspect.signature(Runner.run_template).parameters["args"].default == ()
    assert inspect.signature(Runner.dispatch).parameters["dry_run"].kind == kw_only
    assert inspect.signature(Runner.dispatch).parameters["dry_run"].default is False


class _Structural:
    """Implements Runner without inheriting from it (how tests/fakes.py FakeRunner may do it)."""

    def require_macos(self): ...
    def things_running(self): return True
    def ensure_running(self, *, allow_launch): return False
    def run_template(self, name, args=(), *, timeout=None): ...
    def dispatch(self, turl, *, dry_run=False): ...

    @contextmanager
    def write_lock(self):
        yield

    def wall(self): return 0.0
    def monotonic(self): return 0.0
    def sleep(self, s): ...


def test_runner_is_runtime_checkable_protocol():
    assert isinstance(_Structural(), Runner)
    assert not isinstance(object(), Runner)
    assert typing.Protocol in Runner.__mro__


def test_explicit_subclass_missing_method_raises():
    class Partial(Runner):
        def things_running(self):
            return True

    p = Partial()
    assert p.things_running() is True
    with pytest.raises(NotImplementedError):
        p.require_macos()
    with pytest.raises(NotImplementedError):
        p.dispatch(object())


# ---- conftest fixtures -------------------------------------------------------------------------

def test_state_dir_fixture_isolates_env(state_dir, tmp_path):
    assert os.environ["THINGS_BRIDGE_STATE_DIR"] == str(state_dir)
    assert state_dir.parent == tmp_path
    assert not state_dir.exists()
    assert "THINGS_AUTH_TOKEN" not in os.environ
    assert [k for k in os.environ if k.startswith("THINGS_BRIDGE_")] == ["THINGS_BRIDGE_STATE_DIR"]


def test_fake_clock(clock):
    w, m = clock.wall(), clock.monotonic()
    assert clock.time() == w
    clock.sleep(0.5)
    clock.advance(1.0)
    assert clock.wall() == pytest.approx(w + 1.5)
    assert clock.monotonic() == pytest.approx(m + 1.5)
    assert clock.sleeps == [0.5]
    assert clock.today().isoformat() == "2026-09-28"
    with pytest.raises(ValueError):
        clock.sleep(-1)
