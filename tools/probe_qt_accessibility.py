# -*- coding: utf-8 -*-
"""只读探测：微信进程的 Qt 无障碍相关环境变量与 UIA 桥接状态。

不启动、不结束任何进程，只用 PowerShell/WMI 读环境变量 + UIA 读控件树。
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def weixin_pids() -> list[int]:
    """Toolhelp 快照，筛 weixin.exe。"""
    TH32CS_SNAPPROCESS = 0x00000002
    MAX_PATH = 260

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * MAX_PATH),
        ]

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    pids = []
    try:
        if kernel32.Process32FirstW(snap, ctypes.byref(entry)):
            while True:
                if entry.szExeFile.lower() == "weixin.exe":
                    pids.append(int(entry.th32ProcessID))
                if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snap)
    return pids


def env_of(pid: int) -> dict[str, str]:
    """用 PowerShell 读进程环境变量（只读）。"""
    ps = (
        f"$p = Get-Process -Id {pid} -ErrorAction SilentlyContinue; "
        "if(-not $p){'NO_PROC'; exit}; "
        "$keys = $p.StartInfo.EnvironmentVariables.Keys; "
        "'noenv'"
    )
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return {"raw": (out.stdout or "").strip(), "err": (out.stderr or "").strip()}


def main() -> int:
    pids = weixin_pids()
    print(f"微信进程：{pids}")
    if not pids:
        print("微信没在运行。")
        return 1

    print("\n=== 本进程（诊断器）环境 ===")
    import os
    for k in ("QT_ACCESSIBILITY", "QT_LINUX_ACCESSIBILITY_ALWAYS_ON",
              "QT_LOGGING_RULES", "QT_ENABLE_HIGHDPI_SCALING"):
        print(f"  {k} = {os.environ.get(k, '(未设置)')}")

    print("\n=== 主进程模块加载（是否载入 Qt 无障碍插件） ===")
    for pid in pids[:1]:
        ps = (
            f"Get-Process -Id {pid} -Module -ErrorAction SilentlyContinue | "
            "Where-Object { $_.ModuleName -match 'accessible|uia|oleacc|UIAutomation' } | "
            "Select-Object -ExpandProperty ModuleName"
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        mods = [m for m in (out.stdout or "").splitlines() if m.strip()]
        print(f"  PID {pid}: {mods or '（无匹配模块）'}")

    print("\n=== 全机 accessibility 相关 DLL 是否被任何进程加载 ===")
    ps2 = (
        "Get-Process -Module -ErrorAction SilentlyContinue | "
        "Where-Object { $_.ModuleName -match 'windowsaccessibility|qwindowsuia' } | "
        "Select-Object -ExpandProperty ModuleName -Unique"
    )
    out2 = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps2],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    print("  ", [m for m in (out2.stdout or "").splitlines() if m.strip()] or "（无）")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
