"""Persistent worker subprocess and parent-side proxy for build123d-mcp sessions.

Architecture:
  WorkerSession (parent)  ←── multiprocessing.Pipe ──→  worker_main (child)
                                                             └─ Session + tools

The worker process owns the Session and calls all tool functions directly —
no forking, no namespace serialization per call. OCC/TBB threads are confined
to the worker; the parent process never touches OCC at all.

Timeout is managed at the WorkerSession level: if conn.poll() expires the
parent kills the worker with SIGKILL and restarts it (fresh session).
Within the worker, Session.execute() also applies a SIGALRM guard so that
a hanging execute() call returns an error rather than blocking indefinitely.

InProcessSession (end of this module) is the no-subprocess fallback for MCP
hosts that block child-process creation (#143). It trades away the isolation
above: the Session and OCC run inside the server process, with no crash
containment and no operation timeouts.
"""

import multiprocessing
from collections.abc import Callable
from typing import Any, NamedTuple

_WORKER_READY_TIMEOUT = 60  # seconds to wait for worker import + ready signal


def _build_session(
    library_path: str,
    exec_timeout: int,
    allow_all_imports: bool,
    extra_allowed_imports: tuple[str, ...],
) -> tuple[Any, Any]:
    """Apply security overrides and build the Session (+ optional library index).

    Shared by worker_main (subprocess mode) and InProcessSession so a future
    setup step cannot be added to one mode and forgotten in the other.
    """
    if allow_all_imports or extra_allowed_imports:
        import build123d_mcp.security as _sec

        if allow_all_imports:
            _sec.ALLOW_ALL_IMPORTS = True
        if extra_allowed_imports:
            _sec.EXTRA_ALLOWED_IMPORTS.update(extra_allowed_imports)

    from build123d_mcp.session import Session

    session = Session(exec_timeout=exec_timeout)
    library_index = None
    if library_path:
        from build123d_mcp.tools.library import _LibraryIndex

        library_index = _LibraryIndex(library_path)
    return session, library_index


