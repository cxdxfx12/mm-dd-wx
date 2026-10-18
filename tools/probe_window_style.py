# -*- coding: utf-8 -*-
"""只读：查微信主窗口的窗口样式 / 最大化状态 / 置放。

用于判断 Qt 窗口是否处于「声明为最大化、实际尺寸未最大化」的畸形态 ——
这种状态下 Qt 的布局引擎可能没有完成一次完整布局，
导致渲染子窗口（MMUIRenderSubWindowHW）没有挂上任何无障碍子节点。
"""
import ctypes
import ctypes.wintypes as w

user32 = ctypes.WinDLL("user32", use_last_error=True)

GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_MAXIMIZE = 0x01000000
WS_SIZEBOX = 0x00040000
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_MAXIMIZEBOX = 0x00010000

WPF_RESTORETOMAXIMIZED = 0x0002


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", w.UINT),
        ("flags", w.UINT),
        ("showCmd", w.UINT),
        ("ptMinPosition", w.POINT),
        ("ptMaxPosition", w.POINT),
        ("rcNormalPosition", w.RECT),
    ]


def main():
    found = []

    @ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    def cb(h, _l):
        c = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(h, c, 256)
        n = user32.GetWindowTextLengthW(h)
        t = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(h, t, n + 1)
        if user32.IsWindowVisible(h) and t.value in ("微信", "Weixin"):
            found.append((h, c.value, t.value))
        return True

    user32.EnumWindows(cb, 0)
    if not found:
        print("未找到可见微信窗口")
        return 1

    for hwnd, cls, title in found:
        r = w.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        width, height = r.right - r.left, r.bottom - r.top

        style = user32.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF
        exstyle = user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF

        wp = WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(wp)
        user32.GetWindowPlacement(hwnd, ctypes.byref(wp))

        # 显示器工作区
        MONITOR_DEFAULTTONEAREST = 2
        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT),
                        ("rcWork", w.RECT), ("dwFlags", w.DWORD)]
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(mi)
        hmon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        user32.GetMonitorInfoW(hmon, ctypes.byref(mi))

        print("hwnd=%d class=%s title=%r" % (hwnd, cls, title))
        print("  窗口矩形   = (%d,%d) %dx%d" % (r.left, r.top, width, height))
        print("  监视器工作区= (%d,%d) %dx%d"
              % (mi.rcWork.left, mi.rcWork.top,
                 mi.rcWork.right - mi.rcWork.left, mi.rcWork.bottom - mi.rcWork.top))
        print("  WS_MAXIMIZE  = %s" % bool(style & WS_MAXIMIZE))
        print("  WS_SIZEBOX   = %s" % bool(style & WS_SIZEBOX))
        print("  WS_CAPTION   = %s" % bool(style & WS_CAPTION))
        print("  WS_MAXIMIZEBOX=%s" % bool(style & WS_MAXIMIZEBOX))
        print("  exstyle      = 0x%08X" % exstyle)
        show = {0: "SW_HIDE", 1: "SW_SHOWNORMAL", 2: "SW_SHOWMINIMIZED",
                3: "SW_SHOWMAXIMIZED", 4: "SW_SHOWNOACTIVATE",
                6: "SW_MINIMIZE", 7: "SW_SHOWMINNOACTIVE",
                9: "SW_RESTORE", 11: "SW_SHOWDEFAULT"}
        print("  showCmd      = %s" % show.get(wp.showCmd, wp.showCmd))
        print("  flags        = 0x%X (WPF_RESTORETOMAXIMIZED=%s)"
              % (wp.flags, bool(wp.flags & WPF_RESTORETOMAXIMIZED)))
        print("  rcNormal     = (%d,%d) %dx%d"
              % (wp.rcNormalPosition.left, wp.rcNormalPosition.top,
                 wp.rcNormalPosition.right - wp.rcNormalPosition.left,
                 wp.rcNormalPosition.bottom - wp.rcNormalPosition.top))
        ww = mi.rcWork.right - mi.rcWork.left
        wh = mi.rcWork.bottom - mi.rcWork.top
        wrong = bool(style & WS_MAXIMIZE) and (width < ww - 50 or height < wh - 50)
        print("  ⚠ 最大化标记但尺寸不符 = %s" % wrong)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
