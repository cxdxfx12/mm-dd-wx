"""PC 钉钉群文件面板探针：清空输入框 → 点击右上角「文件夹」图标 → 截图 → Esc。

    python tools/probe_pc_files.py

会点击钉钉窗口（已获用户同意），只展开面板看结构，不下载任何文件。
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

# 右上角工具栏图标：相对窗口左上的偏移（自 pc_files 截图放大实测）
ICON_FILES = (1513, 57)   # 文件夹（群文件）


def main() -> int:
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

    # 1) 清空聊天输入框（ValuePattern 直接设空，不依赖组合键）
    edit = wnd.EditControl(ClassName="im_chat::InputRichTextEdit", searchDepth=14)
    if edit.Exists(2):
        try:
            edit.GetValuePattern().SetValue("")
            print("输入框已清空（ValuePattern）")
        except Exception as exc:
            print(f"ValuePattern 失败：{exc}")
    else:
        print("没找到输入框控件（跳过清理）")

    # 2) 点击右上角「文件夹」图标
    wr = wnd.BoundingRectangle
    fx = wr.left + ICON_FILES[0]
    fy = wr.top + ICON_FILES[1]
    print(f"点击群文件图标 屏幕=({fx},{fy})  窗口rect=({wr.left},{wr.top},{wr.right},{wr.bottom})")
    uia.Click(x=fx, y=fy, waitTime=1.5)

    # 3) 截图
    SHOTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")
    frame = src._grab_frame()
    if frame is not None:
        p = SHOTS / f"pc_files_{stamp}.png"
        src._last_frame.save(p)
        print(f"截图 → {p}")
        blocks = sorted(frame["blocks"], key=lambda b: (b["y0"], b["x0"]))
        for b in blocks:
            t = b["text"].replace("\n", "\\n")
            if len(t) > 70:
                t = t[:70] + "…"
            print(f"  y={b['y0']:>5} x={b['x0']:>5} {b['kind']:<8} {t}")

    # 4) Esc 收起
    uia.SendKeys("{Esc}", waitTime=0.6)
    print("已按 Esc")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
