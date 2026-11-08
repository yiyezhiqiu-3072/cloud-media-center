"""CloudWork —— 云媒中心 · 工作日志与日程甘特图（单文件后端）

* 记录一天的三类事项：将要发生(planned) / 正在发生(doing) / 已发生待跟踪结果(followup)，
  以及跟踪闭环后的 done；每条带日期、起止时间、备注与结果。
* 记录页录入与状态流转，点「查看甘特图」进入按小时轴绘制的甘特图视图（零外部依赖）。
* 支持调用相机拍照，前端 canvas 自动叠加「时间 + 地点」水印后上传，照片存 data/photos/。
* 数据存 data/items.json；统一登录沿用 CloudAuth 的 cm_session。
* 支持静态资源外置热更新与 config.json 热重载（hot_reload.py）。

接口（相对路径，挂在 nginx /work/ 下同样可用）：
  GET    /api/health /api/meta /api/session /api/assets
  GET    /api/items?day=YYYY-MM-DD    事项列表 + 当日统计
  GET    /api/days                    有记录的日期
  GET    /api/export?day=...&fmt=csv 导出 CSV/JSON
  GET    /api/photos/<name>          读取现场照片
  POST   /api/login /api/logout /api/items /api/photos
  PATCH  /api/items/<id>
  DELETE /api/items/<id>
"""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import mimetypes
import os
import posixpath
import re
import secrets
import sys
import threading
import time
import urllib.parse
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import hot_reload
import logger
import portal_auth

VERSION = "1.1.4"
IS_FROZEN = getattr(sys, "frozen", False)
BUNDLE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.dirname(os.path.abspath(sys.executable)) if IS_FROZEN else os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BUNDLE_DIR, "static")
DATA_DIR = os.path.join(APP_DIR, "data")

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8094,
    "username": "admin",
    "password": "",
    "session_days": 30,
    "log_dir": "logs",
    "log_days": 14,
    # 挂在 nginx 后面时信任反代带来的登录用户（仅采信 127.0.0.1 的请求）
    "trust_proxy_auth": False,
    "portal_home": "",
    "portal_status": "",
    "portal_auth_url": "",
    # 数据目录：留空 = <exe 所在目录>/data
    "data_dir": "",
    # 单张现场照片上限（MB）。前端会先缩放到 1600px 再上传，一般不会触碰上限
    "photo_max_mb": 12,
}

STATUSES = ("planned", "doing", "followup", "done")
STATUS_LABELS = {
    "planned": "将要发生",
    "doing": "正在发生",
    "followup": "待跟踪结果",
    "done": "已闭环",
}
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".csv": "text/csv; charset=utf-8",
}

STATE: dict = {
    "items": [],
    "started_at": time.time(),
    "config": dict(DEFAULTS),
}
STATE_LOCK = threading.Lock()
HOT = None

SESSIONS: dict[str, dict] = {}
LOGIN_FAILS: dict[str, list] = {}
COOKIE_NAME = "cw_sid"
PUBLIC_PATHS = {"/login", "/login.html", "/api/login", "/api/health", "/api/meta",
                "/api/assets", "/icon.svg", "/favicon.ico"}


# ---------------------------------------------------------------- 日志 / 配置
def log(msg: str) -> None:
    logger.get().info(msg)


