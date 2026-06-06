# Feasibility: micropython-wasm as a sandbox for build123d

**Status:** assessment only — no implementation.
**Context:** [Simon Willison, "Running Python code in a sandbox with MicroPython and WASM"](https://simonwillison.net/2026/Jun/6/micropython-in-a-sandbox/) (6 Jun 2026), announcing the [`micropython-wasm`](https://github.com/simonw/micropython-wasm) alpha package.

## Verdict

Running build123d *inside* micropython-wasm is **not feasible as a drop-in**, and the one
technically-possible variant (a hybrid sandbox) defeats its own purpose. The realistic
security wins for this project lie in OS-level isolation of the existing worker subprocess —
which the codebase is already architected for.

---

## What micropython-wasm is

A Python package that bundles a custom MicroPython interpreter compiled to a ~362 KB
WebAssembly (WASI) blob and runs it via **wasmtime**. Its strengths:

- Clean PyPI install with binary wheels (wasmtime ships them).
- **Memory limits** native to wasmtime; **CPU limits** via wasmtime "fuel" (instruction budget,
  ~20 M default in the article).
- Default-deny capabilities: no filesystem, no network, no env inheritance unless explicitly granted.
- Host-function interop (a small C shim, ~78 lines, marshals values across the boundary as JSON).
- Persistent interpreter state via a resident VM that blocks on a `__session_next__()` host
  function pulling code off a queue.

Its design target is **pure-Python plugin logic** — Simon's example is "fetch JSON → reformat
into a list of dicts → insert rows." build123d is the opposite kind of workload.

---

## 1. The categorical blocker: build123d is a thin veneer over a C++ kernel

build123d's dependency chain:

```
build123d  →  OCP (pybind11 bindings)  →  OpenCASCADE (libTK*.so, C++)  +  numpy (C ext)
```

`security.py` already encodes this reality. It maintains a separate `OCP_ALLOWLIST` for the
OpenCASCADE Python bindings, and its transitive-safety checker explicitly bails on anything
that isn't pure Python:

```python
if not spec.origin.endswith(".py"):
    return None  # C extension or built-in — no AST to check
```

MicroPython-WASM runs a lean interpreter with **no CPython C-API, no pybind11 ABI, and no
`dlopen` of native shared libraries**. It therefore cannot `import OCP`, and cannot `import
numpy` (also in the allowlist, also a C extension). build123d cannot run inside the sandbox at
all. This is an ABI wall, not a tuning problem (fuel size / memory cap are irrelevant).

## 2. Compiling OCCT itself to WASM is a different (huge) project

OpenCASCADE *has* been compiled to WASM before (e.g. `opencascade.js`), but:

- Those target **Emscripten/browser**, not the **WASI/wasmtime** runtime micropython-wasm uses.
- You would need MicroPython *and* OCCT in the same WASM module, plus bindings bridging them.
  OCP's pybind11 bindings target CPython's C-API, so they would need a ground-up rewrite
  against MicroPython's very different C API.
- That is building a bespoke CAD-in-WASM runtime — a multi-month toolchain effort,
  categorically different from "adopt micropython-wasm."

## 3. The hybrid variant is possible but low-value

You could run only user arithmetic / control-flow inside WASM and marshal geometry calls out
to a trusted build123d worker. But **the dangerous surface *is* the geometry layer** — file
export, `import_cad_file`, and any build123d internal that touches the filesystem. Those would
run *outside* the sandbox with full privileges. You would be sandboxing `for i in range(10)`
while leaving the C++ kernel and all I/O exposed. `security.md` already flags this exact gap
("Build123d internals … could be called from user code"). WASM does not help here.

## 4. The project already implements Simon's own top recommendation

`security.md`'s top hardening recommendation is:

> Run each execute() call in a subprocess … the subprocess can be killed hard on timeout.

`worker.py` already does exactly this: a `spawn`-context subprocess holds the `Session`,
communicates over a `Pipe`, and is **`SIGKILL`-ed and restarted on timeout**
(`WorkerSession._call`). Structurally this is the *same* pattern Simon describes building — a
resident interpreter plus a request/reply queue — reached independently. For a C++-kernel
workload, OS process isolation is the correct and only realistic boundary; WASM is the wrong tool.

## 5. Where the real wins are (no WASM needed)

The gaps a WASM sandbox might be expected to close, and the pragmatic OS-level fix for each
within the *existing* worker architecture:

| Gap (`security.md`) | WASM would fix? | Realistic fix in current architecture |
|---|---|---|
| **Memory exhaustion** (`[0]*10**10`) | yes (linear-mem cap) | `resource.setrlimit(RLIMIT_AS)` in `worker_main` before serving — ~5 lines |
| **CPU** beyond wall-clock | yes (fuel) | already have hard `SIGKILL` timeout; could add `RLIMIT_CPU` |
| **Filesystem** (worker inherits full FS) | partial (WASI preopen) | run worker in container/namespace, or seccomp; read-only mount + one output dir |
| **Network** | yes (default-deny) | `--network none` container, or seccomp socket block |

`worker_main` is a single chokepoint where per-process rlimits / seccomp can be applied.

---

## Bottom line

- **Run build123d in micropython-wasm:** no — C-extension ABI wall (OCP + numpy).
- **Hybrid sandbox:** technically yes, security value ≈ zero — the risky surface stays outside.
- **Borrow ideas:** already present — `WorkerSession` is Simon's persistent-session pattern;
  fuel has no clean CPython equivalent, so wall-clock + `RLIMIT_CPU` is the substitute.
- **Highest-ROI next step (independent of this article):** add `setrlimit(RLIMIT_AS)` to
  `worker_main` to close the one un-mitigated, easy-to-trigger DoS (memory exhaustion), entirely
  within the existing architecture.
