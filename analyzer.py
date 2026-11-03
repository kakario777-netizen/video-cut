"""영상 분석 — 프레임을 작게 줄여 훑으면서 '변화량·밝기·선명도'를 기록하고,
그 기록으로 잘라낼 구간(렉 멈춤, 대기 화면, 흐림·가림, 검은 화면)을 찾음.
기록은 캐시에 저장 → 기준값만 바꿔 '다시 찾기' 하면 즉시 반영"""
import hashlib
import os
import subprocess

import numpy as np

import fftools

FEATURE_VERSION = 2
W, H = 320, 180          # 분석용 크기
PIX_THR = 10             # 이 이상 밝기가 바뀐 화소를 '변한 화소'로 셈

KINDS = {
    "start": "시작 대기",
    "freeze": "렉·멈춤",
    "wait": "대기 화면",
    "blur": "흐림·가림",
    "black": "검은 화면",
    "end": "종료 화면",
    "manual": "직접 추가",
}
KIND_COLORS = {
    "start": "#8e7cc3",
    "freeze": "#e06666",
    "wait": "#f6b26b",
    "blur": "#6fa8dc",
    "black": "#444444",
    "end": "#674ea7",
    "manual": "#93c47d",
}

DEFAULTS = {
    "analysis_fps": 5,       # 1초에 몇 장 볼지
    "use_gpu": True,         # 그래픽카드로 디코딩
    "freeze_min": 1.5,       # 이 시간 이상 멈추면 자름(초)
    "freeze_ratio": 0.3,     # 화면에서 바뀐 부분이 이 % 미만이면 '멈춤'
    "wait_len": 8.0,         # 이보다 길게 멈춘 건 '대기 화면'으로 표시(초)
    "black_luma": 18,        # 평균 밝기가 이 값 미만이면 검은 화면(0~255)
    "black_min": 1.0,        # 검은 화면 최소 길이(초)
    "blur_on": True,
    "blur_ratio": 30,        # 평소 선명도의 이 % 미만이면 흐림
    "blur_min": 2.0,         # 흐림 최소 길이(초)
    "merge_gap": 0.8,        # 잘릴 구간 사이가 이보다 짧으면 이어 붙임(초)
}


def cache_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    d = os.path.join(base, "VideoCut", "cache")
    os.makedirs(d, exist_ok=True)
    return d


def cache_path(path, fps):
    st = os.stat(path)
    key = f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}|{fps}|{W}x{H}|{FEATURE_VERSION}"
    return os.path.join(cache_dir(), hashlib.sha1(key.encode("utf-8")).hexdigest()[:20] + ".npz")


def _decode_cmd(path, fps, mode):
    base = [fftools.FFMPEG, "-v", "error", "-nostdin"]
    out = ["-an", "-sn", "-dn", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    if mode == "cuda_scale":
        return base + ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-i", path,
                       "-vf", f"fps={fps},scale_cuda={W}:{H}:format=nv12,hwdownload,format=nv12,format=gray"] + out
    if mode == "cuda":
        return base + ["-hwaccel", "cuda", "-i", path,
                       "-vf", f"fps={fps},scale={W}:{H}:flags=bilinear,format=gray"] + out
    if mode == "d3d11":
        return base + ["-hwaccel", "d3d11va", "-i", path,
                       "-vf", f"fps={fps},scale={W}:{H}:flags=bilinear,format=gray"] + out
    # cpu: 루프 필터를 건너뛰어 디코딩을 빠르게 (분석용이라 화질 영향 없음)
    return base + ["-threads", "0", "-skip_loop_filter", "all", "-i", path,
                   "-vf", f"fps={fps},scale={W}:{H}:flags=bilinear,format=gray"] + out


