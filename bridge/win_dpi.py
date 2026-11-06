"""Windows DPI 感知：让窗口坐标、鼠标坐标、屏幕截图落在同一套像素上。

2026-11-06 事故：加了第二块屏幕后系统主屏缩放变成 125%，而桥的 **worker
进程是 DPI-unaware** —— ``GetWindowRect`` / ``SetCursorPos`` 拿到的是被
Windows 虚拟化过的逻辑坐标，``PIL.ImageGrab`` 抓的却是物理像素。于是截图
整体偏左上、尺寸小 25%，OCR 读到的是旁边钉钉窗口的内容，点击也跟着错位，
表现为「只转了一条、另一条没按发送」。

GUI 进程（``app/gui.py`` 的 ``_enable_dpi_awareness``）早就声明了 Per-Monitor
V2，但那段只在 ``if __name__ == "__main__"`` 里跑，worker 走 ``run.py`` 完全
没经过。所以这里做成任何入口都能在**最早**调用的公共函数。

必须在创建任何窗口 / HDC 之前调用；重复调用无害（Windows 会返回失败但
不改状态）。
"""
from __future__ import annotations

import ctypes
import sys

# DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = (HANDLE)-4
_PER_MONITOR_AWARE_V2 = -4
_PROCESS_PER_MONITOR_DPI_AWARE = 2


def enable() -> bool:
    """声明 Per-Monitor V2 DPI 感知，返回是否成功。非 Windows 直接 False。"""
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    try:
        if user32.SetProcessDpiAwarenessContext(
                ctypes.c_void_p(_PER_MONITOR_AWARE_V2)):
            return True
    except Exception:                                   # noqa: BLE001
        pass
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(
                _PROCESS_PER_MONITOR_DPI_AWARE) == 0:
            return True
    except Exception:                                   # noqa: BLE001
        pass
    try:
        return bool(user32.SetProcessDPIAware())
    except Exception:                                   # noqa: BLE001
        return False


def awareness() -> int:
    """当前进程的 DPI 感知级别：0=unaware / 1=system / 2=per-monitor。

    排障用：worker 不是 2 就说明坐标会错位，别去调 OCR 阈值。
    """
    if sys.platform != "win32":
        return -1
    try:
        from ctypes import wintypes as wt
        kernel32 = ctypes.windll.kernel32
        value = wt.UINT()
        h = kernel32.GetCurrentProcess()
        hr = ctypes.windll.shcore.GetProcessDpiAwareness(
            ctypes.c_void_p(h), ctypes.byref(value))
        return value.value if hr == 0 else -1
    except Exception:                                   # noqa: BLE001
        return -1
