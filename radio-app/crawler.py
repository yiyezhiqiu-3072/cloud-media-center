"""频道抓取与流地址校验。

职责：
  1. 从公开电台目录接口抓取电台条目（多镜像容错）
  2. 归一化：去重、分类、省份识别、镜像合并、置顶排序
  3. 并发探活：实际请求流地址，确认可播放并记录延迟
  4. 原子落盘到 data/channels.json
"""

from __future__ import annotations

import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import logger

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# 公开电台目录（radio-browser 社区数据库）多镜像
SOURCES = [
    "https://de1.api.radio-browser.info/json/stations/search?countrycode=CN&hidebroken=true&order=clickcount&reverse=true&limit=400",
    "https://de2.api.radio-browser.info/json/stations/search?countrycode=CN&hidebroken=true&order=clickcount&reverse=true&limit=400",
    "https://nl1.api.radio-browser.info/json/stations/search?countrycode=CN&hidebroken=true&order=clickcount&reverse=true&limit=200",
    "https://at1.api.radio-browser.info/json/stations/search?countrycode=CN&hidebroken=true&order=clickcount&reverse=true&limit=200",
]

MAX_STATIONS = 420          # 单轮最多保留条目
VALIDATE_WORKERS = 32       # 探活并发
VALIDATE_TIMEOUT = 9        # 单次探活超时（秒）
MAX_MIRRORS = 3             # 每个电台保留的备用地址数

PROVINCES = [
    "北京", "上海", "天津", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江",
    "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
    "广东", "海南", "四川", "贵州", "云南", "陕西", "甘肃", "青海", "内蒙古",
    "广西", "西藏", "宁夏", "新疆",
    # 港澳台按中国地区规范命名
    "中国香港", "中国澳门", "中国台湾",
]

CATEGORY_RULES = [
    ("音乐", ["音乐", "歌", "music", "经典", "流行", "摇滚", "爵士", "民谣", "hifi", "古典", "怀旧", "金曲"]),
    ("交通", ["交通", "路况", "汽车", "高速", "出行"]),
    ("财经", ["经济", "财经", "股市", "证券", "理财", "business", "商业", "财富"]),
    ("体育", ["体育", "足球", "篮球", "sport"]),
    ("曲艺", ["戏曲", "评书", "相声", "文艺", "故事", "小说", "曲艺", "文化", "读书", "听书", "娱乐"]),
    ("民族", ["民族", "藏语", "维语", "维吾尔", "蒙古", "哈萨克", "朝鲜语", "壮语", "彝语", "藏语广播"]),
    ("少儿", ["少儿", "儿童", "亲子", "动漫", "kids"]),
    ("新闻", ["新闻", "资讯", "时讯", "之声", "news", "talk", "评论", "观察", "综合广播"]),
]

# 国家级电台显式归类（优先于关键词规则）
NAME_OVERRIDES = [
    ("中国之声", "新闻"), ("经济之声", "财经"), ("音乐之声", "音乐"),
    ("经典音乐广播", "音乐"), ("文艺之声", "曲艺"), ("中华之声", "新闻"),
    ("环球资讯", "新闻"), ("中国乡村之声", "综合"), ("乡村之声", "综合"),
    ("大湾区之声", "综合"), ("阅读之声", "曲艺"), ("台海之声", "新闻"),
    ("神州之声", "新闻"), ("藏语广播", "民族"), ("维语广播", "民族"),
    ("哈萨克语广播", "民族"), ("朝鲜语广播", "民族"),
]

# 国家级电台规范名（用于合并 CNR-1 / CMG 等重复条目）
CANONICAL = [
    "中国之声", "经济之声", "音乐之声", "经典音乐广播", "文艺之声", "中华之声",
    "环球资讯广播", "中国乡村之声", "大湾区之声", "阅读之声", "台海之声", "神州之声",
]
_CANON_RE = re.compile(r"^(?:cnr|cmg|中央广播电视总台|中央人民广播电台)\s*[-–—]?\s*\d*\s*[-–—]?\s*", re.I)
_BITRATE_RE = re.compile(r"(\d{2,3})\s*k", re.I)

