#!/usr/bin/env python3
"""Measure pads from a datasheet PDF's recommended PCB layout and build a KiCad footprint.

Commands
  find    PDF [--page N]                      -> suggest region (PDF points) of the recommended layout
  detect  PDF --region x0,y0,x1,y1 [--page N] [--out DIR]
                                              -> pads.json + overlay.png (pads numbered by index)
  build   SPEC.json [--out DIR]               -> .kicad_mod + .csv + verification report
Exit code of `build` is 1 if any check fails.

SPEC.json (written after reading the drawing):
{
  "name": "PART", "pads_json": "pads.json",
  "pins": {"0": "4", "1": "6"},                       # detect index -> pin number
  "calibration": {"a": "5", "b": "1", "distance_mm": 8.8},   # two pins, known distance
  "origin": "center" | "<pin>",
  "pad":   {"default": [1.0, 1.6], "1": [1.0, 1.8]},  # pad w,h mm per pin (from drawing notes)
  "drill": {"default": [0.6, 1.2], "1": [0.6, 1.4]},  # [0,0] -> SMD
  "checks": [["5", "4", "Y", 8.45]]                   # pinA, pinB, X|Y|D, dimension printed on drawing
}
pads_json is resolved relative to the spec file.
"""
import argparse
import csv
import json
import os
import sys

import cv2
import numpy as np
import pymupdf

SNAP = 0.05
TOL = 0.1


def render_crop(page, rect, max_side=6000, max_dpi=1200):
    dpi = int(min(max_dpi, max_side / (max(rect.width, rect.height) / 72)))
    pix = page.get_pixmap(dpi=dpi, clip=rect)
    img = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)[:, :, :3].copy()
    return img, dpi


def preprocess(img, dpi):
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    bw = (gray < 140).astype(np.uint8) * 255
    line_len = max(20, int(dpi * 0.2167))
    for kern in (cv2.getStructuringElement(cv2.MORPH_RECT, (line_len, 1)),
                 cv2.getStructuringElement(cv2.MORPH_RECT, (1, line_len))):
        lines = cv2.morphologyEx(bw, cv2.MORPH_OPEN, kern)
        bw = cv2.subtract(bw, cv2.dilate(lines, np.ones((5, 5), np.uint8)))
    k = max(3, int(dpi * 0.0075)) | 1
    return cv2.morphologyEx(bw, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))


def hatch_ratio(img, b):
    x, y, w, h = b
    r = (cv2.cvtColor(img[y:y + h, x:x + w], cv2.COLOR_RGB2GRAY) < 140).astype(np.float32)
    gx, gy = cv2.Sobel(r, cv2.CV_32F, 1, 0), cv2.Sobel(r, cv2.CV_32F, 0, 1)
    mag, ang = np.hypot(gx, gy), np.degrees(np.arctan2(gy, gx)) % 180
    diag = ((ang > 30) & (ang < 60)) | ((ang > 120) & (ang < 150))
    return float(mag[diag].sum() / (mag.sum() + 1e-6))


