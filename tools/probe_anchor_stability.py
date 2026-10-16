"""锚点稳定性探针（只读，不改任何东西）。

连续抓 N 帧钉钉消息区，逐帧打印：
  * 本帧识别出的消息条数与指纹；
  * 上一帧有多少条仍出现在本帧（锚点存活数）；
  * 每条消息的纵向位置/高度 vs 消息区高度（判断「一屏装几条」）；
  * 相邻帧之间内容变化的消息（OCR 抖动 or 真的新消息）。

背景：线上反复出现「锚点丢失（聊天区可能被滚动）：本帧不转发，已重建基线」，
新消息被整帧丢弃。锚点丢失只有一个原因——上一帧**没有任何一条**消息的指纹
在本帧里再次出现。本探针用来区分三种成因：

  A. OCR 抖动      —— 同屏内容两帧读出的文本有细微差异 → 指纹全变；
  B. 内容整体换屏  —— 消息太高，新消息进来时上一条被整条顶出视口 → 零重叠；
  C. 真被滚动      —— 用户手动滚动，进入的是历史消息。

用法：
    python tools/probe_anchor_stability.py --frames 6 --gap 5
    python tools/probe_anchor_stability.py --frames 60 --gap 5 --compact
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
    ap = argparse.ArgumentParser(description="锚点稳定性探针（只读）")
    ap.add_argument("--frames", type=int, default=6, help="抓取帧数")
    ap.add_argument("--gap", type=float, default=5.0, help="帧间隔秒数（同线上轮询）")
    ap.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    ap.add_argument("--dump", default="", help="把每帧消息文本写到该文件，便于人工对比")
    ap.add_argument("--compact", action="store_true",
                    help="每帧只打一行（适合长时间挂后台观察）")
    args = ap.parse_args()

    from bridge.config import load_config
    from bridge.sources import dingtalk_ocr as D

    cfg = load_config(args.config)
    src = D.DingTalkOCRSource(cfg)

    prev: list[str] | None = None
    prev_units: list[dict] = []
    dump_lines: list[str] = []
    losses: list[str] = []

    for n in range(1, args.frames + 1):
        t0 = time.time()
        frame = src._grab_frame()
        if frame is None:
            print(f"[帧 {n}] 抓帧失败（窗口/消息区不可用）")
            time.sleep(args.gap)
            continue

        lines = D.merge_lines(frame["blocks"])
        units = D.group_units(lines, frame["area_w"])
        fps = [D.unit_fp(u) for u in units]
        ts = time.strftime("%H:%M:%S")

        if prev is not None:
            cur_set = set(fps)
            alive = sum(1 for f in prev if f in cur_set)
        else:
            alive = None

        if args.compact:
            print(f"[{ts}] 帧{n:>3} 消息{len(units)}条 "
                  f"高{[u['y1'] - u['y0'] for u in units]} "
                  f"视口{frame['area_h']} 锚点存活={alive}"
                  f"/{len(prev) if prev else '-'}"
                  f"{'  <<< 锚点丢失' if alive == 0 else ''}", flush=True)
            if alive == 0:
                losses.append(f"{ts} 帧{n}")
        else:
            title = " / ".join(frame["title"]) if frame["title"] else "(无)"
            ok, ratio, matched = src._title_matches(frame["title"])
            print(f"\n[帧 {n}] {ts}  抓帧+OCR {time.time() - t0:.1f}s  "
                  f"区域 {frame['area_w']}x{frame['area_h']}  "
                  f"块 {len(frame['blocks'])}  行 {len(lines)}  消息 {len(units)}")
            print(f"        标题护栏：{'通过' if ok else '不通过'} "
                  f"(相似度 {ratio:.2f}, 命中 {matched!r})")
            for i, u in enumerate(units, 1):
                print(f"         #{i} y={u['y0']:>4}~{u['y1']:<4} "
                      f"高={u['y1'] - u['y0']:>4} "
                      f"sender={u['sender'][:14]!r} "
                      f"time={u['time']!r} "
                      f"内容={u['content'][:34]!r}")

            if prev is not None:
                cur_set = set(fps)
                alive = sum(1 for f in prev if f in cur_set)
                print(f"        锚点：上一帧 {len(prev)} 条 → 本帧仍存活 {alive} 条"
                      f"{'  ← 锚点丢失！' if not alive else ''}")
                picked, _ = D.pick_new_units(units, fps, prev, src.anchor_guard)
                if picked is None:
                    print("        pick_new_units -> None（本帧会被整体丢弃）")
                else:
                    print(f"        pick_new_units -> {len(picked)} 条新消息 "
                          f"{[units[i]['content'][:20] for i in picked]}")
                same_pos = sum(
                    1 for i, u in enumerate(units)
                    if i < len(prev_units) and prev_units[i]["content"] == u["content"])
                print(f"        同位置内容一致：{same_pos}"
                      f"/{min(len(units), len(prev_units))}")

        if args.dump:
            dump_lines.append(f"===== 帧 {n}  {ts} =====")
            for i, u in enumerate(units, 1):
                dump_lines.append(f"{i:>3}. y={u['y0']}~{u['y1']} "
                                  f"[{u['sender'] or '?':<10}] "
                                  f"{u['content']!r}  time={u['time']!r} "
                                  f"self={u['is_self']}")

        prev, prev_units = fps, units
        if n < args.frames:
            time.sleep(args.gap)

    if losses:
        print(f"\n锚点丢失共 {len(losses)} 次：{', '.join(losses)}")
    if args.dump:
        Path(args.dump).write_text("\n".join(dump_lines), encoding="utf-8")
        print(f"逐帧文本已写入 {args.dump}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
