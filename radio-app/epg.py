"""节目单（EPG）模块

数据来源：蜻蜓 FM 移动站频道页内嵌的 window.__initStores
  - 含 basicInfo.name 与 playBill.{yesterday,today,tomorrow}
  - 只做只读解析，用于展示「今天播什么」，不下载、不转存音频

流程：
  1. 从电台的流地址里提取蜻蜓频道 id（/live/<id>/ 或 /channels/<id>）
  2. 拉取频道页，核对频道名与本地电台名是否吻合（防止镜像地址串台）
  3. 解析出节目表，按北京时间归一化为 {start, end, title}
  4. 内存 + 磁盘缓存，默认 3 小时过期
"""

from __future__ import annotations

import difflib
import json
import os
import re
import ssl
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

PAGE = "https://m.qtfm.cn/channels/{qid}/"
SEARCH = "https://search.qtfm.cn/v3/search?include=channel&k={k}&page=1&pagesize=12"
CACHE_TTL = 3 * 3600          # 节目单缓存有效期（秒）
NAME_THRESHOLD = 0.55         # 频道名相似度阈值（最长公共子串占比）
MAX_CANDIDATES = 4
MAX_SEARCH_TRY = 3

_QID_RE = re.compile(r"(?:lhttp|ls|lhttp-hw|live)[a-z0-9.\-]*\.(?:qtfm\.cn|qingting\.fm)/live/(\d+)"
                     r"|qtfm\.cn/(?:channels|vchannels)/(\d+)", re.I)

_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE

_LOCK = threading.Lock()
_MEM: dict[str, dict] = {}      # qid -> {ts, name, days}
_MAP: dict[str, dict] = {}      # station_id -> {qid, score, name, ts}


def _now() -> datetime:
    return datetime.now(CST)


def normalize_name(name: str) -> str:
    s = (name or "").lower()
    s = re.sub(r"fm\s*\d+(\.\d+)?", "", s)
    s = re.sub(r"\d+(\.\d+)?\s*(mhz|兆赫|千赫|khz)", "", s)
    s = re.sub(r"[\s\-—_·|/\\()\[\]（）【】,，.。:：、!！?？'\"“”‘’]+", "", s)
    for word in ("广播电台", "广播", "电台", "频道", "在线收听"):
        s = s.replace(word, "")
    return s


def _is_cjk(text: str) -> bool:
    return all("\u4e00" <= ch <= "\u9fff" for ch in text)


def name_score(local: str, remote: str) -> float:
    a, b = normalize_name(local), normalize_name(remote)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # 频率数字对不上（如 500 首 vs 50 首、FM94.7 vs FM97.4）直接判否
    da = re.sub(r"\D", "", a)
    db = re.sub(r"\D", "", b)
    if da and db and da != db:
        return 0.0
    if a in b or b in a:
        return 0.92
    # 电台名前两个字通常就是城市/台别，开头不一致基本是两台（宜春 vs 伊犁）
    if len(a) >= 2 and len(b) >= 2 and _is_cjk(a[:2]) and _is_cjk(b[:2]) and a[:2] != b[:2]:
        return 0.0
    # 用「最长公共子串」占短名的比例判断，避免「中国之声 / 中国校园之声」这类近似串台
    block = difflib.SequenceMatcher(None, a, b).find_longest_match(0, len(a), 0, len(b))
    return block.size / max(1, min(len(a), len(b)))


def candidate_ids(station: dict) -> list[str]:
    ids: list[str] = []
    for src in station.get("streams") or []:
        for m in _QID_RE.finditer(src.get("url") or ""):
            qid = m.group(1) or m.group(2)
            if qid and qid not in ids:
                ids.append(qid)
    return ids[:MAX_CANDIDATES]


def _http_json(url: str, timeout: int = 12):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA_MOBILE,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://m.qtfm.cn/",
    })
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
        return json.loads(resp.read(512 * 1024).decode("utf-8", "replace"))


def search_candidates(name: str) -> list[dict]:
    """按名称在蜻蜓搜索直播频道，返回 [{qid, title}]。"""
    if not name:
        return []
    try:
        data = _http_json(SEARCH.format(k=urllib.parse.quote(name)))
        docs = (((data or {}).get("data") or {}).get("data") or {}).get("docs") or []
    except Exception:
        return []
    out = []
    for d in docs:
        if not isinstance(d, dict) or d.get("type") != "channel_live":
            continue
        cid = d.get("id")
        title = (d.get("title") or "").strip()
        if cid and title:
            out.append({"qid": str(cid), "title": title})
    return out


def _extract_stores(html: str) -> dict | None:
    marker = "window.__initStores"
    i = html.find(marker)
    if i < 0:
        return None
    j = html.find("{", i)
    if j < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    end = None
    for k in range(j, len(html)):
        c = html[k]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = k + 1
                    break
    if end is None:
        return None
    try:
        return json.loads(html[j:end])
    except Exception:
        return None


