"""Connector datasheet PDF -> KiCad footprint (step-by-step wizard).

Steps: 1 Open PDF  2 Region  3 Pads  4 Pin numbers  5 Scale  6 Verify & export
"""
import csv
import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import cv2
import numpy as np
import pymupdf

STEPS = ["開啟 PDF", "框選區域", "確認焊盤", "標示 Pin 編號", "校準比例", "驗證與匯出"]
HELP = [
    "按「開啟 PDF」選擇規格書。程式會自動尋找「建議焊盤圖 (Recommended PCB layout)」。",
    "紅框是自動找到的建議焊盤圖區域。位置不對時,直接在圖上拖曳重新框選,再按「下一步」。",
    "每個焊盤上應有一個紅點。多了:在紅點上按右鍵刪除;漏了:在焊盤中央按左鍵新增。",
    "依規格書圖上的編號,依序點擊各焊盤。輸入框是目前要指派的編號;右鍵可清除該焊盤的編號。",
    "點選兩個「你知道實際距離」的焊盤,輸入圖上標註的距離 (mm) 後按「套用」。"
    "例:左右兩排 4.40 + 4.40 = 8.80。",
    "檢查座標與尺寸。可在下方輸入規格書的標註值做驗證 (✔ / ✘),確認後匯出。",
]
COLS = ("pin", "x", "y", "pad_w", "pad_h", "drill_w", "drill_h")
SNAP = 0.05


# ---------------------------------------------------------------- core logic
def render_crop(page, rect, max_side=6000, max_dpi=1200):
    dpi = int(min(max_dpi, max_side / (max(rect.width, rect.height) / 72)))
    pix = page.get_pixmap(dpi=dpi, clip=rect)
    img = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)[:, :, :3].copy()
    return img, dpi


def preprocess(img, dpi):
    """Binarize, strip long center/dimension lines, merge hatching into solid blobs."""
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
    """Share of edge energy at ~45 degrees: pads are hatched, text is not."""
    x, y, w, h = b
    r = (cv2.cvtColor(img[y:y + h, x:x + w], cv2.COLOR_RGB2GRAY) < 140).astype(np.float32)
    gx, gy = cv2.Sobel(r, cv2.CV_32F, 1, 0), cv2.Sobel(r, cv2.CV_32F, 0, 1)
    mag, ang = np.hypot(gx, gy), np.degrees(np.arctan2(gy, gx)) % 180
    diag = ((ang > 30) & (ang < 60)) | ((ang > 120) & (ang < 150))
    return float(mag[diag].sum() / (mag.sum() + 1e-6))


def pad_candidates(closed, dpi, img):
    """Boxes that look like a hatched oval pad."""
    lo, hi = 0.04 * dpi, 0.5 * dpi
    cnts, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    out = []
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        if not (lo < w < hi and lo * 1.3 < h < hi * 2 and 0.4 < w / h < 0.85):
            continue
        if 0.25 < cv2.contourArea(c) / (w * h) < 0.95 and hatch_ratio(img, (x, y, w, h)) > 0.30:
            out.append((x, y, w, h))
    return out


def best_group(cands, min_n=3):
    """Most column-aligned cluster of >=3 distinct similar candidates (text/knurls rarely align)."""
    groups = []
    for b in cands:
        for g in groups:
            if abs(b[2] - g[0][2]) <= 0.15 * g[0][2] and abs(b[3] - g[0][3]) <= 0.15 * g[0][3]:
                g.append(b)
                break
        else:
            groups.append([b])
    best, best_score = None, 0
    for g in groups:
        for seed in g:
            nb = [c for c in g if abs(c[0] - seed[0]) < 100 and abs(c[1] - seed[1]) < 100]
            distinct = []  # fragments of one pad count once
            for c in nb:
                if not any(abs(c[0] - d[0]) < 8 and abs(c[1] - d[1]) < 8 for d in distinct):
                    distinct.append(c)
            aligned = sum(1 for d in distinct if any(e is not d and abs(e[0] - d[0]) < 8 for e in distinct))
            score = aligned + 0.01 * len(distinct)
            if len(distinct) >= min_n and aligned >= min_n - 1 and score > best_score:
                best, best_score = nb, score
    return best


