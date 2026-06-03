# Create Engineering Drawing from build123d Geometry

Use this skill when asked to create or fix an engineering drawing for a
build123d component. The drawings live in a project `scripts/drawings/`
directory and export to a `drawings/` output directory.

---

## Step 0 — Understand the part first

Before writing any drawing code, use the MCP server to build and inspect the geometry:

```
mcp__build123d-mcp__execute  — build the part in the session
mcp__build123d-mcp__measure  — confirm volume, bbox, face count
mcp__build123d-mcp__render_view (save_to='/tmp/preview.png') — visual sanity check
```

Note the bounding-box extents. These drive layout decisions below.

---

## Step 1 — Choose views (third-angle projection)

Standard four-view layout. Page size is chosen in Step 2 based on part extents
(A4 landscape 297 × 210 mm for most parts; A3 landscape 420 × 297 mm for large ones).

| View | Camera position | Up vector | Role |
|------|-----------------|-----------|------|
| Main (front/side) | face the longest axis | +Z or +Y | primary dims |
| Left/right end | +X or -X | +Z | cross-section / bore |
| Plan (top) | +Z | +Y | footprint |
| Isometric | (80, 80, 80) | (0, 0, 1) | pictorial, no dims |

The isometric position `(80, 80, 80)` gives the standard equal-axis view. Negate one
axis (e.g. `(-80, 80, 80)`) to flip the pictorial orientation when a key feature is
otherwise hidden.

Verify axis mapping **before** placing any dimensions:

```
mcp__build123d-mcp__view_axes(viewport_origin=[...], viewport_up=[...])
```

This returns `world_X → page_X (±1), world_Y → page_X (±1)` etc.
Copy the result into the script as a comment — it is the source of truth for
coordinate helpers.

---

## Step 2 — Choose page size and scale, then project

Pick `SCALE` and page dimensions so the scaled longest dimension fits comfortably
within the usable page width (≈ page width − 20 mm margins). Use the bounding-box
`x_size / y_size / z_size` values from `measure()` in Step 0.

```python
bbox_max = max(x_size, y_size, z_size)   # longest world dimension in mm

# Rule of thumb: scaled bbox_max should be ≤ 60 % of usable page length
if   bbox_max * 2.0 <= 170:
    SCALE, PAGE_W, PAGE_H = 2.0, 297.0, 210.0   # A4 2:1  — small parts
elif bbox_max * 1.0 <= 170:
    SCALE, PAGE_W, PAGE_H = 1.0, 297.0, 210.0   # A4 1:1
elif bbox_max * 1.0 <= 260:
    SCALE, PAGE_W, PAGE_H = 1.0, 420.0, 297.0   # A3 1:1  — larger parts
else:
    SCALE, PAGE_W, PAGE_H = 0.5, 420.0, 297.0   # A3 1:2  — very large parts
```

Then project:

```python
part_scaled = part.scale(SCALE)

# look_at in scaled space for orthographic views; unscaled for iso
look_at_s = (cx * SCALE, cy * SCALE, cz * SCALE)

vis, hid = part_scaled.project_to_viewport(camera_pos, up, look_at_s)
if not list(vis):
    raise ValueError(f"project_to_viewport returned empty geometry for camera {camera_pos} — check camera position and look_at")
iso_vis, iso_hid = part.project_to_viewport((80, 80, 80), (0, 0, 1), (cx, cy, cz))
```

Place each projected compound at its sheet position:

```python
VIEW_X, VIEW_Y = 148.0, 115.0   # sheet centre for this view
view = Compound(children=list(vis)).locate(Location((VIEW_X, VIEW_Y, 0)))
view_h = Compound(children=list(hid)).locate(Location((VIEW_X, VIEW_Y, 0))) if hid else None
```

---

## Step 3 — Coordinate helpers

Write one helper per view so annotation coords are derived from world geometry,
not hardcoded page numbers. Pattern (example for a view where world_Z → page_X):

```python
# Side view: world_Z → page_X (+1), world_Y → page_Y (+1), look_at Z=z_center
def SX(z): return SV_X + z * SCALE - z_center * SCALE
def SY(y): return SV_Y + y * SCALE
```

Verify a known extent (e.g. top of part) maps to a sensible page Y before using.

---

## Step 4 — Annotate with build123d_drafting

```python
from build123d_drafting import (
    Dimension, Leader, TitleBlock,
    annotate, draft_preset, lint_drawing, place_dims, set_page,
)

draft = draft_preset(font_size=2.5, decimal_precision=1)
```

**Stacked dimensions** (use `place_dims` — it handles offset stacking automatically):

```python
dims = place_dims([
    ((x0, y_base, 0), (x1, y_base, 0), "below", "14.8"),
    ((x0, y_base, 0), (x2, y_base, 0), "below",  "7.1"),
], draft)
for i, d in enumerate(dims):
    annotate(d, f"dim_name_{i}")
```

**Leaders** (diameter callouts, part labels):

```python
ldr = Leader(
    tip=(page_x, page_y, 0),
    elbow=(elbow_x, elbow_y, 0),
    label="ø4.0 BEARING",
    draft=draft,
)
annotate(ldr, "ldr_bearing_d")
```

**Title block** (always include):

```python
tb = TitleBlock(
    "PART NAME", "DWG-NNN",
    scale="2:1",
    material="CZ121 BRASS",
    general_tolerance="ISO 2768-f",
    designed_by="PROJECT NAME",
    date="YYYY-MM-DD",
    width=150.0,
    draft=draft,
).locate(Location((126, 11, 0)))
annotate(tb, "title_block")
```

