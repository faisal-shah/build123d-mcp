"""Tests for the find_holes / find_bosses MCP tools (#264).

Recognition correctness lives in build123d-drafting-helpers (its own suite);
these tests cover the wrapper: object resolution, JSON shape, serialisation
of nested counterbore/spotface records, and error paths.
"""

import json

import pytest

from build123d_mcp.session import Session
from build123d_mcp.tools.find_features import find_bosses, find_holes


@pytest.fixture
def session():
    s = Session()
    s.execute("from build123d import *")
    return s


@pytest.fixture
def cbore_plate(session):
    """60×60×20 plate with a ø10 through hole counterbored ø18×6."""
    session.execute(
        "p = Box(60, 60, 20) - Cylinder(5, 20) - Pos(0, 0, 7) * Cylinder(9, 6)\nshow(p, 'plate')"
    )
    return session


def test_find_holes_counterbored(cbore_plate):
    r = json.loads(find_holes(cbore_plate, "plate"))
    assert r["count"] == 1
    (hole,) = r["holes"]
    assert hole["diameter"] == pytest.approx(10.0)
    assert hole["bottom"] == "through"
    assert hole["axis"] == [0.0, 0.0, -1.0]
    assert hole["location"] == [0.0, 0.0, 10.0]
    assert hole["cbore"] == {"diameter": 18.0, "depth": 6.0}
    assert hole["spotface"] is None


def test_find_holes_defaults_to_current_shape(session):
    session.execute("result = Box(40, 40, 10) - Cylinder(4, 10)")
    r = json.loads(find_holes(session, ""))
    assert r["count"] == 1
    assert r["holes"][0]["diameter"] == pytest.approx(8.0)


def test_find_bosses(session):
    session.execute("p = Box(60, 60, 10) + Pos(0, 0, 9) * Cylinder(12, 8)\nshow(p, 'b')")
    r = json.loads(find_bosses(session, "b"))
    assert r["count"] == 1
    (boss,) = r["bosses"]
    assert boss["diameter"] == pytest.approx(24.0)
    assert boss["height"] == pytest.approx(8.0)
    assert boss["axis"] == [0.0, 0.0, 1.0]
    assert boss["location"] == [0.0, 0.0, 13.0]


def test_plain_box_has_no_features(session):
    session.execute("show(Box(10, 10, 10), 'box')")
    assert json.loads(find_holes(session, "box")) == {"count": 0, "holes": []}
    assert json.loads(find_bosses(session, "box")) == {"count": 0, "bosses": []}


def test_unknown_object_is_a_json_error(session):
    session.execute("show(Box(1, 1, 1), 'a')")
    r = json.loads(find_holes(session, "nope"))
    assert "Unknown object 'nope'" in r["error"]
    assert "'a'" in r["error"]


def test_empty_session_is_a_json_error():
    s = Session()
    r = json.loads(find_bosses(s, ""))
    assert "No shape in session" in r["error"]
