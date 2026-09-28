"""Shared fixtures. Nothing here (or in any test) spawns osascript/open or touches Things."""

import os
from datetime import date
from pathlib import Path

import pytest

# §2.14, plus the token override. Cleared for every test so the host environment can't leak in.
ENV_VARS = (
    "THINGS_AUTH_TOKEN",
    "THINGS_BRIDGE_NO_LAUNCH",
    "THINGS_BRIDGE_READ_ONLY",
    "THINGS_BRIDGE_MAX_BATCH",
    "THINGS_BRIDGE_VERIFY_TIMEOUT",
    "THINGS_BRIDGE_LOG",
    "THINGS_BRIDGE_STATE_DIR",
)


class FakeClock:
    """Deterministic time source.

    ``sleep`` advances both clocks instantly and records the duration. It quacks like the ``time``
    module (``time()``, ``monotonic()``, ``sleep()``) and like the ``Runner`` clock methods
    (``wall()``, ``monotonic()``, ``sleep()``).
    """

    def __init__(self, wall: float = 1_790_000_000.0, mono: float = 1_000.0,
                 today: date = date(2026, 9, 28)):
        self._wall = wall
        self._mono = mono
        self.today_date = today
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self._wall

    def wall(self) -> float:
        return self._wall

    def monotonic(self) -> float:
        return self._mono

    def sleep(self, s: float) -> None:
        if s < 0:
            raise ValueError("sleep length must be non-negative")
        self.sleeps.append(s)
        self.advance(s)

    def advance(self, s: float) -> None:
        self._wall += s
        self._mono += s

    def today(self) -> date:
        return self.today_date


@pytest.fixture(autouse=True)
def state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolated env for every test; ``THINGS_BRIDGE_STATE_DIR`` points at a per-test path.

    The directory itself is not created: ``runner.state_dir()`` is responsible for that (0700).
    """
    for name in list(os.environ):
        if name in ENV_VARS or name.startswith("THINGS_BRIDGE_"):
            monkeypatch.delenv(name, raising=False)
    path = tmp_path / "state"
    monkeypatch.setenv("THINGS_BRIDGE_STATE_DIR", str(path))
    return path


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