def auto_find_layout(page):
    """Propose the recommended-PCB-layout region (PDF points) or None."""
    for key in ("RECOMMEND", "PCB LAYOUT", "LAND PATTERN", "FOOTPRINT"):
        hits = page.search_for(key)
        if hits:
            r = hits[0]
            return pymupdf.Rect(r.x0 - 60, r.y0 - 190, r.x0 + 190, r.y1 + 25) & page.rect
    # Raster fallback: scan the page in 1200 dpi tiles for groups of similar oval pads
    dpi, k = 1200, 72 / 1200
    cands = []
    tw, th, ov = 200, 150, 25
    y = page.rect.y0
    while y < page.rect.y1:
        x = page.rect.x0
        while x < page.rect.x1:
            clip = pymupdf.Rect(x, y, x + tw, y + th) & page.rect
            pix = page.get_pixmap(dpi=dpi, clip=clip)
            img = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)[:, :, :3]
            for bx, by, bw, bh in pad_candidates(preprocess(img, dpi), dpi, img):
                c = (clip.x0 + (bx + bw / 2) * k, clip.y0 + (by + bh / 2) * k, bw * k, bh * k)
                if not any(abs(c[0] - o[0]) < 2 and abs(c[1] - o[1]) < 2 for o in cands):
                    cands.append(c)
            x += tw - ov
        y += th - ov
    best = best_group(cands)
    if not best:
        return None
    x0, y0 = min(c[0] - c[2] / 2 for c in best), min(c[1] - c[3] / 2 for c in best)
    x1, y1 = max(c[0] + c[2] / 2 for c in best), max(c[1] + c[3] / 2 for c in best)
    m = 0.9 * max(x1 - x0, y1 - y0) + 20
    return pymupdf.Rect(x0 - m, y0 - m * 0.6, x1 + m, y1 + m * 0.9) & page.rect


def refine(closed, dpi, cx, cy, w, h):
    """Pad y-center and size from ink profiles (ignores thin stubs touching the pad)."""
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


def detect_pads(closed, dpi, seed, count=0, min_score=0.5):
    """Template-match the seed pad. count=0 -> as many as score >= min_score."""
    x, y, w, h = seed
    score = cv2.matchTemplate(closed, closed[y:y + h, x:x + w], cv2.TM_CCOEFF_NORMED)
    found = []
    for _ in range(count or 80):
        _, mx, _, loc = cv2.minMaxLoc(score)
        if mx < min_score:
            break
        cx = loc[0] + w / 2
        cy, rw, rh = refine(closed, dpi, cx, loc[1] + h / 2, w, h)
        found.append(dict(cx=cx, cy=cy, w=rw, h=rh, score=float(mx), pin=None))
        cv2.rectangle(score, (loc[0] - w, loc[1] - h), (loc[0] + w, loc[1] + h), -1, -1)
    # weak matches (score<0.7) must line up in a column with a strong one; otherwise they are dimension ticks/arrows
    found = [p for p in found if p["score"] >= 0.7 or any(
        q["score"] >= 0.7 and abs(q["cx"] - p["cx"]) < 0.25 * w for q in found)]
    if found:
        mw = float(np.median([p["w"] for p in found]))
        for p in found:
            p["w"] = mw
    return found


def pick_seed(closed, dpi, cands, group):
    """Template seed = the candidate whose template matches the most pads (robust to touched/truncated pads)."""
    pool = list(group) + [c for c in cands if c not in group]
    best, best_key = pool[0], (-1, 0)
    for b in pool[:12]:
        f = detect_pads(closed, dpi, b)
        key = (len(f), min((p["score"] for p in f), default=0))
        if key > best_key:
            best, best_key = b, key
    return best


def snap(v):
    return round(round(v / SNAP) * SNAP, 2)