def log_warn(msg: str) -> None:
    logger.get().warn(msg)


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    path = os.path.join(APP_DIR, "config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                cfg.update({k: v for k, v in json.load(fh).items() if k in DEFAULTS})
        except Exception as exc:
            print(f"config.json 读取失败，使用默认配置：{exc}")
    if os.environ.get("PORT"):
        try:
            cfg["port"] = int(os.environ["PORT"])
        except ValueError:
            pass
    return cfg


# ---------------------------------------------------------------- 存储
def store_path() -> str:
    return os.path.join(DATA_DIR, "items.json")


def load_items() -> None:
    p = store_path()
    if not os.path.isfile(p):
        return
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        STATE["items"] = [x for x in (data or []) if isinstance(x, dict) and x.get("id")]
    except Exception as exc:
        log_warn(f"items.json 读取失败：{exc}")


def save_items() -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = store_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(STATE["items"], fh, ensure_ascii=False, indent=2)
        os.replace(tmp, store_path())
    except Exception as exc:
        log_warn(f"items.json 写入失败：{exc}")


# ---------------------------------------------------------------- 会话
def sessions_path() -> str:
    return os.path.join(DATA_DIR, "sessions.json")


def load_sessions() -> None:
    p = sessions_path()
    if not os.path.isfile(p):
        return
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        now = time.time()
        for k, v in (data or {}).items():
            if isinstance(v, dict) and v.get("exp", 0) > now:
                SESSIONS[k] = v
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
    p = os.path.join(APP_DIR, "config.json")
    data = {}
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            data = {}
    data["username"] = cfg["username"]
    data["password"] = password
    try:
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        log_warn(f"随机密码写入 config.json 失败：{exc}")
    log("未配置密码，已生成随机密码并写入 config.json")
    try:
        print(f"    本次生成的密码：{password}（仅显示在控制台）", flush=True)
    except Exception:
        pass


def fail_locked(ip: str) -> int:
    now = time.time()
    fails = [t for t in LOGIN_FAILS.get(ip, []) if now - t < 15 * 60]
    LOGIN_FAILS[ip] = fails
    if len(fails) >= 8:
        return int(15 * 60 - (now - fails[-1]))
    return 0


def portal_links(prefix: str) -> dict:
    cfg = STATE["config"]
    home = str(cfg.get("portal_home") or "").strip()
    status = str(cfg.get("portal_status") or "").strip()
    if prefix:
        home = home or "../"
        status = status or "../status"
    return {"home": home, "status": status}


# ---------------------------------------------------------------- 事项校验
def to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":", 1)
    return int(h) * 60 + int(m)


def norm_day(value) -> str:
    s = str(value or "").strip()
    if not s:
        return date.today().isoformat()
    if not DAY_RE.match(s):
        raise ValueError("日期格式应为 YYYY-MM-DD")
    datetime.strptime(s, "%Y-%m-%d")     # 校验真实日期
    return s


def norm_time(value, field: str) -> str:
    s = str(value or "").strip()
    if not s:
        raise ValueError(f"请填写{field}")
    if not TIME_RE.match(s):
        raise ValueError(f"{field}格式应为 HH:MM")
    return s


PHOTO_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+\.(?:jpg|jpeg|png|webp)$", re.I)
PHOTO_URL_RE = re.compile(r"^(?:api/)?photos/([A-Za-z0-9_-]+\.(?:jpg|jpeg|png|webp))$", re.I)


def photo_dir() -> str:
    """照片目录。DATA_DIR 会在启动时被 config.data_dir 覆盖，所以动态取值。"""
    return os.path.join(DATA_DIR, "photos")


def norm_photos(raw) -> list:
    """只接受本站 api/photos/ 下的文件名，避免外链与路径穿越；最多 9 张。"""
    if not isinstance(raw, list):
        return []
    out = []
    for p in raw[:9]:
        if isinstance(p, str):
            p = {"url": p}
        if not isinstance(p, dict):
            continue
        m = PHOTO_URL_RE.match(str(p.get("url") or "").strip())
        if not m:
            continue
        name = m.group(1)
        out.append({
            "url": "api/photos/" + name,
            "name": name,
            "time": str(p.get("time") or "")[:40],
            "place": str(p.get("place") or "")[:200],
        })
    return out


