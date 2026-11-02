"""PC 钉钉滚动探针：在消息区用鼠标滚轮向上翻页，逐屏截图 + OCR。

    python tools/probe_pc_scroll.py [翻几次] [每次格数]

只读探测：只滚动、只截屏。会短暂占用鼠标。
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from bridge.config import load_config  # noqa: E402
from bridge.sources.dingtalk_ocr import DingTalkOCRSource  # noqa: E402

SHOTS = Path(__file__).resolve().parent / "shots"


def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    notch = int(sys.argv[2]) if len(sys.argv) > 2 else 3

    cfg = load_config()
    src = DingTalkOCRSource(cfg)
    uia = src._load_uia()

    wnd = src._find_window()
    if wnd is None:
        print("未找到钉钉窗口")
        return 1
    wnd.SetActive()
    wnd.SetFocus()
    time.sleep(0.8)

    msg_rect, hit, _ = src._find_msg_rect(wnd)
    if msg_rect is None:
        print("定位不到消息区")
        return 1
    wr = wnd.BoundingRectangle
    mid_x = wr.left + (msg_rect[0] + msg_rect[2]) // 2
    mid_y = wr.top + (msg_rect[1] + msg_rect[3]) // 2
    print(f"滚轮位置 屏幕坐标=({mid_x},{mid_y})")

    SHOTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")

    for i in range(rounds):
        uia.MoveTo(mid_x, mid_y, waitTime=0.1)
        uia.WheelUp(wheelTimes=notch, interval=0.08, waitTime=0.9)
        frame = src._grab_frame()
        if frame is None:
            print(f"[{i}] 截帧失败")
            continue
        p = SHOTS / f"pc_up{i}_{stamp}.png"
        src._last_frame.crop(msg_rect).save(p)
        texts = [b["text"] for b in sorted(frame["blocks"], key=lambda b: (b["y0"], b["x0"]))]
        joined = " | ".join(texts)
        if len(joined) > 400:
            joined = joined[:400] + "…"
        print(f"[{i}] {p.name}  {joined}")

    # 收尾：滚回底部，不留痕迹
    for _ in range(rounds * notch):
        uia.MoveTo(mid_x, mid_y, waitTime=0.02)
        uia.WheelDown(wheelTimes=6, interval=0.03, waitTime=0.25)
    time.sleep(0.6)
    frame = src._grab_frame()
    if frame is not None:
        print(f"回底 scroll={frame['scroll']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
