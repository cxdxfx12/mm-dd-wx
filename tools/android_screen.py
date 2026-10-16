#!/usr/bin/env python3
"""从电脑侧查看手机上报的最新一帧节点树。

手机端（android/ 那个自建 App）把钉钉当前界面的无障碍节点树 POST 给网关的
``/screen``，这个工具负责在电脑侧把它取回来看。定位很明确：它是**排障与取证的
入口**，不参与转发。

三种用法，对应三个阶段::

    # 1. 手机到底有没有在报？（只看状态，不下载正文）
    python tools/android_screen.py

    # 2. 把最新一帧存下来并出结构判定（判断消息边界靠这个）
    python tools/android_screen.py --save --analyze

    # 3. 盯一段时间，确认上报是持续的而不是只报了一帧
    python tools/android_screen.py --watch 60

token 默认从 config.yaml 的 dingtalk.gateway_token 读，不用手抄。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from bridge.config import load_config  # noqa: E402


def fetch(url: str, timeout: float = 8.0) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError as e:
        raise SystemExit(
            f"连不上网关：{e.reason}\n"
            "  可能的原因，按概率排序：\n"
            "  1. 主程序没在运行（网关是主程序起的，不是常驻服务）\n"
            "  2. config.yaml 的 dingtalk.mode 还不是 android —— 别的模式不会起这个端口\n"
            "  3. 端口被别的程序占了"
        ) from e


def meta_line(meta: dict) -> str:
    if not meta.get("received"):
        return "还没有收到任何画面 —— 手机端没连上，或者无障碍服务没开。"
    age = meta.get("age_sec")
    ago = "刚刚" if age is None or age < 2 else f"{age:.0f} 秒前"
    return (f"最后画面 {meta.get('at', '')}（{ago}）｜ "
            f"{meta.get('bytes', 0)} 字节｜ {meta.get('texts', 0)} 条文本｜ "
            f"第 {meta.get('count', 0)} 帧｜ 设备 {meta.get('device') or '未上报'}｜ "
            f"md5 {str(meta.get('md5', ''))[:8]}")


def run_analyze(path: Path, top: int) -> int:
    sys.path.insert(0, str(BASE_DIR / "tools" / "android_probe"))
    try:
        import analyze_uiautomator as an
    except ImportError as exc:                       # pragma: no cover
        print(f"（分析器不可用，跳过：{exc}）")
        return 0
    return an.report(an.load(path), top)


def watch(base: str, token: str, seconds: int, poll: float) -> int:
    """盯一段时间。打印每一帧的到达时间与体积。

    刻意打印**每一帧**而不是只在变化时打：如果手机每 2 秒报一次、屏幕一直没变，
    这里会看到 count 在涨而 md5 不变 —— 那说明「在报但没变化」。
    如果 count 不涨，就是上报断了，跟画面无关。两种情况要能一眼分开。
    """
    print(f"盯 {seconds} 秒（每 {poll:g} 秒问一次），Ctrl+C 可提前结束\n")
    deadline = time.time() + seconds
    last_count = -1
    last_md5 = ""
    frames = 0
    try:
        while time.time() < deadline:
            code, body = fetch(f"{base}/screen?token={quote(token)}")
            if code != 200:
                print(f"[{datetime.now():%H:%M:%S}] 网关回 {code}："
                      f"{body.decode('utf-8', 'replace')[:120]}")
            else:
                meta = json.loads(body.decode("utf-8"))
                count = meta.get("count", 0)
                md5 = meta.get("md5", "")
                if count != last_count:
                    mark = "变化" if md5 != last_md5 else "同前"
                    print(f"[{datetime.now():%H:%M:%S}] 第 {count} 帧 · {mark} · "
                          f"{meta.get('bytes', 0)} 字节 · {meta.get('texts', 0)} 条文本")
                    last_count, last_md5 = count, md5
                    frames += 1
            time.sleep(poll)
    except KeyboardInterrupt:
        print("\n（提前结束）")

    print(f"\n共看到 {frames} 帧新画面。")
    if frames == 0:
        print("一帧都没有 —— 手机没在上报。请依次确认：\n"
              "  · 手机与电脑在同一网段（手机浏览器打开 /ping?token=... 能不能通）\n"
              "  · App 里填的 IP / 端口 / token 与 config.yaml 一致\n"
              "  · 无障碍服务已开启（App 首页会显示「无障碍服务：已开启」）\n"
              "  · 钉钉停在**具体群聊页**并保持亮屏（停在会话列表读不到正文）")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="查看手机上报到网关的最新一帧钉钉节点树")
    ap.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    ap.add_argument("--host", default="127.0.0.1",
                    help="网关地址。本机查看用默认值即可")
    ap.add_argument("--port", type=int, default=0, help="默认取 config.yaml")
    ap.add_argument("--token", default="", help="默认取 config.yaml")
    ap.add_argument("--save", action="store_true",
                    help="把最新一帧存成 XML 文件")
    ap.add_argument("--out", default="", help="存到哪里，默认 data/screen-<时间>.xml")
    ap.add_argument("--analyze", action="store_true",
                    help="存下来后跑结构判定（判断「一个子节点是不是一条消息」）")
    ap.add_argument("--top", type=int, default=25, help="分析器打印前多少个文本节点")
    ap.add_argument("--watch", type=int, default=0, metavar="秒",
                    help="盯一段时间，看上报是否持续")
    ap.add_argument("--poll", type=float, default=2.0, help="watch 的询问间隔（秒）")
    ap.add_argument("--json", action="store_true", help="原样打印网关返回的 JSON")
    args = ap.parse_args(argv)

    port = args.port
    token = args.token
    if not port or not token:
        try:
            cfg = load_config(args.config)
        except Exception as exc:
            raise SystemExit(f"读不了 {args.config}：{exc}") from exc
        port = port or int(getattr(cfg.dingtalk, "gateway_port", 0) or 8765)
        token = token or str(getattr(cfg.dingtalk, "gateway_token", "") or "").strip()
        if not token:
            raise SystemExit(
                f"{args.config} 里的 dingtalk.gateway_token 是空的。\n"
                "  这个 token 同时也是手机端要填的值；没配的话网关会拒绝启动。")

    base = f"http://{args.host}:{port}"

    if args.watch:
        return watch(base, token, args.watch, args.poll)

    code, body = fetch(f"{base}/screen?token={quote(token)}")
    payload = json.loads(body.decode("utf-8"))
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif code != 200:
        print(f"网关回 {code}：{payload.get('error', '')}")
        hint = payload.get("hint")
        if hint:
            print(f"提示：{hint}")
        return 1
    else:
        print(meta_line(payload))

    if not args.save:
        if code == 200 and not args.json:
            print("\n（要取正文加 --save；要判断消息边界加 --analyze）")
        return 0 if code == 200 else 1

    code2, raw = fetch(f"{base}/screen?token={quote(token)}&raw=1")
    if code2 != 200:
        print(f"取正文失败，网关回 {code2}")
        return 1
    text = raw.decode("utf-8")

    out = Path(args.out) if args.out else (
        BASE_DIR / "data" / f"screen-{datetime.now():%Y%m%d_%H%M%S}.xml")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"\n已保存：{out}（{len(raw)} 字节）")

    if args.analyze:
        print()
        return run_analyze(out, args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
