import json

from build123d import Box

from build123d_mcp.session import Session
from build123d_mcp.tools.execute import execute_code
from build123d_mcp.tools.recover import recover_candidate


def _seed_session():
    session = Session()
    execute_code(session, "from build123d import *\npart = Box(10, 10, 10)\nshow(part, 'part')")
    return session


def test_recover_candidate_registers_separate_candidate(monkeypatch):
    session = _seed_session()
    source = session.objects["part"]
    current = session.current_shape
    candidate = Box(8, 8, 8)

    def fake_run_bounded_recover(session_arg, shape, *, face_indices, max_faces):
        assert session_arg is session
        assert shape is source
        assert face_indices == [2]
        assert max_faces == 1
        return (
            {
                "status": "candidate",
                "rung": "defeature_invalid_faces",
                "selected_face_indices": [2],
            },
            candidate,
        )

    monkeypatch.setattr(
        "build123d_mcp.tools.recover._run_bounded_recover",
        fake_run_bounded_recover,
    )

    report = json.loads(
        recover_candidate(session, "part", "healed_candidate", face_indices=[2], max_faces=1)
    )

    assert report["status"] == "candidate"
    assert report["candidate"] == "healed_candidate"
    assert report["candidate_namespace"] == "healed_candidate"
    assert report["current_shape_unchanged"] is True
    assert report["fidelity_verdict"] == "not_provided"
    assert session.objects["part"] is source
    assert session.current_shape is current
    assert session.objects["healed_candidate"] is candidate
    assert session.namespace["healed_candidate"] is candidate


def test_recover_candidate_rejects_replacing_source(monkeypatch):
    session = _seed_session()
    source = session.objects["part"]

    called = False

    def fake_run_bounded_recover(*args, **kwargs):
        nonlocal called
        called = True
        return {}, None

    monkeypatch.setattr(
        "build123d_mcp.tools.recover._run_bounded_recover",
        fake_run_bounded_recover,
    )

    report = json.loads(recover_candidate(session, "part", "part"))

    assert "error" in report
    assert "source object" in report["error"]
    assert called is False
    assert session.objects["part"] is source


def test_recover_candidate_does_not_inject_invalid_identifier(monkeypatch):
    session = _seed_session()
    candidate = Box(6, 6, 6)

    def fake_run_bounded_recover(*args, **kwargs):
        return {"status": "candidate"}, candidate

    monkeypatch.setattr(
        "build123d_mcp.tools.recover._run_bounded_recover",
        fake_run_bounded_recover,
    )

    report = json.loads(recover_candidate(session, "part", "healed candidate"))

    assert report["candidate"] == "healed candidate"
    assert report["candidate_namespace"] is None
    assert session.objects["healed candidate"] is candidate
    assert "healed candidate" not in session.namespace