def _features(frames, prev):
    """frames: (n,H,W) uint8 → 특징 배열들. prev: 직전 프레임(없으면 None)"""
    f = frames.astype(np.int16)
    if prev is None:
        first = f[:1]
        prevs = np.concatenate([first, f[:-1]], axis=0)
    else:
        prevs = np.concatenate([prev.astype(np.int16)[None], f[:-1]], axis=0)
    d = np.abs(f - prevs)
    chg = (d > PIX_THR).mean(axis=(1, 2)).astype(np.float32) * 100.0   # 바뀐 화소 %
    if prev is None:
        chg[0] = 100.0
    luma = frames.mean(axis=(1, 2)).astype(np.float32)
    bright = (frames > 48).mean(axis=(1, 2)).astype(np.float32) * 100.0
    g = f.astype(np.float32)
    lap = g[:, :-2, 1:-1] + g[:, 2:, 1:-1] + g[:, 1:-1, :-2] + g[:, 1:-1, 2:] - 4 * g[:, 1:-1, 1:-1]
    sharp = lap.var(axis=(1, 2)).astype(np.float32)
    return chg, luma, bright, sharp


class Analysis:
    """특징 기록 (프레임마다 하나씩)"""

    def __init__(self, fps, chg, luma, bright, sharp, mode=""):
        self.fps = float(fps)
        self.chg, self.luma, self.bright, self.sharp = chg, luma, bright, sharp
        self.mode = mode

    def __len__(self):
        return len(self.chg)

    def save(self, p):
        np.savez_compressed(p, fps=self.fps, chg=self.chg, luma=self.luma, bright=self.bright, sharp=self.sharp,
                            mode=self.mode)

    @staticmethod
    def load(p):
        z = np.load(p)
        return Analysis(float(z["fps"]), z["chg"], z["luma"], z["bright"], z["sharp"], str(z["mode"]))


def analyze(path, info, settings, progress=None, job=None, use_cache=True):
    """영상 전체를 훑어 Analysis 반환. progress(비율 0~1, 메시지)"""
    fps = int(settings.get("analysis_fps", 5))
    cp = cache_path(path, fps)
    if use_cache and os.path.isfile(cp):
        try:
            a = Analysis.load(cp)
            if len(a) > 0:
                return a
        except Exception:
            pass
    modes = (["cuda_scale", "cuda", "d3d11"] if settings.get("use_gpu", True) else []) + ["cpu"]
    last_err = ""
    for mode in modes:
        res = _run_decode(path, info, fps, mode, progress, job)
        if job and job.cancelled:
            raise RuntimeError("취소됨")
        if isinstance(res, Analysis):
            try:
                res.save(cp)
            except Exception:
                pass
            return res
        last_err = res
    raise RuntimeError("영상을 읽지 못했습니다.\n" + (last_err or "")[-600:])


def _run_decode(path, info, fps, mode, progress, job):
    """성공 시 Analysis, 실패 시 오류 문자열"""
    cmd = _decode_cmd(path, fps, mode)
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                         creationflags=fftools.NOWIN, bufsize=W * H * 8)
    if job is not None:
        job.proc = p
    import threading
    err = []
    th = threading.Thread(target=lambda: err.append(p.stderr.read().decode("utf-8", "replace")), daemon=True)
    th.start()

    fsz = W * H
    batch = 50
    buf = bytearray(fsz * batch)
    mv = memoryview(buf)
    parts = ([], [], [], [])
    prev = None
    total = max(1, int(info["duration"] * fps))
    n = 0
    while True:
        got = 0
        while got < len(buf):
            k = p.stdout.readinto(mv[got:])
            if not k:
                break
            got += k
        cnt = got // fsz
        if cnt:
            frames = np.frombuffer(bytes(buf[:cnt * fsz]), dtype=np.uint8).reshape(cnt, H, W)
            for lst, arr in zip(parts, _features(frames, prev)):
                lst.append(arr)
            prev = frames[-1].copy()
            n += cnt
            if progress:
                progress(min(1.0, n / total), f"분석 중 ({_mode_name(mode)}) {fftools.fmt_time(n / fps)} / "
                                              f"{fftools.fmt_time(info['duration'])}")
        if got < len(buf):
            break
    p.wait()
    th.join(timeout=3)
    if job and job.cancelled:
        return "취소됨"
    # 끝까지 거의 다 읽었으면 성공으로 봄 (마지막 프레임 몇 장 차이는 무시)
    if p.returncode != 0 and n < total * 0.98:
        return f"[{mode}] " + (err[0] if err else f"종료 코드 {p.returncode}")
    if n == 0:
        return f"[{mode}] 프레임을 하나도 읽지 못함"
    cat = [np.concatenate(x) for x in parts]
    return Analysis(fps, *cat, mode=mode)


