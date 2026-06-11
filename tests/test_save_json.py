"""Tests for the save_json sandbox helper — the structured-output channel (#259)."""

import json
from pathlib import Path

import pytest

from build123d_mcp.session import Session


@pytest.fixture
def session():
    return Session()


def test_round_trip(session):
    out = session.execute("p = save_json('save_json_test', {'holes': [3.2, 4.5], 'n': 2})")
    assert "Error" not in out
    # Read the path back out of the namespace rather than parsing prints.
    path = Path(session.namespace["p"])
    assert path.exists()
    assert json.loads(path.read_text()) == {"holes": [3.2, 4.5], "n": 2}


def test_returns_path_inside_per_process_scratch_dir(session):
    import os

    session.execute("p = save_json('scratch_dir_test', [1, 2])")
    path = Path(session.namespace["p"])
    # Per-process scoping: two servers running side by side must not clobber
    # each other's files.
    assert path.parent.name == f"pid-{os.getpid()}"
    assert path.parent.parent.name == "build123d-mcp"
    assert path.name == "scratch_dir_test.json"


def test_rejects_path_separators(session):
    out = session.execute("save_json('../evil', {})")
    assert "Error" in out and "no path separators" in out


def test_rejects_oversized_payload(session):
    out = session.execute("save_json('huge', ['x' * 1000] * 20000)")
    assert "Error" in out and "cap" in out


def test_non_serializable_values_fall_back_to_str(session):
    session.execute("from build123d import Vector\np = save_json('vec', {'v': Vector(1, 2, 3)})")
    data = json.loads(Path(session.namespace["p"]).read_text())
    assert isinstance(data["v"], str)


def test_open_remains_blocked(session):
    # save_json is the sanctioned channel; the general file API stays closed.
    out = session.execute("open('/tmp/build123d-mcp/x.json')")
    assert "SecurityError" in out
