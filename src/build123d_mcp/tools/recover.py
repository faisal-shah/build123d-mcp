"""Advisory repair-candidate tool.

``recover_candidate()`` implements the safe shape of the old recover idea: run a
bounded repair ladder out-of-process, register the first exact-gate-clean named
candidate if one is produced, and return a change report. It never replaces the
source object and never emits a fidelity verdict.
"""

import json
import keyword
import os
import subprocess
import sys
import tempfile
import time
from typing import Any

from build123d_mcp.tools._budget import op_budget
from build123d_mcp.tools.validate import _resolve_shape

_RECOVER_MARGIN_S = 15
_RECOVER_MIN_S = 10
_RECOVER_GATE_TIMEOUT_MAX_S = 35


def _source_names(session: Any, shape: Any) -> list[str]:
    return [name for name, obj in session.objects.items() if obj is shape]


def _run_bounded_recover(
    session: Any,
    shape: Any,
    *,
    face_indices: list[int] | None,
    max_faces: int,
) -> tuple[dict, Any | None]:
    from build123d_mcp.tools.export import _write_step
    from build123d_mcp.tools.import_step import _load_step

    t0 = time.monotonic()
    work = tempfile.mkdtemp(prefix="b123d_recover_")
    in_step = os.path.join(work, "source.step")
    candidate_step = os.path.join(work, "candidate.step")
    manifest_path = os.path.join(work, "manifest.json")
    out_json = os.path.join(work, "report.json")
    try:
        try:
            _write_step(shape, in_step)
        except Exception as exc:  # noqa: BLE001
            return (
                {
                    "status": "failed",
                    "rung": "recover_ladder",
                    "error": f"could not serialize source shape for bounded recovery: {exc}",
                    "fidelity_verdict": "not_provided",
                    "current_shape_unchanged": True,
                },
                None,
            )

        remaining = op_budget(session) - (time.monotonic() - t0) - _RECOVER_MARGIN_S
        if remaining < _RECOVER_MIN_S:
            return (
                {
                    "status": "failed",
                    "rung": "recover_ladder",
                    "error": "not enough of the op budget left to attempt recovery safely",
                    "fidelity_verdict": "not_provided",
                    "current_shape_unchanged": True,
                },
                None,
            )

        with open(manifest_path, "w") as f:
            json.dump(
                {
                    "input_step": in_step,
                    "candidate_step": candidate_step,
                    "face_indices": face_indices,
                    "max_faces": max_faces,
                    "gate_timeout_s": max(
                        _RECOVER_MIN_S,
                        min(_RECOVER_GATE_TIMEOUT_MAX_S, remaining / 3),
                    ),
                },
                f,
            )

        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "build123d_mcp._recover_subprocess",
                    manifest_path,
                    out_json,
                ],
                capture_output=True,
                text=True,
                timeout=remaining,
            )
        except subprocess.TimeoutExpired:
            return (
                {
                    "status": "timeout",
                    "rung": "recover_ladder",
                    "error": (
                        "bounded recovery exceeded the time budget and was stopped; "
                        "the live session was not mutated"
                    ),
                    "fidelity_verdict": "not_provided",
                    "current_shape_unchanged": True,
                },
                None,
            )
        except OSError as exc:
            return (
                {
                    "status": "failed",
                    "rung": "recover_ladder",
                    "error": f"could not start bounded recovery subprocess: {exc}",
                    "fidelity_verdict": "not_provided",
                    "current_shape_unchanged": True,
                },
                None,
            )

        if proc.returncode != 0 or not os.path.exists(out_json):
            return (
                {
                    "status": "failed",
                    "rung": "recover_ladder",
                    "error": "recover subprocess failed: " + (proc.stderr or "")[-300:],
                    "fidelity_verdict": "not_provided",
                    "current_shape_unchanged": True,
                },
                None,
            )
        try:
            with open(out_json) as f:
                report = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            return (
                {
                    "status": "failed",
                    "rung": "recover_ladder",
                    "error": f"recover subprocess produced an unreadable report: {exc}",
                    "fidelity_verdict": "not_provided",
                    "current_shape_unchanged": True,
                },
                None,
            )

        candidate = None
        if report.get("status") == "candidate":
            if not os.path.exists(candidate_step):
                report["status"] = "failed"
                report["error"] = "recover subprocess reported a candidate but did not write a STEP"
            else:
                try:
                    candidate = _load_step(candidate_step)
                except Exception as exc:  # noqa: BLE001
                    report["status"] = "failed"
                    report["error"] = f"could not import recovered candidate: {exc}"
                    candidate = None
        return report, candidate
    finally:
        for p in (in_step, candidate_step, manifest_path, out_json):
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            os.rmdir(work)
        except OSError:
            pass


def recover_candidate(
    session: Any,
    object_name: str = "",
    store_as: str = "recover_candidate",
    face_indices: list[int] | None = None,
    max_faces: int = 4,
) -> str:
    """Try a bounded advisory repair and register a named candidate.

    The ladder currently tries conservative cleanup, a bounded planar-wire patch
    for one malformed face, a guarded micro-relief cleanup when that patch fails
    solely on refined tessellation, then targeted defeaturing of BRep-invalid
    faces on cleaned and raw topology. A produced candidate is stored under
    ``store_as`` only after the exact structural gate passes, while
    ``current_shape`` and the source object are left unchanged.
    """

    shape, err = _resolve_shape(session, object_name)
    if err is not None:
        return err
    store_as = (store_as or "recover_candidate").strip()
    if not store_as:
        return json.dumps({"error": "store_as must not be empty"})
    if max_faces < 1:
        return json.dumps({"error": "max_faces must be at least 1"})
    sources = _source_names(session, shape)
    if store_as in sources:
        return json.dumps(
            {
                "error": (
                    f"store_as '{store_as}' names the source object. Use a different "
                    "candidate name so the source is not replaced."
                )
            }
        )

    current_before = session.current_shape
    report, candidate = _run_bounded_recover(
        session, shape, face_indices=face_indices, max_faces=max_faces
    )
    report.setdefault("source", object_name or "current_shape")
    report.setdefault("candidate", None)
    report.setdefault("current_shape_unchanged", True)
    report.setdefault("fidelity_verdict", "not_provided")

    if candidate is not None and report.get("status") == "candidate":
        session.objects[store_as] = candidate
        namespace_name = None
        if store_as.isidentifier() and not keyword.iskeyword(store_as):
            session.namespace[store_as] = candidate
            namespace_name = store_as
        session.current_shape = current_before
        report["candidate"] = store_as
        report["candidate_namespace"] = namespace_name
        report["current_shape_unchanged"] = session.current_shape is current_before
        report["next_steps"] = [f"run validate('{store_as}')"]
        report["next_steps"].append(f"compare/render/measure '{store_as}' against the source")
        if namespace_name:
            report["next_steps"].append(
                f"adopt explicitly with execute(\"show({namespace_name}, 'part')\") "
                "only if design intent is preserved"
            )
        else:
            report["next_steps"].append(
                "store_as is not a Python identifier, so the candidate is tool-addressable "
                "by object name but was not injected into execute()"
            )
    else:
        session.current_shape = current_before

    return json.dumps(report, indent=2)
