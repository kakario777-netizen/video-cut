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
LAYOUT_VERSION = 8                  # 크롭 계산 방식이 바뀌면 올림 → 저장된 자동 결과를 다시 계산
ACT = 0.01          # 줄·칸이 '움직인다'로 볼 비율 (내용이 시작되는 곳 찾기)
STILL = 0.03        # 이 비율 미만만 움직이면 '가만히 있는' 화면 요소로 봄 (녹화 잡음 허용)
ROW_STILL = 0.12    # 위·아래 가로 띠(상태바·내비바)는 깜빡이는 작은 표시(녹화 중 알약 등)까지 허용
BAR_MAX = 0.25      # 위·아래(왼·오른) 띠로 볼 최대 두께 (화면의 이 비율까지)
FLAT = 0.80         # 바탕이 한 가지 색으로 볼 비율
MIN_RUN = 2         # 단색 줄이 이만큼 이어져야 화면 요소로 봄
SMOOTH = 0          # 앞뒤 몇 묶음(2초)을 합쳐 판단 (넓으면 구성이 바뀌는 곳이 묻힘)
MIN_SEG = 2         # 구성 구간 최소 길이(묶음 수)


def _motion_span(prof, thr=ACT):
    idx = np.where(prof > thr)[0]
    if len(idx) == 0 and thr > ACT:
        idx = np.where(prof > ACT)[0]
    if len(idx) == 0:
        return None
    return int(idx[0]), int(idx[-1]) + 1


def _expand(lo, hi, flat, act, n, still=STILL):
    """움직이는 범위 [lo,hi) 에서 바깥으로 넓혀 감: 단색·정지 줄이 MIN_RUN 이상 이어지는 곳에서 멈춤"""
    need = max(MIN_RUN, int(round(n * 0.025)))      # 채팅 줄 사이 틈 같은 얇은 단색은 무시

    def walk(start, step, stop):
        i = start
        run = 0
        cut = stop
        while i != stop:
            ui = flat[i] >= FLAT and act[i] <= still
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


EDGE = 22           # 이웃 화소 밝기 차가 이 이상이면 경계
LINE = 0.5          # 곧은 경계선으로 볼 세기 (그 줄 폭의 절반 이상에서 밝기가 확 바뀜)
WEAK_LINE = 0.2     # 원본 화소로 다듬을 때: 흐린 경계(어두운 탭바와 어두운 영상)도 이 정도면 맞춤
UI_FLAT = 0.4       # 메뉴바 띠의 줄들은 바탕이 이 이상 한 가지 색 (블러·영상 장면은 훨씬 낮음)


def _bar_cut(lo, hi, flat, act, edge, n, still=STILL, relax=False):
    """가장자리부터 곧은 선까지 통째로 '가만히 있고 블러·장면이 없는' 띠면 그 선에서 자름.
    위(왼쪽): [0, L) 이 띠, 아래(오른쪽): [L, n) 이 띠. 못 찾으면 None
    움직임으로 찾은 내용 범위(lo, hi)와 상관없이 가장자리 띠(BAR_MAX 이내)도 살펴봄
    (상태바의 깜빡이는 표시 때문에 내용 범위가 0부터 잡히는 경우)"""
    still_row = act <= still
    strict = (flat >= UI_FLAT) & still_row
    # 큰 덩어리(주소창 상자, 목표 막대 등)가 있는 줄: 바로 옆에 완전 단색 줄이 있으면 메뉴 띠로 봄
    pure = flat >= 0.95
    near = np.zeros(n, bool)
    for dk in range(-4, 5):
        near |= np.roll(pure, dk)
    ui_row = (strict | ((flat >= 0.25) & near & still_row)) if relax else strict
    lo = max(lo, int(n * BAR_MAX))
    hi = min(hi, n - int(n * BAR_MAX))
    top = None
    ok_prefix = np.cumsum(~ui_row)          # 0..i 까지 띠가 아닌 줄 수
    for L in range(min(lo + 1, n - 1), 0, -1):
        if ok_prefix[L - 1] != 0:
            continue
        if edge[L - 1] >= LINE:
            top = L
            break
        if L < n - 1 and edge[L] >= LINE:      # 경계가 L 줄 가운데를 지남 → 그 줄까지 자름
            top = L + 1
            break
    # 뚜렷한 선 아래로도 메뉴 띠가 이어지면(어두운 탭바와 어두운 영상처럼 경계가 흐린 경우) 띠 끝까지
    ext = strict                 # 흐린 경계 너머로 이어 붙일 줄 (위·아래 띠에만 씀 — 옆 블러는 영향 없음)
    if top is not None and relax:
        k = top
        while k < min(lo + 1, n) and ext[k]:
            k += 1
        top = k
    bot = None
    ok_suffix = np.cumsum((~ui_row)[::-1])[::-1]   # i..n-1 까지 띠가 아닌 줄 수
    for L in range(max(hi - 1, 1), n):
        if ok_suffix[L] != 0:
            continue
        if edge[L - 1] >= LINE:
            bot = L
            break
        if L >= 2 and edge[L - 2] >= LINE:
            bot = L - 1
            break
    if bot is not None and relax:
        k = bot
        while k - 1 > max(hi - 1, 0) and ext[k - 1]:
            k -= 1
        bot = k
    return top, bot


