"""Extract the recommended PCB layout pads from the connector PDF with OpenCV.

Scale is NOT 1:1 for this view, so it is calibrated from the two pad columns
(4.40 + 4.40 = 8.80 mm, read from the drawing) and then cross-checked against
the other printed dimensions (8.45, 6.30, 3.30, 2.50, pad width 1.00).
Outputs: overlay.png (visual check), DA2-4164R-60BMX-U.extracted.kicad_mod
"""
import sys
import cv2
import numpy as np
import pymupdf

PDF = "DA2-4164R-60BMX-U.pdf"
DPI = 1200
COLUMN_DIST_MM = 8.80
S = 842 / 1990
CROP = pymupdf.Rect(130 * S, 780 * S, 540 * S, 1160 * S)

page = pymupdf.open(PDF)[0]
pix = page.get_pixmap(dpi=DPI, clip=CROP)
img = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)[:, :, :3].copy()
gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
bw = (gray < 140).astype(np.uint8) * 255

# Remove long straight lines (center lines, dimension/extension lines, PCB edge)
L = 260
for kern in (cv2.getStructuringElement(cv2.MORPH_RECT, (L, 1)),
             cv2.getStructuringElement(cv2.MORPH_RECT, (1, L))):
    lines = cv2.morphologyEx(bw, cv2.MORPH_OPEN, kern)
    bw = cv2.subtract(bw, cv2.dilate(lines, np.ones((5, 5), np.uint8)))

closed = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)))
cv2.imwrite("dbg.png", cv2.resize(closed, None, fx=0.4, fy=0.4))
cnts, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)

# Seed: the cleanest isolated pad (stub-free, high fill). Then template-match all 6.
seed = None
for c in cnts:
    x, y, w, h = cv2.boundingRect(c)
    fill = cv2.contourArea(c) / (w * h)
    if 100 < w < 140 and 160 < h < 190 and fill > 0.75:
        seed = (x, y, w, h)
if seed is None:
    sys.exit("no seed pad found")
x, y, w, h = seed
tpl = closed[y:y + h, x:x + w]
score = cv2.matchTemplate(closed, tpl, cv2.TM_CCOEFF_NORMED)
cands = []
for _ in range(6):
    _, mx, _, loc = cv2.minMaxLoc(score)
    print(round(mx, 2), loc)
    if mx < 0.35:
        break
    cands.append((loc[0] + w / 2, loc[1] + h / 2, w, h, mx))
    cv2.rectangle(score, (loc[0] - w, loc[1] - h), (loc[0] + w, loc[1] + h), -1, -1)
cands = [c[:4] for c in cands]


def refine(cx0, cy0):
    """Pad extents from row/column ink profiles (ignores thin center-line stubs)."""
    r = closed[int(cy0 - 118):int(cy0 + 118), int(cx0 - 110):int(cx0 + 110)] > 0
    rows = np.where(r.sum(1) > 20)[0]
    y0, y1 = rows.min(), rows.max()
    cols = np.where(r[y0:y1 + 1].sum(0) > 0.15 * (y1 - y0))[0]
    x0, x1 = cols.min(), cols.max()
    return (cx0 - 110 + (x0 + x1) / 2, cy0 - 118 + (y0 + y1) / 2, x1 - x0 + 1, y1 - y0 + 1)


cands = [refine(c[0], c[1]) for c in cands]
print("template scores ok" if len(cands) == 6 else f"only {len(cands)} matches")
if len(cands) != 6:
    sys.exit(1)

cands.sort(key=lambda p: p[0])
left = sorted(cands[:3], key=lambda p: p[1])
right = sorted(cands[3:], key=lambda p: p[1])
order = {4: left[0], 6: left[1], 5: left[2], 3: right[0], 2: right[1], 1: right[2]}

lx = np.median([p[0] for p in left])
rx = np.median([p[0] for p in right])
px_per_mm = (rx - lx) / COLUMN_DIST_MM
cx = (lx + rx) / 2
oy = order[6][1]  # origin y = pad #6
print(f"scale = {px_per_mm:.1f} px/mm  (drawing scale ~ {px_per_mm / (DPI / 25.4):.2f}:1)")

res = {n: (((lx if n in (4, 5, 6) else rx) - cx) / px_per_mm, (p[1] - oy) / px_per_mm, p[2] / px_per_mm, p[3] / px_per_mm)
       for n, p in order.items()}
for n in sorted(res):
    x, y, w, h = res[n]
    print(f"#{n}: x={x:+.3f} y={y:+.3f} outer={w:.2f}x{h:.2f}")

ok = True
checks = [("#5-#4 (8.45)", res[5][1] - res[4][1], 8.45),
          ("#5-#6 (2.50)", res[5][1] - res[6][1], 2.50),
          ("#2-#3 (6.30-3.30=3.00)", res[2][1] - res[3][1], 3.00),
          ("#3 to #1 (6.30+2.50=8.80)", res[1][1] - res[3][1], 8.80),
          ("#3 to #2 ref line (6.30) vs ref=#2-3.30", (res[2][1] + 3.30) - res[3][1], 6.30),
          ("pad width (1.00)", np.mean([r[2] for r in res.values()]), 1.00),
          ("pad #5 height (1.60)", res[5][3], 1.60),
          ("pad #1 height (1.80)", res[1][3], 1.80)]
for name, got, exp in checks:
    good = abs(got - exp) < 0.12
    ok &= good
    print(f"{name}: measured {got:.2f}  {'OK' if good else 'MISMATCH'}")

ov = img.copy()
for n, p in order.items():
    cv2.circle(ov, (int(p[0]), int(p[1])), 10, (255, 0, 0), -1)
    cv2.putText(ov, f"#{n}", (int(p[0]) + 90, int(p[1]) - 60), cv2.FONT_HERSHEY_SIMPLEX, 3, (255, 0, 0), 6)
cv2.imwrite("overlay.png", cv2.cvtColor(ov, cv2.COLOR_RGB2BGR))


def sizes(n):  # pad w, h, drill w, h from the drawing notes (6-1.00, 6-0.60, 5-1.60, 5-1.20; #1 1.80/1.40)
    return (1.0, 1.8, 0.6, 1.4) if n == 1 else (1.0, 1.6, 0.6, 1.2)


out = ['(footprint "DA2-4164R-60BMX-U"', "\t(version 20240108)", '\t(generator "extract.py")',
       '\t(layer "F.Cu")', '\t(attr through_hole)',
       '\t(property "Reference" "J**" (at 0 -8 0) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))',
       '\t(property "Value" "DA2-4164R-60BMX-U" (at 0 8 0) (layer "F.Fab") (effects (font (size 1 1) (thickness 0.15))))']
for n in sorted(res):
    x, y = (round(v * 20) / 20 for v in res[n][:2])  # snap to 0.05 mm (drawing tolerance)
    pw, ph, dw, dh = sizes(n)
    out.append(f'\t(pad "{n}" thru_hole oval (at {x:.2f} {y:.2f}) (size {pw} {ph}) '
               f'(drill oval {dw} {dh}) (layers "*.Cu" "*.Mask"))')
out.append(")")
open("DA2-4164R-60BMX-U.extracted.kicad_mod", "w", encoding="utf-8").write("\n".join(out) + "\n")
print("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED")






