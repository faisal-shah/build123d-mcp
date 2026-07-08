"""Bounded repair-candidate runner for ``recover_candidate()``.

Run as ``python -m build123d_mcp._recover_subprocess <manifest.json> <out.json>``.

The parent writes the source shape to STEP and starts this process with a hard
timeout. This process imports that STEP, runs a small recovery ladder, writes the
first candidate that passes the same export-style structural gate, and returns a
change report. It never decides fidelity and never mutates the live session.
"""

import json
import os
import sys
import tempfile
from typing import Any

_DEFAULT_GATE_TIMEOUT_S = 35.0
_EPS = 1e-9
_MICRO_RELIEF_MAX_DEFECTS = 3
_MICRO_RELIEF_FACE_AREA_MAX_MM2 = 2.0
_MICRO_RELIEF_SIZES_MM = (0.15, 0.25, 0.4, 0.6)


def _shape_summary(shape: Any) -> dict:
    try:
        bb = shape.bounding_box()
        bbox = {
            "min": [round(bb.min.X, 4), round(bb.min.Y, 4), round(bb.min.Z, 4)],
            "max": [round(bb.max.X, 4), round(bb.max.Y, 4), round(bb.max.Z, 4)],
            "size": [round(bb.size.X, 4), round(bb.size.Y, 4), round(bb.size.Z, 4)],
        }
    except Exception:  # noqa: BLE001 - report best-effort geometry only
        bbox = None
    try:
        volume = round(float(shape.volume), 6)
    except Exception:  # noqa: BLE001
        volume = None
    return {
        "volume": volume,
        "faces": _safe_len(shape, "faces"),
        "edges": _safe_len(shape, "edges"),
        "vertices": _safe_len(shape, "vertices"),
        "bbox": bbox,
    }


def _safe_len(shape: Any, attr: str) -> int | None:
    try:
        return len(getattr(shape, attr)())
    except Exception:  # noqa: BLE001
        return None


def _deltas(before: dict, after: dict) -> dict:
    out: dict[str, Any] = {}
    bv, av = before.get("volume"), after.get("volume")
    if bv is not None and av is not None:
        out["volume_delta"] = round(av - bv, 6)
        out["volume_delta_pct"] = round(((av - bv) / bv) * 100, 6) if abs(bv) > 1e-12 else None
    for key in ("faces", "edges", "vertices"):
        if before.get(key) is not None and after.get(key) is not None:
            out[f"{key}_delta"] = after[key] - before[key]
    return out


def _raw_faces(raw_shape: Any) -> list:
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    faces = []
    exp = TopExp_Explorer(raw_shape, TopAbs_FACE)
    while exp.More():
        faces.append(TopoDS.Face_s(exp.Current()))
        exp.Next()
    return faces


def _face_summary(face: Any, index: int) -> dict:
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    center = props.CentreOfMass()
    try:
        surface = str(BRepAdaptor_Surface(face).GetType()).split(".")[-1].replace("GeomAbs_", "")
    except Exception:  # noqa: BLE001
        surface = "Unknown"
    return {
        "face_index": index,
        "area": round(float(props.Mass()), 6),
        "surface": surface,
        "center": [round(center.X(), 4), round(center.Y(), 4), round(center.Z(), 4)],
    }


def _history_report(history: Any, faces: list, selected: list[int]) -> dict:
    """Best-effort OCCT face-accounting report for the attempted rung."""

    removed = []
    modified = []
    generated = []
    for idx in selected:
        face = faces[idx]
        base = _face_summary(face, idx)
        try:
            if hasattr(history, "IsRemoved") and history.IsRemoved(face):
                removed.append(base)
        except Exception:  # noqa: BLE001
            pass
        try:
            mods = history.Modified(face)
            n = mods.Extent() if hasattr(mods, "Extent") else len(list(mods))
            if n:
                modified.append({**base, "modified_count": int(n)})
        except Exception:  # noqa: BLE001
            pass
        try:
            gens = history.Generated(face)
            n = gens.Extent() if hasattr(gens, "Extent") else len(list(gens))
            if n:
                generated.append({**base, "generated_count": int(n)})
        except Exception:  # noqa: BLE001
            pass
    return {
        "removed": removed,
        "modified": modified,
        "generated": generated,
        "selected": [_face_summary(faces[idx], idx) for idx in selected],
        "note": (
            "OCCT history is reported for auditing only. It is not a fidelity verdict; "
            "inspect the named candidate and compare against design intent before adoption."
        ),
    }


