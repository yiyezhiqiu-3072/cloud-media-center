"""hot_reload.py —— 云媒中心各服务共用的热更新模块。

解决「改一个前端文件 / 改一行配置，就要停 exe、重新 PyInstaller 打包、再启动」：

1. 静态资源外置：优先从 <exe 所在目录>/static/ 读，按**文件**回退到打包在 exe 内的
   _MEIPASS/static。每次请求实时读磁盘，改完文件刷新浏览器即生效，无需重启 exe。
2. config.json 热重载：每个请求开头检查 mtime，变了就重新载入；host/port/data_dir/log_dir
   这类「启动时才生效」的键不会误应用，只记日志提示需要重启。
3. 诊断：status() / assets() / fingerprint() 供 /api/meta、/auth/health、/api/assets 使用。

注意：本模块只解决「前端文件 + 配置」的热更新；Python 后端逻辑改动仍需重新打包 exe。
"""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import threading

# 只在启动时被使用的键：监听地址、数据/日志目录。运行中改动无法安全应用
COLD_KEYS = frozenset({"host", "port", "data_dir", "log_dir"})


class HotReloader:
    def __init__(self, app_dir: str, defaults: dict, state: dict, log_fn,
                 warn_fn=None, bundled_static: str = "", external_name: str = "static"):
        self.app_dir = os.path.abspath(app_dir)
        self.bundled_static = os.path.abspath(bundled_static) if bundled_static else ""
        self.external_static = os.path.join(self.app_dir, external_name)
        self.config_path = os.path.join(self.app_dir, "config.json")
        self.defaults = defaults
        self.state = state
        self.log = log_fn
        self.warn = warn_fn or log_fn
        self._lock = threading.Lock()
        self._mtime = 0.0
        self._cold_notified: set[str] = set()
        self.init_mtime()

    # ---------------- config.json ----------------
    def init_mtime(self) -> None:
        try:
            self._mtime = os.path.getmtime(self.config_path)
        except OSError:
            self._mtime = 0.0

    def refresh(self) -> None:
        """每个 HTTP 请求开头调用；无变化时只有一次 stat，开销可忽略。"""
        try:
            m = os.path.getmtime(self.config_path)
        except OSError:
            return
        if m == self._mtime:
            return
        with self._lock:
            if m == self._mtime:
                return
            self._mtime = m
            try:
                with open(self.config_path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
            except Exception as exc:
                self.warn(f"config.json 热重载失败（继续用原配置）：{exc}")
                return
            cfg = self.state["config"]
            cold = [k for k in COLD_KEYS if k in raw and raw[k] != cfg.get(k)]
            hot = {k: v for k, v in raw.items()
                   if k in self.defaults and k not in COLD_KEYS}
            changed = [k for k, v in hot.items() if cfg.get(k) != v]
            cfg.update(hot)
            if changed:
                self.log("config.json 已热重载：" + "、".join(sorted(changed)))
            for k in cold:
                if k not in self._cold_notified:
                    self.warn(f"config.json 的 {k} 改动需重启 exe 才能生效")
                    self._cold_notified.add(k)

    # ---------------- 静态资源 ----------------
    def resolve_static(self, rel: str):
        """返回 (绝对路径, 是否来自外部目录, 来源标签)；外部优先、按文件回退到内置。"""
        rel = posixpath.normpath((rel or "").lstrip("/"))
        if rel.startswith(".."):
            return None, False, ""
        for base, tag in ((self.external_static, "external"),
                          (self.bundled_static, "bundled")):
            if not base:
                continue
            p = os.path.join(base, rel.replace("/", os.sep))
            if os.path.isdir(p):
                p = os.path.join(p, "index.html")
            if not os.path.isfile(p):
                continue
            try:
                if os.path.commonpath([os.path.abspath(p), base]) != base:
                    continue
            except ValueError:
                continue
            return p, tag == "external", tag
        return None, False, ""

    def external_files(self) -> list[str]:
        try:
            return sorted(f for f in os.listdir(self.external_static)
                          if os.path.isfile(os.path.join(self.external_static, f)))
        except OSError:
            return []

    def fingerprint(self) -> str:
        """外部静态文件的指纹：给前端轮询判断是否出现更新。"""
        h = hashlib.sha256()
        for name in self.external_files():
            try:
                st = os.stat(os.path.join(self.external_static, name))
                h.update(f"{name}:{st.st_mtime_ns}:{st.st_size};".encode())
            except OSError:
                pass
        return h.hexdigest()[:16]

    # ---------------- 诊断 ----------------
    def status(self) -> dict:
        files = self.external_files()
        return {
            "hot_reload": True,
            "static_dir": self.external_static,
            "static_enabled": bool(self.bundled_static),
            "external_files": files,
            "external_count": len(files),
            "config_path": self.config_path,
            "config_mtime": int(self._mtime),
            "restart_required_keys": sorted(COLD_KEYS),
        }

    def assets(self) -> dict:
        return {
            "ok": True,
            "fingerprint": self.fingerprint(),
            "static_dir": self.external_static,
            "external_files": self.external_files(),
            "config_mtime": int(self._mtime),
        }
