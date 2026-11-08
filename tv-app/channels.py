"""电视频道源管理：官方种子源、M3U 解析、并发探活、本地持久化。

设计原则（合规）：
  * 只预置电视台官方公开发布的直播地址（电视台自有 CDN / 官方网页播放器同源地址）
  * 不内置任何付费频道、加密频道、影视点播或第三方盗播列表
  * 用户自行添加的源由用户负责其合法性，程序只做播放与探活
"""

from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

PROBE_TIMEOUT = 10
PROBE_WORKERS = 24

# 央视网官方播放器使用的 CDN 模板（cnr/cntv 自有源，非第三方聚合）
CNTV_CDN = "http://ldncctvwbndhwy.cntv.myhwcdn.cn/ldncctvwbnd/ld{cid}_2/index.m3u8"

# (频道 id, 显示名, 分组, 官方页面, CDN 频道标识)
SEED = [
    ("cctv1",  "CCTV-1 综合",     "央视", "https://tv.cctv.com/live/cctv1/",  "cctv1"),
    ("cctv2",  "CCTV-2 财经",     "央视", "https://tv.cctv.com/live/cctv2/",  "cctv2"),
    ("cctv3",  "CCTV-3 综艺",     "央视", "https://tv.cctv.com/live/cctv3/",  "cctv3"),
    ("cctv4",  "CCTV-4 中文国际", "央视", "https://tv.cctv.com/live/cctv4/",  "cctv4"),
    ("cctv5",  "CCTV-5 体育",     "央视", "https://tv.cctv.com/live/cctv5/",  "cctv5"),
    ("cctv5p", "CCTV-5+ 体育赛事", "央视", "https://tv.cctv.com/live/cctv5plus/", "cctv5p"),
    ("cctv6",  "CCTV-6 电影",     "央视", "https://tv.cctv.com/live/cctv6/",  "cctv6"),
    ("cctv7",  "CCTV-7 国防军事", "央视", "https://tv.cctv.com/live/cctv7/",  "cctv7"),
    ("cctv8",  "CCTV-8 电视剧",   "央视", "https://tv.cctv.com/live/cctv8/",  "cctv8"),
    ("cctv9",  "CCTV-9 纪录",     "央视", "https://tv.cctv.com/live/cctv9/",  "cctv9"),
    ("cctv10", "CCTV-10 科教",    "央视", "https://tv.cctv.com/live/cctv10/", "cctv10"),
    ("cctv11", "CCTV-11 戏曲",    "央视", "https://tv.cctv.com/live/cctv11/", "cctv11"),
    ("cctv12", "CCTV-12 社会与法", "央视", "https://tv.cctv.com/live/cctv12/", "cctv12"),
    ("cctv13", "CCTV-13 新闻",    "央视", "https://tv.cctv.com/live/cctv13/", "cctv13"),
    ("cctv14", "CCTV-14 少儿",    "央视", "https://tv.cctv.com/live/cctv14/", "cctv14"),
    ("cctv15", "CCTV-15 音乐",    "央视", "https://tv.cctv.com/live/cctv15/", "cctv15"),
    ("cctv16", "CCTV-16 奥林匹克", "央视", "https://tv.cctv.com/live/cctv16/", "cctv16"),
    ("cctv17", "CCTV-17 农业农村", "央视", "https://tv.cctv.com/live/cctv17/", "cctv17"),
]

_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def seed_channels() -> list[dict]:
    out = [{
        "id": "demo",
        "name": "播放测试 · 开源示例流",
        "group": "系统",
        "official": "https://test-streams.mux.dev/",
        "desc": "开源动画 Big Buck Bunny（CC-BY），用于验证播放链路是否正常",
        "origin": "demo",
        "sources": [{"url": "https://test-streams.mux.dev/x36xhzz/url_2/193039199_mp4_h264_aac_ld_7.m3u8",
                     "label": "示例源", "hls": True}],
    }]
    for cid, name, group, official, cdn in SEED:
        out.append({
            "id": cid,
            "name": name,
            "group": group,
            "official": official,
            "desc": "央视官方公开直播",
            "origin": "seed",
            "sources": [{"url": CNTV_CDN.format(cid=cdn), "label": "官方 CDN", "hls": True}],
        })
    return out


def http_open(url: str, headers: dict | None = None, timeout: int = PROBE_TIMEOUT):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        **(headers or {}),
    })
    return urllib.request.urlopen(req, timeout=timeout, context=_SSL)


def probe(url: str, referer: str = "") -> dict:
    started = time.time()
    try:
        headers = {"Range": "bytes=0-4095"}
        if referer:
            headers["Referer"] = referer
        resp = http_open(url, headers, timeout=PROBE_TIMEOUT)
        ctype = (resp.headers.get("Content-Type") or "").lower()
        # 浏览器直连播放的前提：源站对任何来源都放行（ACAO: *）。
        # 只认通配符，避免把"回显探测方 Origin"误判成对我们的页面也放行。
        cors = (resp.headers.get("Access-Control-Allow-Origin") or "").strip() == "*"
        body = resp.read(4096)
        final_url = resp.geturl()
        resp.close()
        latency = int((time.time() - started) * 1000)
        head = body.lstrip()
        if head.startswith(b"#EXTM3U"):
            return {"ok": True, "latency": latency, "hls": True, "type": "hls",
                    "final": final_url, "note": "", "cors": cors}
        if ctype.startswith("video/") or ctype.startswith("audio/") or head[:1] == b"\x47":
            return {"ok": True, "latency": latency, "hls": False, "type": "media",
                    "final": final_url, "note": "", "cors": cors}
        if "html" in ctype:
            return {"ok": False, "latency": latency, "hls": False, "type": "html",
                    "final": final_url, "note": "返回的是网页，不是流地址", "cors": cors}
        return {"ok": False, "latency": latency, "hls": False, "type": ctype or "unknown",
                "final": final_url, "note": "无法识别的响应类型", "cors": cors}
    except Exception as exc:
        return {"ok": False, "latency": int((time.time() - started) * 1000), "hls": False,
                "type": "error", "final": url, "note": short_err(exc), "cors": False}


