# -*- coding: utf-8 -*-
"""抓一张指定窗口的截图（只读）。用于确认微信到底停在哪个界面。

用法::

    python tools/grab_window.py              # 自动找标题为「微信」的窗口
    python tools/grab_window.py 42928784     # 指定 hwnd
    python tools/grab_window.py --out a.png
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def find_main_hwnd(title: str = "微信") -> int:
    found: list[int] = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        n = user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(wintypes.HWND(hwnd), buf, n + 1)
            if buf.value == title:
                found.append(int(hwnd))
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return found[0] if found else 0


def grab(hwnd: int, out: Path) -> bool:
    user32.SetProcessDPIAware()
    r = wintypes.RECT()
    user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        print(f"窗口尺寸异常：{w}x{h}")
        return False

    hdc = user32.GetWindowDC(wintypes.HWND(hwnd))
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mdc, bmp)
    try:
        ok = user32.PrintWindow(wintypes.HWND(hwnd), mdc, 2)
        bi = BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(bi)
        bi.biWidth = w
        bi.biHeight = -h            # 负 = 自上而下
        bi.biPlanes = 1
        bi.biBitCount = 32
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
        raw = bytearray(buf.raw)
        # BGRA → PNG（用 Pillow，没有就存 BMP 原始）
        try:
            from PIL import Image
            img = Image.frombuffer("RGBA", (w, h), bytes(raw), "raw", "BGRA", 0, 1)
            out.parent.mkdir(parents=True, exist_ok=True)
            img.convert("RGB").save(out)
            print(f"已保存 {out}（{w}x{h}, PrintWindow={ok}）")
            return True
        except ImportError:
            print("没装 Pillow，无法存 PNG")
            return False
    finally:
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(wintypes.HWND(hwnd), hdc)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out = Path("tools/shots/wechat.png")
    for i, a in enumerate(sys.argv):
        if a == "--out" and i + 1 < len(sys.argv):
            out = Path(sys.argv[i + 1])
    if args and args[0].isdigit():
        hwnd = int(args[0])
    else:
        hwnd = find_main_hwnd()
    if not hwnd:
        print("找不到微信主窗口")
        return 1
    print(f"hwnd={hwnd}")
    return 0 if grab(hwnd, out) else 1


if __name__ == "__main__":
    raise SystemExit(main())
