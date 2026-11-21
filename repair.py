"""깨진 영상 복구 — 녹화가 갑자기 멈춰 끝부분 정보(moov)가 없는 MP4 (삼성 녹화 .pending-… 파일 등)
1) 읽히는 파일이면 그대로 mp4 로 정리
2) 안 읽히면 같은 폰·같은 설정의 정상 녹화 파일(기준 영상)을 참고해 untrunc 로 복구"""
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import fftools


def find_untrunc():
    base = fftools.app_dir()
    for d in (os.path.join(base, "untrunc"), os.path.join(base, "untrunc", "untrunc_x64"), base):
        p = os.path.join(d, "untrunc" + fftools.EXE)
        if os.path.isfile(p):
            return p
    return shutil.which("untrunc")


def nice_stem(path):
    """.pending-1783251470-1782646670892 → 원래 이름(밀리초 시각이면 날짜로)"""
    name = os.path.basename(path)
    m = re.match(r"^\.?(?:pending|trashed)-\d+-(.+)$", name)
    if m:
        name = m.group(1)
    stem = os.path.splitext(name)[0] if re.search(r"\.(mp4|m4v|mov|3gp)$", name, re.I) else name
    stem = stem.lstrip(".") or "복구영상"
    if re.fullmatch(r"1\d{12}", stem):          # 밀리초 시각
        stem = "녹화_" + time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(int(stem) / 1000))
    return stem


def default_out(path, out_dir=""):
    d = out_dir or os.path.dirname(os.path.abspath(path))
    base = os.path.join(d, nice_stem(path) + "_복구.mp4")
    out, i = base, 2
    while os.path.exists(out):
        out = base[:-4] + f" ({i}).mp4"
        i += 1
    return out


def readable(path):
    try:
        info = fftools.probe(path)
        return info if info["duration"] > 0.5 else None
    except Exception:
        return None


def _run(cmd, job, log=None):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         creationflags=fftools.NOWIN)
    if job is not None:
        job.proc = p
    tail = []
    for line in p.stdout:
        t = line.decode("utf-8", "replace").rstrip()
        if t:
            tail.append(t)
            if len(tail) > 60:
                del tail[:30]
            if log:
                log(t)
    p.wait()
    return p.returncode, "\n".join(tail[-12:])


def repair(broken, ref, out, job=None, progress=None):
    """반환: 결과 메시지. 실패 시 RuntimeError"""
    job = job or fftools.Job()
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    part = out[:-4] + ".작업중.mp4"
    try:
        info = readable(broken)
        if info:
            if progress:
                progress(0.1, "읽을 수 있는 파일입니다 — mp4로 정리 중")
            rc, err = job.run(["-y", "-i", broken, "-map", "0", "-c", "copy", "-ignore_unknown",
                               "-movflags", "+faststart", part],
                              on_time=(lambda t: progress(min(1.0, t / info["duration"]), "mp4로 정리 중"))
                              if progress else None)
            if job.cancelled:
                raise RuntimeError("취소됨")
            if rc != 0:
                raise RuntimeError("정리하지 못했습니다.\n" + err[-500:])
            os.replace(part, out)
            return f"정상적으로 읽히는 파일이라 그대로 정리했습니다. ({fftools.fmt_time(info['duration'])})"

        if not ref or not os.path.isfile(ref):
            raise RuntimeError("이 파일은 끝부분 정보가 없어 기준 영상이 필요합니다.\n"
                               "같은 폰·같은 녹화 기능으로 정상 저장된 영상을 '기준 영상'에 지정해주세요.")
        if not readable(ref):
            raise RuntimeError("기준 영상도 재생되지 않는 파일입니다. 정상 녹화 파일을 골라주세요.")
        ut = find_untrunc()
        if not ut:
            raise RuntimeError("복구 도구(untrunc)를 찾지 못했습니다. 프로그램을 다시 설치해주세요.")
        if progress:
            progress(0.0, "복구 중 — 영상 크기에 따라 몇 분 걸릴 수 있습니다")
        size = max(1, os.path.getsize(broken))
        state = {"p": 0.0}

        def log(line):
            m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", line)
            if m and progress:
                state["p"] = min(0.99, float(m.group(1)) / 100)
                progress(state["p"], f"복구 중 {int(state['p'] * 100)}%")

        # 진행률을 안 찍는 경우를 위해 시간 기준으로 조금씩 움직임
        stop = threading.Event()

        def tick():
            t0 = time.time()
            while not stop.wait(1.0):
                if progress and state["p"] < 0.05:
                    guess = min(0.9, (time.time() - t0) / max(20.0, size / 40e6))
                    progress(guess, f"복구 중 ({fftools.fmt_time(time.time() - t0)})")

        th = threading.Thread(target=tick, daemon=True)
        th.start()
        try:
            rc, tail = _run([ut, "-n", "-dst", part, ref, broken], job, log)
        finally:
            stop.set()
        if job.cancelled:
            raise RuntimeError("취소됨")
        if not os.path.isfile(part) or os.path.getsize(part) < 1024:
            raise RuntimeError("복구하지 못했습니다. 기준 영상이 같은 폰·같은 설정(해상도·화질)으로 녹화한 파일인지 "
                               "확인해주세요.\n\n" + tail[-600:])
        fixed = readable(part)
        if not fixed:
            raise RuntimeError("복구한 파일을 읽을 수 없습니다. 다른 기준 영상으로 다시 시도해보세요.\n\n" + tail[-400:])
        # 재생기 호환을 위해 앞쪽에 정보 배치
        if progress:
            progress(0.99, "마무리 중")
        part2 = out[:-4] + ".작업중2.mp4"
        rc, err = job.run(["-y", "-i", part, "-map", "0", "-c", "copy", "-movflags", "+faststart", part2])
        if rc == 0 and os.path.isfile(part2):
            os.replace(part2, part)
        else:
            try:
                os.remove(part2)
            except OSError:
                pass
        os.replace(part, out)
        return f"복구했습니다. ({fftools.fmt_time(fixed['duration'])} 분량)"
    finally:
        for p in (part, out[:-4] + ".작업중2.mp4"):
            if os.path.exists(p) and p != out:
                try:
                    os.remove(p)
                except OSError:
                    pass


