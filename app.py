"""Frame Board: 유튜브 영상에서 캐릭터 애니메이션 프레임을 뽑아 보는 로컬 도구.

실행: uv run app.py          (앱 창이 http://127.0.0.1:8765 를 엽니다)
점검: uv run app.py --selftest   (--selftest-online: 유튜브 고화질 포맷까지 확인)
"""
import hashlib
import http.client
import json
import os
import re
import select
import shutil
import statistics
import subprocess
import sys
import sysconfig
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

FROZEN = getattr(sys, "frozen", False)  # PyInstaller 빌드
RES_DIR = Path(sys._MEIPASS) if FROZEN else Path(__file__).resolve().parent  # index.html, deno 위치


def data_dir():
    if not FROZEN:
        return RES_DIR / "data"
    if sys.platform == "darwin":  # .app은 읽기 전용 위치(App Translocation)에서 실행될 수 있음
        return Path.home() / "Library" / "Application Support" / "FrameBoard"
    d = Path(sys.executable).parent / "data"  # Windows: 압축 푼 폴더 안에 두는 것이 기본
    try:
        d.mkdir(exist_ok=True)
        (d / ".write-test").write_bytes(b"")
        (d / ".write-test").unlink()
        return d
    except OSError:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "FrameBoard"


DATA = data_dir()
VIDEO_DIR = DATA / "videos"
JOB_DIR = DATA / "jobs"
HOST, PORT = "127.0.0.1", 8765
URL = f"http://{HOST}:{PORT}"
FORMAT = ("bv*[vcodec^=avc1][height<=1080][ext=mp4][protocol=https]/bv*[ext=mp4][protocol=https]"
          "/b[ext=mp4][protocol=https]/bv*[protocol=https]")  # https 단일 파일만 중계할 수 있음
# yt-dlp가 유튜브 고화질 포맷을 받으려면 JS 런타임(deno)이 필요합니다. 빌드에는 deno가 함께 들어 있습니다.
DENO = (RES_DIR if FROZEN else Path(sysconfig.get_path("scripts"))) / ("deno.exe" if os.name == "nt" else "deno")
YDL_OPTS = {"quiet": True, "no_warnings": True, "noplaylist": True, "format": FORMAT,
            "js_runtimes": {"deno": {"path": str(DENO)}}}
MAX_FRAMES = 3000
DEDUPE_DIFF = 2.5
MEDIA_TYPES = {"mp4": "video/mp4", "webm": "video/webm"}
CHUNK = 4 << 20  # 유튜브는 긴 연결을 중간에 끊으므로 4MB씩 나눠 받고, 끊기면 그 위치부터 다시 받습니다.
CACHE_MAX = 256 << 20  # 최근에 중계한 조각을 메모리에 보관: 구간 반복으로 같은 곳을 다시 요청해도 유튜브까지 가지 않음
STREAM_TTL = 3 * 3600  # 유튜브 스트림 주소는 몇 시간 뒤 만료됩니다.

LOCK = threading.Lock()
videos = {}  # id -> VideoMeta (+ "source": 유튜브 페이지 주소)
streams = {}  # id -> {"url", "headers", "size", "at"}: 영상을 통째로 받지 않고 필요한 부분만 중계합니다.
PROBE_LOCK = threading.Lock()  # ponytail: 주소 갱신은 전체에서 한 번에 하나, 영상 여러 개를 동시에 쓰게 되면 영상별 잠금으로
pieces = {}  # (id, 파일 크기, 시작 바이트) -> 받은 바이트. 넣은 순서대로 쓸 때마다 맨 뒤로 (오래 안 쓴 것부터 버림)
jobs = {}  # job_id -> job dict
alive = {"n": 0, "seen": False}  # 열려 있는 /api/alive 연결 수, 한 번이라도 연결됐는지

# OpenCV는 Windows에서 한글 경로를 못 여는 경우가 있어, DATA 기준 상대경로(ASCII)로 넘깁니다.
DATA.mkdir(parents=True, exist_ok=True)
os.chdir(DATA)

if FROZEN:  # 창 없는 빌드는 stdout/stderr가 없으므로 로그 파일로 보냅니다.
    _log = DATA / "log.txt"
    try:
        if _log.stat().st_size > 1_000_000:
            _log.unlink()  # Windows에서 이미 실행 중인 쪽이 열어 두었으면 실패하므로 무시
    except OSError:
        pass
    sys.stdout = sys.stderr = open(_log, "a", encoding="utf-8", errors="replace", buffering=1)

# 무거운 라이브러리는 창을 먼저 띄운 뒤 백그라운드에서 불러옵니다. 쓰는 곳은 LIBS_READY를 기다립니다.
cv2 = np = yt_dlp = None
LIBS_READY = threading.Event()


def load_libs():
    global cv2, np, yt_dlp
    try:
        import cv2
        import numpy as np
        import yt_dlp
    finally:
        LIBS_READY.set()  # 실패해도 기다리는 쪽이 멈추지 않도록 (오류는 로그에 남음)


def cvp(path):
    return os.path.relpath(path)


# ---------------------------------------------------------------- 영상 (스트리밍)

def save_meta(meta):
    with LOCK:
        text = json.dumps(meta, ensure_ascii=False, indent=1)
        tmp = VIDEO_DIR / f"{meta['id']}.json.tmp"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, VIDEO_DIR / f"{meta['id']}.json")


def load_videos():
    for f in VIDEO_DIR.glob("*.json"):
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if meta.get("status") == "ready" and (meta.get("source") or meta.get("kind") == "gif"):  # 예전 다운로드 방식 기록은 건너뜀
            videos[meta["id"]] = meta


def clean(e):
    return re.sub(r"\x1b\[[0-9;]*m", "", str(e))  # 터미널 실행 시 yt-dlp가 넣는 색상 코드 제거


def probe(url):
    """URL을 확인하고 (영상 id, 정보)를 돌려줍니다. 문제가 있으면 ValueError(한글 메시지)."""
    LIBS_READY.wait()
    try:
        with yt_dlp.YoutubeDL({**YDL_OPTS, "extract_flat": "in_playlist"}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        raise ValueError(f"영상을 불러올 수 없습니다. 주소를 확인해 주세요. ({clean(e)})"[:300])
    if info.get("_type") == "playlist" or not info.get("id"):
        raise ValueError("재생목록이 아닌 영상 하나의 주소를 넣어 주세요.")
    if not info.get("url"):
        raise ValueError("이 영상은 재생할 수 있는 형식을 찾지 못했습니다.")
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(info["id"])), info


def open_upstream(s, a, b):
    req = urllib.request.Request(s["url"], headers={**s["headers"], "Range": f"bytes={a}-{b}"})
    return urllib.request.urlopen(req, timeout=20)


def new_stream(info):
    s = {"url": info["url"], "headers": info.get("http_headers") or {}, "size": info.get("filesize"),
         "at": time.time()}
    if not s["size"]:
        with open_upstream(s, 0, 0) as r:
            total = (r.headers.get("Content-Range") or "").rsplit("/", 1)[-1]
        if not total.isdigit():
            raise ValueError("이 영상은 재생할 수 있는 형식을 찾지 못했습니다.")
        s["size"] = int(total)
    return s


def stream(vid, refresh=False):
    started = time.time()
    with PROBE_LOCK:  # 여러 요청이 동시에 만료를 만나도 yt-dlp는 한 번만 돌립니다.
        with LOCK:
            s, meta = streams.get(vid), videos.get(vid)
        if s and (s["at"] >= started or not refresh) and time.time() - s["at"] < STREAM_TTL:
            return s
        if not meta:
            raise LookupError("영상을 찾을 수 없습니다.")
        s = new_stream(probe(meta["source"])[1])
        with LOCK:
            streams[vid] = s
        return s


def fetch(vid, a, b):
    """유튜브에서 a~b 바이트 응답을 엽니다. 주소가 만료(403 등)됐으면 새로 받아 한 번 더 시도합니다."""
    try:
        return open_upstream(stream(vid), a, b)
    except urllib.error.HTTPError as e:
        if e.code not in (403, 404, 410):
            raise
    return open_upstream(stream(vid, refresh=True), a, b)


