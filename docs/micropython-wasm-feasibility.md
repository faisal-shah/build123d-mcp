# Feasibility: a WASM + Python sandbox for build123d

**Status:** assessment only — no implementation.
**Context:** [Simon Willison, "Running Python code in a sandbox with MicroPython and WASM"](https://simonwillison.net/2026/Jun/6/micropython-in-a-sandbox/) (6 Jun 2026), announcing the [`micropython-wasm`](https://github.com/simonw/micropython-wasm) alpha package.

> **Correction note.** An earlier version of this document claimed it was *categorically
> impossible* to run build123d in a WASM Python, on the grounds that OCP/OpenCASCADE and numpy
> are C extensions. That reasoning holds **only for MicroPython**. It is **wrong in general**:
> Pyodide (full CPython on WASM) loads C extensions compiled to WASM, and OCP/OpenCASCADE has
> already been ported — **build123d runs in Pyodide+WASM today**. This document is the corrected
> assessment.

## Verdict

- **Simon's `micropython-wasm` (the article's tool): cannot run build123d.** MicroPython has no
  CPython C-API / pybind11 ABI, so it cannot load OCP, OpenCASCADE, or numpy.
- **Pyodide + `OCP.wasm`: *can* run build123d, and does so in shipping projects today.** This is
  a genuine WASM+Python path and was wrongly dismissed before.
- **Whether it is a *stronger* sandbox is a separate question.** The real isolation boundary is
  the host runtime's capability model (Deno / WASI), **not** Pyodide itself — Pyodide has active
  critical sandbox-escape CVEs. Adopting it is a large re-platform with significant losses
  (VTK rendering, file I/O), and is roughly comparable in isolation strength to simply
  confining the existing CPython worker with OS primitives.

---

## 1. MicroPython-wasm: still no

build123d's dependency chain is:

```
build123d  →  OCP (pybind11 bindings)  →  OpenCASCADE (libTK*.so, C++)  +  numpy (C ext)
```

MicroPython-WASM runs a lean interpreter with **no CPython C-API, no pybind11 ABI, and no
`dlopen` of native libraries**. It cannot `import OCP` or `import numpy`. `security.py` already
treats OCP as a special C-extension case (`OCP_ALLOWLIST`) and its transitive checker bails on
non-`.py` modules. For MicroPython specifically, the ABI wall is real.

Simon's tool targets **pure-Python plugin logic** (his example: "fetch JSON → reformat into a
list of dicts → insert rows"), which is the opposite of build123d's C++-kernel workload.

## 2. Pyodide is different — and OCP.wasm already exists

Pyodide is **full CPython compiled to WASM (Emscripten)** and **can load C extensions compiled
to WASM** — this is how numpy/scipy run in the browser. Crucially, the CAD kernel has been
ported:

- **[`Yeicor/OCP.wasm`](https://github.com/Yeicor/OCP.wasm)** — ports OCP + OpenCASCADE (and
  lib3mf) to WASM via Pyodide. build123d runs in a browser Pyodide REPL.
- **`build123d-sandbox`** and **[yet-another-cad-viewer](https://github.com/yeicor-3d/yet-another-cad-viewer)**
  — shipping web apps running build123d fully client-side via Pyodide+WASM.
- Tracked upstream in [CadQuery Discussion #1876](https://github.com/CadQuery/cadquery/discussions/1876).

So "build123d can't run in a WASM Python" is **false**. The earlier categorical claim conflated
MicroPython's limitation with WASM-Python in general.

## 3. The catches that matter for *this* project

**a. Pyodide is browser/Node-native (Emscripten), not server-side WASI/wasmtime.** OCP.wasm runs
in a browser, or under Node/Deno — *not* in the wasmtime model Simon's article is about. Simon
explicitly rejected Pyodide server-side ("can only run in a browser or Node.js"), which is
precisely *why* he built micropython-wasm. To use OCP.wasm on a server you run **Pyodide under
Deno** (see [Simon's TIL](https://til.simonwillison.net/deno/pyodide-sandbox)) or Node.

**b. Pyodide is not itself the security boundary — and has active critical escapes.**
This is the load-bearing point:

- **CVE-2026-5752 (CVSS 9.3): Pyodide sandbox escape → root command execution** (reported
  unpatched).
- Grist's Pyodide-formula sandbox escape ("Cellbreak", Cyera Research).
- **`langchain-sandbox`** — the canonical "Pyodide under Deno" server sandbox — was
  **archived January 2026, "no longer maintained,"** recommending hosted sandbox APIs instead.
  Its own docs state *"the actual security guarantees depend on … Deno permissions."*

In other words, **Deno's (or a WASI runtime's) capability model is the wall, not Pyodide.**
"Stronger sandbox via WASM+Python" decodes to "run a WASM Python under a host that denies
network/filesystem capabilities" — which is the same *kind* of outer-boundary confinement you
can apply to the existing CPython worker.

**c. The re-platform cost is large and hits build123d-mcp specifically:**

- Replace the backend: CPython subprocess → Pyodide-under-Deno, with JS↔Python↔WASM marshaling.
- **No VTK** (the port is `cadquery-ocp-novtk`). `tools/render.py` is pyvista/VTK-based and
  **would not work** — rendering must be redone via mesh export + a JS/three.js viewer.
- **File access is unsupported** in the Pyodide sandbox model — `export`, `import_cad_file` must
  be re-plumbed through a virtual FS.
- 50–70 MB WASM payload, multi-second startup latency, Pyodide-version lag, and OCP.wasm is a
  largely-untested partial port.

## 4. The existing architecture already does the outer-boundary part

`security.md`'s top hardening recommendation — *"run each execute() call in a subprocess …
killed hard on timeout"* — is already implemented in `worker.py`: a `spawn`-context subprocess
holds the `Session`, talks over a `Pipe`, and is `SIGKILL`-ed on timeout (`WorkerSession._call`).
This is structurally the same persistent-session pattern Simon describes building.

Because the real isolation in *both* the Pyodide-under-Deno path and the OS-confined-subprocess
path comes from the **outer capability layer**, the cheaper, lower-risk win is to confine the
existing worker rather than re-platform onto WASM.

---

## Comparison

| Approach | Runs build123d? | Stronger sandbox? | Cost |
|---|---|---|---|
| **MicroPython-wasm** (Simon's tool, the article) | **No** — no C-ext support | n/a | n/a |
| **Pyodide + OCP.wasm under Deno/WASI** | **Yes** (real, shipping) | Yes vs. *today's* setup (network/FS denial) — but the boundary is Deno/WASI, not Pyodide, which has CVSS-9.3 escapes | **Large** re-platform; lose VTK rendering + file I/O; unmaintained reference tooling |
| **Current CPython worker + OS confinement** (container / seccomp / `RLIMIT_AS`) | Yes (unchanged) | Comparable isolation strength | **Small**, fits existing `worker.py` |

## Recommendations

1. **Don't pursue MicroPython-wasm** for build123d — it cannot load the kernel.
2. **Treat Pyodide + OCP.wasm as a real but major option**, justified mainly if a
   *browser-native, zero-install* deployment is itself a goal (as in build123d-sandbox). As a
   pure server-side hardening play it is high-cost and leans on a stack with active critical
   CVEs and unmaintained reference tooling.
3. **Highest-ROI hardening (independent of WASM):** confine the existing worker —
   `resource.setrlimit(RLIMIT_AS)` in `worker_main` to close the unbounded-memory DoS (the one
   easy-to-trigger gap in `security.md`), plus optional container/seccomp/`--network none` for
   the filesystem/network surface. This is where the real isolation lives in every option, at a
   fraction of the engineering cost.