def _brep_invalid_face_defects(shape: Any) -> list[dict]:
    """Cheap BRep-invalid face locator for recovery selection.

    The full ``locate_gate_defects()`` subprocess also runs mesh locators, which
    can dominate recovery on large imports. For recovery's face-defeaturing rung
    we only need BRep-invalid face identities, so keep this intentionally narrow.
    """

    from OCP.BRepCheck import BRepCheck_Analyzer

    faces = _raw_faces(shape.wrapped)
    analyzer = BRepCheck_Analyzer(shape.wrapped)
    defects = []
    for idx, face in enumerate(faces):
        if analyzer.IsValid(face):
            continue
        try:
            status = [str(s).split(".")[-1] for s in analyzer.Result(face).Status()]
        except Exception:  # noqa: BLE001 - best-effort diagnostic
            status = []
        defects.append(
            {
                "kind": "brep_invalid_face",
                **_face_summary(face, idx),
                "status": status,
                "hint": "malformed face - defeature it or rebuild the local patch",
            }
        )
    return defects


def _as_solid(raw_shape: Any):
    from build123d import Solid
    from OCP.ShapeFix import ShapeFix_Solid
    from OCP.TopAbs import TopAbs_COMPOUND, TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopoDS import TopoDS, TopoDS_Iterator

    st = raw_shape.ShapeType()
    if st == TopAbs_SOLID:
        return Solid(TopoDS.Solid_s(raw_shape))
    if st == TopAbs_SHELL:
        return Solid(ShapeFix_Solid().SolidFromShell(TopoDS.Shell_s(raw_shape)))
    if st != TopAbs_COMPOUND:
        raise RuntimeError(f"no solid or shell found in shape of type {st}")

    solids, shells, mixed = [], [], False
    stack = [raw_shape]
    while stack:
        it = TopoDS_Iterator(stack.pop())
        while it.More():
            child = it.Value()
            cst = child.ShapeType()
            if cst == TopAbs_SOLID:
                solids.append(TopoDS.Solid_s(child))
            elif cst == TopAbs_COMPOUND:
                stack.append(child)
            elif cst == TopAbs_SHELL:
                shells.append(TopoDS.Shell_s(child))
            else:
                mixed = True
            it.Next()

    if mixed or (solids and shells):
        raise RuntimeError(f"mixed topology in a {st} - expected only solids or only shells")
    if len(solids) == 1:
        return Solid(solids[0])
    if len(solids) > 1:
        raise RuntimeError(f"expected 1 solid, found {len(solids)} - operation did not merge")
    if len(shells) == 1:
        return Solid(ShapeFix_Solid().SolidFromShell(shells[0]))
    if len(shells) > 1:
        raise RuntimeError(f"expected 1 shell, found {len(shells)} - operation split the shell")
    raise RuntimeError(f"no solid or shell found in shape of type {st}")


def _select_faces(
    defects: list[dict], requested: list[int] | None, max_faces: int, n_faces: int
) -> tuple[list[int], str]:
    located = [
        int(d["face_index"])
        for d in defects
        if d.get("kind") == "brep_invalid_face" and "face_index" in d
    ]
    # Face order can change after STEP round-trip or clean/fix preconditioning.
    # Prefer faces located in this subprocess; use explicit face_indices only as
    # a fallback when no BRep-invalid face is locatable on this rung's topology.
    if located:
        raw = located
        source = (
            "located_brep_invalid_faces"
            if requested is None
            else "located_brep_invalid_faces_preferred_over_requested"
        )
    elif requested is not None:
        raw = [int(i) for i in requested]
        source = "requested_face_indices"
    else:
        raw = []
        source = "located_brep_invalid_faces"

    selected = list(dict.fromkeys(raw))
    bad = [i for i in selected if i < 0 or i >= n_faces]
    if bad:
        raise ValueError(f"face_indices out of range for {n_faces} faces: {bad}")
    if len(selected) > max_faces:
        raise ValueError(
            f"{len(selected)} faces selected, above max_faces={max_faces}; "
            "run a narrower targeted repair."
        )
    return selected, source


