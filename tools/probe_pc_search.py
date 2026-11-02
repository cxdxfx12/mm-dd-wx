"""PC 钉钉搜索探针 v2：Ctrl+Shift+F 打开全局搜索，粘贴关键词，截图看面板。

    python tools/probe_pc_search.py [关键词]

只读探测：截图后 Esc 收起，并确保聊天输入框无残留文字。
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
    keyword = sys.argv[1] if len(sys.argv) > 1 else "客户声音待履约汇总"

    cfg = load_config()
    src = DingTalkOCRSource(cfg)
    uia = src._load_uia()

    wnd = src._find_window()
    if wnd is None:
        print("未找到钉钉窗口")
        return 1
    wnd.SetActive()
    wnd.SetFocus()
    time.sleep(0.6)

    # Ctrl+Shift+F 打开全局搜索
    uia.SendKeys("{Ctrl}{Shift}f", waitTime=1.2)
    uia.SetClipboardText(keyword)
    uia.SendKeys("{Ctrl}v", waitTime=2.2)

    SHOTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")

    frame = src._grab_frame()
    if frame is not None:
        p = SHOTS / f"pc_search2_{stamp}.png"
        src._last_frame.save(p)
        print(f"截图 → {p}")

    # 收尾：Esc 收起搜索面板
    uia.SendKeys("{Esc}", waitTime=0.4)
    uia.SendKeys("{Esc}", waitTime=0.4)
    # 保险：若关键词落进了输入框，全选删掉
    uia.SendKeys("^a{Delete}", waitTime=0.4)
    print("已收起搜索并清理输入框")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
