"""微信 4.x 控件树只读探针。

**只读**：不发送消息、不点击、不修改任何配置。

背景：微信 4.0 起改用 Qt Quick/QML 渲染，默认不向系统暴露 UIA 树，
窗口类名也从 `WeChatMainWndForPC` 变为 `mmui::MainWindow`。
本工具用于判断当前环境是否还能走 UIAutomation 路线。

    python tools/probe_wechat_ui.py
    python tools/probe_wechat_ui.py --out wechat_tree.txt --max-depth 22
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

# 微信各版本主窗口类名
WECHAT_CLASSES = (
    "WeChatMainWndForPC",   # 3.9.x（wxauto 支持）
    "mmui::MainWindow",     # 4.x
)

# 新版微信关键元素（用于判断控件树是否可用）
KEY_ELEMENTS = (
    "mmui::ChatSessionCell",
    "mmui::ChatInputField",
    "mmui::XValidatorTextEdit",
    "mmui::MainWindow",
)


def walk(ctrl, out: list, depth: int = 0, max_depth: int = 20, limit: int = 20000) -> None:
    if depth > max_depth or len(out) >= limit:
        return
    try:
        name = (ctrl.Name or "").replace("\n", "\\n")
        rect = ctrl.BoundingRectangle
        out.append((
            f"{'  ' * depth}[{ctrl.ControlTypeName}] "
            f"class={ctrl.ClassName!r} name={name!r} "
            f"rect=({rect.left},{rect.top},{rect.right},{rect.bottom})",
            ctrl.ControlTypeName,
            ctrl.ClassName or "",
            ctrl.Name or "",
        ))
    except Exception:
        return
    try:
        children = ctrl.GetChildren()
    except Exception:
        return
    for child in children:
        walk(child, out, depth + 1, max_depth, limit)


def main() -> int:
    parser = argparse.ArgumentParser(description="微信 4.x 控件树探针")
    parser.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    parser.add_argument("--out", default="", help="把完整树写入文件")
    parser.add_argument("--max-depth", type=int, default=20)
    args = parser.parse_args()

    cfg = load_config(args.config)
    wxauto_path = Path(cfg.wechat.wxauto_path).resolve()
    if not wxauto_path.exists():
        print(f"[失败] wxauto 路径不存在：{wxauto_path}")
        return 2
    sys.path.insert(0, str(wxauto_path))

    try:
        from wxauto import uiautomation as uia
    except Exception as exc:
        print(f"[失败] UIAutomation 加载失败：{exc}")
        return 2

    print("=" * 68)
    print("微信控件树探针（只读）")
    print("=" * 68)

    wnd = None
    hit_class = ""
    for cls in WECHAT_CLASSES:
        w = uia.WindowControl(ClassName=cls, searchDepth=1)
        if w.Exists(0.5):
            wnd, hit_class = w, cls
            break
    if wnd is None:
        print("[失败] 未找到微信主窗口。")
        print(f"       尝试过的类名：{WECHAT_CLASSES}")
        print("       请确认微信 PC 客户端已启动并登录。")
        return 1

    print(f"[通过] 找到微信主窗口，类名 = {hit_class!r}")
    if hit_class == "WeChatMainWndForPC":
        print("       这是微信 3.9.x —— wxauto 原版即可支持。")
    else:
        print("       这是微信 4.x —— wxauto 3.9 的类名逻辑【不匹配】，需要新适配层。")

    nodes: list = []
    walk(wnd, nodes, max_depth=args.max_depth)
    print(f"       控件树节点总数：{len(nodes)}")

    text_nodes = [n for n in nodes if n[1] in ("TextControl", "EditControl") and n[3].strip()]
    print(f"       含文本的 Text/Edit 控件：{len(text_nodes)}")

    print("\n---- 关键元素检测 ----")
    found_any = False
    for key in KEY_ELEMENTS:
        cnt = sum(1 for n in nodes if n[2] == key)
        if cnt:
            found_any = True
            print(f"  [找到] {key}  × {cnt}")
        else:
            print(f"  [缺失] {key}")

    treesize = len(nodes)
    print("\n" + "=" * 68)
    print("结论")
    print("=" * 68)
    if treesize <= 60 and not found_any:
        print("  ❌ 控件树几乎为空 —— 微信未向系统暴露 UIA 树，")
        print("     UIAutomation 自动化路线在此环境下【不可用】。")
        print()
        print("  可尝试的激活方式（需人工操作，且会短暂朗读屏幕）：")
        print("    Win 键 → 搜索「讲述人 / Narrator」→ 打开 → 立即关闭")
        print("    之后重新运行本探针，观察节点数是否增加。")
    elif found_any:
        print("  ✅ 检测到关键元素，控件树可用，")
        print("     可以基于 mmui::* 类名重写微信投递端。")
    else:
        print(f"  ⚠ 控件树有 {treesize} 个节点但未命中关键元素，")
        print("     需人工查看导出文件确认结构。")

    if args.out:
        Path(args.out).write_text("\n".join(n[0] for n in nodes), encoding="utf-8")
        print(f"\n  完整树已写入：{args.out}")
    else:
        print("\n---- 树结构（前 60 行）----")
        for line, _, _, _ in nodes[:60]:
            print(line)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