def _face_edge_count(face: Any) -> int | None:
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer

    try:
        count = 0
        exp = TopExp_Explorer(face, TopAbs_EDGE)
        while exp.More():
            count += 1
            exp.Next()
        return count
    except Exception:  # noqa: BLE001
        return None


def _complex_selected_faces(faces: list, selected: list[int], max_edges: int = 12) -> list[dict]:
    complex_faces = []
    for idx in selected:
        edge_count = _face_edge_count(faces[idx])
        if edge_count is not None and edge_count > max_edges:
            complex_faces.append({**_face_summary(faces[idx], idx), "edge_count": edge_count})
    return complex_faces


def _mesh_tuple_report(mesh: tuple | None) -> dict:
    if mesh is None:
        return {"ok": False, "error": "exact mesh gate did not return a verdict"}
    return {
        "mesh_nonmanifold_edges": int(mesh[0]),
        "mesh_open_edges": int(mesh[1]),
        "untriangulated_faces": int(mesh[2]),
        "refined_untriangulated_faces": int(mesh[3]),
        "mesh_nonmanifold_vertices": int(mesh[4]),
        "mesh_vertex_deflection_defects": int(mesh[5]),
        "ok": bool(mesh[6]),
    }


def _cheap_structural_gate(shape: Any) -> dict:
    """BRep/edge-only gate used as recovery's prefilter.

    Do not call ``_gate_report(..., exact=False)`` here: for moderate shapes it
    can still enter the inline exact mesh path. Recovery should reserve mesh
    work for the isolated final acceptance check.
    """

    from OCP.BRepCheck import BRepCheck_Analyzer

    from build123d_mcp.tools.validate import _edge_defects

    try:
        n_solids = len(shape.solids())
    except Exception:  # noqa: BLE001
        n_solids = 0
    try:
        volume = round(float(shape.volume), 4)
    except Exception:  # noqa: BLE001
        volume = 0.0
    edge_error: str | None
    try:
        open_edges, nonmanifold_edges, bad_edges_ok = _edge_defects(shape)
    except Exception as exc:  # noqa: BLE001
        open_edges, nonmanifold_edges, bad_edges_ok = 0, 0, False
        edge_error = f"{type(exc).__name__}: {exc}"
    else:
        edge_error = None
    brep_error: str | None
    try:
        brep_valid = bool(BRepCheck_Analyzer(shape.wrapped).IsValid())
    except Exception as exc:  # noqa: BLE001
        brep_valid = False
        brep_error = f"{type(exc).__name__}: {exc}"
    else:
        brep_error = None

    watertight_manifold = bad_edges_ok and open_edges == 0 and nonmanifold_edges == 0
    passes_gate = n_solids == 1 and abs(volume) > _EPS and watertight_manifold and brep_valid
    problems = []
    if n_solids != 1:
        problems.append(f"{n_solids} solid bodies")
    if abs(volume) <= _EPS:
        problems.append("zero/degenerate volume")
    if not brep_valid:
        problems.append("B-rep is not well-formed (BRepCheck failed)")
    if open_edges:
        problems.append(f"{open_edges} open edge(s)")
    if nonmanifold_edges:
        problems.append(f"{nonmanifold_edges} non-manifold edge(s)")
    if edge_error:
        problems.append(f"edge map failed: {edge_error}")
    if brep_error:
        problems.append(f"BRepCheck failed to run: {brep_error}")

    return {
        "passes_gate": passes_gate,
        "n_solids": n_solids,
        "volume": volume,
        "watertight_manifold": watertight_manifold,
        "brep_valid": brep_valid,
        "open_edges": open_edges,
        "nonmanifold_edges": nonmanifold_edges,
        "mesh_check": "not_run",
        "mesh_nonmanifold_edges": 0,
        "mesh_open_edges": 0,
        "untriangulated_faces": 0,
        "refined_untriangulated_faces": 0,
        "mesh_nonmanifold_vertices": 0,
        "mesh_vertex_deflection_defects": 0,
        "summary": None if passes_gate else "; ".join(problems),
    }