TOP_MAX = 0.07      # 맨 위 얇은 띠(상태바) 최대 두께
BOT_MAX = 0.10      # 맨 아래 얇은 띠(내비바) 최대 두께


def _thin_bars(flat, act, edge, n):
    """맨 위·아래 얇은 띠(상태바·내비바): 가장자리부터 이어지는 '가만히 있는 단색 띠'를 찾고,
    그 안에서 가장자리에 가장 가까운 곧은 선에서 자름. → (위에서 자를 줄, 아래에서 자를 줄) 또는 None"""
    ui = (flat >= UI_FLAT) & (act <= ROW_STILL)
    top = None
    k = 0
    while k < n and ui[k] and k < int(n * TOP_MAX) + 1:
        k += 1
    if k >= 2:
        for L in range(2, min(k + 2, n)):          # 가장자리에서 가까운 첫 뚜렷한 선 = 상태바 아래 경계
            if edge[L - 1] >= LINE and all(ui[:L - 1]):
                top = L
                break
        if top is None and k <= int(n * 0.04) and k < n and not ui[k]:
            top = k                                 # 선이 흐려도 아주 얇은 단색 띠면 그 아래에서
    bot = None
    k = 0
    while k < n and ui[n - 1 - k] and k < int(n * BOT_MAX) + 1:
        k += 1
    if k >= 2:
        for L in range(n - 2, max(n - k - 2, 0), -1):
            if edge[L - 1] >= LINE and all(ui[L + 1:]):
                bot = L
                break
        if bot is None and k <= int(n * 0.05) and not ui[n - 1 - k]:
            bot = n - k
    return top, bot


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
    ys, xs = _motion_span(ra, ROW_STILL), _motion_span(ca)
    if ys is None or xs is None:
        return None
    y0, y1 = _expand(ys[0], ys[1], rf, rah, GH, ROW_STILL)
    x0, x1 = _expand(xs[0], xs[1], cf, cah, GW)
    y0 = _snap(y0, re, GH, 0, ys[0])
    y1 = _snap(y1, re, GH, ys[1], GH)
    x0 = _snap(x0, ce, GW, 0, xs[0])
    x1 = _snap(x1, ce, GW, xs[1], GW)
    # 메뉴바·탭바처럼 글자·아이콘이 많은 띠: 곧은 선까지 통째로 가만히 있고 블러가 없으면 그 선에서
    bt, bb = _bar_cut(ys[0], ys[1], rf, rah, re, GH, ROW_STILL, relax=True)
    if bt is not None:
        y0 = max(y0, bt)
    if bb is not None:
        y1 = min(y1, bb)
    tt, tb = _thin_bars(rf, rah, re, GH)
    if tt is not None:
        y0 = max(y0, tt)
    if tb is not None:
        y1 = min(y1, tb)
    bl, br = _bar_cut(xs[0], xs[1], cf, cah, ce, GW)
    if bl is not None:
        x0 = max(x0, bl)
    if br is not None:
        x1 = min(x1, br)
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
    if H > W * 1.6:                  # 길쭉한 폰 세로 화면만 (폴드처럼 네모에 가까운 화면은 일반 방식)
        return _detect_portrait(a, info, W, H)
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
        rx, ry = _even_up(x0 * sx), _even_up(y0 * sy)
        rw = _even_down(x1 * sx) - rx
        rh = _even_down(y1 * sy) - ry
        rw, rh = min(rw, W - rx), min(rh, H - ry)
        st = 0.0 if i == 0 else _switch_time(a, sg["w0"])
        en = dur if i == len(merged) - 1 else _switch_time(a, sg["w1"])
        if rw * 1.6 < H:
            # 양옆을 잘랐더니 세로 모양(가로 녹화 속 세로 화면) → 세로 규칙: 위아래는 상태바·내비바만 (나중에 확인)
            ry, rh = 0, H // 2 * 2
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


