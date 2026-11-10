"""Frame Board: 유튜브 영상에서 캐릭터 애니메이션 프레임을 뽑아 보는 로컬 도구.

실행: uv run app.py          (앱 창이 http://127.0.0.1:8765 를 엽니다)
점검: uv run app.py --selftest   (--selftest-online: 유튜브 고화질 포맷까지 확인)
"""
import http.client
import json
import os
import re
import select
import shutil
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
from urllib.parse import parse_qs, urlparse

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
STREAM_TTL = 3 * 3600  # 유튜브 스트림 주소는 몇 시간 뒤 만료됩니다.

LOCK = threading.Lock()
videos = {}  # id -> VideoMeta (+ "source": 유튜브 페이지 주소)
streams = {}  # id -> {"url", "headers", "size", "at"}: 영상을 통째로 받지 않고 필요한 부분만 중계합니다.
PROBE_LOCK = threading.Lock()  # ponytail: 주소 갱신은 전체에서 한 번에 하나, 영상 여러 개를 동시에 쓰게 되면 영상별 잠금으로
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
        if meta.get("source") and meta.get("status") == "ready":  # 예전 다운로드 방식 기록은 건너뜀
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

def run_track(video_path, out_dir, start, end, step, box, follow, dedupe, job):
    """start~end 프레임을 순서대로 읽으며 box 크기 그대로 잘라 PNG로 저장합니다.

    job: {"cancel": bool, "processed": int, "frames": list} — 진행 상황을 여기에 기록합니다.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(video_path if isinstance(video_path, str) else cvp(video_path))
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        cap.set(cv2.CAP_PROP_POS_FRAMES, start)
        ok, frame = cap.read()
        if not ok:
            raise RuntimeError("시작 프레임을 읽을 수 없습니다.")
        fh, fw = frame.shape[:2]
        x, y, w, h = box
        w, h = max(1, min(w, fw)), max(1, min(h, fh))
        x, y = min(max(x, 0), fw - w), min(max(y, 0), fh - h)
        cx, cy = x + w / 2, y + h / 2
        tracker = None
        if follow:
            tracker = cv2.TrackerCSRT_create()
            try:
                tracker.init(frame, (x, y, w, h))
            except cv2.error:  # 1~2px처럼 너무 작은 영역은 따라가기 없이 고정
                tracker = None
        last = None
        n = start
        while not job["cancel"]:
            if tracker is not None and n > start:
                try:
                    ok, (bx, by, bw, bh) = tracker.update(frame)
                except cv2.error:
                    ok = False
                if ok:  # 실패하면 마지막 중심을 유지
                    cx, cy = bx + bw / 2, by + bh / 2
            if (n - start) % step == 0:
                x0 = min(max(round(cx - w / 2), 0), fw - w)
                y0 = min(max(round(cy - h / 2), 0), fh - h)
                crop = frame[y0:y0 + h, x0:x0 + w]
                gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
                if not (dedupe and last is not None and cv2.absdiff(gray, last).mean() < DEDUPE_DIFF):
                    last = gray
                    name = f"{n:06d}.png"
                    cv2.imencode(".png", crop)[1].tofile(str(out_dir / name))  # imwrite는 Windows 한글 경로 불가
                    with LOCK:
                        job["frames"].append({"n": n, "t": round(n / fps, 4),
                                              "src": f"/files/jobs/{out_dir.name}/{name}", "w": w, "h": h})
            with LOCK:
                job["processed"] = n - start + 1
            if n >= end:
                break
            ok, frame = cap.read()
            if not ok:
                break
            n += 1
    finally:
        cap.release()


def run_job(job, video_path, args):
    LIBS_READY.wait()
    try:
        run_track(video_path, JOB_DIR / job["id"], job=job, **args)
        status, error = ("cancelled" if job["cancel"] else "done"), None
        if status == "done" and job["processed"] < job["total"]:  # 영상 끝이나 네트워크 끊김으로 일찍 멈춤
            error = f"영상 읽기가 중간에 멈춰 {job['processed']}/{job['total']} 프레임까지만 확인했습니다."
    except Exception as e:
        status, error = "error", f"프레임 추출 중 오류가 발생했습니다. ({e})"[:300]
    with LOCK:
        job.update(status=status, error=error)


def start_job(vid, body):
    with LOCK:
        meta = dict(videos.get(vid) or {})
    if not meta:
        raise LookupError("영상을 찾을 수 없습니다.")
    if meta["status"] != "ready":
        raise ValueError("영상이 아직 준비되지 않았습니다.")
    try:
        start, end, step = int(body["start"]), int(body["end"]), int(body.get("step", 1))
        box = [int(v) for v in body["box"]]
        follow, dedupe = bool(body.get("follow", True)), bool(body.get("dedupe", False))
        assert len(box) == 4
    except (KeyError, TypeError, ValueError, AssertionError):
        raise ValueError("요청 형식이 올바르지 않습니다.")
    if step < 1 or start < 0 or end < start:
        raise ValueError("프레임 범위가 올바르지 않습니다.")
    if box[2] < 1 or box[3] < 1:
        raise ValueError("먼저 영상 위에 영역을 그려 주세요.")
    if (end - start) // step + 1 > MAX_FRAMES:
        raise ValueError(f"한 번에 최대 {MAX_FRAMES}장까지 뽑을 수 있습니다. 범위를 줄이거나 간격을 늘려 주세요.")
    job = {"id": uuid.uuid4().hex[:12], "status": "running", "processed": 0, "total": end - start + 1,
           "error": None, "frames": [], "cancel": False}
    with LOCK:
        jobs[job["id"]] = job
    args = {"start": start, "end": end, "step": step, "box": box, "follow": follow, "dedupe": dedupe}
    video_path = f"{URL}{meta['url']}"  # 자기 서버의 중계 주소: 필요한 구간만 받아 읽습니다.
    threading.Thread(target=run_job, args=(job, video_path, args), daemon=True).start()
    return job["id"]


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
        pos, fails = start, 0
        while pos <= end and fails < 5:
            got = 0
            try:  # 헤더를 이미 보냈으므로 어떤 실패든 재시도로 처리합니다(오류 응답을 섞어 쓰면 안 됨).
                r = fetch(vid, pos, min(pos + CHUNK - 1, end))
                if r.status != 206 or not (r.headers.get("Content-Range") or "").startswith(f"bytes {pos}-"):
                    r.close()
                    r = None
            except Exception:
                r = None
            if r:
                with r:
                    while True:
                        try:
                            chunk = r.read(256 << 10)
                        except (OSError, http.client.HTTPException):
                            break  # 유튜브 쪽 끊김: 받은 위치부터 다시
                        if not chunk:
                            break
                        self.wfile.write(chunk)  # 브라우저 쪽 끊김은 ConnectionError로 올라가 safe()가 처리
                        pos += len(chunk)
                        got += len(chunk)
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

    def do_GET(self):
        self.safe(self.route_get)

    def do_POST(self):
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
                    res.update(frames=job["frames"][since:], count=len(job["frames"]))
            return self.send_json(res) if job else self.error(404, "작업을 찾을 수 없습니다.")
        if m := re.fullmatch(r"/media/([A-Za-z0-9_-]+)\.(mp4|webm)", path):
            return self.send_stream(m[1], MEDIA_TYPES[m[2]])
        if m := re.fullmatch(r"/files/jobs/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+\.png)", path):
            return self.send_file(JOB_DIR / m[1] / m[2], "image/png", "max-age=31536000")
        self.error(404, "페이지를 찾을 수 없습니다.")

    def route_post(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/videos":
                url = str(self.read_body().get("url") or "").strip()
                if not url:
                    raise ValueError("영상 주소를 입력해 주세요.")
                return self.send_json({"id": add_video(url)})
            if m := re.fullmatch(r"/api/videos/([A-Za-z0-9_-]+)/track", path):
                return self.send_json({"job_id": start_job(m[1], self.read_body())})
            if m := re.fullmatch(r"/api/jobs/([0-9a-f]+)/cancel", path):
                with LOCK:
                    job = jobs.get(m[1])
                    if job:
                        job["cancel"] = True
                return self.send_json({"ok": True}) if job else self.error(404, "작업을 찾을 수 없습니다.")
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

        # 1) 따라가기 + 2프레임 간격
        job = {"cancel": False, "processed": 0, "frames": []}
        run_track(video, tmp / "follow", 0, 119, 2, [20, 100, 30, 30], True, False, job)
        assert job["processed"] == 120, job["processed"]
        assert [f["n"] for f in job["frames"]] == list(range(0, 120, 2)), "saved frame list"
        assert len(list((tmp / "follow").glob("*.png"))) == 60
        for f in job["frames"]:
            img = cv2.imdecode(np.fromfile(str(tmp / "follow" / f"{f['n']:06d}.png"), np.uint8), cv2.IMREAD_COLOR)
            assert img.shape == (30, 30, 3), img.shape
        # 마지막 크롭 안에서 사각형(빨강+노랑) 중심이 크롭 중심 근처여야 함
        ys, xs = np.nonzero((img[:, :, 2] > 150) & (img[:, :, 0] < 100))
        assert len(xs) > 100, "square lost"
        off = np.hypot(xs.mean() - 14.5, ys.mean() - 14.5)
        assert off < 10, f"tracking off by {off:.1f}px"

        # 2) 고정 박스 + 중복 제거: 정지 구간(1~29)은 건너뛰고 움직이는 구간은 저장
        job = {"cancel": False, "processed": 0, "frames": []}
        run_track(video, tmp / "dedupe", 0, 59, 1, [20, 100, 30, 30], False, True, job)
        saved = [f["n"] for f in job["frames"]]
        assert saved[0] == 0 and not any(1 <= n <= 29 for n in saved), saved
        assert sum(30 <= n <= 59 for n in saved) >= 5, saved

        # 3) 취소
        job = {"cancel": True, "processed": 0, "frames": []}
        run_track(video, tmp / "cancel", 0, 119, 1, [20, 100, 30, 30], True, False, job)
        assert job["frames"] == []
        print(f"tracking offset {off:.1f}px, dedupe saved {len(saved)}/60")
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

def open_window():
    """Edge/Chrome을 주소창 없는 앱 창으로 열고 Popen을 돌려줍니다. 없으면 기본 브라우저로 열고 None."""
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
    """창(/api/alive 연결)이 한 번 열린 뒤 모두 닫힌 채 3초가 지나면 종료합니다. 새로고침은 1초 안에 다시 붙습니다."""
    idle, t0 = 0, time.time()
    while idle < 6:
        time.sleep(0.5)
        with LOCK:  # 창이 60초 안에 한 번도 연결되지 않아도 (창을 바로 닫은 경우) 숨은 채 남지 않고 종료
            idle = idle + 1 if (alive["seen"] or time.time() - t0 > 60) and alive["n"] == 0 else 0
    with LOCK:
        for job in jobs.values():
            job["cancel"] = True
    if browser and sys.platform == "darwin":  # Windows의 terminate는 강제 종료라 Edge가 스스로 닫히게 둡니다.
        browser.terminate()  # macOS의 Chrome은 마지막 창을 닫아도 계속 실행되어 있으므로 직접 닫습니다.
    os._exit(0)


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
            import urllib.request
            # 시스템 프록시를 거치지 않도록 빈 ProxyHandler를 씁니다.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(URL + "/api/videos", timeout=2) as r:
                running = "videos" in json.load(r)
        except Exception:
            running = False
        if not running:
            alert(f"{PORT}번 포트를 사용할 수 없어 실행하지 못했습니다. 다른 프로그램을 끄거나 컴퓨터를 재시작해 주세요. ({e})")
            sys.exit(1)
        print(f"Frame Board가 이미 실행 중입니다. 창을 엽니다: {URL}")
        open_window()  # 이미 떠 있는 전용 브라우저에 새 창이 열립니다.
        return
    load_videos()
    browser = open_window()  # 서버는 이미 바인드됨: 창의 첫 요청은 serve_forever가 시작되면 바로 처리됩니다.
    threading.Thread(target=load_libs, daemon=True).start()
    threading.Thread(target=exit_when_closed, args=(browser,), daemon=True).start()
    print(f"Frame Board 실행 중: {URL}  (앱 창을 닫으면 종료됩니다)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
