"""화면 구성 정리(자동 크롭)
- 살림: 메인 영상 + 블러 배경(움직이든 안 움직이든, 위에 글자가 있어도)
- 자름: 바탕이 한 가지 색인 상태바·내비바·텍스트바, 안 움직이는 단색 영역
- 결과: 원본 해상도 그대로, 남긴 부분은 크기 그대로 가운데, 나머지는 검은색"""
import json
import os

import numpy as np

import analyzer
import fftools

GW, GH = analyzer.W, analyzer.H     # 분석 격자 320x180
ACT = 0.01          # 줄·칸이 '움직인다'로 볼 비율 (내용이 시작되는 곳 찾기)
STILL = 0.03        # 이 비율 미만만 움직이면 '가만히 있는' 화면 요소로 봄 (녹화 잡음 허용)
FLAT = 0.80         # 바탕이 한 가지 색으로 볼 비율
MIN_RUN = 2         # 단색 줄이 이만큼 이어져야 화면 요소로 봄
SMOOTH = 0          # 앞뒤 몇 묶음(2초)을 합쳐 판단 (넓으면 구성이 바뀌는 곳이 묻힘)
MIN_SEG = 2         # 구성 구간 최소 길이(묶음 수)


def _motion_span(prof):
    idx = np.where(prof > ACT)[0]
    if len(idx) == 0:
        return None
    return int(idx[0]), int(idx[-1]) + 1


def _expand(lo, hi, flat, act, n):
    """움직이는 범위 [lo,hi) 에서 바깥으로 넓혀 감: 단색·정지 줄이 MIN_RUN 이상 이어지는 곳에서 멈춤"""
    need = max(MIN_RUN, int(round(n * 0.025)))      # 채팅 줄 사이 틈 같은 얇은 단색은 무시

    def walk(start, step, stop):
        i = start
        run = 0
        cut = stop
        while i != stop:
            ui = flat[i] >= FLAT and act[i] <= STILL
            if ui:
                run += 1
                if run >= need or (i + step == stop and run >= MIN_RUN):
                    cut = i - step * (run - 1)
                    return cut
            else:
                run = 0
            i += step
        return stop
    top = walk(lo - 1, -1, -1) + 1      # 위쪽(또는 왼쪽) 첫 화면 요소 줄 바로 아래
    bot = walk(hi, 1, n)                # 아래쪽 첫 화면 요소 줄
    return max(0, top), min(n, bot)


def _snap(pos, edge, n, lo_lim, hi_lim):
    """경계를 가장 강한 선(±2칸)에 맞춤. edge[i] 는 i 와 i+1 사이"""
    best, bv = pos, 0.0
    for c in range(max(1, pos - 2), min(n - 1, pos + 2) + 1):
        if not (lo_lim <= c <= hi_lim):
            continue
        v = edge[c - 1] if 0 <= c - 1 < len(edge) else 0
        if v > bv + 0.05:
            best, bv = c, v
    return best