def _evaluate_candidate(candidate: Any, manifest: dict) -> dict:
    """Run the fast BRep gate plus the isolated exact mesh gate."""

    from build123d_mcp.tools.export import _write_step
    from build123d_mcp.tools.validate import _gate_report, _run_mesh_gate_subprocess

    fast_report = _cheap_structural_gate(candidate)
    evaluation: dict[str, Any] = {
        "fast_gate": fast_report,
        "exact_mesh_gate": None,
        "gate_report": fast_report,
        "passes_exact_gate": False,
    }
    if (
        not fast_report.get("brep_valid")
        or fast_report.get("open_edges")
        or fast_report.get("nonmanifold_edges")
    ):
        evaluation["reject_reason"] = "candidate failed BRep/edge gate before exact mesh check"
        return evaluation

    timeout = float(manifest.get("gate_timeout_s", _DEFAULT_GATE_TIMEOUT_S))
    with tempfile.TemporaryDirectory(prefix="b123d_recover_gate_") as work:
        step = os.path.join(work, "candidate.step")
        try:
            _write_step(candidate, step)
        except Exception as exc:  # noqa: BLE001
            evaluation["reject_reason"] = f"candidate could not be serialized for exact gate: {exc}"
            return evaluation

        mesh = _run_mesh_gate_subprocess(step, timeout=timeout)
    mesh_report = _mesh_tuple_report(mesh)
    evaluation["exact_mesh_gate"] = mesh_report
    if mesh is None:
        evaluation["gate_report"] = _gate_report(
            candidate, exact=False, mesh_override=(0, 0, 0, 0, 0, 0, False)
        )
        evaluation["reject_reason"] = "exact mesh gate timed out or failed"
        return evaluation
    if not bool(mesh[6]):
        evaluation["gate_report"] = _gate_report(candidate, exact=False, mesh_override=mesh)
        evaluation["reject_reason"] = "exact mesh gate could not verify this candidate"
        return evaluation

    gate_report = _gate_report(candidate, exact=False, mesh_override=mesh)
    evaluation["gate_report"] = gate_report
    evaluation["passes_exact_gate"] = bool(
        gate_report.get("passes_gate") and gate_report.get("mesh_check") == "exact-subprocess"
    )
    if not evaluation["passes_exact_gate"]:
        evaluation["reject_reason"] = gate_report.get("summary") or "candidate failed exact gate"
    return evaluation


def _candidate_entry(
    rung: str,
    candidate: Any,
    source_summary: dict,
    manifest: dict,
    extra: dict | None = None,
) -> dict:
    candidate_summary = _shape_summary(candidate)
    entry: dict[str, Any] = {
        "rung": rung,
        "status": "candidate_rejected",
        "candidate_summary": candidate_summary,
        "deltas": _deltas(source_summary, candidate_summary),
        "gate": _evaluate_candidate(candidate, manifest),
    }
    if extra:
        entry.update(extra)
    if entry["gate"].get("passes_exact_gate"):
        entry["status"] = "candidate"
    return entry


def _defeature_candidate(shape: Any, selected: list[int]) -> tuple[Any, Any]:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Defeaturing

    faces = _raw_faces(shape.wrapped)
    df = BRepAlgoAPI_Defeaturing()
    df.SetShape(shape.wrapped)
    for idx in selected:
        df.AddFaceToRemove(faces[idx])
    df.Build()
    if not df.IsDone():
        raise RuntimeError("BRepAlgoAPI_Defeaturing did not complete")

    candidate = _as_solid(df.Shape())
    try:
        candidate = candidate.clean().fix()
    except Exception:  # noqa: BLE001 - keep the raw candidate if cleanup fails
        pass
    return candidate, df.History()


