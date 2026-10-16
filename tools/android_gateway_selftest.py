"""安卓网关本机自检：起真实网关 → 模拟手机上报 → 核对端到端是否走通。

用途
    在**没有手机**的情况下先把电脑这一侧验通：配置对不对、端口起不起得来、
    鉴权挡不挡得住、报文能不能变成消息、去重有没有生效、投递内容长什么样。

安全
    刻意不碰两样东西：
      · 投递端用桩 —— 绝不启动微信、绝不往群里发消息
      · 数据库用临时文件 —— 不污染 data/bridge.db
    所以随便跑，没有副作用。

它验证不了什么（重要）
    它验证的是**电脑这一侧的链路**，不是「手机能不能读到钉钉消息」。
    后者只有把 tools/android_probe/ 里的东西跑到真机上才知道 ——
    那是整个方案唯一的 go/no-go 闸门，别把这两件事搞混。

用法
    python tools/android_gateway_selftest.py                # 默认端口，2 条样例
    python tools/android_gateway_selftest.py --count 5
    python tools/android_gateway_selftest.py --port 9000 --token abc123
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from bridge.config import (Config, DingTalkConfig, RouteConfig,  # noqa: E402
                          StorageConfig, WeChatConfig)
from bridge.service import BridgeService                        # noqa: E402
from bridge.sources.android_gateway import (AndroidGatewaySource,  # noqa: E402
                                            lan_ips)
from bridge.targets.base import SendResult, Target              # noqa: E402

LINE = "=" * 72

# 样例消息：形状照着生产库里真实的那两条机器人播报来写，包括
# 「昵称带不带机器人后缀」这个已知抖动点（历史上造成过重复采集）。
SAMPLE = [
    {"sender": "签收跟单专员 机器人",
     "content": "@所有人【客户声音履约风险提醒】以下订单存在投诉风险，请尽快跟进："
                "374票 陶龙（1票）、李伟（2票）"},
    {"sender": "拦截跟单专员 机器人",
     "content": "【拦截异常提醒】YT123456789012 已拦截，请核实"},
    {"sender": "播报助手",
     "content": "定时播报：今日累计处理 301286 单"},
]


class StubTarget(Target):
    """只记录，不发送。"""

    name = "selftest-stub"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_text(self, target: str, text: str) -> SendResult:
        self.sent.append((target, text))
        return SendResult(ok=True, target=target, detail="自检桩：未真的发送")


def step(no: int, title: str) -> None:
    print(f"\n[{no}] {title}")


def post(port: int, token: str, payload: dict, header_token: str | None = None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    url = f"http://127.0.0.1:{port}/messages"
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("X-Bridge-Token", header_token if header_token is not None
                   else token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="安卓网关本机自检（不碰微信、不写生产库）")
    ap.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    ap.add_argument("--port", type=int, default=0, help="覆盖配置里的端口")
    ap.add_argument("--token", default="", help="覆盖配置里的 token")
    ap.add_argument("--count", type=int, default=2, help="冒烟上报几条（默认 2）")
    args = ap.parse_args(argv)

    failures: list[str] = []

    # ---------------------------------------------------------- 1. 读配置
    step(1, "读取配置")
    cfg_path = Path(args.config)
    if not cfg_path.is_file():
        print(f"  ✗ 找不到配置文件：{cfg_path}")
        return 1

    from bridge.config import load_config
    real = load_config(cfg_path)
    dt = real.dingtalk
    token = (args.token or getattr(dt, "gateway_token", "") or "").strip()
    port = args.port or int(getattr(dt, "gateway_port", 8765) or 8765)
    chat_name = (dt.chat_names or [""])[0] if dt.chat_names else ""

    print(f"  配置文件    {cfg_path}")
    print(f"  采集模式    {dt.mode}")
    print(f"  网关端口    {port}")
    print(f"  会话名      {chat_name or '(未配置，报文里要自带 chat_name)'}")
    print(f"  token       {'已配置（' + str(len(token)) + ' 字符）' if token else '✗ 没有配置'}")

    if not token:
        print("\n  ✗ dingtalk.gateway_token 是空的。")
        print("    网关要监听局域网，手机才连得上；没有 token 就等于任何人都能")
        print("    往你的微信群发消息 —— 所以程序会拒绝启动。")
        print("    生成一个：")
        print('      python -c "import secrets;print(secrets.token_urlsafe(24))"')
        print(f"    写进 {cfg_path} 的 dingtalk.gateway_token，手机端填同一个值。")
        return 1

    if dt.mode != "android":
        print(f"\n  ⚠ 配置里的 mode 是 {dt.mode!r}，不是 'android'。")
        print("    自检不受影响，但正式运行时要走安卓网关得改成 mode: \"android\"。")

    # ------------------------------------------------- 2. 组装自检用的配置
    step(2, "组装自检环境（临时库 + 投递桩）")
    tmp = tempfile.TemporaryDirectory()
    db = Path(tmp.name) / "selftest.db"
    targets = real.wechat.targets or ["自检目标群"]
    routes = real.routes or [RouteConfig(name="自检", targets=targets)]
    test_cfg = Config(
        dingtalk=DingTalkConfig(
            mode="android", gateway_host="127.0.0.1", gateway_port=port,
            gateway_token=token, chat_names=dt.chat_names,
            companies=dt.companies,
        ),
        wechat=WeChatConfig(dry_run=True, interval=0.0, targets=list(targets)),
        routes=list(routes),
        storage=StorageConfig(db_path=str(db)),
    )
    print(f"  监听地址    127.0.0.1:{port}（只在本机，不触发防火墙提示）")
    print(f"  数据库      {db}（临时，跑完即删）")
    print(f"  投递端      桩：只记录，不启动微信")
    print(f"  转发目标    {'、'.join(targets)}")
    print(f"  路由条数    {len(routes)}")

    stub = StubTarget()
    svc = BridgeService(test_cfg, target=stub)
    src = AndroidGatewaySource(test_cfg)

    # ------------------------------------------------------ 3. 启动网关
    step(3, "启动网关")
    thread = threading.Thread(target=src.run, args=(svc.handle,), daemon=True)
    thread.start()
    if not src.wait_ready(5.0):
        print("  ✗ 网关 5 秒内没起来")
        return 1
    print("  ✓ 已监听")

    try:
        # ------------------------------------------------ 4. 鉴权必须挡住
        step(4, "鉴权检查（没 token / 错 token 必须被挡）")
        code_none, _ = post(port, token, {"messages": []}, header_token="")
        code_bad, _ = post(port, token, {"messages": []}, header_token="wrong")
        print(f"  不带 token → HTTP {code_none} {'✓' if code_none == 401 else '✗ 应该是 401'}")
        print(f"  错 token   → HTTP {code_bad} {'✓' if code_bad == 401 else '✗ 应该是 401'}")
        if code_none != 401:
            failures.append("不带 token 竟然没被拒绝")
        if code_bad != 401:
            failures.append("错误 token 竟然没被拒绝")

        # -------------------------------------------------- 5. 模拟上报
        n = max(1, min(args.count, len(SAMPLE)))
        items = [dict(SAMPLE[i % len(SAMPLE)], seq=i + 1) for i in range(n)]
        payload = {"chat_name": chat_name or "自检群", "messages": items}
        step(5, f"模拟手机上报 {n} 条")
        code, body = post(port, token, payload)
        print(f"  HTTP {code}  {json.dumps(body, ensure_ascii=False)}")
        if code != 200 or body.get("accepted") != n:
            failures.append(f"上报 {n} 条，网关只接受了 {body.get('accepted')} 条")

        # -------------------------------------------------- 6. 落库核对
        step(6, "数据库核对")
        rows = svc.storage.query(limit=50)
        print(f"  库里共 {len(rows)} 条：")
        for r in rows:
            print(f"    · {r['sender_name']} | {str(r['content'])[:44]}"
                  f" | uid={r['uid']}")
        if len(rows) != n:
            failures.append(f"库里应有 {n} 条，实际 {len(rows)} 条")

        # ------------------------------------------------ 7. 去重必须生效
        step(7, "重传同一条（必须被去重）")
        before = len(stub.sent)
        post(port, token, payload)
        after = len(stub.sent)
        print(f"  重传后新增投递 {after - before} 条 "
              f"{'✓ 已去重' if after == before else '✗ 重复转发了'}")
        if after != before:
            failures.append("重传同一条被重复转发 —— 手机会重传，这个必须挡住")

        # -------------------------------------------------- 8. 投递内容
        step(8, "实际会发到微信的内容")
        if not stub.sent:
            print("  ⚠ 一条都没投出去 —— 检查路由规则是否命中（公司/群名过滤）")
            failures.append("消息入库了但没投递出去")
        for tgt, text in stub.sent:
            print(f"  → {tgt}")
            for line in text.splitlines():
                print(f"      {line}")

        # -------------------------------------------------- 9. 手机怎么接
        step(9, "下一步：让手机连进来")
        ips = lan_ips()
        where = f"http://{ips[0]}:{port}" if ips else f"http://<本机IP>:{port}"
        if len(ips) > 1:
            print(f"  本机可能有多个网卡：{', '.join(ips)}")
        print(f"  手机端填：{where}")
        print(f"  token   ：{token}")
        print("  ⚠ 上面这行含 token，别截图往外发。")
        print("  先把 config.yaml 的 mode 改成 \"android\"，再点「开始运行」。")
        print("  想用电脑上的另一台设备试打一发：")
        sample = json.dumps({"chat_name": chat_name or "自检群",
                             "messages": [dict(SAMPLE[0], seq=99)]},
                            ensure_ascii=False)
        print(f'    curl -X POST {where}/messages \\')
        print(f'      -H "X-Bridge-Token: {token}" \\')
        print('      -H "Content-Type: application/json" \\')
        print(f"      -d '{sample}'")

    finally:
        src.stop()
        thread.join(timeout=5.0)
        svc.close()
        tmp.cleanup()

    # ---------------------------------------------------------- 结论
    print("\n" + LINE)
    if failures:
        print("自检未通过：")
        for f in failures:
            print("  ✗ " + f)
        print(LINE)
        return 1
    print("自检通过：电脑这一侧（网关 → 去重 → 路由 → 投递）已就绪。")
    print("注意：这不代表手机读得到钉钉消息 —— 那要靠 tools/android_probe/ 实测。")
    print(LINE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
