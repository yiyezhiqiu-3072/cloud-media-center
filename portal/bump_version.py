#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云媒中心统一发版脚本。

一次调用完成三件事，保证「版本号 / 更新内容 / 发版时间」不会漏登记：
  1. 修改对应 app.py 里的 VERSION 常量（传 '-' 则沿用现有版本，用于纯前端更新）
  2. 在 portal/version.js 的 CM_RELEASES 台账头部追加本次条目，
     at 取当前系统时间，格式 YYYY-MM-DD HH:MM:SS（精确到秒）
  3. 把母版 version.js 同步到 4 个应用的 static/（以及门户自身）

命令行用法：
  python portal/bump_version.py music 1.1.0 "新增服务端自动转码" "修复 wav 播放"
  python portal/bump_version.py auth - "前端：版本号支持点击查看更新记录"

也可以在别的 Python 脚本里直接调用 bump(...) 批量发版（中文内容更稳妥）：
  from bump_version import bump
  bump("music", "1.1.0", ["新增…", "修复…"])

发版后记得重新打包对应 exe（版本号来自后端），前端文件可直接热更新。
"""
import argparse
import datetime
import json
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER = os.path.join(ROOT, "portal", "version.js")
MARKER = "var CM_RELEASES = "

# app-key -> (app.py 相对路径, 服务名)
APPS = {
    "music": ("music-app/app.py", "云音乐"),
    "radio": ("radio-app/app.py", "云收音机"),
    "tv": ("tv-app/app.py", "云电视"),
    "work": ("work-app/app.py", "工作日志"),
    "auth": ("portal/auth-service/app.py", "登录服务"),
}
# 母版 + 4 个应用副本（门户页直接用 portal/version.js）
SYNC = [
    "portal/version.js",
    "radio-app/static/version.js",
    "tv-app/static/version.js",
    "music-app/static/version.js",
    "work-app/static/version.js",
]


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _split_releases(src):
    """返回 (台账 dict, JSON 起始下标, JSON 结束下标)。括号配平，兼容多行 JSON。"""
    i = src.find(MARKER)
    if i < 0:
        raise RuntimeError("version.js 里找不到 " + MARKER)
    i += len(MARKER)
    if i >= len(src) or src[i] != "{":
        raise RuntimeError("CM_RELEASES 不是 JSON 对象")
    depth = 0
    in_str = False
    esc = False
    j = i
    while j < len(src):
        c = src[j]
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
                    j += 1
                    break
        j += 1
    else:
        raise RuntimeError("CM_RELEASES 括号未配平")
    obj = json.loads(src[i:j])
    return obj, i, j


def current_version(app):
    src = _read(os.path.join(ROOT, APPS[app][0]))
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', src, re.M)
    if not m:
        raise RuntimeError("在 " + APPS[app][0] + " 里找不到 VERSION 常量")
    return m.group(1)


def set_version(app, new):
    path = os.path.join(ROOT, APPS[app][0])
    src = _read(path)
    out, n = re.subn(r'^(VERSION\s*=\s*)"[^"]+"',
                     lambda m: m.group(1) + '"' + new + '"',
                     src, count=1, flags=re.M)
    if n != 1:
        raise RuntimeError("修改 VERSION 失败：" + path)
    _write(path, out)


def bump(app, new_version, notes, when=None):
    """发版。new_version 传 '-' 表示沿用现有 VERSION（纯前端更新）。
    notes 为更新内容列表；when 可显式指定时间字符串（一般留空取当前秒）。
    """
    if app not in APPS:
        raise ValueError("未知应用：" + app + "；可选 " + ", ".join(APPS))
    if isinstance(notes, str):
        notes = [notes]
    notes = [str(x).strip() for x in notes if str(x).strip()]
    if not notes:
        raise ValueError("本次更新内容不能为空")

    label = APPS[app][1]
    old = current_version(app)
    if new_version and new_version != "-":
        set_version(app, new_version)
        version = new_version
    else:
        version = old

    at = when or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    src = _read(MASTER)
    obj, i, j = _split_releases(src)
    entry = obj.setdefault(app, {"label": label, "releases": []})
    entry["label"] = label
    rels = entry.setdefault("releases", [])
    # 同版本重复发版：覆盖旧条目，时间更新为本次
    rels[:] = [r for r in rels if r.get("v") != version]
    rels.insert(0, {"v": version, "at": at, "items": notes})
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    _write(MASTER, src[:i] + text + src[j:])
    for rel in SYNC[1:]:
        shutil.copyfile(MASTER, os.path.join(ROOT, rel))

    return {
        "app": app, "label": label, "old": old, "version": version,
        "at": at, "items": len(notes), "synced": len(SYNC),
    }


def main(argv=None):
    p = argparse.ArgumentParser(description="云媒中心发版：递增版本号并登记本次更新内容与时间")
    p.add_argument("app", choices=sorted(APPS), help="应用键")
    p.add_argument("version", help="新版本号；传 - 表示不改 VERSION")
    p.add_argument("notes", nargs="+", help="本次更新内容，每条一个字符串")
    a = p.parse_args(argv)
    r = bump(a.app, a.version, a.notes)
    print("已发版：{label} v{old} -> v{version}".format(**r))
    print("发版时间：{at}（精确到秒）".format(**r))
    print("本次条目：{items} 条".format(**r))
    print("已同步 version.js：{synced} 份".format(**r))
    print("下一步：重新打包 {app} 对应的 exe，或把 version.js 覆盖到部署目录 static/".format(**r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