def _shape_fix_solid(raw_shape: Any, precision: float, max_tolerance: float) -> Any:
    from OCP.ShapeFix import ShapeFix_Shape

    fixer = ShapeFix_Shape(raw_shape)
    fixer.SetPrecision(precision)
    fixer.SetMaxTolerance(max_tolerance)
    fixer.Perform()
    return _as_solid(fixer.Shape())


def _sew_same_parameter_fix(raw_shape: Any, sew_tolerance: float = 1e-3) -> tuple[Any, dict]:
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
    from OCP.BRepLib import BRepLib

    sewer = BRepBuilderAPI_Sewing(sew_tolerance)
    sewer.Add(raw_shape)
    sewer.Perform()
    sewed = sewer.SewedShape()
    try:
        BRepLib.SameParameter_s(sewed, 1e-5, True)
    except Exception:  # noqa: BLE001 - shape fix still gets a chance below
        pass
    candidate = _shape_fix_solid(sewed, 1e-5, 1e-2)
    return candidate, {
        "sew_tolerance": sew_tolerance,
        "free_edges": sewer.NbFreeEdges(),
        "multiple_edges": sewer.NbMultipleEdges(),
        "contiguous_edges": sewer.NbContigousEdges(),
    }


def _planar_wire_patch_candidate(shape: Any, selected: list[int]) -> tuple[Any, dict]:
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.gp import gp_Dir, gp_Pln, gp_Pnt
    from OCP.ShapeBuild import ShapeBuild_ReShape
    from OCP.TopAbs import TopAbs_WIRE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    if len(selected) != 1:
        raise RuntimeError("planar wire patch requires exactly one selected face")
    idx = selected[0]
    bad_face = shape.faces()[idx]
    center = bad_face.center()
    normal = bad_face.normal_at()
    wire_exp = TopExp_Explorer(bad_face.wrapped, TopAbs_WIRE)
    if not wire_exp.More():
        raise RuntimeError("selected face has no boundary wire")
    wire = TopoDS.Wire_s(wire_exp.Current())
    wire_exp.Next()
    if wire_exp.More():
        raise RuntimeError("selected face has multiple wires; planar wire patch is not bounded")

    face_builder = BRepBuilderAPI_MakeFace(
        gp_Pln(
            gp_Pnt(center.X, center.Y, center.Z),
            gp_Dir(normal.X, normal.Y, normal.Z),
        ),
        wire,
        True,
    )
    if not face_builder.IsDone():
        raise RuntimeError("planar replacement face could not be built")

    reshaper = ShapeBuild_ReShape()
    reshaper.Replace(bad_face.wrapped, face_builder.Face())
    candidate, sew_report = _sew_same_parameter_fix(reshaper.Apply(shape.wrapped), 1e-3)
    return candidate, {
        "selected_face": {
            "face_index": idx,
            "center": _face_summary(_raw_faces(shape.wrapped)[idx], idx)["center"],
        },
        "replacement": "planar_face_on_existing_boundary_wire",
        **sew_report,
    }


def _attempt_planar_wire_patch_rung(
    rung: str,
    shape: Any,
    defects: list[dict],
    requested: list[int] | None,
    max_faces: int,
    source_summary: dict,
    manifest: dict,
) -> dict:
    faces = _raw_faces(shape.wrapped)
    selected, selection_source = _select_faces(defects, requested, max_faces, len(faces))
    entry: dict[str, Any] = {
        "rung": rung,
        "status": "skipped",
        "defects": defects,
        "selection_source": selection_source,
        "selected_face_indices": selected,
    }
    if not selected:
        entry["reason"] = (
            "no BRep-invalid faces were located and no face_indices override was supplied"
        )
        return entry
    if len(selected) != 1:
        entry["reason"] = "planar wire patch is limited to one malformed face"
        return entry
    try:
        candidate, patch_report = _planar_wire_patch_candidate(shape, selected)
    except Exception as exc:  # noqa: BLE001
        entry["status"] = "failed"
        entry["reason"] = f"{type(exc).__name__}: {exc}"
        return entry

    candidate_entry = _candidate_entry(
        rung,
        candidate,
        source_summary,
        manifest,
        extra={
            "defects": defects,
            "selection_source": selection_source,
            "selected_face_indices": selected,
            "patch_report": patch_report,
        },
    )
    candidate_entry["_candidate_shape"] = candidate
    return candidate_entry


