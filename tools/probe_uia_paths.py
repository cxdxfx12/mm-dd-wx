# -*- coding: utf-8 -*-
"""对比多种 UIA 访问路径，定位「微信窗口在、但读不到控件」到底断在哪一条路径上。

只读，不启动/不结束微信进程。

用法：
    python tools/probe_uia_paths.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WXAUTO = ROOT.parent / "wxauto-main"
if str(WXAUTO) not in sys.path:
    sys.path.insert(0, str(WXAUTO))

from wxauto import uiautomation as uia  # noqa: E402


def find_main_hwnd() -> int:
    """按标题找标题恰为「微信」的 Qt 顶层窗口。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found = []
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


def walk(root, max_depth=4, want=("mmui",)):
    """从 root 往下 BFS，返回 (depth, class, name) 命中列表。"""
    hits = []
    stack = [(root, 0)]
    while stack:
        node, d = stack.pop()
        if d > max_depth:
            continue
        try:
            kids = node.GetChildren()
        except Exception:
            continue
        for k in kids:
            try:
                cn = str(getattr(k, "ClassName", "") or "")
                nm = str(getattr(k, "Name", "") or "")
            except Exception:
                continue
            if any(w in cn for w in want) or nm == "微信":
                hits.append((d, cn, nm))
            if not cn.startswith("mmui"):
                stack.append((k, d + 1))
    return hits


def main() -> int:
    hwnd = find_main_hwnd()
    print(f"主窗口 hwnd={hwnd}")
    if not hwnd:
        print("没找到标题为「微信」的窗口。")
        return 1

    print("\n=== 路径1：ControlFromHandle(hwnd) ===")
    w = uia.ControlFromHandle(hwnd)
    if w is None:
        print("  返回 None —— 这个句柄不被 UIA 认作可访问窗口")
    else:
        kids = []
        try:
            kids = [x.ClassName for x in w.GetChildren()]
        except Exception as exc:
            print("  GetChildren 异常:", exc)
        print(f"  根={w.ClassName!r} children={kids}")

    print("\n=== 路径2：从桌面根往下遍历（深度4） ===")
    hits = walk(uia.GetRootControl())
    for h in hits[:25]:
        print("  ", h)
    print(f"  命中 {len(hits)} 条，其中 mmui:: {(sum(1 for h in hits if 'mmui' in h[1]))} 条")

    print("\n=== 路径3：uia.Control(Name='微信') ===")
    try:
        c = uia.Control(Name="微信", searchDepth=3)
        ex = c.Exists(0, 0)
        print(f"  exists={ex} class={c.ClassName!r} hwnd={c.NativeWindowHandle}")
        if ex:
            print("  children=", [x.ClassName for x in c.GetChildren()])
    except Exception as exc:
        print("  异常:", type(exc).__name__, exc)

    print("\n=== 路径4：PaneControl(ClassName='mmui::MainWindow') ===")
    try:
        c2 = uia.PaneControl(searchDepth=6, ClassName="mmui::MainWindow")
        print(f"  exists={c2.Exists(0, 0)} hwnd={c2.NativeWindowHandle}")
    except Exception as exc:
        print("  异常:", type(exc).__name__, exc)

    print("\n=== 路径5：Web/Chromium 是否有别的进程暴露 mmui（排除全局） ===")
    try:
        c3 = uia.Control(ClassName="mmui::MainWindow", searchDepth=10)
        print(f"  全桌面搜 mmui::MainWindow exists={c3.Exists(0, 0)}")
    except Exception as exc:
        print("  异常:", type(exc).__name__, exc)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
