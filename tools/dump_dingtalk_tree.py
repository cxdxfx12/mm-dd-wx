"""导出钉钉客户端控件树，用于编写和调整 UI 采集器的选择器。

    python tools/dump_dingtalk_tree.py > dingtalk_tree.txt

钉钉 PC 端为 CEF/Electron 架构，先用本工具确认消息区控件是否可见；
若整片区域为空，说明必须改用 Stream 模式（不依赖界面结构）。
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from bridge.config import load_config  # noqa: E402


def main() -> int:
    cfg = load_config(str(BASE_DIR / "config.yaml"))
    wxauto_path = Path(cfg.wechat.wxauto_path).resolve()
    if not wxauto_path.exists():
        print(f"wxauto 路径不存在：{wxauto_path}")
        return 2
    sys.path.insert(0, str(wxauto_path))

    try:
        from wxauto import uiautomation as uia
    except Exception as exc:
        print(f"UIAutomation 加载失败：{exc}")
        return 2

    candidates = ("StandardFrame_DingTalk", "DingTalkPCMainWindow", "Chrome_WidgetWin_1")
    wnd = None
    for cls in candidates:
        w = uia.WindowControl(ClassName=cls, searchDepth=1)
        if w.Exists(0.5):
            print(f"# 命中窗口类名：{cls}")
            wnd = w
            break
    if wnd is None:
        w = uia.WindowControl(Name="钉钉", searchDepth=1)
        if w.Exists(1):
            print("# 按标题命中「钉钉」窗口")
            wnd = w
    if wnd is None:
        print("# 未找到钉钉窗口，请确认客户端已启动并登录")
        return 1

    def walk(ctrl, depth: int = 0, max_depth: int = 16) -> None:
        if depth > max_depth:
            return
        try:
            name = (ctrl.Name or "").replace("\n", " ")
            rect = ctrl.BoundingRectangle
            print(
                f"{'  ' * depth}[{ctrl.ControlTypeName}] "
                f"class={ctrl.ClassName!r} name={name!r} "
                f"rect=({rect.left},{rect.top},{rect.right},{rect.bottom})"
            )
        except Exception:
            return
        try:
            children = ctrl.GetChildren()
        except Exception:
            return
        for child in children:
            walk(child, depth + 1, max_depth)

    walk(wnd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