def _is_refined_only_rejection(entry: dict) -> bool:
    if entry.get("status") != "candidate_rejected":
        return False
    gate = entry.get("gate") or {}
    mesh = gate.get("exact_mesh_gate") or {}
    if not mesh.get("ok") or int(mesh.get("refined_untriangulated_faces") or 0) <= 0:
        return False
    for key in (
        "mesh_nonmanifold_edges",
        "mesh_open_edges",
        "untriangulated_faces",
        "mesh_nonmanifold_vertices",
        "mesh_vertex_deflection_defects",
    ):
        if int(mesh.get(key) or 0):
            return False
    return bool((gate.get("fast_gate") or {}).get("passes_gate"))


def _roundtrip_for_refined_locator(shape: Any) -> tuple[Any, list[dict]]:
    from build123d import import_step

    from build123d_mcp._locate_subprocess import _mesh_refined_untriangulated_faces
    from build123d_mcp.tools.export import _write_step

    with tempfile.TemporaryDirectory(prefix="b123d_recover_refined_") as work:
        step = os.path.join(work, "candidate.step")
        _write_step(shape, step)
        roundtripped = import_step(step)
    return roundtripped, _mesh_refined_untriangulated_faces(roundtripped)


def _summarize_refined_defects(shape: Any, defects: list[dict]) -> tuple[list[dict], list[dict]]:
    faces = _raw_faces(shape.wrapped)
    selected = []
    skipped = []
    for defect in defects:
        try:
            idx = int(defect["face_index"])
            if idx < 0 or idx >= len(faces):
                raise IndexError(idx)
            summary = _face_summary(faces[idx], idx)
        except Exception as exc:  # noqa: BLE001
            skipped.append({**defect, "skip_reason": f"could not summarize face: {exc}"})
            continue
        merged = {**defect, "face": summary}
        if float(summary.get("area") or 0.0) > _MICRO_RELIEF_FACE_AREA_MAX_MM2:
            merged["skip_reason"] = (
                f"face area exceeds micro-relief limit ({_MICRO_RELIEF_FACE_AREA_MAX_MM2} mm^2)"
            )
            skipped.append(merged)
            continue
        selected.append(merged)
    return selected, skipped


def _micro_relief_candidate(shape: Any, defects: list[dict], size_mm: float) -> Any:
    from build123d import Box, Location

    candidate = shape
    for defect in defects:
        where = defect.get("where")
        if not isinstance(where, (list, tuple)) or len(where) != 3:
            raise RuntimeError(f"refined defect lacks a usable location: {defect}")
        cutter = Box(size_mm, size_mm, size_mm).move(Location(tuple(float(v) for v in where)))
        candidate = candidate - cutter
    try:
        candidate = candidate.clean().fix()
    except Exception:  # noqa: BLE001 - exact gate below decides whether this is usable
        pass
    return candidate


