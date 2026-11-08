"""云音乐 —— 单文件后端（Windows 可直接运行 / 可打包为 exe）

功能：
  * 在线音乐播放器：上传本地音乐文件，生成播放列表，浏览器内播放
  * 上传走 base64 + JSON（避免 multipart 解析，最大 200MB / 曲，可在 config 调）
  * 曲目与播放列表都存在本地 <data>/ 目录，重启不丢
  * 音频用 Range 流式返回，支持拖动进度、续播
  * 同端口托管播放页（无需 nginx）；挂在 nginx 子路径 /music/ 下时复用门户统一登录

接口（前端用）：
  GET  /api/health            探活 + 统计
  GET  /api/meta              版本 / 登录用户 / 门户链接
  GET  /api/session           当前用户
  GET  /api/tracks            曲目库
  POST /api/upload            上传（JSON：{name, mime, b64}），返回曲目记录
  PATCH /api/tracks/<id>      更新时长/标题/艺人（浏览器读到元数据后回写）
  DELETE /api/tracks/<id>     删除曲目（同时从各播放列表移除）
  GET  /api/track/<id>        音频文件（支持 Range）
  GET  /api/playlists         播放列表清单
  POST /api/playlists         新建（{name}）
  DELETE /api/playlists/<id>  删除列表
  POST /api/playlists/<id>/tracks   添加曲目（{track_id}）
  DELETE /api/playlists/<id>/tracks/<tid>  移除曲目
  POST /api/playlists/<id>/reorder      重排（{from, to}）

启动：CloudMusic.exe
      CloudMusic.exe --port 8093
"""

from __future__ import annotations

import base64
import glob
import hashlib
import hmac
import json
import mimetypes
import os
import posixpath
import random
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import hot_reload
import logger
import portal_auth

VERSION = "1.1.3"
IS_FROZEN = getattr(sys, "frozen", False)
BUNDLE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.dirname(os.path.abspath(sys.executable)) if IS_FROZEN else os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BUNDLE_DIR, "static")
DATA_DIR = os.path.join(APP_DIR, "data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8093,
    "username": "admin",
    "password": "",
    "session_days": 30,
    "log_dir": "logs",
    "log_days": 14,
    # 放在 nginx 后面时：信任反向代理带来的登录用户（由 nginx 做登录门禁），
    # 只有来自 127.0.0.1 的请求才会被采信，避免伪造请求头绕过登录
    "trust_proxy_auth": False,
    # 右上角「首页 / 状态」按钮的地址；挂在前缀下时自动回到上一级
    "portal_home": "",
    "portal_status": "",
    # 统一入口登录：挂在 nginx 前缀下时校验门户的 cm_session Cookie
    "portal_auth_url": "",
    # 单首上传上限（MB），默认 200；超过直接拒绝
    "max_upload_mb": 200,
    # 数据/音乐文件保存目录。留空 = <exe 所在目录>/data；
    # 可填绝对路径（如 D:\\CloudMedia\\music）把大文件放到别的盘，
    # 也可填相对路径（相对于 exe 所在目录解析，如 "storage"）
    "data_dir": "",
    # ffmpeg 可执行文件路径。留空则自动依次在「exe 同级目录」「exe 同级/ffmpeg/」
    # 「data_dir」「PATH」里找 ffmpeg.exe，所以把 ffmpeg.exe 放到 exe 旁边即可，无需配置。
    # 装了 ffmpeg 后：上传的非 mp3 会自动转成通用 mp3，播放页也才能切换码率。
    "ffmpeg_path": "",
    # 服务端自动转码总开关：上传后把 wav / flac / aiff 等浏览器兼容性差的格式
    # 后台转成通用 mp3 缓存（data/transcodes/<id>_auto.mp3），播放时优先用缓存。
    # 原文件始终保留，不改动曲目记录；关闭则完全回退到原文件直出。
    "auto_transcode": True,
    # 自动转码的目标码率（kbps）；已转码缓存不会因改这个值重建，除非手动删除缓存
    "auto_transcode_kbps": 192,
}

# 这些扩展名需要服务端转成 mp3：浏览器对它们的支持不一致（尤其 wav 的 24/32bit float、
# aiff / caf / flac 在部分浏览器与手机 WebView 里无法解码），转码后 100% 可播。
# mp3 / m4a / aac / ogg / opus / webm 浏览器原生支持，保持原文件直出，省时省空间。
NEEDS_TRANSCODE = {".wav", ".flac", ".aiff", ".caf", ".mp4"}

# 播放页可选的码率档位（kbps）。仅当服务器能找到 ffmpeg 时才会真正转码
BITRATE_STEPS = [64, 96, 128, 192, 256, 320]

# 允许上传的音频类型（按扩展名兜底，mime 仅作参考）
ALLOWED_EXT = {".mp3", ".wav", ".ogg", ".oga", ".flac", ".m4a", ".aac",
               ".opus", ".webm", ".caf", ".aiff", ".mp4"}
# 播放音轨时按「文件扩展名」强制规范化 Content-Type，绝不信任上传时浏览器给的 File.type。
# 浏览器对 .wav 上报的 File.type 五花八门（audio/x-wav / audio/vnd.wave / application/octet-stream
# / 空串），原样返回会让 <audio> 判定为不支持的格式并报「播放器错误」；mp3 通常恒为 audio/mpeg，
# 所以历史上只有 wav 中招。这里统一映射到浏览器公认的标准类型。
AUDIO_MIME = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".oga": "audio/ogg", ".flac": "audio/flac", ".m4a": "audio/mp4",
    ".aac": "audio/aac", ".opus": "audio/ogg", ".webm": "audio/webm",
    ".caf": "audio/x-caf", ".aiff": "audio/aiff", ".mp4": "audio/mp4",
}
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".webmanifest": "application/manifest+json",
    ".woff2": "font/woff2",
    ".map": "application/json",
}