def hatch_candidates(img, dpi):
    """Oval pads found from their 45-degree hatching, so labels/dimension lines touching a pad don't hide it."""
    g = (cv2.cvtColor(img, cv2.COLOR_RGB2GRAY) < 140).astype(np.uint8) * 255
    L = max(9, int(0.0125 * dpi))
    acc = np.zeros_like(g)
    for flip in (False, True):
        k = np.eye(L, dtype=np.uint8)
        acc |= cv2.morphologyEx(g, cv2.MORPH_OPEN, k[::-1] if flip else k)
    s = max(3, int(0.02 * dpi))
    acc = cv2.morphologyEx(cv2.dilate(acc, np.ones((3, 3), np.uint8)), cv2.MORPH_CLOSE,
                           cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (s, s)))
    cnts, _ = cv2.findContours(acc, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        if 0.04 * dpi < w < 0.5 * dpi and 0.06 * dpi < h < dpi and 0.3 < w / h < 0.9:
            out.append((x, y, w, h))
    return out


def pad_candidates(closed, dpi, img, relaxed=False, hatch=True):
    """Hatched oval pad outlines. relaxed=True also accepts hollow ovals (low fill) with weaker hatching."""
    lo, hi = 0.04 * dpi, 0.5 * dpi
    fmin, hmin = (0.25, 0.30) if relaxed else (0.6, 0.35)
    cnts, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    out = []
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        if not (lo < w < hi and lo * 1.3 < h < hi * 2 and 0.4 < w / h < 0.85):
            continue
        if fmin < cv2.contourArea(c) / (w * h) < 0.95 and hatch_ratio(img, (x, y, w, h)) > hmin:
            out.append((x, y, w, h))
    return out + hatch_candidates(img, dpi) if hatch else out


def group_similar(boxes, tol=0.15):
    groups = []
    for b in boxes:
        for g in groups:
            if abs(b[2] - g[0][2]) <= tol * g[0][2] and abs(b[3] - g[0][3]) <= tol * g[0][3]:
                g.append(b)
                break
        else:
            groups.append([b])
    return groups


def best_cluster(cands, s=1, min_n=3):
    """Most column-aligned compact cluster of >=3 distinct similar candidates (text/knurls rarely align)."""
    best, best_score = None, 0
    for g in group_similar(cands):
        for seed in g:
            nb = [c for c in g if abs(c[0] - seed[0]) < 100 * s and abs(c[1] - seed[1]) < 100 * s]
            distinct = []  # fragments of one pad count once
            for c in nb:
                if not any(abs(c[0] - d[0]) < 8 * s and abs(c[1] - d[1]) < 8 * s for d in distinct):
                    distinct.append(c)
            aligned = sum(1 for d in distinct if any(e is not d and abs(e[0] - d[0]) < 4 * s for e in distinct))
            score = aligned + 0.01 * len(distinct)
            if len(distinct) >= min_n and aligned >= min_n - 1 and score > best_score:
                best, best_score = nb, score
    return best


def find_region(page):
    """Region (PDF points) around the best compact group of similar oval pads (strict, then relaxed)."""
    dpi, k = 1200, 72 / 1200
    tw, th, ov = 200, 150, 25
    cands = ([], [])  # contour-based pads first, hatch-only candidates as fallback
    y = page.rect.y0
    while y < page.rect.y1:
        x = page.rect.x0
        while x < page.rect.x1:
            clip = pymupdf.Rect(x, y, x + tw, y + th) & page.rect
            pix = page.get_pixmap(dpi=dpi, clip=clip)
            img = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)[:, :, :3]
            closed = preprocess(img, dpi)
            for kind, found in ((0, pad_candidates(closed, dpi, img, True, False)), (1, hatch_candidates(img, dpi))):
                for bx, by, bw, bh in found:
                    c = (clip.x0 + (bx + bw / 2) * k, clip.y0 + (by + bh / 2) * k, bw * k, bh * k)
                    if not any(abs(c[0] - o[0]) < 2 and abs(c[1] - o[1]) < 2 for o in cands[kind]):
                        cands[kind].append(c)
            x += tw - ov
        y += th - ov
    best = best_cluster(cands[0]) or best_cluster(cands[1])
    if not best:
        return None
    x0, y0 = min(c[0] - c[2] / 2 for c in best), min(c[1] - c[3] / 2 for c in best)
    x1, y1 = max(c[0] + c[2] / 2 for c in best), max(c[1] + c[3] / 2 for c in best)
    m = 0.9 * max(x1 - x0, y1 - y0) + 20
    return pymupdf.Rect(x0 - m, y0 - m * 0.6, x1 + m, y1 + m * 0.9) & page.rect

def refine(closed, dpi, cx, cy, w, h):
    hh, hw = int(h * 0.66), int(w * 0.9)
    y_lo, x_lo = max(0, int(cy - hh)), max(0, int(cx - hw))
    r = closed[y_lo:int(cy + hh), x_lo:int(cx + hw)] > 0
    rows = np.where(r.sum(1) > dpi * 0.0167)[0]
    if len(rows) == 0:
        return cy, w, h
    y0, y1 = rows.min(), rows.max()
    cols = np.where(r[y0:y1 + 1].sum(0) > 0.15 * (y1 - y0))[0]
    if len(cols) == 0:
        return cy, w, h
    return y_lo + (y0 + y1) / 2, cols.max() - cols.min() + 1, y1 - y0 + 1


def match_pads(closed, img, dpi, seed):
    """Template-match one seed box over the crop; returns pads (weak/non-column matches dropped)."""
    x, y, w, h = seed
    score = cv2.matchTemplate(closed, closed[y:y + h, x:x + w], cv2.TM_CCOEFF_NORMED)
    found = []
    for _ in range(80):
        _, mx, _, loc = cv2.minMaxLoc(score)
        if mx < 0.5:
            break
        cx = loc[0] + w / 2
        cy, rw, rh = refine(closed, dpi, cx, loc[1] + h / 2, w, h)
        cv2.rectangle(score, (loc[0] - w, loc[1] - h), (loc[0] + w, loc[1] + h), -1, -1)
        if hatch_ratio(img, (loc[0], loc[1], w, h)) < 0.30:  # weak matches on plain lines/arrows
            continue
        found.append(dict(cx=float(cx), cy=float(cy), w=float(rw), h=float(rh), score=round(float(mx), 2)))
    # weak matches (score<0.7) must line up in a column with a strong one; otherwise they are dimension ticks/arrows
    return [p for p in found if p["score"] >= 0.7 or any(
        q["score"] >= 0.7 and abs(q["cx"] - p["cx"]) < 0.25 * w for q in found)]


