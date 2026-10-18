# -*- coding: utf-8 -*-
"""带 Qt 无障碍调试日志重启微信，抓取它能打印出的初始化过程。

## 为什么要这么干

2026-10-18 从 `Weixin.dll` 挖到：微信只在 `gAccessibilityMode` 为真时才建立
`mmui::` 无障碍树。带 ``QT_LOGGING_RULES="qt.accessibility.*=true"`` 启动，
可以让 Qt 自己把「无障碍初始化/拒绝初始化」的过程打到 stderr，直接看到卡在哪。

## 做法

1. 检查微信是否在跑 —— **在跑就拒绝执行**，请用户先从托盘退出（本脚本不杀进程）。
2. 用独立进程 + 环境变量启动 `Weixin.exe`，把 stdout/stderr 收到文件。
3. 等待若干秒后自动做一次 UIA 探测，报告是否出现了 `mmui::`。

用法::

    python tools/relaunch_wechat_qtlog.py            # 默认等 25 秒
    python tools/relaunch_wechat_qtlog.py --wait 40
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
LOG_DIR = BASE / "data" / "logs"
OUT = LOG_DIR / "wechat-qt-accessibility.log"

WECHAT_EXE = Path(r"C:\Program Files\Tencent\Weixin\Weixin.exe")


def weixin_pids() -> list[int]:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    TH32CS_SNAPPROCESS = 0x2
    MAX_PATH = 260

    class PE32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_char * MAX_PATH)]

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (0, -1):
        return []
    pids: list[int] = []
    try:
        e = PE32()
        e.dwSize = ctypes.sizeof(PE32)
        ok = k32.Process32First(snap, ctypes.byref(e))
        while ok:
            if e.szExeFile.lower() == b"weixin.exe":
                pids.append(int(e.th32ProcessID))
            ok = k32.Process32Next(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return pids


def probe() -> tuple[bool, str]:
    """独立进程读一次主窗口，返回 (有没有 mmui, 描述)。"""
    code = (
        "import sys; sys.path.insert(0, r'" + str(BASE.parent / "wxauto-main") + "')\n"
        "import comtypes, comtypes.client\n"
        "from comtypes.gen.UIAutomationClient import CUIAutomation, IUIAutomation\n"
        "import ctypes\n"
        "from ctypes import wintypes\n"
        "u = ctypes.WinDLL('user32', use_last_error=True)\n"
        "found = []\n"
        "EP = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)\n"
        "def cb(h, _):\n"
        "    n = u.GetWindowTextLengthW(wintypes.HWND(h))\n"
        "    if n:\n"
        "        b = ctypes.create_unicode_buffer(n + 1)\n"
        "        u.GetWindowTextW(wintypes.HWND(h), b, n + 1)\n"
        "        if b.value == '微信':\n"
        "            found.append(int(h))\n"
        "    return True\n"
        "u.EnumWindows(EP(cb), 0)\n"
        "if not found:\n"
        "    print('NOWINDOW'); raise SystemExit\n"
        "ui = comtypes.client.CreateObject(CUIAutomation._reg_clsid_, interface=IUIAutomation)\n"
        "el = ui.ElementFromHandle(found[0])\n"
        "print('HWND=%d ROOT=%s' % (found[0], el.CurrentClassName))\n"
        "w = ui.RawViewWalker\n"
        "c = w.GetFirstChildElement(el)\n"
        "n = 0\n"
        "while c is not None and n < 8:\n"
        "    try:\n"
        "        print('CHILD=%s' % c.CurrentClassName)\n"
        "    except Exception:\n"
        "        pass\n"
        "    c = w.GetNextSiblingElement(c)\n"
        "    n += 1\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=90)
    out = (r.stdout or "").strip()
    has = "mmui::" in out
    return has, out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=float, default=25.0)
    args = ap.parse_args()

    print("=" * 66)
    print(f"带 Qt 无障碍日志重启微信  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 66)

    pids = weixin_pids()
    if pids:
        print(f"\n⚠ 微信正在运行（{len(pids)} 个进程）。")
        print("  请先从托盘右键 → 退出微信，确认 Weixin.exe 全部消失后再运行本脚本。")
        print("  （本脚本不会杀死你的微信进程）")
        return 2

    if not WECHAT_EXE.exists():
        print(f"✗ 找不到 {WECHAT_EXE}")
        return 1

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["QT_LOGGING_RULES"] = "qt.accessibility.*=true;qt.qml.*=false"
    print("\n启动参数：QT_LOGGING_RULES=qt.accessibility.*=true")
    print(f"日志输出：{OUT}")

    with open(OUT, "w", encoding="utf-8", errors="replace") as fh:
        fh.write(f"# 启动于 {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        fh.write("# QT_LOGGING_RULES=qt.accessibility.*=true\n\n")
        fh.flush()
        proc = subprocess.Popen(
            [str(WECHAT_EXE)], env=env,
            stdout=fh, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        print(f"沙箱 PID={proc.pid}，等待 {args.wait:.0f} 秒让它登录并渲染…")
        time.sleep(args.wait)

    print("\n--- Qt 无障碍日志（前 60 行）---")
    try:
        text = OUT.read_text(encoding="utf-8", errors="replace")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if len(lines) <= 1:
            print("  （没有任何输出 —— Qt 可能把日志重定向到别处，或该构建关闭了日志）")
        for ln in lines[:60]:
            print("  " + ln)
        print(f"  … 共 {len(lines)} 行，完整内容见 {OUT}")
    except Exception as exc:
        print(f"  读取日志失败：{exc}")

    print("\n--- UIA 实测 ---")
    has, out = probe()
    for ln in out.splitlines():
        print("  " + ln)
    print()
    if has:
        print("★ 出现了 mmui:: 控件 —— 本次启动的无障碍树正常！")
        return 0
    print("✗ 仍然没有 mmui:: 控件。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