def _iso_to_hhmm(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(CST)
        return dt.strftime("%H:%M")
    except Exception:
        return ""


def _parse_day(items: list) -> list[dict]:
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        title = (it.get("title") or "").strip()
        if not title:
            continue
        start, end, start_iso, end_iso = "", "", "", ""
        raw_from = it.get("from") or ""
        if len(raw_from) >= 12:
            try:
                start = f"{raw_from[8:10]}:{raw_from[10:12]}"
            except Exception:
                start = ""
        if it.get("startTime"):
            start_iso = it["startTime"]
            start = start or _iso_to_hhmm(start_iso)
        if it.get("endTime"):
            end_iso = it["endTime"]
            end = _iso_to_hhmm(end_iso)
        if not start and it.get("durationStr"):
            parts = re.split(r"\s*[~-]\s*", it["durationStr"])
            if len(parts) == 2:
                start, end = parts[0].strip(), parts[1].strip()
        out.append({
            "title": title,
            "start": start or "--:--",
            "end": end or "--:--",
            "start_iso": start_iso,
            "end_iso": end_iso,
            "pid": it.get("programId"),
        })
    return out


def fetch_channel(qid: str, force: bool = False) -> dict | None:
    """拉取并解析某个蜻蜓频道的三天节目单（带缓存）。"""
    now = time.time()
    with _LOCK:
        hit = _MEM.get(qid)
        if hit and not force and now - hit["ts"] < CACHE_TTL:
            return hit

    url = PAGE.format(qid=qid)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA_MOBILE,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://m.qtfm.cn/",
    })
    try:
        with urllib.request.urlopen(req, timeout=12, context=_SSL) as resp:
            html = resp.read(600 * 1024).decode("utf-8", "replace")
    except Exception:
        return None

    stores = _extract_stores(html)
    if not stores:
        return None
    store = stores.get("ChannelStore") or {}
    basic = store.get("basicInfo") or {}
    bill = store.get("playBill") or {}
    name = (basic.get("name") or "").strip()
    entry = {
        "ts": now,
        "qid": qid,
        "name": name,
        "cover": (basic.get("cover") or "").strip(),
        "days": {
            "yesterday": _parse_day(bill.get("yesterday") or []),
            "today": _parse_day(bill.get("today") or []),
            "tomorrow": _parse_day(bill.get("tomorrow") or []),
        },
    }
    if not entry["days"]["today"]:
        return None
    with _LOCK:
        _MEM[qid] = entry
    return entry


MAP_PATH = None  # 延迟绑定，见 bind()


def bind(data_dir: str) -> None:
    """绑定磁盘缓存路径，服务启动时调用一次。"""
    global MAP_PATH
    MAP_PATH = os.path.join(data_dir, "epg_map.json")
    if os.path.exists(MAP_PATH):
        try:
            with open(MAP_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                _MAP.update(data)
        except Exception:
            pass


def _save_map() -> None:
    if not MAP_PATH:
        return
    try:
        os.makedirs(os.path.dirname(MAP_PATH), exist_ok=True)
        tmp = MAP_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_MAP, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, MAP_PATH)
    except Exception:
        pass


def resolve(station: dict) -> dict | None:
    """为本地电台找到匹配的蜻蜓频道（按名字校验，避免镜像串台）。"""
    sid = station.get("id") or ""
    cached = _MAP.get(sid)
    if cached and time.time() - cached.get("ts", 0) < 12 * 3600:
        entry = _MEM.get(cached["qid"])
        if entry and entry["name"]:
            return entry

    local_name = station.get("name", "")
    best = None
    best_score = 0.0

    def consider(qid: str) -> bool:
        """试一个候选 id，返回是否已经足够好（完全同名）。"""
        nonlocal best, best_score
        entry = fetch_channel(qid)
        if not entry:
            return False
        score = name_score(local_name, entry["name"])
        if score > best_score:
            best, best_score = entry, score
        return score >= 0.999

    # 1) 先试流地址里带的频道 id
    for qid in candidate_ids(station):
        if consider(qid):
            break

    # 2) 不够像就按名字搜索兜底
    if best_score < NAME_THRESHOLD:
        for item in search_candidates(local_name)[:MAX_SEARCH_TRY]:
            if consider(item["qid"]):
                break

    if best and best_score >= NAME_THRESHOLD:
        _MAP[sid] = {
            "qid": best["qid"],
            "score": round(best_score, 3),
            "name": best["name"],
            "ts": time.time(),
        }
        _save_map()
        return best
    return None


def pick_day(entry: dict, day: str) -> str:
    return day if day in entry["days"] else "today"


def epg_for(station: dict, day: str = "today") -> dict:
    """对外接口：返回某电台某天的节目单 + 当前节目。"""
    entry = resolve(station)
    if not entry:
        return {
            "ok": False,
            "reason": "no_epg",
            "hint": "该电台暂无节目单数据",
            "station": station.get("id"),
        }
    day = pick_day(entry, day)
    programs = entry["days"][day]
    now = _now()
    current_idx = -1
    for i, p in enumerate(programs):
        try:
            s = datetime.fromisoformat(p["start_iso"].replace("Z", "+00:00")).astimezone(CST)
            e = datetime.fromisoformat(p["end_iso"].replace("Z", "+00:00")).astimezone(CST)
        except Exception:
            continue
        if s <= now < e:
            current_idx = i
            p["progress"] = round((now - s).total_seconds() / max(1, (e - s).total_seconds()), 3)
            break
    if current_idx >= 0:
        for i, p in enumerate(programs):
            p["is_now"] = i == current_idx
    return {
        "ok": True,
        "station": station.get("id"),
        "station_name": station.get("name"),
        "matched_name": entry["name"],
        "source": "蜻蜓FM",
        "day": day,
        "days": list(entry["days"].keys()),
        "programs": programs,
        "current_index": current_idx,
        "current": programs[current_idx] if current_idx >= 0 else None,
        "updated_at": datetime.fromtimestamp(entry["ts"], CST).isoformat(timespec="seconds"),
    }


def now_program(station: dict) -> dict | None:
    data = epg_for(station, "today")
    return data.get("current") if data.get("ok") else None