def _detect_portrait(a, info, W, H):
    """세로 녹화: 맨 위 상태바·맨 아래 내비바만 자름. 채팅 등 나머지는 절대 건드리지 않음.
    영상 전체를 한꺼번에 보고 한 가지로 정함"""
    lay = a.lay
    ra = (np.percentile(lay["ra"], 90, axis=0) if len(lay["ra"]) > 3 else lay["ra"].max(axis=0)) / 255.0
    rf = np.median(lay["rf"], axis=0) / 255.0
    re = np.median(lay["re"], axis=0) / 255.0
    top, bot = _thin_bars(rf, ra, re, GH)
    y0 = top or 0
    y1 = bot or GH
    sy = H / GH
    ry = _even_up(y0 * sy)
    ye = _even_down(y1 * sy)
    seg = {"start": 0.0, "end": info["duration"], "rect": [0, ry, W // 2 * 2, ye - ry], "on": True,
           "portrait": True}
    plan = {"W": W, "H": H, "segs": [seg]}
    if _is_full(seg["rect"], W, H, tol=0.003):
        plan["segs"] = []
    return plan


def _even_up(v):
    return int(np.ceil(v / 2.0)) * 2


def _even_down(v):
    return int(np.floor(v / 2.0)) * 2


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
    """분석 격자(약 3~13화소 단위)로 찾은 경계를 원본 화소로 다듬음: 구간마다 3장을 보고
    경계 근처(±3칸)에서 가장 뚜렷한 곧은 선에 맞춤. 뚜렷한 선이 없으면 그대로 둠"""
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
        x1, y1 = x + w, y + h
        mx, my = max(4, int(W / GW * 3)), max(4, int(H / GH * 3))

        def hline(pos, c0, c1):
            """pos 근처에서 가장 뚜렷한 가로선 (pos = 선 아래 첫 줄)"""
            lo, hi = max(1, pos - my), min(H - 1, pos + my)
            if hi <= lo or c1 - c0 < 8:
                return pos
            d = np.abs(stack[:, lo:hi + 1, c0:c1] - stack[:, lo - 1:hi, c0:c1]) > EDGE
            score = d.mean(axis=2).mean(axis=0)
            k = int(np.argmax(score))
            return lo + k if score[k] >= WEAK_LINE else pos

        def vline(pos, r0, r1):
            lo, hi = max(1, pos - mx), min(W - 1, pos + mx)
            if hi <= lo or r1 - r0 < 8:
                return pos
            d = np.abs(stack[:, r0:r1, lo:hi + 1] - stack[:, r0:r1, lo - 1:hi]) > EDGE
            score = d.mean(axis=1).mean(axis=0)
            k = int(np.argmax(score))
            return lo + k if score[k] >= WEAK_LINE else pos

        if y > 0:
            y = hline(y, x, x1)
        if y1 < H:
            y1 = hline(y1, x, x1)
        if x > 0:
            x = vline(x, y, y1)
        if x1 < W:
            x1 = vline(x1, y, y1)
        x, y = _even_up(x), _even_up(y)          # 짝수 맞춤은 안쪽으로 (가장자리 한 줄 남지 않게)
        x1, y1 = _even_down(min(W, x1)), _even_down(min(H, y1))
        w, h = max(2, x1 - x), max(2, y1 - y)
        sg["rect"] = [x, y, w, h]
    return plan


def _frames(path, t0, t1, W, H, k):
    """구간 [t0, t1] 에서 고르게 k 장 (원본 해상도, 흑백)"""
    out = []
    span = max(0.0, t1 - t0)
    for i in range(k):
        t = t0 + span * (0.06 + 0.88 * (i + 0.5) / k)
        im = fftools.frame_at(path, t, W)
        if im is not None:
            g = np.asarray(im.convert("L"), dtype=np.int16)
            if g.shape == (H, W):
                out.append(g)
    return np.stack(out) if len(out) >= 3 else None


def trim_bars(path, plan, job=None):
    """원본 장면 여러 장으로 위·아래 화면 요소를 확인해 더 자름 (남길 영역의 폭 안에서만 봄).
    - 화면 요소(상태바·메뉴·탭바·버튼줄·목표 막대·설명 글): 장면이 바뀌어도 픽셀이 거의 '완전히' 같음
    - 영상: 가만히 있는 벽·천장도 카메라 노이즈로 조금씩 흔들림 → 구분됨
    - 블러: 영상을 따라 바뀌거나, 가만히 있어도 글자·선 없이 부드러움 → 지켜짐
    세로 모양 영역은 맨 위 상태바·맨 아래 내비바만 (채팅 유지), 그 밖은 영상 덩어리만 남김"""
    if not plan or not plan.get("segs"):
        return plan
    W, H = plan["W"], plan["H"]
    for sg in plan["segs"]:
        if job and job.cancelled:
            break
        if sg.get("manual"):
            continue
        x, y, w, h = sg["rect"]
        st = _frames(path, sg["start"], sg["end"], W, H, 6 if sg["end"] - sg["start"] < 600 else 8)
        if st is None:
            sg["dbg"] = "장면 못 뽑음"
            continue
        sub = st[:, :, x:x + w]
        rng = sub.max(axis=0) - sub.min(axis=0)
        exact = (rng <= 4).mean(axis=1)                       # 장면이 바뀌어도 거의 그대로인 비율
        f0 = sub[len(sub) // 2]
        med = np.median(f0, axis=1)
        flat = (np.abs(f0 - med[:, None]) <= 8).mean(axis=1)
        detail = np.zeros(H)
        detail[:] = (np.abs(f0[:, 1:] - f0[:, :-1]) > EDGE).mean(axis=1)    # 글자·아이콘 등 또렷한 무늬
        pure = flat >= 0.95
        near = pure.copy()
        for dk in range(1, 5):
            near[dk:] |= pure[:-dk]
            near[:-dk] |= pure[dk:]
        ui = (exact >= 0.6) & (pure | ((flat >= 0.2) & (near | (detail >= 0.01))))
        edge = np.zeros(H)
        edge[1:] = (np.abs(f0[1:] - f0[:-1]) > EDGE).mean(axis=1)           # edge[r] = r-1 과 r 사이
        jump = np.zeros(H)
        jump[1:] = np.abs(med[1:] - med[:-1])                              # 줄 바탕색이 바뀌는 곳
        line = (edge >= LINE) | (jump >= 8)
        portrait = h > w * 1.6
        top, bot = 0, H
        if portrait:
            run = 0
            while run < H and ui[run]:
                run += 1
            cap = int(H * TOP_MAX)
            c = [r for r in range(2, min(run + 2, cap + 1, H)) if line[r] and ui[:r].all()]
            top = c[0] if c else (run if 2 <= run <= int(H * 0.045) else 0)
            run = 0
            while run < H and ui[H - 1 - run]:
                run += 1
            cap = int(H * BOT_MAX)
            c = [r for r in range(H - 2, max(H - run - 2, H - cap - 1, 0), -1) if line[r] and ui[r:].all()]
            bot = c[0] if c else (H - run if 2 <= run <= int(H * 0.06) else H)
        else:
            # 남길 영역의 위·아래 가장자리부터 이어지는 화면 요소 줄 (프레임 끝에서 바뀌는 채팅이 있어도 됨)
            score = np.maximum(edge, np.minimum(1.0, jump / 16.0))
            gap = max(2, H // 200)

            def _run(start, step, limit):
                r, last = start, start - step
                while 0 <= r < H and abs(r - start) < limit:
                    if ui[r]:
                        last = r
                    elif abs(r - last) > gap:
                        break
                    r += step
                return abs(last - start) + 1 if last != start - step else 0

            lim = int(h * 0.35)
            rt = _run(y, 1, lim)
            top = y
            if rt >= 2:
                top = y + rt
                lo_, hi_ = max(1, top - 4), min(H - 1, top + 8)
                k = lo_ + int(np.argmax(score[lo_:hi_ + 1]))
                if score[k] >= WEAK_LINE:
                    top = k
            rb = _run(y + h - 1, -1, lim)
            # 아래쪽 중간의 두꺼운 화면 요소 띠(하트·목표 막대·설명 글) — 그 아래 채팅이 바뀌어도 띠부터 자름
            if rb < 2:
                solid = ui & (exact >= 0.6)
                g2 = max(8, H // 80)                     # 글자 줄 사이·자라는 막대 등 짧은 틈은 이어 봄
                r = y + h - 1
                lo_lim = y + max(int(h * 0.5), top - y + int(H * 0.2))
                while r > lo_lim:
                    if solid[r]:
                        e = r
                        k = r
                        while k > lo_lim and (solid[k] or (k > lo_lim + g2 and solid[k - g2:k].any())):
                            k -= 1
                        if e - k >= H * 0.04:
                            rb = y + h - (k + 1)
                            break
                        r = k
                    r -= 1
            bot = y + h
            if rb >= 2:
                bot = y + h - rb
                lo_, hi_ = max(1, bot - 12), min(H - 1, bot + 4)  # 압축 블록 번짐으로 띠가 조금 늦게 시작할 수 있음
                strong = [r for r in range(lo_, hi_ + 1) if score[r] >= LINE]
                k = strong[0] if strong else lo_ + int(np.argmax(score[lo_:hi_ + 1]))
                if score[k] >= WEAK_LINE:
                    bot = k
        ny0 = max(y, _even_up(top))
        ny1 = min(y + h, _even_down(bot))
        sg["dbg"] = f"장면 {len(st)}장, {'세로' if portrait else '일반'}, 위 {top} 아래 {bot}"
        if ny1 - ny0 >= H * 0.2 and (ny0, ny1) != (y, y + h):
            sg["rect"] = [x, ny0, w, ny1 - ny0]
    merged = []
    for sg in plan["segs"]:
        if merged and merged[-1]["rect"] == sg["rect"] and abs(merged[-1]["end"] - sg["start"]) < 0.5:
            merged[-1]["end"] = sg["end"]
        else:
            merged.append(sg)
    plan["segs"] = [sg for sg in merged if not _is_full(sg["rect"], W, H)]
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
        manual = any(sg.get("manual") for sg in p.get("segs", [])) or p.get("edited")
        if manual or p.get("lv") == LAYOUT_VERSION:
            return p                 # 직접 지정한 크롭은 그대로
    p = compute(path, info, a, job)
    if job is not None and job.cancelled:
        return p
    p = p or {"W": info.get("disp_w"), "H": info.get("disp_h"), "segs": []}
    p["lv"] = LAYOUT_VERSION
    save(path, int(a.fps), p)
    return p


def compute(path, info, a, job=None):
    """자동 크롭 계산 전체: 분석 기록으로 대략 → 원본 화소로 경계 맞춤 → 원본 장면으로 위·아래 띠 확인"""
    p = detect(a, info)
    if p and p["segs"]:
        p = refine(path, info, p, job)
    if p is not None and not p["segs"]:
        # 자동 판단으로 자를 게 없어도 위·아래 메뉴 띠는 원본 장면으로 한 번 더 확인
        p["segs"] = [{"start": 0.0, "end": info["duration"], "rect": [0, 0, p["W"] // 2 * 2, p["H"] // 2 * 2],
                      "on": True}]
    if p is not None:
        p = trim_bars(path, p, job)
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
             "drawbox=x=130:y=6:w=90:h=18:c=0xcc2222:t=fill:enable='lt(mod(t\\,1)\\,0.5)',"
             "drawbox=x=1050:y=8:w=40:h=14:c=white:t=fill,drawbox=x=0:y=0:w=63:h=540:c=0xf0f2f5:t=fill,"
             "drawbox=x=25:y=120:w=14:h=20:c=0x777777:t=fill,drawbox=x=25:y=260:w=14:h=14:c=0x777777:t=fill,"
             "drawbox=x=1085:y=30:w=55:h=510:c=black:t=fill[v]")
    rc, _, err = fftools.run([fftools.FFMPEG, "-v", "error", "-y", "-nostdin",
                              "-f", "lavfi", "-i", "testsrc2=s=1140x540:r=30:d=20",
                              "-filter_complex", graph, "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast",
                              "-crf", "30", "-g", "60", "-pix_fmt", "yuv420p", path], timeout=180)
    if rc != 0:
        raise RuntimeError(err[-300:])


def make_tab_sample(path, dark_top=False):
    """폴드·태블릿 세로형: 상태바, 빨간 메뉴바, 글자 많은 회색 탭바, 영상, 아래 버튼줄, 작업표시줄"""
    tabs = "".join(f",drawbox=x={x}:y=40:w=40:h=10:c=0xdddddd:t=fill" for x in (40, 110, 170, 230, 300, 400, 520))
    icons = "".join(f",drawbox=x={x}:y=515:w=22:h=22:c=0x{c}:t=fill"
                    for x, c in zip((170, 205, 240, 275, 310, 345, 380),
                                    ("50a030", "10a080", "70a000", "30a050", "20a070", "60a010", "40a060")))
    dark = "drawbox=x=0:y=0:w=650:h=60:c=black@0.8:t=fill," if dark_top else ""
    graph = ("[0:v]" + dark + "drawbox=x=0:y=0:w=80:h=40:c=0x3399ff:t=fill,drawbox=x=580:y=5:w=60:h=36:c=0xffcc00:t=fill[vid];"
             "[1:v][vid]overlay=0:58,drawbox=x=0:y=10:w=650:h=22:c=0x9c2a2e:t=fill,"
             "drawbox=x=30:y=14:w=120:h=14:c=white:t=fill,drawbox=x=450:y=15:w=14:h=12:c=white:t=fill,"
             "drawbox=x=500:y=15:w=60:h=12:c=0xeeeeee:t=fill,drawbox=x=0:y=32:w=650:h=26:c=0x333333:t=fill" + tabs +
             ",drawbox=x=10:y=2:w=50:h=7:c=0xcc2222:t=fill:enable='lt(mod(t\\,1)\\,0.5)',drawbox=x=580:y=2:w=50:h=7:c=white:t=fill,"
             "drawbox=x=10:y=470:w=30:h=30:c=0xeeeeee:t=fill,drawbox=x=50:y=475:w=50:h=20:c=0x333333:t=fill,"
             "drawbox=x=560:y=475:w=60:h=22:c=0x559933:t=fill" + icons + "[v]")
    rc, _, err = fftools.run([fftools.FFMPEG, "-v", "error", "-y", "-nostdin",
                              "-f", "lavfi", "-i", "testsrc2=s=650x402:r=30:d=20",
                              "-f", "lavfi", "-i", "color=c=black:s=650x540:r=30:d=20",
                              "-filter_complex", graph, "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast",
                              "-crf", "28", "-g", "60", "-pix_fmt", "yuv420p", path], timeout=180)
    if rc != 0:
        raise RuntimeError(err[-300:])


def make_portrait_sample(path):
    """세로 녹화: 상태바, 영상, 팁 버튼 줄, 채팅(계속 바뀜, 왼쪽 아이콘 줄), 아래 내비바"""
    graph = ("[2:v][0:v]overlay=0:48[a];[a][1:v]overlay=80:620,"
             "drawbox=x=0:y=0:w=540:h=48:c=black:t=fill,drawbox=x=20:y=15:w=40:h=16:c=white:t=fill,"
             "drawbox=x=70:y=12:w=60:h=22:c=0xcc2222:t=fill:enable='lt(mod(t\\,1)\\,0.5)',"
             "drawbox=x=440:y=15:w=80:h=16:c=white:t=fill,drawbox=x=0:y=560:w=540:h=50:c=0x111111:t=fill,"
             "drawbox=x=200:y=568:w=60:h=30:c=0x558833:t=fill,drawbox=x=290:y=568:w=150:h=30:c=0x664400:t=fill,"
             "drawbox=x=0:y=610:w=70:h=650:c=0x262626:t=fill,drawbox=x=20:y=640:w=24:h=24:c=0x999999:t=fill,"
             "drawbox=x=0:y=1260:w=540:h=60:c=0xfbeef0:t=fill,drawbox=x=150:y=1280:w=16:h=20:c=0x777777:t=fill,"
             "drawbox=x=262:y=1280:w=20:h=20:c=0x777777:t=fill[v]")
    rc, _, err = fftools.run([fftools.FFMPEG, "-v", "error", "-y", "-nostdin",
                              "-f", "lavfi", "-i", "testsrc2=s=540x512:r=24:d=20",
                              "-f", "lavfi", "-i", "life=s=440x620:seed=7:mold=10:r=24:ratio=0.08:death_color=#202020:"
                                                   "life_color=#bbbbbb,trim=duration=10,"
                                                   "tpad=stop_mode=clone:stop_duration=10",
                              "-f", "lavfi", "-i", "color=c=0x1a1a1a:s=540x1320:r=24:d=20",
                              "-filter_complex", graph, "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast",
                              "-crf", "28", "-g", "48", "-pix_fmt", "yuv420p", path], timeout=180)
    if rc != 0:
        raise RuntimeError(err[-300:])


def make_fold_full_sample(path):
    """큰 화면 전체화면: 얇은 상태바(깜빡이는 빨간 표시) 바로 아래 영상, 영상 윗부분은 어둡고 가만히"""
    # 영상 윗부분은 어두운 장면 (실제처럼 노이즈로 조금씩 흔들림)
    graph = ("[0:v]split[m][t];[t]crop=650:70:0:0,noise=alls=100:allf=t,"
             "colorlevels=romax=0.5:gomax=0.5:bomax=0.5[dk];[m][dk]overlay=0:0[vid];[1:v][vid]overlay=0:14,"
             "drawbox=x=0:y=0:w=650:h=14:c=black:t=fill,drawbox=x=10:y=2:w=50:h=9:c=0xcc2222:t=fill:"
             "enable='lt(mod(t\\,1)\\,0.5)',drawbox=x=70:y=3:w=20:h=8:c=white:t=fill,"
             "drawbox=x=560:y=3:w=70:h=8:c=white:t=fill[v]")
    rc, _, err = fftools.run([fftools.FFMPEG, "-v", "error", "-y", "-nostdin",
                              "-f", "lavfi", "-i", "testsrc2=s=650x526:r=30:d=20",
                              "-f", "lavfi", "-i", "color=c=black:s=650x540:r=30:d=20",
                              "-filter_complex", graph, "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast",
                              "-crf", "28", "-g", "60", "-pix_fmt", "yuv420p", path], timeout=180)
    if rc != 0:
        raise RuntimeError(err[-300:])


def make_tab_dark_sample(path):
    """폴드형인데 영상 윗부분이 어두워 회색 탭바와의 경계가 흐린 경우"""
    make_tab_sample(path, dark_top=True)


def make_fold_browser_sample(path, live=False):
    """폴드 안쪽(세로 1.2 비율) 크롬: 주소창, 빨간 메뉴바, 회색 탭바, 영상, 하트·팁 줄, 목표 막대, 설명 글"""
    tabs = "".join(f",drawbox=x={x}:y=63:w=40:h=9:c=0xdddddd:t=fill" for x in (40, 110, 170, 230, 330, 440))
    graph = ("[0:v]drawbox=x=0:y=0:w=540:h=50:c=black@0.75:t=fill[vid];[1:v][vid]overlay=0:80,"
             "drawbox=x=0:y=0:w=540:h=30:c=0xf1f3f4:t=fill,drawbox=x=90:y=5:w=330:h=20:c=white:t=fill,"
             "drawbox=x=100:y=11:w=200:h=8:c=0x333333:t=fill,drawbox=x=10:y=9:w=14:h=12:c=0x555555:t=fill,"
             "drawbox=x=0:y=30:w=540:h=26:c=0x9c2a2e:t=fill,drawbox=x=40:y=35:w=110:h=15:c=white:t=fill,"
             "drawbox=x=420:y=36:w=14:h=12:c=white:t=fill,drawbox=x=0:y=56:w=540:h=24:c=0x333333:t=fill" + tabs +
             ",drawbox=x=0:y=370:w=540:h=110:c=0x222222:t=fill,drawbox=x=10:y=376:w=80:h=22:c=0x444444:t=fill,"
             "drawbox=x=20:y=381:w=12:h=12:c=0xdd3344:t=fill,drawbox=x=380:y=376:w=150:h=22:c=0xbb9900:t=fill,"
             "drawbox=x=10:y=408:w=330:h=18:c=0x666666:t=fill,"
             + ("drawbox=x=10:y=408:w='120+t*8':h=18:c=0x557711:t=fill,"
                "drawbox=x=40:y=381:w=30:h=12:c=white:t=fill:enable='lt(mod(t\\,3)\\,1.5)',"
                if live else "drawbox=x=10:y=408:w=200:h=18:c=0x557711:t=fill,") +
             "drawbox=x=10:y=440:w=420:h=8:c=0xbbbbbb:t=fill,drawbox=x=10:y=456:w=160:h=8:c=0xbbbbbb:t=fill[v]")
    rc, _, err = fftools.run([fftools.FFMPEG, "-v", "error", "-y", "-nostdin",
                              "-f", "lavfi", "-i", "testsrc2=s=540x290:r=30:d=20",
                              "-f", "lavfi", "-i", "color=c=black:s=540x650:r=30:d=20",
                              "-filter_complex", graph, "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast",
                              "-crf", "28", "-g", "60", "-pix_fmt", "yuv420p", path], timeout=180)
    if rc != 0:
        raise RuntimeError(err[-300:])


def make_portrait_in_landscape_sample(path):
    """가로 녹화(1110x540) 가운데에 세로 화면: 양옆 검은 여백, 상태바, 영상, 팁 줄, 채팅, 내비바"""
    graph = ("[2:v][0:v]overlay=430:20[a];[a][1:v]overlay=470:250,"
             "drawbox=x=430:y=0:w=250:h=20:c=0xdddddd:t=fill,drawbox=x=440:y=5:w=30:h=10:c=0x333333:t=fill,"
             "drawbox=x=620:y=5:w=50:h=10:c=0x333333:t=fill,"
             "drawbox=x=430:y=220:w=250:h=30:c=0x222222:t=fill,drawbox=x=520:y=225:w=50:h=20:c=0x558833:t=fill,"
             "drawbox=x=430:y=250:w=40:h=265:c=0x2a2a2a:t=fill,"
             "drawbox=x=430:y=515:w=250:h=25:c=0xf4f4f4:t=fill,drawbox=x=500:y=522:w=10:h=12:c=0x777777:t=fill,"
             "drawbox=x=600:y=522:w=10:h=12:c=0x777777:t=fill[v]")
    rc, _, err = fftools.run([fftools.FFMPEG, "-v", "error", "-y", "-nostdin",
                              "-f", "lavfi", "-i", "testsrc2=s=250x200:r=30:d=20",
                              "-f", "lavfi", "-i", "life=s=210x265:seed=7:mold=10:r=30:ratio=0.08:death_color=#202020:"
                                                   "life_color=#bbbbbb,trim=duration=20",
                              "-f", "lavfi", "-i", "color=c=black:s=1110x540:r=30:d=20",
                              "-filter_complex", graph, "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast",
                              "-crf", "28", "-g", "60", "-pix_fmt", "yuv420p", path], timeout=180)
    if rc != 0:
        raise RuntimeError(err[-300:])


def make_browser_live_sample(path):
    """폴드 브라우저 + 목표 막대가 계속 차오르고 하트 수가 바뀌는 경우"""
    make_fold_browser_sample(path, live=True)


def make_pinl_wall_sample(path):
    """가로 녹화 속 세로 화면 — 영상 윗부분이 가만히 있는 흰 벽(카메라 노이즈), 그 위에 밝은 회색 상태바"""
    graph = ("[3:v]noise=alls=12:allf=t[w];[w][0:v]vstack[cam];[2:v][cam]overlay=430:20[a];[a][1:v]overlay=470:250,"
             "drawbox=x=430:y=0:w=250:h=20:c=0xe4e4e4:t=fill,drawbox=x=440:y=5:w=30:h=10:c=0x555555:t=fill,"
             "drawbox=x=620:y=5:w=50:h=10:c=0x555555:t=fill,"
             "drawbox=x=430:y=220:w=250:h=30:c=0x222222:t=fill,drawbox=x=520:y=225:w=50:h=20:c=0x558833:t=fill,"
             "drawbox=x=430:y=250:w=40:h=265:c=0x2a2a2a:t=fill,"
             "drawbox=x=430:y=515:w=250:h=25:c=0xf4f4f4:t=fill,drawbox=x=500:y=522:w=10:h=12:c=0x777777:t=fill,"
             "drawbox=x=600:y=522:w=10:h=12:c=0x777777:t=fill[v]")
    rc, _, err = fftools.run([fftools.FFMPEG, "-v", "error", "-y", "-nostdin",
                              "-f", "lavfi", "-i", "testsrc2=s=250x140:r=30:d=20",
                              "-f", "lavfi", "-i", "life=s=210x265:seed=7:mold=10:r=30:ratio=0.08:death_color=#202020:"
                                                   "life_color=#bbbbbb,trim=duration=20",
                              "-f", "lavfi", "-i", "color=c=black:s=1110x540:r=30:d=20",
                              "-f", "lavfi", "-i", "color=c=0xf0f0f0:s=250x60:r=30:d=20",
                              "-filter_complex", graph, "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast",
                              "-crf", "28", "-g", "60", "-pix_fmt", "yuv420p", path], timeout=180)
    if rc != 0:
        raise RuntimeError(err[-300:])


def make_browser_chat_sample(path):
    """폴드 크롬 — 설명 글 아래에 계속 올라오는 채팅 (프레임 맨 아래가 계속 바뀜)"""
    tmp = path + ".base.mp4"
    make_fold_browser_sample(tmp, live=True)
    rc, _, err = fftools.run([fftools.FFMPEG, "-v", "error", "-y", "-nostdin", "-i", tmp,
                              "-f", "lavfi", "-i", "testsrc2=s=520x160:r=30:d=20",
                              "-filter_complex", "[1:v]hue=s=0,eq=contrast=0.6[c];[0:v][c]overlay=10:485[v]", "-map", "[v]",
                              "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-g", "60",
                              "-pix_fmt", "yuv420p", path], timeout=180)
    try:
        os.remove(tmp)
    except OSError:
        pass
    if rc != 0:
        raise RuntimeError(err[-300:])


SELFTEST_CASES = [
    ("phone", make_phone_sample, [63, 30, 1022, 510]),            # 가로 폰: 상태바·왼쪽 내비바·오른쪽 여백
    ("tab", make_tab_sample, [0, 58, 650, 402]),                  # 폴드형: 메뉴바·탭바·버튼줄·작업표시줄
    ("portrait", make_portrait_sample, [0, 48, 540, 1212]),       # 세로: 상태바·내비바만, 채팅 유지
    ("foldfull", make_fold_full_sample, [0, 14, 650, 526]),       # 큰 화면 전체화면: 얇은 상태바
    ("tabdark", make_tab_dark_sample, [0, 58, 650, 402]),         # 어두운 탭바(흐린 경계)
    ("browser", make_fold_browser_sample, [0, 80, 540, 290]),     # 폴드 안쪽 크롬
    ("browserlive", make_browser_live_sample, [0, 80, 540, 290]), # 목표 막대·하트 수가 바뀜
    ("pinl", make_portrait_in_landscape_sample, [430, 20, 250, 495]),  # 가로 녹화 속 세로 화면
    ("pinlwall", make_pinl_wall_sample, [430, 20, 250, 495]),     # 상태바 아래 가만히 있는 흰 벽
    ("browserchat", make_browser_chat_sample, [0, 80, 540, 290]), # 맨 아래 채팅이 계속 바뀜
]


def selftest(log=print):
    import tempfile
    import exporter
    d = tempfile.mkdtemp(prefix="videocut_crop_")
    # 블러 살리기 + 구성이 바뀌는 시점 + 실제 저장 결과
    src = os.path.join(d, "crop_test.mp4")
    make_sample(src)
    info = fftools.probe(src)
    a = analyzer.analyze(src, info, analyzer.DEFAULTS, use_cache=False)
    plan = compute(src, info, a)
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
    for name, mk, want in SELFTEST_CASES:
        sp = os.path.join(d, name + ".mp4")
        mk(sp)
        ii = fftools.probe(sp)
        aa = analyzer.analyze(sp, ii, analyzer.DEFAULTS, use_cache=False)
        act_ = active_segs(compute(sp, ii, aa))
        good = len(act_) == 1 and all(abs(u - v) <= 4 for u, v in zip(act_[0][2], want))
        log(f"{name} crop: {[r for _, _, r in act_]} {'ok' if good else 'FAIL want ' + str(want)}")
        ok = ok and good
    log("crop ok" if ok else "crop FAIL")
    return ok
