"""日志：同时写控制台和本地 txt 文件。

特性：
  * 按天分文件：logs/radio-2026-11-04.txt
  * 每次写入立即 flush，进程被 kill 也不丢日志
  * 保留最近 N 天，启动时自动清理过期文件
  * 同一条重复消息可加节流（避免某个坏源刷满磁盘）
"""

from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime


class Logger:
    def __init__(self, log_dir: str | None, days: int = 14, prefix: str = "radio"):
        self.dir = log_dir
        self.days = max(1, int(days or 14))
        self.prefix = prefix
        self._lock = threading.Lock()
        self._date = None
        self._fh = None
        self._throttle: dict[str, float] = {}
        if self.dir:
            try:
                os.makedirs(self.dir, exist_ok=True)
            except Exception:
                self.dir = None
        self.cleanup()

    # ---------- 文件管理 ----------
    def path_for(self, day: str | None = None) -> str:
        day = day or datetime.now().strftime("%Y-%m-%d")
        return os.path.join(self.dir or "", f"{self.prefix}-{day}.txt")

    def _open(self) -> None:
        day = datetime.now().strftime("%Y-%m-%d")
        if self._fh is not None and self._date == day:
            return
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
        self._date = day
        try:
            self._fh = open(self.path_for(day), "a", encoding="utf-8", buffering=1)
        except Exception:
            self._fh = None

    def cleanup(self) -> None:
        """删除超过保留天数的旧日志。"""
        if not self.dir or not os.path.isdir(self.dir):
            return
        cutoff = time.time() - self.days * 86400
        for name in os.listdir(self.dir):
            if not (name.startswith(f"{self.prefix}-") and name.endswith(".txt")):
                continue
            path = os.path.join(self.dir, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except Exception:
                pass

    # ---------- 写入 ----------
    def write(self, level: str, msg: str) -> None:
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {level:<5} {msg}"
        with self._lock:
            try:
                print(line, flush=True)
            except Exception:
                pass
            if self.dir:
                self._open()
                if self._fh is not None:
                    try:
                        self._fh.write(line + "\n")
                        self._fh.flush()
                    except Exception:
                        pass

    def info(self, msg: str) -> None:
        self.write("INFO", msg)

    def warn(self, msg: str) -> None:
        self.write("WARN", msg)

    def error(self, msg: str) -> None:
        self.write("ERROR", msg)

    def throttled(self, key: str, msg: str, seconds: float = 300, level: str = "WARN") -> bool:
        """同一 key 在 seconds 内只记一次，返回是否真的写了。"""
        now = time.time()
        last = self._throttle.get(key, 0)
        if now - last < seconds:
            return False
        self._throttle[key] = now
        self.write(level, msg)
        return True


_CURRENT: Logger = Logger(None, prefix="radio")


def setup(log_dir: str | None, days: int = 14, prefix: str = "radio") -> Logger:
    global _CURRENT
    _CURRENT = Logger(log_dir, days, prefix)
    return _CURRENT


def get() -> Logger:
    return _CURRENT
