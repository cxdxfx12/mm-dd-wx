"""转发一条：钉钉抓一帧 → 取一条消息 → 渲染 → 投递到微信群。

默认**只预览不发送**；加 `--yes` 才真实发送（会真的操作微信发出消息）。

    python tools/forward_one.py                       # 预览：抓最新一条，显示将发送的文案
    python tools/forward_one.py --yes                 # 真实转发最新一条
    python tools/forward_one.py --index 2 --yes       # 指定：从最下面往上数第 2 条
    python tools/forward_one.py --text "自定义内容" --yes   # 跳过钉钉，直接测微信投递链路
    python tools/forward_one.py --to "某某群" --yes     # 覆盖目标会话
    python tools/forward_one.py --plain --yes         # 不加【链路测试】提示行

安全设计：
  1. 标题护栏——钉钉当前窗口不是配置里的目标群时，直接中止；
  2. 默认 dry-run，必须显式 `--yes` 才会真发；
  3. 不写数据库——本工具是链路验证用的，不污染正式去重记录。
"""
from __future__ import annotations

import argparse
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

from bridge.config import load_config                          # noqa: E402
from bridge.formatter import DEFAULT_TEMPLATE, render           # noqa: E402
from bridge.models import Message                              # noqa: E402
from bridge.targets.wechat4 import WeChat4Target                # noqa: E402

TEST_BANNER = "【链路测试】本条为自动转发效果测试，可忽略。"


def grab_units(cfg):
    """抓一帧并解析。返回 (units, 标题) 或 (None, None)。"""
    from bridge.sources.dingtalk_ocr import (                   # 延迟导入：OCR 依赖较重
        DingTalkOCRSource, merge_lines, group_units,
    )

    src = DingTalkOCRSource(cfg)
    ok, detail = src.check()
    print(f"[{'通过' if ok else '失败'}] 采集端自检：{detail}")
    if not ok:
        return None, None

    frame = src._grab_frame()
    if frame is None:
        print("[失败] 截帧失败（钉钉未打开？窗口被最小化？）")
        return None, None

    title = (frame["title"] or "").strip()
    print(f"[标题条] {title!r}")
    if src.title_check:
        passed, ratio, matched = src._title_matches(frame["title"])
        print(f"[护栏] 标题校验：{'通过' if passed else '不通过'}"
              f"（相似度 {ratio:.2f}，命中 {matched!r}）")
        if not passed:
            print("[中止] 当前钉钉窗口不是目标会话，拒绝转发（防止发错群内容）。")
            return None, None

    return group_units(merge_lines(frame["blocks"]), frame["area_w"]), title


def main() -> int:
    ap = argparse.ArgumentParser(description="转发一条消息到微信（默认只预览）")
    ap.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    ap.add_argument("--index", type=int, default=1, metavar="N",
                    help="取第 N 条（从最下面/最新往上数，默认 1）")
    ap.add_argument("--text", metavar="TEXT", help="不抓钉钉，直接投递这段文案")
    ap.add_argument("--to", metavar="NAME", help="覆盖目标会话名")
    ap.add_argument("--plain", action="store_true", help="不加【链路测试】提示行")
    ap.add_argument("--yes", action="store_true", help="确认真实发送（不加则只预览）")
    args = ap.parse_args()

    cfg = load_config(args.config)

    # ---------- 目标 ----------
    route = cfg.routes[0] if cfg.routes else None
    target_name = args.to or (route.targets[0] if route and route.targets
                              else (cfg.wechat.targets[0] if cfg.wechat.targets else ""))
    if not target_name:
        print("[失败] 没有配置转发目标，请检查 config.yaml 的 routes[].targets")
        return 1
    template = (route.template if route and route.template else DEFAULT_TEMPLATE)

    # ---------- 取内容 ----------
    source_desc = "手动指定"
    if args.text:
        sender = "手动测试"
        content = args.text
        chat_name = target_name
    else:
        units, title = grab_units(cfg)
        if not units:
            return 1
        print(f"\n[可见消息] {len(units)} 条")
        for i, u in enumerate(reversed(units), 1):
            who = "(我)" if u["is_self"] else (u["sender"] or "(未识别)")
            head = u["content"].replace("\n", " ")[:58]
            print(f"  {'>' if i == args.index else ' '} {i}. [{who}] {head}")

        if not units:
            print("[中止] 当前帧没有解析出消息。")
            return 1
        idx = min(max(1, args.index), len(units))
        if idx != args.index:
            print(f"[提示] index {args.index} 越界，已改用第 {idx} 条（共 {len(units)} 条）")
        u = units[-idx]
        sender = "(我方发出)" if u["is_self"] else (u["sender"] or "")
        content = u["content"]
        chat_name = cfg.dingtalk.chat_names[0] if cfg.dingtalk.chat_names else ""

    msg = Message(
        source="dingtalk",
        chat_id=chat_name,
        chat_name=chat_name,
        sender_id="",
        sender_name=sender,
        company="",
        content=content,
        msg_type="text",
        msg_time=datetime.now(),
        raw_id=f"forward-one-{int(time.time())}",
    )
    body = render(msg, template)
    if not args.plain:
        body = f"{TEST_BANNER}\n{body}"

    print("\n" + "=" * 64)
    print(f"目标会话：{target_name}")
    print(f"来源     ：{source_desc}｜{chat_name or '(未知会话)'}")
    print("-" * 64)
    print(body)
    print("=" * 64)

    # ---------- 投递 ----------
    target = WeChat4Target(
        wxauto_path=str(cfg.resolve(cfg.wechat.wxauto_path)),
        dry_run=not args.yes,
    )
    ok, detail = target.health_check()
    print(f"[{'通过' if ok else '失败'}] 微信投递端：{detail}")
    if not ok:
        target.close()
        return 1

    if not args.yes:
        print("\n[预览模式] 未发送。确认文案无误后，加 --yes 真实转发：")
        print(f'    python tools/forward_one.py'
              f'{f" --index {args.index}" if not args.text else " --text ..."} --yes')
        target.close()
        return 0

    print(f"\n[发送] 正在投递到「{target_name}」…")
    res = target.send_text(target_name, body)
    print(f"[{'成功' if res.ok else '失败'}] {res.detail}")
    target.close()
    return 0 if res.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
