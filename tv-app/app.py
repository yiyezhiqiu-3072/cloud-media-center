"""云电视 —— 单文件后端（Windows 可执行 / 也可直接 python 运行）

  * 频道源管理：官方种子源 + 自定义源（M3U 导入 / 逐条编辑）
  * 并发探活：标出哪些源当前可用，失效的不会出现在默认列表里；
    探活同时检测源站 CORS 放行情况，允许直连的源浏览器可绕过服务器播放
  * 双通道播放：默认优先「浏览器直连源站」（不占服务器带宽），
    直连失败或源站不允许跨域时自动回退到「内置代理」（HLS 播放列表、
    分片、密钥全部走本地服务，解决跨域 / 防盗链 / http 混流）
  * 同端口托管播放页

启动：CloudTV.exe [--port 8081]
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import posixpath
import re
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import channels as ch
import hot_reload
import logger
import portal_auth

VERSION = "1.3.10"
IS_FROZEN = getattr(sys, "frozen", False)
BUNDLE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.dirname(os.path.abspath(sys.executable)) if IS_FROZEN else os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BUNDLE_DIR, "static")
DATA_DIR = os.path.join(APP_DIR, "data")

DEFAULTS = {"host": "0.0.0.0", "port": 8081, "admin_token": "", "probe_on_start": True,
            "log_dir": "logs", "log_days": 14,
            # 直连播放：源站允许跨域(CORS)时，浏览器直接连源站拉流，不占服务器带宽；
            # 设为 false 则一律走服务器代理。直连失败会自动回退代理。
            "direct_play": True,
            # 右上角「首页 / 状态」按钮；留空则按是否在前缀下自动判断
            "portal_home": "", "portal_status": "",
            # 统一入口登录：挂在 nginx 前缀下时校验门户 cm_session Cookie；
            # 留空即用默认 http://127.0.0.1:8092/auth/check，独立运行时不启用
            "portal_auth_url": ""}

HOT = None   # main() 里初始化：静态资源外置 + config.json 热重载

STATE: dict = {
    "channels": [],
    "refreshing": False,
    "progress": "",
    "last_error": "",
    "updated_at": "",
    "started_at": time.time(),
    "config": dict(DEFAULTS),
    "last_refresh_at": 0.0,
}
LOCK = threading.Lock()

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".webmanifest": "application/manifest+json",
}

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


def log(msg: str) -> None:
    logger.get().info(msg)


def log_warn(msg: str) -> None:
    logger.get().warn(msg)


def _excepthook(exc_type, exc, tb) -> None:
    import traceback
    logger.get().error("未捕获异常：\n" + "".join(traceback.format_exception(exc_type, exc, tb)))


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    path = os.path.join(APP_DIR, "config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                cfg.update({k: v for k, v in json.load(fh).items() if k in DEFAULTS})
        except Exception as exc:
            log(f"config.json 读取失败，使用默认值：{exc}")
    for env, key, cast in (("PORT", "port", int), ("HOST", "host", str)):
        if os.environ.get(env):
            try:
                cfg[key] = cast(os.environ[env])
            except ValueError:
                pass
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a in ("--port", "-p") and i + 1 < len(args):
            try:
                cfg["port"] = int(args[i + 1])
            except ValueError:
                pass
        if a in ("--version", "-v"):
            print(f"CloudTV {VERSION}")
            raise SystemExit(0)
        if a in ("--help", "-h"):
            print("用法：CloudTV [--port 8081]")
            raise SystemExit(0)
    return cfg


def hydrate() -> None:
    local = ch.load_local(DATA_DIR)
    if local is None:
        local = ch.seed_channels()
        ch.save_local(DATA_DIR, local)
        log("已写入默认频道表（央视官方公开源）")
    with LOCK:
        STATE["channels"] = local
    log(f"载入频道 {len(local)} 个")


def refresh_blocking() -> None:
    with LOCK:
        if STATE["refreshing"]:
            return
        STATE["refreshing"] = True
        STATE["progress"] = "开始探活"
        STATE["last_error"] = ""
    try:
        log("开始探活频道源")
        items = ch.build(DATA_DIR, progress=lambda m: STATE.__setitem__("progress", m))
        with LOCK:
            STATE["channels"] = items
            STATE["updated_at"] = ch.now_iso()
        online = sum(1 for c in items if c.get("online"))
        log(f"探活完成：{online}/{len(items)} 个频道可用")
        if online == 0:
            log_warn("所有频道均探活失败，请检查服务器出网或源地址")
    except Exception as exc:
        with LOCK:
            STATE["last_error"] = str(exc)
        log(f"探活失败：{exc}")
    finally:
        with LOCK:
            STATE["refreshing"] = False
            STATE["progress"] = "空闲"


def refresh_async() -> None:
    threading.Thread(target=refresh_blocking, daemon=True).start()


def find_channel(cid: str) -> dict | None:
    for c in STATE["channels"]:
        if c["id"] == cid:
            return c
    return None


def portal_links(prefix: str) -> dict:
    """门户页与状态页的地址，供右上角「首页 / 状态」按钮使用。

    挂在 nginx 的 /tv/ 下时页面地址是 /tv/，上一级就是门户；
    独立运行（直接访问 8081）时没有门户可回，返回空串 → 前端隐藏按钮。
    config.json 里可以用 portal_home / portal_status 手工指定。
    """
    cfg = STATE["config"]
    home = str(cfg.get("portal_home") or "").strip()
    status = str(cfg.get("portal_status") or "").strip()
    if prefix:
        home = home or "../"
        status = status or "../status"
    return {"home": home, "status": status}


def b64e(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").strip("=")


def b64d(text: str) -> str:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode("utf-8")


def upstream_headers(channel: dict) -> dict:
    headers = {"User-Agent": ch.UA, "Accept": "*/*"}
    official = channel.get("official") or ""
    if official.startswith("http"):
        parts = urllib.parse.urlsplit(official)
        headers["Referer"] = f"{parts.scheme}://{parts.netloc}/"
    return headers


# 浏览器主动断开（切台 / 缓冲控制 / 关闭页面）时，Windows 上表现为
# WinError 10053 (ConnectionAbortedError)、10054 (ConnectionResetError)。
# 这是正常现象，不该记成「拉流失败」的 WARN。
CLIENT_ABORT = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


class ClientGone(Exception):
    """浏览器端主动断开连接，属正常现象，与上游故障区分开。"""


class Handler(BaseHTTPRequestHandler):
    server_version = "CloudTV/" + VERSION
    protocol_version = "HTTP/1.1"

    def parse_request(self):
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
            self.wfile.write(body)

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def log_message(self, fmt, *args):
        return

    def do_OPTIONS(self):
        self.send_response(204)
        for k, v in (("Access-Control-Allow-Origin", "*"),
                     ("Access-Control-Allow-Headers", "Content-Type, X-Admin-Token"),
                     ("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")):
            self.send_header(k, v)
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

    def do_DELETE(self):
        if not self.gate(urllib.parse.urlsplit(self.path).path):
            return
        if not self.check_admin():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/channels/"):
            cid = path.rsplit("/", 1)[-1]
            local = ch.load_local(DATA_DIR) or []
            remain = [c for c in local if c["id"] != cid]
            if len(remain) == len(local):
                return self._json({"error": "not_found"}, 404)
            log(f"删除频道：{cid}")
            ch.save_local(DATA_DIR, remain)
            with LOCK:
                STATE["channels"] = remain
            return self._json({"ok": True, "removed": cid})
        self._json({"error": "not_found"}, 404)

    def do_POST(self):
        if not self.gate(urllib.parse.urlsplit(self.path).path):
            return
        if not self.check_admin():
            return
        path = urllib.parse.urlsplit(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return self._json({"error": "bad_json"}, 400)

        if path == "/api/channels":
            return self.upsert(payload)
        if path == "/api/import":
            return self.import_m3u(payload)
        if path == "/api/probe":
            url = (payload.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                return self._json({"error": "bad_url"}, 400)
            return self._json(ch.probe(url))
        if path == "/api/refresh":
            if STATE["refreshing"]:
                return self._json({"ok": True, "started": False, "reason": "running"})
            if time.time() - STATE["last_refresh_at"] < 10:
                return self._json({"ok": True, "started": False, "reason": "cooldown"})
            STATE["last_refresh_at"] = time.time()
            refresh_async()
            return self._json({"ok": True, "started": True})
        self._json({"error": "not_found"}, 404)

    def check_admin(self) -> bool:
        token = STATE["config"].get("admin_token") or ""
        if not token:
            return True
        if self.headers.get("X-Admin-Token", "") == token:
            return True
        self._json({"error": "forbidden", "hint": "需要在 config.json 配置 admin_token 并在请求头携带"}, 403)
        return False

    def upsert(self, payload: dict):
        name = (payload.get("name") or "").strip()
        sources = payload.get("sources") or []
        clean = []
        for s in sources:
            url = (s.get("url") if isinstance(s, dict) else str(s) or "").strip()
            if url.startswith(("http://", "https://")):
                clean.append({"url": url,
                              "label": (s.get("label") if isinstance(s, dict) else "") or "自定义",
                              "hls": ".m3u8" in url.lower()})
        if not name or not clean:
            return self._json({"error": "need_name_and_url"}, 400)

        local = ch.load_local(DATA_DIR) or []
        cid = (payload.get("id") or "").strip() or ch.make_id(name, len(local))
        entry = {
            "id": cid,
            "name": name,
            "group": (payload.get("group") or "自定义").strip(),
            "official": (payload.get("official") or "").strip(),
            "desc": (payload.get("desc") or "").strip(),
            "origin": "custom",
            "sources": clean,
        }
        replaced = False
        for i, c in enumerate(local):
            if c["id"] == cid:
                local[i] = entry
                replaced = True
                break
        if not replaced:
            local.append(entry)
        ch.save_local(DATA_DIR, local)
        with LOCK:
            STATE["channels"] = local
        log(f"保存频道：{name}（{cid}），{len(clean)} 个源，{'更新' if replaced else '新增'}")
        refresh_async()
        self._json({"ok": True, "id": cid, "updated": replaced})

    def import_m3u(self, payload: dict):
        text = payload.get("m3u") or ""
        if not text.strip():
            return self._json({"error": "empty"}, 400)
        parsed = ch.parse_m3u(text)
        if not parsed:
            return self._json({"error": "no_channel_found"}, 400)
        local = ch.load_local(DATA_DIR) or []
        existing = {(c["name"], c["sources"][0]["url"]) for c in local if c.get("sources")}
        added = 0
        for item in parsed:
            key = (item["name"], item["sources"][0]["url"])
            if key in existing:
                continue
            local.append(item)
            existing.add(key)
            added += 1
        ch.save_local(DATA_DIR, local)
        with LOCK:
            STATE["channels"] = local
        log(f"导入 M3U：解析 {len(parsed)} 条，新增 {added} 个频道")
        refresh_async()
        self._json({"ok": True, "added": added, "parsed": len(parsed)})

    def route_get(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if not self.gate(path):
            return

        if path.startswith("/api/stream/"):
            return self.serve_stream(path.rsplit("/", 1)[-1], query)
        if path == "/api/health":
            return self._json({
                "ok": True, "version": VERSION,
                "uptime_seconds": int(time.time() - STATE["started_at"]),
                "channels": len(STATE["channels"]),
                "online": sum(1 for c in STATE["channels"] if c.get("online")),
                "refreshing": STATE["refreshing"],
            })
        if path == "/api/meta":
            items = STATE["channels"]
            groups: dict[str, int] = {}
            for c in items:
                groups[c.get("group") or "其他"] = groups.get(c.get("group") or "其他", 0) + 1
            return self._json({
                "version": VERSION,
                "total": len(items),
                "online": sum(1 for c in items if c.get("online")),
                "groups": dict(sorted(groups.items(), key=lambda kv: -kv[1])),
                "updated_at": STATE["updated_at"],
                "refreshing": STATE["refreshing"],
                "progress": STATE["progress"],
                "last_error": STATE["last_error"],
                "direct_play": bool(STATE["config"].get("direct_play", True)),
                "portal": portal_links(self.req_prefix()),
                "hot_reload": HOT.status() if HOT else {},
            })
        if path == "/api/assets":
            return self._json(HOT.assets() if HOT else {"ok": False})
        if path == "/api/channels":
            return self.channel_list(query)
        if path.startswith("/api/channels/"):
            cid = path.rsplit("/", 1)[-1]
            target = find_channel(cid)
            if not target:
                return self._json({"error": "not_found"}, 404)
            return self._json(target)
        return self.static(path)

    def channel_list(self, query):
        group = (query.get("group", [""])[0] or "").strip()
        q = (query.get("q", [""])[0] or "").strip().lower()
        only_online = (query.get("online", [""])[0] or "") == "1"
        items = STATE["channels"]
        if group and group != "全部":
            items = [c for c in items if (c.get("group") or "") == group]
        if q:
            items = [c for c in items if q in c["name"].lower() or q in (c.get("desc") or "").lower()]
        if only_online:
            items = [c for c in items if c.get("online")]
        slim = [{
            "id": c["id"], "name": c["name"], "group": c.get("group", ""),
            "official": c.get("official", ""), "desc": c.get("desc", ""),
            "origin": c.get("origin", ""), "online": bool(c.get("online")),
            "latency": c.get("latency", 0), "hls": c.get("hls", True),
            "error": c.get("error", ""), "source_count": len(c.get("sources") or []),
        } for c in items]
        self._json({"total": len(slim), "items": slim})

    # ---------- 代理 ----------
    def serve_stream(self, cid: str, query):
        channel = find_channel(cid)
        if not channel:
            return self._json({"error": "channel_not_found"}, 404)
        if query.get("u"):
            try:
                target = b64d(query["u"][0])
            except Exception:
                return self._json({"error": "bad_url"}, 400)
        else:
            try:
                idx = int(query.get("i", ["0"])[0])
                target = channel["sources"][idx]["url"]
            except (ValueError, IndexError):
                return self._json({"error": "bad_source_index"}, 400)
        if not target.lower().startswith(("http://", "https://")):
            return self._json({"error": "bad_url"}, 400)
        try:
            self.pipe(channel, target, cid)
        except ClientGone:
            # 浏览器主动断开：切台、缓冲够了、关页面都会触发，正常现象
            logger.get().throttled(f"clientgone:{channel['id']}",
                                   f"客户端断开：{channel['name']}（切台/缓冲控制/关闭页面，正常）", 300,
                                   level="INFO")
        except Exception as exc:
            logger.get().throttled(f"stream:{channel['id']}",
                                   f"拉流失败：{channel['name']}（{ch.short_err(exc)}）", 300)
            try:
                self._json({"error": "upstream_failed", "detail": ch.short_err(exc)}, 502)
            except Exception:
                pass

    def req_prefix(self) -> str:
        raw = (self.headers.get("X-Forwarded-Prefix") or "").strip().rstrip("/")
        return raw if raw.startswith("/") else ""

    # ---------- 门户统一登录 ----------
    def portal_ok(self) -> bool:
        """挂在 nginx 子路径下时，校验门户 CloudAuth 签发的签名 Cookie。"""
        return portal_auth.user(self.headers.get("Cookie") or "",
                                str(STATE["config"].get("portal_auth_url") or "")) is not None

    def portal_login(self) -> str:
        return "/login.html?next=" + urllib.parse.quote(self.req_prefix() + "/", safe="/")

    def redirect_out(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def gate(self, path: str) -> bool:
        """统一入口门禁：返回 True 表示已放行，False 表示已写入拒绝响应。"""
        prefix = self.req_prefix()
        if not prefix or self.portal_ok() or path in ("/api/health", "/api/assets"):
            return True
        if path.startswith("/api/"):
            self._json({"error": "unauthorized", "login": self.portal_login(),
                        "portal_login": True}, 401)
        else:
            self.redirect_out(self.portal_login())
        return False

    def rewrite_playlist(self, text: str, base: str, cid: str) -> bytes:
        out = []
        uri_re = re.compile(r'URI="([^"]+)"')
        prefix = self.req_prefix()

        def prox(raw: str) -> str:
            return f"{prefix}/api/stream/{cid}?u={b64e(urllib.parse.urljoin(base, raw))}"

        for line in text.splitlines():
            s = line.strip()
            if not s:
                out.append("")
            elif s.startswith("#"):
                out.append(uri_re.sub(lambda m: f'URI="{prox(m.group(1))}"', line))
            else:
                out.append(prox(s))
        return ("\n".join(out) + "\n").encode("utf-8")

    def pipe(self, channel: dict, url: str, cid: str):
        headers = upstream_headers(channel)
        # 直播流不能按字节范围取：转发 Range 会拿到 206 + 不完整的 Content-Range，
        # 浏览器据此判定媒体格式错误（实测所有 MP3/TS 直播都会挂）。这里一律按实时流处理。
        req = urllib.request.Request(url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=20, context=SSL_CTX)
        ctype = (resp.headers.get("Content-Type") or "").lower()

        if "mpegurl" in ctype or url.lower().split("?")[0].endswith(".m3u8"):
            body = resp.read(3 * 1024 * 1024).decode("utf-8", "replace")
            final = resp.geturl()
            resp.close()
            if "#EXTM3U" in body:
                data = self.rewrite_playlist(body, final, cid)
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
                return

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
            return
        try:
            while True:
                chunk = resp.read(32768)
                if not chunk:
                    break
                try:
                    # 写浏览器这一段失败 = 客户端断开（切台/缓冲控制/关页面），正常
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except CLIENT_ABORT:
                    raise ClientGone()
        finally:
            resp.close()

    def static(self, path: str):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        rel = posixpath.normpath(rel)
        if rel.startswith(".."):
            return self._json({"error": "forbidden"}, 403)
        # 外部 static/ 优先、按文件回退到 exe 内置资源：改文件后刷新浏览器即生效
        full, is_external, _src = HOT.resolve_static(rel) if HOT else (None, False, "")
        if not full:
            return self._json({"error": "not_found"}, 404)
        ext = os.path.splitext(full)[1].lower()
        ctype = MIME.get(ext) or mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as fh:
            body = fh.read()
        if is_external:
            cache = "no-store, must-revalidate"
        else:
            cache = "public, max-age=86400" if "/vendor/" in path or ext in (".svg", ".png") else "no-cache"
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
    lg = logger.setup(log_dir, cfg.get("log_days") or 14, "tv")
    sys.excepthook = _excepthook
    HOT = hot_reload.HotReloader(APP_DIR, DEFAULTS, STATE, log, log_warn,
                                 bundled_static=STATIC_DIR)
    os.makedirs(DATA_DIR, exist_ok=True)
    log("=" * 60)
    log(f"云电视 v{VERSION} 启动")
    log(f"热更新目录：{HOT.external_static}（放入同名文件即可覆盖，无需重启）")
    log(f"外部静态文件：{len(HOT.external_files())} 个；config.json 改动自动重载")
    log(f"数据目录：{DATA_DIR}")
    log(f"日志文件：{lg.path_for()}（保留 {cfg.get('log_days') or 14} 天）")
    log(f"监听：{cfg.get('host')}:{cfg.get('port')}")
    hydrate()
    if cfg.get("probe_on_start"):
        refresh_async()

    host, port = cfg["host"], int(cfg["port"])
    httpd = None
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
        log("收到退出信号")
    finally:
        httpd.server_close()
        log("服务已停止")


if __name__ == "__main__":
    main()
