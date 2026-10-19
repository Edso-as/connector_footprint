---
name: pdf-to-footprint
description: 'Convert a connector/component datasheet PDF (recommended PCB layout / land pattern) into a verified KiCad .kicad_mod and CSV. Use when asked to make a footprint from a PDF, read a recommended PCB layout, extract pad positions, pitch, drill size or pin numbers from a datasheet drawing.'
---

# PDF → verified KiCad footprint

The LLM reads the drawing (dimensions, pin numbers, pad/drill sizes). A script **measures** pad
positions from the drawing and **checks them against every dimension printed on it**. Never write
coordinates by hand from the numbers alone: hand-read values were wrong in earlier runs (a y off
by 0.10, a pad whose size differed from the others).

Script: `scripts/measure.py` (needs `pymupdf`, `opencv-python-headless`, `numpy`).
Run from the project root; use a work folder such as `out/`.

## Workflow

1. **Look at the page.** Render it (`pymupdf` → PNG) and find the "RECOMMEND PCB LAYOUT" / land
   pattern view. Note whether it is 1:1; do not assume. Many views are enlarged.
2. **Region.** `python .github/skills/pdf-to-footprint/scripts/measure.py find PDF --page N`
   prints `x0,y0,x1,y1` in PDF points. It relies on hatched oval pads; if it prints "not found"
   (solid SMD pads, other styles) choose the region yourself from the rendered page. Check it
   covers the layout view only.
3. **Detect.** `... detect PDF --page N --region x0,y0,x1,y1 --out out`
   Writes `out/pads.json` and `out/overlay.png`. **View overlay.png** and confirm every pad has a
   marker and the count matches the drawing. Indices are *not* pin numbers.
4. **Read the drawing** and write `out/spec.json` (format in the script's docstring):
   - `pins`: detect index → pin number, from the labels (#1…) beside each pad.
   - `calibration`: two pins and the real distance between them (a long, clearly dimensioned span;
     the scale comes from this, not from page scale).
   - `origin`: a pin or `center`.
   - `pad` / `drill`: sizes from the drawing notes (e.g. `6-1.00`, `5-1.60`). Look for per-pin
     exceptions: notes may list different sizes for one pad (e.g. 1.80 vs 1.60).
     Drill `[0,0]` means SMD.
   - `checks`: **every other dimension** between pads printed on the drawing, as
     `[pinA, pinB, "X"|"Y"|"D", value]`. Do not reuse the calibration span. Dimensions that start at
     a point that is not a pad (e.g. a notch centre) must be converted or skipped.
5. **Build.** `... build out/spec.json --out out`
   Writes `<name>.kicad_mod` and `.csv`. Exit code 1 = a check failed or a pad size disagrees with
   the measured outline: re-read the drawing, fix the spec, re-run. Do not loosen tolerances or
   delete failing checks to get a pass.
6. **Report** to the user: the pin table, all checks with measured values, and what is
   **not** verified.

## Always state as unverified / not generated
- Body outline, silkscreen, courtyard beyond the pad bounding box, pin‑1 marker, locating pegs
  (NPTH) and PCB cut-outs are not extracted. Add them only if the drawing gives dimensions, and
  say which values were inferred.
- Pad sizes and drills come from drawing text read by the LLM; the script only confirms the pad
  outline width/height matches (±0.15 mm).
- Check tolerance is 0.10 mm; compare it to the drawing tolerance (e.g. ±0.05).
- Tested on one datasheet only. Hatched oval THT pads are found automatically; other styles need
  a manual region and may need manual measurement.

## Output
- `<name>.kicad_mod` (KiCad 8 format, mm, y axis down) and `<name>.csv`
  (`pin,x_mm,y_mm,pad_w_mm,pad_h_mm,drill_w_mm,drill_h_mm`).
- The file is not opened in KiCad by the script; tell the user to open it in the Footprint Editor
  and compare with the PDF.
