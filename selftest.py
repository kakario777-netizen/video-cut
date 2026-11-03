"""자체 점검 — 가짜 라이브 영상을 만들어 탐지·저장이 제대로 되는지 확인
영상 구성(30초): 0~4 시작 대기(정지) / 8~8.6 짧은 멈춤(자르면 안 됨) / 14~16.5 렉 멈춤
               / 20~24 흐림 / 27~30 검은 화면"""
import os
import tempfile

import analyzer
import exporter
import fftools


def make_sample(path, codec="libx265"):
    vf = ("[0:v]split[a0][b0];[a0][b0]freezeframes=first=1:last=119:replace=0[c0];"
          "[c0]split[a1][b1];[a1][b1]freezeframes=first=241:last=257:replace=240[c1];"
          "[c1]split[a2][b2];[a2][b2]freezeframes=first=421:last=494:replace=420,"
          "gblur=sigma=25:enable='between(t,20,24)',"
          "drawbox=c=black:t=fill:enable='gte(t,27)'[v]")
    enc = (["-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "keyint=30:min-keyint=30:log-level=error"]
           if codec == "libx265" else ["-c:v", "libx264", "-preset", "ultrafast", "-g", "30"])
    rc, _, err = fftools.run([fftools.FFMPEG, "-y", "-v", "error", "-nostdin",
                              "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=30",
                              "-f", "lavfi", "-i", "sine=f=440:d=30",
                              "-filter_complex", vf, "-map", "[v]", "-map", "1:a", *enc, "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", path], timeout=300)
    if rc != 0:
        raise RuntimeError("테스트 영상 만들기 실패: " + err[-400:])


def run(log=print):
    ok = True
    d = tempfile.mkdtemp(prefix="videocut_test_")
    src = os.path.join(d, "테스트 영상.mp4")
    make_sample(src)
    info = fftools.probe(src)
    log(f"probe: {info['vcodec']} {info['width']}x{info['height']} {info['duration']:.2f}s audio={info['has_audio']}")
    a = analyzer.analyze(src, info, dict(analyzer.DEFAULTS), use_cache=False)
    log(f"analyze: {len(a)} frames mode={a.mode}")
    segs = analyzer.detect(a, info["duration"], analyzer.DEFAULTS)
    for s in segs:
        log(f"  seg {s['kind']:7s} {s['start']:6.2f} ~ {s['end']:6.2f}")
    expect = [("start", 0, 4), ("freeze", 14, 16.5), ("blur", 20, 24), ("end", 27, 30)]
    if len(segs) != len(expect):
        log("FAIL: 구간 개수가 다름")
        ok = False
    else:
        for s, (k, st, en) in zip(segs, expect):
            if s["kind"] != k or abs(s["start"] - st) > 0.6 or abs(s["end"] - en) > 0.6:
                log(f"FAIL: 기대 {k} {st}~{en}")
                ok = False
    keep = sum(b - a_ for a_, b in analyzer.keep_ranges(segs, info["duration"]))
    log(f"keep total {keep:.2f}s")

    for mode in ("copy", "encode"):
        out = os.path.join(d, f"out_{mode}.mp4")
        msg = exporter.export(src, info, segs, out, mode, {"quality": 28, "use_gpu": True})
        o = fftools.probe(out)
        log(f"export {mode}: {o['vcodec']} {o['duration']:.2f}s audio={o['has_audio']} | {msg.splitlines()[0]}")
        want = keep
        if mode == "copy":
            sn, _ = exporter.snap_keeps(analyzer.keep_ranges(segs, info["duration"]), fftools.keyframes(src))
            want = sum(b - a_ for a_, b in sn)
        if o["vcodec"] != "hevc" or abs(o["duration"] - want) > 0.4 or not o["has_audio"]:
            log(f"FAIL: export {mode}")
            ok = False
    # H.264 원본 → 자동 모드는 H.265 인코딩
    src2 = os.path.join(d, "h264.mp4")
    make_sample(src2, "libx264")
    info2 = fftools.probe(src2)
    out2 = os.path.join(d, "out_auto264.mp4")
    exporter.export(src2, info2, segs, out2, "auto", {"quality": 30})
    o2 = fftools.probe(out2)
    log(f"export auto(h264): {o2['vcodec']} {o2['duration']:.2f}s")
    if o2["vcodec"] != "hevc":
        ok = False
    log(f"encoder: {exporter.pick_encoder()}")
    log("selftest ok" if ok else "selftest FAIL")
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if run() else 1)