Every annotation object **must** be passed to `annotate()` — otherwise lint and
export will not see it.

---

## Step 5 — Lint gate (run before export)

Note: `lint_drawing` here is called from `build123d_drafting` (imported in Step 4),
not the MCP tool `mcp__build123d-mcp__lint_drawing`. The Python function accepts the
annotation list and scale directly; the MCP tool reads from session state instead.

```python
all_anns = list(dims) + [ldr1, ldr2, tb]
set_page(PAGE_W, PAGE_H, margin=10)   # PAGE_W / PAGE_H set in Step 2
issues = lint_drawing(all_anns, drawing_scale=SCALE)
if issues:
    for iss in issues:
        print(f"  [{iss.severity}] {iss.code}: {iss.message}")
else:
    print("Lint: OK")
```

Do not export until lint is clean (or all issues are understood and accepted).

Common lint failures and fixes:
- `label_axis_swap` — dimension endpoints are swapped (X↔Y); check coord helper signs
- `label_mismatch` — label string doesn't match the geometric distance; recheck scale
- `page_bounds` — annotation is outside the 297×210 margin; adjust view position

---

## Step 6 — Export SVG and DXF

```python
part_color = Color(0, 0, 0)
hid_color  = Color(0.5, 0.5, 0.5)
dim_color  = Color(0, 0.2, 0.7)

svg_exp = ExportSVG(margin=10)
svg_exp.add_layer("part",   line_color=part_color, line_weight=0.5)
svg_exp.add_layer("hidden", line_color=hid_color,  line_weight=0.25,
                  line_type=LineType.HIDDEN)
svg_exp.add_layer("dims",   line_color=dim_color,  fill_color=dim_color,
                  line_weight=0.05)
for shape in [view1, view2, view3, iso]:
    svg_exp.add_shape(shape, layer="part")
for shape in [s for s in [v1_h, v2_h, v3_h, iso_h] if s]:
    svg_exp.add_shape(shape, layer="hidden")
for ann in all_anns:
    svg_exp.add_shape(ann, layer="dims")
svg_exp.write(str(output_dir / "part_name.svg"))
```

Then export DXF (same layer structure; omit `line_color`, `fill_color`, and `line_type` args):

```python
from build123d import ExportDXF

dxf_exp = ExportDXF()
dxf_exp.add_layer("part",   line_weight=0.5)
dxf_exp.add_layer("hidden", line_weight=0.25)
dxf_exp.add_layer("dims",   line_weight=0.05)
for shape in [view1, view2, view3, iso]:
    dxf_exp.add_shape(shape, layer="part")
for shape in [s for s in [v1_h, v2_h, v3_h, iso_h] if s]:
    dxf_exp.add_shape(shape, layer="hidden")
for ann in all_anns:
    dxf_exp.add_shape(ann, layer="dims")
dxf_exp.write(str(output_dir / "part_name.dxf"))
```

---

## Step 7 — Verify the SVG with the MCP server

```
mcp__build123d-mcp__render_drawing(svg_path='drawings/part_name.svg', save_to='/tmp/dwg.png')
```

Send with `[SEND: /tmp/dwg.png]` for user review before moving on.

---

## Step 8 — Combine into a PDF (optional)

To assemble multiple drawing SVGs into a single multi-page PDF, rasterise each
SVG at 200 DPI using `resvg-py` and combine pages with `fpdf2`.

`PAGE_W` and `PAGE_H` are the values chosen in Step 2 (e.g. 297/210 for A4 or
420/297 for A3 landscape). Use them throughout so the PDF matches the drawing sheet.

```python
import resvg_py
from fpdf import FPDF

# PAGE_W, PAGE_H set in Step 2
fmt = "A4" if PAGE_W < 400 else "A3"

png_bytes = resvg_py.svg_to_bytes(svg_path=str(svg_path), dpi=200)

pdf = FPDF(orientation="L", unit="mm", format=fmt)
pdf.add_page()
pdf.image(tmp_png_path, x=0, y=0, w=PAGE_W, h=PAGE_H)
pdf.output("drawings/output.pdf")
```

build123d `ExportSVG` writes Y-up coordinates; the `viewBox` origin encodes
where content sits on the sheet. To recover the correct Y position for PDF
(Y-down, top-left origin):

```python
vb_y = float(svg_root.get("viewBox").split()[1])   # negative in Y-up drawing coords
assert vb_y <= 0, f"Unexpected positive viewBox Y: {vb_y} — check ExportSVG output"
pdf_y = PAGE_H - abs(vb_y)
```

If the assert fires, the SVG was generated with a different coordinate convention;
inspect the `viewBox` before placing the image.

---

## Layout rules of thumb

- Leave ≥ 12 mm between any two view outlines.
- Dimension lines below/left of the view they measure; leader elbows clear the geometry.
- Isometric goes in the corner least occupied by orthographic views (usually bottom-left or far right).
- Title block: bottom-right, 150–170 mm wide, Y anchor ≈ 11 mm from bottom.
- Don't put dimensions on the isometric — it is a pictorial only.

---

## Axis sign quick-reference

`view_axes` output drives all coordinate helpers. Common cases:

| Camera | Up | world_X | world_Y | world_Z |
|--------|----|---------|---------|---------|
| -X (front) | +Z | — | page_X (-1) | page_Y (+1) |
| +Z (plan)  | +Y | page_X (+1) | page_Y (+1) | — |
| -Y (end)   | +Z | page_X (+1) | — | page_Y (+1) |
| +Y (side)  | +Z | page_X (-1) | — | page_Y (+1) |

Signs flip when the camera is on the negative axis — always verify with
`view_axes` rather than assuming.