def piece(key, pos):
    """보관된 조각 중 pos 바이트가 들어 있는 것의 (시작, 바이트). key = (id, 파일 크기)."""
    with LOCK:
        for k, b in pieces.items():
            if k[:2] == key and k[2] <= pos < k[2] + len(b):
                pieces[k] = pieces.pop(k)  # 최근에 씀
                return k[2], b
    return None


def keep(key, start, data):
    with LOCK:
        pieces[(*key, start)] = data
        total = sum(map(len, pieces.values()))
        while total > CACHE_MAX:
            total -= len(pieces.pop(next(iter(pieces))))


def add_video(url):
    vid, info = probe(url)
    s = new_stream(info)
    ext = info.get("ext") if info.get("ext") in MEDIA_TYPES else "mp4"
    with LOCK:
        old = videos.get(vid)
        streams[vid] = s  # 아래 cv2 확인이 중계 주소를 쓰므로 먼저 등록 (기존 기록은 확인이 끝난 뒤 교체)
        meta = {"id": vid, "title": info.get("title") or vid, "status": "ready", "progress": 1.0, "error": None,
                "width": 0, "height": 0, "fps": 0.0, "frame_count": 0, "duration": 0.0,
                "url": f"/media/{vid}.{ext}", "source": info.get("webpage_url") or url,
                "created": old["created"] if old else time.time()}
    # fps는 반드시 실제 영상에서 읽습니다(29.97을 30으로 쓰면 뒤로 갈수록 프레임 번호가 어긋남).
    # 자기 서버의 중계 주소로 열어 HTTPS 지원 여부와 끊김 재시도를 신경 쓰지 않아도 됩니다.
    cap = cv2.VideoCapture(f"{URL}{meta['url']}")
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps, count = cap.get(cv2.CAP_PROP_FPS), int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    if not (w and h and fps):
        raise ValueError("영상을 열 수 없습니다. 잠시 뒤 다시 시도해 주세요.")
    count = count or int((info.get("duration") or 0) * fps)
    meta.update(width=w, height=h, fps=fps, frame_count=count, duration=count / fps)
    with LOCK:
        videos[vid] = meta
    save_meta(meta)
    return vid


# ---------------------------------------------------------------- 프레임 추출 (서버/셀프테스트 공용)

class Frames:
    """GIF처럼 프레임 PNG로 풀어 둔 영상을 cv2.VideoCapture처럼 읽습니다 (set/read/release만)."""

    def __init__(self, paths):
        self.paths, self.i = paths, 0

    def set(self, _prop, n):
        self.i = int(n)

    def read(self):
        if self.i >= len(self.paths):
            return False, None
        f = cv2.imread(cvp(self.paths[self.i]), cv2.IMREAD_UNCHANGED)
        self.i += 1
        return f is not None, f

    def release(self):
        pass


def open_source(src):
    return Frames(src) if isinstance(src, list) else cv2.VideoCapture(src if isinstance(src, str) else cvp(src))


def bgr(frame):
    if frame.ndim == 2:  # 흑백 PNG
        return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    return frame[:, :, :3] if frame.shape[2] == 4 else frame  # GIF는 투명도(BGRA)가 있음