# ── 자체 점검용: 끝부분 정보가 없는 녹화 파일 흉내 ──
def _top_atoms(path):
    import struct
    atoms = []
    with open(path, "rb") as f:
        pos = 0
        size_total = os.path.getsize(path)
        while pos + 8 <= size_total:
            f.seek(pos)
            sz, typ = struct.unpack(">I4s", f.read(8))
            if sz == 1:
                sz = struct.unpack(">Q", f.read(8))[0]
            elif sz == 0:
                sz = size_total - pos
            atoms.append((typ.decode("latin1"), pos, sz))
            pos += sz
    return atoms


def make_broken_sample(d):
    """정상 기준 영상과 moov 가 잘린 영상 만들기 → (ref, broken)"""
    ref = os.path.join(d, "ref_ok.mp4")
    full = os.path.join(d, "full.mp4")
    broken = os.path.join(d, ".pending-1783251470-1782646670892")
    for path, dur in ((ref, 6), (full, 12)):
        rc, _, err = fftools.run([fftools.FFMPEG, "-y", "-v", "error", "-nostdin",
                                  "-f", "lavfi", "-i", f"testsrc2=s=640x360:r=30:d={dur}",
                                  "-f", "lavfi", "-i", f"sine=f=330:d={dur}",
                                  "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-pix_fmt", "yuv420p",
                                  "-c:a", "aac", "-shortest", path], timeout=120)
        if rc != 0:
            raise RuntimeError(err[-300:])
    moov = next(a for a in _top_atoms(full) if a[0] == "moov")
    with open(full, "rb") as f, open(broken, "wb") as g:
        g.write(f.read(moov[1]))
    return ref, broken


def selftest(log=print):
    import tempfile
    if not find_untrunc():
        log("repair skipped (untrunc 없음)")
        return True
    d = tempfile.mkdtemp(prefix="videocut_repair_")
    ref, broken = make_broken_sample(d)
    log(f"broken readable: {bool(readable(broken))}")
    out = default_out(broken)
    msg = repair(broken, ref, out)
    info = readable(out)
    log(f"repair: {msg} -> {os.path.basename(out)} {info and round(info['duration'], 1)}s")
    ok = bool(info) and info["duration"] > 8
    log("repair ok" if ok else "repair FAIL")
    return ok
