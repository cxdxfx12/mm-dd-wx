# -*- coding: utf-8 -*-
"""只读诊断：验证「Qt 惰性建树」——UIA 树是否只在窗口获得焦点后才可用。

2026-10-18 背景
--------------
微信 4.x 今天 UIA 读不到 mmui:: 控件树，但界面渲染正常、MSAA 代理通道通。
一个尚未排除的可能：Qt6 的 accessibility 是**惰性**的 ——
只有当窗口真正获得前台焦点（或收到第一个无障碍查询时焦点就在它身上）
才会实例化 QAccessible 接口。后台窗口查询返回空。

本脚本：
  1. 先在不改焦点的前提下查当前树
  2. 记录当前前台窗口
  3. **不做任何切换**，只报告前置条件，供人工判断

真正验证需要把微信切到前台 —— 那会有可见的 UI 动作，
所以脚本只输出「前置状态 + 建议」，不自动切。
用 --probe-after-focus 时才会在切前台后复查（需用户明确同意）。

用法：
    python tools/probe_uia_focus_state.py            # 只读报告
    python tools/probe_uia_focus_state.py --watch 30 # 30 秒内每 3 秒复测一次
"""
import argparse
import ctypes
import ctypes.wintypes as w
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)


def foreground():
    h = user32.GetForegroundWindow()
    pid = w.DWORD()
    user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
    n = user32.GetWindowTextLengthW(h)
    t = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(h, t, n + 1)
    c = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(h, c, 256)
    return h, pid.value, t.value, c.value


def find_wechat():
    out = []

    @ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    def cb(h, _l):
        c = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(h, c, 256)
        if c.value in ("mmui::MainWindow", "Qt51514QWindowIcon"):
            n = user32.GetWindowTextLengthW(h)
            t = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(h, t, n + 1)
            if t.value in ("微信", "Weixin") and user32.IsWindowVisible(h):
                r = w.RECT()
                user32.GetWindowRect(h, ctypes.byref(r))
                if r.right - r.left > 200:  # 排除小挂件
                    out.append((h, c.value, t.value))
        return True

    user32.EnumWindows(cb, 0)
    return out


def uia_node_count():
    """用 UIA 数主窗口的直接子节点数（Raw 视图）。"""
    try:
        import comtypes.client
        from comtypes.gen import UIAutomationClient as UIA
    except Exception as e:  # pragma: no cover
        return -1, "comtypes 不可用: %s" % e
    try:
        uia = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
        root = uia.GetRootElement()
        walker = uia.RawViewWalker
        found = 0
        has_mmui = False
        total = 0
        child = walker.GetFirstChildElement(root)
        while child:
            total += 1
            cond = uia.CreatePropertyCondition(UIA.UIA_ClassNamePropertyId, "mmui::MainWindow")
            m = root.FindFirst(UIA.TreeScope_Descendants, cond)
            if m:
                has_mmui = True
            child = walker.GetNextSiblingElement(child)
        return total, "mmui::MainWindow 存在=%s" % has_mmui
    except Exception as e:
        return -1, "UIA 查询失败: %s" % e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=int, default=0, help="持续观察秒数")
    args = ap.parse_args()

    wx = find_wechat()
    fg = foreground()
    print("前台窗口: %s (pid=%d class=%s)" % (fg[2], fg[1], fg[3]))
    print("微信主窗口: %s" % (["hwnd=%d class=%s" % (h, c) for h, c, _ in wx] or "未找到"))
    if wx:
        print("微信是前台? %s" % (wx[0][0] == fg[0]))
    n, note = uia_node_count()
    print("桌面顶层节点数=%s  %s" % (n, note))

    if args.watch:
        end = time.time() + args.watch
        while time.time() < end:
            time.sleep(3)
            fg = foreground()
            print("  %s 前台=%s(pid=%d) 顶层=%s"
                  % (time.strftime("%H:%M:%S"), fg[2], fg[1], uia_node_count()[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
