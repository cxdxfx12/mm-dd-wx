"""PC 钉钉群文件探针 v2：点「文件夹」图标后枚举全部顶层窗口，找独立弹出的面板。

    python tools/probe_pc_files2.py
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
ICON_FILES = (1513, 57)   # 文件夹（群文件）相对窗口左上


def list_windows(uia, tag: str) -> None:
    print(f"--- 顶层窗口（{tag}）---")
    desktop = uia.GetRootControl()
    for w in desktop.GetChildren():
        try:
            if not w.IsVisible:
                continue
            r = w.BoundingRectangle
            if r.width() <= 0 or r.height() <= 0:
                continue
            print(f"  [{w.ControlTypeName}] class={w.ClassName!r} name={w.Name!r} "
                  f"rect=({r.left},{r.top},{r.right},{r.bottom})")
        except Exception:
            continue


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
    time.sleep(0.6)

    list_windows(uia, "点击前")

    wr = wnd.BoundingRectangle
    uia.Click(x=wr.left + ICON_FILES[0], y=wr.top + ICON_FILES[1], waitTime=2.0)
    print(f"已点击 屏幕=({wr.left + ICON_FILES[0]},{wr.top + ICON_FILES[1]})")

    list_windows(uia, "点击后")

    SHOTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")

    # 若出现新窗口，逐个截屏
    desktop = uia.GetRootControl()
    for w in desktop.GetChildren():
        try:
            if not w.IsVisible:
                continue
            r = w.BoundingRectangle
            if r.width() <= 0 or r.height() <= 0:
                continue
            if w.ClassName in ("StandardFrame_DingTalk", "DingTalkPCMainWindow"):
                continue
            if "DingTalk" in (w.ClassName or "") or "钉" in (w.Name or ""):
                p = SHOTS / f"pc_files2_wnd_{stamp}_{w.ClassName[:20]}.png"
                from bridge.sources.dingtalk_ocr import print_window
                img = print_window(w.NativeWindowHandle)
                if img is not None:
                    img.save(p)
                    print(f"新窗口截图 → {p}  ({img.size[0]}x{img.size[1]})")
        except Exception:
            continue

    print("（不收尾，面板保持打开，便于人工查看；看完可手动 Esc）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
