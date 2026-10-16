"""微信 4.x 投递端验证工具。

安全设计：**默认只读**，不会发送任何消息。

    python tools/test_wechat4.py --check                     # 探活：控件树是否可用
    python tools/test_wechat4.py --open "申通五星业务员培训群"   # 只切换会话并回读，不发消息
    python tools/test_wechat4.py --list                       # 列出当前会话
    python tools/test_wechat4.py --send "文件传输助手" "测试" --yes   # 真发（必须显式 --yes）

先用 --open 确认能准确定位目标会话，再考虑用 --send 做真实投递验证。
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

from bridge.config import load_config                     # noqa: E402
from bridge.targets.wechat4 import WeChat4Target          # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="微信 4.x 投递端验证（默认只读）")
    parser.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    parser.add_argument("--check", action="store_true", help="探活")
    parser.add_argument("--list", action="store_true", help="列出会话表")
    parser.add_argument("--open", metavar="NAME", help="切换到该会话并回读消息（不发消息）")
    parser.add_argument("--locate", metavar="NAME",
                        help="只定位目标会话、不点击（零副作用的安全检查）")
    parser.add_argument("--send", nargs=2, metavar=("NAME", "TEXT"), help="真实发送")
    parser.add_argument("--yes", action="store_true", help="确认真的发送（--send 必填）")
    parser.add_argument("--limit", type=int, default=15, help="回读消息条数")
    args = parser.parse_args()

    cfg = load_config(args.config)
    # 强制 dry_run，避免误发；--send 时再单独放开
    tgt = WeChat4Target(
        wxauto_path=str(cfg.resolve(cfg.wechat.wxauto_path)),
        dry_run=not (args.send and args.yes),
    )

    print("=" * 64)
    print("微信 4.x 投递端验证")
    print("=" * 64)

    ok, detail = tgt.health_check()
    print(f"[{'通过' if ok else '失败'}] 探活：{detail}")
    if not ok:
        return 1

    if args.locate:
        info = tgt.locate_session(args.locate)
        print(f"\n[定位] 目标：{args.locate}")
        print(f"       会话列表当前渲染 {info['total_cells']} 项")
        print(f"       是否找到   ：{'是' if info['found'] else '否'}")
        if info["found"]:
            print(f"       单元格矩形 ：{info['cell_rect']}")
            print(f"       表格可视区 ：{info['table_rect']}")
            print(f"       计划点击点 ：{info['click_point']}")
            print(f"       可安全点击 ：{'是' if info['visible'] else '否（点击点不在表格内，已阻止）'}")
        else:
            print("       列表中的会话：")
            for n in info.get("first_lines", []):
                print(f"         - {n}")
        print("\n（本步骤不点击、不发送，零副作用）")
        return 0 if info["found"] and info["visible"] else 1

    if args.list or (not args.open and not args.send):
        wnd = tgt._window()
        cells = tgt._session_cells(wnd)
        print(f"\n会话列表（当前渲染 {len(cells)} 项）：")
        for c in cells:
            try:
                name = (c.Name or "").split("\n")[0].strip()
                print(f"  - {name}")
            except Exception:
                continue
        if not args.open and not args.send:
            print("\n（只读模式结束。加 --open \"群名\" 可测试会话定位）")
        return 0

    if args.open:
        print(f"\n[切换] 正在定位会话：{args.open}")
        print("       注意：这会改变微信当前打开的会话（不会发消息）")
        if not tgt.open_chat(args.open):
            print("[失败] 未能定位并切到该会话，请检查群名是否与微信里完全一致。")
            return 1
        wnd = tgt._window()
        print(f"[通过] 已切换到「{args.open}」")
        print(f"       标题栏回读：{tgt._chat_title(wnd)!r}")
        print(f"\n最近 {args.limit} 条消息：")
        for cls, text in tgt.read_recent(limit=args.limit):
            short = cls.replace("mmui::", "")
            print(f"  [{short}] {text[:80]}")
        return 0

    if args.send:
        name, text = args.send
        if not args.yes:
            print("\n[拒绝] --send 需要同时加 --yes 明确确认，避免误发。")
            return 2
        print(f"\n[发送] 目标「{name}」\n       内容：{text!r}")
        res = tgt.send_text(name, text)
        print(f"[{'成功' if res.ok else '失败'}] {res.detail}")
        return 0 if res.ok else 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
