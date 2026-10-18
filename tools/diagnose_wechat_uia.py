"""诊断：微信 4.x 的 UIA 控件树为什么读不到（只读，不改任何配置）。

## 为什么需要它

2026-10-18 现场：前一天微信 UIA 正常，关机重启后开机，微信启动即读不到
`mmui::MainWindow`（主窗口下只有 `Weixin` 与 `MMUIRenderSubWindowHW` 两个 Pane，
`WM_GETOBJECT` 一律不应答）。怀疑是 Qt 的启动时序问题：Qt 6 在窗口创建时
如果没检测到无障碍客户端（`UiaClientsAreListening`），就不初始化 QAccessible，
之后再连客户端也补不回来。

本脚本只做两件事：
1. 记录「当前微信窗口在 UIA 里长什么样」；
2. 按需要**重新拉起一个微信实例**（不杀进程；已有实例时微信自己会激活老窗口，
   这时脚本会提示你先手动退出）。

用法::

    python tools/diagnose_wechat_uia.py            # 只看当前状态
    python tools/diagnose_wechat_uia.py --relaunch # 带 QT_ACCESSIBILITY=1 启动微信后再看

⚠ 它**不会**结束微信进程。要重启微信请自己从托盘退出。


## 2026-10-18 复查结论（重要，别再误读）

当天上午的空壳态又做了一轮交叉验证，几条判据要改：

1. **`WM_GETOBJECT` 返回 0x0 不等于「没注册无障碍」。** 四条 oid
   （UIA_ROOT / OBJID_CLIENT / OBJID_WINDOW / OBJID_NATIVEOM）全是 0x0，
   但同一句柄走 `oleacc.AccessibleObjectFromWindow(hwnd, OBJID_CLIENT,
   IID_IAccessible)` **返回 S_OK 且拿到非空指针** —— 说明 MSAA/UIA 代理是通的，
   只是这个窗口不通过 `WM_GETOBJECT` 自报。**别再拿它当唯一判据。**
2. **主窗口的子节点从 2 个变成 3 个**（多了一个顶层 `PaneControl
   Qt51514QWindowIcon 'Weixin'`），但两个 Pane 的子节点数都是 **0**：
   `MMUIRenderSubWindowHW`（686x1313）里一个控件都没有。
   即：**空壳的证据是「Pane 之下 0 个子节点」，不是「节点总数少」。**
3. **隐藏的 `Weixin` 登录窗（title='Weixin'）始终有 7 个节点**，含
   TitleBar/MenuBar/最小化/最大化/关闭。所以 Qt 自己的 QAccessible 没坏，
   坏的只是 `mmui::MainWindow` 这条渲染路径的桥接。
4. 微信 4.1.15.13 目录里**没有** `plugins/accessible/`，也没有
   `Qt6*Accessibility.dll`；主进程加载了 `uiautomationcore.dll` + `OLEACC.dll`，
   但全机没有任何进程加载 `windowsaccessibility` 模块。
5. 昨天（10-17）能用的时段，日志报的是「通过 — 会话 15 个」；当天 11:57
   第一启动时报的是「收进托盘」（= 能读到窗口、只是不可见），**不是**空壳。
   今天 11:51 第一次探测就报空壳，此后每次重启都是空壳，一次都没成功过。
   → 「开机顺序 / UIA 客户端先到」这条假设**尚未被证实也未被证伪**，
   因为「程序先起、再起微信」这个实验到今天为止**从没真正做过**。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# wxauto 自带一份 uiautomation，和投递端用的是同一份实现
sys.path.insert(0, str((BASE_DIR.parent / "wxauto-main").resolve()))

WECHAT_EXE_CANDIDATES = (
    Path(r"C:\Program Files\Tencent\Weixin\Weixin.exe"),
    Path(r"C:\Program Files (x86)\Tencent\Weixin\Weixin.exe"),
)

WM_GETOBJECT = 0x003D
# Qt 主窗口在 Win32 层的类名带版本号（Qt51514QWindowIcon），认前缀
QT_CLASS_PREFIX = "Qt5"
QT_CLASS_SUFFIX = "QWindowIcon"


def _ctypes():
    import ctypes
    from ctypes import wintypes
    return ctypes, wintypes


def _weixin_pids() -> set[int]:
    """当前 Weixin.exe 的所有进程 ID。用它把钉钉等其它 Qt 程序排除掉。"""
    ctypes, wintypes = _ctypes()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)

    # 枚举进程 → 取映像名。typing 上没类型，但 Win32 稳定
    TH32CS_SNAPPROCESS = 0x00000002
    MAX_PATH = 260
    import struct

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_char * MAX_PATH)]

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    pids: set[int] = set()
    if snap in (0, -1):
        return pids
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        ok = kernel32.Process32First(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == b"weixin.exe":
                pids.add(int(entry.th32ProcessID))
            ok = kernel32.Process32Next(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return pids


def enum_weixin_windows() -> list[dict]:
    """列出所有属于 Weixin.exe 的顶层窗口（Win32 层，不依赖 UIA）。"""
    ctypes, wintypes = _ctypes()
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    target_pids = _weixin_pids()
    rows: list[dict] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        cls = buf.value or ""
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) not in target_pids:
            return True                       # 不是微信的窗口（钉钉也是 Qt）
        n = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, title, n + 1)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        visible = bool(user32.IsWindowVisible(hwnd))
        # 只报有意义的窗口：可见的、或者标题是微信的
        if not visible and title.value not in ("微信", "Weixin", ""):
            return True
        rows.append({
            "hwnd": int(hwnd),
            "class": cls,
            "title": title.value or "",
            "pid": int(pid.value),
            "visible": visible,
            "minimized": bool(user32.IsIconic(hwnd)),
            "rect": [rect.left, rect.top,
                     rect.right - rect.left, rect.bottom - rect.top],
        })
        return True

    user32.EnumWindows(cb, 0)
    return rows


def probe_uia(hwnd: int) -> dict:
    """按句柄取 UIA 树，统计节点数与是否出现 mmui:: 控件。"""
    from wxauto import uiautomation as uia

    out: dict = {"hwnd": hwnd, "nodes": 0, "has_mmui": False, "children": []}
    try:
        ctl = uia.ControlFromHandle(hwnd)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    try:
        out["class"] = str(ctl.ClassName or "")
        out["name"] = str(ctl.Name or "")
    except Exception:
        pass

    count = 0
    stack = [(ctl, 0)]
    while stack:
        node, depth = stack.pop()
        count += 1
        try:
            cls = str(node.ClassName or "")
        except Exception:
            cls = ""
        if cls.startswith("mmui::") and not out["has_mmui"]:
            out["has_mmui"] = True
            out["first_mmui"] = cls
        if depth <= 2:
            out["children"].append({"depth": depth, "type": node.ControlTypeName,
                                    "class": cls, "name": str(node.Name or "")[:24]})
        if depth >= 8 or count > 4000:
            continue
        try:
            for ch in node.GetChildren():
                stack.append((ch, depth + 1))
        except Exception:
            continue
    out["nodes"] = count
    return out


def probe_wm_getobject(hwnd: int) -> dict:
    """问窗口「你是不是 UIA 提供者」。

    ⚠ 2026-10-18 实测：返回 0 并**不代表**没注册无障碍。同一句柄走
    ``oleacc.AccessibleObjectFromWindow(hwnd, OBJID_CLIENT, IID_IAccessible)``
    能拿到 S_OK + 非空指针。这里保留只是为了留档对比，**不要当判据**。
    """
    ctypes, wintypes = _ctypes()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    out = {}
    for oid, label in ((-25, "UIA_ROOT"), (0, "OBJID_CLIENT")):
        res = ctypes.c_size_t(0)
        ok = user32.SendMessageTimeoutW(
            wintypes.HWND(hwnd), WM_GETOBJECT,
            wintypes.WPARAM(oid & 0xFFFFFFFFFFFFFFFF), wintypes.LPARAM(0),
            1, 2000, ctypes.byref(res))
        out[label] = {"responded": bool(ok), "result": hex(res.value)}
    return out


def probe_msaa(hwnd: int) -> dict:
    """用 oleacc 直接要 IAccessible —— 判断「代理通道」到底通不通。

    这才是可靠的「有没有无障碍桥」判据：S_OK + 非空指针 = 通了。
    """
    ctypes, _wintypes = _ctypes()
    out: dict = {"ok": False}
    try:
        import comtypes  # noqa: F401
        import comtypes.client  # noqa: F401
        from comtypes import GUID

        oleacc = ctypes.oledll.oleacc
        p = ctypes.c_void_p()
        iid = GUID("{618736E0-3C3D-11CF-810C-00AA00389B71}")
        hr = oleacc.AccessibleObjectFromWindow(
            ctypes.c_void_p(hwnd), 0, ctypes.byref(iid), ctypes.byref(p))
        out["hr"] = hex(hr & 0xFFFFFFFF)
        out["ptr"] = bool(p.value)
        out["ok"] = (hr == 0) and bool(p.value)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def probe_container_children(hwnd: int) -> list[dict]:
    """列出主窗口直接子节点，以及**每个子节点之下有几个孩子**。

    空壳的正确判据在这里：容器（如 ``MMUIRenderSubWindowHW``）下面 0 个子节点。
    节点总数少说明不了问题 —— 2026-10-18 主窗口有 3 个节点但依然是空壳。
    """
    from wxauto import uiautomation as uia

    out: list[dict] = []
    try:
        root = uia.ControlFromHandle(hwnd)
    except Exception:
        return out
    if root is None:
        return out
    try:
        kids = root.GetChildren()
    except Exception:
        return out
    for k in kids:
        try:
            n = len(k.GetChildren())
        except Exception:
            n = -1
        try:
            rect = k.BoundingRectangle
            size = [rect.width(), rect.height()]
        except Exception:
            size = [0, 0]
        out.append({"class": str(k.ClassName or ""),
                    "type": k.ControlTypeName,
                    "children": n, "size": size})
    return out


def weixin_running() -> int:
    """返回微信进程数（0 = 没在跑）。"""
    import subprocess as sp
    try:
        r = sp.run(["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/NH"],
                   capture_output=True, text=True, encoding="mbcs", errors="replace")
    except Exception:
        return -1
    return r.stdout.count("Weixin.exe")


def launch_weixin(env_extra: dict | None = None) -> bool:
    exe = next((p for p in WECHAT_EXE_CANDIDATES if p.exists()), None)
    if not exe:
        print("  ✗ 找不到 Weixin.exe")
        return False
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    try:
        subprocess.Popen([str(exe)], env=env, close_fds=True,
                         creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        return True
    except Exception as exc:
        print(f"  ✗ 启动失败：{exc}")
        return False


def report(rows: list[dict]) -> bool:
    """打印一帧报告，返回「是否至少有一个窗口 UIA 可用」。"""
    if not rows:
        print("  ！没有找到微信的 Qt 主窗口（微信没启动？）")
        return False
    ok_any = False
    for r in rows:
        print(f"\n  窗口 hwnd={r['hwnd']} 类名={r['class']} 标题={r['title']!r}")
        print(f"    可见={r['visible']} 最小化={r['minimized']} 尺寸={r['rect'][2]}x{r['rect'][3]}")
        wm = probe_wm_getobject(r["hwnd"])
        print("    WM_GETOBJECT(仅留档): " + "  ".join(
            f"{k}={v['result']}" for k, v in wm.items()))
        msaa = probe_msaa(r["hwnd"])
        if msaa.get("ok"):
            print(f"    MSAA 代理: 通（hr={msaa['hr']}, 拿到 IAccessible）")
        else:
            print(f"    MSAA 代理: 不通（{msaa.get('hr') or msaa.get('error')}）")
        u = probe_uia(r["hwnd"])
        print(f"    UIA 节点数={u['nodes']}  有 mmui:: 控件={u['has_mmui']}"
              + (f"（{u.get('first_mmui')}）" if u.get("first_mmui") else ""))
        if u.get("children"):
            for c in u["children"][:8]:
                print(f"      {'  ' * c['depth']}{c['type']} | {c['class']} | {c['name']!r}")
        # 空壳判据：容器之下的孩子数
        kids = probe_container_children(r["hwnd"])
        if kids:
            print("    直接子节点（→ 之下子节点数）:")
            for k in kids:
                flag = "   ← 空容器" if k["children"] == 0 and k["size"][0] > 100 else ""
                print(f"      {k['type']} | {k['class']} | {k['size'][0]}x{k['size'][1]} "
                      f"→ {k['children']}{flag}")
        if u["has_mmui"]:
            ok_any = True
    return ok_any


def main() -> int:
    ap = argparse.ArgumentParser(description="微信 UIA 可读性诊断（只读）")
    ap.add_argument("--relaunch", action="store_true",
                    help="带 QT_ACCESSIBILITY=1 启动微信后再检查（不会杀进程）")
    ap.add_argument("--wait", type=float, default=12.0,
                    help="重启后等待微信初始化的秒数（默认 12）")
    args = ap.parse_args()

    print("=" * 66)
    print(f"微信 UIA 诊断  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 66)
    print(f"微信进程数：{weixin_running()}")

    if args.relaunch:
        if weixin_running() > 0:
            print("\n⚠ 微信已经在运行。请先从托盘退出微信，再运行本脚本。")
            print("  （本脚本不会替你结束进程 —— 强杀微信会打乱它的登录状态）")
            return 2
        print("\n以 QT_ACCESSIBILITY=1 启动微信…")
        if not launch_weixin({"QT_ACCESSIBILITY": "1"}):
            return 1
        print(f"  等待 {args.wait:.0f} 秒让它完成登录与主窗口渲染…")
        time.sleep(args.wait)

    rows = enum_weixin_windows()
    ok = report(rows)

    print("\n" + "-" * 66)
    if ok:
        print("结论：UIA 控件树可读，投递端应该能正常工作。")
        return 0
    print("结论：读不到 mmui:: 控件 —— 微信这次启动没有注册无障碍提供者。")
    print("      重试办法（按代价从低到高）：")
    print("      1) 先开转发桥程序（它会注册 UIA 客户端），再启动微信")
    print("      2) python tools/diagnose_wechat_uia.py --relaunch")
    print("      3) 完全退出微信 → 重启电脑 → 先开程序再开微信")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
