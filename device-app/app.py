"""CloudDevice —— 云媒中心 · 客户分切设备管理（单文件后端）

* 按客户提供的移动端功能截图复刻：主页功能宫格、8 类基础数据字典、
  5 类业务单据（分切机基本信息表 / F 品视觉防呆 / 软件与工控机 / 卷芯居中 / 设备维护表）、
  序时簿（按类型查询）、分切机总览、照片上传、保存与审核。
* 数据存 data/records.json、data/options.json，照片存 data/photos/。
* 挂在 nginx /device/ 下由 CloudAuth 统一登录；独立运行时用页面内登录遮罩。
* 支持静态资源外置热更新与 config.json 热重载（hot_reload.py）。

接口（相对路径）：
  GET    /api/health /api/meta /api/session /api/assets
  GET    /api/options?kind=...              基础数据字典
  POST   /api/options                      新增字典项
  PATCH  /api/options/<kind>/<id>         改字典项
  DELETE /api/options/<kind>/<id>         删字典项
  GET    /api/records?type=&from=&to=&machine=&status=   序时簿
  POST   /api/records                     新建单据
  PATCH  /api/records/<id>                编辑 / 审核
  DELETE /api/records/<id>
  GET    /api/overview                    分切机总览
  GET    /api/export?type=&fmt=csv|json   导出
  GET    /api/photos/<name>               读取照片
  POST   /api/photos                      上传照片
  POST   /api/login /api/logout
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

VERSION = "1.0.1"
IS_FROZEN = getattr(sys, "frozen", False)
BUNDLE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.dirname(os.path.abspath(sys.executable)) if IS_FROZEN else os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BUNDLE_DIR, "static")
DATA_DIR = os.path.join(APP_DIR, "data")

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8095,
    "username": "admin",
    "password": "",
    "session_days": 30,
    "log_dir": "logs",
    "log_days": 14,
    "trust_proxy_auth": False,
    "portal_home": "",
    "portal_status": "",
    "portal_auth_url": "",
    "data_dir": "",
    "photo_max_mb": 12,
}

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

COOKIE_NAME = "cd_sid"
PUBLIC_API = {"/api/health", "/api/meta", "/api/assets", "/api/session", "/api/login"}
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PHOTO_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+\.(?:jpg|jpeg|png|webp)$", re.I)

# ---------------------------------------------------------------- 基础数据字典
OPTION_KINDS = {
    "bases": {"label": "基地", "group": "factory"},
    "workshops": {"label": "车间", "group": "factory"},
    "machines": {"label": "机台", "group": "factory"},
    "camera_models": {"label": "相机型号", "group": "device"},
    "software_versions": {"label": "软件版本", "group": "device"},
    "dongle_versions": {"label": "加密狗版本", "group": "device"},
    "dongle_methods": {"label": "加密狗安装方式", "group": "device"},
    "ipc_cpus": {"label": "工控机CPU", "group": "device"},
}


def fld(key, label, ftype, options=None, required=False, default=None, hint=""):
    return {"key": key, "label": label, "type": ftype,
            "options": options, "required": bool(required),
            "default": default, "hint": hint or ""}


COMMON_FIELDS = [
    fld("day", "日期", "date", required=True, default="today"),
    fld("machine_no", "机台号", "select", "machines", required=True),
    fld("base", "所属基地", "select", "bases"),
    fld("workshop", "所属车间", "select", "workshops"),
    fld("manager", "设备管理员", "text"),
]

FORM_SCHEMAS = {
    "basic": {
        "label": "分切机基本信息表", "group": "visual",
        "fields": COMMON_FIELDS + [
            fld("roof_height", "当前顶棚", "number", default="0"),
            fld("photo_full", "全机照片，带机台号的照片", "photo"),
            fld("photo_part", "局部照片", "photo"),
        ],
    },
    "vision": {
        "label": "分切机F品视觉防呆", "group": "visual",
        "fields": [
            fld("doc_no", "单据号", "text", hint="留空自动生成"),
        ] + COMMON_FIELDS + [
            fld("camera_model", "相机型号", "select", "camera_models"),
            fld("camera_height", "相机高度", "number", default="0"),
            fld("camera_width", "相机视野宽度", "number", default="0"),
            fld("install_date", "安装日期", "date"),
            fld("accept_date", "验收日期", "date"),
            fld("install_note", "安装说明", "textarea"),
            fld("photo_install", "安装照片", "photo"),
        ],
    },
    "software": {
        "label": "分切机软件与工控机", "group": "visual",
        "fields": COMMON_FIELDS + [
            fld("software_version", "当前软件版本", "select", "software_versions"),
            fld("dongle_version", "加密狗版本", "select", "dongle_versions"),
            fld("dongle_method", "加密狗安装方式", "select", "dongle_methods"),
            fld("ipc_cpu", "工位机CPU", "select", "ipc_cpus"),
            fld("update_date", "更新日期", "date"),
            fld("it_note", "换机说明", "textarea",
                default="与IT确认本机台换工位机要设备部门审核，拔下加密狗"),
            fld("it_confirmed", "是否跟IT确认", "switch"),
            fld("dept_notified", "设备部门告知", "switch",
                hint="设备部门签收时告知使用部门，加密狗不能随意拔落"),
            fld("dongle_status", "加密狗安装状态", "textarea"),
            fld("photo_dongle", "加密狗安装状态照片", "photo"),
            fld("photo_ipc", "工控机CPU", "photo"),
            fld("photo_ui", "软件界面", "photo"),
        ],
    },
    "centering": {
        "label": "分切机卷芯居中", "group": "visual",
        "fields": COMMON_FIELDS + [
            fld("center_camera", "居中相机型号", "select", "camera_models"),
            fld("roof_height", "顶棚高度", "number", default="0"),
            fld("ground_height", "离地高度", "number", default="0"),
            fld("line_distance", "直线识别距离", "number", default="0"),
            fld("install_date", "安装日期", "date"),
            fld("accept_date", "验收日期", "date"),
            fld("remark", "备注说明", "textarea"),
            fld("photo_install", "安装照片", "photo"),
        ],
    },
    "maintenance": {
        "label": "设备维护表", "group": "visual",
        "fields": COMMON_FIELDS + [
            fld("partner_staff", "甲方配合人员", "text"),
            fld("our_staff", "我方维护人员", "text"),
            fld("parts_record", "设备改造与更换备件记录", "textarea"),
            fld("maintenance_record", "维护记录", "textarea"),
            fld("attention", "注意事项", "textarea"),
            fld("pending", "遗留事项", "textarea"),
            fld("maintenance_date", "维护日期", "date"),
            fld("repair_place", "维修更换地方", "text"),
            fld("photo_repair", "维修更换地方照片", "photo"),
        ],
    },
}

STATE: dict = {
    "records": [],
    "options": {},
    "started_at": time.time(),
    "config": dict(DEFAULTS),
}
STATE_LOCK = threading.Lock()
HOT = None
SESSIONS: dict[str, dict] = {}


# ---------------------------------------------------------------- 日志 / 配置
def log(msg: str) -> None:
    logger.get().info(msg)


def log_warn(msg: str) -> None:
    logger.get().warn(msg)


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    p = os.path.join(APP_DIR, "config.json")
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as fh:
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
def records_path() -> str:
    return os.path.join(DATA_DIR, "records.json")


def options_path() -> str:
    return os.path.join(DATA_DIR, "options.json")


def _write_json(path: str, data) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_store() -> None:
    try:
        with open(records_path(), "r", encoding="utf-8") as fh:
            STATE["records"] = [x for x in (json.load(fh) or [])
                                if isinstance(x, dict) and x.get("id")]
    except (OSError, ValueError):
        STATE["records"] = []
    try:
        with open(options_path(), "r", encoding="utf-8") as fh:
            raw = json.load(fh) or {}
        STATE["options"] = {k: [{"id": str(x.get("id") or ""), "name": str(x.get("name") or "")}
                                for x in v if isinstance(x, dict) and x.get("id")]
                            for k, v in raw.items() if k in OPTION_KINDS}
    except (OSError, ValueError):
        STATE["options"] = {}
    for k in OPTION_KINDS:
        STATE["options"].setdefault(k, [])


def save_store() -> None:
    try:
        _write_json(records_path(), STATE["records"])
        _write_json(options_path(), STATE["options"])
    except Exception as exc:
        log_warn(f"数据写入失败：{exc}")


# ---------------------------------------------------------------- 会话
def sessions_path() -> str:
    return os.path.join(DATA_DIR, "sessions.json")


def load_sessions() -> None:
    try:
        with open(sessions_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh) or {}
        now = time.time()
        for k, v in data.items():
            if isinstance(v, dict) and v.get("exp", 0) > now:
                SESSIONS[k] = v
    except (OSError, ValueError):
        pass


def save_sessions() -> None:
    try:
        _write_json(sessions_path(), SESSIONS)
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
    return {"user": item.get("user", "")}


def ensure_credentials(cfg: dict) -> None:
    if not (cfg.get("username") or "").strip():
        cfg["username"] = "admin"
    if cfg.get("password"):
        return
    alphabet = "abcdefghjkmnpqrstuvwxyzACDEFGHJKLMNPQRSTUVWXYZ23456789"
    cfg["password"] = "".join(secrets.choice(alphabet) for _ in range(10))
    p = os.path.join(APP_DIR, "config.json")
    data = {}
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            data = {}
    data["username"] = cfg["username"]
    data["password"] = cfg["password"]
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    log("未配置密码，已生成随机密码并写入 config.json")


# ---------------------------------------------------------------- 业务校验
def clean_day(v) -> str:
    s = str(v or "").strip()
    return s if DAY_RE.match(s) else ""


def norm_photo_list(raw) -> list:
    if not isinstance(raw, list):
        return []
    out = []
    for p in raw[:9]:
        if isinstance(p, str):
            p = {"url": p}
        if not isinstance(p, dict):
            continue
        m = re.match(r"^(?:api/)?photos/([A-Za-z0-9_-]+\.(?:jpg|jpeg|png|webp))$",
                     str(p.get("url") or "").strip(), re.I)
        if not m:
            continue
        out.append({"url": "api/photos/" + m.group(1), "name": m.group(1)})
    return out


def norm_fields(ftype: str, raw, base: dict | None = None) -> dict:
    schema = FORM_SCHEMAS.get(ftype)
    if not schema:
        raise ValueError("未知单据类型")
    old = (base or {}).get("fields") or {}
    out = {}
    for f in schema["fields"]:
        key, typ = f["key"], f["type"]
        val = raw.get(key) if isinstance(raw, dict) and key in raw else old.get(key)
        if typ == "photo":
            out[key] = norm_photo_list(val)
        elif typ == "switch":
            out[key] = bool(val)
        elif typ == "number":
            try:
                out[key] = float(val) if val not in (None, "") else 0
            except (TypeError, ValueError):
                out[key] = 0
        elif typ == "date":
            v = clean_day(val)
            if v and f.get("required") is False and not v:
                v = ""
            out[key] = v
        else:
            out[key] = str(val or "")[:2000]
        if f["required"] and not out[key] and typ != "switch":
            raise ValueError("请填写" + f["label"])
    return out


def next_doc_no(ftype: str) -> str:
    stamp = datetime.now().strftime("%y%m%d")
    with STATE_LOCK:
        n = sum(1 for x in STATE["records"] if x.get("type") == ftype
                and str(x.get("doc_no") or "").startswith(stamp)) + 1
    return stamp + str(n).zfill(4)


def norm_record(payload: dict, base: dict | None = None, user: str = "") -> dict:
    ftype = str(payload.get("type") or (base or {}).get("type") or "").strip()
    if ftype not in FORM_SCHEMAS:
        raise ValueError("未知单据类型")
    fields = norm_fields(ftype, payload.get("fields") if isinstance(payload, dict) else {}, base)
    now = int(time.time())
    rec = {
        "id": (base or {}).get("id") or secrets.token_hex(8),
        "type": ftype,
        "type_label": FORM_SCHEMAS[ftype]["label"],
        "doc_no": str(fields.get("doc_no") or (base or {}).get("doc_no") or next_doc_no(ftype))[:40],
        "day": fields.get("day") or date.today().isoformat(),
        "machine_no": fields.get("machine_no") or "",
        "fields": fields,
        "status": (base or {}).get("status") or "draft",
        "created_by": (base or {}).get("created_by") or user or "SYSTEM",
        "created_at": (base or {}).get("created_at") or now,
        "updated_at": now,
        "audited_by": (base or {}).get("audited_by") or "",
        "audited_at": (base or {}).get("audited_at") or "",
    }
    return rec


def filter_records(query: dict) -> list:
    ftype = str((query.get("type") or [""])[0] or "").strip()
    dfrom = clean_day((query.get("from") or [""])[0])
    dto = clean_day((query.get("to") or [""])[0])
    machine = str((query.get("machine") or [""])[0] or "").strip()
    status = str((query.get("status") or [""])[0] or "").strip()
    q = str((query.get("q") or [""])[0] or "").strip().lower()
    with STATE_LOCK:
        items = [dict(x) for x in STATE["records"]]
    out = []
    for x in items:
        if ftype and x.get("type") != ftype:
            continue
        if dfrom and x.get("day", "") < dfrom:
            continue
        if dto and x.get("day", "") > dto:
            continue
        if machine and machine not in str(x.get("machine_no") or ""):
            continue
        if status and x.get("status") != status:
            continue
        if q and q not in json.dumps(x, ensure_ascii=False).lower():
            continue
        out.append(x)
    out.sort(key=lambda x: (x.get("day", ""), x.get("created_at", 0)), reverse=True)
    return out


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "CloudDevice/" + VERSION
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

    def _err(self, msg: str, code: int = 400) -> None:
        self._json({"error": "bad_request", "message": msg}, code)

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

    def identity(self) -> dict | None:
        sess = session_record(self.cookie_token())
        if sess:
            return sess
        if (STATE["config"].get("trust_proxy_auth")
                and self.client_address[0] in ("127.0.0.1", "::1")):
            u = (self.headers.get("X-Auth-User") or "").strip()
            if u:
                return {"user": u}
        if self.req_prefix():
            u = portal_auth.user(self.headers.get("Cookie") or "",
                                 str(STATE["config"].get("portal_auth_url") or ""))
            if u:
                return {"user": u}
        return None

    def authed(self) -> bool:
        return self.identity() is not None

    def static(self, rel: str) -> None:
        rel = (rel or "").lstrip("/") or "index.html"
        full = None
        if HOT:
            full, _ext, _src = HOT.resolve_static(rel)
        if not full:
            candidate = os.path.join(STATIC_DIR, rel.replace("/", os.sep))
            if os.path.isfile(candidate):
                full = candidate
        if not full or not os.path.isfile(full):
            if HOT:
                full, _ext, _src = HOT.resolve_static("index.html")
            if not full:
                full = os.path.join(STATIC_DIR, "index.html")
        if not os.path.isfile(full):
            return self._json({"error": "not_found"}, 404)
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as fh:
            data = fh.read()
        self._send(200, data, MIME.get(ext, "application/octet-stream"))

    # ---------- 读路由 ----------
    def route_get(self) -> None:
        u = urllib.parse.urlsplit(self.path)
        path, query = u.path, urllib.parse.parse_qs(u.query)
        if path.startswith("/api/photos/"):
            return self.serve_photo(urllib.parse.unquote(path.rsplit("/", 1)[-1]))
        if path == "/api/health":
            return self._json({"ok": True, "version": VERSION, "app": "客户分切设备管理",
                               "records": len(STATE["records"]),
                               "uptime_seconds": int(time.time() - STATE["started_at"])})
        if path == "/api/meta":
            return self._json({
                "ok": True, "version": VERSION, "app": "客户分切设备管理",
                "option_kinds": OPTION_KINDS, "schemas": FORM_SCHEMAS,
                "portal": {"home": "../", "status": "../status"},
                "hot_reload": HOT.status() if HOT else {},
            })
        if path == "/api/assets":
            return self._json(HOT.assets() if HOT else {"ok": False})
        if path == "/api/session":
            ident = self.identity()
            return self._json({"ok": bool(ident), "user": ident["user"] if ident else ""})
        if path.startswith("/api/") and path not in PUBLIC_API and not self.authed():
            return self._json({"error": "unauthorized", "login": "/login"}, 401)
        if path == "/api/options":
            kind = str((query.get("kind") or [""])[0] or "").strip()
            if kind and kind not in OPTION_KINDS:
                return self._err("未知字典类型")
            with STATE_LOCK:
                items = {k: list(v) for k, v in STATE["options"].items()}
            return self._json({"ok": True, "kind": kind,
                               "items": items.get(kind, []) if kind else items,
                               "kinds": OPTION_KINDS})
        if path == "/api/records":
            items = filter_records(query)
            return self._json({"ok": True, "total": len(items), "items": items})
        if path == "/api/overview":
            with STATE_LOCK:
                items = [dict(x) for x in STATE["records"]]
            machines = {}
            for x in items:
                no = str(x.get("machine_no") or "未指定机台")
                m = machines.setdefault(no, {"machine_no": no, "total": 0,
                                             "audited": 0, "types": {}})
                m["total"] += 1
                if x.get("status") == "audited":
                    m["audited"] += 1
                cur = m["types"].get(x.get("type"))
                if not cur or (x.get("day", ""), x.get("created_at", 0)) > (cur.get("day", ""), cur.get("created_at", 0)):
                    m["types"][x["type"]] = {"id": x["id"], "day": x.get("day", ""),
                                             "status": x.get("status", "draft"),
                                             "created_at": x.get("created_at", 0)}
            rows = sorted(machines.values(), key=lambda m: m["machine_no"])
            return self._json({"ok": True, "machines": rows, "types": list(FORM_SCHEMAS)})
        if path == "/api/export":
            ftype = str((query.get("type") or [""])[0] or "").strip()
            fmt = str((query.get("fmt") or ["csv"])[0] or "csv").strip()
            items = [x for x in filter_records(query) if not ftype or x.get("type") == ftype]
            schema = FORM_SCHEMAS.get(ftype)
            fields = schema["fields"] if schema else COMMON_FIELDS
            if fmt == "csv":
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(["单据类型", "单据号", "日期", "机台号", "状态"] +
                           [f["label"] for f in fields] + ["制单人", "审核人"])
                for x in items:
                    fs = x.get("fields") or {}
                    row = [x.get("type_label", ""), x.get("doc_no", ""), x.get("day", ""),
                           x.get("machine_no", ""),
                           "已审核" if x.get("status") == "audited" else "草稿"]
                    for f in fields:
                        v = fs.get(f["key"])
                        row.append(len(v) if f["type"] == "photo" and isinstance(v, list)
                                   else ("是" if v else "否") if f["type"] == "switch" else v or "")
                    row += [x.get("created_by", ""), x.get("audited_by", "")]
                    w.writerow(row)
                body = b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")
                return self._send(200, body, MIME[".csv"], {
                    "Content-Disposition": 'attachment; filename="device-' +
                                           (ftype or "all") + '.csv"'})
            return self._json({"ok": True, "items": items})
        if path.startswith("/api/"):
            return self._json({"error": "not_found"}, 404)
        return self.static(path)

    # ---------- 写路由 ----------
    def route_write(self, method: str) -> None:
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
        if not self.authed():
            return self._json({"error": "unauthorized", "login": "/login"}, 401)
        if path == "/api/logout" and method == "POST":
            token = self.cookie_token()
            SESSIONS.pop(hashlib.sha256((token or "").encode()).hexdigest(), None)
            save_sessions()
            return self._json({"ok": True}, extra={
                "Set-Cookie": COOKIE_NAME + "=; Path=/; Max-Age=0"})
        if path == "/api/photos" and method == "POST":
            return self.upload_photo(payload)
        if path == "/api/options" and method == "POST":
            return self.add_option(payload)
        if path.startswith("/api/options/") and method in ("PATCH", "DELETE"):
            parts = path.split("/")
            if len(parts) == 5:
                return self.edit_option(parts[3], parts[4], payload, method)
        if path == "/api/records" and method == "POST":
            return self.create_record(payload)
        if path.startswith("/api/records/") and method in ("PATCH", "DELETE"):
            return self.edit_record(path.rsplit("/", 1)[-1], payload, method)
        return self._json({"error": "not_found"}, 404)

    def do_login(self, payload: dict) -> None:
        user = str(payload.get("username") or "").strip()
        pw = str(payload.get("password") or "")
        cfg = STATE["config"]
        if not user or not hmac.compare_digest(user, str(cfg.get("username") or "")) \
                or not hmac.compare_digest(pw, str(cfg.get("password") or "")):
            return self._json({"error": "bad_credentials"}, 401)
        token = issue_session(user)
        days = int(float(cfg.get("session_days") or 30) * 86400)
        return self._json({"ok": True}, extra={
            "Set-Cookie": f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={days}"})

    # ---------- 照片 ----------
    def photo_dir(self) -> str:
        return os.path.join(DATA_DIR, "photos")

    def upload_photo(self, payload: dict) -> None:
        b64 = str(payload.get("b64") or "")
        try:
            data = base64.b64decode(b64, validate=True)
        except Exception:
            return self._err("照片数据不是合法的 base64")
        limit = int(float(STATE["config"].get("photo_max_mb") or 12) * 1048576)
        if not data or len(data) > limit:
            return self._err("照片为空或超过大小限制")
        if data[:3] == b"\xff\xd8\xff" or data[:2] == b"\xff\xd8":
            ext = ".jpg"
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            ext = ".png"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            ext = ".webp"
        else:
            return self._err("只支持 JPG / PNG / WEBP 图片")
        name = secrets.token_hex(12) + ext
        d = self.photo_dir()
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "wb") as fh:
            fh.write(data)
        log(f"上传照片：{name}（{len(data) // 1024}KB）")
        return self._json({"ok": True, "url": "api/photos/" + name,
                           "name": name, "size": len(data)}, 201)

    def serve_photo(self, name: str) -> None:
        if not PHOTO_NAME_RE.match(name):
            return self._json({"error": "forbidden"}, 403)
        p = os.path.join(self.photo_dir(), name)
        if not os.path.isfile(p):
            return self._json({"error": "not_found"}, 404)
        with open(p, "rb") as fh:
            data = fh.read()
        self._send(200, data, MIME.get(os.path.splitext(name)[1].lower(), "image/jpeg"))

    # ---------- 字典 ----------
    def add_option(self, payload: dict) -> None:
        kind = str(payload.get("kind") or "").strip()
        name = str(payload.get("name") or "").strip()[:80]
        if kind not in OPTION_KINDS or not name:
            return self._err("请选择字典类型并填写名称")
        item = {"id": secrets.token_hex(6), "name": name}
        with STATE_LOCK:
            STATE["options"].setdefault(kind, []).append(item)
            save_store()
        log(f"新增{OPTION_KINDS[kind]['label']}：{name}")
        return self._json({"ok": True, "item": item}, 201)

    def edit_option(self, kind: str, oid: str, payload: dict, method: str) -> None:
        if kind not in OPTION_KINDS:
            return self._err("未知字典类型")
        with STATE_LOCK:
            items = STATE["options"].setdefault(kind, [])
            hit = next((x for x in items if x.get("id") == oid), None)
            if not hit:
                return self._json({"error": "not_found"}, 404)
            if method == "DELETE":
                STATE["options"][kind] = [x for x in items if x.get("id") != oid]
            else:
                name = str(payload.get("name") or "").strip()[:80]
                if not name:
                    return self._err("名称不能为空")
                hit["name"] = name
            save_store()
        return self._json({"ok": True})

    # ---------- 单据 ----------
    def create_record(self, payload: dict) -> None:
        ident = self.identity() or {}
        try:
            rec = norm_record(payload, None, ident.get("user", ""))
        except ValueError as exc:
            return self._err(str(exc))
        if payload.get("audit") is True:
            rec["status"] = "audited"
            rec["audited_by"] = ident.get("user", "")
            rec["audited_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with STATE_LOCK:
            STATE["records"].append(rec)
            save_store()
        log(f"新增单据：{rec['type_label']} {rec['doc_no']}（{rec['machine_no']}）")
        return self._json({"ok": True, "record": rec}, 201)

    def edit_record(self, rid: str, payload: dict, method: str) -> None:
        ident = self.identity() or {}
        with STATE_LOCK:
            base = next((x for x in STATE["records"] if x.get("id") == rid), None)
            if not base:
                return self._json({"error": "not_found"}, 404)
            if method == "DELETE":
                STATE["records"] = [x for x in STATE["records"] if x.get("id") != rid]
                save_store()
                log(f"删除单据：{rid}")
                return self._json({"ok": True})
            try:
                rec = norm_record(payload, base, ident.get("user", ""))
            except ValueError as exc:
                return self._err(str(exc))
            if payload.get("audit") is True:
                rec["status"] = "audited"
                rec["audited_by"] = ident.get("user", "")
                rec["audited_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            elif payload.get("audit") is False:
                rec["status"] = "draft"
                rec["audited_by"] = ""
                rec["audited_at"] = ""
            STATE["records"] = [rec if x.get("id") == rid else x for x in STATE["records"]]
            save_store()
        log(f"更新单据：{rid} -> {rec['status']}")
        return self._json({"ok": True, "record": rec})


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
    logger.setup(log_dir, cfg.get("log_days") or 14, "device")
    sys.excepthook = lambda et, e, tb: logger.get().error(
        "未捕获异常：\n" + "".join(__import__("traceback").format_exception(et, e, tb)))
    HOT = hot_reload.HotReloader(APP_DIR, DEFAULTS, STATE, log, log_warn,
                                 bundled_static=STATIC_DIR)
    os.makedirs(DATA_DIR, exist_ok=True)
    log("=" * 60)
    log(f"客户分切设备管理 v{VERSION} 启动")
    log(f"数据目录：{DATA_DIR}")
    log(f"热更新目录：{HOT.external_static}")
    ensure_credentials(cfg)
    load_sessions()
    load_store()
    log(f"已载入：{len(STATE['records'])} 条单据 / 8 类字典")
    host, port = cfg["host"], int(cfg["port"])
    httpd = ThreadingHTTPServer((host, port), Handler)
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