def _window_rect(lay, w0, w1, h0=None, h1=None):
    """w0~w1 묶음의 영역. 움직임 여부는 h0~h1(더 넓은 범위)까지 봄 — 잠깐 멈춘 채팅 등을 지키려고"""
    h0 = w0 if h0 is None else h0
    h1 = w1 if h1 is None else h1
    def agg(arr, a0, a1):
        x = arr[a0:a1]
        # 여러 묶음이면 가끔 튀는 잡음을 빼고 (90% 지점), 하나면 그대로
        return (np.percentile(x, 90, axis=0) if len(x) > 3 else x.max(axis=0)) / 255.0
    ra = agg(lay["ra"], w0, w1)
    ca = agg(lay["ca"], w0, w1)
    rah = agg(lay["ra"], h0, h1)
    cah = agg(lay["ca"], h0, h1)
    rf = np.median(lay["rf"][w0:w1], axis=0) / 255.0
    cf = np.median(lay["cf"][w0:w1], axis=0) / 255.0
    re = np.median(lay["re"][w0:w1], axis=0) / 255.0
    ce = np.median(lay["ce"][w0:w1], axis=0) / 255.0
    ys, xs = _motion_span(ra), _motion_span(ca)
    if ys is None or xs is None:
        return None
    y0, y1 = _expand(ys[0], ys[1], rf, rah, GH)
    x0, x1 = _expand(xs[0], xs[1], cf, cah, GW)
    y0 = _snap(y0, re, GH, 0, ys[0])
    y1 = _snap(y1, re, GH, ys[1], GH)
    x0 = _snap(x0, ce, GW, 0, xs[0])
    x1 = _snap(x1, ce, GW, xs[1], GW)
    if (x1 - x0) < GW * 0.2 or (y1 - y0) < GH * 0.2:
        return None          # 너무 작으면 믿지 않음
    return (x0, y0, x1, y1)


def _iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua else 0.0


def detect(a, info):
    """→ {"W","H","segs":[{start,end,rect:[x,y,w,h]}]} (원본 화면 기준 화소) 또는 None(자를 것 없음)"""
    lay = a.lay
    if not lay or "rf" not in lay:
        return None
    nw = len(lay["ra"])
    W = int(info.get("disp_w") or info["width"])
    H = int(info.get("disp_h") or info["height"])
    full = (0, 0, GW, GH)
    rects = []
    prev = full
    for w in range(nw):
        r = _window_rect(lay, max(0, w - SMOOTH), min(nw, w + SMOOTH + 1))
        if r is None:
            r = prev                     # 정지·검은 화면 등은 앞 구성 그대로
        rects.append(r)
        prev = r
    # 비슷한 구성끼리 묶기
    segs = []
    for w, r in enumerate(rects):
        if segs and _iou(segs[-1]["r"], r) > 0.9:
            segs[-1]["w1"] = w + 1
            segs[-1]["all"].append(r)
        else:
            segs.append({"w0": w, "w1": w + 1, "r": r, "all": [r]})
    # 너무 짧은 구간은 앞 구간에 합침
    merged = []
    for sg in segs:
        if merged and sg["w1"] - sg["w0"] < MIN_SEG:
            merged[-1]["w1"] = sg["w1"]
            merged[-1]["all"] += sg["all"]
        else:
            merged.append(sg)
    if merged and len(merged) > 1 and merged[0]["w1"] - merged[0]["w0"] < MIN_SEG:
        merged[1]["w0"] = 0
        merged[1]["all"] = merged[0]["all"] + merged[1]["all"]
        merged.pop(0)
    # 2차: 구간 전체를 한꺼번에 보고 다시 계산 (구간 안에서 한 번이라도 움직인 곳은 살림)
    for sg in merged:
        a0, a1 = sg["w0"], sg["w1"]
        if a1 - a0 > 2:              # 구성이 바뀌는 순간이 섞인 맨 앞·뒤 묶음은 빼고
            a0, a1 = a0 + 1, a1 - 1
        r = _window_rect(lay, a0, a1)
        if r is not None:
            sg["all"] = [r]
    out = []
    dur = info["duration"]
    sx, sy = W / GW, H / GH
    for i, sg in enumerate(merged):
        arr = np.array(sg["all"])
        x0, y0, x1, y1 = np.median(arr, axis=0)
        rx, ry = int(round(x0 * sx)) // 2 * 2, int(round(y0 * sy)) // 2 * 2
        rw = (int(round(x1 * sx)) - rx) // 2 * 2
        rh = (int(round(y1 * sy)) - ry) // 2 * 2
        rw, rh = min(rw, W - rx), min(rh, H - ry)
        st = 0.0 if i == 0 else _switch_time(a, sg["w0"])
        en = dur if i == len(merged) - 1 else _switch_time(a, sg["w1"])
        out.append({"start": round(st, 2), "end": round(en, 2), "rect": [rx, ry, rw, rh], "on": True})
    # 이어지는 같은 영역 합치기
    final = []
    for sg in out:
        if final and final[-1]["rect"] == sg["rect"]:
            final[-1]["end"] = sg["end"]
        else:
            final.append(sg)
    plan = {"W": W, "H": H, "segs": final}
    if all(_is_full(sg["rect"], W, H) for sg in final):
        plan["segs"] = []
    return plan


