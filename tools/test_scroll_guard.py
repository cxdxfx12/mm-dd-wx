"""实测防线 A：滚动聊天区不会让历史消息被误转发出去。

    python tools/test_scroll_guard.py             # 向上滚 5 格，测完滚回原位
    python tools/test_scroll_guard.py --times 8   # 指定滚动格数
    python tools/test_scroll_guard.py --dry       # 只抓两帧比对，不做任何滚动

做法：抓基线帧 → 向上滚动聊天区（让更早的消息进入视口）→ 再抓一帧 →
      用正式运行同一套判定逻辑 `pick_new_units` 看哪些会被转发。

对照：同时用 `anchor_guard=False` 再算一遍，直观展示「没有防护时会误发多少条」。
脚本结束时会滚回原位（try/finally 保证）。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from bridge.config import load_config                       # noqa: E402
from bridge.sources.dingtalk_ocr import (                   # noqa: E402
    DingTalkOCRSource, merge_lines, group_units, unit_fp, pick_new_units,
)


def snapshot(src):
    frame = src._grab_frame()
    if frame is None:
        return None, None, None
    units = group_units(merge_lines(frame["blocks"]), frame["area_w"])
    fps = [unit_fp(u) for u in units]
    return frame, units, fps


def show(units, limit=6):
    for i, u in enumerate(units[:limit], 1):
        who = "(我)" if u["is_self"] else (u["sender"] or "(未识别)")
        print(f"    {i}. [{who}] {u['content'][:56]}")
    if len(units) > limit:
        print(f"    … 另有 {len(units) - limit} 条")


def main() -> int:
    ap = argparse.ArgumentParser(description="实测滚动不会导致历史消息被误转发")
    ap.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    ap.add_argument("--times", type=int, default=5, help="滚动格数")
    ap.add_argument("--dry", action="store_true", help="不滚动，只抓两帧比对")
    args = ap.parse_args()

    cfg = load_config(args.config)
    src = DingTalkOCRSource(cfg)
    ok, detail = src.check()
    print(f"[{'通过' if ok else '失败'}] 自检：{detail}")
    if not ok:
        return 1

    frame_a, units_a, fps_a = snapshot(src)
    if units_a is None:
        print("[失败] 抓帧失败")
        return 1
    if src.title_check and not src._title_matches(frame_a["title"])[0]:
        print("[中止] 当前钉钉窗口不是目标会话，请先切到目标群。")
        return 1
    print(f"\n[基线帧] 可见 {len(units_a)} 条：")
    show(units_a)

    ctrl = None
    try:
        if not args.dry:
            wnd = src._find_window()
            msg_rect, hit = src._find_msg_rect(wnd)
            if msg_rect is None:
                print("[失败] 定位不到消息区控件")
                return 1
            ctrl = wnd.Control(ClassName=hit, searchDepth=14)
            if not ctrl.Exists(1):
                print("[失败] 取不到消息区控件句柄")
                return 1
            print(f"\n[滚动] 向上 {args.times} 格，让更早的消息进入视口…")
            ctrl.WheelUp(wheelTimes=args.times)
            time.sleep(2.0)

        frame_b, units_b, fps_b = snapshot(src)
        if units_b is None:
            print("[失败] 滚动后抓帧失败")
            return 1
        print(f"\n[滚动后] 可见 {len(units_b)} 条：")
        show(units_b)

        if set(fps_b) == set(fps_a):
            print("\n[结果] 滚动没有改变可见内容 —— 该会话历史消息不足一屏，"
                  "本次无法实测滚动场景（判定逻辑已由单元测试覆盖）。")
            return 0

        picked, _ = pick_new_units(units_b, fps_b, fps_a, src.anchor_guard)
        print("\n" + "=" * 62)
        if picked is None:
            print("[防线 A 生效] 判定为「视口大幅位移」→ 本帧整体不转发。")
            print("             滚动带进来的历史消息一条都不会外发。")
        elif not picked:
            print("[防线 A 生效] 没有内容被当作新消息，0 条外发。")
        else:
            print(f"[防线 A 生效] 仅 {len(picked)} 条被认定为新消息：")
            for i in picked:
                print(f"    - {units_b[i]['content'][:60]}")

        off, _ = pick_new_units(units_b, fps_b, fps_a, False)
        print("-" * 62)
        if off:
            print(f"[对照] 若关闭 anchor_guard，会有 {len(off)} 条被误当成新消息发出：")
            for i in off[:5]:
                print(f"    ✗ {units_b[i]['content'][:60]}")
        else:
            print("[对照] 关闭 anchor_guard 结果相同（本次滚动未引入新内容）。")
        print("=" * 62)
        return 0
    finally:
        if ctrl is not None:
            try:
                ctrl.WheelDown(wheelTimes=args.times)
                time.sleep(0.8)
                print("[恢复] 已把聊天区滚回原位。")
            except Exception as exc:
                print(f"[警告] 滚回失败，请手动滚动到底部：{exc}")


if __name__ == "__main__":
    raise SystemExit(main())