def footprint_text(name, rows):
    thru = any(r["drill_w"] > 0 for r in rows)
    x0 = min(r["x"] - r["pad_w"] / 2 for r in rows) - 0.25
    x1 = max(r["x"] + r["pad_w"] / 2 for r in rows) + 0.25
    y0 = min(r["y"] - r["pad_h"] / 2 for r in rows) - 0.25
    y1 = max(r["y"] + r["pad_h"] / 2 for r in rows) + 0.25
    out = [f'(footprint "{name}"', "\t(version 20240108)", '\t(generator "footprint_wizard")',
           '\t(layer "F.Cu")', '\t(descr "Generated from datasheet recommended PCB layout")',
           "\t(attr through_hole)" if thru else "\t(attr smd)",
           f'\t(property "Reference" "J**" (at 0 {y0 - 1:g} 0) (layer "F.SilkS") '
           '(effects (font (size 1 1) (thickness 0.15))))',
           f'\t(property "Value" "{name}" (at 0 {y1 + 1:g} 0) (layer "F.Fab") '
           '(effects (font (size 1 1) (thickness 0.15))))']
    for r in rows:
        shape = "oval" if abs(r["pad_w"] - r["pad_h"]) > 1e-6 else "circle"
        if r["drill_w"] > 0:
            drill = (f'(drill {r["drill_w"]:g})' if abs(r["drill_w"] - r["drill_h"]) < 1e-6
                     else f'(drill oval {r["drill_w"]:g} {r["drill_h"]:g})')
            out.append(f'\t(pad "{r["pin"]}" thru_hole {shape} (at {r["x"]:g} {r["y"]:g}) '
                       f'(size {r["pad_w"]:g} {r["pad_h"]:g}) {drill} (layers "*.Cu" "*.Mask"))')
        else:
            out.append(f'\t(pad "{r["pin"]}" smd roundrect (at {r["x"]:g} {r["y"]:g}) '
                       f'(size {r["pad_w"]:g} {r["pad_h"]:g}) (layers "F.Cu" "F.Paste" "F.Mask") '
                       "(roundrect_rratio 0.25))")
    out.append(f'\t(fp_rect (start {x0:g} {y0:g}) (end {x1:g} {y1:g}) '
               '(stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd"))')
    out.append(")")
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------- GUI
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("連接器 PDF → KiCad Footprint")
        self.geometry("1500x900")
        self.doc = self.page = None
        self.region = None
        self.crop_img = self.closed = self.seed = None
        self.crop_dpi = 0
        self.pads = []
        self.scale = None          # mm per crop pixel
        self.cal_sel = []          # pad indices picked for calibration
        self.checks = []
        self.step = 0
        self.view = "page"
        self.zoom_mult = 1.0
        self.f = 1.0
        self.photo = None
        self.drag = None
        self._resize_job = None
        self._build()
        self.go(0)

    # ---------------------------------------------------------------- layout
    def _build(self):
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True)
        left = ttk.Frame(paned)
        paned.add(left, weight=3)
        self.canvas = tk.Canvas(left, bg="#777")
        hs = ttk.Scrollbar(left, orient="horizontal", command=self.canvas.xview)
        vs = ttk.Scrollbar(left, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=hs.set, yscrollcommand=vs.set)
        hs.pack(side="bottom", fill="x")
        vs.pack(side="right", fill="y")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Button-3>", self.on_right)
        self.canvas.bind("<Configure>", self.on_resize)

        right = ttk.Frame(paned, padding=8)
        paned.add(right, weight=2)
        self.step_lbls = []
        bar = ttk.Frame(right)
        bar.pack(fill="x")
        for i, s in enumerate(STEPS):
            lb = tk.Label(bar, text=f"{i + 1}\n{s}", font=("Microsoft JhengHei", 9), width=9,
                          relief="groove", pady=3)
            lb.grid(row=0, column=i, sticky="ew", padx=1)
            bar.columnconfigure(i, weight=1)
            self.step_lbls.append(lb)
        self.help = tk.Label(right, text="", wraplength=470, justify="left", anchor="w",
                             font=("Microsoft JhengHei", 11), fg="#024", bg="#eef5ff", padx=8, pady=8)
        self.help.pack(fill="x", pady=8)
        self.msg = tk.StringVar()
        tk.Label(right, textvariable=self.msg, fg="#b00", anchor="w", wraplength=470,
                 justify="left").pack(fill="x")

        self.holder = ttk.Frame(right)
        self.holder.pack(fill="both", expand=True)
        self.frames = [ttk.Frame(self.holder) for _ in STEPS]
        self._build_frames()

        nav = ttk.Frame(right)
        nav.pack(fill="x", pady=4)
        self.back_btn = ttk.Button(nav, text="← 上一步", command=self.back)
        self.back_btn.pack(side="left")
        self.next_btn = ttk.Button(nav, text="下一步 →", command=self.next)
        self.next_btn.pack(side="right")

        self.preview = tk.Canvas(right, height=230, bg="white", highlightthickness=1,
                                 highlightbackground="#999")
        self.preview.pack(fill="x", pady=(4, 0))
        self.preview.bind("<Configure>", lambda e: self.draw_preview())

    def _build_frames(self):
        f0, f1, f2, f3, f4, f5 = self.frames
        # 0 open
        ttk.Button(f0, text="開啟 PDF…", command=self.open_pdf).pack(anchor="w", pady=4)
        r = ttk.Frame(f0)
        r.pack(anchor="w")
        ttk.Label(r, text="頁碼").pack(side="left")
        self.page_var = tk.IntVar(value=1)
        ttk.Spinbox(r, from_=1, to=99, width=4, textvariable=self.page_var,
                    command=self.load_page).pack(side="left", padx=4)
        self.file_lbl = ttk.Label(f0, text="尚未開啟檔案", foreground="#555")
        self.file_lbl.pack(anchor="w", pady=4)
        # 1 region
        r = ttk.Frame(f1)
        r.pack(anchor="w")
        ttk.Button(r, text="放大 +", command=lambda: self.zoom(1.3)).pack(side="left")
        ttk.Button(r, text="縮小 −", command=lambda: self.zoom(1 / 1.3)).pack(side="left", padx=4)
        ttk.Button(r, text="重新自動尋找", command=self.auto_region).pack(side="left")
        # 2 pads
        r = ttk.Frame(f2)
        r.pack(anchor="w")
        ttk.Label(r, text="焊盤數量 (0 = 自動)").pack(side="left")
        self.count_var = tk.IntVar(value=0)
        ttk.Spinbox(r, from_=0, to=200, width=5, textvariable=self.count_var).pack(side="left", padx=4)
        ttk.Button(r, text="重新偵測", command=self.detect).pack(side="left")
        self.pad_info = ttk.Label(f2, text="")
        self.pad_info.pack(anchor="w", pady=6)
        # 3 pins
        r = ttk.Frame(f3)
        r.pack(anchor="w")
        ttk.Label(r, text="目前指派編號").pack(side="left")
        self.next_pin = tk.StringVar(value="1")
        ttk.Entry(r, width=6, textvariable=self.next_pin, font=("Arial", 14)).pack(side="left", padx=6)
        ttk.Button(r, text="全部清除", command=self.clear_pins).pack(side="left")
        self.pin_info = ttk.Label(f3, text="")
        self.pin_info.pack(anchor="w", pady=6)
        # 4 scale
        r = ttk.Frame(f4)
        r.pack(anchor="w")
        ttk.Label(r, text="實際距離 (mm)").pack(side="left")
        self.dist_var = tk.StringVar(value="")
        ttk.Entry(r, width=8, textvariable=self.dist_var).pack(side="left", padx=6)
        ttk.Button(r, text="套用", command=self.apply_scale).pack(side="left")
        ttk.Button(r, text="重選", command=self.reset_cal).pack(side="left", padx=4)
        self.cal_info = ttk.Label(f4, text="尚未選取焊盤")
        self.cal_info.pack(anchor="w", pady=6)
        # 5 verify / export
        r = ttk.Frame(f5)
        r.pack(fill="x")
        ttk.Label(r, text="原點").pack(side="left")
        self.origin_var = tk.StringVar(value="焊盤中心")
        self.origin_cb = ttk.Combobox(r, textvariable=self.origin_var, width=14, state="readonly")
        self.origin_cb.pack(side="left", padx=4)
        self.origin_cb.bind("<<ComboboxSelected>>", lambda e: self.origin_changed())
        ttk.Label(r, text="(鑽孔預設 = 焊盤 − 0.4,請對照圖面)", foreground="#666").pack(side="left")
        self.tree = ttk.Treeview(f5, columns=COLS, show="headings", height=7)
        for c in COLS:
            self.tree.heading(c, text=c)
            self.tree.column(c, width=58, anchor="center")
        self.tree.pack(fill="x", pady=4)
        self.tree.bind("<Double-1>", self.edit_cell)
        box = ttk.LabelFrame(f5, text="用規格書標註值驗證", padding=4)
        box.pack(fill="x")
        r = ttk.Frame(box)
        r.pack(fill="x")
        self.ca, self.cb, self.cv = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.caxis = tk.StringVar(value="Y")
        ttk.Label(r, text="Pin").pack(side="left")
        ttk.Entry(r, width=4, textvariable=self.ca).pack(side="left", padx=2)
        ttk.Label(r, text="到 Pin").pack(side="left")
        ttk.Entry(r, width=4, textvariable=self.cb).pack(side="left", padx=2)
        ttk.Combobox(r, textvariable=self.caxis, values=["X", "Y", "距離"], width=5,
                     state="readonly").pack(side="left", padx=2)
        ttk.Label(r, text="標註值").pack(side="left")
        ttk.Entry(r, width=7, textvariable=self.cv).pack(side="left", padx=2)
        ttk.Button(r, text="加入", command=self.add_check).pack(side="left", padx=4)
        ttk.Button(r, text="清空", command=lambda: (self.checks.clear(), self.refresh_checks())
                   ).pack(side="left")
        self.check_box = tk.Text(box, height=5, width=50, state="disabled", font=("Consolas", 10))
        self.check_box.pack(fill="x", pady=3)
        r = ttk.Frame(f5)
        r.pack(fill="x", pady=4)
        ttk.Label(r, text="名稱").pack(side="left")
        self.name_var = tk.StringVar(value="MyConnector")
        ttk.Entry(r, width=24, textvariable=self.name_var).pack(side="left", padx=4)
        ttk.Button(r, text="匯出 .kicad_mod + .csv", command=self.export).pack(side="left")

    # ------------------------------------------------------------ navigation
    def go(self, step):
        self.step = step
        self.msg.set("")
        for i, lb in enumerate(self.step_lbls):
            lb.config(bg="#2d7ff9" if i == step else ("#cfe8cf" if i < step else "#eee"),
                      fg="white" if i == step else "black")
        self.help.config(text=HELP[step])
        for i, fr in enumerate(self.frames):
            fr.pack_forget()
            if i == step:
                fr.pack(fill="both", expand=True)
        self.back_btn.state(["!disabled"] if step > 0 else ["disabled"])
        self.next_btn.config(text="完成" if step == len(STEPS) - 1 else "下一步 →")
        self.view = "page" if step <= 1 else "crop"
        self.canvas.config(cursor="crosshair")
        if step == 3:
            self.pin_info_update()
        if step == 4:
            self.reset_cal(keep_scale=True)
        if step == 5:
            self.refresh_origin_list()
            self.refresh_table()
            self.refresh_checks()
        self.redraw(full=True)
        self.draw_preview()

    def back(self):
        if self.step > 0:
            self.go(self.step - 1)

    def next(self):
        s = self.step
        if s == 0:
            if not self.page:
                return self.msg.set("請先開啟 PDF。")
            if not self.region:
                self.auto_region()
        elif s == 1:
            if not self.region:
                return self.msg.set("請先在圖上拖曳框選區域。")
            if not self.build_crop():
                return
        elif s == 2:
            if not self.pads:
                return self.msg.set("沒有焊盤,請重新偵測。")
        elif s == 3:
            pins = [p["pin"] for p in self.pads]
            if None in pins:
                return self.msg.set("還有焊盤沒有編號。")
            if len(set(pins)) != len(pins):
                return self.msg.set("有重複的編號。")
        elif s == 4:
            if not self.scale:
                return self.msg.set("請先套用校準。")
        elif s == 5:
            return self.export()
        self.go(s + 1)

    # ------------------------------------------------------------------ PDF
    def open_pdf(self):
        path = filedialog.askopenfilename(filetypes=[("PDF", "*.pdf")])
        if not path:
            return
        self.doc = pymupdf.open(path)
        self.name_var.set(os.path.splitext(os.path.basename(path))[0])
        self.file_lbl.config(text=os.path.basename(path))
        self.page_var.set(1)
        self.load_page()
        if self.page:
            self.go(1)

    def load_page(self):
        if not self.doc:
            return
        n = min(max(self.page_var.get(), 1), len(self.doc))
        self.page = self.doc[n - 1]
        self.region = self.crop_img = self.closed = None
        self.pads, self.scale, self.checks = [], None, []
        self.zoom_mult = 1.0
        self.auto_region(quiet=True)
        self.redraw(full=True)

    def auto_region(self, quiet=False):
        if not self.page:
            return
        self.config(cursor="watch")
        self.update_idletasks()
        self.region = auto_find_layout(self.page)
        self.config(cursor="")
        if not self.region and not quiet:
            self.msg.set("沒有自動找到,請在圖上拖曳框選。")
        self.redraw()

    def zoom(self, k):
        self.zoom_mult = min(6, max(1, self.zoom_mult * k))
        self.redraw(full=True)

    def build_crop(self):
        self.config(cursor="watch")
        self.msg.set("處理中…")
        self.update_idletasks()
        try:
            self.crop_img, self.crop_dpi = render_crop(self.page, self.region)
            self.closed = preprocess(self.crop_img, self.crop_dpi)
            cands = pad_candidates(self.closed, self.crop_dpi, self.crop_img)
            g = best_group(cands) or best_group(cands, 1) or (cands[:1] or None)
        finally:
            self.config(cursor="")
        if not g:
            self.msg.set("這個區域找不到焊盤,請重新框選。")
            return False
        self.seed = pick_seed(self.closed, self.crop_dpi, cands, g)
        self.scale, self.checks = None, []
        self.count_var.set(0)
        self.detect(quiet=True)
        self.msg.set("")
        return True

    # ---------------------------------------------------------------- pads
    def detect(self, quiet=False):
        if self.closed is None:
            return
        keep = {}
        found = detect_pads(self.closed, self.crop_dpi, self.seed, self.count_var.get())
        for p in found:  # keep pin numbers for pads that did not move
            for q in self.pads:
                if abs(p["cx"] - q["cx"]) < self.seed[2] and abs(p["cy"] - q["cy"]) < self.seed[3]:
                    p["pin"] = q["pin"]
        self.pads = sorted(found, key=lambda p: (round(p["cx"] / (self.seed[2] * 2)), p["cy"]))
        self.scale = None
        self.pad_info_update()
        self.redraw()

    def pad_info_update(self):
        self.pad_info.config(text=f"目前 {len(self.pads)} 個焊盤")

    def hit(self, x, y):
        best, bd = None, None
        for i, p in enumerate(self.pads):
            d = np.hypot(p["cx"] - x, p["cy"] - y)
            if d < max(p["w"], p["h"]) * 0.7 and (bd is None or d < bd):
                best, bd = i, d
        return best

    # --------------------------------------------------------------- canvas
    def on_resize(self, _):
        if self._resize_job:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(150, lambda: self.redraw(full=True))

    def cpos(self, e):
        return self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)

    def on_press(self, e):
        self.drag = self.cpos(e)

    def on_drag(self, e):
        if self.drag and self.step == 1 and self.view == "page":
            x, y = self.cpos(e)
            self.canvas.delete("sel")
            self.canvas.create_rectangle(*self.drag, x, y, outline="#06f", width=2, tags="sel")

    def on_release(self, e):
        if not self.drag:
            return
        x0, y0 = self.drag
        x1, y1 = self.cpos(e)
        self.drag = None
        if self.view == "page":
            if self.step == 1 and abs(x1 - x0) > 20 and abs(y1 - y0) > 20:
                f = self.f
                self.region = pymupdf.Rect(min(x0, x1) / f, min(y0, y1) / f,
                                           max(x0, x1) / f, max(y0, y1) / f) & self.page.rect
                self.redraw()
            return
        px, py = x1 / self.f, y1 / self.f
        i = self.hit(px, py)
        if self.step == 2 and i is None:
            cy, rw, rh = refine(self.closed, self.crop_dpi, px, py, self.seed[2], self.seed[3])
            self.pads.append(dict(cx=px, cy=cy, w=float(np.median([p["w"] for p in self.pads] or [rw])),
                                  h=rh, score=0.0, pin=None))
            self.scale = None
            self.pad_info_update()
        elif self.step == 3 and i is not None:
            label = self.next_pin.get().strip()
            if not label:
                return self.msg.set("請先輸入要指派的編號。")
            self.pads[i]["pin"] = label
            if label.isdigit():
                self.next_pin.set(str(int(label) + 1))
            self.pin_info_update()
            self.scale = None
        elif self.step == 4 and i is not None:
            if i not in self.cal_sel:
                self.cal_sel = (self.cal_sel + [i])[-2:]
            self.cal_info_update()
        self.redraw()

    def on_right(self, e):
        if self.view != "crop":
            return
        x, y = self.cpos(e)
        i = self.hit(x / self.f, y / self.f)
        if i is None:
            return
        if self.step == 2:
            del self.pads[i]
            self.scale = None
            self.pad_info_update()
        elif self.step == 3:
            self.pads[i]["pin"] = None
            self.pin_info_update()
        self.redraw()

    def redraw(self, full=False):
        if not hasattr(self, "canvas"):
            return
        cw, ch = max(300, self.canvas.winfo_width()), max(300, self.canvas.winfo_height())
        if full or self.photo is None:
            if self.view == "page":
                if self.page is None:
                    self.canvas.delete("all")
                    return
                pr = self.page.rect
                self.f = min(cw / pr.width, ch / pr.height) * self.zoom_mult
                pix = self.page.get_pixmap(matrix=pymupdf.Matrix(self.f, self.f))
                data, w, h = pix.tobytes("png"), pix.width, pix.height
            else:
                if self.crop_img is None:
                    return
                h0, w0 = self.crop_img.shape[:2]
                self.f = min(cw / w0, ch / h0)
                im = cv2.resize(self.crop_img, None, fx=self.f, fy=self.f, interpolation=cv2.INTER_AREA)
                data = cv2.imencode(".png", cv2.cvtColor(im, cv2.COLOR_RGB2BGR))[1].tobytes()
                h, w = im.shape[:2]
            self.photo = tk.PhotoImage(data=data)
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
            self.canvas.configure(scrollregion=(0, 0, w, h))
        self.canvas.delete("ov")
        if self.view == "page":
            if self.region:
                r, f = self.region, self.f
                self.canvas.create_rectangle(r.x0 * f, r.y0 * f, r.x1 * f, r.y1 * f, outline="#e00",
                                             width=3, tags="ov")
            return
        f = self.f
        for i, p in enumerate(self.pads):
            cx, cy = p["cx"] * f, p["cy"] * f
            sel = i in self.cal_sel and self.step == 4
            self.canvas.create_rectangle(cx - p["w"] * f / 2, cy - p["h"] * f / 2,
                                         cx + p["w"] * f / 2, cy + p["h"] * f / 2,
                                         outline="#fa0" if sel else "#0a0", width=3 if sel else 1, tags="ov")
            self.canvas.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, fill="#e00", outline="", tags="ov")
            if self.step >= 3:
                txt = p["pin"] if p["pin"] is not None else "?"
                self.canvas.create_text(cx + p["w"] * f / 2 + 14, cy, text=f"#{txt}",
                                        fill="#c00" if p["pin"] is None else "#00c",
                                        font=("Arial", 16, "bold"), tags="ov")

    # ------------------------------------------------------------------ pins
    def pin_info_update(self):
        n = sum(p["pin"] is not None for p in self.pads)
        self.pin_info.config(text=f"已編號 {n} / {len(self.pads)}")

    def clear_pins(self):
        for p in self.pads:
            p["pin"] = None
        self.next_pin.set("1")
        self.pin_info_update()
        self.redraw()

    # ----------------------------------------------------------------- scale
    def cal_info_update(self):
        if len(self.cal_sel) < 2:
            self.cal_info.config(text=f"已選 {len(self.cal_sel)} / 2 個焊盤")
        else:
            a, b = (self.pads[i] for i in self.cal_sel)
            self.cal_info.config(text=f"已選 Pin {a['pin']} 與 Pin {b['pin']},請輸入它們的距離")

    def reset_cal(self, keep_scale=False):
        self.cal_sel = []
        if not keep_scale:
            self.scale = None
        self.cal_info_update()
        self.redraw()

    def apply_scale(self):
        if len(self.cal_sel) != 2:
            return self.msg.set("請先點選兩個焊盤。")
        try:
            d = float(self.dist_var.get())
            assert d > 0
        except (ValueError, AssertionError):
            return self.msg.set("距離格式不正確。")
        a, b = (self.pads[i] for i in self.cal_sel)
        px = float(np.hypot(a["cx"] - b["cx"], a["cy"] - b["cy"]))
        self.scale = d / px
        for p in self.pads:
            p.pop("ov", None)
        self.msg.set("")
        self.cal_info.config(text=f"比例 {1 / self.scale:.1f} px/mm,已套用。按「下一步」。")
        self.draw_preview()

    # ----------------------------------------------------- rows / verification
    def origin_xy(self):
        sel = self.origin_var.get()
        if sel.startswith("Pin "):
            for p in self.pads:
                if p["pin"] == sel[4:]:
                    return p["cx"], p["cy"]
        xs, ys = [p["cx"] for p in self.pads], [p["cy"] for p in self.pads]
        return (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2

    def rows(self):
        if not self.scale:
            return []
        ox, oy = self.origin_xy()
        out = []
        for p in self.pads:
            ov = p.setdefault("ov", {})
            pw = ov.get("pad_w", snap(p["w"] * self.scale))
            ph = ov.get("pad_h", snap(p["h"] * self.scale))
            out.append(dict(
                pin=p["pin"] if p["pin"] is not None else "?",
                x=ov.get("x", snap((p["cx"] - ox) * self.scale)),
                y=ov.get("y", snap((p["cy"] - oy) * self.scale)),
                pad_w=pw, pad_h=ph,
                drill_w=ov.get("drill_w", round(max(0.0, snap(pw - 0.4)), 2)),
                drill_h=ov.get("drill_h", round(max(0.0, snap(ph - 0.4)), 2))))
        out.sort(key=lambda r: (int(r["pin"]) if r["pin"].isdigit() else 10 ** 6, r["pin"]))
        return out

    def refresh_origin_list(self):
        pins = sorted((p["pin"] for p in self.pads if p["pin"]),
                      key=lambda s: (not s.isdigit(), int(s) if s.isdigit() else 0, s))
        self.origin_cb["values"] = ["焊盤中心"] + [f"Pin {s}" for s in pins]

    def origin_changed(self):
        for p in self.pads:
            p.pop("ov", None)
        self.refresh_table()
        self.refresh_checks()
        self.draw_preview()

    def refresh_table(self):
        self.tree.delete(*self.tree.get_children())
        for r in self.rows():
            self.tree.insert("", "end", iid=str(r["pin"]), values=[r[c] for c in COLS])

    def edit_cell(self, e):
        iid, col = self.tree.identify_row(e.y), self.tree.identify_column(e.x)
        if not iid or col == "#1":
            return
        key = COLS[int(col[1:]) - 1]
        x, y, w, h = self.tree.bbox(iid, col)
        ent = ttk.Entry(self.tree)
        ent.place(x=x, y=y, width=w, height=h)
        ent.insert(0, self.tree.set(iid, key))
        ent.focus()

        def done(_=None):
            v = ent.get().strip()
            ent.destroy()
            try:
                val = float(v)
            except ValueError:
                return
            for p in self.pads:
                if p["pin"] == iid:
                    p.setdefault("ov", {})[key] = val
            self.refresh_table()
            self.refresh_checks()
            self.draw_preview()
        ent.bind("<Return>", done)
        ent.bind("<FocusOut>", done)
        ent.bind("<Escape>", lambda _: ent.destroy())

    def add_check(self):
        try:
            self.checks.append((self.ca.get().strip(), self.cb.get().strip(),
                                self.caxis.get(), float(self.cv.get())))
        except ValueError:
            return self.msg.set("標註值格式不正確。")
        self.msg.set("")
        self.refresh_checks()

    def refresh_checks(self):
        rows = {r["pin"]: r for r in self.rows()}
        lines = []
        for a, b, axis, exp in self.checks:
            if a not in rows or b not in rows:
                lines.append(f"?  Pin {a}↔{b}: 找不到此編號")
                continue
            dx, dy = rows[a]["x"] - rows[b]["x"], rows[a]["y"] - rows[b]["y"]
            got = abs(dx) if axis == "X" else abs(dy) if axis == "Y" else float(np.hypot(dx, dy))
            ok = abs(got - exp) <= 0.1
            lines.append(f"{'✔' if ok else '✘'}  Pin {a}↔{b} {axis}: 標註 {exp:.2f}  量測 {got:.2f}  "
                         f"(Δ{got - exp:+.2f})")
        self.check_box.config(state="normal")
        self.check_box.delete("1.0", "end")
        self.check_box.insert("end", "\n".join(lines) or "尚未加入驗證項目")
        self.check_box.config(state="disabled")

    # --------------------------------------------------------------- preview
    def draw_preview(self):
        c = self.preview
        c.delete("all")
        rows = self.rows() if self.scale else []
        if not rows:
            c.create_text(10, 10, anchor="nw", fill="#888", text="Footprint 預覽(校準後顯示)")
            return
        W, H = max(200, c.winfo_width()), max(120, c.winfo_height())
        x0 = min(r["x"] - r["pad_w"] / 2 for r in rows)
        x1 = max(r["x"] + r["pad_w"] / 2 for r in rows)
        y0 = min(r["y"] - r["pad_h"] / 2 for r in rows)
        y1 = max(r["y"] + r["pad_h"] / 2 for r in rows)
        s = min((W - 70) / max(x1 - x0, 1e-3), (H - 50) / max(y1 - y0, 1e-3))
        tx = lambda x: (W - (x1 - x0) * s) / 2 + (x - x0) * s
        ty = lambda y: (H - (y1 - y0) * s) / 2 + (y - y0) * s

        def stadium(x, y, w, h, color):
            long_, short = max(w, h), min(w, h)
            d = (long_ - short) / 2
            ax, ay = (0, d) if h >= w else (d, 0)
            c.create_line(tx(x - ax), ty(y - ay), tx(x + ax), ty(y + ay), width=max(1, short * s),
                          capstyle="round", fill=color)

        for r in rows:
            stadium(r["x"], r["y"], r["pad_w"], r["pad_h"], "#c8503c")
            if r["drill_w"] > 0:
                stadium(r["x"], r["y"], r["drill_w"], r["drill_h"], "white")
            c.create_text(tx(r["x"]) + r["pad_w"] * s / 2 + 12, ty(r["y"]), text=r["pin"],
                          font=("Arial", 10, "bold"))
        c.create_line(tx(0) - 8, ty(0), tx(0) + 8, ty(0), fill="#08f")
        c.create_line(tx(0), ty(0) - 8, tx(0), ty(0) + 8, fill="#08f")
        c.create_text(6, H - 6, anchor="sw", fill="#555",
                      text=f"原點(藍十字)  範圍 {x1 - x0:.2f} × {y1 - y0:.2f} mm")

    # ---------------------------------------------------------------- export
    def export(self):
        rows = self.rows()
        if not rows:
            return self.msg.set("請先完成校準。")
        if any(r["pin"] == "?" for r in rows):
            return self.msg.set("有焊盤尚未編號。")
        bad = [c for c in self.checks]
        path = filedialog.asksaveasfilename(defaultextension=".kicad_mod", initialfile=self.name_var.get(),
                                            filetypes=[("KiCad footprint", "*.kicad_mod")])
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(footprint_text(self.name_var.get(), rows))
        with open(os.path.splitext(path)[0] + ".csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["pin", "x_mm", "y_mm", "pad_w_mm", "pad_h_mm", "drill_w_mm", "drill_h_mm"])
            for r in rows:
                w.writerow([r[c] for c in COLS])
        self.msg.set("")
        messagebox.showinfo("完成", f"已匯出:\n{path}\n{os.path.splitext(path)[0]}.csv"
                            + ("" if bad else "\n\n(注意:尚未加入任何標註值驗證)"))


if __name__ == "__main__":
    App().mainloop()
