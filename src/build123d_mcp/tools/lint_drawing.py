"""lint_drawing — structural checks on a 2D drawing.

Two modes:

  1. Session mode: reconstructs the session's annotations as build123d_drafting
     result objects and **delegates the geometry checks to the helpers**
     (`lint_drawing` + `find_interferences`) — single source of truth, no
     duplicated check logic. The MCP keeps only what the helpers can't do from
     the stored data: the leader elbow check (leader text geometry isn't stored
     separately) and the per-edge page-bounds check.

  2. SVG mode: scans an exported SVG for layer-level pathologies that only show
     up at export time (text on a no-fill layer), plus sidecar label checks.

The structured violation list is JSON so the LLM can iterate without rendering.
Each violation's `check` is the helpers' stable `LintIssue.code`.
"""
import json
import os
import re
import xml.etree.ElementTree as ET

from build123d_drafting import (
    CenterlineResult,
    DimResult,
    find_interferences,
    lint_drawing as _helper_lint,
)

# Checks that apply to a single annotation — run per-item so the violation can
# carry the session object name (the helpers' issues only know the label text).
_PER_ITEM_CODES = {"label_vs_measured", "dim_inside_part"}

# The MCP elevates these helper "warning"s to "error" in its own contract.
_SEVERITY_OVERRIDE = {"label_vs_measured": "error"}


def _violation(issue, obj: str) -> dict:
    return {
        "severity": _SEVERITY_OVERRIDE.get(issue.code, issue.severity),
        "check": issue.code or "lint",
        "object": obj,
        "message": issue.message,
    }


def _pair_object(issue, items) -> str:
    """Best-effort: session names whose label text appears in a pairwise message."""
    names = []
    for name, res in items:
        label = getattr(res, "label_str", "")
        if label and (f'"{label}"' in issue.message or f"'{label}'" in issue.message):
            names.append(name)
    return "+".join(dict.fromkeys(names))


def _reconstruct(session):
    """[(name, result)] for each dim/centerline annotation that has geometry.

    Leaders are skipped here — their text geometry isn't stored separately, so
    the leader check stays in `_lint_leaders`.
    """
    items = []
    for name, meta in session.drawing_annotations.items():
        if name not in session.objects:
            continue
        shape = session.objects[name]
        if meta.get("type") == "centerline":
            items.append((name, CenterlineResult(shape=shape,
                                                 label_str=meta.get("label_str", ""))))
        elif meta.get("elbow") is not None:
            continue  # leader — handled by _lint_leaders
        else:
            items.append((name, DimResult(
                shape=shape,
                label_str=meta.get("label_str", ""),
                measured_length=meta.get("measured_length") or 0.0,
                dim_level_y=meta.get("dim_level_y"),
                label_bbox=meta.get("label_bbox"),
            )))
    return items


def _lint_session(session) -> list[dict]:
    items = _reconstruct(session)
    results = [r for _, r in items]
    violations: list[dict] = []

    # per-item checks (carry the session name)
    for name, res in items:
        for issue in _helper_lint([res]):
            if issue.code in _PER_ITEM_CODES:
                violations.append(_violation(issue, name))

    # pairwise checks + geometry-precise interference over the whole set
    if len(results) >= 2:
        for issue in _helper_lint(results):
            if issue.code not in _PER_ITEM_CODES:
                violations.append(_violation(issue, _pair_object(issue, items)))
    for issue in find_interferences(results):
        violations.append(_violation(issue, _pair_object(issue, items)))

    # MCP-native checks the helpers can't run from the stored data
    violations += _lint_leaders(session)
    if session.drawing_page:
        violations += _lint_page_bounds(
            session.drawing_annotations, session.objects, session.drawing_page)
    return violations


def _lint_leaders(session) -> list[dict]:
    """Leader elbow inside the label region — MCP-native because the leader's
    text geometry isn't stored separately from its lines in the session."""
    violations: list[dict] = []
    for name, ann in session.drawing_annotations.items():
        elbow = ann.get("elbow")
        if not elbow or name not in session.objects:
            continue
        try:
            bb = session.objects[name].bounding_box()
            if bb.min.X <= elbow[0] <= bb.max.X and bb.min.Y <= elbow[1] <= bb.max.Y:
                violations.append({
                    "severity": "warning",
                    "check": "leader_elbow_in_label",
                    "object": name,
                    "message": (
                        f"leader elbow ({elbow[0]:.2f}, {elbow[1]:.2f}) "
                        f"may be inside the label bbox"
                    ),
                })
        except Exception:
            pass
    return violations


