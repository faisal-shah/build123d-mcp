"""Bounded repair-candidate runner for ``recover_candidate()``.

Run as ``python -m build123d_mcp._recover_subprocess <manifest.json> <out.json>``.

The parent writes the source shape to STEP and starts this process with a hard
timeout. This process imports that STEP, locates validity defects, attempts one
targeted repair rung, writes a candidate STEP if one is produced, and returns a
change report. It never decides fidelity and never mutates the live session.
"""

import json
import sys
from typing import Any


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
    if requested is None:
        raw = [
            int(d["face_index"])
            for d in defects
            if d.get("kind") == "brep_invalid_face" and "face_index" in d
        ]
        source = "located_brep_invalid_faces"
    else:
        raw = [int(i) for i in requested]
        source = "requested_face_indices"

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


def _attempt(manifest: dict) -> dict:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Defeaturing

    from build123d_mcp._locate_subprocess import collect_defects
    from build123d_mcp.tools.export import _write_step
    from build123d_mcp.tools.import_step import _load_step

    shape = _load_step(manifest["input_step"])
    source_summary = _shape_summary(shape)
    defects = collect_defects(shape)
    max_faces = max(1, int(manifest.get("max_faces", 4)))
    requested = manifest.get("face_indices")

    faces = _raw_faces(shape.wrapped)
    selected, selection_source = _select_faces(defects, requested, max_faces, len(faces))
    report: dict[str, Any] = {
        "status": "no_action",
        "rung": "defeature_invalid_faces",
        "defects": defects,
        "selection_source": selection_source,
        "selected_face_indices": selected,
        "source_summary": source_summary,
        "fidelity_verdict": "not_provided",
        "current_shape_unchanged": True,
        "candidate_written": False,
        "note": (
            "Advisory mechanism only: no fidelity verdict is emitted. If a candidate is "
            "registered, run validate(), render/measure/shape_compare as needed, then "
            "explicitly adopt or discard it."
        ),
    }
    if not selected:
        report["reason"] = (
            "no BRep-invalid faces were located, and no face_indices override was supplied"
        )
        return report

    df = BRepAlgoAPI_Defeaturing()
    df.SetShape(shape.wrapped)
    for idx in selected:
        df.AddFaceToRemove(faces[idx])
    df.Build()
    if not df.IsDone():
        report["status"] = "failed"
        report["reason"] = "BRepAlgoAPI_Defeaturing did not complete"
        return report

    report["face_accounting"] = _history_report(df.History(), faces, selected)
    candidate = _as_solid(df.Shape())
    candidate_summary = _shape_summary(candidate)
    report["candidate_summary"] = candidate_summary
    report["deltas"] = _deltas(source_summary, candidate_summary)

    _write_step(candidate, manifest["candidate_step"])
    report["status"] = "candidate"
    report["candidate_written"] = True
    return report


def main(manifest_path: str, out_path: str) -> None:
    with open(manifest_path) as f:
        manifest = json.load(f)
    try:
        payload = _attempt(manifest)
    except Exception as exc:  # noqa: BLE001 - child failures must stay structured
        payload = {
            "status": "failed",
            "rung": "defeature_invalid_faces",
            "error": f"{type(exc).__name__}: {exc}",
            "fidelity_verdict": "not_provided",
            "current_shape_unchanged": True,
        }
    with open(out_path, "w") as f:
        json.dump(payload, f)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
