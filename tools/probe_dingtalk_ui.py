"""钉钉 UI 可读性探针（只读，不点击、不发送任何内容）。

用途：判断「钉钉 PC 客户端的聊天消息」是否暴露给 UIAutomation。
这是 UI 自动化路线（mode: ui）能否成立的前提。

    python tools/probe_dingtalk_ui.py
    python tools/probe_dingtalk_ui.py --window "群名关键字"    # 只看包含该关键字的窗口
    python tools/probe_dingtalk_ui.py --limit 3000             # 输出更多节点
"""
from __future__ import annotations

import argparse
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


def load_uia(cfg):
    wxauto_path = Path(cfg.wechat.wxauto_path).resolve()
    if not wxauto_path.exists():
        raise FileNotFoundError(f"wxauto 路径不存在：{wxauto_path}")
    if str(wxauto_path) not in sys.path:
        sys.path.insert(0, str(wxauto_path))
    from wxauto import uiautomation as uia
    return uia


def list_windows(uia) -> list:
    """枚举所有顶层窗口，按可见性过滤。"""
    out = []
    root = uia.GetRootControl()
    for w in root.GetChildren():
        try:
            if w.ControlTypeName not in ("WindowControl", "PaneControl"):
                continue
            name = (w.Name or "").strip()
            cls = w.ClassName or ""
            rect = w.BoundingRectangle
            if rect.width() <= 0 or rect.height() <= 0:
                continue
            if cls.startswith("Windows.UI.Core") or cls in ("Progman", "Shell_TrayWnd"):
                continue
            out.append((name, cls, rect))
        except Exception:
            continue
    return out


def walk(ctrl, out: list, depth: int = 0, max_depth: int = 18, limit: int = 3000) -> None:
    if depth > max_depth or len(out) >= limit:
        return
    try:
        name = (ctrl.Name or "").replace("\n", "\\n")
        rect = ctrl.BoundingRectangle
        line = (
            f"{'  ' * depth}[{ctrl.ControlTypeName}] "
            f"class={ctrl.ClassName!r} name={name!r} "
            f"rect=({rect.left},{rect.top},{rect.right},{rect.bottom})"
        )
        out.append((line, ctrl.ControlTypeName, ctrl.Name or ""))
    except Exception:
        return
    try:
        children = ctrl.GetChildren()
    except Exception:
        return
    for child in children:
        walk(child, out, depth + 1, max_depth, limit)


def main() -> int:
    parser = argparse.ArgumentParser(description="钉钉 UI 可读性探针")
    parser.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    parser.add_argument("--window", default="", help="只深挖窗口标题含该关键字的窗口")
    parser.add_argument("--limit", type=int, default=3000, help="最多输出节点数")
    parser.add_argument("--max-depth", type=int, default=18)
    parser.add_argument("--out", default="", help="把完整树写入文件（默认打印到终端）")
    args = parser.parse_args()

    cfg = load_config(args.config)
    try:
        uia = load_uia(cfg)
    except Exception as exc:
        print(f"[失败] UIAutomation 加载失败：{exc}")
        return 2
    print("[通过] UIAutomation 可用")

    print("\n" + "=" * 70)
    print("顶层窗口清单")
    print("=" * 70)
    windows = list_windows(uia)
    if not windows:
        print("  （未枚举到任何顶层窗口）")
    for name, cls, rect in windows:
        flag = "  <<< 钉钉候选" if ("DingTalk" in cls or "钉钉" in name) else ""
        print(f"  class={cls!r:<34} title={name!r} "
              f"size={rect.width()}x{rect.height()}{flag}")

    ding = [(n, c, r) for n, c, r in windows
            if "DingTalk" in c or "钉钉" in n
            or ("Chrome_WidgetWin_1" in c and n)]
    if args.window:
        ding = [(n, c, r) for n, c, r in ding if args.window in n]
    if not ding:
        print("\n[失败] 未找到钉钉窗口。请确认钉钉客户端已启动并登录。")
        return 1

    for name, cls, rect in ding:
        print("\n" + "=" * 70)
        print(f"深挖窗口 class={cls!r} title={name!r}  size={rect.width()}x{rect.height()}")
        print("=" * 70)
        wnd = uia.WindowControl(searchDepth=1, ClassName=cls)
        if not wnd.Exists(1):
            wnd = uia.WindowControl(searchDepth=1, Name=name)
        if not wnd.Exists(1):
            print("  窗口句柄获取失败")
            continue

        nodes: list = []
        walk(wnd, nodes, max_depth=args.max_depth, limit=args.limit)
        print(f"  节点总数：{len(nodes)}（上限 {args.limit}）")

        text_nodes = [n for n in nodes if n[1] == "TextControl" and n[2].strip()]
        print(f"  其中有文本内容的 TextControl：{len(text_nodes)} 个")

        if args.out:
            Path(args.out).write_text("\n".join(n[0] for n in nodes), encoding="utf-8")
            print(f"  完整树已写入：{args.out}")
        else:
            for line, _, _ in nodes:
                print(line)

        print("\n  ---- 文本节点摘要（前 40 条）----")
        for _, _, t in text_nodes[:40]:
            print(f"    {t[:70]!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
