"""내보내기 — 남길 구간만 이어 붙여 저장
- 무손실(빠름): 다시 인코딩 없이 붙임. 자르는 지점은 키프레임에 맞춰 조금 앞으로 당겨짐
- H.265 인코딩: 구간마다 정확히 잘라 H.265 로 인코딩 후 붙임 (그래픽카드 → 안 되면 CPU)"""
import os
import shutil

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


CHUNK_SEC = 120          # 인코딩 조각 길이(초) — 껐다 켜면 이 단위로 이어서 진행


def work_dir_for(src, out):
    import hashlib
    key = hashlib.sha1(os.path.abspath(src).lower().encode("utf-8")).hexdigest()[:16]
    return os.path.join(os.path.dirname(os.path.abspath(out)), ".videocut_work", key)


def remove_work(wd):
    shutil.rmtree(wd, ignore_errors=True)
    parent = os.path.dirname(wd)
    try:
        if not os.listdir(parent):
            os.rmdir(parent)
    except OSError:
        pass


def export(path, info, segs, out, mode, settings, progress=None, job=None):
    """mode: auto / copy / encode. 반환: 결과 메시지
    결과는 임시 이름으로 만든 뒤 끝나면 진짜 이름으로 바꿈. 인코딩 조각은 작업 폴더에 남아 있어
    중지·종료 후 다시 내보내면 이어서 진행"""
    job = job or fftools.Job()
    dur = info["duration"]
    keeps = analyzer.keep_ranges(segs, dur)
    if not keeps:
        raise RuntimeError("남길 구간이 없습니다.")
    if mode == "auto":
        mode = "copy" if info["vcodec"] == "hevc" else "encode"
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    part = out[:-4] + ".작업중.mp4" if out.lower().endswith(".mp4") else out + ".작업중.mp4"
    wd = work_dir_for(path, out)
    os.makedirs(wd, exist_ok=True)
    try:
        if mode == "copy":
            msg = _copy(path, info, keeps, part, wd, progress, job)
        else:
            msg = _encode(path, info, keeps, part, wd, settings, progress, job)
        if job.cancelled:
            raise RuntimeError("취소됨")
        os.replace(part, out)
        remove_work(wd)
        return msg
    except Exception:
        try:
            os.remove(part)
        except OSError:
            pass
        if job.cancelled:
            raise RuntimeError("취소됨")
        raise


def _concat(list_path, out, info, progress, total, job, audio=None):
    args = ["-y", "-f", "concat", "-safe", "0", "-i", list_path]
    if audio:
        args += ["-i", audio, "-map", "0:v:0", "-map", "1:a:0"]
    else:
        args += ["-map", "0:v:0", "-map", "0:a:0?"]
    args += ["-c", "copy", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart"]
    if info["vcodec"] == "hevc":
        args += ["-tag:v", "hvc1"]
    args.append(out)
    rc, err = job.run(args, on_time=(lambda t: progress(min(1.0, t / max(0.1, total)), "붙이는 중"))
                      if progress else None)
    if rc != 0 and not job.cancelled:
        raise RuntimeError("영상을 붙이지 못했습니다.\n" + err[-700:])


def _copy(path, info, keeps, out, wd, progress, job):
    if progress:
        progress(0.0, "키프레임 위치 확인 중")
    kf = fftools.keyframes(path)
    snapped, merged = snap_keeps(keeps, kf, info.get("start", 0.0))
    lst = os.path.join(wd, "copy_list.txt")
    with open(lst, "w", encoding="utf-8") as f:
        f.write("ffconcat version 1.0\n")
        for s, e in snapped:
            f.write(f"file {_q(os.path.abspath(path))}\ninpoint {s:.3f}\noutpoint {e:.3f}\n")
    total = sum(e - s for s, e in snapped)
    _concat(lst, out, info, progress, total, job)
    msg = f"무손실로 저장했습니다. ({fftools.fmt_time(info['duration'])} → {fftools.fmt_time(total)})"
    if merged:
        msg += f"\n키프레임 간격보다 짧아 무손실로는 못 자른 구간 {merged}개는 남겨 두었습니다. (정확히 자르려면 'H.265 인코딩')"
    return msg


def plan_chunks(keeps, fps, chunk_sec=None):
    """남길 구간 → 프레임 단위 조각 [(시작 프레임, 프레임 수)], 구간 [(시작 프레임, 끝 프레임)]"""
    chunk_sec = chunk_sec or CHUNK_SEC
    step = max(1, int(round(chunk_sec * fps)))
    chunks, ranges = [], []
    for s, e in keeps:
        a, b = int(round(s * fps)), int(round(e * fps))
        if b <= a:
            continue
        ranges.append((a, b))
        while a < b:
            n = min(step, b - a)
            chunks.append((a, n))
            a += n
    return chunks, ranges


def _load_manifest(wd, sig):
    import json
    mp = os.path.join(wd, "manifest.json")
    try:
        with open(mp, encoding="utf-8") as f:
            m = json.load(f)
        if m.get("sig") == sig:
            return set(i for i in m.get("done", []) if os.path.isfile(os.path.join(wd, f"v{i:05d}.mp4")))
    except Exception:
        pass
    # 조건이 바뀌었거나 처음 → 작업 폴더 비우고 새로
    for fn in os.listdir(wd):
        try:
            os.remove(os.path.join(wd, fn))
        except OSError:
            pass
    return set()


def _save_manifest(wd, sig, done):
    import json
    mp = os.path.join(wd, "manifest.json")
    tmp = mp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"sig": sig, "done": sorted(done)}, f)
    os.replace(tmp, mp)


