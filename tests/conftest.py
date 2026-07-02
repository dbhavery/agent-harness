"""Shared pytest fixtures.

Ensures the ``src`` layout is importable without an editable install, and
provides a deterministic clock and a temp trace dir for every test.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_harness.clock import FakeClock  # noqa: E402


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=0.0)


@pytest.fixture
def trace_dir(tmp_path: Path) -> Path:
    d = tmp_path / "traces"
    d.mkdir()
    return d