def _mode_name(mode):
    return {"cuda_scale": "그래픽카드", "cuda": "그래픽카드", "d3d11": "그래픽카드(D3D)", "cpu": "CPU"}.get(mode, mode)


def _runs(mask):
    """True 가 이어진 구간들 [(시작 index, 끝 index 포함)]"""
    if not len(mask):
        return []
    m = np.concatenate([[False], mask.astype(bool), [False]])
    dm = np.diff(m.astype(np.int8))
    starts = np.where(dm == 1)[0]
    ends = np.where(dm == -1)[0] - 1
    return list(zip(starts.tolist(), ends.tolist()))


def _rolling_median(x, win):
    if win <= 1 or len(x) < win:
        return x
    pad = win // 2
    xp = np.pad(x, (pad, win - 1 - pad), mode="edge")
    v = np.lib.stride_tricks.sliding_window_view(xp, win)
    return np.median(v, axis=1).astype(np.float32)


def detect(a, duration, s):
    """특징 기록 → 잘라낼 구간 목록 [{start,end,kind,cut,auto}]"""
    s = {**DEFAULTS, **(s or {})}
    fps = a.fps
    n = len(a)
    if n == 0:
        return []
    label = np.zeros(n, dtype=np.int8)        # 0 없음, 1 멈춤, 2 검은, 3 흐림
    black = (a.luma < float(s["black_luma"])) & (a.bright < 0.5)
    frozen = a.chg < float(s["freeze_ratio"])

    # 흐림: 평소(검은 화면·멈춤 아닌 장면) 선명도의 중앙값과 비교
    if s.get("blur_on", True):
        sharp = _rolling_median(a.sharp, max(1, int(round(fps))))
        normal = sharp[~black & ~frozen]
        if len(normal) > fps * 5:
            ref = float(np.median(normal))
            blur = (sharp < ref * float(s["blur_ratio"]) / 100.0) & ~black
            for i, j in _runs(blur):
                if (j - i + 1) / fps >= float(s["blur_min"]):
                    label[i:j + 1] = 3
    for i, j in _runs(frozen):
        if (j - i + 1) / fps >= float(s["freeze_min"]):
            label[i:j + 1] = 1
    for i, j in _runs(black):
        if (j - i + 1) / fps >= float(s["black_min"]):
            label[i:j + 1] = 2

    # 잘릴 구간 사이 짧은 틈은 메움
    gap = float(s["merge_gap"]) * fps
    cut_runs = _runs(label > 0)
    for (i1, j1), (i2, j2) in zip(cut_runs, cut_runs[1:]):
        if i2 - j1 - 1 < gap:
            label[j1 + 1:i2] = label[j1]

    segs = []
    i = 0
    while i < n:
        if label[i] == 0:
            i += 1
            continue
        j = i
        while j + 1 < n and label[j + 1] == label[i]:
            j += 1
        kind = {1: "freeze", 2: "black", 3: "blur"}[int(label[i])]
        st = max(0.0, (i - 0.5) / fps)
        en = duration if j >= n - 1 else min(duration, (j + 0.5) / fps)
        if kind == "freeze" and en - st >= float(s["wait_len"]):
            kind = "wait"
        segs.append({"start": round(st, 2), "end": round(en, 2), "kind": kind, "cut": True, "auto": True})
        i = j + 1
    if segs:
        if segs[0]["start"] <= 0.5 / fps + 0.01 and segs[0]["kind"] in ("freeze", "wait", "blur"):
            segs[0]["start"] = 0.0
            segs[0]["kind"] = "start"
        if segs[-1]["end"] >= duration - 0.01 and segs[-1]["kind"] in ("black", "freeze", "wait"):
            segs[-1]["kind"] = "end"
    return segs


def keep_ranges(segs, duration, min_keep=0.3):
    """잘라낼 구간들의 나머지 = 남길 구간 [(시작, 끝)]"""
    cuts = sorted((max(0.0, s["start"]), min(duration, s["end"])) for s in segs if s.get("cut"))
    keeps = []
    t = 0.0
    for a, b in cuts:
        if a > t:
            keeps.append((t, a))
        t = max(t, b)
    if t < duration:
        keeps.append((t, duration))
    return [(a, b) for a, b in keeps if b - a >= min_keep]
