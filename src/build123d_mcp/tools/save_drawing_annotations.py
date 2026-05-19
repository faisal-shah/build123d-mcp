"""save_drawing_annotations — write a sidecar .dims.json next to an SVG file.

inspect_drawing(svg_path=...) reads this sidecar when present, restoring
label text and measured-length metadata that would otherwise be lost because
build123d renders Text as filled glyph paths (not SVG <text> elements).
"""
import json
import pathlib


def save_drawing_annotations(session, svg_path: str) -> str:
    """Write session.drawing_annotations to <svg_path>.dims.json.

    Args:
        session:  the active Session object.
        svg_path: path to the SVG file being annotated (need not exist yet).

    Returns:
        A message string describing how many annotations were saved and where.
    """
    sidecar = pathlib.Path(svg_path).with_suffix(".dims.json")
    annotations = session.drawing_annotations
    with open(sidecar, "w") as f:
        json.dump(annotations, f, indent=2, default=str)
    n = len(annotations)
    return f"Saved {n} annotation(s) to {sidecar}"
