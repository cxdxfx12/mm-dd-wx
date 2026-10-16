"""钉钉侧能力探针：实测当前环境该走「Stream 长连接」还是「UI 自动化」。

    python probe_dingtalk.py

只做只读探测，不发送任何消息、不修改任何配置。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from bridge.config import load_config  # noqa: E402

TOKEN_URL = "https://oapi.dingtalk.com/gettoken?appid={cid}&appsecret={secret}"
TIMEOUT = 8


def ok(label: str, detail: str = "") -> None:
    print(f"  [通过] {label}" + (f" — {detail}" if detail else ""))


def bad(label: str, detail: str = "") -> None:
    print(f"  [失败] {label}" + (f" — {detail}" if detail else ""))


def warn(label: str, detail: str = "") -> None:
    print(f"  [注意] {label}" + (f" — {detail}" if detail else ""))


def mask(value: str) -> str:
    if not value:
        return "(空)"
    if len(value) <= 8:
        return value[:2] + "*" * (len(value) - 2)
    return f"{value[:4]}...{value[-4:]}"


def check_token(cid: str, secret: str) -> tuple[bool, str]:
    """调用 gettoken 验证凭证。返回 (是否可用, 说明)。"""
    url = TOKEN_URL.format(cid=cid, secret=secret)
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        return False, f"网络不可达（{exc.reason}）"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"

    if data.get("errcode") == 0:
        return True, "凭证有效，access_token 获取成功（有效期约 7200 秒）"
    return False, f"errcode={data.get('errcode')} errmsg={data.get('errmsg')}"


def check_sdk() -> tuple[bool, str]:
    try:
        import dingtalk_stream  # noqa: F401
        version = getattr(dingtalk_stream, "__version__", "未知版本")
        return True, f"dingtalk-stream 已安装（{version}）"
    except ImportError:
        return False, "未安装，执行：pip install dingtalk-stream"


def check_dingtalk_client() -> tuple[bool, str]:
    """检测本机是否运行钉钉 PC 客户端（UI 自动化路线的前提）。"""
    try:
        import psutil
    except ImportError:
        return False, "psutil 未安装，无法检测进程"
    found = []
    for proc in psutil.process_iter(["name"]):
        name = (proc.info.get("name") or "").lower()
        if name in ("dingtalk.exe", "dingtalk"):
            found.append(proc.info.get("name"))
    if found:
        return True, f"检测到钉钉客户端进程：{found[0]}"
    return False, "未检测到 DingTalk.exe（UI 自动化路线需要保持客户端登录并运行）"


def main() -> int:
    parser = argparse.ArgumentParser(description="钉钉侧能力探针")
    parser.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except Exception as exc:
        print(f"配置加载失败：{exc}")
        return 2

    dt = cfg.dingtalk
    print("=" * 56)
    print("钉钉侧能力探针")
    print("=" * 56)

    print("\n[1] 凭证配置")
    if dt.client_id and dt.client_secret:
        ok("Client ID / Client Secret 已配置",
           f"client_id={mask(dt.client_id)}, secret={mask(dt.client_secret)}")
    else:
        bad("凭证缺失", "请在 config.yaml 填写，或设置环境变量 "
                        "DINGTALK_CLIENT_ID / DINGTALK_CLIENT_SECRET")

    print("\n[2] 凭证有效性")
    token_ok = False
    if dt.client_id and dt.client_secret:
        token_ok, detail = check_token(dt.client_id, dt.client_secret)
        (ok if token_ok else bad)("access_token 获取", detail)
    else:
        warn("跳过", "凭证未配置")

    print("\n[3] 采集端依赖")
    sdk_ok, sdk_detail = check_sdk()
    (ok if sdk_ok else bad)("Stream 长连接 SDK", sdk_detail)

    print("\n[4] 本机钉钉客户端")
    client_ok, client_detail = check_dingtalk_client()
    (ok if client_ok else warn)("钉钉 PC 客户端", client_detail)

    print("\n" + "=" * 56)
    print("结论")
    print("=" * 56)
    if token_ok and sdk_ok:
        print("  推荐路线 A：Stream 长连接（mode: stream）")
        print("  前提确认：消息必须是发到「你自建应用的机器人」上的。")
        print("            机器人只能收到单聊发给它的消息，以及群里 @它 的消息。")
    elif client_ok:
        print("  凭证不可用，但检测到钉钉客户端。")
        print("  备选路线 B：UI 自动化（mode: ui）")
        print("  注意：钉钉 PC 端为 CEF 架构，消息区可能读不到控件树，")
        print("        需要截图 + OCR 兜底，稳定性低于路线 A。")
    else:
        print("  两条路线都不具备条件，请先完成钉钉开放平台应用配置。")
    print("\n  提示：无论走哪条路线，'读取他人应用推给自己的工作通知'")
    print("        都不在钉钉开放 API 的能力范围内。")
    return 0 if (token_ok or client_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
