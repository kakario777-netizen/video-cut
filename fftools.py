"""FFmpeg 도구 모음 — 위치 찾기, 영상 정보, 키프레임, 장면 한 장 뽑기, 진행률 실행"""
import json
import os
import shutil
import subprocess
import sys
import threading

NOWIN = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
EXE = ".exe" if sys.platform == "win32" else ""


def app_dir():
    # Nuitka 로 묶이면 sys.argv[0] 이 실제 exe 위치
    if "__compiled__" in globals() or getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.argv[0]))
    return os.path.dirname(os.path.abspath(__file__))


def _find(name):
    base = app_dir()
    for d in (os.path.join(base, "ffmpeg", "bin"), os.path.join(base, "ffmpeg"), base):
        p = os.path.join(d, name + EXE)
        if os.path.isfile(p):
            return p
    return shutil.which(name)


FFMPEG = _find("ffmpeg")
FFPROBE = _find("ffprobe")
FFPLAY = _find("ffplay")


def available():
    return bool(FFMPEG and FFPROBE)


def run(args, timeout=None):
    """짧은 명령 실행 → (returncode, stdout bytes, stderr text)"""
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                       creationflags=NOWIN, timeout=timeout)
    return p.returncode, p.stdout, p.stderr.decode("utf-8", "replace")


def _ratio(s):
    try:
        a, b = s.split("/")
        return float(a) / float(b) if float(b) else 0.0
    except Exception:
        try:
            return float(s)
        except Exception:
            return 0.0


def probe(path):
    """영상 정보: duration, vcodec, width, height, fps, pix_fmt, ten_bit, has_audio, acodec"""
    rc, out, err = run([FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
                       timeout=60)
    if rc != 0:
        raise RuntimeError("영상 정보를 읽지 못했습니다.\n" + err[-500:])
    d = json.loads(out.decode("utf-8", "replace"))
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not v:
        raise RuntimeError("영상 트랙이 없는 파일입니다.")
    dur = float(d.get("format", {}).get("duration") or v.get("duration") or 0)
    pix = v.get("pix_fmt", "")
    return {
        "path": path,
        "duration": dur,
        "vcodec": v.get("codec_name", ""),
        "width": int(v.get("width") or 0),
        "height": int(v.get("height") or 0),
        "fps": _ratio(v.get("avg_frame_rate", "0/1")) or _ratio(v.get("r_frame_rate", "0/1")),
        "pix_fmt": pix,
        "ten_bit": "10" in pix or "12" in pix,
        "has_audio": a is not None,
        "acodec": a.get("codec_name", "") if a else "",
        "start": float(v.get("start_time") or 0),
        "size": int(d.get("format", {}).get("size") or 0),
    }


def keyframes(path):
    """키프레임 시각 목록(초). 패킷 표시만 읽으므로 디코딩 없이 빠름"""
    p = subprocess.Popen([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "packet=pts_time,flags", "-of", "csv=p=0", path],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                         creationflags=NOWIN)
    kf = []
    for line in p.stdout:
        line = line.decode("ascii", "ignore").strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) >= 2 and "K" in parts[1]:
            try:
                kf.append(float(parts[0]))
            except ValueError:
                pass
    p.wait()
    kf.sort()
    return kf


def frame_at(path, t, width=320):
    """t초 장면 한 장 → PIL 이미지 (실패 시 None)"""
    from io import BytesIO
    from PIL import Image
    t = max(0.0, t)
    rc, out, _ = run([FFMPEG, "-v", "error", "-nostdin", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1",
                      "-vf", f"scale={width}:-2", "-f", "image2pipe", "-vcodec", "png", "-"], timeout=30)
    if rc != 0 or not out:
        return None
    try:
        return Image.open(BytesIO(out)).convert("RGB")
    except Exception:
        return None


def play(path, start, dur=None, title="미리보기"):
    """ffplay 로 구간 재생 (창 따로 뜸)"""
    if not FFPLAY:
        raise RuntimeError("ffplay 가 없어 재생할 수 없습니다.")
    args = [FFPLAY, "-hide_banner", "-loglevel", "error", "-autoexit", "-window_title", title,
            "-x", "960", "-y", "540", "-ss", f"{max(0.0, start):.3f}"]
    if dur:
        args += ["-t", f"{dur:.3f}"]
    args.append(path)
    return subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=NOWIN)


class Job:
    """오래 걸리는 ffmpeg 실행 — 진행률(초) 콜백, 취소 가능"""

    def __init__(self):
        self.proc = None
        self.cancelled = False

    def cancel(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.kill()
            except Exception:
                pass

    def run(self, args, on_time=None):
        """args 는 ffmpeg 뒤의 인자. 반환: (returncode, stderr 끝부분)"""
        if self.cancelled:
            return -1, "취소됨"
        full = [FFMPEG, "-hide_banner", "-nostdin", "-progress", "pipe:1", "-nostats"] + args
        self.proc = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                     creationflags=NOWIN)
        tail = []

        def read_err():
            for line in self.proc.stderr:
                tail.append(line.decode("utf-8", "replace"))
                if len(tail) > 40:
                    del tail[:20]

        th = threading.Thread(target=read_err, daemon=True)
        th.start()
        for line in self.proc.stdout:
            line = line.decode("ascii", "ignore").strip()
            if on_time and line.startswith("out_time_us="):
                try:
                    on_time(int(line.split("=", 1)[1]) / 1e6)
                except ValueError:
                    pass
        self.proc.wait()
        th.join(timeout=2)
        return self.proc.returncode, "".join(tail[-15:])


def fmt_time(t, ms=False):
    t = max(0.0, t)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    if ms:
        return f"{h}:{m:02d}:{s:04.1f}" if h else f"{m:02d}:{s:04.1f}"
    return f"{h}:{m:02d}:{int(s):02d}" if h else f"{m:02d}:{int(s):02d}"


def parse_time(s):
    """'1:02:03.5' / '02:03' / '123.4' → 초"""
    s = s.strip()
    if not s:
        raise ValueError("빈 시간")
    parts = s.split(":")
    t = 0.0
    for p in parts:
        t = t * 60 + float(p)
    return t
