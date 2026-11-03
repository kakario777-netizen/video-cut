"""내보내기 — 남길 구간만 이어 붙여 저장
- 무손실(빠름): 다시 인코딩 없이 붙임. 자르는 지점은 키프레임에 맞춰 조금 앞으로 당겨짐
- H.265 인코딩: 구간마다 정확히 잘라 H.265 로 인코딩 후 붙임 (그래픽카드 → 안 되면 CPU)"""
import os
import shutil
import tempfile

import analyzer
import fftools

ENCODERS = ["hevc_nvenc", "hevc_qsv", "hevc_amf", "libx265"]
ENC_NAMES = {"hevc_nvenc": "엔비디아 그래픽카드", "hevc_qsv": "인텔 내장 그래픽", "hevc_amf": "AMD 그래픽",
             "libx265": "CPU(느림)"}
_enc_cache = {}


def encoder_ok(name):
    if name in _enc_cache:
        return _enc_cache[name]
    try:
        rc, _, _ = fftools.run([fftools.FFMPEG, "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                                "color=c=gray:s=640x360:r=30:d=0.5", "-c:v", name, "-f", "null", "-"], timeout=40)
        ok = rc == 0
    except Exception:
        ok = False
    _enc_cache[name] = ok
    return ok


def pick_encoder(prefer="auto"):
    if prefer and prefer != "auto" and encoder_ok(prefer):
        return prefer
    for e in ENCODERS:
        if encoder_ok(e):
            return e
    return None


def enc_args(enc, q, ten_bit, gop):
    q = str(int(q))
    if enc == "hevc_nvenc":
        a = ["-c:v", enc, "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", q, "-b:v", "0"]
        a += ["-pix_fmt", "p010le", "-profile:v", "main10"] if ten_bit else ["-pix_fmt", "yuv420p", "-profile:v", "main"]
    elif enc == "hevc_qsv":
        a = ["-c:v", enc, "-preset", "medium", "-global_quality", q, "-pix_fmt", "p010le" if ten_bit else "nv12"]
    elif enc == "hevc_amf":
        a = ["-c:v", enc, "-quality", "quality", "-rc", "cqp", "-qp_i", q, "-qp_p", q,
             "-pix_fmt", "p010le" if ten_bit else "nv12"]
    else:
        a = ["-c:v", "libx265", "-preset", "fast", "-crf", q, "-x265-params", "log-level=error",
             "-pix_fmt", "yuv420p10le" if ten_bit else "yuv420p"]
    return a + ["-g", str(gop), "-tag:v", "hvc1"]


def unique_out(path):
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{stem} ({i}){ext}"):
        i += 1
    return f"{stem} ({i}){ext}"


def default_out(src):
    stem, _ = os.path.splitext(src)
    return unique_out(stem + "_편집.mp4")


def _q(path):
    return "'" + path.replace("\\", "/").replace("'", "'\\''") + "'"


def snap_keeps(keeps, kf, start_offset=0.0):
    """무손실용: 남길 구간 시작을 바로 앞 키프레임으로 당김. 겹치면 합침.
    반환: (새 구간들, 합쳐져서 못 자른 구간 수)"""
    import bisect
    rel = [k - start_offset for k in kf]
    out = []
    merged = 0
    for s, e in keeps:
        if s > 0.001 and rel:
            i = bisect.bisect_right(rel, s + 0.05) - 1
            s2 = max(0.0, rel[i]) if i >= 0 else 0.0
        else:
            s2 = 0.0
        if out and s2 <= out[-1][1] + 0.01:
            out[-1] = (out[-1][0], e)
            merged += 1
        else:
            out.append((s2, e))
    return out, merged


def export(path, info, segs, out, mode, settings, progress=None, job=None):
    """mode: auto / copy / encode. 반환: 결과 메시지"""
    job = job or fftools.Job()
    dur = info["duration"]
    keeps = analyzer.keep_ranges(segs, dur)
    if not keeps:
        raise RuntimeError("남길 구간이 없습니다.")
    if mode == "auto":
        mode = "copy" if info["vcodec"] == "hevc" else "encode"
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".videocut_", dir=os.path.dirname(os.path.abspath(out)))
    try:
        if mode == "copy":
            msg = _copy(path, info, keeps, out, tmp, progress, job)
        else:
            msg = _encode(path, info, keeps, out, tmp, settings, progress, job)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if job.cancelled:
        try:
            os.remove(out)
        except OSError:
            pass
        raise RuntimeError("취소됨")
    return msg


def _concat(list_path, out, info, progress, total, job, base=0.0, overall=None):
    args = ["-y", "-f", "concat", "-safe", "0", "-i", list_path, "-map", "0:v:0", "-map", "0:a:0?",
            "-c", "copy", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart"]
    if info["vcodec"] == "hevc":
        args += ["-tag:v", "hvc1"]
    args.append(out)
    rc, err = job.run(args, on_time=(lambda t: progress(min(1.0, (base + t) / (overall or total)), "붙이는 중"))
                      if progress else None)
    if rc != 0 and not job.cancelled:
        raise RuntimeError("영상을 붙이지 못했습니다.\n" + err[-700:])


def _copy(path, info, keeps, out, tmp, progress, job):
    if progress:
        progress(0.0, "키프레임 위치 확인 중")
    kf = fftools.keyframes(path)
    snapped, merged = snap_keeps(keeps, kf, info.get("start", 0.0))
    lst = os.path.join(tmp, "list.txt")
    with open(lst, "w", encoding="utf-8") as f:
        f.write("ffconcat version 1.0\n")
        for s, e in snapped:
            f.write(f"file {_q(os.path.abspath(path))}\ninpoint {s:.3f}\noutpoint {e:.3f}\n")
    total = sum(e - s for s, e in snapped)
    _concat(lst, out, info, progress, total, job)
    kept = sum(e - s for s, e in snapped)
    msg = f"무손실로 저장했습니다. ({fftools.fmt_time(info['duration'])} → {fftools.fmt_time(kept)})"
    if merged:
        msg += f"\n키프레임 간격보다 짧아 무손실로는 못 자른 구간 {merged}개는 남겨 두었습니다. (정확히 자르려면 'H.265 인코딩')"
    return msg


def _encode(path, info, keeps, out, tmp, settings, progress, job):
    enc = pick_encoder(settings.get("encoder", "auto"))
    if not enc:
        raise RuntimeError("H.265 인코더를 찾지 못했습니다.")
    q = settings.get("quality", 24)
    gop = max(10, int(round((info.get("fps") or 30) * 2)))
    total = sum(e - s for s, e in keeps)
    done = 0.0
    files = []
    hw_dec = enc == "hevc_nvenc" and settings.get("use_gpu", True)
    for idx, (s, e) in enumerate(keeps):
        if job.cancelled:
            return ""
        seg = os.path.join(tmp, f"seg{idx:04d}.mp4")
        body = ["-ss", f"{s:.3f}", "-i", path, "-t", f"{e - s:.3f}", "-map", "0:v:0", "-map", "0:a:0?"]
        body += enc_args(enc, q, info.get("ten_bit"), gop)
        body += ["-c:a", "aac", "-b:a", "192k", "-video_track_timescale", "90000",
                 "-avoid_negative_ts", "make_zero", seg]
        label = f"인코딩 중 ({ENC_NAMES.get(enc, enc)}) 구간 {idx + 1}/{len(keeps)}"
        cb = (lambda t, d=done, lb=label: progress(min(1.0, (d + t) / total * 0.97), lb)) if progress else None
        rc, err = (-1, "")
        if hw_dec:
            rc, err = job.run(["-y", "-hwaccel", "cuda"] + body, on_time=cb)
            if rc != 0 and not job.cancelled:
                hw_dec = False      # 그래픽카드 디코딩이 안 되면 이후로는 CPU 디코딩
        if rc != 0 and not job.cancelled:
            rc, err = job.run(["-y"] + body, on_time=cb)
        if rc != 0 and not job.cancelled and enc != "libx265":
            # 그래픽카드 인코딩 실패 → CPU 인코딩으로 전환
            enc = "libx265"
            body2 = ["-ss", f"{s:.3f}", "-i", path, "-t", f"{e - s:.3f}", "-map", "0:v:0", "-map", "0:a:0?"] + \
                    enc_args(enc, q, info.get("ten_bit"), gop) + \
                    ["-c:a", "aac", "-b:a", "192k", "-video_track_timescale", "90000",
                     "-avoid_negative_ts", "make_zero", seg]
            rc, err = job.run(["-y"] + body2, on_time=cb)
        if job.cancelled:
            return ""
        if rc != 0:
            raise RuntimeError(f"구간 {idx + 1} 인코딩 실패\n" + err[-700:])
        files.append(seg)
        done += e - s
    lst = os.path.join(tmp, "list.txt")
    with open(lst, "w", encoding="utf-8") as f:
        f.write("ffconcat version 1.0\n")
        for p in files:
            f.write(f"file {_q(p)}\n")
    info2 = dict(info, vcodec="hevc")
    _concat(lst, out, info2, (lambda r, m: progress(0.97 + 0.03 * r, "붙이는 중")) if progress else None,
            total, job)
    return (f"H.265 로 저장했습니다. ({ENC_NAMES.get(enc, enc)}, "
            f"{fftools.fmt_time(info['duration'])} → {fftools.fmt_time(total)})")
