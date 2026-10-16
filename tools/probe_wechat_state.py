"""微信窗口状态探针（只读，不点击不发送）。

用于诊断「投递失败：未能切到目标会话（标题未匹配）」这类问题：

    python tools/probe_wechat_state.py
    python tools/probe_wechat_state.py --target "某个群名"

输出内容：
    1. 窗口是否最小化 / 是否在前台 / 窗口矩形
    2. 会话列表表格矩形与已渲染单元格
    3. 标题栏两条回读路径各自的结果（类名法 / 位置法）
"""
from __future__ import annotations

import argparse
import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from bridge.config import load_config  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
SW_MINIMIZE, SW_MAXIMIZE, SW_RESTORE = 6, 3, 9
SW_SHOW = 5


def window_state(hwnd: int) -> dict:
    rect = wintypes.RECT()
    user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    placement = ctypes.create_string_buffer(44)
    placement._obj = None  # 占位，下面用 WINDOWPLACEMENT 结构
    return {
        "hwnd": hwnd,
        "IsIconic(最小化)": bool(user32.IsIconic(wintypes.HWND(hwnd))),
        "IsZoomed(最大化)": bool(user32.IsZoomed(wintypes.HWND(hwnd))),
        "IsWindowVisible": bool(user32.IsWindowVisible(wintypes.HWND(hwnd))),
        "foreground": user32.GetForegroundWindow() == hwnd,
        "rect": (rect.left, rect.top, rect.right, rect.bottom),
    }


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("rcNormalPosition", wintypes.RECT),
    ]


def show_cmd(hwnd: int) -> int:
    wp = WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(WINDOWPLACEMENT)
    if user32.GetWindowPlacement(wintypes.HWND(hwnd), ctypes.byref(wp)):
        return int(wp.showCmd)
    return -1


def main() -> int:
    ap = argparse.ArgumentParser(description="微信窗口状态探针（只读）")
    ap.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    ap.add_argument("--target", default="", help="会话名，用于检查会话列表匹配情况")
    ap.add_argument("--title-dump", action="store_true", help="打印标题条带内的全部 XTextView")
    args = ap.parse_args()

    cfg = load_config(args.config)
    targets = args.target or " / ".join(cfg.wechat.targets or [])

    wxauto_path = Path(cfg.wechat.wxauto_path).resolve()
    sys.path.insert(0, str(wxauto_path))
    from wxauto import uiautomation as uia

    print("=" * 68)
    print("微信窗口状态探针（只读）")
    print("=" * 68)
    print(f"关注会话：{targets!r}")

    wnd = uia.WindowControl(ClassName="mmui::MainWindow", searchDepth=1)
    if not wnd.Exists(1):
        print("[失败] 未找到微信 4.x 主窗口")
        return 1

    hwnd = wnd.NativeWindowHandle
    st = window_state(hwnd)
    print(f"\n---- 1. 窗口状态（hwnd={hwnd}）----")
    for k, v in st.items():
        if k in ("hwnd", "rect"):
            continue
        print(f"  {k:<20} = {v}")
    print(f"  GetWindowPlacement.showCmd = {show_cmd(hwnd)}"
          f"（1=正常 2=最小化 3=最大化）")
    print(f"  rect = {st['rect']}")
    print(f"  wnd.BoundingRectangle = ({wnd.BoundingRectangle.left},"
          f"{wnd.BoundingRectangle.top},{wnd.BoundingRectangle.right},"
          f"{wnd.BoundingRectangle.bottom})")

    print("\n---- 2. 会话列表 ----")
    table = wnd.ListControl(ClassName="mmui::XTableView", searchDepth=30)
    if not table.Exists(1):
        print("  [缺失] 读不到 XTableView")
        cells = []
    else:
        t = table.BoundingRectangle
        print(f"  table rect = ({t.left},{t.top},{t.right},{t.bottom})")
        try:
            cells = [c for c in table.GetChildren()
                     if c.ControlTypeName == "ListItemControl"]
        except Exception as exc:
            print(f"  读取子项失败：{exc}")
            cells = []
    print(f"  渲染中的会话单元格：{len(cells)} 个")
    for c in cells[:40]:
        try:
            r = c.BoundingRectangle
            name = (c.Name or "").split("\n")[0]
            mark = ""
            if args.target and name == args.target:
                mark = "   <== 目标"
            print(f"    - {name!r} rect=({r.left},{r.top},{r.right},{r.bottom}){mark}")
        except Exception:
            continue

    print("\n---- 3. 标题回读 ----")
    bar = wnd.GroupControl(ClassName="mmui::ChatTitleBarChatRoomView", searchDepth=30)
    print(f"  [类名法] ChatTitleBarChatRoomView.Exists = {bar.Exists(0.5)}")
    if bar.Exists(0.1):
        tv = bar.TextControl(ClassName="mmui::XTextView", searchDepth=10)
        print(f"           XTextView.Exists = {tv.Exists(0.5)}  Name = {tv.Name!r}")

    wr = wnd.BoundingRectangle
    strip_top, strip_bottom = wr.top + 20, wr.top + 90
    panel_left = wr.left + 300
    print(f"  [位置法] 标题条带 y∈[{strip_top},{strip_bottom}]  x≥{panel_left}")
    hits: list[str] = []

    def dfs(ctrl, depth: int) -> None:
        if depth > 28:
            return
        try:
            r = ctrl.BoundingRectangle
        except Exception:
            return
        if r.height() <= 0 or r.bottom < strip_top or r.top > strip_bottom:
            return
        if ctrl.ClassName == "mmui::XTextView" and (ctrl.Name or "").strip():
            hits.append(f"x={r.left} y={r.top} {r.right - r.left}x{r.bottom - r.top} "
                        f"name={(ctrl.Name or '')!r}"
                        f"{'  [x<panel_left 被拒]' if r.left < panel_left else ''}")
        try:
            children = ctrl.GetChildren()
        except Exception:
            return
        for ch in children:
            dfs(ch, depth + 1)

    dfs(wnd, 0)
    print(f"          标题条带内 XTextView 共 {len(hits)} 个：")
    for h in hits[:30]:
        print(f"            {h}")

    print("\n---- 4. 结论 ----")
    if st["IsIconic(最小化)"]:
        print("  ❌ 微信窗口处于【最小化】状态。")
        print("     最小化时：会话列表可能不再渲染、鼠标点击与 SendKeys 均不会生效，")
        print("     open_chat 必然失败 → 投递失败。运行转发桥时微信必须保持可见")
        print("     （可以被遮挡，但不能最小化）。")
    else:
        print("  ✅ 微信窗口未最小化。")

    if args.title_dump:
        print("\n---- 附：整棵树的 XTextView ----")
        cnt = 0
        def walk_all(ctrl, depth=0):
            nonlocal cnt
            if depth > 24 or cnt > 200:
                return
            try:
                if ctrl.ClassName == "mmui::XTextView" and (ctrl.Name or "").strip():
                    r = ctrl.BoundingRectangle
                    print(f"    ({r.left},{r.top})-({r.right},{r.bottom}) {ctrl.Name!r}")
                    cnt += 1
                for ch in ctrl.GetChildren():
                    walk_all(ch, depth + 1)
            except Exception:
                return
        walk_all(wnd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
