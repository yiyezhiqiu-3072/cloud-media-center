"""门户统一登录（CloudAuth）会话校验模块 —— 云收音机 / 云电视共用。

只有在通过 nginx 子路径访问（X-Forwarded-Prefix 非空）时才启用；
独立运行（直接访问 8080/8081 端口）时后端沿用原有登录逻辑，不依赖本服务。

校验方式：把浏览器带来的 cm_session Cookie 转给 CloudAuth 的 /auth/check，
由它验证 HMAC 签名、有效期与账号是否仍存在。成功结果缓存 20 秒，
避免每个静态资源 / 视频分片都发一次本机 HTTP 请求。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.request

COOKIE = "cm_session"
DEFAULT_CHECK_URL = "http://127.0.0.1:8092/auth/check"
CACHE_TTL = 20

_CACHE: dict[str, tuple[float, str]] = {}
_LOCK = threading.Lock()


def token(cookie_header: str) -> str:
    for part in (cookie_header or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == COOKIE:
            return value
    return ""


def user(cookie_header: str, check_url: str = "") -> str | None:
    """校验通过返回用户名，否则返回 None。"""
    tok = token(cookie_header)
    if not tok:
        return None
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(tok)
        if hit and hit[0] > now:
            return hit[1]
        for key, val in list(_CACHE.items()):
            if val[0] <= now:
                _CACHE.pop(key, None)
    endpoint = (check_url or "").strip() or DEFAULT_CHECK_URL
    name = None
    try:
        req = urllib.request.Request(endpoint, headers={"Cookie": f"{COOKIE}={tok}"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
        if data.get("ok") and data.get("user"):
            name = str(data["user"])
    except Exception:
        name = None
    if name:
        with _LOCK:
            _CACHE[tok] = (now + CACHE_TTL, name)
    return name


def digest(user_name: str) -> str:
    """生成与现有日志/收听面板一致的身份摘要。"""
    return hashlib.sha256(f"portal:{user_name}".encode("utf-8")).hexdigest()