def short_err(exc: Exception) -> str:
    text = str(exc)
    if "timed out" in text or "timeout" in text:
        return "连接超时"
    if "getaddrinfo" in text or "Name or service" in text:
        return "域名解析失败"
    if "refused" in text:
        return "连接被拒绝"
    if "10053" in text or "软件中止" in text:
        return "连接被中止（10053：客户端断开或链路被重置）"
    if "10054" in text or "重置" in text:
        return "连接被重置"
    if "403" in text:
        return "被拒绝访问（403）"
    if "404" in text:
        return "地址不存在（404）"
    if "reset" in text:
        return "连接被重置"
    return text[:80]


def validate(channels: list[dict], progress=lambda m: None) -> list[dict]:
    tasks = [(c, i, s["url"]) for c in channels for i, s in enumerate(c["sources"])]
    total = max(1, len(tasks))
    done = 0
    results: dict[tuple[str, int], dict] = {}

    def work(task):
        nonlocal done
        ch, idx, url = task
        r = probe(url, ch.get("official") or "")
        results[(ch["id"], idx)] = r
        done += 1
        if done % 8 == 0 or done == total:
            progress(f"探活 {done}/{total}")

    with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
        list(pool.map(work, tasks))

    out = []
    for ch in channels:
        good, bad = [], []
        for i, src in enumerate(ch["sources"]):
            r = results.get((ch["id"], i), {})
            entry = dict(src)
            entry.update({
                "ok": bool(r.get("ok")),
                "latency": r.get("latency", 0),
                "hls": bool(r.get("hls")),
                "cors": bool(r.get("cors")),
                "note": r.get("note", ""),
                "checked_at": now_iso(),
            })
            (good if entry["ok"] else bad).append(entry)
        good.sort(key=lambda s: s["latency"])
        ch = dict(ch)
        ch["sources"] = good + bad
        ch["online"] = bool(good)
        ch["latency"] = good[0]["latency"] if good else 0
        ch["hls"] = good[0]["hls"] if good else True
        ch["error"] = "" if good else (bad[0]["note"] if bad else "没有配置源")
        out.append(ch)
    return out


M3U_ATTR = re.compile(r'([a-zA-Z\-]+)="([^"]*)"')


def parse_m3u(text: str) -> list[dict]:
    """解析 M3U/M3U8 播放列表，返回频道列表。"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out = []
    pending = None
    for ln in lines:
        if ln.startswith("#EXTINF"):
            attrs = {k.lower(): v for k, v in M3U_ATTR.findall(ln)}
            title = ln.split(",", 1)[1].strip() if "," in ln else ""
            name = attrs.get("tvg-name") or title or "未命名频道"
            group = attrs.get("group-title") or "自定义"
            pending = {"name": name, "group": group, "logo": attrs.get("tvg-logo", "")}
        elif ln.startswith("#"):
            continue
        elif ln.lower().startswith(("http://", "https://")):
            if pending is None:
                pending = {"name": urllib.parse.urlsplit(ln).netloc, "group": "自定义", "logo": ""}
            cid = make_id(pending["name"], len(out))
            out.append({
                "id": cid,
                "name": pending["name"],
                "group": pending["group"],
                "official": "",
                "desc": "来自导入的播放列表",
                "origin": "import",
                "sources": [{"url": ln, "label": "导入源",
                             "hls": ".m3u8" in ln.lower()}],
            })
            pending = None
    return out


def make_id(name: str, salt: int = 0) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not base:
        base = "ch"
    return f"{base[:24]}-{int(time.time()) % 100000}{salt}"[:40]


def store_path(data_dir: str) -> str:
    return os.path.join(data_dir, "channels.local.json")


def load_local(data_dir: str) -> list[dict] | None:
    path = store_path(data_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return data
        return data.get("channels")
    except Exception:
        return None


def save_local(data_dir: str, channels: list[dict]) -> None:
    os.makedirs(data_dir, exist_ok=True)
    path = store_path(data_dir)
    tmp = path + ".tmp"
    payload = {"updated_at": now_iso(), "channels": channels}
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def build(data_dir: str, progress=lambda m: None) -> list[dict]:
    """种子 + 用户自定义，合并后整体探活。"""
    local = load_local(data_dir)
    if local is None:
        channels = seed_channels()
        # 首次运行立刻落盘，便于用户直接在文件里改
        save_local(data_dir, channels)
    else:
        channels = local
    progress(f"频道 {len(channels)} 个，开始探活")
    return validate(channels, progress)
