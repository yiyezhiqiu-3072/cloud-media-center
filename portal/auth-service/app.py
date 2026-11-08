"""CloudAuth —— 云媒中心统一登录服务（单文件，Windows 可直接打包成 exe）

替代 nginx 自带的浏览器 Basic Auth 弹框：
  * 门户首页公开，点「进入收听 / 进入观看」时弹出自定义登录页
  * 账号密码仍读同一个 htpasswd 文件（{SHA} 格式，make-htpasswd.bat 生成）
  * 登录成功后签发 HMAC-SHA256 签名的 HttpOnly Cookie
  * /radio/ 与 /tv/ 两个后端通过 /auth/check 校验这个 Cookie

接口（nginx 把 /auth/ 反代到本机 8092）：
  GET  /auth/session   当前是否已登录 {ok, user}
  GET  /auth/check     校验会话，200 / 401（给 nginx 和两个后端用）
  POST /auth/login     {username,password,next} -> {ok,user,next} + Set-Cookie
  POST /auth/logout    清除会话
  GET  /auth/health    本机各端口探活，供门户首页显示「在线」状态
  GET  /auth/weather   国内天气代理（登录页与两个应用的天气组件共用）
  GET  /auth/ping      最简存活探测
  GET  /auth/diag      本机诊断：线程数 / 限流 / 账号文件 / 密钥指纹（仅 127.0.0.1）

天气数据源全部在国内：太平洋IP库 / ipip.net 定位 → 中国天气网 toy1 城市ID
→ d1 实况与今日高低温。不使用任何境外天气服务。

启动：CloudAuth.exe [--port 8092]
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import hot_reload

VERSION = "1.1.3"
IS_FROZEN = getattr(sys, "frozen", False)
APP_DIR = os.path.dirname(os.path.abspath(sys.executable)) if IS_FROZEN else os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")

COOKIE = "cm_session"
DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8092,
    # 账号文件沿用 nginx 的 htpasswd；make-htpasswd.bat 修改后立即生效
    "htpasswd": "C:/nginx/conf/htpasswd",
    "session_days": 7,
    # 两个后端端口，只用于 /auth/health 的探活显示
    "radio_port": 8090,
    "tv_port": 8091,
    "music_port": 8093,
    "work_port": 8094,
    "log_dir": "logs",
    "log_days": 14,
    # 天气数据源开关：默认只使用国内接口，不使用任何境外服务
    "weather_enabled": True,
    "weather_ttl": 600,
}

STATE = {"config": dict(DEFAULTS)}
HOT = None   # main() 里初始化：config.json 热重载
LOG_LOCK = threading.Lock()
# 登录失败限流：key=ip -> [失败次数, 首次失败时间, 锁定到]
FAILS: dict[str, list] = {}
FAILS_LOCK = threading.Lock()

# 天气缓存：key=访客IP -> (过期时间, 数据)
WEATHER_CACHE: dict[str, tuple[float, dict]] = {}
WEATHER_LOCK = threading.Lock()
# 同一时刻只允许一个线程取天气，避免外网不通时每次页面加载都堆一个慢线程
WEATHER_FETCH_LOCK = threading.Lock()

# /auth/health 探活结果缓存（状态页会定时轮询，避免每次都建 3 个 socket）
HEALTH_CACHE: dict = {"at": 0.0, "data": None}
HEALTH_LOCK = threading.Lock()
CHECK_LOG: dict = {"at": 0.0, "n": 0}

# nginx 用 HTTP/1.1 keep-alive 反代；空闲连接必须限时释放线程
IDLE_TIMEOUT = 10

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CN_REFERER = "https://www.weather.com.cn/"
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


# ---------------------------------------------------------------- 配置 / 日志
def load_config() -> dict:
    cfg = dict(DEFAULTS)
    path = os.path.join(APP_DIR, "config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                cfg.update({k: v for k, v in json.load(fh).items() if k in DEFAULTS})
        except Exception as exc:
            print(f"config.json 读取失败，使用默认值：{exc}")
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a in ("--port", "-p") and i + 1 < len(args):
            try:
                cfg["port"] = int(args[i + 1])
            except ValueError:
                pass
        if a in ("--version", "-v"):
            print(f"CloudAuth {VERSION}")
            raise SystemExit(0)
    return cfg


def log_dir() -> str:
    d = STATE["config"].get("log_dir") or "logs"
    return d if os.path.isabs(d) else os.path.join(APP_DIR, d)


def log(msg: str, level: str = "INFO") -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {level:<5} {msg}"
    with LOG_LOCK:
        try:
            print(line, flush=True)
        except Exception:
            pass
        try:
            os.makedirs(log_dir(), exist_ok=True)
            path = os.path.join(log_dir(), f"auth-{datetime.now().strftime('%Y-%m-%d')}.txt")
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass


def cleanup_logs() -> None:
    try:
        d = log_dir()
        if not os.path.isdir(d):
            return
        cutoff = time.time() - int(STATE["config"].get("log_days") or 14) * 86400
        for name in os.listdir(d):
            if name.startswith("auth-") and name.endswith(".txt"):
                p = os.path.join(d, name)
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
    except Exception:
        pass


# ---------------------------------------------------------------- 账号文件
def htpasswd_path() -> str:
    p = str(STATE["config"].get("htpasswd") or "")
    return p if os.path.isabs(p) else os.path.join(APP_DIR, p)


def read_users() -> dict[str, str]:
    """返回 {用户名: 密码串}，支持 {SHA}、{PLAIN}、明文三种写法。"""
    out: dict[str, str] = {}
    try:
        with open(htpasswd_path(), "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                user, _, rest = line.partition(":")
                out[user.strip()] = rest.split(":", 1)[0].strip()
    except FileNotFoundError:
        log(f"账号文件不存在：{htpasswd_path()}", "WARN")
    except Exception as exc:
        log(f"账号文件读取失败：{exc}", "WARN")
    return out


def password_ok(stored: str, password: str) -> bool:
    if stored.startswith("{SHA}"):
        want = base64.b64encode(hashlib.sha1(password.encode("utf-8")).digest()).decode("ascii")
        return hmac.compare_digest(stored[5:], want)
    if stored.startswith("{PLAIN}"):
        return hmac.compare_digest(stored[7:], password)
    return hmac.compare_digest(stored, password)


# ---------------------------------------------------------------- 会话签名
def secret() -> bytes:
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "secret.key")
    if not os.path.exists(path):
        with open(path, "wb") as fh:
            fh.write(os.urandom(32))
        log("已生成会话签名密钥 data/secret.key（删除它会让所有已登录用户重新登录）")
    with open(path, "rb") as fh:
        return fh.read()


def sign_text(user: str, exp: int, stored: str) -> str:
    """把账号文件里的密码散列也纳入签名：改密码后旧的登录态立即失效。"""
    raw = f"{user}.{exp}.{stored}".encode("utf-8")
    return hmac.new(SECRET, raw, hashlib.sha256).hexdigest()[:32]


def make_token(user: str, stored: str) -> str:
    exp = int(time.time()) + max(1, int(STATE["config"].get("session_days") or 7)) * 86400
    uid = base64.urlsafe_b64encode(user.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{uid}.{exp}.{sign_text(user, exp, stored)}"


def parse_token(token: str) -> str | None:
    """校验签名/有效期/账号是否仍存在，返回用户名或 None。"""
    try:
        uid, exp_s, sig = token.split(".", 2)
        exp = int(exp_s)
        if exp <= int(time.time()):
            return None
        user = base64.urlsafe_b64decode(uid + "=" * (-len(uid) % 4)).decode("utf-8")
        stored = read_users().get(user)
        if stored is None:
            return None
        if not hmac.compare_digest(sig, sign_text(user, exp, stored)):
            return None
        return user
    except Exception:
        return None


def cookie_value(headers) -> str:
    for part in (headers.get("Cookie") or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == COOKIE:
            return value
    return ""


def safe_next(raw: str) -> str:
    """只允许站内绝对路径，避免登录后被跳转到外部站点。"""
    p = urllib.parse.unquote(raw or "").strip()
    if not p.startswith("/") or p.startswith("//"):
        return "/"
    if p.startswith(("/radio", "/tv", "/music", "/work", "/status")):
        return p
    return "/"


# ---------------------------------------------------------------- 限流
def client_ip(headers, peer: str) -> str:
    for name in ("X-Real-IP", "X-Forwarded-For"):
        v = (headers.get(name) or "").split(",")[0].strip()
        if v:
            return v
    return peer


def locked(ip: str) -> bool:
    with FAILS_LOCK:
        rec = FAILS.get(ip)
        return bool(rec and rec[2] > time.time())


def note_fail(ip: str) -> None:
    now = time.time()
    with FAILS_LOCK:
        rec = FAILS.get(ip)
        # 300 秒窗口过期、或上一次锁定已经到期，就重新开始计数；
        # rec[2] 为 0（从未锁定）时不能被这条误判为「锁定已过期」
        if not rec or now - rec[1] > 300 or (rec[2] and rec[2] <= now):
            rec = [0, now, 0]
        rec[0] += 1
        # 只在「未锁定且达到阈值」时开始锁定，锁定期间不反复延长
        if rec[0] >= 5 and rec[2] <= now:
            rec[2] = now + 60
            log(f"登录失败过多，临时锁定 {ip} 60 秒", "WARN")
        FAILS[ip] = rec
        # 清理陈旧的 IP 记录，避免长期运行无限增长
        for key, val in list(FAILS.items()):
            if now - val[1] > 600:
                FAILS.pop(key, None)


def note_ok(ip: str) -> None:
    with FAILS_LOCK:
        FAILS.pop(ip, None)


# ---------------------------------------------------------------- 天气（仅国内数据源）
# 链路：访客 IP → 太平洋 IP 库 / ipip.net 定位中文省市
#       → 中国天气网 toy1 查城市 ID → d1 实况 + 今日高低温
# d1 接口校验 Referer，浏览器无法伪造，因此由本服务端代取；
# 前端只请求同源 /auth/weather，不直连任何境外天气服务。
def http_text(url: str, referer: str = "", timeout: float = 4) -> str:
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
        raw = resp.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", "replace")


def num_of(text) -> float | None:
    m = re.search(r"-?\d+(?:\.\d+)?", str(text or ""))
    return float(m.group(0)) if m else None


def clean_area(name: str, suffixes: tuple[str, ...]) -> str:
    s = (name or "").strip()
    for suf in suffixes:
        if s.endswith(suf):
            return s[:-len(suf)]
    return s


def geo_by_ip(ip: str) -> tuple[str, str]:
    """返回 (城市, 省份)，中文；两个数据源都在国内。"""
    # 太平洋 IP 库：支持指定 IP，返回 GBK 中文
    try:
        url = "https://whois.pconline.com.cn/ipJson.jsp?ip=" + urllib.parse.quote(ip) + "&json=true"
        d = json.loads(http_text(url, "https://whois.pconline.com.cn/"))
        city = clean_area(d.get("city") or "", ("特别行政区", "自治州", "地区", "盟", "市", "县", "区"))
        region = clean_area(d.get("pro") or "", ("壮族自治区", "回族自治区", "维吾尔自治区", "自治区", "省", "市"))
        if city:
            return city, region
    except Exception:
        pass
    # 兜底：ipip.net（国内，按服务器出口 IP）
    try:
        d = json.loads(http_text("https://myip.ipip.net/json"))
        loc = (d.get("data") or {}).get("location") or []
        if len(loc) >= 3:
            return clean_area(loc[2], ("市",)), clean_area(loc[1], ("省", "市"))
    except Exception:
        pass
    return "", ""


def city_weather_id(city: str, region: str) -> str:
    """中国天气网城市 ID（JSONP 接口，服务端可自由设置 Referer）。"""
    for name in (city, region):
        if not name:
            continue
        try:
            url = ("https://toy1.weather.com.cn/search?cityname="
                   + urllib.parse.quote(name) + "&callback=cb")
            txt = http_text(url, CN_REFERER)
            start, end = txt.find("("), txt.rfind(")")
            if start < 0 or end <= start:
                continue
            arr = json.loads(txt[start + 1:end])
            if arr:
                return str(arr[0]["ref"]).split("~", 1)[0]
        except Exception:
            pass
    return ""


def weather_icon(desc: str) -> str:
    """按中文天气描述选图标，不依赖任何天气代码表。"""
    s = desc or ""
    if "雷" in s:
        return "storm"
    if "雨" in s and "雪" in s:
        return "sleet"
    if "雪" in s:
        return "snow"
    if "雨" in s:
        return "rain"
    if any(k in s for k in ("雾", "霾", "沙", "尘", "浮")):
        return "fog"
    if "多云" in s:
        return "cloudsun"
    if "晴" in s:
        return "sun"
    return "cloud"


def fetch_weather(ip: str) -> dict | None:
    city, region = geo_by_ip(ip)
    cid = city_weather_id(city, region)
    if not cid:
        return None
    # 实况：温度 / 湿度 / 风速 / 天气描述
    txt = http_text(f"https://d1.weather.com.cn/sk_2d/{cid}.html", CN_REFERER)
    m = re.search(r"var\s+dataSK\s*=\s*(\{.*\})", txt, re.S)
    if not m:
        return None
    sk = json.loads(m.group(1))
    temp = num_of(sk.get("temp"))
    if temp is None:
        return None
    desc = (sk.get("weather") or "").strip() or "多云"
    # 今日最高/最低（失败不致命）
    hi = lo = None
    try:
        dz = http_text(f"https://d1.weather.com.cn/dingzhi/{cid}.html", CN_REFERER)
        mm = re.search(r"=\s*(\{.*?\});", dz, re.S)
        if mm:
            info = json.loads(mm.group(1)).get("weatherinfo") or {}
            hi = num_of(info.get("temp"))
            lo = num_of(info.get("tempn"))
    except Exception:
        pass
    return {
        "ok": True,
        "city": city or (sk.get("cityname") or "").strip(),
        "region": region,
        "country": "中国",
        "temp": temp,
        "feels": temp,
        "desc": desc,
        "icon": weather_icon(desc),
        "max": hi,
        "min": lo,
        "hum": num_of(sk.get("SD")),
        "wind": num_of((sk.get("wse") or "").replace("km/h", "")),
        "source": "中国天气网",
        "updated": datetime.now().strftime("%H:%M"),
    }


# ---------------------------------------------------------------- HTTP
def note_check_fail() -> None:
    """汇总记录会话校验失败，便于判断是密钥/账号文件问题还是偶发超时。"""
    now = time.time()
    CHECK_LOG["n"] += 1
    if now - CHECK_LOG["at"] > 60:
        if CHECK_LOG["n"] > 1:
            log(f"会话校验失败 {CHECK_LOG['n']} 次/最近一分钟（Cookie 过期、改过密码或密钥变动属正常）", "WARN")
        CHECK_LOG["at"] = now
        CHECK_LOG["n"] = 0


class AuthServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128      # 默认只有 5，状态页轮询 + 多应用并发时容易排队
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    server_version = "CloudAuth/" + VERSION
    protocol_version = "HTTP/1.1"

    def parse_request(self):
        ok = super().parse_request()
        if ok and HOT:
            try:
                HOT.refresh()
            except Exception:
                pass
        return ok

    def setup(self):
        super().setup()
        # 关键：nginx 到本服务是 HTTP/1.1 keep-alive。不设超时的话，每个空闲连接
        # 会永久占一个线程，长期运行后线程堆积，登录与 /auth/check 会变慢甚至超时，
        # 表现为「输入密码进不去、重启 CloudAuth 才恢复」。
        try:
            self.connection.settimeout(IDLE_TIMEOUT)
        except OSError:
            pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (socket.timeout, TimeoutError):
            self.close_connection = True     # 空闲连接安静关闭，释放线程

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
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        })

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self.route_get()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as exc:
            log(f"GET {self.path} 异常：{exc}", "WARN")
            try:
                self._json({"ok": False, "error": "internal_error"}, 500)
            except Exception:
                pass

    def do_POST(self):
        try:
            self.route_post()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as exc:
            log(f"POST {self.path} 异常：{exc}", "WARN")
            try:
                self._json({"ok": False, "error": "internal_error"}, 500)
            except Exception:
                pass

    def body_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    def user_of(self) -> str | None:
        return parse_token(cookie_value(self.headers))

    def route_get(self):
        path = urllib.parse.urlsplit(self.path).path
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        user = self.user_of()

        if path == "/auth/weather":
            if not STATE["config"].get("weather_enabled", True):
                return self._json({"ok": False, "error": "weather_disabled"}, 503)
            ip = client_ip(self.headers, self.client_address[0])
            ttl = max(60, int(STATE["config"].get("weather_ttl") or 600))
            now = time.time()
            force = "refresh" in query
            with WEATHER_LOCK:
                hit = WEATHER_CACHE.get(ip)
                if hit and hit[0] > now and not force:
                    return self._json(hit[1])
            # 同一时刻只允许一个线程访问外部天气接口；外网不通时不会堆一串慢线程
            if not WEATHER_FETCH_LOCK.acquire(blocking=False):
                with WEATHER_LOCK:
                    hit = WEATHER_CACHE.get(ip)
                return self._json(hit[1] if hit else
                                  {"ok": False, "error": "weather_busy"}, 502)
            try:
                try:
                    data = fetch_weather(ip)
                except Exception as exc:
                    log(f"天气获取失败 {ip}：{exc}", "WARN")
                    data = None
                with WEATHER_LOCK:
                    # 失败也做 60 秒负缓存，避免每次页面加载都重复一串超时请求
                    WEATHER_CACHE[ip] = (now + (ttl if data else 60),
                                         data or {"ok": False, "error": "weather_unavailable"})
                    for key, val in list(WEATHER_CACHE.items()):
                        if val[0] <= now:
                            WEATHER_CACHE.pop(key, None)
                    result = WEATHER_CACHE[ip][1]
                return self._json(result, 200 if data else 502)
            finally:
                WEATHER_FETCH_LOCK.release()

        if path == "/auth/ping":
            return self._json({"ok": True, "version": VERSION})

        if path == "/auth/health":
            now = time.time()
            with HEALTH_LOCK:
                cached = HEALTH_CACHE["data"]
                if cached and now - HEALTH_CACHE["at"] < 2:
                    return self._json(cached)
            def listening(port: int) -> bool:
                try:
                    with socket.create_connection(("127.0.0.1", int(port)), timeout=0.4):
                        return True
                except OSError:
                    return False
            cfg = STATE["config"]
            data = {
                "ok": True, "version": VERSION, "auth": True,
                "radio": listening(cfg.get("radio_port", 8090)),
                "tv": listening(cfg.get("tv_port", 8091)),
                "music": listening(cfg.get("music_port", 8093)),
                "work": listening(cfg.get("work_port", 8094)),
                "uptime": int(now - STARTED),
                "hot_reload": HOT.status() if HOT else {},
            }
            with HEALTH_LOCK:
                HEALTH_CACHE["at"] = now
                HEALTH_CACHE["data"] = data
            return self._json(data)

        if path == "/auth/diag":
            # 只允许服务器本机直接访问（nginx 反代会带 X-Real-IP，外部访问会被拒）
            if client_ip(self.headers, self.client_address[0]) not in ("127.0.0.1", "::1"):
                return self._json({"error": "forbidden"}, 403)
            hp = htpasswd_path()
            try:
                st = os.stat(hp)
                hp_info = {"path": hp, "exists": True, "size": st.st_size, "mtime": int(st.st_mtime)}
            except OSError:
                hp_info = {"path": hp, "exists": False}
            with FAILS_LOCK:
                lockouts = sum(1 for r in FAILS.values() if r[2] > time.time())
                fail_records = len(FAILS)
            with WEATHER_LOCK:
                wc = len(WEATHER_CACHE)
            return self._json({
                "ok": True, "version": VERSION, "uptime": int(time.time() - STARTED),
                "threads": threading.active_count(), "idle_timeout": IDLE_TIMEOUT,
                "login_fail_records": fail_records, "active_lockouts": lockouts,
                "weather_cache": wc, "htpasswd": hp_info,
                "users": len(read_users()),
                "secret_fingerprint": hashlib.sha256(SECRET).hexdigest()[:12],
            })

        if path in ("/auth/session", "/auth/check"):
            if user:
                return self._json({"ok": True, "user": user}, 200, {"X-Auth-User": user})
            if path.endswith("check"):
                note_check_fail()
                return self._json({"ok": False, "user": ""}, 401)
            return self._json({"ok": False, "user": ""}, 200)

        if path == "/auth/logout":
            return self._json({"ok": True}, 200, self.clear_cookie())
        self._json({"error": "not_found"}, 404)

    def route_post(self):
        path = urllib.parse.urlsplit(self.path).path
        ip = client_ip(self.headers, self.client_address[0])

        if path == "/auth/logout":
            return self._json({"ok": True}, 200, self.clear_cookie())

        if path != "/auth/login":
            return self._json({"error": "not_found"}, 404)

        # 限流只在「密码错误」时生效：同一出口 IP 下别人输错密码，
        # 不该把密码正确的用户也挡在门外（旧版在这里就 429，是登录卡住的原因之一）
        data = self.body_json()
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
        nxt = safe_next(str(data.get("next") or "/"))

        users = read_users()
        if not users:
            log(f"登录失败 {ip}：账号文件为空或不存在 {htpasswd_path()}", "WARN")
            return self._json({"ok": False, "error": "账号文件不可用，请检查 htpasswd"}, 503)

        stored = users.get(username)
        # 用户名不存在时也做一次散列计算，减少时序差异
        ok = password_ok(stored or "{SHA}" + base64.b64encode(b"\x00" * 20).decode(), password) and stored is not None
        if not ok:
            was_locked = locked(ip)
            note_fail(ip)
            log(f"登录失败 {ip} 用户 {username or '(空)'}")
            if was_locked or locked(ip):
                return self._json({"ok": False, "error": "尝试次数过多，请 60 秒后再试"}, 429)
            return self._json({"ok": False, "error": "用户名或密码不正确"}, 401)

        note_ok(ip)
        token = make_token(username, stored)
        days = max(1, int(STATE["config"].get("session_days") or 7))
        cookie = (f"{COOKIE}={token}; Path=/; Max-Age={days * 86400}; "
                  f"HttpOnly; SameSite=Lax")
        log(f"登录成功 {ip} 用户 {username} -> {nxt}")
        return self._json({"ok": True, "user": username, "next": nxt}, 200,
                          {"Set-Cookie": cookie, "X-Auth-User": username})

    def clear_cookie(self) -> dict:
        return {"Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"}


def main() -> None:
    global SECRET, STARTED, HOT
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    STATE["config"] = load_config()
    HOT = hot_reload.HotReloader(APP_DIR, DEFAULTS, STATE, log,
                                 lambda m: log(m, "WARN"))
    SECRET = secret()
    STARTED = time.time()
    cleanup_logs()
    log("=" * 56)
    log(f"CloudAuth v{VERSION} 启动，监听 {STATE['config']['host']}:{STATE['config']['port']}")
    log(f"账号文件：{htpasswd_path()}")
    log(f"密钥文件：{os.path.join(DATA_DIR, 'secret.key')}")
    log(f"空闲连接超时：{IDLE_TIMEOUT}s（防止 keep-alive 线程堆积）")
    log("config.json 热重载已启用（host / port 等需重启的键只提示不应用）")
    httpd = AuthServer((STATE["config"]["host"], int(STATE["config"]["port"])), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("收到退出信号")
    finally:
        httpd.server_close()


SECRET = b""
STARTED = time.time()

if __name__ == "__main__":
    main()