def build_item(payload: dict, base: dict | None = None) -> dict:
    """校验并构造一条事项；base 非空时为编辑（沿用 id / 创建时间）。"""
    title = str(payload.get("title") or (base or {}).get("title") or "").strip()
    if not title:
        raise ValueError("请填写事项标题")
    status = str(payload.get("status") or (base or {}).get("status") or "planned").strip()
    if status not in STATUSES:
        raise ValueError("未知状态")
    day = norm_day(payload.get("day") or (base or {}).get("day"))
    start = norm_time(payload.get("start") or (base or {}).get("start"), "开始时间")
    end = str(payload.get("end") or (base or {}).get("end") or "").strip()
    if not end:
        m = min(to_minutes(start) + 60, 1439)      # 未填结束时间默认 1 小时
        end = f"{m // 60:02d}:{m % 60:02d}"
    end = norm_time(end, "结束时间")
    if to_minutes(end) <= to_minutes(start):
        raise ValueError("结束时间必须晚于开始时间")
    now = int(time.time())
    item = {
        "id": (base or {}).get("id") or secrets.token_hex(8),
        "title": title[:120],
        "status": status,
        "day": day,
        "start": start,
        "end": end,
        "note": str(payload.get("note") if "note" in payload else (base or {}).get("note") or "")[:2000],
        "result": str(payload.get("result") if "result" in payload else (base or {}).get("result") or "")[:2000],
        "photos": norm_photos(payload.get("photos") if "photos" in payload else (base or {}).get("photos") or []),
        "created_at": (base or {}).get("created_at") or now,
        "updated_at": now,
    }
    return item


def sorted_items(items: list) -> list:
    return sorted(items, key=lambda x: (x.get("day", ""), x.get("start", ""), x.get("created_at", 0)))


