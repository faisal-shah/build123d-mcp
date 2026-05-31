"""Report installed versions of the server and its key dependencies.

Kept dependency-light (only importlib.metadata) so the `version` op stays
fast and never touches the OCC kernel.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _dist_version

# key in the returned dict -> distribution name on PyPI
_PACKAGES = {
    "build123d_mcp": "build123d-mcp",
    "build123d": "build123d",
    "build123d_drafting_helpers": "build123d-drafting-helpers",
}


def version_info() -> dict:
    """Return {key: version_string} for the server and its render-path deps.

    A package that is not installed reports "unknown" rather than raising,
    so the tool always returns a complete dict.
    """
    out: dict = {}
    for key, dist in _PACKAGES.items():
        try:
            out[key] = _dist_version(dist)
        except PackageNotFoundError:
            out[key] = "unknown"
    return out