def encode_progress(path, out):
    """이전에 끝낸 조각 비율 (0~1) — 목록 표시용"""
    import json
    try:
        with open(os.path.join(work_dir_for(path, out), "manifest.json"), encoding="utf-8") as f:
            m = json.load(f)
        n = m["sig"]["chunks"]
        return len(m.get("done", [])) / max(1, n)
    except Exception:
        return 0.0


def _encode(path, info, keeps, out, wd, settings, progress, job):
    enc = pick_encoder(settings.get("encoder", "auto"))
    if not enc:
        raise RuntimeError("H.265 인코더를 찾지 못했습니다.")
    while True:
        try:
            return _encode_with(path, info, keeps, out, wd, settings, progress, job, enc)
        except _EncoderFailed as e:
            if enc == "libx265":
                raise RuntimeError(str(e))
            enc = "libx265"         # 그래픽카드 인코딩이 안 되면 CPU 로 처음부터 (섞이면 안 붙음)


class _EncoderFailed(Exception):
    pass


def _encode_with(path, info, keeps, out, wd, settings, progress, job, enc):
    q = int(settings.get("quality", 24))
    fps = info.get("fps") or 30.0
    fps_str = info.get("fps_str") or f"{fps:.6f}"
    gop = max(10, int(round(fps * 2)))
    chunks, ranges = plan_chunks(keeps, fps)
    st = os.stat(path)
    sig = {"src": os.path.abspath(path), "size": st.st_size, "mtime": int(st.st_mtime), "enc": enc, "q": q,
           "ten": bool(info.get("ten_bit")), "fps": fps_str, "ranges": ranges, "chunks": len(chunks),
           "chunk_sec": CHUNK_SEC, "v": 1}
    import json
    sig = json.loads(json.dumps(sig))       # 저장된 것과 같은 형태로 비교 (튜플→목록)
    done = _load_manifest(wd, sig)
    total_f = sum(n for _, n in chunks)
    done_f = sum(chunks[i][1] for i in done)
    hw_dec = settings.get("use_gpu", True) and enc == "hevc_nvenc"
    name = ENC_NAMES.get(enc, enc)
    for idx, (a, n) in enumerate(chunks):
        if idx in done:
            continue
        job.wait_if_paused()
        if job.cancelled:
            return ""
        fn = os.path.join(wd, f"v{idx:05d}.mp4")
        tmp = os.path.join(wd, f"v{idx:05d}.part.mp4")
        ss = max(0.0, (a - 0.25) / fps)
        body = ["-ss", f"{ss:.6f}", "-i", path, "-map", "0:v:0", "-an", "-sn", "-dn",
                "-frames:v", str(n), "-fps_mode", "cfr", "-r", fps_str] + \
            enc_args(enc, q, info.get("ten_bit"), gop) + ["-video_track_timescale", "90000", "-f", "mp4", tmp]
        label = f"인코딩 중 ({name}) {int(done_f / max(1, total_f) * 100)}%  ·  조각 {idx + 1}/{len(chunks)}"
        cb = (lambda t, d=done_f, nn=n, lb=label: progress(min(1.0, (d + min(nn, max(0.0, t) * fps)) / total_f) * 0.95, lb)) \
            if progress else None
        rc, err = -1, ""
        if hw_dec:
            rc, err = job.run(["-y", "-hwaccel", "cuda"] + body, on_time=cb)
            if rc != 0 and not job.cancelled:
                hw_dec = False      # 그래픽카드 디코딩이 안 되면 이후로는 CPU 디코딩
        if rc != 0 and not job.cancelled:
            rc, err = job.run(["-y"] + body, on_time=cb)
        if job.cancelled:
            return ""
        if rc != 0:
            raise _EncoderFailed(f"조각 {idx + 1} 인코딩 실패\n" + err[-700:])
        os.replace(tmp, fn)
        done.add(idx)
        done_f += n
        _save_manifest(wd, sig, done)
    if progress:
        progress(0.95, "소리 정리 중")
    audio = None
    if info.get("has_audio"):
        job.wait_if_paused()
        audio = os.path.join(wd, "audio.m4a")
        script = os.path.join(wd, "audio_filter.txt")
        parts = [f"[0:a:0]atrim=start={a / fps:.6f}:end={b / fps:.6f},asetpts=PTS-STARTPTS[a{i}]"
                 for i, (a, b) in enumerate(ranges)]
        graph = ";".join(parts) + ";" + "".join(f"[a{i}]" for i in range(len(ranges))) + \
            f"concat=n={len(ranges)}:v=0:a=1[aout]"
        with open(script, "w", encoding="utf-8") as f:
            f.write(graph)
        total_s = total_f / fps
        rc, err = job.run(["-y", "-i", path, "-filter_complex_script", script, "-map", "[aout]",
                           "-c:a", "aac", "-b:a", "192k", audio],
                          on_time=(lambda t: progress(0.95 + 0.03 * min(1.0, t / total_s), "소리 정리 중"))
                          if progress else None)
        if job.cancelled:
            return ""
        if rc != 0:
            raise RuntimeError("소리를 정리하지 못했습니다.\n" + err[-700:])
    lst = os.path.join(wd, "list.txt")
    with open(lst, "w", encoding="utf-8") as f:
        f.write("ffconcat version 1.0\n")
        for i in range(len(chunks)):
            f.write(f"file {_q(os.path.join(wd, f'v{i:05d}.mp4'))}\n")
    _concat(lst, out, dict(info, vcodec="hevc"), (lambda r, m: progress(0.98 + 0.02 * r, "붙이는 중"))
            if progress else None, total_f / fps, job, audio=audio)
    return (f"H.265 로 저장했습니다. ({name}, "
            f"{fftools.fmt_time(info['duration'])} → {fftools.fmt_time(total_f / fps)})")
