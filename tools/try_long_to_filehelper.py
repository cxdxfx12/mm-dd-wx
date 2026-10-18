"""把钉钉最新那条长消息试发到微信「文件传输助手」。

⚠ **只发文件传输助手**。这是用户 2026-10-18 定下的铁律：调试期严禁对任何
业务群试发（OCR 投递端调试时误发到「杭州下沙团队」「服装客户充值对接群」）。

用途：验证长消息「攒齐再发」这条链路在真机上是对的 —— 特别是表格还原和
两截拼接。默认只**预览**，加 ``--send`` 才真发。

    python tools/try_long_to_filehelper.py            # 预览
    python tools/try_long_to_filehelper.py --send     # 真发到文件传输助手

取数走网关 ``GET /screen?raw=1``（手机最近一次上报的那一帧），不碰数据库、
不动基线、不影响正在跑的转发。

``--last N`` 走**已入库的消息**（安装目录的 data/bridge.db），取最近 N 条
真业务消息逐条发出。为什么另开一条路：可视区只有 1753 px，一条 1200 字的长
消息就占掉 1661 px，屏上常常只剩这 1 条 —— 想「看最近三条」时另外两条已经
被顶出屏幕，靠 ``/screen`` 取不回来（手机端的翻屏是「翻上去→立刻翻回来」
成对动作，停不住，没法借它停在上一屏）。而库里那几条本来就是判新老、去重
之后的产物，正是用户想核对的内容。

    python tools/try_long_to_filehelper.py --last 3            # 预览
    python tools/try_long_to_filehelper.py --last 3 --send     # 真发
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import urllib.request
from pathlib import Path

# 程序跑的是**安装版**，库和日志都在安装目录，不是工作区这份（工作区那个
# 停在 10-17，查它会白忙）。``--db`` 可覆盖。
INSTALLED_DB = Path(r"C:\Program Files\喵喵钉钉-微信消息转发桥\data\bridge.db")
WORKSPACE_DB = Path(__file__).resolve().parent.parent / "data" / "bridge.db"

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


def pick_recent(db: Path, n: int) -> list[dict]:
    """从库里取最近 n 条**真业务消息**（剔掉本程序自己的【系统】通知）。

    【系统】那批是运维通知，service 层本来就不投递，混进来只会挤占
    「最近三条」的名额。
    """
    if not db.exists():
        raise SystemExit(f"数据库不存在：{db}")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in
                conn.execute("select * from messages order by rowid desc")]
    finally:
        conn.close()
    biz = [r for r in rows
           if not (r.get("content") or "").lstrip().startswith("【系统】")]
    return list(reversed(biz[:n]))      # 按时间正序发，读起来才顺


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
    ap.add_argument("--last", type=int, default=0, metavar="N",
                    help="改从库里取最近 N 条业务消息逐条发（默认 0 = 仍按"
                         "「这一帧里最长的一条」）")
    ap.add_argument("--db", default="",
                    help="配合 --last 指定数据库（默认安装版，没有则退回工作区）")
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

    # ── 取要发的文本 ────────────────────────────────────────────────
    # 每条都带一行头（群名·发送者·时间），否则连发三条长报告长得几乎一样，
    # 分不清哪条是哪条 —— 这本来就是「我看下」要核对的东西。
    if a.last:
        db = Path(a.db) if a.db else (
            INSTALLED_DB if INSTALLED_DB.exists() else WORKSPACE_DB)
        rows = pick_recent(db, a.last)
        if not rows:
            raise SystemExit("库里没有业务消息")
        print(f"数据库：{db}")
        texts = []
        for i, r in enumerate(rows, 1):
            head = (f"【{i}/{len(rows)}】{r.get('chat_name') or ''}"
                    f"｜{r.get('sender_name') or ''}｜{r.get('created_at') or ''}")
            print(f"{head}  {len(r.get('content') or '')} 字")
            texts.append(f"{head}\n{r.get('content') or ''}")
        print("=" * 60)
        for t in texts:
            print(t)
            print("-" * 60)
    else:
        msg, frame = pick_longest(fetch_frame(a.gateway, token))
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
            text = (f"【数字质控·大江东】{msg.sender_name} "
                    f"{msg.msg_time}\n{msg.content}")
        texts = [text]

    if not a.send:
        print(f"\n预览模式，未发送（共 {len(texts)} 条）。"
              f"要真发到「{TARGET}」：加 --send")
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
    bad = 0
    try:
        for i, text in enumerate(texts, 1):
            print(f"\n>>> [{i}/{len(texts)}] 发送到「{TARGET}」"
                  f"（{len(text)} 字，通道 {a.via}）……")
            res = tgt.send_text(TARGET, text)
            print(f"<<< [{i}/{len(texts)}] ok={res.ok}  {res.detail}")
            if not res.ok:
                bad += 1
    finally:
        tgt.close()
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
