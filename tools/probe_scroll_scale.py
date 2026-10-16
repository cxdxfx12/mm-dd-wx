"""定量验证「滚动条数值差 == 像素位移」（只读 + 一次极小滚动实验）。

为什么必须验：几何兜底的公式是
    boundary = 可见内容底部 - Δ,   Δ = |Maximum_当前| - |Maximum_基线|
它成立的前提是「滚动条数值的变化量 1:1 等于内容像素位移」。本脚本实测这条。

做法：
  1. 读滚动条 (Value, Maximum, Minimum)，抓一帧，记下每条消息的内容与 y 坐标；
  2. `WheelUp(1)` 向上滚一格，再抓一帧；
  3. 对两帧都出现的消息，比较 y 的位移与 |Value 的变化量|；
  4. 滚回底部（WheelDown 若干格），并校验 Value 回到 Maximum。

    python tools/probe_scroll_scale.py
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


def main() -> int:
    ap = argparse.ArgumentParser(description="滚动条数值 vs 像素位移（只读）")
    ap.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    ap.add_argument("--notches", type=int, default=1, help="向上滚动的格数")
    args = ap.parse_args()

    from bridge.config import load_config
    from bridge.sources import dingtalk_ocr as D

    cfg = load_config(args.config)
    src = D.DingTalkOCRSource(cfg)
    uia = src._load_uia()

    def snapshot(tag):
        frame = src._grab_frame()
        if frame is None:
            print(f"[{tag}] 抓帧失败")
            return None
        units = D.group_units(D.merge_lines(frame["blocks"]), frame["area_w"])
        sc = frame["scroll"]
        if sc:
            print(f"\n[{tag}] 滚动条 值/最大 = {sc[0]:.0f}/{sc[1]:.0f}  "
                  f"贴底={D.at_bottom(sc)}  消息 {len(units)} 条")
        else:
            print(f"\n[{tag}] 滚动条不可读  消息 {len(units)} 条")
        for u in units:
            print(f"          y={u['y0']:>4}~{u['y1']:<4} "
                  f"{u['content'][:44]!r}")
        return frame, units, sc

    base = snapshot("滚动前")
    if base is None:
        return 1
    _, units0, sc0 = base

    area = src._find_window().Control(ClassName=D.MSG_AREA_CLASS, searchDepth=14)
    print(f"\n>>> WheelUp({args.notches})")
    try:
        area.WheelUp(wheelTimes=args.notches)
    except Exception as exc:
        print("  滚动失败：", exc)
        return 1
    time.sleep(1.5)

    after = snapshot("滚动后")
    if after is None:
        return 1
    _, units1, sc1 = after

    print("\n=== 定量比对 ===")
    if sc0 and sc1:
        dv = abs(sc1[0]) - abs(sc0[0])
        dmax = abs(sc1[1]) - abs(sc0[1])
        print(f"  滚动条数值变化：Value {dv:+.0f}px  Maximum {dmax:+.0f}px")
    else:
        dv = None
        print("  滚动条不可读，无法比对")

    by_content = {u["content"]: u for u in units1}
    common = 0
    for u0 in units0:
        u1 = by_content.get(u0["content"])
        if u1 is None:
            continue
        common += 1
        dy = u1["y0"] - u0["y0"]
        verdict = ""
        if dv is not None:
            verdict = "  ✅ 一致" if abs(abs(dy) - abs(dv)) <= 2 else "  ❌ 不一致"
        print(f"  共有消息 {u0['content'][:30]!r}: y {u0['y0']} → {u1['y0']}"
              f"（位移 {dy:+d}px）{verdict}")
    if not common:
        print("  （两帧没有共同消息，无法比对 —— 滚动幅度可能太大或消息太短）")

    print(f"\n>>> 滚回底部（WheelDown {args.notches + 15}）")
    try:
        area.WheelDown(wheelTimes=args.notches + 15)
    except Exception as exc:
        print("  滚动失败：", exc)
    time.sleep(1.5)
    frame = src._grab_frame()
    sc = frame["scroll"] if frame else None
    if sc:
        print(f"  回到底部后 值/最大 = {sc[0]:.0f}/{sc[1]:.0f}  "
              f"贴底={D.at_bottom(sc)}")
    else:
        print("  回到底部后仍读不到滚动条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