def worker_main(
    conn: Any,
    library_path: str = "",
    exec_timeout: int = 120,
    allow_all_imports: bool = False,
    extra_allowed_imports: tuple[str, ...] = (),
) -> None:
    """Entry point run in the worker subprocess.

    Loops receiving requests until the parent closes the connection.
    """
    session, library_index = _build_session(
        library_path, exec_timeout, allow_all_imports, extra_allowed_imports
    )

    conn.send({"ready": True})

    while True:
        try:
            request = conn.recv()
        except EOFError:
            break

        op = request["op"]
        args = request.get("args", {})

        try:
            result = _dispatch(session, op, args, library_index)
            conn.send({"ok": True, "result": result})
        except Exception as exc:
            conn.send({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


# --------------------------------------------------------------------------- #
# Op table — single source of truth for worker-routed operations (issue #220). #
# --------------------------------------------------------------------------- #

# Parent-side time budgets (seconds). Geometry-heavy queries (boolean ops,
# per-face walks, BREP analysis, part construction) can legitimately exceed
# 10s on complex parts, and a timeout here kills the worker and destroys all
# session state — so the budget errs generous (issue #214). _SHORT_TIMEOUT is
# only for ops that read session bookkeeping without touching geometry kernels.
_RENDER_TIMEOUT = 120
_EXPORT_TIMEOUT = 60
_INTERFERENCE_TIMEOUT = 30
_GEOMETRY_TIMEOUT = 60
_SHORT_TIMEOUT = 10


def _exec_budget(ws: "WorkerSession") -> int:
    return ws._exec_timeout


def _import_cad_budget(ws: "WorkerSession") -> int:
    # STEP import of heavy geometry (threads, gears — lots of BSpline faces)
    # can outlast _EXPORT_TIMEOUT, and a timeout kills the whole session.
    # Honour the user's exec-timeout knob (--exec-timeout /
    # BUILD123D_EXEC_TIMEOUT) when it is the larger budget (#229).
    return max(_EXPORT_TIMEOUT, ws._exec_timeout)


def _load_part_budget(ws: "WorkerSession") -> int:
    # Library part scripts can build heavy geometry (threads, gears) just
    # like a STEP import, so honour the exec-timeout knob here too (#229).
    return max(_GEOMETRY_TIMEOUT, ws._exec_timeout)


class _OpSpec(NamedTuple):
    """One worker-routed operation.

    handler: (session, args, library_index) -> result, run inside the worker.
    timeout: parent-side wait budget in seconds, or a callable on the
        WorkerSession for budgets derived from the exec-timeout knob.
    params: positional parameter order of the generated WorkerSession proxy
        method — the wire-interface documentation. Optionals a caller omits
        fall through to the tool function's own defaults.
    """

    handler: "Callable[[Any, dict, Any], Any]"
    timeout: "int | Callable[[WorkerSession], int]"
    params: tuple[str, ...]


def _tool(path: str) -> "Callable[[Any, dict, Any], Any]":
    """Handler for the common case: lazily import ``module:function`` and call
    ``fn(session, **args)``. Wire arg names must equal the function's parameter
    names, with defaults living on the function itself."""
    module_name, _, func_name = path.partition(":")

    def handler(session: Any, args: dict, library_index: Any) -> Any:
        import importlib

        fn = getattr(importlib.import_module(module_name), func_name)
        return fn(session, **args)

    return handler


# --- Ops with logic beyond fn(session, **args) ---


def _op_execute(session: Any, args: dict, library_index: Any) -> Any:
    return session.execute(args["code"])


def _op_objects_types(session: Any, args: dict, library_index: Any) -> Any:
    return session.objects_types()


def _op_save_snapshot(session: Any, args: dict, library_index: Any) -> Any:
    name = args["name"]
    session.save_snapshot(name)
    saved = (["current_shape"] if session.current_shape is not None else []) + list(
        session.snapshots[name]["objects"].keys()
    )
    return f"Snapshot '{name}' saved. Geometry captured: {', '.join(saved) if saved else 'none'}."


def _op_restore_snapshot(session: Any, args: dict, library_index: Any) -> Any:
    name = args["name"]
    try:
        session.restore_snapshot(name)
    except KeyError as e:
        return f"Error: {e}"
    restored = (["current_shape"] if session.current_shape is not None else []) + list(
        session.objects.keys()
    )
    return f"Snapshot '{name}' restored. Active geometry: {', '.join(restored) if restored else 'none'}."


def _op_reset(session: Any, args: dict, library_index: Any) -> Any:
    session.reset()
    return "Session reset."


def _op_search_library(session: Any, args: dict, library_index: Any) -> Any:
    if library_index is None:
        return "No part library configured."
    from build123d_mcp.tools.library import search_library

    return search_library(library_index, args.get("query", ""))


def _op_load_part(session: Any, args: dict, library_index: Any) -> Any:
    if library_index is None:
        return "No part library configured."
    from build123d_mcp.tools.library import load_part

    return load_part(session, library_index, args["name"], args.get("params", ""))


def _op_view_axes(session: Any, args: dict, library_index: Any) -> Any:
    # Pure helper: takes no session; sequence args normalised to tuples.
    from build123d_mcp.tools.view_axes import view_axes

    return view_axes(
        tuple(args["viewport_origin"]),
        tuple(args.get("viewport_up", (0.0, 1.0, 0.0))),
        tuple(args.get("look_at", (0.0, 0.0, 0.0))),
    )


def _op_render_drawing(session: Any, args: dict, library_index: Any) -> Any:
    # Pure helper: rasterises an SVG file from disk; takes no session.
    from build123d_mcp.tools.render_drawing import render_drawing

    return render_drawing(args["svg_path"], args.get("width", 0), args.get("save_to", ""))


_T = "build123d_mcp.tools"

_OPS: dict[str, _OpSpec] = {
    "execute": _OpSpec(_op_execute, _exec_budget, ("code",)),
    "render_view": _OpSpec(
        _tool(f"{_T}.render:render_view"),
        _RENDER_TIMEOUT,
        (
            "direction",
            "objects",
            "quality",
            "clip_plane",
            "clip_at",
            "azimuth",
            "elevation",
            "save_to",
            "format",
            "label_objects",
            "highlights",
            "colors",
            "mode",
        ),
    ),
    "export_file": _OpSpec(
        _tool(f"{_T}.export:export_file"),
        _EXPORT_TIMEOUT,
        ("filename", "format", "object_name"),
    ),
    "interference": _OpSpec(
        _tool(f"{_T}.interference:interference"),
        _INTERFERENCE_TIMEOUT,
        ("object_a", "object_b"),
    ),
    "measure": _OpSpec(
        _tool(f"{_T}.measure:measure"),
        _GEOMETRY_TIMEOUT,
        ("object_name", "density", "material"),
    ),
    "clearance": _OpSpec(
        _tool(f"{_T}.measure:clearance"), _GEOMETRY_TIMEOUT, ("object_a", "object_b")
    ),
    "cross_sections": _OpSpec(
        _tool(f"{_T}.cross_sections:cross_sections"),
        _GEOMETRY_TIMEOUT,
        ("object_name", "axis", "num_slices"),
    ),
    "save_snapshot": _OpSpec(_op_save_snapshot, _GEOMETRY_TIMEOUT, ("name",)),
    "restore_snapshot": _OpSpec(_op_restore_snapshot, _SHORT_TIMEOUT, ("name",)),
    "reset": _OpSpec(_op_reset, _SHORT_TIMEOUT, ()),
    "search_library": _OpSpec(_op_search_library, _SHORT_TIMEOUT, ("query",)),
    "load_part": _OpSpec(_op_load_part, _load_part_budget, ("name", "params")),
    "diff_snapshot": _OpSpec(
        _tool(f"{_T}.diff:diff_snapshot"),
        _GEOMETRY_TIMEOUT,
        ("snapshot_a", "snapshot_b", "format"),
    ),
    "session_state": _OpSpec(_tool(f"{_T}.session_state:session_state"), _SHORT_TIMEOUT, ()),
    "objects_types": _OpSpec(_op_objects_types, _SHORT_TIMEOUT, ()),
    "health_check": _OpSpec(_tool(f"{_T}.health_check:health_check"), _RENDER_TIMEOUT, ()),
    "last_error": _OpSpec(_tool(f"{_T}.last_error:last_error"), _SHORT_TIMEOUT, ()),
    "shape_compare": _OpSpec(
        _tool(f"{_T}.shape_compare:shape_compare"), _GEOMETRY_TIMEOUT, ("object_a", "object_b")
    ),
    "import_cad_file": _OpSpec(
        _tool(f"{_T}.import_step:import_cad_file"), _import_cad_budget, ("path", "name")
    ),
    "inspect_drawing": _OpSpec(
        _tool(f"{_T}.inspect_drawing:inspect_drawing"), _SHORT_TIMEOUT, ("objects", "svg_path")
    ),
    "view_axes": _OpSpec(
        _op_view_axes, _SHORT_TIMEOUT, ("viewport_origin", "viewport_up", "look_at")
    ),
    "lint_drawing": _OpSpec(
        _tool(f"{_T}.lint_drawing:lint_drawing"),
        _SHORT_TIMEOUT,
        ("svg_path", "drawing_scale", "view_shape_names"),
    ),
    "render_drawing": _OpSpec(
        _op_render_drawing, _RENDER_TIMEOUT, ("svg_path", "width", "save_to")
    ),
    "save_drawing_annotations": _OpSpec(
        _tool(f"{_T}.save_drawing_annotations:save_drawing_annotations"),
        _SHORT_TIMEOUT,
        ("svg_path",),
    ),
    "align_check": _OpSpec(
        _tool(f"{_T}.align_check:align_check"),
        _GEOMETRY_TIMEOUT,
        ("object_a", "object_b", "axis", "mode"),
    ),
    "resolve": _OpSpec(
        _tool(f"{_T}.resolve:resolve"), _GEOMETRY_TIMEOUT, ("object_name", "selector", "label")
    ),
    "suggest_view_layout": _OpSpec(
        _tool(f"{_T}.suggest_view_layout:suggest_view_layout"),
        _GEOMETRY_TIMEOUT,
        (
            "object_name",
            "page_w",
            "page_h",
            "scale",
            "views",
            "title_block_w",
            "title_block_h",
            "margin",
            "extents",
            "centroid",
        ),
    ),
    "script": _OpSpec(_tool(f"{_T}.script:script"), _SHORT_TIMEOUT, ("save_to",)),
    "analyze_printability": _OpSpec(
        _tool(f"{_T}.analyze_printability:analyze_printability"),
        _GEOMETRY_TIMEOUT,
        (
            "object_name",
            "support_angle",
            "nozzle",
            "min_perimeters",
            "build_volume",
            "bed_tol",
            "min_feature",
        ),
    ),
}


def _dispatch(session: Any, op: str, args: dict, library_index: Any) -> Any:
    spec = _OPS.get(op)
    if spec is None:
        raise ValueError(f"Unknown operation: '{op}'")
    return spec.handler(session, args, library_index)


class WorkerSession:
    """Parent-side proxy to the persistent worker subprocess.

    Exposes the same interface as Session so server.py can use either.
    """

    def __init__(
        self,
        exec_timeout: int = 120,
        library_path: str = "",
        allow_all_imports: bool = False,
        extra_allowed_imports: tuple[str, ...] = (),
    ) -> None:
        self._exec_timeout = exec_timeout
        self._library_path = library_path
        self._allow_all_imports = allow_all_imports
        self._extra_allowed_imports = tuple(extra_allowed_imports)
        self._conn: Any = None
        self._proc: Any = None
        self._start_worker()

    @property
    def has_library(self) -> bool:
        """Whether a part library was configured (drives search_library/load_part)."""
        return bool(self._library_path)

    def _start_worker(self) -> None:
        ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe()
        self._proc = ctx.Process(
            target=worker_main,
            args=(
                child_conn,
                self._library_path,
                self._exec_timeout,
                self._allow_all_imports,
                self._extra_allowed_imports,
            ),
            daemon=True,
        )
        self._proc.start()
        child_conn.close()
        self._conn = parent_conn

        if not self._conn.poll(_WORKER_READY_TIMEOUT):
            exitcode = self._proc.exitcode  # read before kill: None means still running
            self._proc.kill()
            self._proc.join(5)
            detail = (
                f"the worker exited with code {exitcode} before signalling ready"
                if exitcode is not None
                else f"the worker did not signal ready within {_WORKER_READY_TIMEOUT}s"
            )
            raise RuntimeError(
                f"Worker process failed to start: {detail}. If your MCP host blocks "
                "subprocess creation (seen with sandboxed hosts on Windows, issue #143), "
                "relaunch the server with --in-process or BUILD123D_IN_PROCESS=1 — a "
                "degraded mode without crash containment or operation timeouts."
            )
        self._conn.recv()  # consume the ready signal

    def _kill_worker(self) -> None:
        try:
            self._proc.kill()
            self._proc.join(5)
        except Exception:
            pass

    def _call(self, op: str, args: dict, timeout: int) -> Any:
        if not self._proc.is_alive():
            self._start_worker()
            raise RuntimeError(
                "Worker crashed; session restarted. All session state (variables, "
                "shapes, named objects, snapshots) has been lost — re-run your setup code."
            )

        self._conn.send({"op": op, "args": args})

        if not self._conn.poll(timeout):
            self._kill_worker()
            self._start_worker()
            from build123d_mcp.security import ExecutionTimeout

            if op == "execute":
                raise ExecutionTimeout(
                    f"Code exceeded the {timeout}s execution time limit. "
                    f"All session state (variables, shapes, named objects) has been lost — "
                    f"the worker was restarted.\n"
                    f"For complex builds with many booleans (IsoThread, multi-body fillets, "
                    f"high-face-count solids): write the build as a plain Python script and "
                    f"run it with Bash, then load the result with import_cad_file() and use "
                    f"render_view() / measure() here. "
                    f"The timeout limit can be raised with the --exec-timeout flag or "
                    f"BUILD123D_EXEC_TIMEOUT env var."
                )
            raise RuntimeError(
                f"Operation '{op}' timed out after {timeout}s. The worker was killed "
                f"and restarted — all session state (variables, shapes, named objects, "
                f"snapshots) has been lost. Re-run your setup code."
            )

        try:
            response = self._conn.recv()
        except EOFError:
            self._start_worker()
            raise RuntimeError(
                "Worker process crashed unexpectedly; session restarted. All session "
                "state (variables, shapes, named objects, snapshots) has been lost — "
                "re-run your setup code."
            )

        if response["ok"]:
            return response["result"]
        raise RuntimeError(response["error"])

    # --- Session-compatible interface ---
    #
    # Proxy methods are generated from _OPS via __getattr__: positional args
    # map through spec.params, the timeout comes from the table, and optionals
    # the caller omits fall through to the tool-function defaults in the
    # worker. Only ops needing parent-side behaviour beyond "send and wait"
    # (execute, reset) are written out explicitly.

    def execute(self, code: str) -> str:
        from build123d_mcp.security import ExecutionTimeout

        try:
            return self._call("execute", {"code": code}, self._exec_timeout)
        except (RuntimeError, ExecutionTimeout) as e:
            return f"Error: {e}"

    def reset(self) -> str:
        if not self._proc.is_alive():
            self._start_worker()
            return "Session reset."
        return self._call("reset", {}, _SHORT_TIMEOUT)

    def __getattr__(self, name: str) -> Any:
        # Fires only for attributes not found normally, so explicit methods
        # and instance attributes take precedence. The Any return type also
        # lets mypy accept the generated methods at call sites.
        spec = _OPS.get(name)
        if spec is None:
            raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

        def proxy(*args: Any, **kwargs: Any) -> Any:
            if len(args) > len(spec.params):
                raise TypeError(f"{name}() takes at most {len(spec.params)} positional argument(s)")
            payload = dict(zip(spec.params, args))
            for key in kwargs:
                if key in payload:
                    raise TypeError(f"{name}() got multiple values for argument '{key}'")
                if key not in spec.params:
                    raise TypeError(f"{name}() got an unexpected keyword argument '{key}'")
            payload.update(kwargs)
            timeout = spec.timeout(self) if callable(spec.timeout) else spec.timeout
            return self._call(name, payload, timeout)

        proxy.__name__ = name
        return proxy


class InProcessSession(WorkerSession):
    """WorkerSession-compatible session that skips the worker subprocess.

    Fallback for MCP hosts that block subprocess creation, where the spawn'd
    worker never signals ready (#143: Codex desktop on Windows). The Session
    and all OCC/VTK work live in the server process, so this mode is degraded
    by design:

    - no crash containment — an OCCT segfault kills the whole MCP server;
    - no operation timeouts — a runaway execute() blocks the server (the
      in-Session SIGALRM guard still applies on Unix main threads, but not
      on Windows, which is exactly where this mode is needed);
    - OCC/TBB threads share the process with the MCP event loop;
    - platform render helpers still spawn subprocesses where required
      (macOS VTK guard, Linux xvfb) and may also fail under a host that
      blocks spawning — Windows renders in-process and is unaffected.

    Enabled via --in-process / BUILD123D_IN_PROCESS=1.
    """

    def _start_worker(self) -> None:
        self._session, self._library_index = _build_session(
            self._library_path,
            self._exec_timeout,
            self._allow_all_imports,
            self._extra_allowed_imports,
        )

    def _kill_worker(self) -> None:
        pass

    def reset(self) -> str:
        # The base method short-circuits via self._proc.is_alive(); there is
        # no process here, so always dispatch the real reset.
        return self._call("reset", {}, _SHORT_TIMEOUT)

    def _call(self, op: str, args: dict, timeout: int) -> Any:
        # Same error contract as the worker path: tool exceptions surface as
        # RuntimeError("TypeName: message"), mirroring worker_main's error
        # envelope. (Session.execute() handles ExecutionTimeout internally
        # and returns an error string, so no special-casing is needed here.)
        try:
            return _dispatch(self._session, op, args, self._library_index)
        except Exception as exc:
            raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc
