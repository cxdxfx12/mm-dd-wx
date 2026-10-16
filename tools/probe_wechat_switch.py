"""复现「微信最小化 / 会话切不过去」的诊断工具（自动还原窗口状态）。

    python tools/probe_wechat_switch.py --target "群名"
    python tools/probe_wechat_switch.py --target "群名" --minimize

只做只读/无害操作：
    - 枚举会话列表、回读标题
    - --minimize 时先把微信最小化，测完自动还原

刻意**不发送**任何消息。
"""
from __future__ import annotations

import argparse
import ctypes
import sys
import time
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
SW_MINIMIZE, SW_RESTORE = 6, 9


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("rcNormalPosition", wintypes.RECT),
    ]


def get_placement(hwnd: int):
    wp = WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(WINDOWPLACEMENT)
    user32.GetWindowPlacement(wintypes.HWND(hwnd), ctypes.byref(wp))
    return wp


def set_placement(hwnd: int, wp) -> None:
    user32.SetWindowPlacement(wintypes.HWND(hwnd), ctypes.byref(wp))


def snapshot(tgt, label: str) -> None:
    print(f"\n=========== {label} ===========")
    wnd = tgt._window()
    hwnd = wnd.NativeWindowHandle
    print(f"  IsIconic = {bool(user32.IsIconic(wintypes.HWND(hwnd)))}"
          f" | foreground = {user32.GetForegroundWindow() == hwnd}")
    print(f"  wnd.BoundingRectangle = {tuple(getattr(wnd.BoundingRectangle, k) for k in ('left','top','right','bottom'))}")
    print(f"  _chat_title() = {tgt._chat_title(wnd)!r}")
    t0 = time.time()
    info = tgt.locate_session(TARGET)
    print(f"  locate_session 用时 {time.time() - t0:.2f}s")
    print(f"    found={info['found']} visible={info['visible']} "
          f"cells={info['total_cells']} cell_rect={info['cell_rect']}")
    print(f"    table_rect={info['table_rect']}")
    print(f"    click_point={info.get('click_point')}")
    fl = info.get("first_lines") or []
    print(f"    列表前 6 项：{fl[:6]}")


def main() -> int:
    global TARGET
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    ap.add_argument("--target", required=True)
    ap.add_argument("--minimize", action="store_true",
                    help="把微信最小化后测试（测完自动还原）")
    ap.add_argument("--switch", action="store_true", help="真的执行 open_chat 切会话")
    args = ap.parse_args()
    TARGET = args.target

    cfg = load_config(args.config)
    sys.path.insert(0, str(Path(cfg.wechat.wxauto_path).resolve()))
    from bridge.targets.wechat4 import WeChat4Target

    tgt = WeChat4Target(wxauto_path=cfg.wechat.wxauto_path, dry_run=True)
    wnd = tgt._window()
    hwnd = wnd.NativeWindowHandle
    saved = get_placement(hwnd)

    try:
        snapshot(tgt, "原始状态")
        if args.minimize:
            print("\n>>> 最小化微信窗口 …")
            user32.ShowWindow(wintypes.HWND(hwnd), SW_MINIMIZE)
            time.sleep(1.2)
            snapshot(tgt, "最小化之后")
        if args.switch:
            print("\n>>> 执行 open_chat（只切会话，不发送）…")
            t0 = time.time()
            ok = tgt.open_chat(TARGET)
            print(f"  open_chat = {ok}（用时 {time.time() - t0:.2f}s）")
            print(f"  之后 _chat_title = {tgt._chat_title(tgt._window())!r}")
    finally:
        if args.minimize:
            print("\n>>> 还原窗口状态 …")
            set_placement(hwnd, saved)
            user32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
