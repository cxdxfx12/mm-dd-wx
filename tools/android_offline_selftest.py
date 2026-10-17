"""失联告警自检：不用手机，验证「心跳 → 在线 / 断心跳 → 告警 / 再心跳 → 恢复」。

为什么需要这个工具
    「手机端失联」这条通路的全部价值在于**它真的会响**。可它平时一声不响，
    要验证只能真的把手机 App 杀掉再等 5 分钟 —— 而如果这时告警逻辑写坏了，
    你等来的只有安静，分不清「手机没问题」和「告警根本不会触发」。
    所以这里用假心跳把它压成十几秒的确定性流程。

安全
    只起网关、只发心跳，**不投递任何消息、不碰微信、不连数据库**。

它不是生产配置的证明
    默认阈值 300 秒、检查间隔 10 秒；这里为了跑得快，把两者都压到秒级
    （直接改模块常量）。所以它证明的是「状态机与日志对」，不是「现场等 5 分钟」。

用法
    python tools/android_offline_selftest.py
    python tools/android_offline_selftest.py --after 2 --port 9123
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import bridge.sources.android_gateway as gw                       # noqa: E402
from bridge.config import Config, DingTalkConfig                  # noqa: E402

LINE = "=" * 72


class Capture(logging.Handler):
    """把网关 logger 的记录收进内存，便于按阶段断言。"""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(f"{record.levelname}|{record.getMessage()}")
        except Exception:                        # noqa: BLE001 —— 日志不能反过来炸
            pass

    def since(self, mark: int) -> list[str]:
        return self.lines[mark:]


def heartbeat(port: int, token: str) -> tuple[int, dict]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/ping?hb=1",
                                 data=b"{}", method="POST")
    req.add_header("X-Bridge-Token", token)
    req.add_header("X-Ddwx-Device", "SELFTEST")
    with urllib.request.urlopen(req, timeout=3) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def wait_for(cap: Capture, mark: int, keyword: str,
             timeout: float) -> str | None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for line in cap.since(mark):
            if keyword in line:
                return line
        time.sleep(0.1)
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=0, help="0 = 自动挑一个空闲端口")
    ap.add_argument("--token", default="selftest-token")
    ap.add_argument("--after", type=float, default=2.0,
                    help="失联阈值（秒）。生产是 300，这里压到秒级才好验")
    args = ap.parse_args(argv)

    # 检查间隔也要压小：生产 10 秒一跳，这里压成 0.3 秒。
    gw.OFFLINE_TICK = 0.3

    cfg = Config(dingtalk=DingTalkConfig(
        mode="android", gateway_host="127.0.0.1",
        gateway_port=args.port or _free_port(), gateway_token=args.token,
        chat_names=["自检群"], android_parse=False,
        android_offline_after=args.after))
    port = cfg.dingtalk.gateway_port

    cap = Capture()
    gw.log.addHandler(cap)
    gw.log.setLevel(logging.INFO)

    source = gw.AndroidGatewaySource(cfg)
    thread = threading.Thread(target=source.run, args=(lambda m: None,),
                              daemon=True)
    thread.start()
    if not source.wait_ready(5.0):
        print("✘ 网关没能在 5 秒内就绪")
        return 1

    print(LINE)
    print(f"网关 127.0.0.1:{port} · 失联阈值 {args.after:g} 秒 · "
          f"检查间隔 {gw.OFFLINE_TICK:g} 秒")
    print(LINE)

    problems: list[str] = []
    try:
        # ---- 1. 一次都不发心跳：应报「一次都没有收到」----
        mark = len(cap.lines)
        got = wait_for(cap, mark, "一次都没有收到", args.after + 4)
        _report("① 启动后无任何上报", got, "WARNING|", problems)

        # ---- 2. 发一次心跳：应报「终于连上了」----
        mark = len(cap.lines)
        code, body = heartbeat(port, args.token)
        got = wait_for(cap, mark, "终于连上", 5.0)
        _report(f"② 首个心跳（HTTP {code}）", got, "WARNING|", problems)

        # ---- 3. 停掉心跳：应报「失联」----
        mark = len(cap.lines)
        got = wait_for(cap, mark, "手机端失联", args.after + 4)
        _report("③ 心跳停止", got, "WARNING|", problems)

        # ---- 4. 再发心跳：应报「恢复上报」----
        mark = len(cap.lines)
        code, body = heartbeat(port, args.token)
        got = wait_for(cap, mark, "恢复上报", 5.0)
        _report(f"④ 心跳恢复（HTTP {code}）", got, "WARNING|", problems)

        # ---- 5. 心跳被拒：不能算「活着」----
        mark = len(cap.lines)
        try:
            heartbeat(port, "wrong-token")
        except urllib.error.HTTPError as e:
            if e.code != 401:
                problems.append(f"⑤ 错误 token 竟然回了 {e.code}，应为 401")
        else:
            problems.append("⑤ 错误 token 没有被拒（应 401）")
        print("⑤ 错误 token 被拒：✔" if not any("⑤" in p for p in problems)
              else "⑤ 错误 token 被拒：✘")
    finally:
        source.stop()
        thread.join(timeout=5)
        gw.log.removeHandler(cap)

    print(LINE)
    if problems:
        print(f"不通过：{len(problems)} 处")
        for p in problems:
            print("  -", p)
        print("\n最近几条网关日志：")
        for line in cap.lines[-8:]:
            print("  ", line)
        return 1
    print("全部通过：失联告警的四条路径（从未收到 / 首次连上 / 失联 / 恢复）都按预期触发")
    return 0


def _report(title: str, got: str | None, prefix: str,
            problems: list[str]) -> None:
    if got is None:
        print(f"{title}：✘ 没有等到预期的日志")
        problems.append(f"{title}：没有触发")
    elif not got.startswith(prefix):
        print(f"{title}：✘ 级别不对 —— {got[:80]}")
        problems.append(f"{title}：级别应为 {prefix}，实际 {got[:40]}")
    else:
        print(f"{title}：✔")
        print(f"    {got[got.index('|') + 1:][:100]}…")


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


if __name__ == "__main__":
    sys.exit(main())
