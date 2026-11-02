"""PC 钉钉群文件探针 v3：Win32 枚举窗口，点「文件夹」图标后对比差异。

    python tools/probe_pc_files3.py
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from bridge.config import load_config  # noqa: E402
from bridge.sources.dingtalk_ocr import DingTalkOCRSource, print_window  # noqa: E402

SHOTS = Path(__file__).resolve().parent / "shots"
ICON_FILES = (1513, 57)

user32 = ctypes.windll.user32


def list_windows(tag: str) -> dict[int, tuple[str, str, tuple]]:
    result: dict[int, tuple[str, str, tuple]] = {}
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _lp):
        if user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    for hwnd in found:
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        if not title and not cls:
            continue
        # 只看有实际面积的
        if rect.right - rect.left <= 0 or rect.bottom - rect.top <= 0:
            continue
        # 过滤无标题且非钉钉相关的杂项（浏览器内核隐藏窗等按类名粗筛）
        if not title and "DingTalk" not in cls.value and "ding" not in cls.value.lower():
            continue
        result[hwnd] = (title, cls.value, (rect.left, rect.top, rect.right, rect.bottom))

    print(f"--- 可见窗口（{tag}）共 {len(result)} 个 ---")
    for hwnd, (title, cls, rect) in result.items():
        print(f"  hwnd=0x{hwnd:X} class={cls!r} name={title!r} rect={rect}")
    return result


def main() -> int:
    cfg = load_config()
    src = DingTalkOCRSource(cfg)
    uia = src._load_uia()

    wnd = src._find_window()
    if wnd is None:
        print("未找到钉钉窗口")
        return 1
    wnd.SetActive()
    wnd.SetFocus()
    time.sleep(0.6)

    before = list_windows("点击前")

    wr = wnd.BoundingRectangle
    uia.Click(x=wr.left + ICON_FILES[0], y=wr.top + ICON_FILES[1], waitTime=2.0)

    after = list_windows("点击后")

    new_hwnds = [h for h in after if h not in before]
    print(f"\n新增窗口：{[hex(h) for h in new_hwnds]}")

    SHOTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")
    # 截新增的窗口 + 主窗本身（面板若内嵌则主窗截图里能看到）
    for h in new_hwnds:
        img = print_window(h)
        if img is not None:
            p = SHOTS / f"pc_files3_new_{stamp}_{h:X}.png"
            img.save(p)
            print(f"新增窗口截图 → {p} ({img.size[0]}x{img.size[1]})")

    # 主窗也截一张（不滚回，看是否内嵌变化）
    frame = src._grab_frame()
    if frame is not None:
        p = SHOTS / f"pc_files3_main_{stamp}.png"
        src._last_frame.save(p)
        print(f"主窗截图 → {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
