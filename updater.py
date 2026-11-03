"""자동 업데이트 — GitHub 최신 릴리스의 설치 파일을 받아 조용히 설치 후 다시 실행"""
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.request

REPO = "kakario777-netizen/video-cut"
SETUP = "VideoCut_Setup.exe"
WORK = os.path.join(tempfile.gettempdir(), "VideoCut_update")


def is_frozen():
    return bool(getattr(sys, "frozen", False) or "__compiled__" in globals())


def parse_ver(v):
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums) if nums else (0,)


def is_newer(tag, current):
    return parse_ver(tag) > parse_ver(current)


def latest_release(timeout=15):
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                 headers={"User-Agent": "VideoCut", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    asset = next((a for a in data.get("assets", []) if a.get("name") == SETUP), None)
    return {"tag": data.get("tag_name", ""), "notes": data.get("body") or "",
            "url": asset["browser_download_url"] if asset else None, "size": asset["size"] if asset else 0}


def download(url, progress=None):
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)
    path = os.path.join(WORK, SETUP)
    req = urllib.request.Request(url, headers={"User-Agent": "VideoCut"})
    with urllib.request.urlopen(req, timeout=60) as r, open(path, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if progress:
                progress(got, total)
    return path


def run_setup(path):
    """설치 파일을 조용히 실행(진행 막대만 보임). 끝나면 설치 파일이 프로그램을 다시 켬"""
    import ctypes
    params = "/SILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS /restart=1"
    r = ctypes.windll.shell32.ShellExecuteW(None, "open", path, params, os.path.dirname(path), 1)
    if r <= 32:
        raise RuntimeError("설치 파일을 실행하지 못했습니다. (관리자 확인을 취소했다면 다시 시도해주세요)")


def cleanup_later():
    for _ in range(20):
        shutil.rmtree(WORK, ignore_errors=True)
        if not os.path.exists(WORK):
            return
        time.sleep(1)