# 置顶权重：数值越小越靠前
BOOST = [
    (0, ["中国之声"]),
    (1, ["经济之声", "音乐之声"]),
    (2, ["经典音乐广播", "文艺之声", "中华之声", "环球资讯", "中国乡村之声", "都市广播", "阅读之声", "大湾区之声"]),
    (3, ["交通广播", "交通之声", "新闻广播", "音乐广播", "经济广播"]),
]

_BAD_NAME = re.compile(r"^(unknown|test|untitled|null|$)|\btv\b|电视|电视台", re.I)
_HLS_HINT = re.compile(r"\.m3u8($|\?)", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _norm_name(name: str) -> str:
    name = re.sub(r"\s+", " ", (name or "").strip())
    return name.replace("（", "(").replace("）", ")")


def _canonical_name(name: str) -> str:
    """把 'CNR-1 中国之声'、'CMG 环球资讯广播' 之类归一到规范名。"""
    stripped = _CANON_RE.sub("", name).strip(" -–—")
    for canon in CANONICAL:
        if stripped == canon or stripped.startswith(canon):
            return canon
    return name


def _key(name: str) -> str:
    return re.sub(r"[\s\-—_·|/\\()\[\]（）【】,，.。:：]+", "", name).lower()


def infer_category(name: str, tags: str) -> str:
    for canon, cat in NAME_OVERRIDES:
        if canon in name:
            return cat
    hay = f"{name} {tags}".lower()
    for cat, words in CATEGORY_RULES:
        if any(w in hay for w in words):
            return cat
    return "综合"


def infer_bitrate(url: str, given: int) -> int:
    if given:
        return given
    m = _BITRATE_RE.search(url)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return 0
    return 0


def infer_codec(url: str, given: str) -> str:
    given = (given or "").strip().upper()
    if given and given not in ("UNKNOWN", "N/A", "NONE"):
        return given
    low = url.lower()
    if ".m3u8" in low:
        return "HLS"
    if ".mp3" in low:
        return "MP3"
    if ".aac" in low or "aac" in low:
        return "AAC"
    return ""


def infer_province(name: str) -> str:
    for p in PROVINCES:
        if p.replace("中国", "") in name or p in name:
            return p
    return "全国"


def boost_rank(name: str) -> int:
    for rank, words in BOOST:
        if any(w in name for w in words):
            return rank
    return 50


def short_err(exc: Exception) -> str:
    text = str(exc)
    if "timed out" in text or "timeout" in text:
        return "连接超时"
    if "getaddrinfo" in text or "Name or service" in text:
        return "域名解析失败"
    if "refused" in text:
        return "连接被拒绝"
    if "403" in text:
        return "被拒绝访问（403）"
    if "404" in text:
        return "地址不存在（404）"
    if "reset" in text:
        return "连接被重置"
    return text[:80]


def http_get(url: str, timeout: float = 12, byte_limit: int = 12000, headers: dict | None = None):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        **(headers or {}),
    })
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.status, resp.headers, resp.read(byte_limit)


def fetch_source(url: str) -> list[dict]:
    try:
        status, _, body = http_get(url, timeout=20, byte_limit=6 * 1024 * 1024)
        if status != 200:
            return []
        data = json.loads(body.decode("utf-8", "replace"))
        return data if isinstance(data, list) else []
    except Exception as exc:  # 镜像失败不影响其它源
        logger.get().warn(f"抓取源不可用：{url.split('/')[2]} -> {short_err(exc)}")
        return []


