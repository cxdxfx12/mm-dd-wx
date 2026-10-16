"""探测钉钉消息区滚动条（只读）。

目的：找一个能**可靠判断「视口是否贴底」**的信号。有了它，即使两帧内容
零重叠（消息太高、新消息把上一条整条顶出视口），也能安全判断「这批内容
就是最新消息」而不是「用户滚上去的历史」，从而不再整帧丢弃。

本探针打印：
  * ChatBubbleWidget 的矩形；
  * 其下所有滚动条控件的类名/矩形/支持的 Pattern；
  * RangeValuePattern / ScrollPattern 的值（贴底 vs 向上滚后分别是什么）。

    python tools/probe_dingtalk_scrollbar.py            # 只读打印
    python tools/probe_dingtalk_scrollbar.py --scroll   # 额外做一次「向上滚→回底」
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MSG_AREA_CLASS = "im_chat::ChatBubbleWidget"


def main() -> int:
    ap = argparse.ArgumentParser(description="钉钉消息区滚动条探针（只读）")
    ap.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    ap.add_argument("--scroll", action="store_true", help="额外做一次滚动实验")
    args = ap.parse_args()

    from bridge.config import load_config
    from bridge.sources import dingtalk_ocr as D

    cfg = load_config(args.config)
    src = D.DingTalkOCRSource(cfg)
    uia = src._load_uia()
    wnd = src._find_window()
    if wnd is None:
        print("未找到钉钉主窗口")
        return 1
    print("钉钉窗口:", wnd.ClassName, wnd.BoundingRectangle)

    def dump(tag: str):
        print(f"\n===== {tag} =====")
        area = wnd.Control(ClassName=MSG_AREA_CLASS, searchDepth=14)
        if not area.Exists(1):
            print("  消息区不存在")
            return
        print("  消息区:", area.ClassName, area.BoundingRectangle,
              "size=", area.BoundingRectangle.width(), "x", area.BoundingRectangle.height())

        # 滚动条可能在消息区内部（实测是 ChatBubbleWidget 的子控件）
        for depth_path, ctrl in _walk(area, max_depth=3):
            cls = ctrl.ClassName or ""
            if "ScrollBar" not in cls and "Scroll" not in cls:
                continue
            r = ctrl.BoundingRectangle
            print(f"  {depth_path} [{ctrl.ControlTypeName}] class={cls!r} "
                  f"rect=({r.left},{r.top},{r.right},{r.bottom})")
            _patterns(ctrl, "    ")
            _children(ctrl, "    ", depth=2)

        # 整个窗口范围内再找一遍（有的版本滚动条挂在别处）
        for depth_path, ctrl in _walk(wnd, max_depth=14):
            cls = ctrl.ClassName or ""
            if "ScrollBar" not in cls:
                continue
            r = ctrl.BoundingRectangle
            if r.bottom < area.BoundingRectangle.top or r.top > area.BoundingRectangle.bottom:
                continue
            print(f"  [窗口级] {depth_path} [{ctrl.ControlTypeName}] class={cls!r} "
                  f"rect=({r.left},{r.top},{r.right},{r.bottom})")
            _patterns(ctrl, "    ")
    dump("初始状态")

    if args.scroll:
        area = wnd.Control(ClassName=MSG_AREA_CLASS, searchDepth=14)
        print("\n>>> 向上滚 5 格")
        try:
            area.WheelUp(wheelTimes=5)
        except Exception as exc:
            print("  WheelUp 失败:", exc)
        time.sleep(1.2)
        dump("向上滚之后")
        print("\n>>> 滚回底部（WheelDown 20 格）")
        try:
            area.WheelDown(wheelTimes=20)
        except Exception as exc:
            print("  WheelDown 失败:", exc)
        time.sleep(1.2)
        dump("滚回底部之后")

    return 0


def _walk(ctrl, max_depth: int):
    """广度遍历子控件，产出 (路径, 控件)。"""
    queue = [("", ctrl, 0)]
    seen = 0
    while queue and seen < 4000:
        path, cur, d = queue.pop(0)
        seen += 1
        if d >= max_depth:
            continue
        try:
            kids = cur.GetChildren()
        except Exception:
            continue
        for i, k in enumerate(kids):
            p = f"{path}/{i}"
            yield p, k
            queue.append((p, k, d + 1))


def _patterns(ctrl, indent: str):
    for name in ("RangeValuePattern", "ScrollPattern", "ValuePattern"):
        try:
            pat = getattr(ctrl, "Get" + name)()
        except Exception:
            continue
        if pat is None:
            continue
        info = {}
        for attr in ("Value", "IsReadOnly", "Minimum", "Maximum",
                     "LargeChange", "SmallChange",
                     "VerticalScrollPercent", "HorizontalScrollPercent",
                     "VerticalViewSize", "VerticallyScrollable"):
            try:
                info[attr] = getattr(pat, attr)
            except Exception:
                pass
        print(f"{indent}{name}: {info}")


def _children(ctrl, indent: str, depth: int = 2):
    """打印滚动条自身的子树（滚动条的滑块常是子控件，可用矩形算比例）。"""
    queue = [("", ctrl, 0)]
    while queue:
        path, cur, d = queue.pop(0)
        if d >= depth:
            continue
        try:
            kids = cur.GetChildren()
        except Exception:
            continue
        for i, k in enumerate(kids):
            r = k.BoundingRectangle
            print(f"{indent}{path}/{i} [{k.ControlTypeName}] "
                  f"class={k.ClassName!r} rect=({r.left},{r.top},{r.right},{r.bottom})")
            _patterns(k, indent + "  ")
            queue.append((f"{path}/{i}", k, d + 1))


if __name__ == "__main__":
    raise SystemExit(main())
