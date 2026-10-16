"""微信侧只读探针：列出会话，校验转发目标是否存在。

**只读**：不发送任何消息，不修改任何内容。

    python tools/probe_wechat.py
    python tools/probe_wechat.py --check "申通五星业务员培训群"
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


def main() -> int:
    parser = argparse.ArgumentParser(description="微信侧只读探针")
    parser.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    parser.add_argument("--check", action="append", default=[],
                        help="要校验存在的会话名，可重复传入")
    args = parser.parse_args()

    cfg = load_config(args.config)
    wxauto_path = Path(cfg.wechat.wxauto_path).resolve()
    if not wxauto_path.exists():
        print(f"[失败] wxauto 路径不存在：{wxauto_path}")
        return 2
    sys.path.insert(0, str(wxauto_path))

    try:
        from wxauto import WeChat
    except Exception as exc:
        print(f"[失败] wxauto 导入失败：{exc}")
        return 2

    print("=" * 60)
    print("微信侧只读探针")
    print("=" * 60)
    try:
        wx = WeChat()
    except Exception as exc:
        print(f"[失败] 连接微信失败：{type(exc).__name__}: {exc}")
        print("       请确认微信 PC 客户端已启动并登录。")
        return 1

    nick = getattr(wx, "nickname", "") or "未知"
    print(f"[通过] 已连接微信，当前账号：{nick}")
    print(f"       wxauto 版本：{getattr(wx, 'version', '未知')}")
    print(f"       微信窗口版本：{getattr(wx, 'wx_version', '未知')}")

    sessions: list[str] = []
    try:
        sessions = wx.GetSessionList()
    except Exception as exc:
        print(f"[注意] 读取会话列表失败：{type(exc).__name__}: {exc}")

    print(f"\n会话列表（共 {len(sessions)} 个）：")
    for i, name in enumerate(sessions, 1):
        print(f"  {i:>3}. {name}")

    if args.check:
        print("\n" + "=" * 60)
        print("目标校验")
        print("=" * 60)
        missing = 0
        for want in args.check:
            hit = [s for s in sessions if want in s]
            if hit:
                tag = "完全匹配" if want in sessions else f"包含匹配 → {hit[0]}"
                print(f"  [找到] {want}  （{tag}）")
            else:
                missing += 1
                print(f"  [缺失] {want}")
                near = [s for s in sessions if want[:4] in s] if len(want) >= 4 else []
                if near:
                    print(f"         相近的会话：{near}")
                print("         注意：会话列表只显示最近会话，"
                      "老群需要先在微信里打开一次才会出现。")
        return 1 if missing else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
