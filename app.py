"""비디오 컷 — 라이브 녹화 영상에서 대기·렉 멈춤·흐림·검은 화면을 찾아 확인 후 잘라내는 프로그램"""
import json
import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

import analyzer
import exporter
import fftools
import updater
from version import VERSION

APP_NAME = "비디오 컷"
FONT = ("맑은 고딕", 10)
FONT_B = ("맑은 고딕", 10, "bold")
FONT_S = ("맑은 고딕", 9)
GRAY = "#666"
THUMB_W, THUMB_H = 200, 113
VIDEO_EXT = (".mp4", ".mkv", ".mov", ".ts", ".flv", ".m4v", ".avi", ".webm")

if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except Exception:
    TkinterDnD = None
    DND_FILES = None

EXPORT_MODES = [
    ("auto", "자동 (H.265 원본은 무손실, 그 외는 H.265 인코딩)"),
    ("copy", "무손실 (빠름 · 키프레임 기준으로 자름)"),
    ("encode", "H.265 인코딩 (정확하게 자름)"),
]


# ── 설정 ──
def settings_file():
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    d = os.path.join(base, "VideoCut")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "settings.json")


def load_settings():
    s = dict(analyzer.DEFAULTS)
    s.update({"quality": 24, "encoder": "auto", "export_mode": "auto", "geometry": "1520x900"})
    try:
        with open(settings_file(), encoding="utf-8") as f:
            s.update(json.load(f))
    except Exception:
        pass
    return s


