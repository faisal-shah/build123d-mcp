"""find_holes / find_bosses — feature recognition on session objects (#264).

Thin wrappers around ``build123d_drafting.find_holes`` / ``find_bosses``
(pzfreo/build123d-drafting-helpers#87): resolve the named session object,
run the recognition, serialise the dataclass records to JSON.
"""

import json
from dataclasses import asdict

from build123d_mcp.tools.measure import _resolve_shape


def _record(feature) -> dict:
    rec = asdict(feature)
    for key in ("axis", "location"):
        rec[key] = [round(c, 4) for c in rec[key]]
    return rec


def find_holes(session, object_name: str = "") -> str:
    """Recognise drilled holes on a named session object."""
    from build123d_drafting import find_holes as _find_holes

    try:
        shape = _resolve_shape(session, object_name)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    holes = [_record(h) for h in _find_holes(shape)]
    return json.dumps({"count": len(holes), "holes": holes})


def find_bosses(session, object_name: str = "") -> str:
    """Recognise external cylindrical bosses on a named session object."""
    from build123d_drafting import find_bosses as _find_bosses

    try:
        shape = _resolve_shape(session, object_name)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    bosses = [_record(b) for b in _find_bosses(shape)]
    return json.dumps({"count": len(bosses), "bosses": bosses})
