"""비디오 컷 — 라이브 녹화 영상에서 대기·렉 멈춤·흐림·검은 화면을 찾아 확인 후 잘라내는 프로그램
화면: 위 프리뷰 + 오른쪽 구간 목록 · 아래 타임라인(끌어서 이동/구간 경계 조절)"""
import json
import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageTk

import analyzer
import exporter
import fftools
import player as playmod
import repair
import updater
from version import VERSION

APP_NAME = "비디오 컷"
FONT = ("맑은 고딕", 10)
FONT_B = ("맑은 고딕", 10, "bold")
FONT_S = ("맑은 고딕", 9)
FONT_T = ("Consolas", 13, "bold")
VIDEO_EXT = (".mp4", ".mkv", ".mov", ".ts", ".flv", ".m4v", ".avi", ".webm")

# 색 (어두운 편집기 느낌)
BG = "#1c1e23"
PANEL = "#25282f"
PANEL2 = "#2f333b"
LINE = "#3b404a"
TEXT = "#e7e9ec"
SUB = "#9aa1ab"
ACCENT = "#1fc8a9"
ACCENT_D = "#159c84"
TL_BG = "#17191d"
RULER_BG = "#202329"
PLAYHEAD = "#ff4d5e"

# 타임라인 배치
RULER_H = 26
LANE_Y = 34
LANE_H = 72
RES_Y = LANE_Y + LANE_H + 10
RES_H = 10
TL_H = RES_Y + RES_H + 12
EDGE_PX = 7

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
    ("auto", "자동 (H.265 원본은 무손실)"),
    ("copy", "무손실 (빠름 · 키프레임 기준)"),
    ("encode", "H.265 인코딩 (정확하게 자름)"),
]
PREVIEW_Q = [("1/4", 0.25), ("1/2", 0.5), ("1:1", 1.0)]
SPEEDS = [("0.5x", 0.5), ("1x", 1.0), ("2x", 2.0), ("4x", 4.0)]
HANGUL_KEYS = {"ㅌ": "x", "ㅑ": "i", "ㅐ": "o", "ㅁ": "a"}


# ── 설정 ──
def settings_file():
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    d = os.path.join(base, "VideoCut")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "settings.json")


def load_settings():
    s = dict(analyzer.DEFAULTS)
    s.update({"quality": 24, "encoder": "auto", "export_mode": "auto", "geometry": "1560x940",
              "preview_q": "1/2", "darken": True, "skip_cut": False, "sound": True, "speed": "1x"})
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


def hex_rgb(h):
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float32)


def apply_theme(root):
    st = ttk.Style()
    st.theme_use("clam")
    root.configure(bg=BG)
    root.option_add("*Font", FONT)
    root.option_add("*TCombobox*Listbox.background", PANEL2)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT_D)
    root.option_add("*TCombobox*Listbox.selectForeground", "white")
    st.configure(".", background=BG, foreground=TEXT, fieldbackground=PANEL2, bordercolor=LINE,
                 lightcolor=PANEL2, darkcolor=PANEL2, troughcolor=PANEL, focuscolor=ACCENT,
                 selectbackground=ACCENT_D, selectforeground="white", insertcolor=TEXT, font=FONT,
                 arrowcolor=TEXT)
    st.configure("TFrame", background=BG)
    st.configure("Panel.TFrame", background=PANEL)
    st.configure("TLabel", background=BG, foreground=TEXT)
    st.configure("Panel.TLabel", background=PANEL, foreground=TEXT)
    st.configure("Sub.TLabel", background=PANEL, foreground=SUB, font=FONT_S)
    st.configure("SubBG.TLabel", background=BG, foreground=SUB, font=FONT_S)
    st.configure("Head.TLabel", background=PANEL, foreground=TEXT, font=FONT_B)
    st.configure("Time.TLabel", background=PANEL, foreground=ACCENT, font=FONT_T)
    st.configure("TButton", background=PANEL2, foreground=TEXT, padding=(10, 5), borderwidth=1,
                 bordercolor=LINE, focusthickness=0)
    st.map("TButton", background=[("disabled", PANEL), ("pressed", "#2a2e35"), ("active", "#3a3f49")],
           foreground=[("disabled", "#666b73")])
    st.configure("Tool.TButton", padding=(8, 3))
    st.configure("Accent.TButton", background=ACCENT, foreground="#08201b", font=FONT_B, padding=(18, 6),
                 bordercolor=ACCENT)
    st.map("Accent.TButton", background=[("disabled", "#2d5a52"), ("pressed", ACCENT_D), ("active", "#3fd8bc")])
    st.configure("Tab.TButton", background=PANEL, foreground=SUB, padding=(14, 6), bordercolor=PANEL,
                 font=FONT_B)
    st.map("Tab.TButton", background=[("active", PANEL2)])
    st.configure("TabOn.TButton", background=BG, foreground=ACCENT, padding=(14, 6), bordercolor=ACCENT,
                 font=FONT_B)
    st.map("TabOn.TButton", background=[("active", BG)])
    st.configure("Play.TButton", background=PANEL2, font=("맑은 고딕", 11, "bold"), padding=(14, 4))
    st.configure("TCheckbutton", background=PANEL, foreground=TEXT, indicatorbackground=PANEL2,
                 indicatorforeground=ACCENT, focusthickness=0)
    st.map("TCheckbutton", background=[("active", PANEL)], indicatorbackground=[("selected", PANEL2)])
    st.configure("BG.TCheckbutton", background=BG)
    st.map("BG.TCheckbutton", background=[("active", BG)])
    st.configure("TCombobox", fieldbackground=PANEL2, background=PANEL2, foreground=TEXT, arrowcolor=TEXT,
                 bordercolor=LINE)
    st.map("TCombobox", fieldbackground=[("readonly", PANEL2)], foreground=[("readonly", TEXT)],
           selectbackground=[("readonly", PANEL2)], selectforeground=[("readonly", TEXT)])
    st.configure("TEntry", fieldbackground=PANEL2, foreground=TEXT, bordercolor=LINE)
    st.configure("TSpinbox", fieldbackground=PANEL2, foreground=TEXT, background=PANEL2, arrowcolor=TEXT,
                 bordercolor=LINE)
    st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=27,
                 bordercolor=PANEL, borderwidth=0)
    st.map("Treeview", background=[("selected", ACCENT_D)], foreground=[("selected", "white")])
    st.configure("Treeview.Heading", background=PANEL2, foreground=SUB, font=FONT_S, relief="flat",
                 bordercolor=PANEL2)
    st.map("Treeview.Heading", background=[("active", PANEL2)])
    st.configure("TScrollbar", background=PANEL2, troughcolor=TL_BG, bordercolor=TL_BG, arrowcolor=SUB,
                 lightcolor=PANEL2, darkcolor=PANEL2)
    st.map("TScrollbar", background=[("active", "#454b56")])
    st.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=PANEL2, bordercolor=PANEL2,
                 lightcolor=ACCENT, darkcolor=ACCENT)
    st.configure("TLabelframe", background=PANEL, bordercolor=LINE)
    st.configure("TLabelframe.Label", background=PANEL, foreground=SUB, font=FONT_S)
    st.configure("TPanedwindow", background=LINE)
    st.configure("TSeparator", background=LINE)


def btn(parent, text, cmd, style="Tool.TButton", **kw):
    b = ttk.Button(parent, text=text, command=cmd, style=style, takefocus=0, **kw)
    return b


