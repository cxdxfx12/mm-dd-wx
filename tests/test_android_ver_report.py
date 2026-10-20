"""版本号必须能从**心跳**里读到（android_gateway 的 /ping）。

真机事故（2026-10-20）
    用户装了新版，``/ping`` 却一直答 ``app_ver: "(旧版未上报)"``。

    原因很具体：``_Ctx.touch()`` 支持 ``ver`` 参数，``/messages`` 那条路径也传了，
    但 **``do_POST`` 的 ``/ping`` 分支漏传** —— 而心跳是手机端**唯一规律发送**的
    请求（60 秒一次），``/screen`` 只在画面变化时才来。所以「装没装上」这个问题
    本来就只能靠心跳回答，漏了它，整条版本号通路等于没做。

    上一轮只测了 ``/screen`` 与 ``/messages``，没有一条走心跳，于是整个漏过去。
    这里改用现成的 ``GatewayCase`` 起真网关、发真 HTTP，把三条入口都覆盖住。

    python -m unittest tests.test_android_ver_report -v
"""
from __future__ import annotations

import json
import sys
import unittest
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from tests.test_android_gateway import FRAME_1, GatewayCase  # noqa: E402


class VerCase(GatewayCase):

    def op(self):
        # 本机若有系统代理，urllib 会把 127.0.0.1 当外网发出去（见 MEMORY 里的 502 假故障）
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def post_ping(self, ver=None, device="PAL-AL00"):
        headers = {"X-Bridge-Token": self.token, "X-Ddwx-Device": device,
                   "Content-Type": "application/json"}
        if ver is not None:
            headers["X-Ddwx-Ver"] = ver
        req = urllib.request.Request(self.url("/ping"), data=b"{}",
                                     headers=headers, method="POST")
        self.op().open(req, timeout=6).read()

    def get_ping(self) -> dict:
        req = urllib.request.Request(self.url("/ping"),
                                     headers={"X-Bridge-Token": self.token})
        return json.loads(self.op().open(req, timeout=6).read().decode())


class TestVersionFromHeartbeat(VerCase):
    """本次事故的正题：心跳必须带得动版本号。"""

    def test_heartbeat_reports_version(self):
        self.post_ping(ver="0.4.1")
        self.assertEqual(self.get_ping().get("app_ver"), "0.4.1")

    def test_old_apk_says_so_explicitly(self):
        """旧 APK 不带这个头 —— 要答「旧版未上报」，不能答空串。"""
        self.post_ping(ver=None)
        got = self.get_ping().get("app_ver")
        self.assertEqual(
            got, "(旧版未上报)",
            f"旧版 APK 应明确答「旧版未上报」，实际是 {got!r}；"
            f"答空串会让「没上报」和「读不到」分不清")

    def test_version_updates_on_next_heartbeat(self):
        """升级后下一次心跳就该反映新版本，不用等画面变化。"""
        self.post_ping(ver="0.3.0")
        self.assertEqual(self.get_ping().get("app_ver"), "0.3.0")
        self.post_ping(ver="0.4.1")
        self.assertEqual(self.get_ping().get("app_ver"), "0.4.1")

    def test_version_not_clobbered_by_later_screen(self):
        """心跳记下版本后，一帧不带版本的 /screen 不能把它擦成「旧版未上报」。"""
        self.post_ping(ver="0.4.1")
        req = urllib.request.Request(
            self.url("/screen"), data=FRAME_1.encode("utf-8"),
            headers={"X-Bridge-Token": self.token,
                     "Content-Type": "text/plain; charset=utf-8"},
            method="POST")
        self.op().open(req, timeout=10).read()
        self.assertEqual(self.get_ping().get("app_ver"), "0.4.1")


class TestDeviceStillReported(VerCase):
    """改 touch() 调用点时别把 device 弄丢。"""

    def test_heartbeat_reports_device(self):
        self.post_ping(ver="0.4.1", device="PAL-AL00")
        self.assertEqual(self.get_ping().get("device"), "PAL-AL00")


if __name__ == "__main__":
    unittest.main()