STATE: dict = {
    "tracks": [],
    "playlists": [],
    "started_at": time.time(),
    "config": dict(DEFAULTS),
}
STATE_LOCK = threading.Lock()
TRANSCODE_LOCK = threading.Lock()

SESSIONS: dict[str, dict] = {}
LOGIN_FAILS: dict[str, list] = {}
COOKIE_NAME = "cmu_sid"
PUBLIC_PATHS = {"/login", "/login.html", "/api/login", "/api/health", "/api/meta",
                "/api/assets", "/icon.svg", "/favicon.ico"}
HOT = None   # main() 里初始化：静态资源外置 + config.json 热重载


def log(msg: str) -> None:
    logger.get().info(msg)


def log_warn(msg: str) -> None:
    logger.get().warn(msg)


def log_err(msg: str) -> None:
    logger.get().error(msg)


# ---------------------------------------------------------------- 配置
def load_config() -> dict:
    cfg = dict(DEFAULTS)
    path = os.path.join(APP_DIR, "config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                cfg.update({k: v for k, v in json.load(fh).items() if k in DEFAULTS})
        except Exception as exc:
            log(f"config.json 读取失败，使用默认配置：{exc}")
    if os.environ.get("PORT"):
        try:
            cfg["port"] = int(os.environ["PORT"])
        except ValueError:
            pass
    if os.environ.get("HOST"):
        cfg["host"] = os.environ["HOST"]
    return parse_args(cfg)


def parse_args(cfg: dict) -> dict:
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        nxt = args[i + 1] if i + 1 < len(args) else None
        if arg in ("--port", "-p") and nxt:
            try:
                cfg["port"] = int(nxt)
            except ValueError:
                pass
            i += 2
            continue
        if arg == "--host" and nxt:
            cfg["host"] = nxt
            i += 2
            continue
        if arg in ("--version", "-v"):
            print(f"CloudMusic {VERSION}")
            raise SystemExit(0)
        if arg in ("--help", "-h"):
            print("用法：CloudMusic [--port 8093] [--host 127.0.0.1]")
            raise SystemExit(0)
        i += 1
    return cfg


# ---------------------------------------------------------------- 存储
def tracks_path() -> str:
    return os.path.join(DATA_DIR, "tracks.json")


def playlists_path() -> str:
    return os.path.join(DATA_DIR, "playlists.json")


def load_store() -> None:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    try:
        with open(tracks_path(), "r", encoding="utf-8") as fh:
            STATE["tracks"] = json.load(fh) or []
    except Exception:
        STATE["tracks"] = []
    try:
        with open(playlists_path(), "r", encoding="utf-8") as fh:
            STATE["playlists"] = json.load(fh) or []
    except Exception:
        STATE["playlists"] = []


def save_tracks() -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = tracks_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(STATE["tracks"], fh, ensure_ascii=False)
        os.replace(tmp, tracks_path())
    except Exception as exc:
        log_err(f"曲目库保存失败：{exc}")


def save_playlists() -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = playlists_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(STATE["playlists"], fh, ensure_ascii=False)
        os.replace(tmp, playlists_path())
    except Exception as exc:
        log_err(f"播放列表保存失败：{exc}")


def find_track(tid: str) -> dict | None:
    for t in STATE["tracks"]:
        if t["id"] == tid:
            return t
    return None


def find_playlist(pid: str) -> dict | None:
    for p in STATE["playlists"]:
        if p["id"] == pid:
            return p
    return None


def purge_track_from_playlists(tid: str) -> None:
    changed = False
    for p in STATE["playlists"]:
        if tid in p["tracks"]:
            p["tracks"] = [x for x in p["tracks"] if x != tid]
            changed = True
    if changed:
        save_playlists()


def safe_name(name: str) -> str:
    name = (name or "未命名").strip()
    for ch in ('\\', '/', ':', '*', '?', '"', '<', '>', '|', '\0'):
        name = name.replace(ch, "_")
    return name[:120] or "未命名"


def parse_meta(filename: str) -> tuple[str, str, str]:
    """从文件名猜 艺人 - 曲名（没有 ID3 时不依赖外部工具）。"""
    base = os.path.splitext(filename)[0]
    if " - " in base:
        artist, title = base.split(" - ", 1)
        return title.strip(), artist.strip(), ""
    return base.strip(), "", ""


# ---------------------------------------------------------------- 会话
def sessions_path() -> str:
    return os.path.join(DATA_DIR, "sessions.json")


def load_sessions() -> None:
    path = sessions_path()
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        now = time.time()
        for key, item in (data or {}).items():
            if isinstance(item, dict) and item.get("exp", 0) > now:
                SESSIONS[key] = item
    except Exception:
        pass


def save_sessions() -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = sessions_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(SESSIONS, fh)
        os.replace(tmp, sessions_path())
    except Exception:
        pass


def issue_session(user: str) -> str:
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    days = float(STATE["config"].get("session_days") or 30)
    SESSIONS[digest] = {"user": user, "exp": time.time() + days * 86400}
    save_sessions()
    return token


def session_ok(token: str) -> bool:
    if not token:
        return False
    digest = hashlib.sha256(token.encode()).hexdigest()
    item = SESSIONS.get(digest)
    if not item:
        return False
    if item.get("exp", 0) <= time.time():
        SESSIONS.pop(digest, None)
        save_sessions()
        return False
    return True


def session_record(token: str) -> dict | None:
    digest = hashlib.sha256((token or "").encode()).hexdigest()
    item = SESSIONS.get(digest)
    if not item or item.get("exp", 0) <= time.time():
        return None
    return {"digest": digest, "user": item.get("user", "")}


def ensure_credentials(cfg: dict) -> None:
    if not (cfg.get("username") or "").strip():
        cfg["username"] = "admin"
    if cfg.get("password"):
        return
    alphabet = "abcdefghjkmnpqrstuvwxyzACDEFGHJKLMNPQRSTUVWXYZ23456789"
    password = "".join(secrets.choice(alphabet) for _ in range(10))
    cfg["password"] = password
    path = os.path.join(APP_DIR, "config.json")
    data = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            data = {}
    data["username"] = cfg["username"]
    data["password"] = password
    data.setdefault("session_days", cfg.get("session_days", 30))
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        log(f"随机密码写入 config.json 失败：{exc}")
    log(f"未配置密码，已生成随机密码并写入 config.json（用户名 {cfg['username']}）")
    try:
        print(f"    本次生成的密码：{password}（仅显示在控制台，不写入日志文件）", flush=True)
    except Exception:
        pass


def fail_locked(ip: str) -> int:
    now = time.time()
    fails = [t for t in LOGIN_FAILS.get(ip, []) if now - t < 15 * 60]
    LOGIN_FAILS[ip] = fails
    if len(fails) >= 8:
        return int(15 * 60 - (now - fails[-1]))
    return 0


def note_fail(ip: str) -> None:
    LOGIN_FAILS.setdefault(ip, []).append(time.time())


def portal_links(prefix: str) -> dict:
    cfg = STATE["config"]
    home = str(cfg.get("portal_home") or "").strip()
    status = str(cfg.get("portal_status") or "").strip()
    if prefix:
        home = home or "../"
        status = status or "../status"
    return {"home": home, "status": status}


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "CloudMusic/" + VERSION
    protocol_version = "HTTP/1.1"

    def parse_request(self):
        # 每个请求开头检查 config.json 是否被改动（无变化时只有一次 stat）
        ok = super().parse_request()
        if ok and HOT:
            try:
                HOT.refresh()
            except Exception:
                pass
        return ok

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    def _json(self, data, code: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def log_message(self, fmt, *args):
        return

    # ---------- 登录态 ----------
    def cookie_token(self) -> str:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            key, _, value = part.strip().partition("=")
            if key == COOKIE_NAME:
                return value
        return ""

    def req_prefix(self) -> str:
        raw = (self.headers.get("X-Forwarded-Prefix") or "").strip().rstrip("/")
        return raw if raw.startswith("/") else ""

    def client_ip(self) -> str:
        ip = self.client_address[0]
        if ip in ("127.0.0.1", "::1") and STATE["config"].get("trust_proxy_auth"):
            real = (self.headers.get("X-Real-IP") or "").strip()
            if not real:
                real = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            if real:
                return real
        return ip

    def proxy_user(self) -> str:
        if not STATE["config"].get("trust_proxy_auth"):
            return ""
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return ""
        return (self.headers.get("X-Auth-User") or "").strip()

    def identity(self) -> dict | None:
        sess = session_record(self.cookie_token())
        if sess:
            return sess
        user = self.proxy_user()
        if user:
            return {"digest": hashlib.sha256(f"proxy:{user}".encode()).hexdigest(), "user": user}
        if self.req_prefix():
            name = portal_auth.user(self.headers.get("Cookie") or "",
                                    str(STATE["config"].get("portal_auth_url") or ""))
            if name:
                return {"digest": portal_auth.digest(name), "user": name}
        return None

    def authed(self) -> bool:
        return self.identity() is not None

    def portal_login(self) -> str:
        return "/login.html?next=" + urllib.parse.quote(self.req_prefix() + "/", safe="/")

    def redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", self.req_prefix() + location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def redirect_out(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def serve_login(self) -> None:
        # 云音乐把登录遮罩内联在 index.html 里，没有独立的 login.html；
        # 独立运行（未挂 nginx）被重定向到 /login 时直接回首页，别返回一段 JSON 错误。
        # 走 HOT.resolve_static，外部 static/ 里的首页也能热更新。
        full, _ext, _src = HOT.resolve_static("login.html") if HOT else (None, False, "")
        if not full:
            full, _ext, _src = HOT.resolve_static("index.html") if HOT else (None, False, "")
        if not full:
            return self._json({"error": "login_page_missing"}, 500)
        try:
            with open(full, "rb") as fh:
                body = fh.read()
        except Exception:
            return self._json({"error": "login_page_missing"}, 500)
        self._send(200, body, "text/html; charset=utf-8", {"Cache-Control": "no-store"})

    def do_login(self, payload: dict) -> None:
        ip = self.client_ip()
        wait = fail_locked(ip)
        if wait > 0:
            return self._json({"error": "locked", "message": f"尝试次数过多，请 {max(1, wait // 60)} 分钟后再试"}, 429)
        user = (payload.get("username") or "").strip()
        pwd = payload.get("password") or ""
        cfg = STATE["config"]
        ok_user = hmac.compare_digest(user.encode(), (cfg.get("username") or "").encode())
        ok_pass = hmac.compare_digest(pwd.encode(), (cfg.get("password") or "").encode())
        if not (ok_user and ok_pass):
            note_fail(ip)
            log_warn(f"登录失败：{ip} 尝试用户名 {user!r}")
            return self._json({"error": "bad_credentials", "message": "用户名或密码不正确"}, 401)
        LOGIN_FAILS.pop(ip, None)
        token = issue_session(user)
        max_age = int(float(cfg.get("session_days") or 30) * 86400)
        body = json.dumps({"ok": True, "user": user}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", f"{COOKIE_NAME}={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
        log(f"登录成功：{ip} / {user}")

    def do_logout(self) -> None:
        revoke_session(self.cookie_token())
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---------- 路由 ----------
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        try:
            self.route_get()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as exc:
            log(f"请求异常 {self.path}：{exc}")
            try:
                self._json({"error": "internal_error", "detail": str(exc)}, 500)
            except Exception:
                pass

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if self.req_prefix() and not self.authed():
            return self._json({"error": "unauthorized", "login": self.portal_login(),
                               "portal_login": True}, 401)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return self._json({"error": "bad_json"}, 400)

        if path == "/api/login":
            return self.do_login(payload)
        if path == "/api/logout":
            return self.do_logout()
        if not self.authed():
            return self._json({"error": "unauthorized", "login": "/login"}, 401)

        if path == "/api/upload":
            return self.do_upload(payload)
        if path == "/api/resort":
            return self.resort_tracks(payload)
        if path == "/api/playlists":
            return self.create_playlist(payload)
        if path.startswith("/api/playlists/") and path.endswith("/tracks"):
            pid = path.split("/")[3]
            return self.add_to_playlist(pid, payload)
        if path.endswith("/reorder"):
            pid = path.split("/")[3]
            return self.reorder_playlist(pid, payload)
        self._json({"error": "not_found"}, 404)

    def do_PATCH(self):
        path = urllib.parse.urlsplit(self.path).path
        if not self.authed():
            return self._json({"error": "unauthorized", "login": "/login"}, 401)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return self._json({"error": "bad_json"}, 400)
        if path.startswith("/api/tracks/"):
            tid = path.rsplit("/", 1)[-1]
            return self.patch_track(tid, payload)
        self._json({"error": "not_found"}, 404)

    def do_DELETE(self):
        path = urllib.parse.urlsplit(self.path).path
        if not self.authed():
            return self._json({"error": "unauthorized", "login": "/login"}, 401)
        if path.startswith("/api/tracks/"):
            tid = path.rsplit("/", 1)[-1]
            return self.delete_track(tid)
        if path.startswith("/api/playlists/") and "/tracks/" in path:
            pid, tid = path.split("/api/playlists/")[1].split("/tracks/")
            return self.remove_from_playlist(pid, tid)
        if path.startswith("/api/playlists/"):
            pid = path.rsplit("/", 1)[-1]
            return self.delete_playlist(pid)
        self._json({"error": "not_found"}, 404)

    # ---------- 上传 ----------
    def do_upload(self, payload: dict) -> None:
        name = safe_name(str(payload.get("name") or ""))
        mime = str(payload.get("mime") or "").strip()
        b64 = str(payload.get("b64") or "")
        if not name or not b64:
            return self._json({"error": "bad_request", "message": "缺少 name 或 b64"}, 400)
        ext = os.path.splitext(name)[1].lower()
        if ext not in ALLOWED_EXT:
            return self._json({"error": "unsupported_type",
                               "message": f"不支持的格式 {ext or '(无扩展名)'}，仅支持 " +
                               "/".join(sorted(ALLOWED_EXT))}, 415)
        try:
            data = base64.b64decode(b64)
        except Exception:
            return self._json({"error": "bad_base64"}, 400)
        max_bytes = int(STATE["config"].get("max_upload_mb") or 200) * 1024 * 1024
        if len(data) > max_bytes:
            return self._json({"error": "too_large",
                               "message": f"文件超过上限 {STATE['config'].get('max_upload_mb')}MB"}, 413)
        if len(data) < 64:
            return self._json({"error": "empty_file", "message": "文件为空"}, 400)

        tid = secrets.token_hex(8)
        fname = f"{tid}{ext}"
        try:
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            with open(os.path.join(UPLOAD_DIR, fname), "wb") as fh:
                fh.write(data)
        except Exception as exc:
            return self._json({"error": "write_failed", "detail": str(exc)}, 500)

        title, artist, _ = parse_meta(name)
        try:
            dur = float(payload.get("duration"))
            dur = round(dur, 3) if 0 < dur < 86400 else None
        except (TypeError, ValueError):
            dur = None
        rec = {
            "id": tid,
            "name": os.path.splitext(name)[0],
            "filename": fname,
            "ext": ext,
            "mime": mime or mimetypes.guess_type(fname)[0] or "application/octet-stream",
            "size": len(data),
            "title": title,
            "artist": artist,
            "album": "",
            "duration": dur,
            "bitrate": calc_bitrate(len(data), dur),
            "lyrics": "",
            "uploaded_at": int(time.time()),
        }
        with STATE_LOCK:
            STATE["tracks"].insert(0, rec)
            save_tracks()
        log(f"上传曲目：{name}（{len(data)//1024}KB）用户 {self.identity()['user'] if self.identity() else '?'}")
        self._json({"ok": True, "track": public_track(rec)}, 200)
        # 上传响应已返回，后台异步转码（wav/flac/aiff 等 → 通用 mp3），不拖慢上传
        if ext in NEEDS_TRANSCODE and auto_transcode_enabled():
            threading.Thread(target=transcode_worker, args=(tid,),
                             daemon=True, name="cm-transcode-upload").start()

    # ---------- 曲目 ----------
    def patch_track(self, tid: str, payload: dict) -> None:
        rec = find_track(tid)
        if not rec:
            return self._json({"error": "not_found"}, 404)
        for key in ("title", "artist", "album"):
            if key in payload and isinstance(payload[key], str):
                rec[key] = payload[key][:200]
        if "duration" in payload:
            try:
                d = float(payload["duration"])
                if 0 < d < 86400:
                    rec["duration"] = round(d, 3)
            except (TypeError, ValueError):
                pass
        if "lyrics" in payload and isinstance(payload["lyrics"], str):
            # LRC 文本统一换行、限制大小，避免把 tracks.json 撑爆
            rec["lyrics"] = payload["lyrics"][:128000].replace("\r\n", "\n")
        rec["bitrate"] = calc_bitrate(rec.get("size", 0), rec.get("duration"))
        with STATE_LOCK:
            save_tracks()
        self._json({"ok": True, "track": public_track(rec)}, 200)

    def resort_tracks(self, payload: dict) -> None:
        by = (payload.get("by") or "random")
        with STATE_LOCK:
            items = STATE["tracks"]
            if by == "random":
                # 随机重排：给每首一个随机的「上传时间」，再按时间降序排列，
                # 列表顺序即随机；每次点击都不同（满足「重写标准是随机」）。
                base = int(time.time())
                for t in items:
                    t["uploaded_at"] = base - random.randint(0, 10_000_000)
                items.sort(key=lambda t: t.get("uploaded_at", 0), reverse=True)
            elif by == "uploaded_at":
                items.sort(key=lambda t: t.get("uploaded_at", 0))
            elif by == "artist":
                items.sort(key=lambda t: (t.get("artist") or "").strip().lower())
            elif by == "name":
                items.sort(key=lambda t: (t.get("name") or "").strip().lower())
            else:  # 按 歌手 + 标题
                items.sort(key=lambda t: (
                    (t.get("artist") or "").strip().lower(),
                    (t.get("title") or t.get("name") or "").strip().lower(),
                ))
                base = int(time.time())
                n = len(items)
                for i, t in enumerate(items):
                    t["uploaded_at"] = base - i * 86400
            save_tracks()
            return self._json({"ok": True, "count": len(items), "by": by}, 200)

    def delete_track(self, tid: str) -> None:
        rec = find_track(tid)
        if not rec:
            return self._json({"error": "not_found"}, 404)
        with STATE_LOCK:
            STATE["tracks"] = [t for t in STATE["tracks"] if t["id"] != tid]
            save_tracks()
        purge_track_from_playlists(tid)
        try:
            os.remove(os.path.join(UPLOAD_DIR, rec["filename"]))
        except OSError:
            pass
        for cached in glob.glob(os.path.join(transcode_dir(), tid + "_*.mp3")):
            try:
                os.remove(cached)
            except OSError:
                pass
        log(f"删除曲目：{rec.get('name')}")
        self._json({"ok": True}, 200)

    def serve_track(self, tid: str, quality: str = "") -> None:
        rec = find_track(tid)
        if not rec:
            return self._json({"error": "not_found"}, 404)
        fpath = os.path.join(UPLOAD_DIR, rec["filename"])
        if not os.path.isfile(fpath):
            return self._json({"error": "file_missing"}, 404)
        # 以扩展名为准规范化 Content-Type（历史上传记录里可能存了非标准的 File.type）
        ext = str(rec.get("ext") or os.path.splitext(rec["filename"])[1] or "").lower()
        ctype = AUDIO_MIME.get(ext) or rec.get("mime") or "application/octet-stream"
        # 播放页选的码率档位：只降不升，且需要 ffmpeg；转码失败自动回退原文件
        try:
            q = int(quality)
        except (TypeError, ValueError):
            q = 0
        orig_kbps = rec.get("bitrate") or calc_bitrate(rec.get("size", 0), rec.get("duration")) or 0

        # 服务端自动转码缓存（wav/flac/aiff 等 → 通用 mp3）：有现成缓存就直接播，
        # 彻底绕开浏览器对原始格式解码能力的差异。这里刻意【不】同步触发转码，
        # 避免播放请求阻塞在转码锁上；缓存尚未就绪时先直出原文件并异步补转。
        if auto_transcode_enabled() and ext in NEEDS_TRANSCODE:
            cache = playable_cache(rec)
            if os.path.isfile(cache) and os.path.getsize(cache) > 0:
                fpath, ctype = cache, "audio/mpeg"
            else:
                threading.Thread(target=transcode_worker, args=(tid,),
                                 daemon=True, name="cm-transcode-lazy").start()
        if q in BITRATE_STEPS and (not orig_kbps or q < orig_kbps):
            alt = make_transcode(fpath, tid, q)
            if alt:
                fpath, ctype = alt, "audio/mpeg"
        return self.stream_file(fpath, ctype)

    def stream_file(self, fpath: str, ctype: str) -> None:
        size = os.path.getsize(fpath)
        range_hdr = self.headers.get("Range") or ""
        with open(fpath, "rb") as fh:
            if range_hdr.startswith("bytes="):
                spec = range_hdr[6:].split(",")[0].strip()
                start_s, _, end_s = spec.partition("-")
                try:
                    start = int(start_s) if start_s else 0
                except ValueError:
                    start = 0
                try:
                    end = int(end_s) if end_s else size - 1
                except ValueError:
                    end = size - 1
                end = min(end, size - 1)
                if start < 0 or end >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    return
                fh.seek(start)
                remaining = end - start + 1
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(remaining))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                # 分块流式写：wav 未压缩常几十 MB，一次性 read 整段会吃内存/超时
                try:
                    while remaining > 0:
                        buf = fh.read(min(65536, remaining))
                        if not buf:
                            break
                        self.wfile.write(buf)
                        remaining -= len(buf)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                while True:
                    buf = fh.read(65536)
                    if not buf:
                        break
                    self.wfile.write(buf)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    # ---------- 播放列表 ----------
    def create_playlist(self, payload: dict) -> None:
        name = safe_name(str(payload.get("name") or "新建播放列表"))
        pid = secrets.token_hex(8)
        pl = {"id": pid, "name": name, "created_at": int(time.time()), "tracks": []}
        with STATE_LOCK:
            STATE["playlists"].append(pl)
            save_playlists()
        self._json({"ok": True, "playlist": pl}, 200)

    def delete_playlist(self, pid: str) -> None:
        pl = find_playlist(pid)
        if not pl:
            return self._json({"error": "not_found"}, 404)
        with STATE_LOCK:
            STATE["playlists"] = [p for p in STATE["playlists"] if p["id"] != pid]
            save_playlists()
        self._json({"ok": True}, 200)

    def add_to_playlist(self, pid: str, payload: dict) -> None:
        pl = find_playlist(pid)
        if not pl:
            return self._json({"error": "not_found"}, 404)
        tid = str(payload.get("track_id") or "").strip()
        if not find_track(tid):
            return self._json({"error": "track_not_found"}, 404)
        with STATE_LOCK:
            if tid not in pl["tracks"]:
                pl["tracks"].append(tid)
                save_playlists()
        self._json({"ok": True, "playlist": pl}, 200)

    def remove_from_playlist(self, pid: str, tid: str) -> None:
        pl = find_playlist(pid)
        if not pl:
            return self._json({"error": "not_found"}, 404)
        with STATE_LOCK:
            pl["tracks"] = [x for x in pl["tracks"] if x != tid]
            save_playlists()
        self._json({"ok": True, "playlist": pl}, 200)

    def reorder_playlist(self, pid: str, payload: dict) -> None:
        pl = find_playlist(pid)
        if not pl:
            return self._json({"error": "not_found"}, 404)
        try:
            frm = int(payload.get("from")); to = int(payload.get("to"))
        except (TypeError, ValueError):
            return self._json({"error": "bad_index"}, 400)
        arr = pl["tracks"]
        if not (0 <= frm < len(arr) and 0 <= to < len(arr)):
            return self._json({"error": "out_of_range"}, 400)
        with STATE_LOCK:
            arr.insert(to, arr.pop(frm))
            save_playlists()
        self._json({"ok": True, "playlist": pl}, 200)

    # ---------- 静态 / 路由 ----------
    def route_get(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path

        if path in ("/login", "/login.html"):
            if self.authed():
                return self.redirect("/")
            if self.req_prefix():
                return self.redirect_out(self.portal_login())
            return self.serve_login()

        if path == "/api/login":
            return self._json({"error": "use_post"}, 405)

        if not self.authed() and path not in PUBLIC_PATHS:
            if self.req_prefix():
                if path.startswith("/api/"):
                    return self._json({"error": "unauthorized", "login": self.portal_login(),
                                       "portal_login": True}, 401)
                return self.redirect_out(self.portal_login())
            if path.startswith("/api/"):
                return self._json({"error": "unauthorized", "login": "/login"}, 401)
            return self.redirect("/login")

        if path.startswith("/api/track/"):
            q = urllib.parse.parse_qs(parsed.query).get("q", [""])[0]
            return self.serve_track(path.rsplit("/", 1)[-1], q)
        if path.startswith("/api/lyrics/"):
            rec = find_track(path.rsplit("/", 1)[-1])
            if not rec:
                return self._json({"error": "not_found"}, 404)
            return self._json({"ok": True, "lyrics": rec.get("lyrics") or ""})

        if path == "/api/health":
            return self._json({
                "ok": True,
                "version": VERSION,
                "uptime_seconds": int(time.time() - STATE["started_at"]),
                "tracks": len(STATE["tracks"]),
                "playlists": len(STATE["playlists"]),
            })
        if path == "/api/session":
            sess = self.identity()
            return self._json({"ok": True, "user": sess["user"] if sess else ""})
        if path == "/api/meta":
            sess = self.identity()
            ff = bool(ffmpeg_bin())
            _tc_ready = 0
            _tc_total = 0
            for _t in STATE["tracks"]:
                if str(_t.get("ext") or "").lower() not in NEEDS_TRANSCODE:
                    continue
                _tc_total += 1
                _p = playable_cache(_t)
                try:
                    if os.path.isfile(_p) and os.path.getsize(_p) > 0:
                        _tc_ready += 1
                except OSError:
                    pass
            return self._json({
                "version": VERSION,
                "auth": bool(sess),
                "user": sess["user"] if sess else "",
                "portal": portal_links(self.req_prefix()),
                # 登录后才能看到服务器本地路径，避免未认证时泄露目录结构
                "data_dir": DATA_DIR if sess else "",
                "upload_dir": UPLOAD_DIR if sess else "",
                "max_upload_mb": STATE["config"].get("max_upload_mb") or 200,
                "allowed_ext": sorted(ALLOWED_EXT),
                # 播放页据此决定码率档位是否可选
                "transcode": ff,
                "bitrate_steps": BITRATE_STEPS if ff else [],
                # 服务端自动转码状态（部署自检用）
                "auto_transcode": auto_transcode_enabled(),
                "auto_transcode_kbps": STATE["config"].get("auto_transcode_kbps") or 192,
                "needs_transcode": sorted(NEEDS_TRANSCODE),
                "transcode_ready": _tc_ready,
                "transcode_pending": _tc_total - _tc_ready,
                "hot_reload": HOT.status() if HOT else {},
            })
        if path == "/api/assets":
            # 前端 version.js 轮询这个指纹，发现外部静态文件变了就提示刷新
            return self._json(HOT.assets() if HOT else {"ok": False})
        if path == "/api/tracks":
            with STATE_LOCK:
                items = [public_track(t) for t in STATE["tracks"]]
            return self._json({"total": len(items), "items": items})
        if path == "/api/playlists":
            with STATE_LOCK:
                items = [dict(p) for p in STATE["playlists"]]
            return self._json({"total": len(items), "items": items})
        if path.startswith("/api/playlists/") and path.count("/") == 3:
            pid = path.rsplit("/", 1)[-1]
            pl = find_playlist(pid)
            if not pl:
                return self._json({"error": "not_found"}, 404)
            return self._json(pl)

        return self.static(path)

    def static(self, path: str):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        rel = posixpath.normpath(rel)
        if rel.startswith(".."):
            return self._json({"error": "forbidden"}, 403)
        # 外部 static/ 优先、按文件回退到 exe 内置资源：改文件后刷新浏览器即生效
        full, is_external, _src = HOT.resolve_static(rel) if HOT else (None, False, "")
        if not full:
            return self._json({"error": "not_found", "path": path}, 404)
        ext = os.path.splitext(full)[1].lower()
        ctype = MIME.get(ext) or mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as fh:
            body = fh.read()
        if is_external:
            cache = "no-store, must-revalidate"   # 外部文件禁止缓存，改动立刻生效
        else:
            cache = "public, max-age=86400" if ext in (".png", ".svg", ".woff2") else "no-cache"
        self._send(200, body, ctype, {
            "Cache-Control": cache,
            "X-Asset-Source": "external" if is_external else "bundled",
        })


# ---------------------------------------------------------------- 码率 / 转码
def calc_bitrate(size: int, duration) -> int | None:
    """由文件大小和时长估算平均码率（kbps）：CBR 接近标称值，VBR 为平均值。"""
    try:
        d = float(duration)
        if size > 0 and 0 < d < 86400:
            v = round(size * 8 / d / 1000)
            return v if 8 <= v <= 10000 else None
    except (TypeError, ValueError):
        pass
    return None


def transcode_dir() -> str:
    return os.path.join(DATA_DIR, "transcodes")


def ffmpeg_bin() -> str:
    """返回可用的 ffmpeg 路径；找不到返回空串（此时不转码、只直出原文件）。

    查找顺序：config.ffmpeg_path（绝对/相对 exe 目录）→ exe 同级 ffmpeg.exe
    → exe 同级 ffmpeg/ffmpeg.exe → data 目录 → PATH。
    这样部署时把 ffmpeg.exe 放到 exe 旁边即可生效，无需改任何配置。
    """
    p = str(STATE["config"].get("ffmpeg_path") or "").strip()
    if p:
        cands = [p] if os.path.isabs(p) else [os.path.join(APP_DIR, p), p]
        for c in cands:
            if os.path.isfile(c):
                return c
        # 显式配置了路径却找不到：不要静默回退，便于排查部署问题；节流避免每请求刷屏
        logger.get().throttled("ffmpeg_path_missing",
                               f"ffmpeg_path 指向的文件不存在：{p}", 300)
        return ""
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    cands = [
        os.path.join(APP_DIR, name),
        os.path.join(APP_DIR, "ffmpeg", name),
        os.path.join(DATA_DIR, name),
        os.path.join(DATA_DIR, "ffmpeg", name),
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    return shutil.which("ffmpeg") or ""


def auto_transcode_enabled() -> bool:
    return bool(STATE["config"].get("auto_transcode", True)) and bool(ffmpeg_bin())


def playable_cache(rec: dict) -> str:
    return os.path.join(transcode_dir(), f"{rec['id']}_auto.mp3")


def ensure_playable(rec: dict) -> str | None:
    """把浏览器兼容性差的原文件后台转成通用 mp3 并缓存；已有缓存直接返回。

    生成过程写入 .tmp 再原子替换，避免播放请求读到半截文件；失败返回 None，
    调用方回退原文件（配合按扩展名规范化的 Content-Type），不影响可用性。
    """
    if not auto_transcode_enabled():
        return None
    ext = str(rec.get("ext") or os.path.splitext(rec["filename"])[1] or "").lower()
    if ext not in NEEDS_TRANSCODE:
        return None
    src = os.path.join(UPLOAD_DIR, rec["filename"])
    if not os.path.isfile(src):
        return None
    d = transcode_dir()
    os.makedirs(d, exist_ok=True)
    dst = playable_cache(rec)
    if os.path.isfile(dst) and os.path.getsize(dst) > 0:
        return dst
    try:
        kbps = int(STATE["config"].get("auto_transcode_kbps") or 192)
    except (TypeError, ValueError):
        kbps = 192
    kbps = max(64, min(320, kbps))
    kwargs = {}
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        kwargs["startupinfo"] = si        # 不弹黑色控制台窗口
    with TRANSCODE_LOCK:
        if os.path.isfile(dst) and os.path.getsize(dst) > 0:
            return dst
        tmp = dst + ".tmp.mp3"
        cmd = [ffmpeg_bin(), "-nostdin", "-y", "-i", src, "-vn",
               "-map_metadata", "-1", "-codec:a", "libmp3lame",
               "-b:a", f"{kbps}k", tmp]
        try:
            r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=900, **kwargs)
            if r.returncode != 0 or not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
                raise RuntimeError(f"ffmpeg 退出码 {r.returncode}")
            os.replace(tmp, dst)
            log(f"服务端转码完成：{rec.get('name') or rec['id']} → {kbps}kbps mp3")
            return dst
        except Exception as exc:
            log_warn(f"服务端转码失败（回退原文件）：{rec.get('name') or rec['id']} / {exc}")
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            return None


def transcode_worker(tid: str) -> None:
    """后台线程：上传完成后异步转码，不阻塞上传响应。"""
    try:
        rec = find_track(tid)
        if rec:
            ensure_playable(rec)
    except Exception as exc:
        log_warn(f"后台转码异常 {tid}：{exc}")


def transcode_all_worker() -> None:
    """启动后补转历史曲目（部署 ffmpeg 之前上传的文件也能受益），逐个串行避免打满 CPU。"""
    if not auto_transcode_enabled():
        return
    with STATE_LOCK:
        pending = [t for t in STATE["tracks"]
                   if str(t.get("ext") or "").lower() in NEEDS_TRANSCODE]
    for t in pending:
        dst = playable_cache(t)
        if os.path.isfile(dst) and os.path.getsize(dst) > 0:
            continue
        transcode_worker(t["id"])
    if pending:
        log(f"历史曲目转码检查完成：{len(pending)} 首")


def make_transcode(src: str, tid: str, kbps: int) -> str | None:
    """按需生成指定码率的 mp3 并缓存；失败返回 None（调用方回退原文件）。"""
    exe = ffmpeg_bin()
    if not exe or kbps not in BITRATE_STEPS:
        return None
    d = transcode_dir()
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, f"{tid}_{kbps}.mp3")
    if os.path.isfile(dst) and os.path.getsize(dst) > 0:
        return dst
    kwargs = {}
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        kwargs["startupinfo"] = si       # 转码时不弹黑色控制台窗口
    with TRANSCODE_LOCK:
        if os.path.isfile(dst) and os.path.getsize(dst) > 0:
            return dst
        tmp = dst + ".tmp.mp3"
        cmd = [exe, "-nostdin", "-y", "-i", src, "-vn", "-map_metadata", "-1",
               "-codec:a", "libmp3lame", "-b:a", f"{kbps}k", tmp]
        try:
            r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=300, **kwargs)
            if r.returncode != 0 or not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
                raise RuntimeError(f"ffmpeg 退出码 {r.returncode}")
            os.replace(tmp, dst)
            log(f"已生成 {kbps}kbps 转码缓存：{tid}")
            return dst
        except Exception as exc:
            log_warn(f"转码失败（自动回退原码率）：{kbps}kbps / {tid} / {exc}")
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            return None


def public_track(t: dict) -> dict:
    return {
        "id": t["id"], "name": t["name"], "title": t.get("title", t["name"]),
        "artist": t.get("artist", ""), "album": t.get("album", ""),
        "ext": t["ext"], "mime": t["mime"], "size": t["size"],
        "duration": t.get("duration"), "uploaded_at": t.get("uploaded_at", 0),
        "bitrate": t.get("bitrate") or calc_bitrate(t.get("size", 0), t.get("duration")),
        "has_lyrics": bool((t.get("lyrics") or "").strip()),
    }


def revoke_session(token: str) -> None:
    digest = hashlib.sha256((token or "").encode()).hexdigest()
    if SESSIONS.pop(digest, None):
        save_sessions()


def main() -> None:
    global DATA_DIR, UPLOAD_DIR, HOT
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cfg = load_config()
    STATE["config"] = cfg
    # 允许通过 config.json 的 data_dir 把音乐文件放到别的目录/盘符
    raw_dir = str(cfg.get("data_dir") or "").strip()
    if raw_dir:
        DATA_DIR = os.path.normpath(raw_dir if os.path.isabs(raw_dir) else os.path.join(APP_DIR, raw_dir))
        UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
    log_dir = cfg.get("log_dir") or "logs"
    if not os.path.isabs(log_dir):
        log_dir = os.path.join(APP_DIR, log_dir)
    lg = logger.setup(log_dir, cfg.get("log_days") or 14, "music")
    sys.excepthook = lambda et, e, tb: logger.get().error(
        "未捕获异常：\n" + "".join(__import__("traceback").format_exception(et, e, tb)))
    HOT = hot_reload.HotReloader(APP_DIR, DEFAULTS, STATE, log, log_warn,
                                 bundled_static=STATIC_DIR)

    os.makedirs(DATA_DIR, exist_ok=True)
    log("=" * 60)
    log(f"云音乐 v{VERSION} 启动")
    log(f"热更新目录：{HOT.external_static}（放入同名文件即可覆盖，无需重启）")
    log(f"外部静态文件：{len(HOT.external_files())} 个；config.json 改动自动重载")
    log(f"数据目录：{DATA_DIR}")
    log(f"音乐文件保存目录：{UPLOAD_DIR}")
    ff = ffmpeg_bin()
    log(f"ffmpeg：{ff or '未找到（自动转码与码率切换均停用）'}")
    log("服务端自动转码：" + ("已启用" if auto_transcode_enabled() else "未启用（缺 ffmpeg 或配置关闭）"))
    log(f"日志文件：{lg.path_for()}（保留 {cfg.get('log_days') or 14} 天）")
    log(f"监听：{cfg.get('host')}:{cfg.get('port')}")
    ensure_credentials(cfg)
    load_sessions()
    load_store()
    log(f"已载入：{len(STATE['tracks'])} 首曲目 / {len(STATE['playlists'])} 个播放列表")
    # 启动后后台补转历史曲目：部署 ffmpeg 之前上传的 wav/flac 等也能自动受益，
    # 单个串行线程，不阻塞服务启动与播放请求。
    if auto_transcode_enabled():
        threading.Thread(target=transcode_all_worker, daemon=True,
                         name="cm-transcode-all").start()

    host, port = cfg["host"], int(cfg["port"])
    for attempt in range(6):
        try:
            httpd = ThreadingHTTPServer((host, port), Handler)
            break
        except OSError as exc:
            if attempt == 5:
                log(f"端口 {port} 绑定失败：{exc}")
                raise
            log(f"端口 {port} 被占用，3 秒后重试…")
            time.sleep(3)

    httpd.daemon_threads = True
    log(f"服务已就绪：http://127.0.0.1:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("收到退出信号，正在关闭…")
    finally:
        httpd.server_close()
        log("服务已停止")


if __name__ == "__main__":
    main()
