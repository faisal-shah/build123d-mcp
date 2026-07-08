import json
import re

_HINTS: list[tuple[list[str], str]] = [
    (
        [
            r"B-rep is not well-formed",
            r"BRepCheck failed",
            r"brep_invalid_face",
        ],
        "Gate failure: malformed B-rep face. Run `locate_gate_defects()` to get "
        "the face index and coordinates, then rebuild or replace that local patch "
        "explicitly in `execute()`. Verify with `export(..., 'step')`, not "
        "`validate()` alone.",
    ),
    (
        [r"\bopen edge", r"not watertight", r"unsewn", r"mesh_open_edge"],
        "Gate failure: open boundary or non-conformal face junction. Run "
        "`locate_gate_defects()` for the edge midpoint, then add the missing face, "
        "re-sew the local shell, or rebuild the adjacent faces in `execute()`. "
        "Use snapshots and re-check the written STEP with `export()`.",
    ),
    (
        [
            r"non-manifold edge",
            r"mesh_nonmanifold_edge",
            r"faces meet >2-ways",
            r"self-touch",
            r"coincident face",
        ],
        "Gate failure: self-touching/coincident geometry. Run `locate_gate_defects()` "
        "for the edge coordinate, then redo the boolean or add explicit clearance/"
        "relief so the surfaces form a true 2-manifold. Keep the repair visible in "
        "`execute()` code and gate the exported STEP.",
    ),
    (
        [r"non-manifold vertex", r"mesh_nonmanifold_vertex", r"corner-to-corner"],
        "Gate failure: bodies touch at a single point. Separate them or add enough "
        "overlap/material that the result fuses into one manifold solid; then verify "
        "with the export gate.",
    ),
    (
        [
            r"finer mesh deflection",
            r"refined.*untriangulated",
            r"mesh_refined_untriangulated_face",
        ],
        "Gate failure: fragile face only fails under finer tessellation. Use "
        "`locate_gate_defects()` for the face index/center, then simplify, re-patch, "
        "or re-sew that local face and verify the written STEP.",
    ),
    (
        [
            r"vertex.*deflection",
            r"mesh_vertex_deflection_defect",
            r"misses its BREP vertex",
        ],
        "Gate failure: a tessellated edge endpoint misses its BREP vertex. Rebuild "
        "the local patch so topology and geometry agree; tolerance-only fixes often "
        "look closed in memory but still fail the exported STEP gate.",
    ),
    (
        [r"AttributeError.*'ShapeList'.*has no attribute", r"'ShapeList'.*has no attribute"],
        "ShapeList is not a Part — `Box() + Cylinder()` concatenates shapes into a list "
        "rather than fusing them. Fix: use `Part() + Box(...) + Cylinder(...)` to get a "
        "fused solid, or call `.fuse()` explicitly, or iterate over `.solids()` for "
        "per-solid access.",
    ),
    (
        [r"NoneType.*has no attribute", r"AttributeError.*None"],
        "Shape is None. If you used BuildPart context manager, access the result with "
        "`.part` (e.g. `result = bp.part`). In algebra mode, assign directly: "
        "`result = Box(10,10,10) - Cylinder(3,12)`.",
    ),
    (
        [r"None context requested", r"No context.*requested"],
        "build123d algebra mode requires no context manager — create shapes directly "
        "and assign to `result` or call `show()`. Remove `with BuildPart()` wrappers "
        "if you're using operator-based construction.",
    ),
    (
        [r"cq\.", r"Workplane", r"CadQuery"],
        "CadQuery syntax detected. build123d uses a different API: "
        "`Box(w,h,d)` not `cq.Workplane().box(w,h,d)`. "
        "Replace `.translate()` with `.move(Location((x,y,z)))`, "
        "`.rotate()` with `.rotate(Axis.Z, angle)`, "
        "and `.union()`/`.cut()` with `+`/`-` operators.",
    ),
    (
        [r"TypeError.*Location", r"Location.*argument"],
        "Location syntax: pass a tuple — `Location((x, y, z))` not `Location(x, y, z)`. "
        "For combined translation + rotation: `Location((x,y,z), (rx,ry,rz))`.",
    ),
    (
        [r"[Ff]illet.*edge", r"[Ee]dge.*fillet", r"ValueError.*edges.*fillet"],
        "Fillet edge selection: edges must be non-tangent and the radius must be smaller "
        "than the adjacent wall thickness. Select edges with "
        "`shape.edges().filter_by(Axis.Z)` or index them with `shape.edges()[0]`. "
        "Avoid `shape.edges()` (all edges) on complex shapes — pick specific ones.",
    ),
    (
        [
            r"NameError.*\b(Box|Cylinder|Sphere|Cone|Torus|Extrude|BuildPart|"
            r"BuildSketch|Align|Axis|Location|Plane|Vector|Color|Compound|Shell|"
            r"Fillet|Chamfer|extrude|loft|sweep)\b"
        ],
        "build123d name not in scope. Add `from build123d import *` at the top of "
        "the execute() call. If it was imported in a previous call, re-run that import "
        "or include it in this snippet.",
    ),
    (
        [r"Call to '.*' is not allowed", r"Access to dunder attribute .* is not allowed"],
        "Blocked by the execute() sandbox — this is a call/attribute block, NOT an import. "
        "getattr, vars, eval, exec, compile, open and explicit dunder access are blocked; "
        "hasattr() and dir() ARE allowed. Probe attributes with hasattr/try-except/isinstance "
        "and use operators or syntax instead of explicit dunders. In a trusted environment the "
        "server can run with --no-sandbox / BUILD123D_NO_SANDBOX=1 to lift all sandbox layers.",
    ),
    (
        [
            r"ImportError",
            r"Import of .* is not allowed",
            r"not allowed.*import",
            r"import.*not allowed",
        ],
        "Import blocked. Allowed modules include: build123d, bd_warehouse, math, numpy, "
        "json, re, collections, itertools, functools, copy, typing, dataclasses, enum, "
        "and most OCP geometry sub-modules (OCP.gp, OCP.BRepGProp, OCP.TopExp, etc.). "
        "Blocked: os, sys, pathlib, subprocess, socket (file/network/shell access). "
        "Pure-Python packages on sys.path whose imports stay within the allowed list "
        "above are permitted automatically — no config needed. "
        "For project geometry (e.g. a build_shaft() function), export to STEP and use "
        "import_cad_file(path, name) to load it without any import restrictions. "
        "To force-allow a package that imports os or similar, use --allow-imports or "
        "BUILD123D_ALLOW_IMPORTS env var.",
    ),
    (
        [r"Constraint failed", r"AssertionError"],
        "Constraint failed — a dimension is physically impossible. Common causes: "
        "fillet radius larger than the adjacent face, hole diameter larger than "
        "the wall, or zero/negative dimensions. Check all numeric parameters.",
    ),
    (
        [r"empty.*[Ss]hape", r"[Ss]hape.*empty", r"degenerate", r"no.*solid"],
        "Degenerate or empty shape after boolean operation. The cutter probably doesn't "
        "overlap the base, or the result has zero volume. Verify positions with "
        "measure(bounding_box) on both shapes before the boolean.",
    ),
    (
        [r"ExecutionTimeout"],
        "Execution timed out. Likely causes: very high-resolution mesh "
        "(lower angular_deflection), deeply nested boolean operations, or an "
        "infinite loop. Simplify the geometry or break it into smaller steps.",
    ),
    (
        [r"\.part\b"],
        "If you see an error referencing `.part`: in BuildPart context manager usage "
        "you must explicitly read `context.part` to get the Shape. "
        "In algebra mode (recommended) you don't need `.part` at all.",
    ),
]


def repair_hints(error_text: str) -> str:
    matches = []
    for patterns, hint in _HINTS:
        if any(re.search(p, error_text) for p in patterns):
            matches.append(hint)

    if not matches:
        matches.append(
            "No specific hint matched. Call last_error() for the exact line and "
            "exception, then check: (1) shapes are non-None before boolean ops, "
            "(2) `from build123d import *` is in scope, "
            "(3) Location uses a tuple argument."
        )

    return json.dumps({"hints": matches}, indent=2)