_BITRATES = {
    # 键为 (版本组, 层号)：层号 3=Layer I，2=Layer II，1=Layer III
    (1, 3): [32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
    (1, 2): [32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
    (1, 1): [32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    (2, 3): [32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
    (2, 2): [8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
    (2, 1): [8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
_SAMPLE_RATES = {
    3: [44100, 48000, 32000],   # MPEG1
    2: [22050, 24000, 16000],   # MPEG2
    0: [11025, 12000, 8000],    # MPEG2.5
}


def _mp3_frame(buf: bytes, i: int):
    """若 i 处是合法 MPEG 帧头，返回 (帧长, 版本, 层, 采样率)，否则 None。"""
    if i + 4 > len(buf):
        return None
    b0, b1, b2, b3 = buf[i], buf[i + 1], buf[i + 2], buf[i + 3]
    if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
        return None
    version = (b1 >> 3) & 0x03
    layer = (b1 >> 1) & 0x03
    if version == 1 or layer == 0 or version not in _SAMPLE_RATES:
        return None
    br_idx = (b2 >> 4) & 0x0F
    sr_idx = (b2 >> 2) & 0x03
    if br_idx in (0, 15) or sr_idx == 3:
        return None
    bitrate = _BITRATES[(1 if version == 3 else 2, layer)][br_idx - 1] * 1000
    sample_rate = _SAMPLE_RATES[version][sr_idx]
    padding = (b2 >> 1) & 0x01
    if layer == 3:
        samples = 384
    elif layer == 2:
        samples = 1152
    else:
        samples = 1152 if version == 3 else 576
    frame_len = int(samples / 8 * bitrate / sample_rate) + padding
    if not 100 <= frame_len <= 6000:
        return None
    return frame_len, version, layer, sample_rate


def _mp3_chain(buf: bytes, start: int, need: int = 4) -> int:
    """从 start 开始顺着帧长往下走，返回连续合法帧数。"""
    i = start
    count = 0
    first = _mp3_frame(buf, i)
    if not first:
        return 0
    version, layer, rate = first[1], first[2], first[3]
    while True:
        f = _mp3_frame(buf, i)
        if not f or f[1] != version or f[2] != layer or f[3] != rate:
            return count
        count += 1
        i += f[0]
        if count >= need or i + 4 >= len(buf):
            return count


def mp3_ok(buf: bytes, scan: int = 2048) -> bool:
    """连续 4 帧都能对上才算真音频；加密流/随机数据几乎不可能满足。"""
    if buf[:3] == b"ID3":
        # 有 ID3 头时从标签后继续校验
        try:
            size = ((buf[6] & 0x7F) << 21) | ((buf[7] & 0x7F) << 14) | ((buf[8] & 0x7F) << 7) | (buf[9] & 0x7F)
        except IndexError:
            size = 0
        return _mp3_chain(buf, 10 + size) >= 2 or _mp3_chain(buf, 0) >= 2
    end = min(len(buf) - 4, scan)
    for i in range(0, end):
        if _mp3_chain(buf, i) >= 4:
            return True
    return False


def aac_ok(buf: bytes, scan: int = 2048) -> bool:
    """ADTS：连续 3 帧按帧长都能接上才算数。"""
    end = min(len(buf) - 9, scan)
    for i in range(0, end):
        if buf[i] != 0xFF or (buf[i + 1] & 0xF6) != 0xF0:
            continue
        j = i
        count = 0
        while count < 3:
            if j + 7 > len(buf) or buf[j] != 0xFF or (buf[j + 1] & 0xF6) != 0xF0:
                break
            flen = ((buf[j + 3] & 0x03) << 11) | (buf[j + 4] << 3) | (buf[j + 5] >> 5)
            if flen < 16:
                break
            j += flen
            count += 1
        if count >= 3:
            return True
    return False


def audio_kind(body: bytes, ctype: str) -> str:
    """识别音频容器类型；识别不出说明是加密流或错误页。"""
    if not body:
        return ""
    head = body[:16]
    if mp3_ok(body):
        return "MP3"
    if aac_ok(body):
        return "AAC"
    if body[:4] == b"OggS":
        return "OGG"
    if body[4:8] == b"ftyp":
        return "MP4"
    if body[:4] == b"fLaC":
        return "FLAC"
    if body[:4] == b"RIFF":
        return "WAV"
    if head[:1] == b"\x47" and head[188:189] == b"\x47":
        return "TS"
    return ""


def probe(url: str) -> dict:
    """探测单个流地址是否真的能播（不只看 HTTP 200，还要认出音频数据）。"""
    started = time.time()
    try:
        headers = {"Range": "bytes=0-8191"}
        status, resp_headers, body = http_get(url, timeout=VALIDATE_TIMEOUT, byte_limit=8192, headers=headers)
        latency = int((time.time() - started) * 1000)
        ctype = (resp_headers.get("Content-Type") or "").lower()
        head = body.lstrip()

        if head[:7] == b"#EXTM3U" or "mpegurl" in ctype:
            return {"ok": head[:7] == b"#EXTM3U", "latency": latency, "hls": True,
                    "codec": "HLS", "note": "" if head[:7] == b"#EXTM3U" else "播放列表异常"}

        if status not in (200, 206):
            return {"ok": False, "latency": latency, "hls": False, "codec": "",
                    "note": f"HTTP {status}"}

        kind = audio_kind(body, ctype)
        if kind:
            return {"ok": True, "latency": latency, "hls": False, "codec": kind, "note": ""}

        # 前 8KB 认不出来，可能只是帧头靠后：再抓 64KB 复核一次
        try:
            _, _, bigger = http_get(url, timeout=VALIDATE_TIMEOUT, byte_limit=65536,
                                    headers={"Range": "bytes=0-65535"})
        except Exception:
            bigger = b""
        if bigger:
            kind = audio_kind(bigger, ctype)
            if kind:
                return {"ok": True, "latency": latency, "hls": False, "codec": kind, "note": ""}

        if "html" in ctype or head[:1] == b"<":
            note = "返回的是网页，不是流地址"
        elif not body or (ctype.startswith(("audio/", "video/")) and len(bigger or body) < 1024):
            note = "没有返回音频数据"
        elif ctype.startswith(("audio/", "video/", "application/octet-stream")):
            # 容器不常见但服务端明确标了音视频，放行，播放时若失败会自动切下一路
            codec = ctype.split("/")[1].split(";")[0].upper() if "/" in ctype else ""
            return {"ok": True, "latency": latency, "hls": False, "codec": codec, "note": "未识别的音频封装"}
        else:
            note = "不是可播放的音频流"
        return {"ok": False, "latency": latency, "hls": False, "codec": "", "note": note}
    except Exception as exc:
        return {"ok": False, "latency": int((time.time() - started) * 1000), "hls": False,
                "codec": "", "note": short_err(exc)}


def normalize(items: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for it in items:
        raw_name = _norm_name(it.get("name") or "")
        name = _canonical_name(raw_name)
        url = (it.get("url_resolved") or it.get("url") or "").strip()
        if not name or not url.lower().startswith(("http://", "https://")):
            continue
        if _BAD_NAME.search(name):
            continue
        tags = (it.get("tags") or "").replace(",", " ").strip()
        key = _key(name)
        if not key:
            continue
        entry = merged.get(key)
        votes = int(it.get("votes") or 0)
        if entry is None:
            merged[key] = {
                "key": key,
                "name": name,
                "homepage": (it.get("homepage") or "").strip(),
                "favicon": (it.get("favicon") or "").strip(),
                "tags": tags,
                "category": infer_category(name, tags),
                "region": infer_province(name),
                "votes": votes,
                "bitrate": infer_bitrate(url, int(it.get("bitrate") or 0)),
                "codec": infer_codec(url, (it.get("codec") or "")),
                "hint_hls": bool(_HLS_HINT.search(url)),
                "urls": [url],
            }
        else:
            entry["votes"] = max(entry["votes"], votes)
            if tags and tags not in entry["tags"]:
                entry["tags"] = (entry["tags"] + " " + tags).strip()[:120]
            if url not in entry["urls"] and len(entry["urls"]) < MAX_MIRRORS:
                entry["urls"].append(url)
            if not entry["bitrate"]:
                entry["bitrate"] = infer_bitrate(url, int(it.get("bitrate") or 0))
            if not entry["codec"]:
                entry["codec"] = infer_codec(url, (it.get("codec") or ""))

    stations = []
    for e in merged.values():
        e["boost"] = boost_rank(e["name"])
        stations.append(e)
    stations.sort(key=lambda s: (s["boost"], -s["votes"], s["name"]))
    return stations[:MAX_STATIONS]


def validate_all(stations: list[dict], progress=lambda msg: None) -> list[dict]:
    tasks = []
    for st in stations:
        for i, url in enumerate(st["urls"]):
            tasks.append((st, i, url))

    results: dict[tuple[str, int], dict] = {}
    done = 0
    total = max(1, len(tasks))
    lock = threading.Lock()

    def work(task):
        nonlocal done
        st, i, url = task
        r = probe(url)
        with lock:
            results[(st["key"], i)] = r
            done += 1
            if done % 25 == 0 or done == total:
                progress(f"校验流地址 {done}/{total}")

    with ThreadPoolExecutor(max_workers=VALIDATE_WORKERS) as pool:
        list(pool.map(work, tasks))

    out = []
    for idx, st in enumerate(stations):
        good, bad = [], []
        hls_any = st.get("hint_hls", False)
        for i, url in enumerate(st["urls"]):
            r = results.get((st["key"], i), {})
            if r.get("ok"):
                good.append({"url": url, "latency": r.get("latency", 0), "hls": r.get("hls", False)})
                hls_any = hls_any or r.get("hls", False)
            else:
                bad.append({"url": url, "latency": r.get("latency", 0)})
        if not good:
            continue
        good.sort(key=lambda x: x["latency"])
        codec = "HLS" if good[0]["hls"] else (st.get("codec") or "")
        out.append({
            "id": f"r{idx:04d}",
            "name": st["name"],
            "category": st["category"],
            "region": st["region"],
            "tags": st["tags"],
            "homepage": st["homepage"],
            "favicon": st["favicon"],
            "bitrate": st["bitrate"],
            "codec": codec,
            "hls": hls_any,
            "votes": st["votes"],
            "boost": st["boost"],
            "streams": good,
            "bad_streams": bad,
            "latency": good[0]["latency"],
            "online": True,
        })
    # 稳定排序 → 按流地址去重（同一路流只保留排名最靠前的条目）→ 重新编号
    out.sort(key=lambda s: (s["boost"], -s["votes"], s["name"]))
    used: set[str] = set()
    deduped: list[dict] = []
    for st in out:
        urls = {s["url"] for s in st["streams"]}
        if urls and urls <= used:
            continue
        used |= urls
        deduped.append(st)
    for i, st in enumerate(deduped):
        st["id"] = f"r{i+1:04d}"
    return deduped


def crawl(data_dir: str, progress=lambda msg: None) -> dict:
    progress("正在抓取电台目录…")
    raw: list[dict] = []
    got_source = False
    for src in SOURCES:
        chunk = fetch_source(src)
        if chunk:
            got_source = True
            raw.extend(chunk)
            if len(raw) >= MAX_STATIONS * 2:
                break

    if not got_source:
        existing = load_store(data_dir)
        if existing and existing.get("stations"):
            progress("抓取源暂时不可用，保留上一次的频道数据")
            existing["stale"] = True
            return existing
        raise RuntimeError("所有抓取源均不可用，请检查服务器网络或代理设置")

    stations = normalize(raw)
    progress(f"共 {len(stations)} 个电台，开始探活…")
    validated = validate_all(stations, progress)

    payload = {
        "version": 2,
        "updated_at": _now(),
        "total": len(validated),
        "raw_total": len(raw),
        "categories": _count(validated, "category"),
        "regions": _count(validated, "region"),
        "stations": validated,
        "stale": False,
    }
    save_store(data_dir, payload)
    progress(f"完成，可用频道 {len(validated)} 个")
    return payload


def _count(stations: list[dict], field: str) -> dict:
    counts: dict[str, int] = {}
    for s in stations:
        counts[s[field]] = counts.get(s[field], 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def store_path(data_dir: str) -> str:
    return os.path.join(data_dir, "channels.json")


def load_store(data_dir: str) -> dict | None:
    path = store_path(data_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def save_store(data_dir: str, payload: dict) -> None:
    os.makedirs(data_dir, exist_ok=True)
    path = store_path(data_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def warm_socket() -> None:
    """避免首次 TLS 握手在探活阶段抖动。"""
    try:
        socket.setdefaulttimeout(VALIDATE_TIMEOUT + 3)
    except Exception:
        pass


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    warm_socket()
    target = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    logger.setup(os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"), 14, "radio")
    started = time.time()
    result = crawl(target, progress=lambda m: print(m, flush=True))
    logger.get().info(f"抓取结束：耗时 {time.time() - started:.1f}s，可用频道 {result['total']} 个")
