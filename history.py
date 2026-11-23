"""변환 기록 — 내보낼 때마다 전·후 정보와 썸네일을 쌓아 둠 (목록을 지우거나 껐다 켜도 남음)
위치: %APPDATA%\\VideoCut\\history\\  (history.jsonl + thumbs\\*.jpg)"""
import json
import os
import time

import fftools


def base_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    d = os.path.join(base, "VideoCut", "history")
    os.makedirs(os.path.join(d, "thumbs"), exist_ok=True)
    return d


def log_file():
    return os.path.join(base_dir(), "history.jsonl")


def _fmt(info):
    if not info:
        return ""
    c = {"hevc": "HEVC", "h264": "H264", "av1": "AV1", "vp9": "VP9"}.get(info.get("vcodec", ""),
                                                                      (info.get("vcodec") or "?").upper())
    w, h = info.get("disp_w") or info.get("width"), info.get("disp_h") or info.get("height")
    fps = info.get("fps") or 0
    return f"{c} {w}x{h} {fps:.0f}fps" + (" 10bit" if info.get("ten_bit") else "")


def _thumb(path, t, dst):
    try:
        im = fftools.frame_at(path, t, 320)
        if im is None:
            return ""
        im.thumbnail((200, 200))
        im.save(dst, "JPEG", quality=80)
        return os.path.basename(dst)
    except Exception:
        return ""


def record(src, out, src_info, msg="", cuts=0, crop=None, elapsed=0.0):
    """내보내기 성공 1건 기록 (실패해도 내보내기에는 영향 없음)"""
    try:
        oi = fftools.probe(out)
    except Exception:
        oi = {}
    sid = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
    td = os.path.join(base_dir(), "thumbs")
    sd = src_info.get("duration") or 0
    od = oi.get("duration") or 0
    e = {
        "id": sid, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "src": os.path.abspath(src), "out": os.path.abspath(out),
        "src_dur": sd, "out_dur": od,
        "src_size": os.path.getsize(src) if os.path.isfile(src) else 0,
        "out_size": os.path.getsize(out) if os.path.isfile(out) else 0,
        "src_fmt": _fmt(src_info), "out_fmt": _fmt(oi), "cuts": int(cuts),
        "crop": bool(crop), "elapsed": round(float(elapsed), 1), "msg": (msg or "").splitlines()[0] if msg else "",
        "th_src": _thumb(src, sd * 0.3, os.path.join(td, sid + "_a.jpg")),
        "th_out": _thumb(out, od * 0.3, os.path.join(td, sid + "_b.jpg")),
    }
    with open(log_file(), "a", encoding="utf-8") as f:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return e


def load():
    items = []
    try:
        with open(log_file(), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        items.append(json.loads(line))
                    except Exception:
                        pass
    except OSError:
        pass
    items.sort(key=lambda x: x.get("time", ""))
    return items


def thumb_path(name):
    return os.path.join(base_dir(), "thumbs", name) if name else ""


def mb(x):
    return f"{x / 1073741824:.2f}GB" if x >= 1073741824 else f"{x / 1048576:.0f}MB"
