"""PC 钉钉「下载通道」探针：把当前聊天窗口截下来，落图 + 列出全部文字块。

    python tools/probe_pc_download.py

只读探测：只截屏、只 OCR，不点击、不发送、不改配置。
产物：tools/shots/pc_frame_<时间>.png（整窗）与 pc_area_<时间>.png（消息区）
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
    cfg = load_config()
    src = DingTalkOCRSource(cfg)

    wnd = src._find_window()
    if wnd is None:
        print("未找到钉钉窗口（钉钉没开？）")
        return 1
    wr = wnd.BoundingRectangle
    print(f"钉钉窗口 rect=({wr.left},{wr.top},{wr.right},{wr.bottom})")

    try:
        wnd.SetActive()
        wnd.SetFocus()
        time.sleep(0.8)
    except Exception as exc:
        print(f"激活窗口失败（继续，只影响截图是否完整）：{exc}")

    msg_rect, hit, area_ctrl = src._find_msg_rect(wnd)
    if msg_rect is None:
        print("定位不到消息区控件")
        return 1
    print(f"消息区 class={hit} 相对窗口 rect={msg_rect}")

    frame = src._grab_frame()
    if frame is None:
        print("截帧失败")
        return 1

    SHOTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")
    full = SHOTS / f"pc_frame_{stamp}.png"
    area_png = SHOTS / f"pc_area_{stamp}.png"

    img = src._last_frame
    img.save(full)
    img.crop(msg_rect).save(area_png)
    print(f"整窗截图 → {full}")
    print(f"消息区   → {area_png}  (area {frame['area_w']}x{frame['area_h']})")
    print(f"标题候选 → {frame['title']}")
    print(f"滚动条   → {frame['scroll']}")

    blocks = sorted(frame["blocks"], key=lambda b: (b["y0"], b["x0"]))
    print(f"\n共 {len(blocks)} 个文字块（按屏幕顺序）：")
    for b in blocks:
        t = b["text"].replace("\n", "\\n")
        if len(t) > 70:
            t = t[:70] + "…"
        print(f"  y={b['y0']:>5} x={b['x0']:>5} {b['kind']:<8} {t}")

    units = src._group_units(frame) if hasattr(src, "_group_units") else None
    if units is None:
        from bridge.sources.dingtalk_ocr import group_units, merge_lines
        units = group_units(merge_lines(frame["blocks"]), frame["area_w"])
    print(f"\n解析出 {len(units)} 条消息单元：")
    for u in units:
        body = (u.get("text") or "").replace("\n", "\\n")
        if len(body) > 90:
            body = body[:90] + "…"
        print(f"  [{u.get('sender','?')}] {body}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
