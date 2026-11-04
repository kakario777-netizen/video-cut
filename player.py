"""프리뷰 재생 — ffmpeg 로 프레임을 받아 시간에 맞춰 내보냄(소리는 ffplay 로 따로)
- Player: 재생 (그래픽카드 디코딩 → 안 되면 CPU)
- Scrubber: 타임라인을 끌 때 그 지점 한 장 (가장 최근 요청만 처리)"""
import subprocess
import threading
import time

from PIL import Image

import fftools


class FrameSlot:
    """가장 최근 프레임 하나만 보관"""

    def __init__(self):
        self.lock = threading.Lock()
        self.seq = 0
        self.data = None

    def put(self, img, t, kind):
        with self.lock:
            self.seq += 1
            self.data = (self.seq, img, t, kind)

    def get(self):
        with self.lock:
            return self.data


def _kill(p):
    if p is not None:
        try:
            if p.poll() is None:
                p.kill()
        except Exception:
            pass


def _atempo(speed):
    if abs(speed - 1.0) < 0.01:
        return None
    parts = []
    s = speed
    while s > 2.0:
        parts.append("atempo=2.0")
        s /= 2.0
    parts.append(f"atempo={s:.3f}")
    return ",".join(parts)


class Player:
    def __init__(self, slot):
        self.slot = slot
        self.playing = False
        self.gen = 0
        self.vproc = None
        self.aproc = None
        self.gpu_ok = True
        self.t = 0.0
        self.frames = 0

    def start(self, path, info, t0, size, speed=1.0, sound=True, use_gpu=True):
        self.stop()
        self.gen += 1
        self.playing = True
        self.t = t0
        threading.Thread(target=self._run, args=(self.gen, path, info, t0, size, speed, sound, use_gpu),
                         daemon=True).start()

    def stop(self):
        self.gen += 1
        self.playing = False
        _kill(self.vproc)
        _kill(self.aproc)
        self.vproc = self.aproc = None

    def _run(self, gen, path, info, t0, size, speed, sound, use_gpu):
        w, h = size
        out_fps = min(info.get("fps") or 30.0, 30.0)
        step_fps = out_fps / speed          # 영상 시간 기준 프레임 간격
        fsz = w * h * 3
        vf = f"fps={step_fps:.6f},scale={w}:{h}:flags=fast_bilinear,format=rgb24"
        tries = [True, False] if (use_gpu and self.gpu_ok) else [False]
        p, first = None, b""
        for gpu in tries:
            cmd = [fftools.FFMPEG, "-v", "error", "-nostdin"] + (["-hwaccel", "cuda"] if gpu else []) + \
                  ["-ss", f"{t0:.3f}", "-i", path, "-an", "-sn", "-dn", "-vf", vf, "-f", "rawvideo", "-"]
            try:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     stdin=subprocess.DEVNULL, creationflags=fftools.NOWIN, bufsize=fsz * 2)
            except Exception:
                p = None
                continue
            if gen != self.gen:
                _kill(p)
                return
            self.vproc = p
            first = p.stdout.read(fsz)
            if len(first) == fsz:
                break
            _kill(p)
            if gen != self.gen:
                return
            if gpu:
                self.gpu_ok = False
        if len(first) != fsz:
            if gen == self.gen:
                self.playing = False
            return
        if sound and info.get("has_audio") and fftools.FFPLAY and gen == self.gen:
            a = [fftools.FFPLAY, "-nodisp", "-autoexit", "-loglevel", "quiet", "-vn", "-sn", "-ss", f"{t0:.3f}"]
            af = _atempo(speed)
            if af:
                a += ["-af", af]
            try:
                self.aproc = subprocess.Popen(a + [path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                              stderr=subprocess.DEVNULL, creationflags=fftools.NOWIN)
            except Exception:
                self.aproc = None
        start = time.perf_counter()
        k = 0
        buf = first
        while gen == self.gen:
            t = t0 + k / step_fps
            due = start + k / out_fps
            now = time.perf_counter()
            if now < due:
                time.sleep(due - now)
                now = due
            if now - due < 1.5 / out_fps:          # 많이 늦은 프레임은 건너뜀
                self.slot.put(Image.frombuffer("RGB", (w, h), buf, "raw", "RGB", 0, 1), t, "play")
                self.frames += 1
            self.t = t
            k += 1
            buf = p.stdout.read(fsz)
            if len(buf) < fsz:
                break
        _kill(p)
        if gen == self.gen:
            self.playing = False
            _kill(self.aproc)


class Scrubber:
    def __init__(self, slot):
        self.slot = slot
        self.req = None
        self.ev = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def request(self, path, t, size):
        self.req = (path, t, size)
        self.ev.set()

    def cancel(self):
        self.req = None

    def _loop(self):
        while True:
            self.ev.wait()
            self.ev.clear()
            r = self.req
            self.req = None
            if not r:
                continue
            path, t, (w, h) = r
            try:
                rc, out, _ = fftools.run([fftools.FFMPEG, "-v", "error", "-nostdin", "-threads", "0",
                                          "-ss", f"{max(0.0, t):.3f}", "-i", path, "-frames:v", "1", "-an", "-sn",
                                          "-vf", f"scale={w}:{h}:flags=bilinear,format=rgb24",
                                          "-f", "rawvideo", "-"], timeout=20)
            except Exception:
                continue
            if len(out) >= w * h * 3:
                self.slot.put(Image.frombuffer("RGB", (w, h), out[:w * h * 3], "raw", "RGB", 0, 1), t, "scrub")