def top_left(c, size, shape):
    """중심 c에 놓인 size 박스의 왼쪽 위. 화면 밖으로 나가지 않게 붙입니다.

    index.html의 boxRect와 같은 계산이어야 합니다 (미리보기 박스 = 잘리는 영역). 반올림 차이가 없게 정수로만 셉니다.
    """
    (w, h), (fh, fw) = size, shape[:2]
    return min(max(int(c[0]) - w // 2, 0), fw - w), min(max(int(c[1]) - h // 2, 0), fh - h)


# 추적 판단 기준. 실제 게임 영상(INARI)에서 맞춤: 맞게 따라간 프레임은 키 박스와 0.4 안팎, 캐릭터가 빠져나간 박스는 0.1~0.2
MATCH_OK = 0.3  # 키 박스(사용자가 그린 박스)의 모습과 이만큼 닮으면 제대로 따라가는 중
MATCH_CORE = 0.25  # 박스 가운데(캐릭터 자리)도 이만큼은 닮아야 함: 밝은 무늬 배경에 박스만 남으면 전체는 닮아도 가운데가 다름
MATCH_FOUND = 0.6  # 놓친 뒤 넓게 찾을 때 이만큼 닮은 곳이 있으면 되찾음
MATCH_MARGIN = 0.1  # 되찾을 곳은 지금 위치보다 이만큼 더 닮아야 옮김 (비슷한 것 사이에서 튀지 않게)
SEARCH_PX = 24  # 넓게 찾을 때는 박스의 짧은 변이 이 크기가 되게 줄여서 찾고, 찾은 곳 둘레만 원본으로 다시 봄
GOOD, LOST, GUESS = 1, 0, 2  # 프레임별 상태: 추적됨 / 놓침 / 놓쳤다 되찾은 사이(앞뒤로 이어 붙인 위치)


class Tracker:
    """CSRT로 따라가면서, 사용자가 그린 박스의 모습(좌우 반전 포함)과 계속 비교합니다.

    CSRT는 대쉬·구르기처럼 모습이 잠깐 찌그러지면 그 모습을 배우거나 배경에 붙어 버리고 다시 못 찾습니다.
    그래서 원래 모습과 닮지 않으면 '놓침'으로 보고, 놓친 시간만큼 넓은 범위에서 원래 모습을 찾아 되찾습니다.
    """

    def __init__(self, size):
        self.w, self.h = size
        self.looks, self.csrt, self.c, self.lost = [], None, None, 0
        self.k = min(1.0, SEARCH_PX / min(self.w, self.h))
        self.small = []  # looks를 k배로 줄인 것

    def patch(self, frame, c):
        x, y = top_left(c, (self.w, self.h), frame.shape)
        return frame[y:y + self.h, x:x + self.w]

    def remember(self, frame, c):
        p = self.patch(frame, c)
        for t in (p, cv2.flip(p, 1)):  # 캐릭터는 방향을 바꾸면 좌우가 뒤집힘
            self.looks.append(t)
            self.small.append(self.shrink(t))

    def shrink(self, img):
        return img if self.k == 1 else cv2.resize(img, None, fx=self.k, fy=self.k, interpolation=cv2.INTER_AREA)

    def restart(self, frame, c):
        self.c, self.lost = c, 0
        self.csrt = cv2.TrackerCSRT_create()
        x, y = top_left(c, (self.w, self.h), frame.shape)
        try:
            self.csrt.init(frame, (x, y, self.w, self.h))
        except cv2.error:  # 1~2px처럼 너무 작은 영역은 모습 비교만으로 찾음
            self.csrt = None

    def key(self, frame, c):
        self.remember(frame, c)
        self.restart(frame, c)

    def fits(self, frame, c):
        """(키 박스와 닮은 정도, 캐릭터가 거기 있는지). 가운데 절반도 따로 비교해서 배경만 닮은 경우를 걸러냅니다."""
        p, y, x = self.patch(frame, c), self.h // 4, self.w // 4
        ncc = lambda a, b: float(np.nan_to_num(cv2.matchTemplate(a, b, cv2.TM_CCOEFF_NORMED)[0, 0]))  # noqa: E731
        s = max(ncc(p, t) for t in self.looks)
        mid = lambda a: a[y:self.h - y, x:self.w - x]  # noqa: E731
        return s, s >= MATCH_OK and max(ncc(mid(p), mid(t)) for t in self.looks) >= MATCH_CORE

    def search(self, frame, c, r):
        """c 둘레 r 안에서 원래 모습과 가장 닮은 곳 (점수, 중심)."""
        fh, fw = frame.shape[:2]
        x0, y0 = max(0, int(c[0] - r - self.w / 2)), max(0, int(c[1] - r - self.h / 2))
        x1, y1 = min(fw, int(c[0] + r + self.w / 2)), min(fh, int(c[1] + r + self.h / 2))
        if x1 - x0 < self.w or y1 - y0 < self.h:
            return 0.0, c
        area = frame[y0:y1, x0:x1]
        small, best = self.shrink(area), None  # 1080p에서 큰 박스로 화면 전체를 원본 크기로 찾으면 프레임마다 1초 가까이 걸림
        for t, ts in zip(self.looks, self.small):
            if ts.shape[0] > small.shape[0] or ts.shape[1] > small.shape[1]:
                continue
            _, s, _, (mx, my) = cv2.minMaxLoc(np.nan_to_num(cv2.matchTemplate(small, ts, cv2.TM_CCOEFF_NORMED)))
            if best is None or s > best[0]:
                best = (s, round(mx / self.k), round(my / self.k), t)
        if best is None:
            return 0.0, c
        _, bx, by, t = best
        pad = int(np.ceil(2 / self.k))  # 줄여서 찾은 위치의 오차 범위를 원본으로 다시 맞춤
        ax, ay = max(0, bx - pad), max(0, by - pad)
        near = area[ay:by + pad + self.h, ax:bx + pad + self.w]
        if near.shape[0] < self.h or near.shape[1] < self.w:
            return 0.0, c
        _, s, _, (mx, my) = cv2.minMaxLoc(np.nan_to_num(cv2.matchTemplate(near, t, cv2.TM_CCOEFF_NORMED)))
        return s, (x0 + ax + mx + self.w / 2, y0 + ay + my + self.h / 2)

    def update(self, frame):
        ok = False
        if self.csrt is not None:
            try:
                ok, (bx, by, bw, bh) = self.csrt.update(frame)
            except cv2.error:
                pass
        c = (bx + bw / 2, by + bh / 2) if ok else self.c  # 크기는 고정이라 중심만 씁니다
        s, here = self.fits(frame, c)
        if ok and here:
            self.c, self.lost = c, 0
            return c, GOOD
        self.lost += 1
        fs, fc = self.search(frame, self.c, max(self.w, self.h) * (1 + self.lost))  # 대쉬로 멀리 가도 찾도록
        if fs >= MATCH_FOUND and fs >= s + MATCH_MARGIN and self.fits(frame, fc)[1]:  # 배경만 닮은 곳으로는 옮기지 않음
            if np.hypot(fc[0] - c[0], fc[1] - c[1]) < max(self.w, self.h) / 4:
                fc = c  # 거의 같은 곳이면 CSRT 위치를 그대로 (박스가 떨리지 않게)
            self.restart(frame, fc)
            return fc, GOOD
        if ok:
            self.c = c  # 모습이 바뀌는 중(찌그러짐 등)일 수 있으니 일단 CSRT를 따라갑니다
        return self.c, LOST


def run_path(src, start, end, size, keys, frm, job):
    """start~end에서 캐릭터 중심을 찾아 job["out"]에 [n, cx, cy, 상태]로 쌓습니다. 이미지는 만들지 않습니다.

    keys: {프레임: 중심} 사용자가 박스를 놓은 곳. 그 프레임에서 다시 출발합니다. frm(키 프레임)부터 계산합니다.
    놓쳤다가 되찾으면, 놓친 동안의 프레임을 앞뒤 위치로 이어 붙여 같은 n으로 다시 보냅니다(덮어쓰기).
    """
    cap = open_source(src)
    try:
        tr = Tracker(size)
        for n in sorted(k for k in keys if k < frm):  # 앞쪽 키 박스의 모습도 기억해 둠 (처음부터 계산한 것과 같게)
            cap.set(cv2.CAP_PROP_POS_FRAMES, n)
            ok, f = cap.read()
            if ok:
                tr.remember(bgr(f), keys[n])
        cap.set(cv2.CAP_PROP_POS_FRAMES, frm)
        n, good, gap, prev = frm, None, [], None  # good: 마지막으로 제대로 찾은 (n, 중심), gap: 그 뒤로 놓친 프레임
        while n <= end and not job["cancel"]:
            ok, frame = cap.read()
            if not ok:
                break
            frame = bgr(frame)
            if n in keys:
                tr.key(frame, keys[n])
                c, st = keys[n], GOOD
            else:
                c, st = tr.update(frame)
            out = [[n, round(c[0]), round(c[1]), st]]
            if st == LOST:
                gap.append(out[0])
            elif gap:  # 다시 찾음
                if np.hypot(c[0] - gap[-1][1], c[1] - gap[-1][2]) < max(size) / 2:
                    # CSRT가 끊김 없이 따라온 곳에서 다시 찾음: 모습만 달랐던 것(구르기 등)이라 그 위치를 그대로 믿음.
                    # 1~2프레임은 동작이 바뀌는 순간일 뿐이라 표시하지 않음 (실제 영상에서 이런 깜빡임이 많음)
                    out = [[*g[:3], GOOD if len(gap) < 3 else GUESS] for g in gap] + out
                else:  # 멀리서 다시 찾음: CSRT가 엉뚱한 데 붙어 있었으므로 앞뒤 위치 사이로 이어 붙임
                    (n0, c0), k = good, n - good[0]  # frm은 키 프레임이라 good은 항상 있음
                    out = [[g[0], round(c0[0] + (c[0] - c0[0]) * (g[0] - n0) / k),
                            round(c0[1] + (c[1] - c0[1]) * (g[0] - n0) / k), GUESS] for g in gap] + out
                gap = []
            if st == GOOD:
                good = (n, c)
                if prev and n not in keys and np.hypot(c[0] - prev[0], c[1] - prev[1]) > max(size):
                    out[-1][3] = GUESS  # 한 프레임에 박스 크기보다 멀리 = 순간이동: 다른 것으로 옮겨 붙었을 수 있어 확인하도록
            prev = out[-1][1:3]
            with LOCK:
                job["out"] += out
                job["processed"] = n - frm + 1
            n += 1
    finally:
        cap.release()


def run_crop(src, out_dir, start, end, step, size, centers, dedupe, fps, job):
    """centers(start부터 프레임마다 중심) 위치에서 size 크기로 잘라 PNG로 저장합니다. 미리보기에서 본 박스 그대로입니다."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cap = open_source(src)
    try:
        cap.set(cv2.CAP_PROP_POS_FRAMES, start)
        w, h = size
        last = None
        for n in range(start, end + 1):
            if job["cancel"]:
                break
            ok, frame = cap.read()
            if not ok:
                break
            if (n - start) % step == 0:
                x, y = top_left(centers[n - start], (w, h), frame.shape)
                crop = frame[y:y + h, x:x + w]
                gray = cv2.cvtColor(bgr(crop), cv2.COLOR_BGR2GRAY)
                if not (dedupe and last is not None and cv2.absdiff(gray, last).mean() < DEDUPE_DIFF):
                    last = gray
                    name = f"{n:06d}.png"
                    cv2.imencode(".png", crop)[1].tofile(str(out_dir / name))  # imwrite는 Windows 한글 경로 불가
                    with LOCK:
                        job["out"].append({"n": n, "t": round(n / fps, 4),
                                           "src": f"/files/jobs/{out_dir.name}/{name}", "w": w, "h": h})
            with LOCK:
                job["processed"] = n - start + 1
    finally:
        cap.release()


def run_job(job, fn, *args):
    LIBS_READY.wait()
    try:
        fn(*args, job=job)
        status, error = ("cancelled" if job["cancel"] else "done"), None
        if status == "done" and job["processed"] < job["total"]:  # 영상 끝이나 네트워크 끊김으로 일찍 멈춤
            error = f"영상 읽기가 중간에 멈춰 {job['processed']}/{job['total']} 프레임까지만 확인했습니다."
    except Exception as e:
        status, error = "error", f"프레임을 읽는 중 오류가 발생했습니다. ({e})"[:300]
    with LOCK:
        job.update(status=status, error=error)


def source_of(meta):
    if meta.get("kind") == "gif":
        return [VIDEO_DIR / meta["id"] / f"{n:06d}.png" for n in range(meta["frame_count"])]
    return f"{URL}{meta['url']}"  # 자기 서버의 중계 주소: 필요한 구간만 받아 읽습니다.


def ints(v, n):
    """길이 n인 정수 목록. 아니면 ValueError."""
    v = [int(x) for x in v]
    if len(v) != n:
        raise ValueError
    return v


def new_job(vid, kind, total, fn, *args):
    job = {"id": uuid.uuid4().hex[:12], "vid": vid, "kind": kind, "status": "running", "processed": 0,
           "total": total, "error": None, "out": [], "cancel": False}
    with LOCK:
        if kind == "track":  # 박스를 고칠 때마다 새로 계산하므로, 같은 영상의 이전 계산은 멈추고 버립니다.
            for k in [k for k, j in jobs.items() if j["vid"] == vid and j["kind"] == "track"]:
                jobs.pop(k)["cancel"] = True
        jobs[job["id"]] = job
    threading.Thread(target=run_job, args=(job, fn, *args), daemon=True).start()
    return job["id"]


def parse_span(vid, body):
    with LOCK:
        meta = dict(videos.get(vid) or {})
    if not meta:
        raise LookupError("영상을 찾을 수 없습니다.")
    try:
        start, end = int(body["start"]), int(body["end"])
        w, h = ints(body["size"], 2)
    except (KeyError, TypeError, ValueError):
        raise ValueError("요청 형식이 올바르지 않습니다.")
    if start < 0 or end < start:
        raise ValueError("프레임 범위가 올바르지 않습니다.")
    if w < 1 or h < 1:
        raise ValueError("먼저 영상 위에 박스를 그려 주세요.")
    return meta, start, end, (min(w, meta["width"]), min(h, meta["height"]))


def start_track(vid, body):
    meta, start, end, size = parse_span(vid, body)
    try:
        keys = {n: (cx, cy) for n, cx, cy in (ints(k, 3) for k in body["keys"]) if start <= n <= end}
        frm = int(body.get("from", start))
    except (KeyError, TypeError, ValueError):
        raise ValueError("요청 형식이 올바르지 않습니다.")
    if frm not in keys:
        raise ValueError("추적을 시작할 프레임에 박스가 없습니다.")
    if end - start + 1 > MAX_FRAMES * 6:  # 간격 6프레임으로 최대 장수를 뽑는 길이까지
        raise ValueError("구간이 너무 깁니다. 끝 프레임을 앞으로 당겨 주세요.")
    return new_job(vid, "track", end - frm + 1, run_path, source_of(meta), start, end, size, keys, frm)


def start_extract(vid, body):
    meta, start, end, size = parse_span(vid, body)
    try:
        step, dedupe = int(body.get("step", 1)), bool(body.get("dedupe", False))
        centers = [ints(c, 2) for c in body["centers"]]
    except (KeyError, TypeError, ValueError):
        raise ValueError("요청 형식이 올바르지 않습니다.")
    if step < 1 or len(centers) != end - start + 1:
        raise ValueError("프레임 범위가 올바르지 않습니다.")
    if (end - start) // step + 1 > MAX_FRAMES:
        raise ValueError(f"한 번에 최대 {MAX_FRAMES}장까지 뽑을 수 있습니다. 구간을 줄이거나 간격을 늘려 주세요.")
    job_id = uuid.uuid4().hex[:12]
    return new_job(vid, "extract", end - start + 1, run_crop, source_of(meta), JOB_DIR / job_id, start, end, step,
                   size, centers, dedupe, meta["fps"])


# ---------------------------------------------------------------- GIF

GIF_MAX = 100 << 20
IMAGE_TYPES = ("image/gif", "image/webp", "image/png", "image/apng")  # OpenCV가 움직이는 이미지로 풀 수 있는 형식


def add_gif(data, title):
    """움직이는 이미지(GIF 등)를 프레임 PNG로 풀어 영상처럼 등록합니다. 크롭은 이 원본 픽셀에서 바로 잘립니다."""
    LIBS_READY.wait()
    if len(data) > GIF_MAX:
        raise ValueError(f"파일이 너무 큽니다. {GIF_MAX >> 20}MB까지 열 수 있습니다.")
    vid = "gif_" + hashlib.sha1(data).hexdigest()[:12]
    with LOCK:
        if vid in videos:
            return vid
    ok, anim = cv2.imdecodeanimation(np.frombuffer(data, np.uint8), 0, MAX_FRAMES)
    if not ok or not anim.frames:
        raise ValueError("이미지를 읽지 못했습니다. GIF 파일이나 GIF 주소인지 확인해 주세요.")
    d = VIDEO_DIR / vid
    d.mkdir(parents=True, exist_ok=True)
    for i, f in enumerate(anim.frames):
        cv2.imencode(".png", f)[1].tofile(str(d / f"{i:06d}.png"))
    # 브라우저처럼 10ms 이하 지연은 100ms로 봅니다 (그런 GIF가 흔하고, 그대로 쓰면 너무 빨리 재생됨).
    delays = [int(t) if int(t) > 10 else 100 for t in anim.durations] or [100]
    fh, fw = anim.frames[0].shape[:2]
    fps = round(1000 / statistics.median(delays), 3)
    meta = {"id": vid, "kind": "gif", "title": title or "GIF", "status": "ready", "progress": 1.0, "error": None,
            "width": fw, "height": fh, "fps": fps, "frame_count": len(anim.frames), "duration": sum(delays) / 1000,
            "delays": delays, "url": f"/files/videos/{vid}/", "created": time.time()}
    with LOCK:
        videos[vid] = meta
    save_meta(meta)
    return vid


def add_url(url):
    """GIF 주소면 받아서 열고, 아니면 yt-dlp로 영상을 엽니다."""
    if re.match(r"https?://", url, re.I) and not re.search(r"(^|\.)(youtube\.com|youtu\.be)/", urlparse(url).netloc + "/"):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20)
        except Exception:
            r = None  # 웹 페이지가 아니면 yt-dlp가 알맞은 오류를 냅니다.
        if r:
            with r:
                head = r.peek(12)[:12]  # 서버가 Content-Type을 octet-stream으로 주는 GIF도 있어 파일 앞부분으로도 봄
                if r.headers.get_content_type() in IMAGE_TYPES or head[:4] == b"GIF8" or head[:8] == b"\x89PNG\r\n\x1a\n" \
                        or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
                    return add_gif(r.read(GIF_MAX + 1), unquote(Path(urlparse(url).path).name) or "GIF")
    return add_video(url)


# ---------------------------------------------------------------- 앱 창 (pywebview)

WINDOW = None  # 앱 창. 없으면 브라우저 창으로 띄운 경우


def save_files(items):
    """보드에서 고른 PNG를 사용자가 고른 곳에 복사합니다. 몇 장이든 저장 창은 한 번만 뜹니다."""
    import webview
    files = []
    for it in items:
        m = re.fullmatch(r"/files/jobs/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+\.png)", str(it.get("src")))
        name = re.sub(r'[\\/:*?"<>|]', "_", str(it.get("name") or "frame.png"))
        if m and (JOB_DIR / m[1] / m[2]).is_file():
            files.append((JOB_DIR / m[1] / m[2], name))
    if not files:
        raise ValueError("저장할 이미지가 없습니다.")
    if len(files) == 1:
        r = WINDOW.create_file_dialog(webview.FileDialog.SAVE, save_filename=files[0][1])
        if not r:
            return 0
        shutil.copyfile(files[0][0], r if isinstance(r, str) else r[0])
        return 1
    r = WINDOW.create_file_dialog(webview.FileDialog.FOLDER)
    if not r:
        return 0
    folder, used = Path(r if isinstance(r, str) else r[0]), set()
    for src, name in files:
        stem, k = Path(name).stem, 1
        while name in used or (folder / name).exists():  # 같은 프레임 번호, 폴더에 있던 파일은 덮어쓰지 않음
            k += 1
            name = f"{stem}_{k}.png"
        used.add(name)
        shutil.copyfile(src, folder / name)
    return len(files)


# ---------------------------------------------------------------- HTTP

def parse_range(header, size):
    """'bytes=a-b' -> (start, end) 포함 구간. 형식이 다르면 None(전체 전송), 범위 밖이면 ValueError(416)."""
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", (header or "").strip())
    if not m or not (m[1] or m[2]):
        return None
    if m[1]:
        start, end = int(m[1]), int(m[2]) if m[2] else size - 1
    else:
        start, end = max(0, size - int(m[2])), size - 1
    end = min(end, size - 1)
    if start > end:
        raise ValueError
    return start, end


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def error(self, code, msg):
        self.send_json({"error": msg}, code)

    def read_body(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise ValueError("요청 형식이 올바르지 않습니다.")
        return body

    def send_file(self, path, ctype, cache="no-cache"):
        if not path.is_file():
            return self.error(404, "파일을 찾을 수 없습니다.")
        data = path.read_bytes()  # index.html과 작은 PNG뿐입니다.
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    def send_stream(self, vid, ctype):
        """유튜브 영상을 Range 요청 그대로 중계합니다. 연결이 끊기면 받은 위치부터 이어서 받습니다."""
        size = stream(vid)["size"]
        try:
            rng = parse_range(self.headers.get("Range"), size)
        except ValueError:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        start, end = rng or (0, size - 1)
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "no-cache")
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        pos, fails, key = start, 0, (vid, size)
        while pos <= end and fails < 5:
            hit = piece(key, pos)
            if hit:
                part = hit[1][pos - hit[0]:end - hit[0] + 1]
                self.wfile.write(part)
                pos += len(part)
                continue
            got, buf, at = 0, [], pos
            try:  # 헤더를 이미 보냈으므로 어떤 실패든 재시도로 처리합니다(오류 응답을 섞어 쓰면 안 됨).
                r = fetch(vid, pos, min(pos + CHUNK - 1, end))
                if r.status != 206 or not (r.headers.get("Content-Range") or "").startswith(f"bytes {pos}-"):
                    r.close()
                    r = None
            except Exception:
                r = None
            if r:
                try:
                    with r:
                        while True:
                            try:
                                chunk = r.read(256 << 10)
                            except (OSError, http.client.HTTPException):
                                break  # 유튜브 쪽 끊김: 받은 위치부터 다시
                            if not chunk:
                                break
                            buf.append(chunk)
                            got += len(chunk)
                            self.wfile.write(chunk)  # 브라우저 쪽 끊김은 ConnectionError로 올라가 safe()가 처리
                            pos += len(chunk)
                finally:  # 브라우저가 중간에 끊어도 받은 만큼은 보관
                    if got:
                        keep(key, at, b"".join(buf))
            fails = 0 if got else fails + 1
            if not got:
                time.sleep(0.3 * fails)

    def safe(self, fn):
        try:
            fn()
        except ConnectionError:  # 브라우저가 영상 요청을 중간에 끊는 것은 정상
            pass
        except Exception as e:
            try:
                self.error(500, f"서버 오류가 발생했습니다. ({e})")
            except ConnectionError:
                pass

    def keep_alive(self):
        """창이 열려 있는 동안 붙어 있는 SSE 연결. 모두 끊긴 채 3초가 지나면 프로그램이 종료됩니다."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b"retry: 1000\n\n")
        with LOCK:
            alive["n"] += 1
            alive["seen"] = True
        try:
            # EventSource는 아무것도 보내지 않으므로, 읽을 게 생겼다는 것은 연결이 끊겼다는 뜻입니다.
            while not select.select([self.connection], [], [], 10)[0]:
                self.wfile.write(b": ping\n\n")
        finally:
            with LOCK:
                alive["n"] -= 1

    def allowed(self):
        # DNS 리바인딩 막기: 다른 사이트가 자기 도메인을 127.0.0.1로 가리켜 들어와도 Host가 달라서 거절됩니다.
        if self.headers.get("Host") in (f"{HOST}:{PORT}", f"localhost:{PORT}"):
            return True
        self.error(403, "허용되지 않은 요청입니다.")

    def do_GET(self):
        if self.allowed():
            self.safe(self.route_get)

    def do_POST(self):
        if self.allowed():
            self.safe(self.route_post)

    def route_get(self):
        url = urlparse(self.path)
        path = url.path
        if path in ("/", "/index.html"):
            return self.send_file(RES_DIR / "index.html", "text/html; charset=utf-8")
        if path == "/api/alive":
            return self.keep_alive()
        if path == "/api/videos":
            with LOCK:
                items = sorted((dict(m) for m in videos.values()), key=lambda m: m.get("created", 0), reverse=True)
            return self.send_json({"videos": items})
        if m := re.fullmatch(r"/api/videos/([A-Za-z0-9_-]+)", path):
            with LOCK:
                meta = dict(videos.get(m[1]) or {})
            return self.send_json(meta) if meta else self.error(404, "영상을 찾을 수 없습니다.")
        if m := re.fullmatch(r"/api/jobs/([0-9a-f]+)", path):
            try:
                since = max(0, int(parse_qs(url.query).get("since", ["0"])[0]))
            except ValueError:
                since = 0
            with LOCK:
                job = jobs.get(m[1])
                if job:
                    res = {k: job[k] for k in ("status", "processed", "total", "error")}
                    res.update(items=job["out"][since:], count=len(job["out"]))
            return self.send_json(res) if job else self.error(404, "작업을 찾을 수 없습니다.")
        if m := re.fullmatch(r"/media/([A-Za-z0-9_-]+)\.(mp4|webm)", path):
            return self.send_stream(m[1], MEDIA_TYPES[m[2]])
        if m := re.fullmatch(r"/files/(jobs|videos)/([A-Za-z0-9_-]+)/([0-9]+\.png)", path):  # 크롭, GIF 프레임
            return self.send_file((JOB_DIR if m[1] == "jobs" else VIDEO_DIR) / m[2] / m[3], "image/png",
                                  "max-age=31536000")
        self.error(404, "페이지를 찾을 수 없습니다.")

    def route_post(self):
        url = urlparse(self.path)
        path = url.path
        # 직접 만든 헤더가 있으면 브라우저가 다른 사이트의 요청을 사전 확인(CORS)에서 막습니다: 웹페이지가 이 서버를 못 부림
        if self.headers.get("X-Frame-Board") != "1":
            return self.error(403, "허용되지 않은 요청입니다.")
        try:
            if path == "/api/videos":
                url = str(self.read_body().get("url") or "").strip()
                if not url:
                    raise ValueError("영상 주소를 입력해 주세요.")
                return self.send_json({"id": add_url(url)})
            if path == "/api/gifs":  # 본문 = 파일 그대로 (붙여넣기, 끌어다 놓기)
                size = int(self.headers.get("Content-Length") or 0)
                if size > GIF_MAX:
                    raise ValueError(f"파일이 너무 큽니다. {GIF_MAX >> 20}MB까지 열 수 있습니다.")
                name = parse_qs(url.query).get("name", ["GIF"])[0]
                return self.send_json({"id": add_gif(self.rfile.read(size), name)})
            if m := re.fullmatch(r"/api/videos/([A-Za-z0-9_-]+)/track", path):
                return self.send_json({"job_id": start_track(m[1], self.read_body())})
            if m := re.fullmatch(r"/api/videos/([A-Za-z0-9_-]+)/extract", path):
                return self.send_json({"job_id": start_extract(m[1], self.read_body())})
            if m := re.fullmatch(r"/api/jobs/([0-9a-f]+)/cancel", path):
                with LOCK:
                    job = jobs.get(m[1])
                    if job:
                        job["cancel"] = True
                return self.send_json({"ok": True}) if job else self.error(404, "작업을 찾을 수 없습니다.")
            if path == "/api/save":
                if not WINDOW:
                    return self.error(501, "앱 창에서만 쓸 수 있습니다.")  # 브라우저 창은 다운로드로 저장
                return self.send_json({"saved": save_files(self.read_body().get("items") or [])})
            if path == "/api/show":  # 이미 실행 중일 때 다시 열면, 새로 띄우는 대신 기존 창을 앞으로
                if WINDOW:
                    bring_to_front()
                return self.send_json({"window": bool(WINDOW)})
            self.error(404, "페이지를 찾을 수 없습니다.")
        except LookupError as e:
            self.error(404, str(e))
        except ValueError as e:
            self.error(400, str(e))


class Server(ThreadingHTTPServer):
    # Windows의 SO_REUSEADDR는 사용 중인 포트에도 바인드되어 중복 실행을 못 막으므로 끕니다.
    allow_reuse_address = os.name != "nt"

    def handle_error(self, request, client_address):
        # 브라우저가 연결을 끊는 것(특히 Windows 10053/10054)은 정상이므로 콘솔에 traceback을 찍지 않습니다.
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)


# ---------------------------------------------------------------- 셀프테스트

def selftest(online):
    load_libs()
    tmp = DATA / "selftest"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        assert parse_range("bytes=0-99", 1000) == (0, 99)
        assert parse_range("bytes=500-", 1000) == (500, 999)
        assert parse_range("bytes=-100", 1000) == (900, 999)
        assert parse_range("bytes=0-5000", 1000) == (0, 999)
        assert parse_range(None, 1000) is None
        keep(("v", 9), 100, b"a" * 50)  # 중계 조각 보관: 들어 있는 위치만 찾고, 넘치면 오래 안 쓴 것부터 버림
        assert piece(("v", 9), 149) == (100, b"a" * 50) and piece(("v", 9), 150) is None and piece(("v", 8), 120) is None
        keep(("v", 9), 0, bytes(CACHE_MAX - 40))  # 합이 넘침 -> 먼저 넣은 조각이 빠짐
        assert list(pieces) == [("v", 9, 0)]
        pieces.clear()
        try:
            parse_range("bytes=2000-", 1000)
            raise AssertionError("416 expected")
        except ValueError:
            pass

        # 320x240, 120프레임. 0~29 정지, 30부터 오른쪽으로 2px/프레임 이동하는 30x30 사각형.
        video = tmp / "test.mp4"
        writer = cv2.VideoWriter(cvp(video), cv2.VideoWriter_fourcc(*"mp4v"), 30, (320, 240))
        assert writer.isOpened(), "VideoWriter failed"
        sx = lambda i: 20 + max(0, i - 30) * 2  # noqa: E731
        for i in range(120):
            img = np.full((240, 320, 3), 40, np.uint8)
            img[:, ::16] = 70  # 배경 줄무늬
            x = sx(i)
            img[100:130, x:x + 30] = (0, 0, 230)  # 빨강 (BGR)
            img[110:120, x + 10:x + 20] = (0, 230, 230)  # 가운데 노랑
            writer.write(img)
        writer.release()

        def path(src, end, size, keys, frm=0):
            job = {"cancel": False, "processed": 0, "out": []}
            run_path(src, 0, end, size, keys, frm, job)
            last = {}
            for n, cx, cy, st in job["out"]:  # 되찾으면 같은 n이 다시 옴 (덮어쓰기)
                last[n] = (cx, cy, st)
            return job, last

        # 1) 따라가기
        job, p = path(video, 119, (30, 30), {0: (35, 115)})
        assert job["processed"] == 120 and sorted(p) == list(range(120)), job["processed"]
        off = max(np.hypot(p[n][0] - sx(n) - 15, p[n][1] - 115) for n in range(120))
        assert off < 6, f"tracking off by {off:.1f}px"

        # 2) 대쉬: 걷다가 납작하게 찌그러진 채(잔상 포함) 프레임당 30px 이동, 좌우가 뒤집힌 원래 모습으로 돌아옴
        #    (예전 CSRT만으로는 여기서 놓친 뒤 끝까지 못 찾았음)
        rng = np.random.default_rng(1)
        bg = cv2.resize(rng.integers(30, 150, (30, 60, 3), dtype=np.uint8), (480, 240), interpolation=cv2.INTER_NEAREST)
        body = np.zeros((28, 20, 3), np.uint8)
        body[:] = (0, 0, 220)
        body[4:10, 12:18], body[6:8, 15:17], body[12:20, 4:16] = (255, 255, 255), (20, 20, 20), (0, 220, 230)
        dash = tmp / "dash.mp4"
        writer = cv2.VideoWriter(cvp(dash), cv2.VideoWriter_fourcc(*"mp4v"), 30, (480, 240))
        truth, x = [], 30
        for i in range(80):
            f = bg.copy()
            if 30 <= i < 38:
                x += 30
                f[132:144, x:x + 36] = (240, 240, 240)
                f[135:141, x + 10:x + 30] = (0, 0, 220)
                for k in (1, 2):  # 잔상
                    g = f[134:142, x - 30 * k:x - 30 * k + 36]
                    g[:] = (g * 0.4 + (80, 80, 102)).astype(np.uint8)
                truth.append((x + 18, 138))
            else:
                x += 1
                legs = (60, 30, 10)
                s = body.copy() if i < 30 else body[:, ::-1].copy()
                (s[22:28, 0:8] if (i // 4) % 2 else s[22:28, 12:20])[:] = legs  # 걷는 동작
                f[116:144, x:x + 20] = s
                truth.append((x + 10, 130))
            writer.write(f)
        writer.release()
        job, p = path(dash, 79, (28, 36), {0: (40, 130)})
        miss = [n for n in range(40, 80) if np.hypot(p[n][0] - truth[n][0], p[n][1] - truth[n][1]) > 10]
        assert not miss, f"lost after dash: {miss}"
        assert all(p[n][2] == GUESS for n in range(30, 38)), "dash frames should be marked as guessed"

        # 2-1) 구르기: 몸을 만 모습으로 포물선을 그리며 이동. 모습이 달라 '놓침'이어도 CSRT가 끊김 없이 따라갔으면
        #      그 위치를 믿어야 함 (되찾은 뒤 앞뒤를 직선으로 이어 덮어쓰면 점프 동안 박스가 캐릭터를 벗어남)
        plain = body.copy()
        plain[6:8, 15:17] = (255, 255, 255)
        ball = cv2.resize(cv2.rotate(plain, cv2.ROTATE_90_CLOCKWISE), (20, 20), interpolation=cv2.INTER_NEAREST)
        roll = tmp / "roll.mp4"
        writer = cv2.VideoWriter(cvp(roll), cv2.VideoWriter_fourcc(*"mp4v"), 30, (480, 240))
        truth2 = []
        for i in range(90):
            f, x = bg.copy(), 42 + i * 2
            if 30 <= i < 60:
                y = 120 + int(-320 * (i - 30) / 30 * (1 - (i - 30) / 30))
                f[y:y + 20, x:x + 20] = ball
                truth2.append((x + 10, y + 10))
            else:
                f[116:144, x:x + 20] = plain
                truth2.append((x + 10, 130))
            writer.write(f)
        writer.release()
        _, p = path(roll, 89, (28, 36), {0: truth2[0]})
        miss = [n for n in range(90) if np.hypot(p[n][0] - truth2[n][0], p[n][1] - truth2[n][1]) > 10]
        assert not miss, f"roll off: {[(n, p[n], truth2[n]) for n in miss[:5]]}"

        # 2-2) 캐릭터보다 훨씬 큰 박스 + 화면이 흐르는 밝은 무늬 배경: 박스가 배경에 붙어 같이 흘러가는데도
        #      '추적됨'(파랑)이면 사용자가 틀린 곳을 못 찾음 (예전 기준으로는 90프레임 중 76프레임이 그랬음)
        wide = cv2.GaussianBlur(cv2.resize(np.random.default_rng(3).integers(40, 160, (30, 64, 3), dtype=np.uint8),
                                           (1280, 300), interpolation=cv2.INTER_NEAREST), (0, 0), 1.5)
        hero = np.zeros((30, 22, 3), np.uint8)
        hero[:], hero[3:11, 6:18], hero[6:8, 12:15], hero[14:24, 2:20] = (40, 30, 30), (220, 220, 230), (20, 20, 20), (60, 60, 200)
        leap = cv2.resize(cv2.rotate(hero, cv2.ROTATE_90_CLOCKWISE), (24, 22), interpolation=cv2.INTER_NEAREST)
        scroll = tmp / "scroll.mp4"
        writer = cv2.VideoWriter(cvp(scroll), cv2.VideoWriter_fourcc(*"mp4v"), 30, (640, 300))
        truth3 = []
        for i in range(90):
            f = np.ascontiguousarray(wide[:, i * 3:i * 3 + 640])
            if 30 <= i < 60:  # 제자리 점프 (화면은 계속 흐름)
                y = 178 - int(400 * (i - 30) / 30 * (1 - (i - 30) / 30))
                f[y:y + 22, 300:324] = leap
                truth3.append((312, y + 11))
            else:
                f[170:200, 300:322] = hero if (i // 5) % 2 else hero[:, ::-1]  # 달리기 대신 좌우로 뒤집힘
                truth3.append((311, 185))
            writer.write(f)
        writer.release()
        _, p = path(scroll, 89, (64, 76), {0: truth3[0]})
        stuck = [n for n in range(90) if p[n][2] == GOOD and np.hypot(p[n][0] - truth3[n][0], p[n][1] - truth3[n][1]) > 38]
        assert len(stuck) <= 5, f"box drifted with the background but marked tracked: {stuck}"

        # 3) 키 박스로 도중부터 다시 계산: 처음부터 계산한 것과 같아야 함 (화면에서 고친 뒤 이어서 계산)
        keys = {0: (40, 130), 50: truth[50]}
        _, full = path(dash, 79, (28, 36), keys)
        _, part = path(dash, 79, (28, 36), keys, frm=50)
        assert all(part[n] == full[n] for n in range(50, 80)), "resume from key differs"

        # 4) 크롭 + 중복 제거: 고정 위치에서 정지 구간(1~29)은 건너뛰고 움직이는 구간은 저장
        job = {"cancel": False, "processed": 0, "out": []}
        run_crop(video, tmp / "crop", 0, 59, 1, (30, 30), [(35, 115)] * 60, True, 30.0, job)
        saved = [f["n"] for f in job["out"]]
        assert saved[0] == 0 and not any(1 <= n <= 29 for n in saved), saved
        assert sum(30 <= n <= 59 for n in saved) >= 5, saved
        img = cv2.imdecode(np.fromfile(str(tmp / "crop" / "000000.png"), np.uint8), cv2.IMREAD_COLOR)
        assert img.shape == (30, 30, 3), img.shape

        # 5) 취소
        job = {"cancel": True, "processed": 0, "out": []}
        run_crop(video, tmp / "cancel", 0, 119, 1, (30, 30), [(35, 115)] * 120, False, 30.0, job)
        assert job["out"] == []

        # 6) GIF: 홀수 크기, 프레임마다 다른 지연, 투명 배경. 프레임 PNG로 풀어 영상처럼 추적·크롭
        anim = cv2.Animation()
        frames = []
        for i in range(12):
            f = np.zeros((41, 63, 4), np.uint8)
            f[10:20, 5 + i * 4:15 + i * 4] = (0, 0, 230, 255)
            frames.append(f)
        anim.frames, anim.durations = frames, [80, 0, 120] * 4
        ok, buf = cv2.imencodeanimation(".gif", anim)
        assert ok, "gif encode failed"
        global VIDEO_DIR
        VIDEO_DIR, saved_dir = tmp / "videos", VIDEO_DIR
        try:
            vid = add_gif(buf.tobytes(), "test.gif")
            meta = videos.pop(vid)
            assert (meta["frame_count"], meta["width"], meta["height"]) == (12, 63, 41), meta
            assert meta["delays"][:3] == [80, 100, 120] and meta["fps"] == 10.0, meta
            job, p = path(source_of(meta), 11, (10, 10), {0: (10, 15)})
            assert all(abs(p[n][0] - (10 + n * 4)) <= 2 for n in range(12)), p
            job = {"cancel": False, "processed": 0, "out": []}
            run_crop(source_of(meta), tmp / "gifcrop", 0, 11, 1, (10, 10), [p[n][:2] for n in range(12)], False,
                     meta["fps"], job)
            crop = cv2.imdecode(np.fromfile(str(tmp / "gifcrop" / "000011.png"), np.uint8), cv2.IMREAD_UNCHANGED)
            assert crop.shape == (10, 10, 4) and crop[:, :, 3].mean() > 200, crop.shape  # 투명도 유지
        finally:
            VIDEO_DIR = saved_dir
        print(f"tracking offset {off:.1f}px, dedupe saved {len(saved)}/60, dash ok, gif ok")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 빌드에 deno와 yt-dlp-ejs(유튜브 JS 해석기)가 들어 있고 실제로 쓸 수 있는지
    import yt_dlp_ejs.yt.solver
    assert len(yt_dlp_ejs.yt.solver.core()) > 1000, "yt-dlp-ejs solver missing"
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        rt = ydl._js_runtimes["deno"].info
    assert rt, f"deno not usable: {DENO}"
    print(f"deno {rt.version} ({DENO})")
    if online:
        # JS 런타임이 없어도 1080p가 나올 때가 있어, "JavaScript runtime" 같은 yt-dlp 경고가 없는지도 봅니다.
        warns = []
        log = type("Log", (), {"debug": print, "info": print, "warning": warns.append, "error": warns.append})
        with yt_dlp.YoutubeDL({**YDL_OPTS, "logger": log}) as ydl:
            info = ydl.extract_info("https://www.youtube.com/watch?v=aqz-KE-bpKQ", download=False)
        assert not warns, warns
        hd = [f["format_id"] for f in info["formats"] if (f.get("vcodec") or "").startswith("avc1")
              and (f.get("height") or 0) >= 1080 and f.get("protocol") == "https"]
        assert hd, "no 1080p avc1 https format (deno/ejs not working?)"
        print(f"online ok: chosen format {info['format_id']} ({info.get('height')}p {info.get('vcodec')}), 1080p avc1: {hd}")
    print("selftest ok")


# ---------------------------------------------------------------- 창과 수명

# 창이 뜨고 화면을 불러오기 전까지 보이는 배경 (흰 화면이 번쩍이지 않게, index.html의 --bg와 같은 색)
BLANK = "<style>html{background:#e8e8eb}@media(prefers-color-scheme:dark){html{background:#141416}}</style>"
KO = {"global.quitConfirmation": "종료할까요?", "global.ok": "확인", "global.quit": "종료", "global.cancel": "취소",
      "global.saveFile": "저장", "cocoa.menu.about": "Frame Board에 관하여", "cocoa.menu.services": "서비스",
      "cocoa.menu.view": "보기", "cocoa.menu.edit": "편집", "cocoa.menu.hide": "Frame Board 가리기",
      "cocoa.menu.hideOthers": "기타 가리기", "cocoa.menu.showAll": "모두 보기", "cocoa.menu.quit": "Frame Board 종료",
      "cocoa.menu.fullscreen": "전체 화면 시작", "cocoa.menu.cut": "오려두기", "cocoa.menu.copy": "복사하기",
      "cocoa.menu.paste": "붙여넣기", "cocoa.menu.selectAll": "전체 선택",
      "windows.fileFilter.allFiles": "모든 파일", "windows.fileFilter.otherFiles": "기타 파일"}


def unlock_120hz():
    """macOS 웹뷰는 기본으로 화면을 60fps 근처로만 그립니다(WebKit 기능 플래그). 끄면 ProMotion 화면에서 120Hz로 그립니다.

    비공개 API라 macOS가 바뀌어 없어지면 아무 일 없이 60fps로 남습니다. 화면을 불러오기 전에 불러야 합니다.
    """
    try:
        import WebKit
        from PyObjCTools import AppHelper
        from webview.platforms.cocoa import BrowserView

        def apply():
            try:
                prefs = BrowserView.instances[WINDOW.uid].webview.configuration().preferences()
                for f in WebKit.WKPreferences._features():
                    if f.key() == "PreferPageRenderingUpdatesNear60FPSEnabled":
                        prefs._setEnabled_forFeature_(False, f)
            except Exception as e:
                print(f"120Hz 설정 실패: {e}")
        AppHelper.callAfter(apply)  # 창 관련 일은 메인 스레드에서 (load_url도 같은 줄에 서므로 그보다 먼저 실행됨)
    except Exception as e:
        print(f"120Hz 설정 실패: {e}")


def bring_to_front():
    try:
        if os.name == "nt":  # pywebview의 restore()는 최대화한 창도 보통 크기로 되돌리므로, 내려가 있을 때만 SW_RESTORE
            import ctypes
            from webview.platforms.winforms import BrowserView
            hwnd, user32 = BrowserView.instances[WINDOW.uid].Handle.ToInt64(), ctypes.windll.user32
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)
            user32.SetForegroundWindow(hwnd)
        else:
            WINDOW.restore()
    except Exception as e:
        print(f"창을 앞으로 가져오지 못했습니다: {e}")
    WINDOW.show()


def save_before_close(window):
    """창을 닫기 직전에 보드를 바로 저장합니다(화면은 0.4초 모아서 저장하므로 직전 편집이 남지 않을 수 있음).

    closing 처리는 메인 스레드에서 돌아 여기서 JS를 기다리면 멈추므로, 한 번 닫기를 미루고 다른 스레드에서 저장한 뒤 닫습니다.
    """
    if getattr(window, "saved", False):
        return True

    def flush():
        t = threading.Thread(target=lambda: window.evaluate_js("window.frameBoardSave && frameBoardSave()"), daemon=True)
        t.start()
        t.join(1.5)  # 화면이 응답하지 않아도 닫히게
        window.saved = True
        window.destroy()
    threading.Thread(target=flush, daemon=True).start()
    return False


def run_app_window():
    """운영체제 기본 웹뷰(macOS WKWebView, Windows WebView2)로 프로그램 자체의 창을 띄우고, 창이 닫히면 돌아옵니다.

    Dock/작업 표시줄 아이콘이 이 프로그램이고, 아이콘을 누르면 이 창이 앞으로 옵니다. 웹뷰를 쓸 수 없으면 False.
    """
    global WINDOW
    try:
        import webview
        if os.name == "nt":
            from webview.platforms import winforms
            if not winforms.is_chromium:  # WebView2가 없으면 pywebview는 옛 IE 엔진으로 내려가므로 쓰지 않음
                return False
    except Exception as e:
        print(f"앱 창을 쓸 수 없어 브라우저 창으로 엽니다: {e}")
        return False
    webview.settings["ALLOW_DOWNLOADS"] = True  # 복사가 막혔을 때 PNG 파일로 저장하는 경로
    mac = sys.platform == "darwin"
    if mac:
        try:  # 메뉴 이름 뒤에 앱 이름을 또 붙이지 않게 (KO에 이미 들어 있음)
            from webview.platforms.cocoa import BrowserView
            BrowserView._append_app_name = lambda self, val: val
        except Exception:
            pass
    # macOS는 120Hz 설정을 화면을 불러오기 전에 해야 하므로 빈 화면부터. Windows는 바로 (빈 화면이 '뒤로 가기' 기록에 남음)
    WINDOW = webview.create_window("Frame Board", html=BLANK if mac else None, url=None if mac else URL,
                                   width=1440, height=900, min_size=(900, 600))
    WINDOW.events.closing += save_before_close

    def ready():
        if mac:
            unlock_120hz()
            WINDOW.load_url(URL)
    try:
        # private_mode=False: 보드는 localStorage에 있으므로 다음 실행에도 남아 있어야 합니다.
        webview.start(ready, private_mode=False, storage_path=str(DATA / "webview"), localization=KO)
    except Exception as e:
        print(f"앱 창을 띄우지 못해 브라우저 창으로 엽니다: {e}")
        WINDOW = None
        return False
    return True


def open_window():
    """앱 창을 쓸 수 없을 때: Edge/Chrome을 주소창 없는 창으로 열고 Popen을 돌려줍니다. 없으면 기본 브라우저로 열고 None."""
    if os.name == "nt":
        env = os.environ.get
        found = [rf"{env('ProgramFiles(x86)', '')}\Microsoft\Edge\Application\msedge.exe",
                 rf"{env('ProgramFiles', '')}\Microsoft\Edge\Application\msedge.exe",
                 rf"{env('ProgramFiles', '')}\Google\Chrome\Application\chrome.exe",
                 rf"{env('LOCALAPPDATA', '')}\Google\Chrome\Application\chrome.exe"]
    elif sys.platform == "darwin":
        found = [f"{root}/{app}.app/Contents/MacOS/{app}" for app in ("Google Chrome", "Microsoft Edge")
                 for root in ("/Applications", str(Path.home() / "Applications"))]
    else:
        found = []
    for exe in found:
        if os.path.isfile(exe):
            try:
                # 전용 프로필 폴더 = 사용자의 브라우저와 별개인, 이 프로그램 전용 브라우저 인스턴스
                return subprocess.Popen(
                    [exe, f"--app={URL}", f"--user-data-dir={DATA / 'browser-profile'}", "--no-first-run",
                     "--no-default-browser-check", "--disable-sync", "--window-size=1440,900"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass
    import webbrowser
    webbrowser.open(URL)
    return None


def exit_when_closed(browser):
    """(브라우저 창일 때) /api/alive 연결이 한 번 열린 뒤 모두 닫힌 채 3초가 지나면 종료합니다. 새로고침은 1초 안에 다시 붙습니다."""
    idle, t0 = 0, time.time()
    while idle < 6:
        time.sleep(0.5)
        with LOCK:  # 창이 60초 안에 한 번도 연결되지 않아도 (창을 바로 닫은 경우) 숨은 채 남지 않고 종료
            idle = idle + 1 if (alive["seen"] or time.time() - t0 > 60) and alive["n"] == 0 else 0
    if browser and sys.platform == "darwin":  # Windows의 terminate는 강제 종료라 Edge가 스스로 닫히게 둡니다.
        browser.terminate()  # macOS의 Chrome은 마지막 창을 닫아도 계속 실행되어 있으므로 직접 닫습니다.


def alert(msg):
    """창 없는 빌드에는 콘솔이 없으므로 운영체제 알림창으로 보여 줍니다."""
    print(msg)
    if FROZEN and os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "Frame Board", 0x10)
    elif FROZEN and sys.platform == "darwin":
        subprocess.run(["osascript", "-e", "on run argv", "-e",
                        'display alert "Frame Board" message (item 1 of argv) as critical', "-e", "end run", msg])


# ---------------------------------------------------------------- main

def main():
    if FROZEN and os.name == "nt":
        # 창 없는 exe를 실행하면 Windows는 첫 창을 기다리며 몇 초간 "작업 중" 마우스 커서를 보여 줍니다.
        # 메시지 큐를 한 번 들여다보면 시작이 끝난 것으로 보고 커서를 바로 되돌립니다. (MSG 구조체 크기 48바이트)
        import ctypes
        ctypes.windll.user32.PeekMessageW(ctypes.create_string_buffer(64), None, 0, 0, 0)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if "--selftest" in sys.argv or "--selftest-online" in sys.argv:
        try:
            selftest("--selftest-online" in sys.argv)
        except Exception:
            import traceback
            traceback.print_exc()
            sys.exit(1)
        sys.exit(0)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    try:
        server = Server((HOST, PORT), Handler)
    except OSError as e:
        try:
            # 시스템 프록시를 거치지 않도록 빈 ProxyHandler를 씁니다.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(URL + "/api/videos", timeout=2) as r:
                running = "videos" in json.load(r)
        except Exception:
            running = False
        if not running:
            alert(f"{PORT}번 포트를 사용할 수 없어 실행하지 못했습니다. 다른 프로그램을 끄거나 컴퓨터를 재시작해 주세요. ({e})")
            sys.exit(1)
        print(f"Frame Board가 이미 실행 중입니다. 창을 앞으로 가져옵니다: {URL}")
        try:
            if os.name == "nt":  # 실행 중인 쪽이 창을 앞으로 가져올 수 있게 허락 (Windows는 다른 프로세스의 포커스 가져가기를 막음)
                import ctypes
                ctypes.windll.user32.AllowSetForegroundWindow(-1)
            req = urllib.request.Request(URL + "/api/show", data=b"{}", headers={"X-Frame-Board": "1"})
            with opener.open(req, timeout=5) as r:
                has_window = json.load(r).get("window")
        except Exception:
            has_window = False
        if not has_window:
            open_window()  # 브라우저 창 방식으로 떠 있으면, 그 전용 브라우저에 새 창이 열립니다.
        return
    load_videos()
    # 창보다 서버가 먼저: 서버는 이미 바인드되어 있어 창의 첫 요청은 바로 처리됩니다. 무거운 라이브러리는 창을 띄운 뒤 불러옵니다.
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=load_libs, daemon=True).start()
    print(f"Frame Board 실행 중: {URL}  (창을 닫으면 종료됩니다)", flush=True)
    try:
        if not run_app_window():
            if sys.platform == "darwin":
                try:  # 창이 브라우저라 이 프로그램에는 Cocoa 이벤트 루프가 없음: Dock 아이콘이 '응답 없음'으로 보이지 않게 숨김
                    import AppKit
                    AppKit.NSApplication.sharedApplication().setActivationPolicy_(
                        AppKit.NSApplicationActivationPolicyAccessory)
                except Exception:
                    pass
            exit_when_closed(open_window())
    except KeyboardInterrupt:
        pass
    with LOCK:
        for job in jobs.values():
            job["cancel"] = True
    os._exit(0)  # 남은 스레드(영상 중계, 추출)를 기다리지 않고 바로 끝냄


if __name__ == "__main__":
    main()
