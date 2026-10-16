"""只读观察器：把「真实到达的新消息」与滚动条数值对齐记录。

用途 —— 验证整套新逻辑的关键假设：
    「两帧都贴底时，滚动条 Maximum 的下移量 Δ 恰好等于新到达内容的高度」

同时验证：新逻辑在真实群里是否还会出现「锚点丢失」。

**只读**：不发送任何微信消息，也不点击/滚动钉钉（只截屏 + 读控件）。
适合与线上桥同时运行。

    python tools/watch_collect.py --minutes 10
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
SCROLLBAR_CLASS = "ddesign::SmoothScrollBar"


def main() -> int:
    ap = argparse.ArgumentParser(description="真实群新消息观察器（只读）")
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--gap", type=float, default=5.0)
    ap.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    ap.add_argument("--log", default="", help="把逐帧记录写到文件")
    args = ap.parse_args()

    from bridge.config import load_config
    from bridge.sources import dingtalk_ocr as D

    cfg = load_config(args.config)
    src = D.DingTalkOCRSource(cfg)
    src.title_check = True
    log_lines: list[str] = []

    # 把采集端的日志直接打出来（自检、锚点丢失、切回补发等都在里面）
    import logging

    class _Printer(logging.Handler):
        def emit(self, record):
            print(f"        [日志/{record.levelname}] "
                  f"{record.getMessage()[:130]}", flush=True)

    _bridge_log = logging.getLogger("bridge")
    _bridge_log.addHandler(_Printer())
    _bridge_log.setLevel(logging.INFO)

    # 记录滚动的完整范围，便于验证 Δ 的语义
    def full_scroll():
        try:
            wnd = src._find_window()
            if wnd is None:
                return None
            rect, hit, area = src._find_msg_rect(wnd)
            if area is None:
                return None
            bar = area.Control(ClassName=SCROLLBAR_CLASS, searchDepth=3)
            if not bar.Exists(0.5):
                return None
            pat = bar.GetPattern(src._load_uia().PatternId.RangeValuePattern)
            if pat is None:
                return None
            return float(pat.Value), float(pat.Maximum), float(pat.Minimum)
        except Exception:
            return None

    prev_full = None
    total = 0
    deadline = time.time() + args.minutes * 60

    print(f"开始观察 {args.minutes:.0f} 分钟（只读，不发送、不点击）…\n", flush=True)
    while time.time() < deadline:
        frame = src._grab_frame()
        if frame is None:
            print("[抓帧失败]", flush=True)
            time.sleep(args.gap)
            continue

        f3 = full_scroll()
        lines = D.merge_lines(frame["blocks"])
        units = D.group_units(lines, frame["area_w"])

        out: list[str] = []
        before = time.time()
        try:
            src._poll_once(lambda m: out.append(m.content))
        except Exception as exc:
            print("轮询异常：", exc)
        took = time.time() - before

        ts = time.strftime("%H:%M:%S")
        v = f"{f3[0]:.0f}/{f3[1]:.0f}/{f3[2]:.0f}" if f3 else "n/a"
        growth = ""
        if f3 and prev_full:
            growth = (f"  底部下移 {abs(f3[1]) - abs(prev_full[1]):+.0f}px"
                      f"  当前距底 {abs(f3[1]) - abs(f3[0]):.0f}px")
        kind = "★新消息" if out else "·"
        print(f"[{ts}] {kind} 值/最大/最小={v}{growth}  "
              f"消息{len(units)}条 耗时{took:.1f}s", flush=True)
        for c in out:
            total += 1
            print(f"        → 转发：{c[:70]!r}", flush=True)
        # 底部移动或识别到新消息时，把整帧内容都摊开（用于核对 Δ 与几何边界）
        moved = bool(growth) and "+0px" not in growth
        if out or moved:
            for i, u in enumerate(units, 1):
                print(f"           #{i} y={u['y0']}~{u['y1']}"
                      f"({u['y1'] - u['y0']}px) {u['content'][:40]!r}", flush=True)

        log_lines.append(
            f"[{ts}] v={v} growth={growth.strip()} units={len(units)} emitted={len(out)}"
            + "".join(f"\n    -> {c[:80]!r}" for c in out)
            + "".join(f"\n    #{i} y={u['y0']}~{u['y1']} {u['content'][:50]!r}"
                      for i, u in enumerate(units, 1)))
        prev_full = f3
        time.sleep(args.gap)

    print(f"\n观察结束：共识别并转发 {total} 条", flush=True)
    if args.log:
        Path(args.log).write_text("\n".join(log_lines), encoding="utf-8")
        print(f"逐帧记录已写入 {args.log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
