"""drafting_api — auto-generated API reference for build123d-drafting-helpers.

Exposed as the ``build123d://drafting-api`` MCP resource (#260). The reference
is built with ``inspect`` against the installed library at request time, so it
always matches the version actually importable inside execute() — there is no
hand-maintained signature list to drift when drafting-helpers releases.
"""

import inspect
from typing import Any


def _first_doc_line(obj: Any) -> str:
    doc = inspect.getdoc(obj)
    return doc.split("\n", 1)[0].strip() if doc else ""


def _signature(obj: Any) -> str:
    try:
        return str(inspect.signature(obj))
    except (ValueError, TypeError):
        return "(...)"


def _class_entry(name: str, cls: type) -> list[str]:
    lines = [f"{name}{_signature(cls)}"]
    doc = _first_doc_line(cls)
    if doc:
        lines.append(f"    {doc}")
    for mname, method in sorted(vars(cls).items()):
        if mname.startswith("_") or not callable(method):
            continue
        lines.append(f"    .{mname}{_signature(method)}")
        mdoc = _first_doc_line(method)
        if mdoc:
            lines.append(f"        {mdoc}")
    return lines


def drafting_api(session: Any) -> str:
    """Build the API reference text from the installed build123d_drafting.

    The session argument is unused — the reference depends only on the
    installed library — but kept for the uniform worker-op calling convention.
    """
    try:
        import build123d_drafting as bd
    except ImportError as exc:
        return f"Error: build123d_drafting is not installed: {exc}"

    version = getattr(bd, "__version__", "unknown")
    classes: list[str] = []
    functions: list[str] = []
    for name in getattr(bd, "__all__", sorted(dir(bd))):
        obj = getattr(bd, name, None)
        if obj is None or inspect.ismodule(obj):
            continue
        if inspect.isclass(obj):
            classes.extend(_class_entry(name, obj))
            classes.append("")
        elif callable(obj):
            functions.append(f"{name}{_signature(obj)}")
            doc = _first_doc_line(obj)
            if doc:
                functions.append(f"    {doc}")
            functions.append("")

    header = [
        f"build123d-drafting-helpers API reference (installed version: {version})",
        "Generated from the live library — signatures match what execute() imports",
        "via `from build123d_drafting import ...`.",
        "",
        "=== Classes ===",
        "",
    ]
    return "\n".join(header + classes + ["=== Functions ===", ""] + functions).rstrip() + "\n"
