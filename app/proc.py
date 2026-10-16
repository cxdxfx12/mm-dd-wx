"""子进程封装：启动 / 读日志 / 停止。

停止策略（Windows 上有个坑）
    ``CREATE_NO_WINDOW`` 启动的子进程没有控制台，``CTRL_BREAK_EVENT`` 送不进去；
    而带隐藏控制台又只能在同控制台内投递控制事件。所以这里用**停止哨兵文件**：
    GUI 写一个文件，工作进程里的看门狗线程发现后，在主线程抛 KeyboardInterrupt，
    bridge 就会走它自己的 KeyboardInterrupt 收尾路径（关库、断开微信句柄）。
    哨兵失效时再依次降级 terminate → kill，保证一定停得掉。

其余要点
    * ``PYTHONUNBUFFERED`` 强制不缓冲，日志实时可见；
    * ``PYTHONWARNINGS=ignore`` 静音 wxauto 内嵌库的转义警告噪音。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

NO_WINDOW = 0x08000000
NEW_GROUP = 0x00000200
LOG_PREFIX = "@@LOG@@"


def parse_line(raw: str) -> dict | None:
    """识别 ``@@LOG@@{json}`` 形式的结构化日志；普通行返回 None。"""
    s = raw.strip()
    if not s.startswith(LOG_PREFIX):
        return None
    try:
        data = json.loads(s[len(LOG_PREFIX):])
        return data if isinstance(data, dict) else None
    except Exception:
        return None


class ProcRunner:
    """一个可启停、可读流的子进程。同一时刻只管理一个进程。"""

    def __init__(self, on_line: Callable[[str], None],
                 on_exit: Callable[[int], None] | None = None):
        self.on_line = on_line
        self.on_exit = on_exit
        self.proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._stop_flag = False
        self.started_at: float = 0.0
        self.argv: list[str] = []

    # ---------- 状态 ----------

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def exit_code(self) -> int | None:
        return None if self.proc is None else self.proc.poll()

    # ---------- 控制 ----------

    def start(self, argv: list[str], cwd: str | None = None,
              extra_env: dict | None = None, stop_file: str | Path | None = None) -> None:
        if self.alive():
            raise RuntimeError("已有任务在运行，请先停止")
        env = os.environ.copy()
        env.update({
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            # wxauto / 内嵌 uiautomation 里有大量无效转义与弃用写法，
            # 会把警告刷进日志，对使用者是纯噪音，这里静音。
            "PYTHONWARNINGS": "ignore",
        })
        if extra_env:
            env.update(extra_env)
        self._stop_flag = False
        self.stop_file = Path(stop_file) if stop_file else None
        if self.stop_file is not None:
            self.stop_file.unlink(missing_ok=True)
        self.argv = list(argv)
        self.proc = subprocess.Popen(
            argv, cwd=cwd, env=env, creationflags=NO_WINDOW | NEW_GROUP,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
            errors="replace", bufsize=1,
        )
        self.started_at = time.time()
        self._reader = threading.Thread(target=self._pump, daemon=True,
                                        name="proc-reader")
        self._reader.start()

    def _pump(self) -> None:
        proc = self.proc
        assert proc is not None and proc.stdout is not None
        try:
            for line in proc.stdout:
                try:
                    self.on_line(line.rstrip("\r\n"))
                except Exception:
                    pass
        except Exception:
            pass
        code = proc.wait()
        was_stop = self._stop_flag
        self.proc = None
        if self.on_exit is not None:
            try:
                self.on_exit(code if not was_stop else -999)
            except Exception:
                pass

    def stop(self, grace: float = 4.0) -> bool:
        """优雅停止：哨兵文件 → CTRL_BREAK → terminate → kill。返回是否已停止。"""
        if not self.alive():
            return True
        proc = self.proc
        self._stop_flag = True
        if self.stop_file is not None:
            try:
                self.stop_file.write_text("stop", encoding="utf-8")
            except Exception:
                pass
        deadline = time.time() + grace
        while time.time() < deadline:
            if proc.poll() is not None:
                return True
            time.sleep(0.1)
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        except Exception:
            pass
        deadline = time.time() + 1.5
        while time.time() < deadline:
            if proc.poll() is not None:
                return True
            time.sleep(0.1)
        try:
            proc.terminate()
        except Exception:
            pass
        deadline = time.time() + 4.0
        while time.time() < deadline:
            if proc.poll() is not None:
                return True
            time.sleep(0.1)
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        return True
