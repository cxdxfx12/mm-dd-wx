"""验证「让路」接口：起一个真网关，POST /watch-pause，核对下发行为。

验的是三件事（2026-10-20 那次 176 条事故的直接教训）：

1. 让路期间，收到手机上报帧时下发 watch_min=0（手机据此停掉巡检定时器）；
2. 让路期间，帧/心跳都带 watch_paused_left，方便两头对时间；
3. 解除后立刻恢复配置里的 watch_min，**不残留**。

刻意不碰：不读真实 config.yaml、不碰 data/、不启动微信、端口用 8799。
"""
from __future__ import annotations

import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from bridge.config import (Config, DingTalkConfig, RouteConfig,  # noqa: E402
                          StorageConfig, WeChatConfig)
from bridge.service import BridgeService                        # noqa: E402
from bridge.sources.android_gateway import AndroidGatewaySource  # noqa: E402

PORT = 8799
TOKEN = "selftest-token"
GROUP = "自检目标群"
WATCH_MIN = 10


def _post(path: str, payload=None, token: str = TOKEN, raw: bytes | None = None,
          headers: dict | None = None):
    if raw is not None:
        data = raw
    else:
        data = json.dumps(payload or {}).encode()
    hdrs = {"Content-Type": "application/json", "X-Bridge-Token": token}
    hdrs.update(headers or {})
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", data=data, method="POST", headers=hdrs,
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=5) as resp:
        return json.loads(resp.read().decode())


# 手机上报的是 uiautomator 节点树 XML（不是 JSON），网关会在门口校验
# `<hierarchy`，形状不对直接 400。这里给一份最小合法的。
_FRAME_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<hierarchy rotation="0"><node text="自检" bounds="[0,0][10,10]" /></hierarchy>'
).encode("utf-8")


def _screen(token: str = TOKEN) -> dict:
    return _post("/screen", raw=_FRAME_XML, token=token, headers={
        "Content-Type": "application/xml",
        "X-Ddwx-Device": "SELFTEST",
        "X-Ddwx-Ver": "selftest",
        "X-Ddwx-Texts": "1",
    })


def main() -> int:
    # ignore_cleanup_errors：网关的 sqlite 句柄在 daemon 线程里，进程退出时
    # 可能还没关，Windows 上会因文件被占而抛 PermissionError 盖掉测试结论。
    # 用独立文件名放临时目录，留给系统自己清。
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    db = Path(tmp.name) / "selftest.db"
    cfg = Config(
        dingtalk=DingTalkConfig(
            mode="android", gateway_host="127.0.0.1", gateway_port=PORT,
            gateway_token=TOKEN, chat_names=[GROUP],
            android_parse=False, android_watch_min=WATCH_MIN,
        ),
        wechat=WeChatConfig(dry_run=True, interval=0.0, targets=["自检"]),
        routes=[RouteConfig(name="自检", targets=["自检"])],
        storage=StorageConfig(db_path=str(db)),
    )

    class _Stub:
        def send(self, *a, **k):
            return True

    svc = BridgeService(cfg, target=_Stub())          # type: ignore[arg-type]
    src = AndroidGatewaySource(cfg)
    import threading
    threading.Thread(target=src.run, args=(svc.handle,), daemon=True).start()
    if not src.wait_ready(5.0):
        print("！网关 5 秒内没起来（端口被占？）")
        return 1

    fails: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {name}{'' if cond else '  → ' + detail}")
        if not cond:
            fails.append(name)

    try:
        print("[1] 基线：没让路时下发配置里的 watch_min")
        body = _screen()
        check("watch_min = 10", body.get("watch_min") == WATCH_MIN,
              str(body.get("watch_min")))

        print("[2] 请求让路 120 秒")
        r = _post("/watch-pause", {"seconds": 120})
        check("接口 ok", r.get("ok") is True, str(r))
        check("回落 paused_left > 0", (r.get("paused_left") or 0) > 0,
              str(r.get("paused_left")))

        print("[3] 让路期间再报一帧：必须下发 0")
        body = _screen()
        check("watch_min = 0", body.get("watch_min") == 0,
              str(body.get("watch_min")))
        check("带 watch_paused_left", (body.get("watch_paused_left") or 0) > 0,
              str(body.get("watch_paused_left")))

        print("[4] 让路期间心跳也带标记")
        hb = _post("/ping", {})
        check("心跳带 paused_left", (hb.get("watch_paused_left") or 0) > 0,
              str(hb.get("watch_paused_left")))

        print("[5] 解除让路：立刻恢复")
        r = _post("/watch-pause", {"seconds": 0})
        check("接口 ok", r.get("ok") is True, str(r))
        body = _screen()
        check("watch_min 恢复 10", body.get("watch_min") == WATCH_MIN,
              str(body.get("watch_min")))
        check("不再带 paused_left", "watch_paused_left" not in body,
              str(body.get("watch_paused_left")))

        print("[6] 鉴权：错 token 必须 401")
        try:
            _post("/watch-pause", {"seconds": 60}, token="wrong-token")
            check("错 token 被拒", False, "居然通过了")
        except urllib.error.HTTPError as exc:
            check("错 token 被拒（401）", exc.code == 401, f"HTTP {exc.code}")

        print("[7] seconds 缺省值 = 120")
        r = _post("/watch-pause", {})
        check("默认 120", r.get("seconds") == 120, str(r.get("seconds")))
        _post("/watch-pause", {"seconds": 0})

        print("[8] 非法 seconds 返回 400，且不改变当前状态")
        try:
            _post("/watch-pause", {"seconds": "abc"})
            check("非法值被拒", False, "居然通过了")
        except urllib.error.HTTPError as exc:
            check("非法值被拒（400）", exc.code == 400, f"HTTP {exc.code}")
        body = _screen()
        check("状态没被写坏", body.get("watch_min") == WATCH_MIN,
              str(body.get("watch_min")))

    finally:
        try:
            src.stop()
        except Exception:
            pass

    print()
    if fails:
        print(f"失败 {len(fails)} 项：{fails}")
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