def day_stats(items: list) -> dict:
    out = {s: 0 for s in STATUSES}
    minutes = 0
    for it in items:
        out[it.get("status", "planned")] = out.get(it.get("status", "planned"), 0) + 1
        try:
            minutes += max(0, to_minutes(it.get("end", "0:00")) - to_minutes(it.get("start", "0:00")))
        except Exception:
            pass
    out["minutes"] = minutes
    out["total"] = len(items)
    return out


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "CloudWork/" + VERSION
    protocol_version = "HTTP/1.1"

    def parse_request(self):
        ok = super().parse_request()
        if ok and HOT:
            try:
                HOT.refresh()
            except Exception:
                pass
        return ok

    def log_message(self, fmt, *args):
        return

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    def _json(self, data, code: int = 200, extra: dict | None = None) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", extra)

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain", {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
        })

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self.route_get()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as exc:
            log_warn(f"GET {self.path} 异常：{exc}")
            try:
                self._json({"error": "internal_error"}, 500)
            except Exception:
                pass

    def do_POST(self):
        self.route_write("POST")

    def do_PATCH(self):
        self.route_write("PATCH")

    def do_DELETE(self):
        self.route_write("DELETE")

    # ---------- 身份 ----------
    def cookie_token(self) -> str:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE_NAME:
                return v
        return ""

    def req_prefix(self) -> str:
        raw = (self.headers.get("X-Forwarded-Prefix") or "").strip().rstrip("/")
        return raw if raw.startswith("/") else ""

    def client_ip(self) -> str:
        for name in ("X-Real-IP", "X-Forwarded-For"):
            v = (self.headers.get(name) or "").split(",")[0].strip()
            if v:
                return v
        return self.client_address[0]

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
            return {"user": user}
        if self.req_prefix():
            name = portal_auth.user(self.headers.get("Cookie") or "",
                                    str(STATE["config"].get("portal_auth_url") or ""))
            if name:
                return {"user": name}
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
        # 登录遮罩内联在 index.html 里，独立运行被重定向到 /login 时直接回首页
        full, _ext, _src = HOT.resolve_static("login.html") if HOT else (None, False, "")
        if not full:
            full, _ext, _src = HOT.resolve_static("index.html") if HOT else (None, False, "")
        if not full:
            return self._json({"error": "login_page_missing"}, 500)
        with open(full, "rb") as fh:
            self._send(200, fh.read(), "text/html; charset=utf-8", {"Cache-Control": "no-store"})

    def static(self, path: str):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        rel = posixpath.normpath(rel)
        if rel.startswith(".."):
            return self._json({"error": "forbidden"}, 403)
        full, is_external, _src = HOT.resolve_static(rel) if HOT else (None, False, "")
        if not full:
            return self._json({"error": "not_found", "path": path}, 404)
        ext = os.path.splitext(full)[1].lower()
        ctype = MIME.get(ext) or mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as fh:
            body = fh.read()
        cache = "no-store, must-revalidate" if is_external else (
            "public, max-age=86400" if ext in (".png", ".svg", ".woff2") else "no-cache")
        self._send(200, body, ctype, {
            "Cache-Control": cache,
            "X-Asset-Source": "external" if is_external else "bundled",
        })

    # ---------- GET 路由 ----------
    def route_get(self):
        parsed = urllib.parse.urlsplit(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)

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

        if path == "/api/health":
            return self._json({"ok": True, "version": VERSION,
                               "uptime_seconds": int(time.time() - STATE["started_at"]),
                               "items": len(STATE["items"])})
        if path == "/api/meta":
            sess = self.identity()
            return self._json({
                "version": VERSION, "auth": bool(sess),
                "user": sess["user"] if sess else "",
                "portal": portal_links(self.req_prefix()),
                "statuses": [{"key": s, "label": STATUS_LABELS[s]} for s in STATUSES],
                "today": date.today().isoformat(),
                "hot_reload": HOT.status() if HOT else {},
            })
        if path == "/api/assets":
            return self._json(HOT.assets() if HOT else {"ok": False})
        if path == "/api/session":
            sess = self.identity()
            return self._json({"ok": True, "user": sess["user"] if sess else ""})
        if path.startswith("/api/photos/"):
            return self.serve_photo(urllib.parse.unquote(path.rsplit("/", 1)[-1]))
        if path == "/api/days":
            with STATE_LOCK:
                days = sorted({x.get("day", "") for x in STATE["items"] if x.get("day")}, reverse=True)
            return self._json({"days": days})
        if path == "/api/export":
            day = (query.get("day") or [""])[0]
            with STATE_LOCK:
                items = [dict(x) for x in STATE["items"] if not day or x.get("day") == day]
            items = sorted_items(items)
            fmt = (query.get("fmt") or ["json"])[0]
            if fmt == "csv":
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(["日期", "状态", "标题", "开始", "结束", "内容", "结果", "现场照片水印"])
                for x in items:
                    marks = "; ".join(
                        ((p.get("time") or "") + " · " + (p.get("place") or "")).strip(" ·")
                        for p in (x.get("photos") or []) if isinstance(p, dict))
                    w.writerow([x.get("day", ""), STATUS_LABELS.get(x.get("status"), ""),
                                x.get("title", ""), x.get("start", ""), x.get("end", ""),
                                x.get("note", ""), x.get("result", ""), marks])
                body = "\ufeff" + buf.getvalue()
                fname = day or "all"
                return self._send(200, body.encode("utf-8"), MIME[".csv"], {
                    "Content-Disposition": f'attachment; filename="work-{fname}.csv"'})
            return self._json({"ok": True, "items": items})
        if path == "/api/items":
            day = (query.get("day") or [""])[0]
            with STATE_LOCK:
                items = [dict(x) for x in STATE["items"] if not day or x.get("day") == day]
            items = sorted_items(items)
            return self._json({"total": len(items), "items": items, "stats": day_stats(items)})
        return self.static(path)

    # ---------- 写路由 ----------
    def route_write(self, method: str):
        path = urllib.parse.urlsplit(self.path).path
        payload = {}
        if method in ("POST", "PATCH"):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b"{}"
                payload = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                return self._json({"error": "bad_json"}, 400)

        if path == "/api/login" and method == "POST":
            return self.do_login(payload)
        if path == "/api/logout" and method == "POST":
            return self.do_logout()
        if not self.authed():
            if self.req_prefix():
                return self._json({"error": "unauthorized", "login": self.portal_login(),
                                   "portal_login": True}, 401)
            return self._json({"error": "unauthorized", "login": "/login"}, 401)

        if path == "/api/photos" and method == "POST":
            return self.upload_photo(payload)
        if path == "/api/items" and method == "POST":
            return self.create_item(payload)
        if path.startswith("/api/items/") and method == "PATCH":
            return self.patch_item(path.rsplit("/", 1)[-1], payload)
        if path.startswith("/api/items/") and method == "DELETE":
            return self.delete_item(path.rsplit("/", 1)[-1])
        return self._json({"error": "not_found"}, 404)

    def do_login(self, payload: dict) -> None:
        ip = self.client_ip()
        wait = fail_locked(ip)
        if wait > 0:
            return self._json({"ok": False, "error": "locked",
                               "message": f"尝试次数过多，请 {max(1, wait // 60)} 分钟后再试"}, 429)
        user = str(payload.get("username") or "").strip()
        pwd = str(payload.get("password") or "")
        cfg = STATE["config"]
        ok = (hmac.compare_digest(user, str(cfg.get("username") or "")) and
              hmac.compare_digest(pwd, str(cfg.get("password") or "")))
        if not ok:
            LOGIN_FAILS.setdefault(ip, []).append(time.time())
            return self._json({"ok": False, "error": "用户名或密码不正确"}, 401)
        token = issue_session(user)
        days = max(1, int(cfg.get("session_days") or 30))
        cookie = f"{COOKIE_NAME}={token}; Path=/; Max-Age={days * 86400}; HttpOnly; SameSite=Lax"
        return self._json({"ok": True, "user": user}, 200,
                          {"Set-Cookie": cookie, "X-Auth-User": user})

    def do_logout(self) -> None:
        digest = hashlib.sha256(self.cookie_token().encode()).hexdigest()
        if SESSIONS.pop(digest, None):
            save_sessions()
        return self._json({"ok": True}, 200,
                          {"Set-Cookie": f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})

    def upload_photo(self, payload: dict) -> None:
        """保存前端 canvas 生成的带「时间 + 地点」水印照片。"""
        raw = str(payload.get("b64") or "")
        if "," in raw[:64]:                     # 容错：允许直接传 data URL
            raw = raw.split(",", 1)[1]
        try:
            data = base64.b64decode(raw, validate=True)
        except Exception:
            return self._json({"error": "bad_image", "message": "图片数据无效"}, 400)
        max_bytes = max(1, int(STATE["config"].get("photo_max_mb") or 12)) * 1024 * 1024
        if not data or len(data) > max_bytes:
            return self._json({"error": "bad_image", "message": "图片为空或超过大小上限"}, 400)
        if data[:3] == b"\xff\xd8\xff":
            ext = ".jpg"
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            ext = ".png"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            ext = ".webp"
        else:
            return self._json({"error": "bad_image", "message": "不是有效的图片格式"}, 400)
        name = secrets.token_hex(10) + ext
        try:
            os.makedirs(photo_dir(), exist_ok=True)
            with open(os.path.join(photo_dir(), name), "wb") as fh:
                fh.write(data)
        except OSError as exc:
            log_warn(f"照片保存失败：{exc}")
            return self._json({"error": "write_failed", "message": "照片保存失败"}, 500)
        log(f"保存现场照片：{name}（{len(data) // 1024}KB）")
        return self._json({
            "ok": True, "url": "api/photos/" + name, "name": name,
            "time": str(payload.get("time") or "")[:40],
            "place": str(payload.get("place") or "")[:200],
        }, 200)

    def serve_photo(self, name: str) -> None:
        if not PHOTO_NAME_RE.match(name):
            return self._json({"error": "bad_name"}, 400)
        p = os.path.join(photo_dir(), name)
        if not os.path.isfile(p):
            return self._json({"error": "not_found"}, 404)
        with open(p, "rb") as fh:
            body = fh.read()
        return self._send(200, body, MIME.get(os.path.splitext(p)[1].lower()) or "image/jpeg")

    def _purge_photos(self, names) -> None:
        """删除不再被任何事项引用的照片文件，失败静默（不影响主流程）。"""
        for n in names or []:
            name = str(n or "").rsplit("/", 1)[-1]
            if not PHOTO_NAME_RE.match(name):
                continue
            try:
                os.remove(os.path.join(photo_dir(), name))
            except OSError:
                pass

    def create_item(self, payload: dict) -> None:
        try:
            item = build_item(payload)
        except ValueError as exc:
            return self._json({"error": "bad_request", "message": str(exc)}, 400)
        with STATE_LOCK:
            STATE["items"].append(item)
            save_items()
        log(f"新增事项：{item['day']} {item['start']} {item['title']}（{item['status']}）")
        return self._json({"ok": True, "item": item}, 200)

    def patch_item(self, tid: str, payload: dict) -> None:
        with STATE_LOCK:
            base = next((x for x in STATE["items"] if x.get("id") == tid), None)
            if not base:
                return self._json({"error": "not_found"}, 404)
            try:
                item = build_item(payload, base)
            except ValueError as exc:
                return self._json({"error": "bad_request", "message": str(exc)}, 400)
            old_names = {p.get("name") for p in (base.get("photos") or []) if isinstance(p, dict)}
            new_names = {p.get("name") for p in item.get("photos", [])}
            removed = old_names - new_names
            STATE["items"] = [item if x.get("id") == tid else x for x in STATE["items"]]
            save_items()
        self._purge_photos(removed)
        log(f"更新事项：{tid} -> {item['status']}")
        return self._json({"ok": True, "item": item}, 200)

    def delete_item(self, tid: str) -> None:
        with STATE_LOCK:
            target = next((x for x in STATE["items"] if x.get("id") == tid), None)
            if not target:
                return self._json({"error": "not_found"}, 404)
            photos = [p.get("name") for p in (target.get("photos") or []) if isinstance(p, dict)]
            STATE["items"] = [x for x in STATE["items"] if x.get("id") != tid]
            save_items()
        self._purge_photos(photos)
        log(f"删除事项：{tid}")
        return self._json({"ok": True}, 200)