class App:
    def __init__(self, root):
        self.root = root
        self.s = load_settings()
        self.path = None
        self.info = None
        self.analysis = None
        self.segs = []
        self.sel = None
        self.job = None          # 내보내기 작업
        self.fg_job = None       # 지금 보는 영상 분석
        self.exporting = False
        self.items = []          # 영상 목록 [{path, include, reviewed, state, pct, dur}]
        self.aw_queue = []       # 백그라운드 분석 대기
        self.aw_lock = threading.Lock()
        self.aw_thread = None
        self.aw_job = None
        self.aw_current = None
        self.fg_path = None
        self.q = []
        self.view = (0.0, 1.0)
        self.t = 0.0
        self.slot = playmod.FrameSlot()
        self.player = playmod.Player(self.slot)
        self.scrub = playmod.Scrubber(self.slot)
        self.shown_seq = 0
        self.frames_shown = 0
        self.last_img = None
        self.photo = None
        self.drag = None
        self.lane_photo = None

        root.title(f"{APP_NAME} {VERSION}")
        try:
            root.iconbitmap(default=os.path.join(fftools.app_dir(), "icon.ico"))
        except Exception:
            pass
        root.geometry(self.s.get("geometry", "1560x940"))
        root.minsize(1240, 780)
        apply_theme(root)
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self._poll)
        root.after(16, self._display_loop)
        threading.Thread(target=self._quiet_check, daemon=True).start()
        root.after(300, self._restore_queue)
        if not fftools.available():
            self.status("FFmpeg 를 찾지 못했습니다. 프로그램을 다시 설치해주세요.")

    # ════════════════ 화면 구성 ════════════════
    def _build(self):
        r = self.root
        r.grid_columnconfigure(0, weight=1)
        r.grid_rowconfigure(1, weight=1)

        # 위 막대
        top = ttk.Frame(r, style="Panel.TFrame", padding=(10, 7))
        top.grid(row=0, column=0, sticky="ew")
        self.tab_edit = btn(top, "자르기·인코딩", lambda: self.show_page("edit"), "TabOn.TButton")
        self.tab_edit.pack(side="left")
        self.tab_fix = btn(top, "깨진 영상 복구", lambda: self.show_page("fix"), "Tab.TButton")
        self.tab_fix.pack(side="left", padx=(2, 14))
        btn(top, "영상 추가", self.open_file, "TButton").pack(side="left")
        self.file_lbl = ttk.Label(top, text="영상 파일을 추가하거나 이 창에 끌어다 놓으세요. (여러 개 가능)", style="Sub.TLabel")
        self.file_lbl.pack(side="left", padx=14)
        self.export_btn = btn(top, "체크한 영상 모두 내보내기", self.export_all, "Accent.TButton")
        self.export_btn.pack(side="right")
        btn(top, "설정", lambda: SettingsDialog(self), "TButton").pack(side="right", padx=6)
        self.upd_btn = btn(top, "업데이트 확인", lambda: UpdateDialog(self), "TButton")
        self.upd_btn.pack(side="right")

        # 가운데: 프리뷰 + 오른쪽 패널
        mid = ttk.PanedWindow(r, orient="horizontal")
        mid.grid(row=1, column=0, sticky="nsew", pady=(1, 0))
        self.page_mid = mid
        self.page = "edit"
        left = ttk.Frame(mid, style="TFrame")
        right = ttk.Frame(mid, style="Panel.TFrame", width=410)
        mid.add(left, weight=4)
        mid.add(right, weight=1)

        self.pv = tk.Canvas(left, bg="#000000", highlightthickness=0, cursor="arrow")
        self.pv.pack(fill="both", expand=True)
        self.pv_img = self.pv.create_image(0, 0, anchor="center")
        self.pv_msg = self.pv.create_text(0, 0, text="영상을 열거나 끌어다 놓으세요", fill=SUB,
                                          font=("맑은 고딕", 14))
        self.pv_badge_bg = self.pv.create_rectangle(0, 0, 0, 0, fill="#c62838", outline="", state="hidden")
        self.pv_badge = self.pv.create_text(0, 0, text="", fill="white", font=FONT_B, anchor="nw", state="hidden")
        self.pv_frame = self.pv.create_rectangle(0, 0, 0, 0, outline="#c62838", width=3, state="hidden")
        self.pv.bind("<Configure>", lambda e: self._layout_preview())
        self.pv.bind("<Button-1>", lambda e: self.toggle_play())

        tr = ttk.Frame(left, style="Panel.TFrame", padding=(10, 6))
        tr.pack(fill="x")
        self.time_lbl = ttk.Label(tr, text="00:00.0 / 00:00", style="Time.TLabel", width=22)
        self.time_lbl.pack(side="left")
        mid_btns = ttk.Frame(tr, style="Panel.TFrame")
        mid_btns.pack(side="left", padx=(10, 0))
        btn(mid_btns, "|◀ 이전 구간", lambda: self.jump_seg(-1)).pack(side="left")
        btn(mid_btns, "◀ 1초", lambda: self.step(-1.0)).pack(side="left", padx=2)
        self.play_btn = btn(mid_btns, "▶ 재생", self.toggle_play, "Play.TButton", width=8)
        self.play_btn.pack(side="left", padx=4)
        btn(mid_btns, "1초 ▶", lambda: self.step(1.0)).pack(side="left", padx=2)
        btn(mid_btns, "다음 구간 ▶|", lambda: self.jump_seg(1)).pack(side="left")

        opt = ttk.Frame(tr, style="Panel.TFrame")
        opt.pack(side="right")
        self.dark_var = tk.BooleanVar(value=bool(self.s.get("darken", True)))
        self.skip_var = tk.BooleanVar(value=bool(self.s.get("skip_cut", False)))
        self.sound_var = tk.BooleanVar(value=bool(self.s.get("sound", True)))
        ttk.Checkbutton(opt, text="잘릴 부분 어둡게", variable=self.dark_var, takefocus=0,
                        command=self._opt_changed).pack(side="left", padx=4)
        ttk.Checkbutton(opt, text="잘릴 부분 건너뛰고 재생", variable=self.skip_var, takefocus=0,
                        command=self._opt_changed).pack(side="left", padx=4)
        ttk.Checkbutton(opt, text="소리", variable=self.sound_var, takefocus=0,
                        command=self._opt_changed).pack(side="left", padx=4)
        ttk.Label(opt, text="속도", style="Sub.TLabel").pack(side="left", padx=(10, 3))
        self.speed_var = tk.StringVar(value=self.s.get("speed", "1x"))
        cb = ttk.Combobox(opt, textvariable=self.speed_var, values=[k for k, _ in SPEEDS], state="readonly", width=5,
                          takefocus=0)
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self._opt_changed(restart=True))
        ttk.Label(opt, text="프리뷰 화질", style="Sub.TLabel").pack(side="left", padx=(10, 3))
        self.pq_var = tk.StringVar(value=self.s.get("preview_q", "1/2"))
        cb = ttk.Combobox(opt, textvariable=self.pq_var, values=[k for k, _ in PREVIEW_Q], state="readonly", width=5,
                          takefocus=0)
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self._opt_changed(restart=True))

        # 오른쪽 패널: 영상 목록 / 잘라낼 구간 / 내보내기
        rpw = ttk.PanedWindow(right, orient="vertical")
        rpw.pack(fill="both", expand=True)
        qp = ttk.Frame(rpw, style="Panel.TFrame", padding=(12, 10, 12, 6))
        rp = ttk.Frame(rpw, style="Panel.TFrame", padding=(12, 8, 12, 10))
        rpw.add(qp, weight=2)
        rpw.add(rp, weight=3)
        qh = ttk.Frame(qp, style="Panel.TFrame")
        qh.pack(fill="x")
        ttk.Label(qh, text="영상 목록", style="Head.TLabel").pack(side="left")
        self.q_lbl = ttk.Label(qh, text="", style="Sub.TLabel")
        self.q_lbl.pack(side="left", padx=8)
        qf = ttk.Frame(qp, style="Panel.TFrame")
        qf.pack(fill="both", expand=True, pady=(6, 0))
        qcols = ("chk", "name", "len", "state")
        self.qtree = ttk.Treeview(qf, columns=qcols, show="headings", selectmode="browse", height=4)
        for c, t, w in zip(qcols, ("출력", "파일", "길이", "상태"), (44, 170, 66, 96)):
            self.qtree.heading(c, text=t)
            self.qtree.column(c, width=w, anchor="w" if c == "name" else "center", stretch=c == "name")
        qsb = ttk.Scrollbar(qf, orient="vertical", command=self.qtree.yview)
        self.qtree.configure(yscrollcommand=qsb.set)
        self.qtree.pack(side="left", fill="both", expand=True)
        qsb.pack(side="right", fill="y")
        self.qtree.tag_configure("off", foreground="#6f7680")
        self.qtree.tag_configure("done", foreground="#6fd3a3")
        self.qtree.tag_configure("err", foreground="#ff7b85")
        self.qtree.tag_configure("cur", font=FONT_B)
        self.qtree.bind("<ButtonRelease-1>", self._qtree_click)
        self.qtree.bind("<<TreeviewSelect>>", self._qtree_sel)
        qb = ttk.Frame(qp, style="Panel.TFrame")
        qb.pack(fill="x", pady=(6, 0))
        btn(qb, "영상 추가", self.open_file).pack(side="left")
        btn(qb, "빼기", self.remove_item).pack(side="left", padx=4)
        btn(qb, "완료 지우기", self.clear_done).pack(side="left")
        ttk.Label(qb, text="출력 칸 클릭 = 체크/해제", style="Sub.TLabel").pack(side="right")
        ttk.Label(rp, text="잘라낼 구간", style="Head.TLabel").pack(anchor="w")
        self.sum_lbl = ttk.Label(rp, text="영상을 열면 자동으로 찾습니다.", style="Sub.TLabel", justify="left")
        self.sum_lbl.pack(anchor="w", pady=(2, 6))
        tf = ttk.Frame(rp, style="Panel.TFrame")
        tf.pack(fill="both", expand=True)
        cols = ("no", "kind", "start", "len", "act")
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", selectmode="browse", height=4)
        for c, t, w in zip(cols, ("#", "종류", "시작", "길이", "처리"), (38, 84, 92, 64, 60)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="center", stretch=c == "kind")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("keep", foreground="#6f7680")
        for k, c in analyzer.KIND_COLORS.items():
            self.tree.tag_configure("k_" + k, foreground=TEXT)
        self.tree.bind("<<TreeviewSelect>>", self._tree_sel)
        rb = ttk.Frame(rp, style="Panel.TFrame")
        rb.pack(fill="x", pady=(8, 0))
        btn(rb, "다시 찾기", self.redetect).pack(side="left")
        btn(rb, "모두 자르기", lambda: self.all_cut(True)).pack(side="left", padx=4)
        btn(rb, "모두 살리기", lambda: self.all_cut(False)).pack(side="left")

        ex = ttk.LabelFrame(rp, text="내보내기 설정", padding=(10, 8))
        ex.pack(fill="x", pady=(14, 0))
        ex.grid_columnconfigure(1, weight=1)
        ttk.Label(ex, text="방식", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        self.mode_var = tk.StringVar(value=dict(EXPORT_MODES).get(self.s.get("export_mode", "auto")))
        ttk.Combobox(ex, textvariable=self.mode_var, values=[m[1] for m in EXPORT_MODES], state="readonly",
                     takefocus=0).grid(row=0, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=2)
        ttk.Label(ex, text="화질", style="Panel.TLabel").grid(row=1, column=0, sticky="w")
        qf = ttk.Frame(ex, style="Panel.TFrame")
        qf.grid(row=1, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=2)
        self.q_var = tk.IntVar(value=int(self.s.get("quality", 24)))
        ttk.Spinbox(qf, from_=16, to=36, textvariable=self.q_var, width=5).pack(side="left")
        ttk.Label(qf, text="낮을수록 좋음 (인코딩 때만)", style="Sub.TLabel").pack(side="left", padx=6)
        ttk.Label(ex, text="출력 폴더", style="Panel.TLabel").grid(row=2, column=0, sticky="w")
        self.dir_var = tk.StringVar(value=self.s.get("out_dir", ""))
        ttk.Entry(ex, textvariable=self.dir_var).grid(row=2, column=1, sticky="ew", padx=(8, 4), pady=2)
        btn(ex, "선택", self.pick_dir).grid(row=2, column=2)
        ttk.Label(ex, text="비워두면 원본과 같은 폴더 · 파일 이름 뒤에 '_편집'", style="Sub.TLabel").grid(
            row=3, column=1, columnspan=2, sticky="w", padx=(8, 0))
        eb = ttk.Frame(ex, style="Panel.TFrame")
        eb.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.one_btn = btn(eb, "지금 보는 영상만 내보내기", self.export_current)
        self.one_btn.pack(side="left")
        btn(eb, "원본 폴더로", lambda: self.dir_var.set("")).pack(side="right")
        # 내보내기 설정은 항상 보이게: 아래부터 채우고 목록이 남는 공간을 씀
        for w in (tf, rb, ex):
            w.pack_forget()
        ex.pack(side="bottom", fill="x", pady=(12, 0))
        rb.pack(side="bottom", fill="x", pady=(8, 0))
        tf.pack(fill="both", expand=True)

        # 타임라인
        tlw = ttk.Frame(r, style="TFrame")
        tlw.grid(row=2, column=0, sticky="ew")
        self.page_tl = tlw
        tb = ttk.Frame(tlw, style="Panel.TFrame", padding=(10, 5))
        tb.pack(fill="x", pady=(1, 0))
        btn(tb, "－", lambda: self._zoom_center(1.5), width=3).pack(side="left")
        btn(tb, "＋", lambda: self._zoom_center(1 / 1.5), width=3).pack(side="left", padx=2)
        btn(tb, "전체 보기", lambda: self._set_view(0, self._dur())).pack(side="left")
        ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=10)
        self.toggle_btn = btn(tb, "자르기/살리기 (X)", self.toggle)
        self.toggle_btn.pack(side="left")
        btn(tb, "구간 삭제 (Del)", self.delete_seg).pack(side="left", padx=2)
        btn(tb, "시작을 여기로 (I)", lambda: self.set_edge_here("start")).pack(side="left", padx=2)
        btn(tb, "끝을 여기로 (O)", lambda: self.set_edge_here("end")).pack(side="left", padx=2)
        btn(tb, "여기에 구간 추가 (A)", self.add_here).pack(side="left", padx=2)
        lg = ttk.Frame(tb, style="Panel.TFrame")
        lg.pack(side="right")
        for k in ("start", "freeze", "wait", "blur", "black", "end", "manual"):
            tk.Label(lg, text="", bg=analyzer.KIND_COLORS[k], width=2, height=1, bd=0).pack(side="left", padx=(8, 3))
            ttk.Label(lg, text=analyzer.KINDS[k], style="Sub.TLabel").pack(side="left")

        self.tl = tk.Canvas(tlw, height=TL_H, bg=TL_BG, highlightthickness=0)
        self.tl.pack(fill="x")
        self.hbar = ttk.Scrollbar(tlw, orient="horizontal", command=self._scroll_view)
        self.hbar.pack(fill="x")
        self.tl.bind("<Configure>", lambda e: self.draw_timeline())
        self.tl.bind("<ButtonPress-1>", self._tl_press)
        self.tl.bind("<B1-Motion>", self._tl_motion)
        self.tl.bind("<ButtonRelease-1>", self._tl_release)
        self.tl.bind("<Motion>", self._tl_hover)
        self.tl.bind("<Double-Button-1>", lambda e: self._set_view(0, self._dur()))
        self.tl.bind("<MouseWheel>", self._tl_wheel)
        self.tl.bind("<Shift-MouseWheel>", lambda e: self._pan(-1 if e.delta > 0 else 1))
        self.tl.bind("<Button-4>", lambda e: self._zoom_at(e.x, 0.8))
        self.tl.bind("<Button-5>", lambda e: self._zoom_at(e.x, 1.25))

        # 아래 상태줄
        bot = ttk.Frame(r, style="Panel.TFrame", padding=(10, 5))
        bot.grid(row=3, column=0, sticky="ew", pady=(1, 0))
        self.status_lbl = ttk.Label(bot, text="준비", style="Panel.TLabel", width=70)
        self.status_lbl.pack(side="left")
        self.cancel_btn = btn(bot, "중지", self.cancel, state="disabled")
        self.cancel_btn.pack(side="right")
        self.pause_btn = btn(bot, "일시정지", self.toggle_job_pause, state="disabled", width=9)
        self.pause_btn.pack(side="right", padx=(0, 4))
        self.prog = ttk.Progressbar(bot, maximum=1000)
        self.prog.pack(side="right", fill="x", expand=True, padx=8)

        self._build_fix_page(r)

        if TkinterDnD and DND_FILES:
            try:
                r.drop_target_register(DND_FILES)
                r.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass
        r.bind_all("<Key>", self._on_key)
        r.bind_all("<Control-o>", lambda e: self.open_file())

    # ════════════════ 깨진 영상 복구 ════════════════
    def _build_fix_page(self, r):
        pg = ttk.Frame(r, style="Panel.TFrame", padding=(24, 18))
        self.page_fix = pg
        ttk.Label(pg, text="깨진 영상 복구", style="Head.TLabel", font=("맑은 고딕", 14, "bold")).pack(anchor="w")
        ttk.Label(pg, text="녹화가 갑자기 멈춰 재생되지 않는 영상(삼성 녹화 .pending-… 파일 등)을 고칩니다. "
                           "이름·확장자가 이상해도 그대로 넣으면 됩니다.", style="Sub.TLabel").pack(anchor="w", pady=(4, 14))
        rf = ttk.Frame(pg, style="Panel.TFrame")
        rf.pack(fill="x")
        ttk.Label(rf, text="기준 영상", style="Panel.TLabel", width=10).pack(side="left")
        self.ref_var = tk.StringVar(value=self.s.get("repair_ref", ""))
        ttk.Entry(rf, textvariable=self.ref_var).pack(side="left", fill="x", expand=True, padx=6)
        btn(rf, "선택", self.pick_ref).pack(side="left")
        ttk.Label(pg, text="같은 폰·같은 녹화 기능·같은 화질로 정상 저장된 영상 하나 (짧아도 됩니다). "
                           "한 번 정하면 계속 기억합니다.", style="Sub.TLabel").pack(anchor="w", padx=(86, 0), pady=(3, 12))
        lf = ttk.Frame(pg, style="Panel.TFrame")
        lf.pack(fill="both", expand=True)
        cols = ("name", "size", "state")
        self.ftree = ttk.Treeview(lf, columns=cols, show="headings", selectmode="extended", height=6)
        for c, t, w in zip(cols, ("파일", "크기", "상태"), (520, 100, 420)):
            self.ftree.heading(c, text=t)
            self.ftree.column(c, width=w, anchor="w" if c != "size" else "center", stretch=c != "size")
        fsb = ttk.Scrollbar(lf, orient="vertical", command=self.ftree.yview)
        self.ftree.configure(yscrollcommand=fsb.set)
        self.ftree.pack(side="left", fill="both", expand=True)
        fsb.pack(side="right", fill="y")
        self.ftree.tag_configure("done", foreground="#6fd3a3")
        self.ftree.tag_configure("err", foreground="#ff7b85")
        bf = ttk.Frame(pg, style="Panel.TFrame")
        bf.pack(fill="x", pady=(8, 0))
        btn(bf, "깨진 파일 추가", self.add_broken_dialog).pack(side="left")
        btn(bf, "빼기", self.remove_broken).pack(side="left", padx=4)
        btn(bf, "목록 비우기", self.clear_broken).pack(side="left")
        ttk.Label(bf, text="파일을 이 창에 끌어다 놓아도 됩니다.", style="Sub.TLabel").pack(side="left", padx=12)
        of = ttk.Frame(pg, style="Panel.TFrame")
        of.pack(fill="x", pady=(16, 0))
        ttk.Label(of, text="저장 폴더", style="Panel.TLabel", width=10).pack(side="left")
        self.fix_dir_var = tk.StringVar(value=self.s.get("repair_dir", ""))
        ttk.Entry(of, textvariable=self.fix_dir_var).pack(side="left", fill="x", expand=True, padx=6)
        btn(of, "선택", self.pick_fix_dir).pack(side="left")
        btn(of, "원본 폴더로", lambda: self.fix_dir_var.set("")).pack(side="left", padx=4)
        ttk.Label(pg, text="비워두면 깨진 파일과 같은 폴더에 '이름_복구.mp4'로 저장 (원본은 그대로 둡니다)",
                  style="Sub.TLabel").pack(anchor="w", padx=(86, 0), pady=(3, 0))
        cf = ttk.Frame(pg, style="Panel.TFrame")
        cf.pack(fill="x", pady=(14, 0))
        self.fix_add_var = tk.BooleanVar(value=bool(self.s.get("repair_add", True)))
        ttk.Checkbutton(cf, text="복구한 영상을 '자르기·인코딩' 목록에 자동으로 추가", variable=self.fix_add_var,
                        takefocus=0).pack(side="left")
        self.fix_btn = btn(cf, "복구 시작", self.start_repair, "Accent.TButton")
        self.fix_btn.pack(side="right")
        self.broken = []        # [{path, state, msg, out}]

    def show_page(self, page):
        if page == self.page:
            return
        self.page = page
        if page == "fix":
            self.pause()
            self.page_mid.grid_remove()
            self.page_tl.grid_remove()
            self.page_fix.grid(row=1, column=0, rowspan=2, sticky="nsew", pady=(1, 0))
        else:
            self.page_fix.grid_remove()
            self.page_mid.grid()
            self.page_tl.grid()
        self.tab_edit.config(style="TabOn.TButton" if page == "edit" else "Tab.TButton")
        self.tab_fix.config(style="TabOn.TButton" if page == "fix" else "Tab.TButton")

    def pick_ref(self):
        p = filedialog.askopenfilename(title="기준 영상 (정상 녹화 파일)",
                                       initialdir=os.path.dirname(self.ref_var.get()) or self.s.get("last_dir") or None,
                                       filetypes=[("영상 파일", " ".join("*" + x for x in VIDEO_EXT)), ("모든 파일", "*.*")])
        if p:
            self.ref_var.set(os.path.normpath(p))
            self._save_fix_settings()

    def pick_fix_dir(self):
        d = filedialog.askdirectory(title="복구 파일 저장 폴더", initialdir=self.fix_dir_var.get() or None)
        if d:
            self.fix_dir_var.set(os.path.normpath(d))
            self._save_fix_settings()

    def _save_fix_settings(self):
        self.s["repair_ref"] = self.ref_var.get().strip()
        self.s["repair_dir"] = self.fix_dir_var.get().strip()
        self.s["repair_add"] = self.fix_add_var.get()
        self._save_settings_only()

    def add_broken_dialog(self):
        paths = filedialog.askopenfilenames(title="깨진 영상 파일 추가", initialdir=self.s.get("last_dir") or None,
                                            filetypes=[("모든 파일", "*.*"), ("영상 파일", " ".join("*" + x for x in VIDEO_EXT))])
        if paths:
            self.add_broken(list(paths))

    def add_broken(self, paths):
        for p in paths:
            p = os.path.normpath(os.path.abspath(p))
            if not os.path.isfile(p) or any(b["path"] == p for b in self.broken):
                continue
            b = {"path": p, "state": "대기", "tag": ""}
            self.broken.append(b)
            self.ftree.insert("", "end", iid=p, values=(os.path.basename(p), f"{os.path.getsize(p) / 1048576:,.0f} MB",
                                                        "대기"))

    def _frow(self, b):
        if self.ftree.exists(b["path"]):
            self.ftree.item(b["path"], tags=(b.get("tag", ""),), values=(
                os.path.basename(b["path"]), self.ftree.item(b["path"], "values")[1], b["state"]))

    def remove_broken(self):
        if self.exporting:
            return
        for iid in self.ftree.selection():
            self.broken = [b for b in self.broken if b["path"] != iid]
            self.ftree.delete(iid)

    def clear_broken(self):
        if self.exporting:
            return
        self.broken = []
        self.ftree.delete(*self.ftree.get_children())

    def start_repair(self):
        if self.exporting:
            messagebox.showinfo(APP_NAME, "다른 작업(내보내기·복구)이 끝난 뒤 시작해주세요.")
            return
        todo = [b for b in self.broken if b["state"] != "완료"]
        if not todo:
            messagebox.showinfo(APP_NAME, "복구할 파일을 목록에 넣어주세요.")
            return
        self._save_fix_settings()
        ref = self.ref_var.get().strip()
        d = self.fix_dir_var.get().strip()
        self._set_exporting(True)
        self.job = fftools.Job()
        job = self.job
        plan = [(b["path"], repair.default_out(b["path"], d)) for b in todo]
        add = self.fix_add_var.get()

        def work():
            done = []
            for k, (src, out) in enumerate(plan):
                if job.cancelled:
                    break
                name = os.path.basename(src)
                self.post("fix", src, "복구 중...", "")
                try:
                    msg = repair.repair(src, ref, out, job,
                                        progress=lambda r, m, k=k: self.post("bprog", (k + r) / len(plan),
                                                                             f"[{k + 1}/{len(plan)}] {name} — {m}"))
                    self.post("fix", src, f"완료 → {os.path.basename(out)}  ·  {msg}", "done")
                    done.append(out)
                except Exception as e:
                    if job.cancelled:
                        self.post("fix", src, "중지됨", "")
                        break
                    self.post("fix", src, "실패: " + str(e).splitlines()[0], "err")
                    self.post("fixerr", name, str(e))
            self.post("fix_done", done, add, job.cancelled)

        threading.Thread(target=work, daemon=True).start()

    # ════════════════ 작업 일시정지 ════════════════
    def toggle_job_pause(self):
        if not self.exporting or not self.job:
            return
        if self.job.paused:
            self.job.resume()
            self.pause_btn.config(text="일시정지")
            self.status("이어서 진행합니다.")
        else:
            self.job.pause()
            self.pause_btn.config(text="계속")
            self.status("일시정지했습니다. '계속'을 누르면 멈춘 곳에서 이어갑니다. (프로그램을 꺼도 다음에 이어서 할 수 있어요)")

    # ════════════════ 공용 ════════════════
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
        if kind == "aprog":
            path, r, msg = m[1], m[2], m[3]
            it = self._item(path)
            if it:
                it["state"], it["pct"] = "analyzing", r
                self._qrow(it)
            if path == self.path:
                self.prog.config(value=int(r * 1000))
                self.status(msg)
        elif kind == "bprog":
            self.prog.config(value=int(m[1] * 1000))
            self.status(m[2])
        elif kind == "probed":
            self._on_probed(m[1], m[2])
        elif kind == "analyzed":
            self._on_analyzed(*m[1:])
        elif kind == "afail":
            path, msg = m[1], m[2]
            it = self._item(path)
            if it:
                it["state"] = "error" if msg != "취소됨" else "wait"
                it["msg"] = msg
                self._qrow(it)
            if path == self.path:
                self.prog.config(value=0)
                self.status(msg.splitlines()[0])
                if msg != "취소됨":
                    messagebox.showerror(APP_NAME, f"{os.path.basename(path)}\n\n{msg}\n\n"
                                                   "녹화가 중간에 끊긴 파일이면 위쪽 '깨진 영상 복구'에서 고칠 수 있습니다.")
        elif kind == "item":
            it = self._item(m[1])
            if it:
                changed = "state" in m[2] and m[2]["state"] != it.get("state")
                it.update(m[2])
                self._qrow(it)
                if changed or "out" in m[2]:
                    self._save_queue()          # 진행 상황 바로 저장
        elif kind == "batch_done":
            self._batch_done(m[1], m[2])
        elif kind == "fix":
            b = next((x for x in self.broken if x["path"] == m[1]), None)
            if b:
                b["state"] = "완료" if m[3] == "done" else m[2]
                b["tag"] = m[3]
                self._frow(b)
                if m[3] == "done":
                    self.ftree.set(b["path"], "state", m[2])
        elif kind == "fixerr":
            messagebox.showerror(APP_NAME, f"{m[1]}\n\n{m[2]}")
        elif kind == "fix_done":
            done, add, cancelled = m[1], m[2], m[3]
            self._set_exporting(False)
            self.prog.config(value=1000 if done else 0)
            self.status(f"복구 {'중지' if cancelled else '끝'} — 성공 {len(done)}개")
            if done and add:
                self.add_files(done, select=False)
                self.status(f"복구 끝 — {len(done)}개를 '자르기·인코딩' 목록에 추가했습니다.")
        elif kind == "upd":
            self.upd_btn.config(text=m[1])

    def _set_exporting(self, b):
        self.exporting = b
        st = "disabled" if b else "normal"
        self.export_btn.config(state=st)
        self.one_btn.config(state=st)
        self.fix_btn.config(state=st)
        self.pause_btn.config(state="normal" if b else "disabled", text="일시정지")
        self.cancel_btn.config(state="normal" if b else ("normal" if self.fg_job else "disabled"))
        self._update_export_btn()

    def _update_export_btn(self):
        n = sum(1 for it in self.items if it["include"] and it["state"] == "paused")
        self.export_btn.config(text=f"이어서 내보내기 ({n}개 멈춤)" if n and not self.exporting
                               else "체크한 영상 모두 내보내기")

    def _dur(self):
        return self.info["duration"] if self.info else 1.0

    def _quiet_check(self):
        try:
            info = updater.latest_release(timeout=8)
            if updater.is_newer(info["tag"], VERSION):
                self.post("upd", f"새 버전 {info['tag']} 있음!")
        except Exception:
            pass

    def _opt_changed(self, restart=False):
        self.s["darken"] = self.dark_var.get()
        self.s["skip_cut"] = self.skip_var.get()
        self.s["sound"] = self.sound_var.get()
        self.s["speed"] = self.speed_var.get()
        self.s["preview_q"] = self.pq_var.get()
        self._save_settings_only()
        if self.player.playing and restart:
            self.play(self.t)
        elif self.player.playing and not self.sound_var.get():
            self.play(self.t)
        elif not self.player.playing:
            if restart:
                self.show_at(self.t)
            elif self.last_img is not None:
                self._render(self.last_img, self.t)

    # ════════════════ 열기·분석 ════════════════
    def _on_drop(self, e):
        files = [f for f in self.root.tk.splitlist(e.data) if os.path.isfile(f)]
        if self.page == "fix":
            self.add_broken(files)
            return
        vids = [f for f in files if f.lower().endswith(VIDEO_EXT)] or files
        if vids:
            self.add_files(vids)

    def open_file(self, path=None):
        if self.page == "fix" and not path:
            self.add_broken_dialog()
            return
        if path:
            self.add_files([path])
            return
        paths = filedialog.askopenfilenames(
            title="영상 추가 (여러 개 선택 가능)", initialdir=self.s.get("last_dir") or None,
            filetypes=[("영상 파일", " ".join("*" + x for x in VIDEO_EXT)), ("모든 파일", "*.*")])
        if paths:
            self.add_files(list(paths))

    # ════════════════ 영상 목록 ════════════════
    def _item(self, path):
        return next((it for it in self.items if it["path"] == path), None)

    def _cached(self, path):
        try:
            return os.path.isfile(analyzer.cache_path(path, int(self.s.get("analysis_fps", 5))))
        except OSError:
            return False

    def add_files(self, paths, select=True):
        added = []
        for p in paths:
            p = os.path.normpath(os.path.abspath(p))
            if not os.path.isfile(p) or self._item(p):
                continue
            it = {"path": p, "include": True, "reviewed": False, "state": "wait", "pct": 0.0, "dur": None}
            if self._cached(p):
                it["state"] = "ready"
            self.items.append(it)
            added.append(it)
            self.s["last_dir"] = os.path.dirname(p)
        for it in added:
            self.qtree.insert("", "end", iid=it["path"], values=("", "", "", ""))
            self._qrow(it)
            threading.Thread(target=self._probe_len, args=(it["path"],), daemon=True).start()
            if it["state"] == "wait":
                self.queue_analysis(it["path"])
        self._save_queue()
        if added and select and (self.path is None or len(added) == len(self.items)):
            self.switch_to(added[0]["path"])
        elif added:
            self.status(f"영상 {len(added)}개를 목록에 추가했습니다. 뒤에서 차례로 분석합니다.")

    def _probe_len(self, path):
        try:
            d = fftools.probe(path)["duration"]
            self.post("item", path, {"dur": d})
        except Exception:
            pass

    def _qrow(self, it):
        if not self.qtree.exists(it["path"]):
            return
        st = it["state"]
        if st == "wait":
            txt = "분석 대기"
        elif st == "analyzing":
            txt = f"분석 {int(it.get('pct', 0) * 100)}%"
        elif st == "ready":
            txt = "확인함" if it.get("reviewed") else "미확인"
        elif st == "exporting":
            txt = f"내보내는 중 {int(it.get('pct', 0) * 100)}%"
        elif st == "done":
            txt = "완료"
        elif st == "paused":
            txt = f"멈춤 {int(it.get('pct', 0) * 100)}%"
        else:
            txt = "오류"
        tags = []
        if not it["include"]:
            tags.append("off")
        if st == "done":
            tags.append("done")
        if st == "error":
            tags.append("err")
        if it["path"] == self.path:
            tags.append("cur")
        name = os.path.basename(it["path"])
        self.qtree.item(it["path"], tags=tags, values=(
            "✔" if it["include"] else "—", ("▶ " if it["path"] == self.path else "") + name,
            fftools.fmt_time(it["dur"]) if it.get("dur") else "", txt))
        n_on = sum(1 for x in self.items if x["include"])
        self.q_lbl.config(text=f"{len(self.items)}개 · 출력 체크 {n_on}개")
        self._update_export_btn()

    def _qtree_click(self, e):
        if self.qtree.identify_region(e.x, e.y) != "cell" or self.qtree.identify_column(e.x) != "#1":
            return
        row = self.qtree.identify_row(e.y)
        it = self._item(row)
        if it:
            it["include"] = not it["include"]
            self._qrow(it)
            self._save_queue()

    def _qtree_sel(self, _e=None):
        sel = self.qtree.selection()
        if sel and sel[0] != self.path:
            self.switch_to(sel[0])

    def remove_item(self):
        sel = self.qtree.selection()
        if not sel:
            return
        path = sel[0]
        it = self._item(path)
        if not it or it["state"] == "exporting":
            return
        idx = self.items.index(it)
        self.items.remove(it)
        with self.aw_lock:
            if path in self.aw_queue:
                self.aw_queue.remove(path)
        if self.aw_current == path and self.aw_job:
            self.aw_job.cancel()
        self.qtree.delete(path)
        self._save_queue()
        if path == self.path:
            if self.items:
                self.switch_to(self.items[min(idx, len(self.items) - 1)]["path"])
            else:
                self._clear_editor()
        for x in self.items:
            self._qrow(x)

    def clear_done(self):
        for it in [x for x in self.items if x["state"] == "done"]:
            if it["path"] == self.path:
                continue
            self.items.remove(it)
            self.qtree.delete(it["path"])
        self._save_queue()
        for x in self.items:
            self._qrow(x)

    def _save_queue(self):
        q = []
        for it in self.items:
            st = it["state"]
            if st == "exporting":
                st = "paused"
            q.append({"path": it["path"], "include": it["include"], "reviewed": it.get("reviewed", False),
                      "done": st == "done", "state": st if st in ("done", "paused") else "",
                      "pct": it.get("pct", 0.0) if st == "paused" else 0.0, "out": it.get("out", "")})
        self.s["queue"] = q
        self.s["current"] = self.path
        self._save_settings_only()

    def _restore_queue(self):
        q = [x for x in self.s.get("queue", []) if os.path.isfile(x.get("path", ""))]
        if not q:
            return
        self.add_files([x["path"] for x in q], select=False)
        for x in q:
            it = self._item(os.path.normpath(os.path.abspath(x["path"])))
            if it:
                it["include"] = x.get("include", True)
                it["reviewed"] = x.get("reviewed", False)
                if x.get("done") and it["state"] == "ready":
                    it["state"] = "done"
                elif x.get("state") == "paused":
                    it["state"] = "paused"
                    it["out"] = x.get("out", "")
                    it["pct"] = x.get("pct", 0.0)
                self._qrow(it)
        cur = self.s.get("current")
        target = self._item(os.path.normpath(cur)) if cur else None
        self.switch_to((target or self.items[0])["path"])
        n = sum(1 for it in self.items if it["state"] == "paused")
        if n:
            self.root.after(1500, lambda: self.status(
                f"지난번에 멈춘 내보내기가 {n}개 있습니다. 오른쪽 위 '이어서 내보내기'를 누르면 멈춘 곳부터 계속합니다."))

    def _clear_editor(self):
        self.pause()
        self.path = self.info = self.analysis = None
        self.segs, self.sel, self.t = [], None, 0.0
        self.last_img = None
        self.pv.itemconfig(self.pv_img, image="")
        self.pv.itemconfig(self.pv_msg, state="normal")
        for it in (self.pv_badge_bg, self.pv_badge, self.pv_frame):
            self.pv.itemconfig(it, state="hidden")
        self.file_lbl.config(text="영상 파일을 추가하거나 이 창에 끌어다 놓으세요. (여러 개 가능)", foreground=SUB)
        self.sum_lbl.config(text="")
        self.refresh()
        self._update_time()

    # ════════════════ 분석 (백그라운드 + 지금 보는 영상) ════════════════
    def queue_analysis(self, path):
        with self.aw_lock:
            if path in self.aw_queue or path == self.aw_current:
                return
            self.aw_queue.append(path)
            if self.aw_thread is None or not self.aw_thread.is_alive():
                self.aw_thread = threading.Thread(target=self._aw_loop, daemon=True)
                self.aw_thread.start()

    def _aw_loop(self):
        while True:
            with self.aw_lock:
                if not self.aw_queue:
                    self.aw_current = None
                    return
                path = self.aw_queue.pop(0)
                self.aw_current = path
                self.aw_job = fftools.Job()
                job = self.aw_job
            if path != self.fg_path:
                self._analyze_work(path, job, foreground=False)
            with self.aw_lock:
                self.aw_current = None

    def _analyze_work(self, path, job, foreground, use_cache=True, keep_saved=True):
        try:
            info = fftools.probe(path)
            if foreground:
                self.post("probed", path, info)
            a = analyzer.analyze(path, info, dict(self.s), progress=lambda r, msg: self.post("aprog", path, r, msg),
                                 job=job, use_cache=use_cache)
            segs, restored = None, False
            sf = segs_file(path, int(a.fps))
            if keep_saved and os.path.isfile(sf):
                try:
                    with open(sf, encoding="utf-8") as f:
                        segs = json.load(f)
                    restored = True
                except Exception:
                    segs = None
            if segs is None:
                segs = analyzer.detect(a, info["duration"], self.s)
                try:
                    with open(sf, "w", encoding="utf-8") as f:
                        json.dump(segs, f, ensure_ascii=False)
                except Exception:
                    pass
            self.post("analyzed", path, info, a, segs, restored)
        except Exception as e:
            self.post("afail", path, "취소됨" if job.cancelled else str(e))

    def switch_to(self, path, use_cache=True, keep_saved=True):
        """목록의 영상을 편집 화면에 띄움"""
        it = self._item(path)
        if not it:
            return
        self.pause()
        self._save_state()
        old = self.path
        self.path = path
        self.info = None
        self.analysis = None
        self.segs = []
        self.sel = None
        self.t = 0.0
        self.last_img = None
        self.refresh()
        self.sum_lbl.config(text="분석 중...")
        self.file_lbl.config(text=os.path.basename(path), foreground=TEXT)
        if self.qtree.selection() != (path,):
            self.qtree.selection_set(path)
        self.qtree.see(path)
        for x in self.items:
            if x["path"] in (old, path):
                self._qrow(x)
        self.s["current"] = path
        # 이미 백그라운드에서 분석 중이면 그 결과를 기다림, 아니면 바로 따로 분석
        with self.aw_lock:
            if path in self.aw_queue:
                self.aw_queue.remove(path)
            waiting = path == self.aw_current and use_cache
        if self.fg_job:
            self.fg_job.cancel()
        threading.Thread(target=self._probe_only, args=(path,), daemon=True).start()
        if waiting:
            self.status("뒤에서 분석 중인 영상입니다. 끝나면 바로 보여드립니다.")
            return
        self.fg_path = path
        self.fg_job = fftools.Job()
        self.cancel_btn.config(state="normal")
        job = self.fg_job

        def work():
            self._analyze_work(path, job, foreground=False, use_cache=use_cache, keep_saved=keep_saved)
            if self.fg_job is job:
                self.fg_job = None
                self.fg_path = None

        threading.Thread(target=work, daemon=True).start()

    def _probe_only(self, path):
        try:
            self.post("probed", path, fftools.probe(path))
        except Exception as e:
            self.post("afail", path, str(e))

    def _on_probed(self, path, info):
        """분석 전이라도 바로 재생·탐색 가능"""
        if path != self.path or self.info is not None:
            return
        self.info = info
        self.view = (0.0, info["duration"])
        codec = info["vcodec"].upper() + (" 10bit" if info.get("ten_bit") else "")
        self.file_lbl.config(text=f"{os.path.basename(self.path)}   ·   {codec} {info['width']}x{info['height']}"
                                  f"   ·   {info.get('fps', 0):.0f}fps   ·   {fftools.fmt_time(info['duration'])}",
                             foreground=TEXT)
        self.pv.itemconfig(self.pv_msg, state="hidden")
        self.draw_timeline()
        self.show_at(0.0)

    def _on_analyzed(self, path, info, a, segs, restored):
        it = self._item(path)
        if it:
            if it["state"] not in ("exporting", "done", "paused"):
                it["state"] = "ready"
            it["dur"] = info["duration"]
        if path != self.path:
            if it:
                self._qrow(it)
            return
        if not self.exporting and not self.fg_job:
            self.cancel_btn.config(state="disabled")
        if self.info is None:
            self._on_probed(path, info)
        self.info, self.analysis, self.segs = info, a, segs
        if it:
            it["reviewed"] = True
            self._qrow(it)
            self._save_queue()
        self.prog.config(value=1000)
        self.refresh()
        if self.segs:
            self.select(0)
        msg = "이전에 확인하던 내용을 불러왔습니다. (새로 찾으려면 '다시 찾기')" if restored else \
            f"분석 완료 — 잘라낼 구간 {len(self.segs)}개. 타임라인에서 확인하고 경계를 끌어 조절하세요."
        self.status(msg)

    def redetect(self):
        if not self.path or self.analysis is None:
            return
        if any(not s.get("auto", True) for s in self.segs) or not all(s.get("cut") for s in self.segs):
            if not messagebox.askyesno(APP_NAME, "직접 고친 내용이 사라지고 설정 기준으로 다시 찾습니다. 계속할까요?"):
                return
        if int(self.analysis.fps) != int(self.s.get("analysis_fps", 5)):
            self.switch_to(self.path, use_cache=True, keep_saved=False)
            return
        self.segs = analyzer.detect(self.analysis, self.info["duration"], self.s)
        self.sel = None
        self._save_state()
        self.refresh()
        if self.segs:
            self.select(0)
        self.status(f"다시 찾았습니다 — 잘라낼 구간 {len(self.segs)}개")

    # ════════════════ 프리뷰 ════════════════
    def _pv_size(self):
        """프리뷰 디코딩 크기 (화질 선택 반영, 짝수)"""
        f = dict(PREVIEW_Q).get(self.pq_var.get(), 0.5)
        w = max(160, int(self.info["width"] * f)) // 2 * 2
        h = max(90, int(self.info["height"] * f)) // 2 * 2
        return w, h

    def _layout_preview(self):
        w, h = self.pv.winfo_width(), self.pv.winfo_height()
        self.pv.coords(self.pv_img, w / 2, h / 2)
        self.pv.coords(self.pv_msg, w / 2, h / 2)
        if self.last_img is not None:
            self._render(self.last_img, self.t)

    def cut_at(self, t):
        for s in self.segs:
            if s.get("cut") and s["start"] <= t < s["end"]:
                return s
        return None

    def _render(self, img, t):
        self.last_img = img
        cw, ch = max(2, self.pv.winfo_width()), max(2, self.pv.winfo_height())
        iw, ih = img.size
        sc = min(cw / iw, ch / ih)
        dw, dh = max(2, int(iw * sc)), max(2, int(ih * sc))
        im = img.resize((dw, dh), Image.BILINEAR) if (dw, dh) != (iw, ih) else img
        seg = self.cut_at(t) if self.dark_var.get() else None
        if seg is not None:
            im = ImageEnhance.Brightness(im).enhance(0.32)
        if self.photo is not None and (self.photo.width(), self.photo.height()) == (dw, dh):
            self.photo.paste(im)
        else:
            self.photo = ImageTk.PhotoImage(im)
            self.pv.itemconfig(self.pv_img, image=self.photo)
        x0, y0 = (cw - dw) / 2, (ch - dh) / 2
        st = "normal" if seg is not None else "hidden"
        if seg is not None:
            txt = f"  ✂ 잘릴 구간 · {analyzer.KINDS.get(seg['kind'], seg['kind'])}  "
            self.pv.itemconfig(self.pv_badge, text=txt)
            self.pv.coords(self.pv_badge, x0 + 14, y0 + 14)
            bb = self.pv.bbox(self.pv_badge)
            if bb:
                self.pv.coords(self.pv_badge_bg, bb[0] - 2, bb[1] - 4, bb[2] + 2, bb[3] + 4)
            self.pv.coords(self.pv_frame, x0 + 1, y0 + 1, x0 + dw - 2, y0 + dh - 2)
        for it in (self.pv_badge_bg, self.pv_badge, self.pv_frame):
            self.pv.itemconfig(it, state=st)
        self.pv.tag_raise(self.pv_badge_bg)
        self.pv.tag_raise(self.pv_badge)

    def _display_loop(self):
        try:
            d = self.slot.get()
            if d and d[0] != self.shown_seq:
                seq, img, t, kind = d
                self.shown_seq = seq
                if kind == "play" and self.player.playing:
                    self.t = t
                    self._render(img, t)
                    self.frames_shown += 1
                    self._after_play_frame(t)
                elif kind != "play" and not self.player.playing:
                    self._render(img, t)
                    self.frames_shown += 1
            if self.player.playing is False and self.play_btn.cget("text") != "▶ 재생":
                self.play_btn.config(text="▶ 재생")
        except Exception as e:
            self.status(f"프리뷰 오류: {e}")
        self.root.after(15, self._display_loop)

    def _after_play_frame(self, t):
        self._update_time()
        self._move_playhead()
        v0, v1 = self.view
        span = v1 - v0
        if t > v1 - span * 0.03 or t < v0:
            self._set_view(t - span * 0.1, t - span * 0.1 + span)
        if self.skip_var.get():
            seg = self.cut_at(t + 0.05)
            if seg is not None:
                if seg["end"] >= self._dur() - 0.1:
                    self.pause()
                else:
                    self.play(seg["end"] + 0.01)

    def show_at(self, t, fast=False):
        """멈춘 상태에서 t 지점 보여주기 (끄는 중엔 썸네일 먼저)"""
        if not self.info:
            return
        self.t = min(max(0.0, t), self._dur())
        self._update_time()
        self._move_playhead()
        if fast and self.analysis is not None:
            th = self.analysis.thumb(self.t)
            if th is not None:
                self._render(Image.fromarray(th), self.t)
        self.scrub.request(self.path, self.t, self._pv_size())

    def _update_time(self):
        self.time_lbl.config(text=f"{fftools.fmt_time(self.t, True)} / {fftools.fmt_time(self._dur())}")

    def play(self, t=None):
        if not self.info:
            return
        t = self.t if t is None else t
        if t >= self._dur() - 0.2:
            t = 0.0
        self.scrub.cancel()
        self.t = t
        sp = dict(SPEEDS).get(self.speed_var.get(), 1.0)
        self.player.start(self.path, self.info, t, self._pv_size(), speed=sp, sound=self.sound_var.get(),
                          use_gpu=self.s.get("use_gpu", True))
        self.play_btn.config(text="❚❚ 정지")

    def pause(self):
        if self.player.playing:
            self.player.stop()
        self.play_btn.config(text="▶ 재생")

    def toggle_play(self):
        if self.player.playing:
            self.pause()
        else:
            self.play()

    def step(self, d):
        was = self.player.playing
        self.pause()
        t = self.t + d
        if was:
            self.play(max(0.0, t))
        else:
            self.show_at(t)

    def jump_seg(self, d):
        if not self.segs:
            return
        if self.sel is None:
            i = 0 if d > 0 else len(self.segs) - 1
        else:
            i = min(max(0, self.sel + d), len(self.segs) - 1)
        self.select(i)

    # ════════════════ 키보드 ════════════════
    def _on_key(self, e):
        w = self.root.focus_get()
        if isinstance(w, (tk.Entry, ttk.Entry, ttk.Spinbox, ttk.Combobox)) and e.keysym not in ("F5",):
            return
        if w is not None and w.winfo_toplevel() is not self.root:
            return
        ks = e.keysym
        ch = HANGUL_KEYS.get(e.char, e.char.lower() if e.char else "")
        shift = bool(e.state & 0x1)
        if ks == "space":
            self.toggle_play()
        elif ks == "Left":
            self.step(-5.0 if shift else -1.0)
        elif ks == "Right":
            self.step(5.0 if shift else 1.0)
        elif ch == ",":
            self.step(-1.0 / max(1.0, self.info["fps"] if self.info else 30))
        elif ch == ".":
            self.step(1.0 / max(1.0, self.info["fps"] if self.info else 30))
        elif ks in ("Up", "Prior") and w is not self.tree:
            self.jump_seg(-1)
        elif ks in ("Down", "Next") and w is not self.tree:
            self.jump_seg(1)
        elif ks == "Home":
            self.step(-self.t)
        elif ks == "End":
            self.step(self._dur() - self.t - 0.1)
        elif ks == "Delete":
            self.delete_seg()
        elif ch == "x":
            self.toggle()
        elif ch == "i":
            self.set_edge_here("start")
        elif ch == "o":
            self.set_edge_here("end")
        elif ch == "a":
            self.add_here()
        else:
            return
        return "break"

    # ════════════════ 목록·선택 ════════════════
    def refresh(self):
        self.segs.sort(key=lambda s: (s["start"], s["end"]))
        self.tree.delete(*self.tree.get_children())
        for i, s in enumerate(self.segs):
            tags = () if s.get("cut") else ("keep",)
            self.tree.insert("", "end", iid=str(i), tags=tags, values=(
                i + 1, analyzer.KINDS.get(s["kind"], s["kind"]), fftools.fmt_time(s["start"], True),
                f"{s['end'] - s['start']:.1f}초", "자르기" if s.get("cut") else "살리기"))
        if self.sel is not None and self.sel < len(self.segs):
            self.tree.selection_set(str(self.sel))
            self.tree.see(str(self.sel))
        self._summary()
        self.draw_timeline()

    def _summary(self):
        if not self.info or self.analysis is None:
            return
        cuts = [s for s in self.segs if s.get("cut")]
        keep = sum(b - a for a, b in analyzer.keep_ranges(self.segs, self.info["duration"]))
        self.sum_lbl.config(text=f"자를 구간 {len(cuts)}개 · 총 {fftools.fmt_time(self.info['duration'] - keep)} 삭제\n"
                                 f"결과 길이 {fftools.fmt_time(keep)}  (원본 {fftools.fmt_time(self.info['duration'])})")

    def _tree_sel(self, _e=None):
        sel = self.tree.selection()
        if sel:
            i = int(sel[0])
            if i != self.sel:
                self.select(i, from_tree=True)

    def select(self, i, from_tree=False, move=True):
        if i is None or not (0 <= i < len(self.segs)):
            return
        self.sel = i
        s = self.segs[i]
        if not from_tree:
            self.tree.selection_set(str(i))
            self.tree.see(str(i))
        v0, v1 = self.view
        span = v1 - v0
        need = (s["end"] - s["start"]) * 1.6 + 4
        if need > span or s["start"] < v0 or s["end"] > v1:
            span = max(span, need) if need > span else span
            c = (s["start"] + s["end"]) / 2
            self._set_view(c - span / 2, c + span / 2)
        else:
            self.draw_timeline()
        if move:
            self.pause()
            self.show_at(s["start"], fast=True)

    # ════════════════ 편집 ════════════════
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
        if self.last_img is not None and not self.player.playing:
            self._render(self.last_img, self.t)

    def delete_seg(self):
        s = self._cur()
        if not s:
            return
        del self.segs[self.sel]
        self.sel = min(self.sel, len(self.segs) - 1) if self.segs else None
        self._changed()

    def set_edge_here(self, which):
        s = self._cur()
        if not s:
            return
        t = round(self.t, 2)
        if which == "start" and t < s["end"] - 0.2:
            s["start"] = t
        elif which == "end" and t > s["start"] + 0.2:
            s["end"] = t
        else:
            self.status("구간의 시작은 끝보다 앞이어야 합니다.")
            return
        s["auto"] = False
        self._changed()

    def add_here(self):
        if not self.info:
            return
        st = round(self.t, 2)
        nxt = min([s["start"] for s in self.segs if s["start"] > st + 0.2] + [self._dur()])
        if self.cut_at(st) is not None:
            self.status("이미 잘릴 구간 안입니다.")
            return
        seg = {"start": st, "end": round(min(nxt, st + 5.0), 2), "kind": "manual", "cut": True, "auto": False}
        if seg["end"] - seg["start"] < 0.2:
            return
        self.segs.append(seg)
        self._changed(seg)
        self.status("구간을 추가했습니다. 타임라인에서 끝 경계를 끌어 길이를 맞추세요.")

    def all_cut(self, v):
        for s in self.segs:
            s["cut"] = v
        self._changed()

    def _changed(self, keep=None):
        cur = keep or self._cur()
        self.segs.sort(key=lambda s: (s["start"], s["end"]))
        if cur in self.segs:
            self.sel = self.segs.index(cur)
        self._save_state()
        self.refresh()
        s = self._cur()
        if s:
            self.toggle_btn.config(text="살리기 (X)" if s.get("cut") else "자르기 (X)")

    def _save_state(self):
        if self.path and self.analysis:
            try:
                with open(segs_file(self.path, int(self.analysis.fps)), "w", encoding="utf-8") as f:
                    json.dump(self.segs, f, ensure_ascii=False)
            except Exception:
                pass
        self._save_settings_only()

    def _save_settings_only(self):
        try:
            self.s["out_dir"] = self.dir_var.get().strip()
        except Exception:
            pass
        save_settings(self.s)

    # ════════════════ 타임라인 ════════════════
    def _w(self):
        return max(10, self.tl.winfo_width())

    def _x(self, t):
        v0, v1 = self.view
        return (t - v0) / max(1e-6, v1 - v0) * self._w()

    def _t(self, x):
        v0, v1 = self.view
        return v0 + (v1 - v0) * x / self._w()

    def _set_view(self, a, b):
        d = self._dur()
        span = min(d, max(3.0, b - a))
        a = min(max(0.0, a), max(0.0, d - span))
        self.view = (a, a + span)
        self.draw_timeline()

    def _scroll_view(self, *args):
        d = self._dur()
        v0, v1 = self.view
        span = v1 - v0
        if args[0] == "moveto":
            self._set_view(float(args[1]) * d, float(args[1]) * d + span)
        elif args[0] == "scroll":
            self._pan(int(args[1]) * (5 if args[2] == "pages" else 1))

    def _pan(self, n):
        v0, v1 = self.view
        step = (v1 - v0) * 0.12 * n
        self._set_view(v0 + step, v1 + step)

    def _tl_wheel(self, e):
        if e.state & 0x1:
            return
        self._zoom_at(e.x, 0.8 if e.delta > 0 else 1.25)

    def _zoom_center(self, f):
        x = self._x(self.t)
        if not (0 <= x <= self._w()):
            x = self._w() / 2
        self._zoom_at(x, f)

    def _zoom_at(self, x, f):
        if not self.info:
            return
        t = self._t(x)
        v0, v1 = self.view
        span = (v1 - v0) * f
        r = x / self._w()
        self._set_view(t - span * r, t - span * r + span)

    def _lane_image(self, w):
        """썸네일 띠 + 잘릴 구간 색을 입힌 이미지"""
        h = LANE_H
        arr = np.empty((h, w, 3), dtype=np.float32)
        arr[:] = (38, 42, 50)
        d = self._dur()
        a = self.analysis
        x_end = int(min(w, self._x(d)))
        if a is not None and len(a.thumbs):
            tw = int(round(analyzer.TW * h / analyzer.TH))
            for x in range(0, x_end, tw):
                th = a.thumb(self._t(x + tw / 2))
                if th is None:
                    continue
                tile = np.asarray(Image.fromarray(th).resize((tw, h), Image.BILINEAR), dtype=np.float32)
                xe = min(x + tw, x_end)
                arr[:, x:xe] = tile[:, :xe - x]
                arr[:, x:x + 1] *= 0.55
        v0, v1 = self.view
        for s in self.segs:
            if not s.get("cut") or s["end"] < v0 or s["start"] > v1:
                continue
            xa, xb = max(0, int(self._x(s["start"]))), min(w, int(self._x(s["end"])) + 1)
            if xb <= xa:
                xb = min(w, xa + 2)
            col = hex_rgb(analyzer.KIND_COLORS.get(s["kind"], "#999999"))
            arr[:, xa:xb] = arr[:, xa:xb] * 0.3 + col * 0.62
        return Image.fromarray(arr.clip(0, 255).astype(np.uint8))

    def draw_timeline(self):
        c = self.tl
        c.delete("all")
        w = self._w()
        c.create_rectangle(0, 0, w, RULER_H, fill=RULER_BG, outline="")
        if not self.info:
            c.create_text(w / 2, TL_H / 2 + 8, text="영상을 열면 여기에 잘라낼 구간이 표시됩니다.", fill=SUB, font=FONT)
            self.hbar.set(0, 1)
            return
        d = self._dur()
        v0, v1 = self.view
        self.hbar.set(v0 / d, v1 / d)
        span = v1 - v0
        step = next((st for st in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200)
                     if w / (span / st) >= 100), 7200)
        t = (int(v0 // step)) * step
        while t <= v1:
            x = self._x(t)
            if x >= 0:
                c.create_line(x, RULER_H - 9, x, RULER_H, fill="#5b616c")
                c.create_text(x + 4, 9, text=fftools.fmt_time(t, step < 1), anchor="w", fill=SUB, font=FONT_S)
                for k in range(1, 5):
                    xs = self._x(t + step * k / 5)
                    c.create_line(xs, RULER_H - 4, xs, RULER_H, fill="#3f444d")
            t += step
        # 썸네일 띠
        self.lane_photo = ImageTk.PhotoImage(self._lane_image(w))
        c.create_image(0, LANE_Y, image=self.lane_photo, anchor="nw")
        c.create_rectangle(0, LANE_Y, w, LANE_Y + LANE_H, outline="#30343c")
        # 결과 줄 (남는 부분 = 초록)
        c.create_rectangle(self._x(0), RES_Y, self._x(d), RES_Y + RES_H, fill="#2b8a5d", outline="")
        c.create_text(4, RES_Y + RES_H + 6, text="", fill=SUB)
        for i, s in enumerate(self.segs):
            if s["end"] < v0 or s["start"] > v1:
                continue
            xa, xb = self._x(s["start"]), self._x(s["end"])
            if xb - xa < 2:
                xb = xa + 2
            col = analyzer.KIND_COLORS.get(s["kind"], "#999")
            if s.get("cut"):
                c.create_rectangle(xa, RES_Y, xb, RES_Y + RES_H, fill="#3a1d22", outline="")
                c.create_rectangle(xa, LANE_Y, xb, LANE_Y + LANE_H, outline=col, width=1)
            else:
                c.create_rectangle(xa + 1, LANE_Y + 2, xb - 1, LANE_Y + LANE_H - 2, outline=col, dash=(4, 3), width=2)
            if xb - xa > 70:
                label = analyzer.KINDS.get(s["kind"], s["kind"]) + ("" if s.get("cut") else " (살림)")
                c.create_text(xa + 6, LANE_Y + 5, text=label, anchor="nw", fill="white", font=FONT_S)
                if xb - xa > 130:
                    c.create_text(xa + 6, LANE_Y + LANE_H - 5, text=f"{s['end'] - s['start']:.1f}초", anchor="sw",
                                  fill="#dddddd", font=FONT_S)
            if i == self.sel:
                c.create_rectangle(xa - 1, LANE_Y - 2, xb + 1, LANE_Y + LANE_H + 2, outline="white", width=2)
                for xe in (xa, xb):
                    c.create_rectangle(xe - 4, LANE_Y + LANE_H / 2 - 16, xe + 4, LANE_Y + LANE_H / 2 + 16,
                                       fill="white", outline="#222")
                    c.create_line(xe, LANE_Y + LANE_H / 2 - 8, xe, LANE_Y + LANE_H / 2 + 8, fill="#555")
        x_end = self._x(d)
        if x_end < w:
            c.create_rectangle(x_end, LANE_Y, w, RES_Y + RES_H, fill=TL_BG, outline="")
        self._move_playhead(create=True)

    def _move_playhead(self, create=False):
        c = self.tl
        if not self.info:
            return
        x = self._x(self.t)
        if create or not c.find_withtag("ph"):
            c.delete("ph")
            c.create_line(x, 6, x, TL_H, fill=PLAYHEAD, width=2, tags=("ph", "phl"))
            c.create_polygon(x - 7, 2, x + 7, 2, x + 7, 12, x, 19, x - 7, 12, fill=PLAYHEAD, outline="",
                             tags=("ph", "phh"))
        else:
            c.coords("phl", x, 6, x, TL_H)
            c.coords("phh", x - 7, 2, x + 7, 2, x + 7, 12, x, 19, x - 7, 12)
            c.tag_raise("ph")

    def _edge_hit(self, x, y):
        if not (LANE_Y - 4 <= y <= LANE_Y + LANE_H + 4):
            return None
        order = list(range(len(self.segs)))
        if self.sel is not None:
            order.sort(key=lambda i: i != self.sel)
        best = None
        for i in order:
            s = self.segs[i]
            for which in ("start", "end"):
                dx = abs(self._x(s[which]) - x)
                if dx <= EDGE_PX and (best is None or dx < best[2]):
                    best = (i, which, dx)
            if best and best[0] == self.sel:
                break
        return best

    def _tl_hover(self, e):
        if not self.info or self.drag:
            return
        hit = self._edge_hit(e.x, e.y)
        self.tl.config(cursor="sb_h_double_arrow" if hit else ("hand2" if e.y < RULER_H else ""))

    def _tl_press(self, e):
        if not self.info:
            return
        self.tl.focus_set()
        self.pause()
        hit = self._edge_hit(e.x, e.y)
        if hit:
            i, which, _ = hit
            self.select(i, move=False)
            seg = self.segs[i]
            self.drag = ("edge", seg, which)
            self.show_at(seg[which] if which == "start" else max(0.0, seg[which] - 0.05), fast=True)
            return
        t = min(max(0.0, self._t(e.x)), self._dur())
        if LANE_Y <= e.y <= LANE_Y + LANE_H:
            for i, s in enumerate(self.segs):
                if s["start"] <= t <= s["end"]:
                    if i != self.sel:
                        self.select(i, move=False)
                    break
        self.drag = ("scrub",)
        self.show_at(t, fast=True)

    def _tl_motion(self, e):
        if not self.drag:
            return
        d = self._dur()
        w = self._w()
        if e.x > w - 8:            # 가장자리에서 끌면 화면 이동
            self._pan(0.25)
        elif e.x < 8:
            self._pan(-0.25)
        t = min(max(0.0, self._t(e.x)), d)
        if self.drag[0] == "scrub":
            self.show_at(t, fast=True)
            return
        _, seg, which = self.drag
        idx = self.segs.index(seg)
        lo = self.segs[idx - 1]["end"] if idx > 0 else 0.0
        hi = self.segs[idx + 1]["start"] if idx + 1 < len(self.segs) else d
        if which == "start":
            seg["start"] = round(min(max(t, lo), seg["end"] - 0.2), 2)
            show = seg["start"]
        else:
            seg["end"] = round(max(min(t, hi), seg["start"] + 0.2), 2)
            show = max(0.0, seg["end"] - 0.05)
        seg["auto"] = False
        self.draw_timeline()
        self.show_at(show, fast=True)
        self.status(f"{'시작' if which == 'start' else '끝'} 경계 {fftools.fmt_time(seg[which], True)}"
                    f"  ·  구간 길이 {seg['end'] - seg['start']:.1f}초")

    def _tl_release(self, e):
        if self.drag and self.drag[0] == "edge":
            seg = self.drag[1]
            self._changed(seg)
        self.drag = None

    # ════════════════ 저장 ════════════════
    def pick_dir(self):
        cur = self.dir_var.get().strip()
        d = filedialog.askdirectory(title="출력 폴더 선택", initialdir=cur or self.s.get("last_dir") or None,
                                    mustexist=False)
        if d:
            self.dir_var.set(os.path.normpath(d))
            self._save_settings_only()

    def _out_path(self, src, taken, out_dir=None):
        d = (self.dir_var.get().strip() if out_dir is None else out_dir) or os.path.dirname(src)
        stem = os.path.splitext(os.path.basename(src))[0]
        out = os.path.join(d, stem + "_편집.mp4")
        base, ext = os.path.splitext(out)
        i = 2
        while os.path.exists(out) or out in taken:
            out = f"{base} ({i}){ext}"
            i += 1
        return out

    def _mode(self):
        v = self.mode_var.get()
        return next((k for k, t in EXPORT_MODES if t == v), "auto")

    def export_current(self):
        if not self.path:
            messagebox.showinfo(APP_NAME, "먼저 영상을 추가해주세요.")
            return
        if self.analysis is None:
            messagebox.showinfo(APP_NAME, "분석이 끝난 뒤 내보낼 수 있습니다.")
            return
        self.start_export([self._item(self.path)])

    def export_all(self):
        todo = [it for it in self.items if it["include"] and it["state"] != "done"]
        if not todo:
            if any(it["include"] for it in self.items):
                if messagebox.askyesno(APP_NAME, "체크한 영상이 모두 이미 완료 상태입니다. 다시 내보낼까요?"):
                    for it in self.items:
                        if it["include"] and it["state"] == "done":
                            it["state"] = "ready"
                            it["out"] = ""
                            self._qrow(it)
                    todo = [it for it in self.items if it["include"]]
            else:
                messagebox.showinfo(APP_NAME, "영상 목록에서 내보낼 영상을 체크(출력 칸)해주세요.")
            if not todo:
                return
        self.start_export(todo)

    def start_export(self, todo):
        if self.exporting or not todo:
            return
        d = self.dir_var.get().strip()
        if d and not os.path.isdir(d):
            try:
                os.makedirs(d, exist_ok=True)
            except OSError:
                messagebox.showerror(APP_NAME, "출력 폴더를 만들 수 없습니다. 다른 폴더를 골라주세요.")
                return
        mode = self._mode()
        unrev = [it for it in todo if not it.get("reviewed")]
        names = "\n".join(f"  {i + 1}. {os.path.basename(it['path'])}" for i, it in enumerate(todo[:12]))
        if len(todo) > 12:
            names += f"\n  ... 외 {len(todo) - 12}개"
        how = {"auto": "자동 (H.265 원본은 무손실, 그 외는 H.265 인코딩)", "copy": "무손실",
               "encode": "H.265 인코딩"}[mode]
        resumed = sum(1 for it in todo if it["state"] == "paused")
        msg = (f"영상 {len(todo)}개를 순서대로 잘라서 저장합니다.\n\n{names}\n\n"
               f"저장 방식: {how}\n출력 폴더: {d or '각 원본과 같은 폴더'}")
        if resumed:
            msg += f"\n\n멈췄던 {resumed}개는 멈춘 곳부터 이어서 합니다."
        if len(todo) > 1:
            msg += "\n진행 중에 목록에 체크해서 넣은 영상도 이어서 처리합니다."
        if unrev:
            msg += f"\n\n※ 아직 열어보지 않은 영상 {len(unrev)}개는 자동으로 찾은 구간 그대로 잘립니다."
        if not messagebox.askyesno(APP_NAME, msg + "\n\n진행할까요?"):
            return
        self.pause()
        self.s["export_mode"] = mode
        try:
            self.s["quality"] = int(self.q_var.get())
        except Exception:
            pass
        self._save_state()
        # 백그라운드 분석은 멈춤 (내보내기에서 필요한 것만 분석)
        with self.aw_lock:
            self.aw_queue.clear()
            if self.aw_job:
                self.aw_job.cancel()
        for it in self.items:
            if it["state"] in ("wait", "analyzing") and it not in todo:
                it["state"] = "wait"
                self._qrow(it)
        self._set_exporting(True)
        self.prog.config(value=0)
        self.job = fftools.Job()
        job = self.job
        settings = dict(self.s)
        cur_path, cur_segs = self.path, [dict(x) for x in self.segs] if self.analysis is not None else None
        settings["out_dir_now"] = d
        for it in todo:
            if it["state"] == "error":
                it["state"] = "ready"
            if it["state"] != "paused":
                it["pct"] = 0.0
        self.batch_first = [it["path"] for it in todo]
        self.batch_tried = set()
        self._update_export_btn()
        threading.Thread(target=self._batch_work, args=(mode, settings, job), daemon=True).start()

    def _next_batch_item(self):
        """다음에 처리할 영상: 처음 고른 것들 먼저, 그 뒤 새로 체크된 것"""
        items = list(self.items)
        order = [self._item(p) for p in self.batch_first] + items
        for it in order:
            if it and it["include"] and it["state"] not in ("done", "error") and it["path"] not in self.batch_tried:
                self.batch_tried.add(it["path"])
                return dict(it)
        return None

    def _batch_work(self, mode, settings, job):
        results = []
        k = 0
        while not job.cancelled:
            it = self._next_batch_item()
            if it is None:
                break
            k += 1
            path = it["path"]
            name = os.path.basename(path)
            out = it.get("out") or ""
            if not out or os.path.exists(out):
                out = self._out_path(path, set(), settings.get("out_dir_now", ""))
            left = sum(1 for x in self.items if x["include"] and x["state"] not in ("done", "error")
                       and x["path"] not in self.batch_tried)
            head = f"[{k}번째 · 남은 {left}개] {name}"

            def prog(r, m, path=path, head=head, part=(0.0, 1.0)):
                a0, a1 = part
                rr = a0 + (a1 - a0) * r
                self.post("item", path, {"state": "exporting", "pct": rr})
                self.post("bprog", rr, f"{head} — {m}{'  (일시정지)' if job.paused else ''}")

            try:
                self.post("item", path, {"state": "exporting", "out": out})
                info = fftools.probe(path)
                a = analyzer.analyze(path, info, settings, progress=lambda r, m: prog(r, m, part=(0.0, 0.3)), job=job)
                sf = segs_file(path, int(a.fps))
                if os.path.isfile(sf):
                    with open(sf, encoding="utf-8") as f:
                        segs = json.load(f)
                else:
                    segs = analyzer.detect(a, info["duration"], settings)
                    with open(sf, "w", encoding="utf-8") as f:
                        json.dump(segs, f, ensure_ascii=False)
                part = (0.0, 1.0)
                msg = exporter.export(path, info, segs, out, mode, settings,
                                      progress=lambda r, m: prog(r, m, part=part), job=job)
                self.post("item", path, {"state": "done", "pct": 1.0, "out": out})
                results.append((path, out, True, msg.splitlines()[0]))
            except Exception as e:
                if job.cancelled:
                    pct = exporter.encode_progress(path, out)
                    self.post("item", path, {"state": "paused", "pct": pct, "out": out})
                    break
                self.post("item", path, {"state": "error", "msg": str(e)})
                results.append((path, out, False, str(e).splitlines()[0] if str(e) else "오류"))
        self.post("batch_done", results, job.cancelled)

    def _batch_done(self, results, cancelled):
        self._set_exporting(False)
        self._save_queue()
        ok = [r for r in results if r[2]]
        bad = [r for r in results if not r[2]]
        self.prog.config(value=1000 if ok and not cancelled else 0)
        head = "중지했습니다. (다음에 '이어서 내보내기'로 멈춘 곳부터 계속)" if cancelled else "모두 끝났습니다."
        lines = [f"{head}  완료 {len(ok)}개" + (f" · 실패 {len(bad)}개" if bad else "")]
        self.status(lines[0])
        for p, out, good, m in results[:15]:
            lines.append(f"{'✔' if good else '✖'} {os.path.basename(p)} — {m}")
        # 백그라운드 분석 다시 이어감
        for it in self.items:
            if it["state"] == "wait":
                self.queue_analysis(it["path"])
        if not results:
            return
        if ok:
            if messagebox.askyesno(APP_NAME, "\n".join(lines) + "\n\n저장된 폴더를 열까요?"):
                self.reveal(ok[-1][1])
        else:
            messagebox.showerror(APP_NAME, "\n".join(lines))

    def cancel(self):
        if self.exporting and self.job:
            self.job.cancel()
            self.status("취소하는 중...")
        elif self.fg_job:
            self.fg_job.cancel()
            self.status("분석을 취소했습니다.")

    def reveal(self, path):
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        except Exception:
            pass

    def on_close(self):
        if self.exporting and not messagebox.askyesno(
                APP_NAME, "작업 중입니다. 멈추고 닫을까요?\n\n다음에 켜서 '이어서 내보내기'를 누르면 멈춘 곳부터 계속합니다."):
            return
        if self.exporting:
            for it in self.items:
                if it["state"] == "exporting":
                    it["state"] = "paused"
                    it["pct"] = exporter.encode_progress(it["path"], it.get("out", ""))
        for j in (self.job, self.fg_job, self.aw_job):
            if j:
                j.cancel()
        with self.aw_lock:
            self.aw_queue.clear()
        self.player.stop()
        self.s["geometry"] = self.root.geometry()
        self._save_state()
        self._save_queue()
        self.root.destroy()

def dialog_base(win, app, title):
    win.title(title)
    win.transient(app.root)
    win.resizable(False, False)
    win.configure(bg=PANEL)
    f = ttk.Frame(win, style="Panel.TFrame", padding=18)
    f.pack(fill="both", expand=True)
    return f


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
        f = dialog_base(self, app, "설정")
        ttk.Label(f, text="자동으로 찾는 기준", style="Head.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        self.vars = {}
        r = 1
        for key, label, lo, hi, inc in self.FIELDS:
            ttk.Label(f, text=label, style="Panel.TLabel").grid(row=r, column=0, sticky="w", pady=2)
            v = tk.StringVar(value=str(app.s.get(key, analyzer.DEFAULTS[key])))
            ttk.Spinbox(f, from_=lo, to=hi, increment=inc, textvariable=v, width=8).grid(row=r, column=1, padx=8)
            self.vars[key] = (v, lo, hi)
            r += 1
        self.blur_on = tk.BooleanVar(value=bool(app.s.get("blur_on", True)))
        ttk.Checkbutton(f, text="흐림·가림 화면도 찾기", variable=self.blur_on).grid(row=r, column=0, sticky="w",
                                                                              pady=(6, 0))
        r += 1
        ttk.Label(f, text="그래픽카드", style="Head.TLabel").grid(row=r, column=0, columnspan=2, sticky="w",
                                                             pady=(14, 0))
        r += 1
        self.gpu = tk.BooleanVar(value=bool(app.s.get("use_gpu", True)))
        ttk.Checkbutton(f, text="분석·프리뷰·디코딩에 그래픽카드 사용(안 되면 자동으로 CPU)",
                        variable=self.gpu).grid(row=r, column=0, columnspan=2, sticky="w")
        r += 1
        ttk.Label(f, text="H.265 인코더", style="Panel.TLabel").grid(row=r, column=0, sticky="w", pady=2)
        self.enc_names = [("auto", "자동 선택")] + [(e, exporter.ENC_NAMES[e]) for e in exporter.ENCODERS]
        self.enc = tk.StringVar(value=dict(self.enc_names).get(app.s.get("encoder", "auto"), "자동 선택"))
        ttk.Combobox(f, textvariable=self.enc, values=[n for _, n in self.enc_names], state="readonly",
                     width=18).grid(row=r, column=1, padx=8)
        r += 1
        bf = ttk.Frame(f, style="Panel.TFrame")
        bf.grid(row=r, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(bf, text="기본값", command=self.reset).pack(side="left")
        ttk.Button(bf, text="저장", command=self.ok, style="Accent.TButton").pack(side="left", padx=6)
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
        f = dialog_base(self, app, "업데이트 확인")
        ttk.Label(f, text=f"지금 버전: {VERSION}", style="Head.TLabel").pack(anchor="w")
        self.msg = ttk.Label(f, text="새 버전을 확인하는 중...", wraplength=380, justify="left", style="Panel.TLabel")
        self.msg.pack(anchor="w", pady=(10, 6))
        self.prog = ttk.Progressbar(f, mode="indeterminate", length=380)
        self.prog.pack(fill="x")
        self.prog.start(12)
        self.notes = tk.Text(f, height=7, width=52, wrap="word", font=FONT_S, relief="flat", bd=0,
                             bg=PANEL2, fg=TEXT)
        self.btns = ttk.Frame(f, style="Panel.TFrame")
        self.btns.pack(fill="x", pady=(12, 0))
        ttk.Button(self.btns, text="닫기", command=self.destroy).pack(side="right")
        self.q = []
        threading.Thread(target=self._check, daemon=True).start()
        self.after(100, self._poll)

    def _check(self):
        try:
            info = updater.latest_release()
        except Exception as e:
            if getattr(e, "code", None) == 404:
                self.q.append(("err", "업데이트 정보를 찾을 수 없습니다. (저장소가 비공개이거나 아직 배포된 버전이 없음)"))
            else:
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
                self.app.exporting = False
                self.app.on_close()
                return
        if self.winfo_exists():
            self.after(100, self._poll)


def _ui_test(app, root):
    """화면 쪽 점검: 테스트 영상을 열어 목록·타임라인·끌기·재생·탐색 확인"""
    import glob
    import tempfile
    import time
    src = sorted(glob.glob(os.path.join(tempfile.gettempdir(), "videocut_test_*", "테스트 영상.mp4")),
                 key=os.path.getmtime)[-1]
    app.sound_var.set(False)

    def pump(sec):
        end = time.time() + sec
        while time.time() < end:
            root.update()
            time.sleep(0.01)

    app.add_files([src])
    end = time.time() + 120
    while app.analysis is None and time.time() < end:
        pump(0.1)
    a = app.analysis
    print("thumbs:", a.thumbs.shape, "queue:", len(app.items), flush=True)
    pump(0.3)

    # 경계 끌기: 두 번째 구간 끝을 오른쪽으로 0.5초
    app.select(1)
    pump(0.3)
    seg = app.segs[1]
    old_end = seg["end"]
    x = app._x(seg["end"])
    y = LANE_Y + LANE_H // 2
    ev = type("E", (), {})()
    ev.x, ev.y, ev.state = x, y, 0
    app._tl_press(ev)
    ev.x = app._x(old_end + 0.5)
    app._tl_motion(ev)
    app._tl_release(ev)
    print(f"drag end: {old_end} -> {app.segs[1]['end']}", flush=True)
    drag_ok = abs(app.segs[1]["end"] - (old_end + 0.5)) < 0.15
    # 탐색 한 장
    n0 = app.frames_shown
    app.show_at(5.0)
    pump(2.0)
    scrub_ok = app.frames_shown > n0
    print("scrub frames:", app.frames_shown - n0, flush=True)
    # 재생 (잘릴 부분 건너뛰기 켜고 13초부터 → 14.1~ 렉 구간 건너뜀)
    app.skip_var.set(True)
    n1 = app.frames_shown
    app.play(12.5)
    pump(3.0)
    played = app.frames_shown - n1
    t_now = app.t
    app.pause()
    print(f"play frames: {played}  t: {t_now:.2f}", flush=True)
    play_ok = played > 20 and t_now > 16.4
    for q in ("1/4", "1:1"):
        app.pq_var.set(q)
        app._opt_changed(restart=True)
        pump(1.0)
    print("ui segs:", len(app.segs), "tree:", len(app.tree.get_children()), flush=True)
    tree_ok = len(app.tree.get_children()) == 4
    # 일괄 내보내기: 두 번째 영상(H.264)을 목록에 넣고 둘 다 순서대로 저장
    src2 = os.path.join(os.path.dirname(src), "h264.mp4")
    app.add_files([src2], select=False)
    outdir = tempfile.mkdtemp(prefix="videocut_batch_")
    app.dir_var.set(outdir)
    messagebox.askyesno = lambda *a, **k: True
    app.reveal = lambda p: None
    app.export_all()
    end = time.time() + 300
    pump(0.5)
    while app.exporting and time.time() < end:
        pump(0.2)
    outs = sorted(os.listdir(outdir))
    codecs = [fftools.probe(os.path.join(outdir, f))["vcodec"] for f in outs]
    states = [it["state"] for it in app.items]
    print("batch out:", outs, codecs, states, flush=True)
    batch_ok = len(outs) == 2 and all(c == "hevc" for c in codecs) and states == ["done", "done"]
    # 일시정지 → 중지 → 이어서 내보내기 (조각 단위로 이어지는지)
    exporter.CHUNK_SEC = 3
    for it in app.items:
        it["state"] = "ready"
        it["out"] = ""
        it["pct"] = 0.0
    app.items[1]["include"] = False
    app.mode_var.set(dict(EXPORT_MODES)["encode"])
    outdir2 = tempfile.mkdtemp(prefix="videocut_resume_")
    app.dir_var.set(outdir2)
    app.export_all()
    end = time.time() + 120
    while app.items[0].get("pct", 0) < 0.4 and app.exporting and time.time() < end:
        pump(0.05)
    app.toggle_job_pause()
    pump(1.0)
    paused_ok = app.exporting and app.job.paused and app.pause_btn.cget("text") == "계속"
    app.cancel()
    end = time.time() + 30
    while app.exporting and time.time() < end:
        pump(0.1)
    it0 = app.items[0]
    saved = app.s["queue"][0]
    print("stopped:", it0["state"], round(it0.get("pct", 0), 2), saved.get("state"), it0.get("out"), flush=True)
    wroot = os.path.join(outdir2, ".videocut_work")
    for dp, dn, fn in os.walk(wroot):
        print("  work:", dp, sorted(fn), flush=True)
    stop_ok = it0["state"] == "paused" and saved.get("state") == "paused" and it0.get("pct", 0) > 0.1
    btn_ok = app.export_btn.cget("text").startswith("이어서")
    app.export_all()
    first_pct = None
    end = time.time() + 300
    while app.exporting and time.time() < end:
        pump(0.05)
        if first_pct is None and it0.get("state") == "exporting" and it0.get("pct", 0) > 0:
            first_pct = it0["pct"]
    outs2 = os.listdir(outdir2)
    print("resumed from:", first_pct and round(first_pct, 2), "outs:", outs2, it0["state"], flush=True)
    resume_ok = it0["state"] == "done" and len([f for f in outs2 if f.endswith("_편집.mp4")]) == 1 and \
        not os.path.exists(os.path.join(outdir2, ".videocut_work"))
    exporter.CHUNK_SEC = 120
    # 복구 화면 전환
    app.show_page("fix")
    pump(0.3)
    app.show_page("edit")
    pump(0.3)
    print(f"checks drag={drag_ok} scrub={scrub_ok} play={play_ok} tree={tree_ok} batch={batch_ok} "
          f"pause={paused_ok} stop={stop_ok} btn={btn_ok} resume={resume_ok}", flush=True)
    return all((drag_ok, scrub_ok, play_ok, tree_ok, batch_ok, paused_ok, stop_ok, btn_ok, resume_ok))


def main():
    if "--selftest" in sys.argv:
        # 점검은 실제 설정·목록을 건드리지 않게 임시 폴더에서
        import tempfile
        tmp = tempfile.mkdtemp(prefix="videocut_cfg_")
        os.environ["APPDATA"] = tmp
        os.environ["LOCALAPPDATA"] = tmp
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
                ui = _ui_test(app, root)
                ui = repair.selftest(lambda *a: print(*a, flush=True)) and ui
                print("dnd:", bool(TkinterDnD), flush=True)
                if ok and ui:
                    print("ui ok", flush=True)
                else:
                    sys.exitcode = 1
            except Exception:
                import traceback
                traceback.print_exc()
                sys.exitcode = 1
            finally:
                app.player.stop()
                root.destroy()

        root.after(800, test)
    root.mainloop()
    if getattr(sys, "exitcode", 0):
        sys.exit(1)


if __name__ == "__main__":
    main()