def save_settings(s):
    try:
        with open(settings_file(), "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def segs_file(path, fps):
    return analyzer.cache_path(path, fps)[:-4] + ".segs.json"


class App:
    def __init__(self, root):
        self.root = root
        self.s = load_settings()
        self.path = None
        self.info = None
        self.analysis = None
        self.segs = []
        self.sel = None
        self.job = None
        self.busy = False
        self.q = []
        self.view = (0.0, 1.0)
        self.thumb_gen = 0
        self.thumb_cache = {}
        self.player = None

        root.title(f"{APP_NAME} {VERSION}")
        try:
            root.iconbitmap(default=os.path.join(fftools.app_dir(), "icon.ico"))
        except Exception:
            pass
        root.geometry(self.s.get("geometry", "1520x900"))
        root.minsize(1200, 760)
        style = ttk.Style()
        try:
            style.theme_use("vista" if sys.platform == "win32" else "clam")
        except tk.TclError:
            pass
        root.option_add("*Font", FONT)
        style.configure(".", font=FONT)
        style.configure("Treeview", rowheight=26)
        style.configure("Treeview.Heading", font=FONT_B)
        style.configure("Big.TButton", font=("맑은 고딕", 12, "bold"), padding=(14, 8))
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self._poll)
        threading.Thread(target=self._quiet_check, daemon=True).start()
        if not fftools.available():
            self.status("FFmpeg 를 찾지 못했습니다. 프로그램을 다시 설치해주세요.")

    # ── 화면 구성 ──
    def _build(self):
        r = self.root
        top = ttk.Frame(r, padding=(10, 8, 10, 4))
        top.pack(fill="x")
        ttk.Button(top, text="영상 열기", command=self.open_file).pack(side="left")
        self.file_lbl = ttk.Label(top, text="영상 파일을 열거나 이 창에 끌어다 놓으세요.", foreground=GRAY)
        self.file_lbl.pack(side="left", padx=12)
        self.upd_btn = ttk.Button(top, text="업데이트 확인", command=lambda: UpdateDialog(self))
        ttk.Button(top, text="⚙ 설정", command=lambda: SettingsDialog(self)).pack(side="right")
        self.upd_btn.pack(side="right", padx=6)

        body = ttk.PanedWindow(r, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10)
        left = ttk.Frame(body)
        right = ttk.Frame(body, width=430)
        body.add(left, weight=3)
        body.add(right, weight=1)

        # 미리보기 5장
        pv = ttk.LabelFrame(left, text="선택한 구간 미리보기", padding=8)
        pv.pack(fill="x")
        self.blank = ImageTk.PhotoImage(Image.new("RGB", (THUMB_W, THUMB_H), "#d9d9d9"))
        self.thumb_lbls, self.thumb_caps = [], []
        caps = ["잘리기 직전", "구간 시작", "구간 중간", "구간 끝", "잘린 직후"]
        for i, c in enumerate(caps):
            col = ttk.Frame(pv)
            col.grid(row=0, column=i, padx=4)
            cap = ttk.Label(col, text=c, font=FONT_S, foreground=GRAY)
            cap.pack()
            edge = "#2e7d32" if i in (0, 4) else "#c62828"
            lb = tk.Label(col, image=self.blank, bd=0, highlightthickness=3,
                          highlightbackground=edge, highlightcolor=edge, cursor="hand2")
            lb.pack()
            lb.bind("<Button-1>", lambda e, k=i: self.play_thumb(k))
            self.thumb_lbls.append(lb)
            self.thumb_caps.append(cap)
        self.sel_lbl = ttk.Label(pv, text="", font=FONT_B)
        self.sel_lbl.grid(row=1, column=0, columnspan=5, sticky="w", pady=(6, 0))
        ttk.Label(pv, text="초록 테두리 = 남는 장면, 빨강 테두리 = 잘리는 장면 · 사진을 누르면 그 지점부터 재생",
                  font=FONT_S, foreground=GRAY).grid(row=2, column=0, columnspan=5, sticky="w")

        ctl = ttk.Frame(left, padding=(0, 8, 0, 0))
        ctl.pack(fill="x")
        ttk.Button(ctl, text="▶ 구간 재생", command=self.play_seg).pack(side="left")
        ttk.Button(ctl, text="▶ 시작 경계", command=lambda: self.play_edge("start")).pack(side="left", padx=4)
        ttk.Button(ctl, text="▶ 끝 경계", command=lambda: self.play_edge("end")).pack(side="left")
        ttk.Separator(ctl, orient="vertical").pack(side="left", fill="y", padx=10)
        self.toggle_btn = ttk.Button(ctl, text="살리기 (Space)", command=self.toggle)
        self.toggle_btn.pack(side="left")
        ttk.Button(ctl, text="구간 지우기 (Del)", command=self.delete_seg).pack(side="left", padx=4)

        adj = ttk.Frame(left, padding=(0, 6, 0, 0))
        adj.pack(fill="x")
        for which, name in (("start", "시작"), ("end", "끝")):
            ttk.Label(adj, text=f"{name} 조정").pack(side="left", padx=(0 if which == "start" else 14, 4))
            for d in (-1.0, -0.2, 0.2, 1.0):
                ttk.Button(adj, text=f"{d:+g}초", width=6,
                           command=lambda w=which, dd=d: self.adjust(w, dd)).pack(side="left", padx=1)

        tl = ttk.LabelFrame(left, text="타임라인 (휠: 확대/축소 · 더블클릭: 전체 보기 · 클릭: 구간 선택)", padding=6)
        tl.pack(fill="x", pady=(10, 0))
        self.canvas = tk.Canvas(tl, height=96, bg="white", highlightthickness=0)
        self.canvas.pack(fill="x")
        self.hbar = ttk.Scrollbar(tl, orient="horizontal", command=self._scroll_view)
        self.hbar.pack(fill="x")
        self.canvas.bind("<Configure>", lambda e: self.draw_timeline())
        self.canvas.bind("<Button-1>", self._tl_click)
        self.canvas.bind("<Double-Button-1>", lambda e: self._set_view(0, self._dur()))
        self.canvas.bind("<MouseWheel>", self._tl_wheel)
        self.canvas.bind("<Button-4>", lambda e: self._zoom_at(e.x, 0.8))
        self.canvas.bind("<Button-5>", lambda e: self._zoom_at(e.x, 1.25))
        lg = ttk.Frame(tl)
        lg.pack(fill="x", pady=(4, 0))
        for k in ("start", "freeze", "wait", "blur", "black", "end", "manual"):
            tk.Label(lg, text="  ", bg=analyzer.KIND_COLORS[k]).pack(side="left", padx=(8, 2))
            ttk.Label(lg, text=analyzer.KINDS[k], font=FONT_S).pack(side="left")
        ttk.Label(lg, text="  점선 = 살리기로 바꾼 구간", font=FONT_S, foreground=GRAY).pack(side="left", padx=8)

        # 오른쪽: 구간 목록
        self.sum_lbl = ttk.Label(right, text="", font=FONT_B, wraplength=420, justify="left")
        self.sum_lbl.pack(fill="x", padx=(8, 0))
        tf = ttk.Frame(right)
        tf.pack(fill="both", expand=True, padx=(8, 0), pady=6)
        cols = ("no", "kind", "start", "end", "len", "act")
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", selectmode="browse")
        for c, t, w in zip(cols, ("#", "종류", "시작", "끝", "길이", "처리"), (40, 86, 78, 78, 62, 58)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="center", stretch=c == "kind")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("keep", foreground="#999999")
        self.tree.bind("<<TreeviewSelect>>", self._tree_sel)
        self.tree.bind("<space>", lambda e: (self.toggle(), "break")[1])
        self.tree.bind("<Delete>", lambda e: self.delete_seg())
        self.tree.bind("<Return>", lambda e: self.play_seg())
        rb = ttk.Frame(right)
        rb.pack(fill="x", padx=(8, 0))
        ttk.Button(rb, text="다시 찾기", command=self.redetect).pack(side="left")
        ttk.Button(rb, text="구간 추가", command=self.add_segment).pack(side="left", padx=4)
        ttk.Button(rb, text="모두 자르기", command=lambda: self.all_cut(True)).pack(side="left")
        ttk.Button(rb, text="모두 살리기", command=lambda: self.all_cut(False)).pack(side="left", padx=4)

        # 아래: 저장
        bot = ttk.LabelFrame(r, text="확인이 끝나면 저장", padding=8)
        bot.pack(fill="x", padx=10, pady=(8, 0))
        row = ttk.Frame(bot)
        row.pack(fill="x")
        ttk.Label(row, text="저장 방식").pack(side="left")
        self.mode_var = tk.StringVar(value=dict(EXPORT_MODES).get(self.s.get("export_mode", "auto")))
        ttk.Combobox(row, textvariable=self.mode_var, values=[m[1] for m in EXPORT_MODES], state="readonly",
                     width=44).pack(side="left", padx=6)
        ttk.Label(row, text="화질(낮을수록 좋음)").pack(side="left", padx=(10, 0))
        self.q_var = tk.IntVar(value=int(self.s.get("quality", 24)))
        ttk.Spinbox(row, from_=16, to=36, textvariable=self.q_var, width=4).pack(side="left", padx=6)
        ttk.Label(row, text="저장 위치").pack(side="left", padx=(10, 0))
        self.out_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.out_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(row, text="변경", command=self.pick_out).pack(side="left")
        row2 = ttk.Frame(bot)
        row2.pack(fill="x", pady=(8, 0))
        self.save_btn = ttk.Button(row2, text="확인 완료 — 잘라서 저장", style="Big.TButton", command=self.start_export)
        self.save_btn.pack(side="left")
        self.cancel_btn = ttk.Button(row2, text="취소", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        self.prog = ttk.Progressbar(row2, maximum=1000)
        self.prog.pack(side="left", fill="x", expand=True, padx=8)
        self.status_lbl = ttk.Label(r, text="준비", anchor="w", padding=(10, 4))
        self.status_lbl.pack(fill="x")

        if TkinterDnD and DND_FILES:
            try:
                r.drop_target_register(DND_FILES)
                r.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass
        r.bind_all("<Control-o>", lambda e: self.open_file())

    # ── 공용 ──
    def status(self, msg):
        self.status_lbl.config(text=msg)

    def post(self, *m):
        self.q.append(m)

    def _poll(self):
        while self.q:
            m = self.q.pop(0)
            try:
                self._handle(m)
            except Exception as e:
                self.status(f"오류: {e}")
        self.root.after(80, self._poll)

    def _handle(self, m):
        kind = m[0]
        if kind == "prog":
            self.prog.config(value=int(m[1] * 1000))
            self.status(m[2])
        elif kind == "analyzed":
            self._on_analyzed(*m[1:])
        elif kind == "fail":
            self._set_busy(False)
            self.prog.config(value=0)
            self.status(m[1].splitlines()[0])
            if m[1] != "취소됨":
                messagebox.showerror(APP_NAME, m[1])
        elif kind == "thumbs":
            gen, imgs = m[1], m[2]
            if gen == self.thumb_gen:
                for lb, im in zip(self.thumb_lbls, imgs):
                    if im is not None:
                        ph = ImageTk.PhotoImage(im)
                        lb.config(image=ph)
                        lb.image = ph
        elif kind == "exported":
            self._set_busy(False)
            self.prog.config(value=1000)
            out, msg = m[1], m[2]
            self.status(msg.splitlines()[0])
            if messagebox.askyesno(APP_NAME, msg + f"\n\n{out}\n\n저장된 폴더를 열까요?"):
                self.reveal(out)
        elif kind == "upd":
            self.upd_btn.config(text=m[1])

    def _set_busy(self, b):
        self.busy = b
        self.save_btn.config(state="disabled" if b else "normal")
        self.cancel_btn.config(state="normal" if b else "disabled")

    def _dur(self):
        return self.info["duration"] if self.info else 1.0

    def _quiet_check(self):
        try:
            info = updater.latest_release(timeout=8)
            if updater.is_newer(info["tag"], VERSION):
                self.post("upd", f"새 버전 {info['tag']} 있음!")
        except Exception:
            pass

    # ── 열기·분석 ──
    def _on_drop(self, e):
        files = [f for f in self.root.tk.splitlist(e.data) if os.path.isfile(f)]
        vids = [f for f in files if f.lower().endswith(VIDEO_EXT)] or files
        if vids:
            self.open_file(vids[0])

    def open_file(self, path=None):
        if self.busy:
            messagebox.showinfo(APP_NAME, "작업이 끝난 뒤 열어주세요.")
            return
        if not path:
            path = filedialog.askopenfilename(
                title="영상 열기", initialdir=self.s.get("last_dir") or None,
                filetypes=[("영상 파일", " ".join("*" + x for x in VIDEO_EXT)), ("모든 파일", "*.*")])
        if not path:
            return
        self.s["last_dir"] = os.path.dirname(path)
        self._save_state()
        self.path = path
        self.info = None
        self.analysis = None
        self.segs = []
        self.sel = None
        self.thumb_cache = {}
        self.refresh()
        self.file_lbl.config(text=os.path.basename(path), foreground="black")
        self.out_var.set(exporter.default_out(path))
        self._start_analyze(path, use_cache=True)

    def _start_analyze(self, path, use_cache=True, keep_saved=True):
        self._set_busy(True)
        self.job = fftools.Job()
        job = self.job
        settings = dict(self.s)

        def work():
            try:
                info = fftools.probe(path)
                a = analyzer.analyze(path, info, settings, progress=lambda r, msg: self.post("prog", r, msg),
                                     job=job, use_cache=use_cache)
                saved = None
                sf = segs_file(path, int(a.fps))
                if keep_saved and os.path.isfile(sf):
                    try:
                        with open(sf, encoding="utf-8") as f:
                            saved = json.load(f)
                    except Exception:
                        saved = None
                segs = saved if saved else analyzer.detect(a, info["duration"], settings)
                self.post("analyzed", info, a, segs, bool(saved))
            except Exception as e:
                self.post("fail", "취소됨" if job.cancelled else str(e))

        threading.Thread(target=work, daemon=True).start()

    def _on_analyzed(self, info, a, segs, restored):
        self._set_busy(False)
        self.info, self.analysis, self.segs = info, a, segs
        self.prog.config(value=1000)
        self.view = (0.0, info["duration"])
        codec = info["vcodec"].upper() + (" 10bit" if info.get("ten_bit") else "")
        self.file_lbl.config(text=f"{os.path.basename(self.path)}   ·   {codec} {info['width']}x{info['height']}"
                                  f"   ·   {fftools.fmt_time(info['duration'])}")
        self.refresh()
        if self.segs:
            self.select(0)
        msg = "이전에 확인하던 내용을 불러왔습니다. (새로 찾으려면 '다시 찾기')" if restored else \
            f"분석 완료 — 잘라낼 구간 {len(self.segs)}개를 찾았습니다. 확인해주세요."
        self.status(msg)

    def redetect(self):
        if not self.path or self.busy:
            return
        if any(not s.get("auto", True) for s in self.segs) or not all(s.get("cut") for s in self.segs):
            if not messagebox.askyesno(APP_NAME, "직접 고친 내용이 사라지고 설정 기준으로 다시 찾습니다. 계속할까요?"):
                return
        if not self.analysis or int(self.analysis.fps) != int(self.s.get("analysis_fps", 5)):
            self._start_analyze(self.path, use_cache=True, keep_saved=False)
            return
        self.segs = analyzer.detect(self.analysis, self.info["duration"], self.s)
        self.sel = None
        self._save_state()
        self.refresh()
        if self.segs:
            self.select(0)
        self.status(f"다시 찾았습니다 — 잘라낼 구간 {len(self.segs)}개")

    # ── 목록·선택 ──
    def refresh(self):
        self.segs.sort(key=lambda s: (s["start"], s["end"]))
        self.tree.delete(*self.tree.get_children())
        for i, s in enumerate(self.segs):
            tags = () if s.get("cut") else ("keep",)
            self.tree.insert("", "end", iid=str(i), tags=tags, values=(
                i + 1, analyzer.KINDS.get(s["kind"], s["kind"]), fftools.fmt_time(s["start"], True),
                fftools.fmt_time(s["end"], True), f"{s['end'] - s['start']:.1f}초", "자르기" if s.get("cut") else "살리기"))
        if self.sel is not None and self.sel < len(self.segs):
            self.tree.selection_set(str(self.sel))
            self.tree.see(str(self.sel))
        self._summary()
        self.draw_timeline()

    def _summary(self):
        if not self.info:
            self.sum_lbl.config(text="")
            return
        cuts = [s for s in self.segs if s.get("cut")]
        keep = sum(b - a for a, b in analyzer.keep_ranges(self.segs, self.info["duration"]))
        removed = self.info["duration"] - keep
        self.sum_lbl.config(text=f"자를 구간 {len(cuts)}개 (총 {fftools.fmt_time(removed)})\n"
                                 f"저장 후 길이 {fftools.fmt_time(keep)} / 원본 {fftools.fmt_time(self.info['duration'])}")

    def _tree_sel(self, _e=None):
        sel = self.tree.selection()
        if sel:
            i = int(sel[0])
            if i != self.sel:
                self.select(i, from_tree=True)

    def select(self, i, from_tree=False):
        if i is None or not (0 <= i < len(self.segs)):
            return
        self.sel = i
        s = self.segs[i]
        if not from_tree:
            self.tree.selection_set(str(i))
            self.tree.see(str(i))
        self.toggle_btn.config(text="살리기 (Space)" if s.get("cut") else "자르기 (Space)")
        self.sel_lbl.config(text=f"#{i + 1}  {analyzer.KINDS.get(s['kind'], s['kind'])}   "
                                 f"{fftools.fmt_time(s['start'], True)} ~ {fftools.fmt_time(s['end'], True)}"
                                 f"   ({s['end'] - s['start']:.1f}초)   →   {'자르기' if s.get('cut') else '살리기'}")
        # 선택 구간이 보이도록 타임라인 이동
        v0, v1 = self.view
        if s["end"] < v0 or s["start"] > v1:
            span = v1 - v0
            c = (s["start"] + s["end"]) / 2
            self._set_view(c - span / 2, c + span / 2)
        else:
            self.draw_timeline()
        self._load_thumbs(s)

    def _thumb_times(self, s):
        d = self._dur()
        st, en = s["start"], s["end"]
        return [max(0.0, st - 1.0), min(d - 0.05, st + 0.15), (st + en) / 2,
                max(0.0, en - 0.25), min(d - 0.05, en + 0.6)]

    def _load_thumbs(self, s):
        self.thumb_gen += 1
        gen = self.thumb_gen
        for lb in self.thumb_lbls:
            lb.config(image=self.blank)
            lb.image = self.blank
        times = self._thumb_times(s)
        for cap, t, name in zip(self.thumb_caps, times, ["잘리기 직전", "구간 시작", "구간 중간", "구간 끝", "잘린 직후"]):
            cap.config(text=f"{name}  {fftools.fmt_time(t, True)}")
        path = self.path

        def work():
            imgs = []
            for t in times:
                if gen != self.thumb_gen:
                    return
                key = round(t, 2)
                im = self.thumb_cache.get(key)
                if im is None:
                    im = fftools.frame_at(path, t, THUMB_W)
                    if im is not None:
                        im = im.resize((THUMB_W, THUMB_H)) if im.size != (THUMB_W, THUMB_H) else im
                        self.thumb_cache[key] = im
                imgs.append(im)
                self.post("thumbs", gen, imgs + [None] * (5 - len(imgs)))

        threading.Thread(target=work, daemon=True).start()

    # ── 편집 ──
    def _cur(self):
        if self.sel is None or self.sel >= len(self.segs):
            return None
        return self.segs[self.sel]

    def toggle(self):
        s = self._cur()
        if not s:
            return
        s["cut"] = not s.get("cut")
        self._changed()
        if self.sel + 1 < len(self.segs):     # 다음 구간으로 넘어가 빠르게 확인
            self.select(self.sel + 1)

    def delete_seg(self):
        s = self._cur()
        if not s:
            return
        del self.segs[self.sel]
        self.sel = min(self.sel, len(self.segs) - 1) if self.segs else None
        self._changed()
        if self.sel is not None:
            self.select(self.sel)

    def adjust(self, which, d):
        s = self._cur()
        if not s:
            return
        dur = self._dur()
        if which == "start":
            s["start"] = round(min(max(0.0, s["start"] + d), s["end"] - 0.2), 2)
        else:
            s["end"] = round(max(min(dur, s["end"] + d), s["start"] + 0.2), 2)
        s["auto"] = False
        cur = s
        self._changed()
        self.select(self.segs.index(cur))

    def all_cut(self, v):
        for s in self.segs:
            s["cut"] = v
        self._changed()
        if self.sel is not None:
            self.select(self.sel)

    def add_segment(self):
        if not self.info:
            return
        s = self._cur()
        st = s["end"] if s else 0.0
        AddDialog(self, st, min(self._dur(), st + 5.0))

    def _changed(self):
        self._save_state()
        cur = self._cur()
        self.refresh()
        if cur in self.segs:
            self.sel = self.segs.index(cur)
            self.tree.selection_set(str(self.sel))

    def _save_state(self):
        if self.path and self.analysis:
            try:
                with open(segs_file(self.path, int(self.analysis.fps)), "w", encoding="utf-8") as f:
                    json.dump(self.segs, f, ensure_ascii=False)
            except Exception:
                pass
        save_settings(self.s)

    # ── 재생 ──
    def _play(self, start, dur=None):
        if not self.path:
            return
        try:
            if self.player and self.player.poll() is None:
                self.player.kill()
            self.player = fftools.play(self.path, start, dur, title=f"{APP_NAME} 미리보기 ({fftools.fmt_time(start, True)})")
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e))

    def play_seg(self):
        s = self._cur()
        if s:
            self._play(s["start"], min(s["end"] - s["start"], 30.0))

    def play_edge(self, which):
        s = self._cur()
        if s:
            t = s["start"] if which == "start" else s["end"]
            self._play(max(0.0, t - 3.0), 6.0)

    def play_thumb(self, k):
        s = self._cur()
        if s:
            self._play(self._thumb_times(s)[k], 8.0)

    # ── 타임라인 ──
    def _set_view(self, a, b):
        d = self._dur()
        span = min(d, max(2.0, b - a))
        a = min(max(0.0, a), d - span)
        self.view = (a, a + span)
        self.draw_timeline()

    def _scroll_view(self, *args):
        d = self._dur()
        v0, v1 = self.view
        span = v1 - v0
        if args[0] == "moveto":
            self._set_view(float(args[1]) * d, float(args[1]) * d + span)
        elif args[0] == "scroll":
            step = span * (0.9 if args[2] == "pages" else 0.1) * int(args[1])
            self._set_view(v0 + step, v1 + step)

    def _tl_wheel(self, e):
        self._zoom_at(e.x, 0.8 if e.delta > 0 else 1.25)

    def _zoom_at(self, x, f):
        if not self.info:
            return
        w = max(1, self.canvas.winfo_width())
        v0, v1 = self.view
        t = v0 + (v1 - v0) * x / w
        span = (v1 - v0) * f
        r = x / w
        self._set_view(t - span * r, t - span * r + span)

    def _x(self, t):
        v0, v1 = self.view
        return (t - v0) / max(1e-6, v1 - v0) * max(1, self.canvas.winfo_width())

    def draw_timeline(self):
        c = self.canvas
        c.delete("all")
        w = max(1, c.winfo_width())
        if not self.info:
            c.create_text(w / 2, 48, text="영상을 열면 여기에 잘라낼 구간이 표시됩니다.", fill=GRAY, font=FONT)
            self.hbar.set(0, 1)
            return
        d = self._dur()
        v0, v1 = self.view
        self.hbar.set(v0 / d, v1 / d)
        y0, y1 = 26, 72
        c.create_rectangle(self._x(0), y0, self._x(d), y1, fill="#d9ead3", outline="")
        # 눈금
        span = v1 - v0
        step = next((st for st in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200)
                     if w / (span / st) >= 90), 7200)
        t = (int(v0 // step)) * step
        while t <= v1:
            x = self._x(t)
            if x >= 0:
                c.create_line(x, 18, x, y1 + 4, fill="#cccccc")
                c.create_text(x + 3, 9, text=fftools.fmt_time(t), anchor="w", fill=GRAY, font=FONT_S)
            t += step
        for i, s in enumerate(self.segs):
            if s["end"] < v0 or s["start"] > v1:
                continue
            xa, xb = self._x(s["start"]), self._x(s["end"])
            if xb - xa < 2:
                xb = xa + 2
            col = analyzer.KIND_COLORS.get(s["kind"], "#999")
            if s.get("cut"):
                c.create_rectangle(xa, y0, xb, y1, fill=col, outline="")
            else:
                c.create_rectangle(xa, y0 + 4, xb, y1 - 4, outline=col, dash=(3, 2), width=2)
            if i == self.sel:
                c.create_rectangle(xa - 2, y0 - 4, xb + 2, y1 + 4, outline="black", width=2)
                c.create_polygon((xa + xb) / 2 - 6, y1 + 16, (xa + xb) / 2 + 6, y1 + 16, (xa + xb) / 2, y1 + 6,
                                 fill="black")

    def _tl_click(self, e):
        if not self.info or not self.segs:
            return
        v0, v1 = self.view
        t = v0 + (v1 - v0) * e.x / max(1, self.canvas.winfo_width())
        best, bd = None, 1e18
        for i, s in enumerate(self.segs):
            if s["start"] <= t <= s["end"]:
                best, bd = i, 0
                break
            dist = min(abs(self._x(s["start"]) - e.x), abs(self._x(s["end"]) - e.x))
            if dist < bd:
                best, bd = i, dist
        if best is not None and bd <= 12:
            self.select(best)

    # ── 저장 ──
    def pick_out(self):
        cur = self.out_var.get()
        p = filedialog.asksaveasfilename(title="저장 위치", defaultextension=".mp4",
                                         initialdir=os.path.dirname(cur) if cur else None,
                                         initialfile=os.path.basename(cur) if cur else "",
                                         filetypes=[("MP4", "*.mp4")])
        if p:
            self.out_var.set(p)

    def _mode(self):
        v = self.mode_var.get()
        return next((k for k, t in EXPORT_MODES if t == v), "auto")

    def start_export(self):
        if not self.info or self.busy:
            return
        out = self.out_var.get().strip()
        if not out:
            messagebox.showinfo(APP_NAME, "저장 위치를 정해주세요.")
            return
        if os.path.abspath(out) == os.path.abspath(self.path):
            messagebox.showerror(APP_NAME, "원본과 같은 이름으로는 저장할 수 없습니다.")
            return
        mode = self._mode()
        real = mode if mode != "auto" else ("copy" if self.info["vcodec"] == "hevc" else "encode")
        cuts = [s for s in self.segs if s.get("cut")]
        keep = sum(b - a for a, b in analyzer.keep_ranges(self.segs, self.info["duration"]))
        how = "무손실(다시 인코딩 없음)" if real == "copy" else "H.265 인코딩"
        if os.path.exists(out) and not messagebox.askyesno(APP_NAME, "같은 이름의 파일이 있습니다. 덮어쓸까요?"):
            return
        if not messagebox.askyesno(APP_NAME, f"자를 구간 {len(cuts)}개를 지우고 저장합니다.\n\n"
                                             f"저장 방식: {how}\n저장 후 길이: {fftools.fmt_time(keep)}\n"
                                             f"저장 위치: {out}\n\n진행할까요?"):
            return
        self.s["export_mode"] = mode
        try:
            self.s["quality"] = int(self.q_var.get())
        except Exception:
            pass
        self._save_state()
        self._set_busy(True)
        self.prog.config(value=0)
        self.job = fftools.Job()
        job = self.job
        path, info, segs, settings = self.path, dict(self.info), [dict(s) for s in self.segs], dict(self.s)

        def work():
            try:
                msg = exporter.export(path, info, segs, out, mode, settings,
                                      progress=lambda r, m: self.post("prog", r, m), job=job)
                self.post("exported", out, msg)
            except Exception as e:
                self.post("fail", "취소됨" if job.cancelled else str(e))

        threading.Thread(target=work, daemon=True).start()

    def cancel(self):
        if self.job:
            self.job.cancel()
            self.status("취소하는 중...")

    def reveal(self, path):
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        except Exception:
            pass

    def on_close(self):
        if self.busy and not messagebox.askyesno(APP_NAME, "작업 중입니다. 취소하고 닫을까요?"):
            return
        if self.job:
            self.job.cancel()
        try:
            if self.player and self.player.poll() is None:
                self.player.kill()
        except Exception:
            pass
        self.s["geometry"] = self.root.geometry()
        self._save_state()
        self.root.destroy()


class AddDialog(tk.Toplevel):
    def __init__(self, app, st, en):
        super().__init__(app.root)
        self.app = app
        self.title("구간 추가")
        self.transient(app.root)
        self.resizable(False, False)
        f = ttk.Frame(self, padding=16)
        f.pack()
        ttk.Label(f, text="잘라낼 구간을 직접 넣습니다. (예: 1:02:03.5 또는 12:30)").grid(row=0, column=0, columnspan=2,
                                                                               sticky="w", pady=(0, 8))
        self.a = tk.StringVar(value=fftools.fmt_time(st, True))
        self.b = tk.StringVar(value=fftools.fmt_time(en, True))
        ttk.Label(f, text="시작").grid(row=1, column=0, sticky="e")
        ttk.Entry(f, textvariable=self.a, width=16).grid(row=1, column=1, sticky="w", padx=6, pady=3)
        ttk.Label(f, text="끝").grid(row=2, column=0, sticky="e")
        ttk.Entry(f, textvariable=self.b, width=16).grid(row=2, column=1, sticky="w", padx=6, pady=3)
        bf = ttk.Frame(f)
        bf.grid(row=3, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(bf, text="추가", command=self.ok).pack(side="left")
        ttk.Button(bf, text="닫기", command=self.destroy).pack(side="left", padx=4)

    def ok(self):
        try:
            a, b = fftools.parse_time(self.a.get()), fftools.parse_time(self.b.get())
        except Exception:
            messagebox.showerror(APP_NAME, "시간 형식을 확인해주세요.", parent=self)
            return
        d = self.app._dur()
        a, b = max(0.0, min(a, d)), max(0.0, min(b, d))
        if b - a < 0.2:
            messagebox.showerror(APP_NAME, "끝이 시작보다 뒤여야 합니다.", parent=self)
            return
        seg = {"start": round(a, 2), "end": round(b, 2), "kind": "manual", "cut": True, "auto": False}
        self.app.segs.append(seg)
        self.app._changed()
        self.app.select(self.app.segs.index(seg))
        self.destroy()


class SettingsDialog(tk.Toplevel):
    FIELDS = [
        ("freeze_min", "멈춤으로 볼 최소 시간(초)", 0.5, 10, 0.1),
        ("freeze_ratio", "멈춤 민감도 — 화면 변화가 이 % 미만이면 멈춤", 0.05, 5, 0.05),
        ("wait_len", "이보다 긴 멈춤은 '대기 화면'으로 표시(초)", 2, 120, 1),
        ("black_luma", "검은 화면 밝기 기준(0~255)", 4, 60, 1),
        ("black_min", "검은 화면 최소 시간(초)", 0.2, 10, 0.1),
        ("blur_ratio", "흐림 기준 — 평소 선명도의 이 % 미만", 5, 80, 1),
        ("blur_min", "흐림 최소 시간(초)", 0.5, 30, 0.5),
        ("merge_gap", "잘릴 구간 사이가 이보다 짧으면 합침(초)", 0, 5, 0.1),
        ("analysis_fps", "분석할 때 1초에 볼 장면 수", 2, 15, 1),
    ]

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("설정")
        self.transient(app.root)
        self.resizable(False, False)
        f = ttk.Frame(self, padding=16)
        f.pack()
        ttk.Label(f, text="자동으로 찾는 기준", font=FONT_B).grid(row=0, column=0, columnspan=2, sticky="w")
        self.vars = {}
        r = 1
        for key, label, lo, hi, inc in self.FIELDS:
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=2)
            v = tk.StringVar(value=str(app.s.get(key, analyzer.DEFAULTS[key])))
            ttk.Spinbox(f, from_=lo, to=hi, increment=inc, textvariable=v, width=8).grid(row=r, column=1, padx=8)
            self.vars[key] = (v, lo, hi)
            r += 1
        self.blur_on = tk.BooleanVar(value=bool(app.s.get("blur_on", True)))
        ttk.Checkbutton(f, text="흐림·가림 화면도 찾기", variable=self.blur_on).grid(row=r, column=0, sticky="w", pady=(6, 0))
        r += 1
        ttk.Label(f, text="그래픽카드", font=FONT_B).grid(row=r, column=0, columnspan=2, sticky="w", pady=(14, 0))
        r += 1
        self.gpu = tk.BooleanVar(value=bool(app.s.get("use_gpu", True)))
        ttk.Checkbutton(f, text="분석·디코딩에 그래픽카드 사용(안 되면 자동으로 CPU)", variable=self.gpu).grid(
            row=r, column=0, columnspan=2, sticky="w")
        r += 1
        ttk.Label(f, text="H.265 인코더").grid(row=r, column=0, sticky="w", pady=2)
        self.enc_names = [("auto", "자동 선택")] + [(e, exporter.ENC_NAMES[e]) for e in exporter.ENCODERS]
        self.enc = tk.StringVar(value=dict(self.enc_names).get(app.s.get("encoder", "auto"), "자동 선택"))
        ttk.Combobox(f, textvariable=self.enc, values=[n for _, n in self.enc_names], state="readonly",
                     width=18).grid(row=r, column=1, padx=8)
        r += 1
        bf = ttk.Frame(f)
        bf.grid(row=r, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(bf, text="기본값", command=self.reset).pack(side="left")
        ttk.Button(bf, text="저장", command=self.ok).pack(side="left", padx=4)
        ttk.Button(bf, text="닫기", command=self.destroy).pack(side="left")

    def reset(self):
        for key, (v, _, _) in self.vars.items():
            v.set(str(analyzer.DEFAULTS[key]))
        self.blur_on.set(True)
        self.gpu.set(True)
        self.enc.set("자동 선택")

    def ok(self):
        s = self.app.s
        for key, (v, lo, hi) in self.vars.items():
            try:
                x = float(v.get())
            except ValueError:
                messagebox.showerror(APP_NAME, "숫자를 확인해주세요.", parent=self)
                return
            x = min(max(x, lo), hi)
            s[key] = int(x) if key in ("analysis_fps", "black_luma", "blur_ratio") else x
        s["blur_on"] = self.blur_on.get()
        s["use_gpu"] = self.gpu.get()
        s["encoder"] = next((k for k, n in self.enc_names if n == self.enc.get()), "auto")
        save_settings(s)
        self.destroy()
        if self.app.analysis and messagebox.askyesno(APP_NAME, "바꾼 기준으로 지금 영상에서 다시 찾을까요?"):
            self.app.redetect()


class UpdateDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("업데이트 확인")
        self.transient(app.root)
        self.resizable(False, False)
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=f"지금 버전: {VERSION}", font=FONT_B).pack(anchor="w")
        self.msg = ttk.Label(f, text="새 버전을 확인하는 중...", wraplength=380, justify="left")
        self.msg.pack(anchor="w", pady=(10, 6))
        self.prog = ttk.Progressbar(f, mode="indeterminate", length=380)
        self.prog.pack(fill="x")
        self.prog.start(12)
        self.notes = tk.Text(f, height=7, width=52, wrap="word", font=FONT_S, relief="solid", bd=1)
        self.btns = ttk.Frame(f)
        self.btns.pack(fill="x", pady=(12, 0))
        ttk.Button(self.btns, text="닫기", command=self.destroy).pack(side="right")
        self.q = []
        threading.Thread(target=self._check, daemon=True).start()
        self.after(100, self._poll)

    def _check(self):
        try:
            info = updater.latest_release()
        except Exception as e:
            self.q.append(("err", f"새 버전을 확인하지 못했습니다. 인터넷 연결을 확인해주세요.\n({e})"))
            return
        if not updater.is_newer(info["tag"], VERSION):
            self.q.append(("latest", f"최신 버전을 쓰고 있습니다. ({info['tag'] or VERSION})"))
            return
        if not info["url"]:
            self.q.append(("err", f"새 버전 {info['tag']}의 설치 파일이 아직 준비되지 않았습니다. 잠시 뒤 다시 눌러주세요."))
            return
        self.q.append(("found", info))
        if not updater.is_frozen():
            self.q.append(("err", "개발용 실행이라 자동 적용은 건너뜁니다."))
            return
        try:
            p = updater.download(info["url"], lambda g, t: self.q.append(("prog", g, t or info["size"])))
            self.q.append(("apply", p))
        except Exception as e:
            self.q.append(("err", f"업데이트 중 문제가 생겼습니다.\n{e}"))

    def _poll(self):
        while self.q:
            m = self.q.pop(0)
            k = m[0]
            if k in ("err", "latest"):
                self.prog.stop()
                self.prog.config(mode="determinate", value=100 if k == "latest" else 0)
                self.msg.config(text=m[1])
                if k == "latest":
                    self.app.upd_btn.config(text="업데이트 확인")
            elif k == "found":
                info = m[1]
                self.msg.config(text=f"새 버전 {info['tag']}이 있습니다. 자동으로 내려받아 적용합니다.")
                if info["notes"].strip():
                    self.notes.insert("1.0", "바뀐 점\n" + info["notes"].strip())
                    self.notes.config(state="disabled")
                    self.notes.pack(fill="x", pady=(8, 0), before=self.btns)
                self.prog.stop()
                self.prog.config(mode="determinate", value=0, maximum=100)
            elif k == "prog":
                g, t = m[1], m[2]
                if t:
                    self.prog.config(value=g * 100 / t)
                    self.msg.config(text=f"내려받는 중... {g / 1048576:.1f} / {t / 1048576:.1f} MB")
            elif k == "apply":
                self.msg.config(text="설치 중입니다. 관리자 확인 창이 뜨면 '예'를 눌러주세요.\n설치가 끝나면 자동으로 다시 켜집니다.")
                self.update()
                try:
                    updater.run_setup(m[1])
                except Exception as e:
                    self.msg.config(text=f"설치 파일을 실행하지 못했습니다.\n{e}")
                    return
                self.app.busy = False
                self.app.on_close()
                return
        if self.winfo_exists():
            self.after(100, self._poll)


def main():
    root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
    app = App(root)
    if "--updated" in sys.argv:
        threading.Thread(target=updater.cleanup_later, daemon=True).start()
        app.status(f"{VERSION} 버전으로 업데이트했습니다.")
    if "--selftest" in sys.argv:
        if "--selftest-out" in sys.argv:
            out = open(sys.argv[sys.argv.index("--selftest-out") + 1], "w", encoding="utf-8")
            sys.stdout = sys.stderr = out
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

        def test():
            try:
                import selftest
                print("ffmpeg:", fftools.FFMPEG, "ffplay:", bool(fftools.FFPLAY), flush=True)
                ok = selftest.run(lambda *a: print(*a, flush=True))
                # 화면 쪽 점검: 테스트 영상을 열어 목록·타임라인·미리보기 확인
                import glob
                import tempfile
                src = sorted(glob.glob(os.path.join(tempfile.gettempdir(), "videocut_test_*", "테스트 영상.mp4")),
                             key=os.path.getmtime)[-1]
                app.path = src
                info = fftools.probe(src)
                a = analyzer.analyze(src, info, app.s)
                app._on_analyzed(info, a, analyzer.detect(a, info["duration"], app.s), False)
                root.update()
                app.toggle()
                app.adjust("end", 0.2)
                root.update()
                print("ui segs:", len(app.segs), "tree:", len(app.tree.get_children()), flush=True)
                print("dnd:", bool(TkinterDnD), flush=True)
                if not ok or len(app.tree.get_children()) != 4:
                    sys.exitcode = 1
                else:
                    print("ui ok", flush=True)
            except Exception:
                import traceback
                traceback.print_exc()
                sys.exitcode = 1
            finally:
                root.destroy()

        root.after(800, test)
    root.mainloop()
    if getattr(sys, "exitcode", 0):
        sys.exit(1)


if __name__ == "__main__":
    main()
