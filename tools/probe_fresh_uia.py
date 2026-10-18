# -*- coding: utf-8 -*-
"""在**全新独立的 COM 环境**里读微信控件树，排除「UIA 客户端实例状态坏掉」。

之前的诊断都在同一个 Python 进程里反复调用，若该进程的 UIA 代理缓存异常，
所有结果会一致地失真。本脚本用子进程 + 强制 MTA + 全新 CUIAutomation 实例，
是最干净的复现方式。

用法::

    python tools/probe_fresh_uia.py [hwnd]
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

CHILD = r'''
import sys
sys.path.insert(0, r"{wxauto}")

import comtypes
# comtypes 在 import 时已把主线程初始化为 STA，此处不能再改线程模式（会报
# WinError -2147417850）。独立进程 + 全新 CUIAutomation 实例已经足够干净。
from comtypes.gen.UIAutomationClient import CUIAutomation, IUIAutomation
import comtypes.client

uia = comtypes.client.CreateObject(CUIAutomation._reg_clsid_, interface=IUIAutomation)
hwnd = {hwnd}
el = uia.ElementFromHandle(hwnd)
print("root:", el.CurrentClassName, "| name:", el.CurrentName)

for label, walker in (("Raw", uia.RawViewWalker),
                      ("Control", uia.ControlViewWalker),
                      ("Content", uia.ContentViewWalker)):
    print("--", label, "--")
    c = walker.GetFirstChildElement(el)
    n = 0
    while c is not None and n < 15:
        try:
            print("   ", c.CurrentClassName, "|", repr(c.CurrentName))
        except Exception as exc:
            print("    err:", exc)
        c = walker.GetNextSiblingElement(c)
        n += 1
    print("    (共", n, "个)")
'''


def find_main_hwnd() -> int:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found: list[int] = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        n = user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(wintypes.HWND(hwnd), buf, n + 1)
            if buf.value == "微信":
                found.append(int(hwnd))
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return found[0] if found else 0


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    wxauto = root.parent / "wxauto-main"
    hwnd = int(sys.argv[1]) if len(sys.argv) > 1 else find_main_hwnd()
    if not hwnd:
        print("没找到微信主窗口")
        return 1
    print(f"在独立进程里读 hwnd={hwnd}（强制 MTA + 全新 CUIAutomation）\n")
    code = CHILD.format(wxauto=str(wxauto), hwnd=hwnd)
    r = subprocess.run([sys.executable, "-c", code],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=90)
    print(r.stdout or "(无输出)")
    if r.stderr.strip():
        print("stderr:", r.stderr.strip()[:800])
    return r.returncode


if __name__ == "__main__":
    raise SystemExit(main())
