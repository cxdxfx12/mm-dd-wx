# -*- coding: utf-8 -*-
"""只读诊断：对比「微信进程内 IME 状态」与「UIA 控件树是否可读」。

2026-10-18 排查背景
-------------------
微信 4.x 今天不暴露 mmui:: 控件树。可疑对象之一：搜狗五笔输入法
(SogouWBTSF.ime / SogouWb.ime + ManualNewWord.dll / StatisticsModule.dll)
被加载进微信主进程。IME 会子类化窗口过程、挂接消息链，
理论上有可能破坏 Qt 的 WM_GETOBJECT 响应。

本脚本一次性打印：
  1. 微信各进程的 IME 相关模块
  2. 主窗口类名 / 尺寸 / 是否 mmui::
  3. WM_GETOBJECT 在四种 oid 下的返回值
  4. 该窗口绑定的 HIMC / IME 类名（ImmGetDefaultIMEWnd）
  5. 全机 IME 热键与默认输入法

不做任何写操作，不发按键，不碰 UI。纯只读。
"""
import ctypes
import ctypes.wintypes as w

user32 = ctypes.WinDLL("user32", use_last_error=True)
imm32 = ctypes.WinDLL("imm32", use_last_error=True)

WM_GETOBJECT = 0x003D
OIDS = ((-25, "UIA_ROOT"), (0, "OBJID_CLIENT"), (-16, "OBJID_WINDOW"), (-4, "OBJID_NATIVEOM"))

MMUI_KEYS = ("mmui::", "ChatInputField", "mmui")


def enum_top_windows(pid_filter=None):
    out = []

    @ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    def cb(h, _l):
        pid = w.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid_filter is not None and pid.value != pid_filter:
            return True
        n = user32.GetWindowTextLengthW(h)
        title = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(h, title, n + 1)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(h, cls, 256)
        r = w.RECT()
        user32.GetWindowRect(h, ctypes.byref(r))
        out.append({
            "hwnd": h,
            "pid": pid.value,
            "title": title.value,
            "cls": cls.value,
            "w": r.right - r.left,
            "h": r.bottom - r.top,
            "visible": bool(user32.IsWindowVisible(h)),
        })
        return True

    user32.EnumWindows(cb, 0)
    return out


def ime_of_window(hwnd):
    """这个窗口绑定的输入上下文信息。"""
    info = {}
    himc = imm32.ImmGetContext(w.HWND(hwnd))
    info["himc"] = himc
    if himc:
        try:
            buf = ctypes.create_unicode_buffer(256)
            n = imm32.ImmGetDescriptionW(w.HHOOK(himc), buf, 256)
            info["description"] = buf.value if n else ""
            buf2 = ctypes.create_unicode_buffer(256)
            n2 = imm32.ImmGetIMEFileNameW(w.HHOOK(himc), buf2, 256)
            info["imefile"] = buf2.value if n2 else ""
        finally:
            imm32.ImmReleaseContext(w.HWND(hwnd), w.HHOOK(himc))
    else:
        info["error"] = ctypes.get_last_error()
    # 默认 IME 窗口（输入法状态条所属的那个窗口，属于 IME 进程）
    info["default_ime_wnd"] = imm32.ImmGetDefaultIMEWnd(w.HWND(hwnd))
    return info


def describe_hwnd(hwnd, label):
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    pid = w.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return "%s hwnd=%d pid=%d class=%s" % (label, hwnd, pid.value, cls.value)


def main():
    wins = enum_top_windows()
    wx = [x for x in wins if x["cls"] in ("mmui::MainWindow", "Qt51514QWindowIcon")]
    if not wx:
        print("没有找到微信主窗口（mmui::MainWindow 或 Qt51514QWindowIcon）")
        return 1
    for win in wx:
        print("=" * 60)
        print("微信主窗口：%s" % describe_hwnd(win["hwnd"], ""))
        print("  标题=%r 类名=%r 尺寸=%dx%d 可见=%s pid=%d"
              % (win["title"], win["cls"], win["w"], win["h"], win["visible"], win["pid"]))
        print("  类名是否 mmui:: = %s" % str(win["cls"].startswith("mmui::")))

        print("  --- WM_GETOBJECT ---")
        for oid, label in OIDS:
            lr = user32.SendMessageW(w.HWND(win["hwnd"]), WM_GETOBJECT,
                                     w.WPARAM(oid & 0xFFFFFFFF), w.LPARAM(0))
            print("    %-14s oid=%-3d -> 0x%X" % (label, oid, (lr or 0) & 0xFFFFFFFFFFFFFFFF))

        info = ime_of_window(win["hwnd"])
        print("  --- IME ---")
        print("    himc           = %s" % info["himc"])
        print("    description    = %r" % info.get("description", ""))
        print("    imefile        = %r" % info.get("imefile", ""))
        print("    default_ime_wnd= %s" % describe_hwnd(info["default_ime_wnd"], "")
              if info["default_ime_wnd"] else "    default_ime_wnd= 0")

        # 列出主窗口的直接子窗口（看 IME 是否塞了子窗口）
        kids = []

        @ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        def cb_child(h, _l):
            kids.append(h)
            return True

        user32.EnumChildWindows(w.HWND(win["hwnd"]), cb_child, 0)
        print("  --- 子窗口 %d 个 ---" % len(kids))
        for h in kids:
            print("    %s" % describe_hwnd(h, ""))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