def _switch_time(a, w):
    """묶음 경계 앞뒤 2초 안에서 화면이 가장 크게 바뀐 프레임 = 구성이 바뀐 때"""
    fps = a.fps
    c = int(round(w * analyzer.LAY_WIN * fps))
    r = int(round(analyzer.LAY_WIN * fps))
    lo, hi = max(1, c - r), min(len(a.chg), c + r)
    if hi <= lo:
        return round(w * analyzer.LAY_WIN, 2)
    k = lo + int(np.argmax(a.chg[lo:hi]))
    return round(k / fps, 2)


def _is_full(rect, W, H, tol=0.01):
    x, y, w, h = rect
    return x <= W * tol and y <= H * tol and w >= W * (1 - 2 * tol) and h >= H * (1 - 2 * tol)


def refine(path, info, plan, job=None):
    """분석 격자(약 6~13화소 단위)로 찾은 경계를 원본 화소로 다듬음: 구간마다 3장을 보고
    경계 근처에서 단색 줄이 시작되는 정확한 위치를 찾음"""
    if not plan or not plan["segs"]:
        return plan
    W, H = plan["W"], plan["H"]
    for sg in plan["segs"]:
        if job and job.cancelled:
            break
        if _is_full(sg["rect"], W, H):
            continue
        times = [sg["start"] + (sg["end"] - sg["start"]) * f for f in (0.25, 0.5, 0.75)]
        imgs = []
        for t in times:
            im = fftools.frame_at(path, t, W)
            if im is not None:
                g = np.asarray(im.convert("L"), dtype=np.int16)
                if g.shape == (H, W):
                    imgs.append(g)
        if not imgs:
            continue
        stack = np.stack(imgs)
        x, y, w, h = sg["rect"]
        mx, my = max(4, W // GW * 3), max(4, H // GH * 3)

        def flat_rows(r0, r1, c0, c1):
            r0, r1 = max(0, r0), min(H, r1)
            seg = stack[:, r0:r1, c0:c1]
            med = np.median(seg, axis=2, keepdims=True)
            flat = (np.abs(seg - med) <= 6).mean(axis=2).min(axis=0)     # 세 장 모두 단색이어야
            still = (np.abs(seg - seg[:1]) <= 10).mean(axis=2).min(axis=0)
            return r0, (flat >= FLAT) & (still >= 0.95)

        def flat_cols(c0, c1, r0, r1):
            c0, c1 = max(0, c0), min(W, c1)
            seg = stack[:, r0:r1, c0:c1]
            med = np.median(seg, axis=1, keepdims=True)
            flat = (np.abs(seg - med) <= 6).mean(axis=1).min(axis=0)
            still = (np.abs(seg - seg[:1]) <= 10).mean(axis=1).min(axis=0)
            return c0, (flat >= FLAT) & (still >= 0.95)

        # 위쪽: 경계 근처에서 위로 가며 마지막 '내용' 줄 바로 위까지
        if y > 0:
            r0, m = flat_rows(y - my, y + my, x, x + w)
            nonflat = np.where(~m)[0]
            inside = nonflat[nonflat + r0 >= y - my]
            if len(inside):
                # 경계 아래쪽 범위에서 처음 내용이 시작되는 줄
                first = r0 + int(inside[0])
                y = max(0, min(first, y + my))
        if y + h < H:
            yb = y + h
            r0, m = flat_rows(yb - my, yb + my, x, x + w)
            nonflat = np.where(~m)[0]
            if len(nonflat):
                yb = min(H, r0 + int(nonflat[-1]) + 1)
            h = yb - y
        if x > 0:
            c0, m = flat_cols(x - mx, x + mx, y, y + h)
            nonflat = np.where(~m)[0]
            if len(nonflat):
                x = max(0, c0 + int(nonflat[0]))
        if x + w < W:
            xb = sg["rect"][0] + sg["rect"][2]
            c0, m = flat_cols(xb - mx, xb + mx, y, y + h)
            nonflat = np.where(~m)[0]
            if len(nonflat):
                xb = min(W, c0 + int(nonflat[-1]) + 1)
            w = xb - x
        x, y = x // 2 * 2, y // 2 * 2
        w, h = max(2, min(W - x, w)) // 2 * 2, max(2, min(H - y, h)) // 2 * 2
        sg["rect"] = [x, y, w, h]
    return plan


def plan_file(path, fps):
    return analyzer.cache_path(path, fps)[:-4] + ".crop.json"


def load(path, fps):
    try:
        with open(plan_file(path, fps), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save(path, fps, plan):
    try:
        with open(plan_file(path, fps), "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False)
    except Exception:
        pass


def get(path, info, a, job=None):
    """저장된 계획이 있으면 그것, 없으면 찾아서 저장"""
    p = load(path, int(a.fps))
    if p is not None:
        return p
    p = detect(a, info)
    if p and p["segs"]:
        p = refine(path, info, p, job)
    if job is not None and job.cancelled:
        return p
    save(path, int(a.fps), p or {"W": info.get("disp_w"), "H": info.get("disp_h"), "segs": []})
    return p


def active_segs(plan, enabled=True):
    """실제로 적용할 크롭 구간 [(start, end, rect)]"""
    if not enabled or not plan:
        return []
    W, H = plan["W"], plan["H"]
    return [(sg["start"], sg["end"], sg["rect"]) for sg in plan.get("segs", [])
            if sg.get("on", True) and not _is_full(sg["rect"], W, H)]


def rect_at(plan, t, enabled=True):
    for st, en, r in active_segs(plan, enabled):
        if st <= t < en:
            return r
    return None


def vf_for(rect, W, H):
    """잘라서 원래 크기 그대로 가운데, 나머지 검은색"""
    x, y, w, h = rect
    return f"crop={w}:{h}:{x}:{y},pad={W}:{H}:{(W - w) // 2 // 2 * 2}:{(H - h) // 2 // 2 * 2}:black"


# ── 자체 점검: 10~25초에만 상태바·내비바·회색 화면 요소가 보이는 영상 ──
def make_sample(path):
    graph = ("[0:v]split=3[full][small][still];"
             "[still]trim=start_frame=0:end_frame=1,loop=loop=-1:size=1,setpts=N/30/TB,scale=540:270,boxblur=20:2,"
             "drawbox=x=30:y=230:w=200:h=8:c=white:t=fill,drawbox=x=30:y=245:w=150:h=6:c=white:t=fill[blur];"
             "[small]scale=400:225[sv];[1:v][blur]overlay=60:30[b1];[b1][sv]overlay=130:52[b2];"
             "[b2]drawbox=x=0:y=0:w=640:h=18:c=0x202020:t=fill,drawbox=x=560:y=5:w=30:h=8:c=white:t=fill,"
             "drawbox=x=0:y=0:w=40:h=360:c=black:t=fill,drawbox=x=14:y=150:w=12:h=12:c=white:t=fill,"
             "drawbox=x=100:y=320:w=300:h=10:c=0x555555:t=fill[ui];"
             "[full][ui]overlay=enable='between(t,10,25)'[v]")
    rc, _, err = fftools.run([fftools.FFMPEG, "-v", "error", "-y", "-nostdin",
                              "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=40",
                              "-f", "lavfi", "-i", "color=c=0x9a9a9a:s=640x360:r=30:d=40",
                              "-f", "lavfi", "-i", "sine=d=40", "-filter_complex", graph,
                              "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-preset", "ultrafast", "-g", "30",
                              "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", path], timeout=180)
    if rc != 0:
        raise RuntimeError(err[-300:])


def make_phone_sample(path):
    """폰 가로 녹화 흉내: 위 검은 상태바, 왼쪽 흰 내비바, 오른쪽 검은 여백, 오른쪽 일부 블러"""
    graph = ("[0:v]split[a][b];[b]crop=300:540:840:0,boxblur=25:2[blr];[a][blr]overlay=840:0,"
             "drawbox=x=0:y=0:w=1140:h=30:c=black:t=fill,drawbox=x=60:y=8:w=50:h=14:c=white:t=fill,"
             "drawbox=x=1050:y=8:w=40:h=14:c=white:t=fill,drawbox=x=0:y=0:w=63:h=540:c=0xf0f2f5:t=fill,"
             "drawbox=x=25:y=120:w=14:h=20:c=0x777777:t=fill,drawbox=x=25:y=260:w=14:h=14:c=0x777777:t=fill,"
             "drawbox=x=1085:y=30:w=55:h=510:c=black:t=fill[v]")
    rc, _, err = fftools.run([fftools.FFMPEG, "-v", "error", "-y", "-nostdin",
                              "-f", "lavfi", "-i", "testsrc2=s=1140x540:r=30:d=20",
                              "-filter_complex", graph, "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast",
                              "-crf", "30", "-g", "60", "-pix_fmt", "yuv420p", path], timeout=180)
    if rc != 0:
        raise RuntimeError(err[-300:])


def selftest(log=print):
    import tempfile
    import exporter
    d = tempfile.mkdtemp(prefix="videocut_crop_")
    src = os.path.join(d, "crop_test.mp4")
    make_sample(src)
    info = fftools.probe(src)
    a = analyzer.analyze(src, info, analyzer.DEFAULTS, use_cache=False)
    plan = refine(src, info, detect(a, info))
    act = active_segs(plan)
    log(f"crop plan: {act}")
    ok = len(act) == 1 and abs(act[0][0] - 10) < 0.6 and abs(act[0][1] - 25) < 0.6 and \
        all(abs(u - v) <= 4 for u, v in zip(act[0][2], [60, 30, 540, 270]))
    out = os.path.join(d, "crop_out.mp4")
    exporter.export(src, info, [], out, "auto", {"quality": 30}, crop={"W": plan["W"], "H": plan["H"], "segs": act})
    o = fftools.probe(out)
    im = fftools.frame_at(out, 15.0, 640)
    g = np.asarray(im.convert("L"), dtype=np.int16) if im is not None else None
    black_edge = g is not None and g[:10].mean() < 20 and g[:, :20].mean() < 20
    center = g is not None and g[150:210, 280:360].mean() > 40
    log(f"crop out: {o['width']}x{o['height']} {o['vcodec']} black_edge={black_edge} center={center}")
    ok = ok and o["width"] == 640 and o["height"] == 360 and black_edge and center
    # 폰 가로 녹화 (녹화 잡음이 있는 상태바·내비바)
    src2 = os.path.join(d, "phone.mp4")
    make_phone_sample(src2)
    info2 = fftools.probe(src2)
    a2 = analyzer.analyze(src2, info2, analyzer.DEFAULTS, use_cache=False)
    act2 = active_segs(refine(src2, info2, detect(a2, info2)))
    log(f"phone crop: {act2}")
    ok2 = len(act2) == 1 and all(abs(u - v) <= 4 for u, v in zip(act2[0][2], [63, 30, 1022, 510]))
    ok = ok and ok2
    log("crop ok" if ok else "crop FAIL")
    return ok
