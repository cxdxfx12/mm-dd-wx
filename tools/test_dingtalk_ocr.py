"""钉钉 OCR 采集端一次性预览（默认只读，不转发、不落库）。

    python tools/test_dingtalk_ocr.py                 # 截一帧，显示解析出的消息
    python tools/test_dingtalk_ocr.py --raw           # 额外显示原始 OCR 块与颜色分类
    python tools/test_dingtalk_ocr.py --watch 30      # 连续观察 30 秒，显示新增
    python tools/test_dingtalk_ocr.py --out shot.png  # 保存消息区截图

判读方法：输出的「将转发的消息」就是正式运行时会发到微信群的内容。
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from bridge.config import load_config                  # noqa: E402
from bridge.sources.dingtalk_ocr import (              # noqa: E402
    DingTalkOCRSource, merge_lines, group_units,
    is_cross_day, unit_fp, pick_new_units,
)


def show_frame(src: DingTalkOCRSource, raw: bool) -> Counter:
    frame = src._grab_frame()
    if frame is None:
        print("[失败] 截帧失败（钉钉未开？窗口最小化？）")
        return Counter()

    print(f"\n[标题条] {frame['title']!r}")
    if src.title_check:
        ok, ratio, matched = src._title_matches(frame["title"])
        mark = "通过" if ok else "不通过（正式运行时本帧全部丢弃）"
        print(f"[护栏] 标题校验：{mark}（相似度 {ratio:.2f}，命中配置：{matched!r}）")

    if raw:
        print(f"\n[原始块] {len(frame['blocks'])} 个（颜色分类后）")
        for b in sorted(frame["blocks"], key=lambda x: (x["y0"], x["x0"])):
            print(f"  y={b['y0']:<5} x={b['x0']:<5} [{b['kind']:<7}] {b['text']}")

    lines = merge_lines(frame["blocks"])
    units = group_units(lines, frame["area_w"])
    print(f"\n[将转发的消息] {len(units)} 条（首帧仅建基线，正式运行时不倒灌历史）")
    dropped = 0
    for i, u in enumerate(units, 1):
        who = "(我)" if u["is_self"] else (u["sender"] or "(未识别)")
        tag = f"  [{u['time']}]" if u["time"] else ""
        if src.time_filter == "today" and is_cross_day(u["time"]):
            tag += "  ✕ 跨天，会被丢弃"
            dropped += 1
        body = u["content"].replace("\n", "\n     ")
        print(f"  {i}. [{who}]{tag} {body}")
    if dropped:
        print(f"  → 其中 {dropped} 条带跨天时间标记，正式运行时会因 time_filter=today 被丢弃")
    return Counter(unit_fp(u) for u in units)


def main() -> int:
    ap = argparse.ArgumentParser(description="钉钉 OCR 采集端预览（默认只读）")
    ap.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    ap.add_argument("--raw", action="store_true", help="显示原始 OCR 块")
    ap.add_argument("--watch", type=int, default=0, metavar="SEC",
                    help="连续观察 SEC 秒，实时显示新消息")
    ap.add_argument("--out", metavar="PNG", help="保存消息区截图到该文件")
    args = ap.parse_args()

    cfg = load_config(args.config)
    src = DingTalkOCRSource(cfg)

    ok, detail = src.check()
    print(f"[{'通过' if ok else '失败'}] 自检：{detail}")
    if not ok:
        return 1

    if args.out:
        frame = src._grab_frame()
        if frame is not None:
            pass
        # _grab_frame 不返回图，这里重截一次
        from bridge.sources.dingtalk_ocr import print_window
        wnd = src._find_window()
        rect, _ = src._find_msg_rect(wnd)
        img = print_window(wnd.NativeWindowHandle).crop(rect)
        img.save(args.out)
        print(f"[截图] 已保存 {args.out}")
        return 0

    if args.watch:
        print(f"\n开始连续观察 {args.watch}s（每 {src.interval:.0f}s 一帧，Ctrl+C 退出）")
        print("判定逻辑与正式运行完全一致：锚点防护 + 跨天过滤")
        deadline = time.time() + args.watch
        prev_fps: list[str] | None = None
        while time.time() < deadline and not src._stop.is_set():
            frame = src._grab_frame()
            if frame is None:
                time.sleep(1)
                continue
            if src.title_check and not src._title_matches(frame["title"])[0]:
                print(f"\n--- {time.strftime('%H:%M:%S')}  标题校验不通过 → 不转发 ---")
                prev_fps = None
                time.sleep(src.interval)
                continue

            lines = merge_lines(frame["blocks"])
            units = group_units(lines, frame["area_w"])
            fps = [unit_fp(u) for u in units]
            print(f"\n--- {time.strftime('%H:%M:%S')}  可见 {len(units)} 条 ---")

            if prev_fps is None:
                print("  （基线帧，历史不转发）")
            else:
                picked, _ = pick_new_units(units, fps, prev_fps, src.anchor_guard)
                if picked is None:
                    print("  ⚠ 锚点丢失（聊天区可能被滚动）→ 本帧整体不转发，只重建基线")
                elif not picked:
                    print("  （无新消息）")
                for i in picked:
                    u = units[i]
                    who = "(我)" if u["is_self"] else (u["sender"] or "(未识别)")
                    if src.time_filter == "today" and is_cross_day(u["time"]):
                        print(f"  ✕ 丢弃跨天 [{u['time']}] {u['content'][:70]}")
                        continue
                    print(f"  ★ 新消息 [{who}] {u['content'][:90]}")
            prev_fps = fps
            time.sleep(src.interval)
        return 0

    show_frame(src, args.raw)
    print("\n（只读预览结束，未转发任何内容）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