def _lint_page_bounds(annotations: dict, objects: dict, page: dict) -> list[dict]:
    """Check every annotation stays within the drawable page area."""
    violations: list[dict] = []
    for name in annotations:
        if name not in objects:
            continue
        try:
            bb = objects[name].bounding_box()
        except Exception:
            continue
        overshoots = []
        if bb.min.X < page["min_x"]:
            overshoots.append(f"left by {page['min_x'] - bb.min.X:.1f} mm")
        if bb.max.X > page["max_x"]:
            overshoots.append(f"right by {bb.max.X - page['max_x']:.1f} mm")
        if bb.min.Y < page["min_y"]:
            overshoots.append(f"bottom by {page['min_y'] - bb.min.Y:.1f} mm")
        if bb.max.Y > page["max_y"]:
            overshoots.append(f"top by {bb.max.Y - page['max_y']:.1f} mm")
        for detail in overshoots:
            violations.append({
                "severity": "error",
                "check": "annotation_out_of_bounds",
                "object": name,
                "message": (
                    f"annotation '{name}' extends past page edge ({detail}) "
                    f"— move it inward or reduce offset"
                ),
            })
    return violations


_SVG_NS = "{http://www.w3.org/2000/svg}"


def _lint_svg(svg_path: str) -> list[dict]:
    """Layer-level checks on an exported SVG file, plus sidecar label checks.

    - text on a group with `fill='none'` renders glyphs as illegible outlines;
    - label-vs-measured divergence from the `.dims.json` sidecar (no live
      geometry here, so the helper's per-item check is run on shape-less
      reconstructed `DimResult`s).
    """
    violations: list[dict] = []
    try:
        tree = ET.parse(svg_path)
    except (FileNotFoundError, ET.ParseError) as e:
        return [{"severity": "error", "check": "svg_parse",
                 "object": svg_path, "message": str(e)}]

    def walk(elem, inherited_fill):
        fill = elem.get("fill", inherited_fill)
        m = re.search(r"fill:\s*([^;]+)", elem.get("style", ""))
        if m:
            fill = m.group(1).strip()
        if elem.tag.replace(_SVG_NS, "") == "text" and fill in (None, "none", ""):
            layer_id = elem.get("id") or "?"
            violations.append({
                "severity": "error",
                "check": "text_no_fill",
                "object": layer_id,
                "message": (
                    f"<text> element id='{layer_id}' has fill='{fill}'; "
                    f"glyphs will render as thick outlines, not filled. "
                    f"Set fill_color on the SVG layer when exporting."
                ),
            })
        for child in elem:
            walk(child, fill)

    walk(tree.getroot(), None)

    sidecar = os.path.splitext(svg_path)[0] + ".dims.json"
    if os.path.isfile(sidecar):
        try:
            with open(sidecar) as f:
                annotations = json.load(f)
            for name, meta in annotations.items():
                dim = DimResult(
                    shape=None,
                    label_str=meta.get("label_str", ""),
                    measured_length=meta.get("measured_length") or 0.0,
                )
                for issue in _helper_lint([dim]):
                    if issue.code == "label_vs_measured":
                        violations.append(_violation(issue, name))
        except Exception as exc:
            violations.append({
                "severity": "warning",
                "check": "sidecar_read",
                "object": sidecar,
                "message": f"Could not read sidecar: {exc}",
            })

    return violations


def lint_drawing(session, svg_path: str = "") -> str:
    """Run structural drawing checks; return JSON with a `violations` list.

    Args:
        svg_path: if given, lint the SVG file at this path (mode 2). Otherwise
            lint the live session (mode 1).

    Returns:
        JSON: {"violations": [{severity, check, object, message}, ...]}
    """
    violations = _lint_svg(svg_path) if svg_path else _lint_session(session)
    return json.dumps({"violations": violations}, indent=2)