def main() -> None:
    global DATA_DIR, HOT
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cfg = load_config()
    STATE["config"] = cfg
    raw_dir = str(cfg.get("data_dir") or "").strip()
    if raw_dir:
        DATA_DIR = os.path.normpath(raw_dir if os.path.isabs(raw_dir) else os.path.join(APP_DIR, raw_dir))
    log_dir = cfg.get("log_dir") or "logs"
    if not os.path.isabs(log_dir):
        log_dir = os.path.join(APP_DIR, log_dir)
    lg = logger.setup(log_dir, cfg.get("log_days") or 14, "work")
    sys.excepthook = lambda et, e, tb: logger.get().error(
        "未捕获异常：\n" + "".join(__import__("traceback").format_exception(et, e, tb)))
    HOT = hot_reload.HotReloader(APP_DIR, DEFAULTS, STATE, log, log_warn,
                                 bundled_static=STATIC_DIR)

    os.makedirs(DATA_DIR, exist_ok=True)
    log("=" * 60)
    log(f"工作日志 v{VERSION} 启动")
    log(f"数据文件：{store_path()}")
    log(f"热更新目录：{HOT.external_static}（放入同名文件即可覆盖，无需重启）")
    log(f"日志文件：{lg.path_for()}")
    log(f"监听：{cfg.get('host')}:{cfg.get('port')}")
    ensure_credentials(cfg)
    load_sessions()
    load_items()
    log(f"已载入：{len(STATE['items'])} 条事项")

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
    log(f"服务已就绪：http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("收到退出信号")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
