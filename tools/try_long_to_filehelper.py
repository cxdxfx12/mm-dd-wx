"""把钉钉最新那条长消息试发到微信「文件传输助手」。

⚠ **只发文件传输助手**。这是用户 2026-10-18 定下的铁律：调试期严禁对任何
业务群试发（OCR 投递端调试时误发到「杭州下沙团队」「服装客户充值对接群」）。

用途：验证长消息「攒齐再发」这条链路在真机上是对的 —— 特别是表格还原和
两截拼接。默认只**预览**，加 ``--send`` 才真发。

    python tools/try_long_to_filehelper.py            # 预览
    python tools/try_long_to_filehelper.py --send     # 真发到文件传输助手

取数走网关 ``GET /screen?raw=1``（手机最近一次上报的那一帧），不碰数据库、
不动基线、不影响正在跑的转发。
"""
from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.sources.android_parse import (parse_frame, clipped_units,
                                          units_to_messages)

TARGET = "文件传输助手"          # ⚠ 不要改。测试消息只发这里。
DEFAULT_GW = "http://127.0.0.1:8765"


def fetch_frame(url: str, token: str) -> str:
    req = urllib.request.Request(f"{url}/screen?token={token}&raw=1")
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read().decode("utf-8", errors="replace")
    if not raw.lstrip().startswith("<"):
        raise SystemExit(f"网关没返回画面 XML，前 200 字：{raw[:200]!r}")
    return raw


def pick_longest(raw: str):
    """取这一帧里最长的那条消息（长消息转发就是这个场景）。"""
    frame = parse_frame(raw)
    if not frame.units:
        raise SystemExit("这一帧没解析出消息 —— 手机可能停在别的界面")
    msgs = units_to_messages(frame.units, chat_name="probe")
    if not msgs:
        raise SystemExit("解析出单元但没组成消息")
    return max(msgs, key=lambda m: len(m.content)), frame


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", default=DEFAULT_GW)
    ap.add_argument("--token", default="")
    ap.add_argument("--send", action="store_true", help="真的发出去（默认只预览）")
    ap.add_argument("--as-template", action="store_true",
                    help="套用路由模板再发（默认发原文）")
    ap.add_argument("--via", choices=["ocr", "uia"], default="ocr",
                    help="投递端：ocr=截屏+模拟点击（默认，微信自绘空壳时唯一可用）；"
                         "uia=控件树（要求微信处于 mmui:: 正常形态）")
    a = ap.parse_args()

    token = a.token
    if not token:
        for line in Path("config.yaml").read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("gateway_token:"):
                token = s.split(":", 1)[1].strip().strip('"\'')
                break
    if not token:
        raise SystemExit("config.yaml 里找不到 gateway_token")

    msg, frame = pick_longest(fetch_frame(a.gateway, a.token or token))
    cut = clipped_units(frame)
    print(f"可视区 {frame.view}  单元 {len(frame.units)}  页面 {frame.page}")
    print(f"选中最长一条：{msg.sender_name}  {len(msg.content)} 字")
    print(f"这一帧被判截断的条数：{len(cut)}")
    for u in cut:
        print(f"  ⚠ 截断：{u.sender} {len(u.content)} 字")
    print("=" * 60)
    print(msg.content)
    print("=" * 60)

    text = msg.content
    if a.as_template:
        text = f"【数字质控·大江东】{msg.sender_name} {msg.msg_time}\n{msg.content}"

    if not a.send:
        print(f"\n预览模式，未发送。要真发到「{TARGET}」：加 --send")
        return 0

    from bridge.config import load_config
    cfg = load_config("config.yaml")
    if a.via == "uia":
        from bridge.targets.wechat4 import WeChat4Target
        tgt = WeChat4Target(dry_run=False,
                            show_hotkey=getattr(cfg.wechat, "show_hotkey",
                                                "{Ctrl}{Alt}w"))
    else:
        # OCR 端不走 UIA：微信主窗口退化成「Qt 自绘空壳」时它照样能发
        # （那时 mmui:: 控件一个都读不到，UIA 端必然失败，见记忆里的铁律）。
        from bridge.targets.wechat_ocr import WeChatOcrTarget
        tgt = WeChatOcrTarget(dry_run=False)
    print(f"\n>>> 发送到「{TARGET}」（{len(text)} 字，通道 {a.via}）……")
    res = tgt.send_text(TARGET, text)
    print(f"<<< ok={res.ok}  {res.detail}")
    tgt.close()
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