def detect(page, rect, out_dir):
    img, dpi = render_crop(page, rect)
    closed = preprocess(img, dpi)
    cands = pad_candidates(closed, dpi, img, True)
    g = best_cluster(cands, dpi / 72) or best_cluster(cands, dpi / 72, 1) or cands[:1]
    if not g:
        sys.exit("No hatched oval pads found in the region. Use a tighter region or measure manually.")
    pool = list(g) + [c for c in cands if c not in g]
    best = None
    for seed in pool[:12]:  # keep the seed whose template matches the most pads
        f = match_pads(closed, img, dpi, seed)
        key = (len(f), min((p["score"] for p in f), default=0))
        if best is None or key > best[0]:
            best = (key, seed, f)
    _, (x, y, w, h), found = best
    if not found:
        sys.exit("No hatched oval pads found in the region. Use a tighter region or measure manually.")
    mw = float(np.median([p["w"] for p in found]))
    for p in found:
        p["w"] = mw
    found.sort(key=lambda p: (round(p["cx"] / (w * 2)), p["cy"]))  # column, then top->bottom
    for i, p in enumerate(found):
        p["index"] = i
    ov = img.copy()
    for p in found:
        cv2.circle(ov, (int(p["cx"]), int(p["cy"])), max(6, dpi // 100), (255, 0, 0), -1)
        cv2.putText(ov, str(p["index"]), (int(p["cx"] + p["w"]), int(p["cy"])), cv2.FONT_HERSHEY_SIMPLEX,
                    dpi / 400, (0, 0, 255), max(2, dpi // 200))
    os.makedirs(out_dir, exist_ok=True)
    cv2.imwrite(os.path.join(out_dir, "overlay.png"), cv2.cvtColor(ov, cv2.COLOR_RGB2BGR))
    with open(os.path.join(out_dir, "pads.json"), "w") as f:
        json.dump(dict(dpi=dpi, region=list(rect), pads=found), f, indent=1)
    print(f"{len(found)} pads (score min {min(p['score'] for p in found)}). "
          f"Wrote {out_dir}/pads.json and overlay.png (indices are NOT pin numbers).")
    for p in found:
        print(f"  index {p['index']}: px=({p['cx']:.0f},{p['cy']:.0f}) size={p['w']:.0f}x{p['h']:.0f} score={p['score']}")


def snap(v):
    return round(round(v / SNAP) * SNAP, 2)


def footprint_text(name, rows):
    thru = any(r["drill_w"] > 0 for r in rows)
    x0, x1 = min(r["x"] - r["pad_w"] / 2 for r in rows) - .25, max(r["x"] + r["pad_w"] / 2 for r in rows) + .25
    y0, y1 = min(r["y"] - r["pad_h"] / 2 for r in rows) - .25, max(r["y"] + r["pad_h"] / 2 for r in rows) + .25
    out = [f'(footprint "{name}"', "\t(version 20240108)", '\t(generator "pdf-to-footprint")', '\t(layer "F.Cu")',
           "\t(attr through_hole)" if thru else "\t(attr smd)",
           f'\t(property "Reference" "J**" (at 0 {y0 - 1:g} 0) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))',
           f'\t(property "Value" "{name}" (at 0 {y1 + 1:g} 0) (layer "F.Fab") (effects (font (size 1 1) (thickness 0.15))))']
    for r in rows:
        shape = "oval" if abs(r["pad_w"] - r["pad_h"]) > 1e-6 else "circle"
        if r["drill_w"] > 0:
            d = (f'(drill {r["drill_w"]:g})' if abs(r["drill_w"] - r["drill_h"]) < 1e-6
                 else f'(drill oval {r["drill_w"]:g} {r["drill_h"]:g})')
            out.append(f'\t(pad "{r["pin"]}" thru_hole {shape} (at {r["x"]:g} {r["y"]:g}) '
                       f'(size {r["pad_w"]:g} {r["pad_h"]:g}) {d} (layers "*.Cu" "*.Mask"))')
        else:
            out.append(f'\t(pad "{r["pin"]}" smd roundrect (at {r["x"]:g} {r["y"]:g}) '
                       f'(size {r["pad_w"]:g} {r["pad_h"]:g}) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))')
    out.append(f'\t(fp_rect (start {x0:g} {y0:g}) (end {x1:g} {y1:g}) '
               '(stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd"))')
    out.append(")")
    return "\n".join(out) + "\n"


def build(spec_path, out_dir):
    spec = json.load(open(spec_path, encoding="utf-8-sig"))
    base = os.path.dirname(os.path.abspath(spec_path))
    pj = spec["pads_json"] if os.path.isabs(spec["pads_json"]) else os.path.join(base, spec["pads_json"])
    pads = json.load(open(pj))["pads"]
    pin_of = {int(k): str(v) for k, v in spec["pins"].items()}
    missing = [p["index"] for p in pads if p["index"] not in pin_of]
    if missing:
        sys.exit(f"pins missing for detect indices {missing}")
    by_pin = {pin_of[p["index"]]: p for p in pads}
    cal = spec["calibration"]
    a, b = by_pin[str(cal["a"])], by_pin[str(cal["b"])]
    scale = cal["distance_mm"] / float(np.hypot(a["cx"] - b["cx"], a["cy"] - b["cy"]))
    if spec.get("origin", "center") == "center":
        xs, ys = [p["cx"] for p in pads], [p["cy"] for p in pads]
        ox, oy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    else:
        ox, oy = by_pin[str(spec["origin"])]["cx"], by_pin[str(spec["origin"])]["cy"]

    def pick(table, pin, fallback):
        return table.get(pin, table.get("default", fallback))
    rows = []
    for pin, p in by_pin.items():
        pw, ph = pick(spec.get("pad", {}), pin, [snap(p["w"] * scale), snap(p["h"] * scale)])
        dw, dh = pick(spec.get("drill", {}), pin, [0, 0])
        rows.append(dict(pin=pin, x=snap((p["cx"] - ox) * scale), y=snap((p["cy"] - oy) * scale),
                         pad_w=pw, pad_h=ph, drill_w=dw, drill_h=dh,
                         meas_w=round(p["w"] * scale, 2), meas_h=round(p["h"] * scale, 2)))
    rows.sort(key=lambda r: (int(r["pin"]) if r["pin"].isdigit() else 10 ** 6, r["pin"]))
    os.makedirs(out_dir, exist_ok=True)
    name = spec["name"]
    with open(os.path.join(out_dir, name + ".kicad_mod"), "w", encoding="utf-8") as f:
        f.write(footprint_text(name, rows))
    cols = ["pin", "x", "y", "pad_w", "pad_h", "drill_w", "drill_h"]
    with open(os.path.join(out_dir, name + ".csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["pin", "x_mm", "y_mm", "pad_w_mm", "pad_h_mm", "drill_w_mm", "drill_h_mm"])
        for r in rows:
            w.writerow([r[c] for c in cols])
    print(f"scale {1 / scale:.1f} px/mm; wrote {name}.kicad_mod and .csv to {out_dir}")
    for r in rows:
        print(f"  pin {r['pin']}: x={r['x']:+.2f} y={r['y']:+.2f} pad={r['pad_w']}x{r['pad_h']} "
              f"drill={r['drill_w']}x{r['drill_h']} (measured outer {r['meas_w']}x{r['meas_h']})")
    ok = True
    pos = {r["pin"]: r for r in rows}
    for a_, b_, axis, exp in spec.get("checks", []):
        dx, dy = pos[str(a_)]["x"] - pos[str(b_)]["x"], pos[str(a_)]["y"] - pos[str(b_)]["y"]
        got = abs(dx) if axis == "X" else abs(dy) if axis == "Y" else float(np.hypot(dx, dy))
        good = abs(got - exp) <= TOL
        ok &= good
        print(f"  [{'OK' if good else 'FAIL'}] pin {a_}-{b_} {axis}: drawing {exp:.2f}, measured {got:.2f}")
    for r in rows:  # pad size given in the spec must agree with the measured outline
        if abs(r["pad_w"] - r["meas_w"]) > 0.15 or abs(r["pad_h"] - r["meas_h"]) > 0.15:
            ok = False
            print(f"  [FAIL] pin {r['pin']} pad size {r['pad_w']}x{r['pad_h']} vs measured {r['meas_w']}x{r['meas_h']}")
    print("ALL CHECKS PASSED" if ok else "CHECKS FAILED")
    if not spec.get("checks"):
        print("WARNING: no dimension checks supplied")
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("find")
    f.add_argument("pdf")
    f.add_argument("--page", type=int, default=1)
    d = sub.add_parser("detect")
    d.add_argument("pdf")
    d.add_argument("--page", type=int, default=1)
    d.add_argument("--region", required=True, help="x0,y0,x1,y1 in PDF points")
    d.add_argument("--out", default="out")
    b = sub.add_parser("build")
    b.add_argument("spec")
    b.add_argument("--out", default="out")
    a = ap.parse_args()
    if a.cmd == "build":
        build(a.spec, a.out)
        return
    page = pymupdf.open(a.pdf)[a.page - 1]
    if a.cmd == "find":
        r = find_region(page)
        print("not found; choose the region manually" if r is None
              else f"{r.x0:.1f},{r.y0:.1f},{r.x1:.1f},{r.y1:.1f}")
    else:
        detect(page, pymupdf.Rect(*map(float, a.region.split(","))), a.out)


if __name__ == "__main__":
    main()