def _attempt_micro_relief_rung(
    rung: str,
    base_candidate: Any,
    source_summary: dict,
    manifest: dict,
    parent_rung: str,
) -> dict:
    entry: dict[str, Any] = {
        "rung": rung,
        "status": "skipped",
        "parent_rung": parent_rung,
        "reason": "parent candidate did not expose refined tessellation defects",
    }
    try:
        roundtripped, defects = _roundtrip_for_refined_locator(base_candidate)
    except Exception as exc:  # noqa: BLE001
        entry["status"] = "failed"
        entry["reason"] = f"{type(exc).__name__}: {exc}"
        return entry

    entry["refined_defects"] = defects
    if not defects:
        return entry
    if len(defects) > _MICRO_RELIEF_MAX_DEFECTS:
        entry["reason"] = (
            f"micro relief is limited to {_MICRO_RELIEF_MAX_DEFECTS} refined faces; "
            f"found {len(defects)}"
        )
        return entry

    selected, skipped = _summarize_refined_defects(roundtripped, defects)
    entry["selected_refined_defects"] = selected
    if skipped:
        entry["skipped_refined_defects"] = skipped
    if not selected:
        entry["reason"] = "no refined defects were small enough for micro relief"
        return entry

    attempts = []
    base_summary = _shape_summary(roundtripped)
    for size in _MICRO_RELIEF_SIZES_MM:
        try:
            candidate = _micro_relief_candidate(roundtripped, selected, size)
        except Exception as exc:  # noqa: BLE001
            attempts.append(
                {
                    "relief_size_mm": size,
                    "status": "failed",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        attempt = _candidate_entry(
            rung,
            candidate,
            source_summary,
            manifest,
            extra={
                "parent_rung": parent_rung,
                "relief_size_mm": size,
                "selected_refined_defects": selected,
                "roundtrip_deltas": _deltas(base_summary, _shape_summary(candidate)),
            },
        )
        attempts.append(
            {
                "relief_size_mm": size,
                "status": attempt.get("status"),
                "gate": attempt.get("gate"),
                "deltas": attempt.get("deltas"),
                "roundtrip_deltas": attempt.get("roundtrip_deltas"),
            }
        )
        if attempt.get("status") == "candidate":
            attempt["attempts"] = attempts
            attempt["_candidate_shape"] = candidate
            return attempt

    entry["status"] = "candidate_rejected"
    entry["reason"] = "no micro-relief size produced a candidate that passed the exact gate"
    entry["attempts"] = attempts
    return entry


def _attempt_defeature_rung(
    rung: str,
    shape: Any,
    defects: list[dict],
    requested: list[int] | None,
    max_faces: int,
    source_summary: dict,
    manifest: dict,
) -> dict:
    faces = _raw_faces(shape.wrapped)
    selected, selection_source = _select_faces(defects, requested, max_faces, len(faces))
    entry: dict[str, Any] = {
        "rung": rung,
        "status": "skipped",
        "defects": defects,
        "selection_source": selection_source,
        "selected_face_indices": selected,
    }
    if not selected:
        entry["reason"] = (
            "no BRep-invalid faces were located and no face_indices override was supplied"
        )
        return entry
    complex_faces = _complex_selected_faces(faces, selected)
    if complex_faces:
        entry["reason"] = (
            "selected face has a complex boundary; native OCCT defeaturing can run "
            "unbounded on this topology, so this rung was skipped"
        )
        entry["complex_faces"] = complex_faces
        return entry
    try:
        candidate, history = _defeature_candidate(shape, selected)
    except Exception as exc:  # noqa: BLE001
        entry["status"] = "failed"
        entry["reason"] = f"{type(exc).__name__}: {exc}"
        return entry

    entry["face_accounting"] = _history_report(history, faces, selected)
    candidate_entry = _candidate_entry(rung, candidate, source_summary, manifest)
    candidate_entry.update(
        {
            "defects": defects,
            "selection_source": selection_source,
            "selected_face_indices": selected,
            "face_accounting": entry["face_accounting"],
        }
    )
    if candidate_entry.get("status") == "candidate":
        candidate_entry["_candidate_shape"] = candidate
    return candidate_entry


def _attempt(manifest: dict) -> dict:
    from build123d_mcp.tools.export import _write_step
    from build123d_mcp.tools.import_step import _load_step

    shape = _load_step(manifest["input_step"])
    source_summary = _shape_summary(shape)
    source_fast_gate = _cheap_structural_gate(shape)
    defects = _brep_invalid_face_defects(shape)
    max_faces = max(1, int(manifest.get("max_faces", 4)))
    requested = manifest.get("face_indices")
    report: dict[str, Any] = {
        "status": "no_action",
        "rung": None,
        "defects": defects,
        "source_fast_gate": source_fast_gate,
        "requested_face_indices": requested,
        "source_summary": source_summary,
        "fidelity_verdict": "not_provided",
        "current_shape_unchanged": True,
        "candidate_written": False,
        "rungs": [],
        "note": (
            "Advisory mechanism only: no fidelity verdict is emitted. If a candidate is "
            "registered, run validate(), render/measure/shape_compare as needed, then "
            "explicitly adopt or discard it."
        ),
    }

    if not defects and requested is None and source_fast_gate.get("passes_gate"):
        source_exact_gate = _evaluate_candidate(shape, manifest)
        report["source_exact_gate"] = source_exact_gate
        if source_exact_gate.get("passes_exact_gate"):
            report["reason"] = "source already passes the exact structural gate"
            return report

    def accept_if_candidate(entry: dict) -> bool:
        candidate_shape = entry.pop("_candidate_shape", None)
        report["rungs"].append(entry)
        if entry.get("status") != "candidate":
            return False
        if candidate_shape is None:
            entry["status"] = "failed"
            entry["reason"] = "internal error: accepted rung did not retain candidate shape"
            return False
        _write_step(candidate_shape, manifest["candidate_step"])
        report.update(entry)
        report["candidate_written"] = True
        return True

    # Rung 1: conservative build123d cleanup. This can clear purely stale
    # topology but is only accepted after the exact gate.
    try:
        clean_shape = _load_step(manifest["input_step"]).clean().fix()
        entry = _candidate_entry("clean_fix", clean_shape, source_summary, manifest)
        entry["_candidate_shape"] = clean_shape
    except Exception as exc:  # noqa: BLE001
        entry = {"rung": "clean_fix", "status": "failed", "reason": f"{type(exc).__name__}: {exc}"}
    if accept_if_candidate(entry):
        return report

    # Rung 2: for complex imported faces, replace the malformed face with a
    # planar patch on its existing boundary. This is bounded and generic, unlike
    # native defeaturing on high-edge-count self-touching faces. It is still only
    # accepted after the exact gate passes.
    entry = _attempt_planar_wire_patch_rung(
        "planar_wire_patch_invalid_face",
        _load_step(manifest["input_step"]),
        defects,
        requested,
        max_faces,
        source_summary,
        manifest,
    )
    planar_candidate = entry.get("_candidate_shape")
    if accept_if_candidate(entry):
        return report
    if planar_candidate is not None and _is_refined_only_rejection(entry):
        entry = _attempt_micro_relief_rung(
            "planar_wire_patch_micro_relief",
            planar_candidate,
            source_summary,
            manifest,
            "planar_wire_patch_invalid_face",
        )
        if accept_if_candidate(entry):
            return report

    # Rung 3: clean/fix first, then locate invalid faces on that topology and
    # defeature them. Fixture 217 needs this; raw defeature leaves it invalid.
    try:
        clean_shape = _load_step(manifest["input_step"]).clean().fix()
        clean_defects = _brep_invalid_face_defects(clean_shape)
        entry = _attempt_defeature_rung(
            "clean_fix_defeature_invalid_faces",
            clean_shape,
            clean_defects,
            requested,
            max_faces,
            source_summary,
            manifest,
        )
    except Exception as exc:  # noqa: BLE001
        entry = {
            "rung": "clean_fix_defeature_invalid_faces",
            "status": "failed",
            "reason": f"{type(exc).__name__}: {exc}",
        }
    if accept_if_candidate(entry):
        return report

    # Rung 4: raw targeted defeature, preserved for cases where preconditioning
    # would move topology in an undesirable way.
    raw_shape = _load_step(manifest["input_step"])
    raw_defects = _brep_invalid_face_defects(raw_shape)
    entry = _attempt_defeature_rung(
        "defeature_invalid_faces",
        raw_shape,
        raw_defects,
        requested,
        max_faces,
        source_summary,
        manifest,
    )
    if accept_if_candidate(entry):
        return report

    report["status"] = "failed" if report["rungs"] else "no_action"
    report["reason"] = "no recovery rung produced a candidate that passed the exact gate"
    return report


def main(manifest_path: str, out_path: str) -> None:
    with open(manifest_path) as f:
        manifest = json.load(f)
    try:
        payload = _attempt(manifest)
    except Exception as exc:  # noqa: BLE001 - child failures must stay structured
        payload = {
            "status": "failed",
            "rung": "recover_ladder",
            "error": f"{type(exc).__name__}: {exc}",
            "fidelity_verdict": "not_provided",
            "current_shape_unchanged": True,
        }
    with open(out_path, "w") as f:
        json.dump(payload, f)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
