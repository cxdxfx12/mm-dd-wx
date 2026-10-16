#!/usr/bin/env python3
"""只收不转的验收/排障入口：起一个接收网关，等手机把画面报上来。

为什么单独做一个工具
    正式运行时，网关是主程序（``mode: "android"``）顺带起的。但联调阶段
    「起主程序」代价太大：要开设置中心、要自检微信、还会去抢微信焦点。
    而这个阶段我们要的只是**一帧真实画面**，用来确认「一个子节点 = 一条消息」
    这个边界判据。

    所以这里只起 ``AndroidGatewaySource`` 这一层，别的什么都不接。

安全
    刻意不碰四样东西：
      · 不开 BridgeService —— 不接受任何消息，也就无所谓转发
      · 不启微信、不连数据库 —— 连 ``data/bridge.db`` 都不打开
      · 不改 ``config.yaml``（只读 port/token，mode 是 ocr 也能跑）
      · ``/messages`` 收到什么都是丢进黑洞，不会进微信

用法
    # 等手机报第一帧，存下来并出结构判定（最常用）
    python tools/android_capture.py

    # 只在本机验（不触发防火墙提示，配合 curl 手工打一发）
    python tools/android_capture.py --bind 127.0.0.1

    # 多等一会儿
    python tools/android_capture.py --timeout 300

    # 已经收到过帧了？那就直接看最新那一帧
    python tools/android_capture.py --now
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from bridge.config import (Config, DingTalkConfig, RouteConfig,  # noqa: E402
                           StorageConfig, WeChatConfig, load_config)
from bridge.sources.android_gateway import (AndroidGatewaySource,  # noqa: E402
                                            lan_ips)
import urllib.error                                            # noqa: E402
import urllib.request                                          # noqa: E402

LINE = "-" * 68


# ------------------------------------------------------------------ 纯函数
# 抽出来是为了能单测：这些地方错了，现场表现是「等了半天一帧没有」，
# 而真正的原因可能只是端口或 token 读错了 —— 那种问题最难查。

def build_capture_config(real: Config, *, host: str = "", port: int = 0,
                         token: str = "") -> Config:
    """从真实配置里抠出「只收」所需的最小配置。

    只带走网关相关的三个值 + 会话名/公司（路由匹配和日志展示要用），
    其余一律用无害的默认值 —— 尤其 ``routes`` 留空、``dry_run`` 为 True。
    """
    dt = real.dingtalk
    return Config(
        dingtalk=DingTalkConfig(
            mode="android",
            gateway_host=host or str(getattr(dt, "gateway_host", "") or "").strip()
            or "0.0.0.0",
            gateway_port=int(port or getattr(dt, "gateway_port", 0) or 8765),
            gateway_token=token or str(getattr(dt, "gateway_token", "") or "").strip(),
            chat_names=list(getattr(dt, "chat_names", None) or []),
            companies=list(getattr(dt, "companies", None) or []),
        ),
        wechat=WeChatConfig(dry_run=True, interval=0.0),
        routes=[],
        storage=StorageConfig(db_path=":memory:"),
    )


def first_lan_ip(ips: list[str]) -> str:
    """挑一个适合给手机填的地址：跳过回环与本机链路本地地址。"""
    for ip in ips:
        if ip.startswith("127.") or ip.startswith("169.254."):
            continue
        return ip
    return ips[0] if ips else "<本机IP>"


def meta_line(meta: dict) -> str:
    age = meta.get("age_sec")
    ago = "刚刚" if age is None or age < 2 else f"{age:.0f} 秒前"
    return (f"第 {meta.get('count', 0)} 帧 · {meta.get('at', '')}（{ago}）· "
            f"{meta.get('bytes', 0)} 字节 · {meta.get('texts', 0)} 条文本 · "
            f"设备 {meta.get('device') or '未上报'}")


# ------------------------------------------------------------------ 取帧
def fetch(url: str, timeout: float = 8.0) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        return 0, str(exc).encode("utf-8")


def get_screen(base: str, token: str, raw: bool = False) -> tuple[int, bytes]:
    url = f"{base}/screen?token={quote(token)}" + ("&raw=1" if raw else "")
    return fetch(url, timeout=8.0)


def analyze(path: Path, top: int) -> None:
    sys.path.insert(0, str(BASE_DIR / "tools" / "android_probe"))
    try:
        import analyze_uiautomator as an
    except ImportError as exc:                        # pragma: no cover
        print(f"（分析器不可用，跳过结构判定：{exc}）")
        return
    an.report(an.load(path), top)


def save_frame(base: str, token: str, args) -> Path | None:
    code, raw = get_screen(base, token, raw=True)
    if code != 200:
        print(f"取正文失败：网关回 {code} —— {raw.decode('utf-8', 'replace')[:200]}")
        return None
    out = Path(args.out) if args.out else (
        BASE_DIR / "data" / f"screen-{datetime.now():%Y%m%d_%H%M%S}.xml")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(raw.decode("utf-8"), encoding="utf-8")
    print(f"已存下这一帧：{out}（{len(raw)} 字节）")
    return out


# ------------------------------------------------------------------ 主流程
def build_parser() -> argparse.ArgumentParser:
    """抽出来是为了能单测默认值 —— 参数默认值改错，现场表现同样是「一帧没有」。"""
    ap = argparse.ArgumentParser(
        description="只收不转：起接收网关，等手机报一帧画面并出结构判定")
    ap.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    ap.add_argument("--bind", default="",
                    help="监听的地址，默认取 config.yaml 的 gateway_host（0.0.0.0）。"
                         "只在本机自测时用 127.0.0.1")
    ap.add_argument("--port", type=int, default=0, help="默认取 config.yaml")
    ap.add_argument("--token", default="", help="默认取 config.yaml")
    ap.add_argument("--timeout", type=float, default=180.0,
                    help="等第一帧最多等多少秒（默认 180）")
    ap.add_argument("--now", action="store_true",
                    help="不等，直接看当前最新一帧")
    ap.add_argument("--keep", action="store_true",
                    help="收到后继续盯着（观察上报是否持续）")
    ap.add_argument("--out", default="", help="存到哪里，默认 data/screen-<时间>.xml")
    ap.add_argument("--top", type=int, default=25, help="判定时打印前多少个文本节点")
    ap.add_argument("--no-analyze", action="store_true", help="只存不做结构判定")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # ------------------------------------------------------- 1. 读配置
    cfg_path = Path(args.config)
    if not cfg_path.is_file():
        print(f"找不到配置文件：{cfg_path}")
        return 1
    real = load_config(cfg_path)
    cap = build_capture_config(real, host=args.bind, port=args.port,
                               token=args.token)

    if not cap.dingtalk.gateway_token:
        print(f"{cfg_path} 里的 dingtalk.gateway_token 是空的。")
        print("  这个 token 同时也是手机端要填的值；没配的话网关会拒绝启动。")
        print('  生成一个：python -c "import secrets;print(secrets.token_urlsafe(24))"')
        return 1

    src = AndroidGatewaySource(cap)
    host, port, token = src.host, src.port, src.token
    # 本机取帧一律走 127.0.0.1：0.0.0.0 是「所有网卡」这个绑定语义，
    # 拿它当连接地址在 Windows 上未必通。
    view_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    base = f"http://{view_host}:{port}"
    phone_ip = "127.0.0.1" if view_host == "127.0.0.1" else first_lan_ip(lan_ips())

    print(LINE)
    print("只收不转模式：不开微信、不连数据库、不改 config.yaml")
    print(LINE)
    print(f"配置文件      {cfg_path}")
    print(f"当前采集模式  {real.dingtalk.mode}（本工具不受它影响，照跑）")
    print(f"监听          {host}:{port}")
    print(f"手机端填      http://{phone_ip}:{port}")
    print(f"token         {token}")
    print("              ⚠ 这行含 token，别截图往外发。")
    print(LINE)

    # ------------------------------------------------------- 2. 起网关
    thread = threading.Thread(target=src.run, args=(lambda _m: None,), daemon=True)
    thread.start()
    if not src.wait_ready(6.0):
        print("网关 6 秒内没起来 —— 端口八成被占了。")
        print(f"  查一下：netstat -ano | findstr :{port}")
        return 1
    print(f"网关已监听，开始等手机上报（最多 {args.timeout:g} 秒）")

    try:
        # --------------------------------------------------- 3. 等第一帧
        first: dict | None = None
        if args.now:
            code, body = get_screen(base, token)
            first = json.loads(body.decode("utf-8")) if code == 200 else {}
        else:
            deadline = time.time() + args.timeout
            next_notice = time.time() + 10.0
            while time.time() < deadline:
                code, body = get_screen(base, token)
                if code == 404:
                    # 「还没有收到任何画面」就是这个接口的正常答复 ——
                    # 不是错误，别吓人。
                    meta = {}
                elif code != 200:
                    print(f"  网关回了 {code}：{body.decode('utf-8', 'replace')[:120]}")
                    time.sleep(2.0)
                    continue
                else:
                    meta = json.loads(body.decode("utf-8"))
                    if meta.get("received"):
                        first = meta
                        break
                if time.time() >= next_notice:
                    left = int(deadline - time.time())
                    print(f"  [{datetime.now():%H:%M:%S}] 还没有画面……"
                          f"（继续等，剩 {left} 秒）")
                    next_notice = time.time() + 15.0
                time.sleep(1.0)

        if not first or not first.get("received"):
            print(f"\n{args.timeout:g} 秒内没收到任何画面。按概率排序排查：")
            print("  1. 手机和电脑不在同一网段 —— 手机浏览器打开"
                  f" http://{first_lan_ip(lan_ips())}:{port}/ping?token=… 试试")
            print("  2. App 里填的 IP / 端口 / token 与上面不一致")
            print("  3. 无障碍服务没开（App 首页会显示「无障碍服务：已开启」）")
            print("  4. 钉钉停在**会话列表**而不是群聊页 —— 那里读不到正文，"
                  "有些 ROM 甚至整树为空")
            print("  5. 手机锁屏了（锁屏后无障碍节点树可能整体不可读）")
            print("\n电脑侧已确认没问题（网关起得来、端口在听）。"
                  "剩下的都在手机那一侧。")
            return 1

        print(f"\n收到画面了：{meta_line(first)}")

        # --------------------------------------------------- 4. 存 + 判定
        out = save_frame(base, token, args)
        if out and not args.no_analyze:
            print(LINE)
            print("结构判定（关键：看有没有「一个子节点 = 一条消息」的容器）")
            print(LINE)
            analyze(out, args.top)
            print(LINE)
            print("怎么读这个判定：")
            print("  · 出现 RecyclerView/ListView 容器、下面挂着若干子节点，"
                  "每个子节点里")
            print("    有发送者 + 正文 → 边界清楚，解析器就好写")
            print("  · 消息被拆成一堆散落的 TextView（发送者、时间、正文各自独立、")
            print("    没有共同父节点）→ 得靠坐标聚类，先别写解析器，把这一帧发我")
            print("  · 一条文本都没有 → 钉钉在这一页没暴露节点树，回到 OCR 方案")

        # --------------------------------------------------- 5. 继续盯
        if args.keep:
            print(LINE)
            print("继续盯着（Ctrl+C 结束）")
            last_count = int(first.get("count", 0))
            last_md5 = str(first.get("md5", ""))
            try:
                while True:
                    time.sleep(2.0)
                    code, body = get_screen(base, token)
                    if code != 200:
                        print(f"  网关回 {code}")
                        continue
                    meta = json.loads(body.decode("utf-8"))
                    count = int(meta.get("count", 0))
                    if count != last_count:
                        mark = "变化" if meta.get("md5") != last_md5 else "同前"
                        print(f"  [{datetime.now():%H:%M:%S}] 第 {count} 帧 · {mark} · "
                              f"{meta.get('texts', 0)} 条文本")
                        last_count, last_md5 = count, str(meta.get("md5", ""))
            except KeyboardInterrupt:
                print("\n（结束）")
        return 0

    finally:
        src.stop()
        thread.join(timeout=5.0)


if __name__ == "__main__":
    raise SystemExit(main())
