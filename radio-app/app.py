"""云收音机 —— 单文件后端（Windows 可直接运行 / 可打包为 exe）

功能：
  * 定时从公开电台目录自动抓取频道 + 校验流媒体地址
  * 对外提供频道列表 / 搜索 / 分类接口
  * 内置流代理：解决 http 混合内容、防盗链 Referer、m3u8 跨域等问题
  * 同端口托管播放页（无需 nginx）

启动：CloudRadio.exe            （默认 0.0.0.0:8080）
      CloudRadio.exe --port 9000
      set PORT=9000 && CloudRadio.exe
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import posixpath
import re
import secrets
import socket
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import crawler
import epg
import hot_reload
import logger
import portal_auth

VERSION = "1.5.11"
IS_FROZEN = getattr(sys, "frozen", False)
BUNDLE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.dirname(os.path.abspath(sys.executable)) if IS_FROZEN else os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BUNDLE_DIR, "static")
DATA_DIR = os.path.join(APP_DIR, "data")

DEFAULTS = {
    "host": "0.0.0.0",
    "port": 8080,
    "refresh_hours": 6,
    "admin_token": "",
    "crawl_on_start": True,
    # 登录凭据：明文保存，方便 SSH 上去直接改；留空则自动生成随机密码
    "username": "admin",
    "password": "",
    "session_days": 30,
    # 日志：按天写到 <log_dir>/radio-YYYY-MM-DD.txt，保留 log_days 天
    "log_dir": "logs",
    "log_days": 14,
    # 放在 nginx 后面时：信任反向代理带来的登录用户（由 nginx 做 basic auth），
    # 只有来自 127.0.0.1 的请求才会被采信，避免伪造请求头绕过登录
    "trust_proxy_auth": False,
    # 右上角「首页 / 状态」按钮的地址；留空则自动判断：
    # 挂在前缀下（如 /radio/）时回到上一级，独立运行时隐藏按钮
    "portal_home": "",
    "portal_status": "",
    # 统一入口登录：挂在 nginx 前缀下时校验门户的 cm_session Cookie。
    # 留空即用默认地址 http://127.0.0.1:8092/auth/check；独立运行时不启用。
    "portal_auth_url": "",
}

STATE: dict = {
    "stations": [],
    "meta": {},
    "refreshing": False,
    "progress": "",
    "last_error": "",
    "started_at": time.time(),
    "config": dict(DEFAULTS),
}
STATE_LOCK = threading.Lock()

SESSIONS: dict[str, dict] = {}
# 当前收听状态：session digest -> {user, ip, station_id, station_name, started_at, updated_at}
LISTENING: dict[str, dict] = {}
LISTENING_TTL = 12 * 3600  # 超过 12 小时没心跳的记录视为失效
LOGIN_FAILS: dict[str, list] = {}
COOKIE_NAME = "cr_sid"
PUBLIC_PATHS = {"/login", "/login.html", "/api/login", "/api/health",
                "/api/assets", "/icon.svg", "/favicon.ico"}
HOT = None   # main() 里初始化：静态资源外置 + config.json 热重载

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


def log(msg: str) -> None:
    logger.get().info(msg)


def _excepthook(exc_type, exc, tb) -> None:
    import traceback
    logger.get().error("未捕获异常：\n" + "".join(traceback.format_exception(exc_type, exc, tb)))


def _thread_excepthook(args) -> None:
    import traceback
    logger.get().error("线程异常：\n" + "".join(
        traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))


def log_warn(msg: str) -> None:
    logger.get().warn(msg)


def log_err(msg: str) -> None:
    logger.get().error(msg)


def parse_args(cfg: dict) -> dict:
    """支持 --port 9000 / --host 0.0.0.0 / --refresh-hours 3"""
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
        if arg in ("--refresh-hours", "--refresh") and nxt:
            try:
                cfg["refresh_hours"] = max(0.2, float(nxt))
            except ValueError:
                pass
            i += 2
            continue
        if arg in ("--version", "-v"):
            print(f"CloudRadio {VERSION}")
            raise SystemExit(0)
        if arg in ("--help", "-h"):
            print("用法：CloudRadio [--port 8080] [--host 0.0.0.0] [--refresh-hours 6]")
            raise SystemExit(0)
        i += 1
    return cfg


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
    if os.environ.get("REFRESH_HOURS"):
        try:
            cfg["refresh_hours"] = max(0.2, float(os.environ["REFRESH_HOURS"]))
        except ValueError:
            pass
    return parse_args(cfg)


def hydrate() -> None:
    # 首次运行：把随程序打包的种子频道库复制到数据目录，界面立刻有内容
    if not os.path.exists(crawler.store_path(DATA_DIR)):
        seed = os.path.join(BUNDLE_DIR, "seed", "channels.json")
        if os.path.exists(seed):
            try:
                os.makedirs(DATA_DIR, exist_ok=True)
                with open(seed, "rb") as src, open(crawler.store_path(DATA_DIR), "wb") as dst:
                    dst.write(src.read())
                log("已载入内置种子频道库")
            except Exception as exc:
                log(f"种子频道库复制失败：{exc}")
    stored = crawler.load_store(DATA_DIR)
    if stored and stored.get("stations"):
        with STATE_LOCK:
            STATE["stations"] = stored["stations"]
            STATE["meta"] = {k: v for k, v in stored.items() if k != "stations"}
        log(f"已载入本地频道库：{len(stored['stations'])} 个频道（更新于 {stored.get('updated_at', '未知')}）")


def refresh_blocking(reason: str = "手动") -> None:
    with STATE_LOCK:
        if STATE["refreshing"]:
            return
        STATE["refreshing"] = True
        STATE["progress"] = "开始抓取"
        STATE["last_error"] = ""
    log(f"开始抓取频道库（触发方式：{reason}）")
    try:
        result = crawler.crawl(DATA_DIR, progress=lambda m: STATE.__setitem__("progress", m))
        with STATE_LOCK:
            STATE["stations"] = result["stations"]
            STATE["meta"] = {k: v for k, v in result.items() if k != "stations"}
        log(f"频道库更新完成：{result['total']} 个可用频道")
    except Exception as exc:
        with STATE_LOCK:
            STATE["last_error"] = str(exc)
        import traceback
        logger.get().error(f"抓取失败：{exc}\n" + traceback.format_exc())
    finally:
        with STATE_LOCK:
            STATE["refreshing"] = False
            STATE["progress"] = "空闲"


def refresh_async() -> None:
    threading.Thread(target=refresh_blocking, daemon=True).start()


def scheduler() -> None:
    interval = float(STATE["config"]["refresh_hours"]) * 3600
    while True:
        time.sleep(180)
        try:
            meta = STATE.get("meta") or {}
            updated = meta.get("updated_at")
            due = True
            if updated:
                try:
                    ts = datetime.fromisoformat(updated).timestamp()
                    due = (time.time() - ts) > interval
                except Exception:
                    due = True
            if due and not STATE["refreshing"]:
                refresh_blocking(reason="定时任务")
        except Exception as exc:
            log(f"调度器异常：{exc}")


def find_station(sid: str) -> dict | None:
    for st in STATE["stations"]:
        if st["id"] == sid:
            return st
    return None


def b64e(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").strip("=")


def b64d(text: str) -> str:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad).decode("utf-8")


def upstream_headers(station: dict) -> dict:
    headers = {"User-Agent": crawler.UA, "Accept": "*/*"}
    home = station.get("homepage") or ""
    if home.startswith("http"):
        p = urllib.parse.urlsplit(home)
        headers["Referer"] = f"{p.scheme}://{p.netloc}/"
        headers["Origin"] = f"{p.scheme}://{p.netloc}"
    return headers


SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# 浏览器主动断开（换台 / 暂停 / 关页面）在 Windows 上表现为
# WinError 10053 (ConnectionAbortedError)。属正常现象，不按故障记录。
CLIENT_ABORT = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


class ClientGone(Exception):
    """浏览器端主动断开连接，与上游故障区分开。"""


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


def prune_sessions() -> None:
    now = time.time()
    expired = [k for k, v in SESSIONS.items() if v.get("exp", 0) <= now]
    for key in expired:
        SESSIONS.pop(key, None)
    if expired:
        save_sessions()


def issue_session(user: str) -> str:
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    days = float(STATE["config"].get("session_days") or 30)
    SESSIONS[digest] = {"user": user, "exp": time.time() + days * 86400, "ip": ""}
    save_sessions()
    return token


def revoke_session(token: str) -> None:
    digest = hashlib.sha256((token or "").encode()).hexdigest()
    if SESSIONS.pop(digest, None):
        save_sessions()


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


def session_digest(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def session_record(token: str) -> dict | None:
    """返回会话记录（含 digest / user），未登录或已过期返回 None。"""
    digest = session_digest(token)
    item = SESSIONS.get(digest)
    if not item or item.get("exp", 0) <= time.time():
        return None
    return {"digest": digest, "user": item.get("user", "")}


UNAUTH_STATS: dict[str, dict] = {}


def note_unauth(ip: str, path: str) -> None:
    """未登录访问的记录：同一 IP 每小时最多写一条，带累计次数与路径示例。

    端口对公网开放后这类探测会一直有，逐个记录会把日志刷满；
    聚合成一条既能看出"有没有人在扫"，又不淹没正常日志。
    """
    now = time.time()
    stat = UNAUTH_STATS.get(ip)
    if stat is None:
        if len(UNAUTH_STATS) > 800:          # 防止被大量 IP 撑爆内存
            for key in list(UNAUTH_STATS)[:400]:
                UNAUTH_STATS.pop(key, None)
        stat = {"count": 0, "since": now, "logged_at": 0.0, "paths": set()}
        UNAUTH_STATS[ip] = stat
    stat["count"] += 1
    if len(stat["paths"]) < 12:
        stat["paths"].add(path)
    if now - stat["logged_at"] >= 3600:
        stat["logged_at"] = now
        sample = "、".join(sorted(stat["paths"])[:4])
        logger.get().warn(f"未登录访问被拒：{ip} 累计 {stat['count']} 次，路径示例：{sample}")
        stat["paths"].clear()


def portal_links(prefix: str) -> dict:
    """返回门户页与状态页的地址，供页面右上角的「首页 / 状态」按钮使用。

    挂在 nginx 的 /radio/ 下时，页面地址是 /radio/，所以上一级就是门户；
    独立运行（直接访问 8080）时没有门户可回，返回空串，前端就不显示按钮。
    也可以用 config.json 里的 portal_home / portal_status 手工指定。
    """
    cfg = STATE["config"]
    home = str(cfg.get("portal_home") or "").strip()
    status = str(cfg.get("portal_status") or "").strip()
    if prefix:
        home = home or "../"
        status = status or "../status"
    return {"home": home, "status": status}


def prune_listening() -> None:
    now = time.time()
    for key in [k for k, v in LISTENING.items() if now - v.get("updated_at", 0) > LISTENING_TTL]:
        LISTENING.pop(key, None)


def start_listening(digest: str, user: str, ip: str, station: dict) -> dict:
    """记录某个登录会话正在收听哪个电台；同台重复上报只更新时间。"""
    prune_listening()
    now = time.time()
    current = LISTENING.get(digest)
    changed = not current or current.get("station_id") != station.get("id")
    if changed:
        LISTENING[digest] = {
            "user": user,
            "ip": ip,
            "station_id": station.get("id"),
            "station_name": station.get("name", ""),
            "category": station.get("category", ""),
            "started_at": now,
            "updated_at": now,
        }
        if current:
            log(f"切换电台：{user} ({ip}) {current.get('station_name','')} → {station.get('name','')}")
        else:
            log(f"播放：{user} ({ip}) 开始收听 {station.get('name','')}")
    else:
        LISTENING[digest]["updated_at"] = now
        LISTENING[digest]["ip"] = ip
    return LISTENING[digest]


def stop_listening(digest: str, user: str, ip: str) -> None:
    item = LISTENING.pop(digest, None)
    if not item:
        return
    secs = max(0, time.time() - item.get("started_at", time.time()))
    span = "不到 1 分钟" if secs < 60 else f"{int(secs // 60)} 分钟"
    log(f"停止收听：{user} ({ip}) {item.get('station_name','')}，本次 {span}")


def listening_items(current_digest: str = "") -> list[dict]:
    prune_listening()
    now = time.time()
    items = []
    for digest, item in LISTENING.items():
        items.append({
            "user": item.get("user", ""),
            "ip": item.get("ip", ""),
            "station_id": item.get("station_id", ""),
            "station_name": item.get("station_name", ""),
            "category": item.get("category", ""),
            "started_at": item.get("started_at", 0),
            "started_at_text": datetime.fromtimestamp(item.get("started_at", now)).strftime("%H:%M:%S"),
            "seconds": int(now - item.get("started_at", now)),
            "updated_at": item.get("updated_at", 0),
            "idle_seconds": int(now - item.get("updated_at", now)),
            "self": digest == current_digest,
        })
    items.sort(key=lambda x: x["started_at"])
    return items


def ensure_credentials(cfg: dict) -> None:
    """密码为空时生成一个随机密码，并写回 config.json，方便用户查看/修改。"""
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
    log(f"未配置密码，已生成随机密码并写入 config.json（用户名 {cfg['username']}，保留 {cfg.get('session_days', 30)} 天登录）")
    try:
        print(f"    本次生成的密码：{password}（仅显示在控制台，不写入日志文件）", flush=True)
    except Exception:
        pass


def fail_locked(ip: str) -> int:
    """登录失败锁定：15 分钟内失败 8 次锁定，返回还需等待的秒数。"""
    now = time.time()
    fails = [t for t in LOGIN_FAILS.get(ip, []) if now - t < 15 * 60]
    LOGIN_FAILS[ip] = fails
    if len(fails) >= 8:
        return int(15 * 60 - (now - fails[-1]))
    return 0


def note_fail(ip: str) -> None:
    LOGIN_FAILS.setdefault(ip, []).append(time.time())


class Handler(BaseHTTPRequestHandler):
    server_version = "CloudRadio/" + VERSION
    protocol_version = "HTTP/1.1"

    def parse_request(self):
        ok = super().parse_request()
        if ok and HOT:
            try:
                HOT.refresh()
            except Exception:
                pass
        return ok

    # ---------- 基础响应 ----------
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
            self.wfile.write(body)

    def _json(self, data, code: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def log_message(self, fmt, *args):  # 精简访问日志
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
        """反向代理注入的子路径前缀，例如 /radio（末尾不带斜杠）。"""
        raw = (self.headers.get("X-Forwarded-Prefix") or "").strip().rstrip("/")
        return raw if raw.startswith("/") else ""

    def client_ip(self) -> str:
        """真实客户端 IP。放在 nginx 后面时，连接都来自 127.0.0.1，
        需要用反代传来的 X-Real-IP / X-Forwarded-For，否则日志里全是本机地址。"""
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
        """当前请求的登录身份：cookie 会话 / 反代用户名 / 门户统一登录会话。"""
        sess = session_record(self.cookie_token())
        if sess:
            return sess
        user = self.proxy_user()
        if user:
            return {"digest": hashlib.sha256(f"proxy:{user}".encode()).hexdigest(), "user": user}
        # 挂在 nginx 子路径下时，校验门户 CloudAuth 签发的 cm_session Cookie。
        # 独立运行（无 X-Forwarded-Prefix）时完全不依赖登录服务。
        if self.req_prefix():
            name = portal_auth.user(self.headers.get("Cookie") or "",
                                    str(STATE["config"].get("portal_auth_url") or ""))
            if name:
                return {"digest": portal_auth.digest(name), "user": name}
        return None

    def authed(self) -> bool:
        return self.identity() is not None

    def portal_login(self) -> str:
        """门户自定义登录页地址（absolute path），登录后回到当前子应用。"""
        return "/login.html?next=" + urllib.parse.quote(self.req_prefix() + "/", safe="/")

    def redirect_out(self, location: str) -> None:
        """跳到门户级别的页面：不加子路径前缀。"""
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", self.req_prefix() + location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def serve_login(self) -> None:
        path = os.path.join(STATIC_DIR, "login.html")
        try:
            with open(path, "rb") as fh:
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
        sess = self.identity()
        if sess:
            stop_listening(sess["digest"], sess["user"], self.client_ip())
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
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Admin-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
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
        # 统一入口部署时，禁止绕过门户用程序自带的账号直接换取会话
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

        if path in ("/api/playing", "/api/playing/stop"):
            sess = self.identity()
            if not sess:
                return self._json({"error": "unauthorized"}, 401)
            ip = self.client_ip()
            if path == "/api/playing/stop":
                stop_listening(sess["digest"], sess["user"], ip)
                return self._json({"ok": True})
            sid = (payload.get("station_id") or "").strip()
            station = find_station(sid)
            if not station:
                return self._json({"error": "station_not_found"}, 404)
            item = start_listening(sess["digest"], sess["user"], ip, station)
            return self._json({"ok": True, "station": item["station_name"]})

        if path == "/api/refresh":
            token = STATE["config"].get("admin_token") or ""
            given = self.headers.get("X-Admin-Token", "")
            # 未配置 token 时允许任意客户端触发（仅重建频道库，无破坏性）；配置后必须带 token
            allowed = (given == token) if token else True
            if not allowed:
                return self._json({"error": "forbidden", "hint": "需要 X-Admin-Token，或在 config.json 配置 admin_token"}, 403)
            if STATE["refreshing"]:
                return self._json({"ok": True, "started": False, "reason": "already_running"})
            if time.time() - float(STATE.get("last_refresh_at") or 0) < 20:
                return self._json({"ok": True, "started": False, "reason": "cooldown"})
            STATE["last_refresh_at"] = time.time()
            log(f"收到手动更新请求：{self.client_ip()}")
            threading.Thread(target=refresh_blocking, kwargs={"reason": "手动触发"}, daemon=True).start()
            return self._json({"ok": True, "started": True})
        self._json({"error": "not_found"}, 404)

    def route_get(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path in ("/login", "/login.html"):
            if self.authed():
                return self.redirect("/")
            # 统一入口部署时用门户的自定义登录页，不再显示程序自带的旧登录页
            if self.req_prefix():
                return self.redirect_out(self.portal_login())
            return self.serve_login()
        if path == "/api/login":
            return self._json({"error": "use_post"}, 405)
        if not self.authed() and path not in PUBLIC_PATHS:
            note_unauth(self.client_ip(), path)
            if self.req_prefix():
                if path.startswith("/api/"):
                    return self._json({"error": "unauthorized", "login": self.portal_login(),
                                       "portal_login": True}, 401)
                return self.redirect_out(self.portal_login())
            if path.startswith("/api/"):
                return self._json({"error": "unauthorized", "login": self.req_prefix() + "/login"}, 401)
            return self.redirect("/login")

        if path.startswith("/api/stream/"):
            return self.serve_stream(path.rsplit("/", 1)[-1], query)
        if path == "/api/health":
            return self._json({
                "ok": True,
                "version": VERSION,
                "uptime_seconds": int(time.time() - STATE["started_at"]),
                "stations": len(STATE["stations"]),
                "refreshing": STATE["refreshing"],
                "last_error": STATE["last_error"],
            })
        if path == "/api/session":
            sess = self.identity()
            return self._json({"ok": True, "user": sess["user"] if sess else ""})
        if path == "/api/meta":
            meta = dict(STATE["meta"])
            meta["refreshing"] = STATE["refreshing"]
            meta["progress"] = STATE["progress"]
            meta["last_error"] = STATE["last_error"]
            meta["version"] = VERSION
            meta["auth"] = True
            meta["user"] = STATE["config"].get("username", "")
            meta["listening"] = len(LISTENING)
            meta["auth_mode"] = "proxy" if self.proxy_user() and not session_ok(self.cookie_token()) else "cookie"
            meta["portal"] = portal_links(self.req_prefix())
            meta["hot_reload"] = HOT.status() if HOT else {}
            return self._json(meta)
        if path == "/api/assets":
            return self._json(HOT.assets() if HOT else {"ok": False})
        if path == "/api/listening":
            token = self.cookie_token()
            digest = session_digest(token)
            return self._json({"total": len(LISTENING), "items": listening_items(digest)})
        if path == "/api/channels":
            return self.channels(query)
        if path.startswith("/api/channels/"):
            sid = path.rsplit("/", 1)[-1]
            st = find_station(sid)
            if not st:
                return self._json({"error": "not_found"}, 404)
            return self._json(st)
        if path.startswith("/api/epg/"):
            sid = path.rsplit("/", 1)[-1]
            st = find_station(sid)
            if not st:
                return self._json({"error": "station_not_found"}, 404)
            day = (query.get("day", ["today"])[0] or "today").strip()
            if day not in ("yesterday", "today", "tomorrow"):
                day = "today"
            try:
                data = epg.epg_for(st, day)
            except Exception as exc:
                log(f"节目单获取失败 {st['name']}：{exc}")
                data = {"ok": False, "reason": "error", "hint": "节目单服务暂时不可用"}
            return self._json(data)
        return self.static(path, query)

    def channels(self, query):
        q = (query.get("q", [""])[0] or "").strip().lower()
        cat = (query.get("cat", [""])[0] or "").strip()
        region = (query.get("region", [""])[0] or "").strip()
        limit = min(500, max(1, int(query.get("limit", ["300"])[0] or 300)))
        offset = max(0, int(query.get("offset", ["0"])[0] or 0))

        items = STATE["stations"]
        if q:
            items = [s for s in items if q in s["name"].lower() or q in s.get("tags", "").lower() or q in s.get("region", "").lower()]
        if cat and cat != "全部":
            items = [s for s in items if s["category"] == cat]
        if region and region not in ("全部", ""):
            items = [s for s in items if s["region"] == region]

        total = len(items)
        page = items[offset:offset + limit]
        slim = [{
            "id": s["id"], "name": s["name"], "category": s["category"], "region": s["region"],
            "tags": s.get("tags", ""), "favicon": s.get("favicon", ""), "hls": s.get("hls", False),
            "bitrate": s.get("bitrate", 0), "codec": s.get("codec", ""), "latency": s.get("latency", 0),
            "votes": s.get("votes", 0),
        } for s in page]
        self._json({"total": total, "offset": offset, "limit": limit, "items": slim})

    # ---------- 流代理 ----------
    def serve_stream(self, sid: str, query):
        st = find_station(sid)
        if not st:
            return self._json({"error": "station_not_found"}, 404)

        if query.get("u"):
            try:
                target = b64d(query["u"][0])
            except Exception:
                return self._json({"error": "bad_url"}, 400)
        else:
            try:
                idx = int(query.get("i", ["0"])[0])
                entry = st["streams"][idx]
            except (ValueError, IndexError):
                return self._json({"error": "bad_stream_index"}, 400)
            target = entry["url"]

        if not target.lower().startswith(("http://", "https://")):
            return self._json({"error": "bad_url"}, 400)

        try:
            is_playlist = self.pipe_stream(st, target, sid)
            if is_playlist == "rewrite":
                pass
        except ClientGone:
            # 听众主动断开：换台/暂停/关页面都会触发，正常现象
            logger.get().throttled(f"clientgone:{st['id']}",
                                   f"听众断开：{st['name']}（换台/暂停/关页面，正常）", 300,
                                   level="INFO")
        except Exception as exc:
            logger.get().throttled(f"stream:{st['id']}",
                                   f"流代理失败 {st['name']}：{exc}", 300)
            try:
                self._json({"error": "upstream_failed", "detail": str(exc)}, 502)
            except Exception:
                pass

    def _rewrite_playlist(self, text: str, base: str, sid: str) -> bytes:
        out = []
        uri_re = re.compile(r'URI="([^"]+)"')

        def prox(raw: str) -> str:
            absolute = urllib.parse.urljoin(base, raw)
            return f"{self.req_prefix()}/api/stream/{sid}?u={b64e(absolute)}"

        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                out.append("")
                continue
            if stripped.startswith("#"):
                out.append(uri_re.sub(lambda m: f'URI="{prox(m.group(1))}"', line))
            else:
                out.append(prox(stripped))
        return ("\n".join(out) + "\n").encode("utf-8")

    def pipe_stream(self, st: dict, url: str, sid: str):
        headers = upstream_headers(st)
        # 直播流是无限长、从当前位置开始的，不能按字节范围处理：
        # 转发 Range 会让浏览器拿到 206 + 错误的 Content-Range，进而判定格式错误。
        req = urllib.request.Request(url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=20, context=SSL_CTX)
        ctype = (resp.headers.get("Content-Type") or "").lower()

        # HLS 播放列表：整体重写后返回，让分片也走代理
        if "mpegurl" in ctype or url.lower().split("?")[0].endswith(".m3u8"):
            body = resp.read(2 * 1024 * 1024).decode("utf-8", "replace")
            resp.close()
            if "#EXTM3U" in body:
                data = self._rewrite_playlist(body, resp.geturl(), sid)
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if self.command != "HEAD":
                    try:
                        self.wfile.write(data)
                    except CLIENT_ABORT:
                        raise ClientGone()
                return "rewrite"

        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        length = resp.headers.get("Content-Length")
        if length:
            self.send_header("Content-Length", length)
        else:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Accept-Ranges", "none")
        self.end_headers()
        if self.command == "HEAD":
            resp.close()
            return "raw"
        try:
            while True:
                chunk = resp.read(16384)
                if not chunk:
                    break
                try:
                    # 写浏览器失败 = 听众断开（换台/暂停/关页面），正常
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    raise ClientGone()
        finally:
            resp.close()
        return "raw"

    # ---------- 静态资源 ----------
    def static(self, path: str, query):
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
            cache = "no-store, must-revalidate"
        else:
            cache = "public, max-age=86400" if "/vendor/" in path or ext in (".png", ".svg", ".woff2") else "no-cache"
        self._send(200, body, ctype, {
            "Cache-Control": cache,
            "X-Asset-Source": "external" if is_external else "bundled",
        })


def main() -> None:
    global HOT
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cfg = load_config()
    STATE["config"] = cfg
    log_dir = cfg.get("log_dir") or "logs"
    if not os.path.isabs(log_dir):
        log_dir = os.path.join(APP_DIR, log_dir)
    lg = logger.setup(log_dir, cfg.get("log_days") or 14, "radio")
    sys.excepthook = _excepthook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_excepthook

    HOT = hot_reload.HotReloader(APP_DIR, DEFAULTS, STATE, log, log_warn,
                                 bundled_static=STATIC_DIR)
    os.makedirs(DATA_DIR, exist_ok=True)
    log("=" * 60)
    log(f"云收音机 v{VERSION} 启动")
    log(f"热更新目录：{HOT.external_static}（放入同名文件即可覆盖，无需重启）")
    log(f"外部静态文件：{len(HOT.external_files())} 个；config.json 改动自动重载")
    log(f"数据目录：{DATA_DIR}")
    log(f"日志文件：{lg.path_for()}（保留 {cfg.get('log_days') or 14} 天）")
    log(f"监听：{cfg.get('host')}:{cfg.get('port')}")
    ensure_credentials(cfg)
    load_sessions()
    log(f"登录保护已开启：用户名 {cfg.get('username')}（密码见 config.json）")
    epg.bind(DATA_DIR)
    hydrate()

    if cfg.get("crawl_on_start"):
        meta = STATE.get("meta") or {}
        if not STATE["stations"] or meta.get("stale"):
            threading.Thread(target=refresh_blocking, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()

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
    log(f"服务已就绪：http://127.0.0.1:{port}   （对外监听 {host}:{port}）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("收到退出信号，正在关闭…")
    finally:
        httpd.server_close()
        log("服务已停止")


if __name__ == "__main__":
    main()
