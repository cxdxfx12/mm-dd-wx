"""手机把连接掐了，网关不该打 traceback。

背景（2026-10-20 用户看到的那句）：

    ConnectionResetError: [WinError 10054] 远程主机强迫关闭了一个现有的连接。

这是**移动端的日常** —— 上滑杀掉 App、关屏、换 WiFi、切后台，任何一个都会让
电脑侧那条 TCP 连接被 RST。它不代表程序坏了：消息会重传，真断了有按心跳判的
失联告警兜着。但 ``BaseHTTPRequestHandler`` 不接这个异常，一路冒到
``ThreadingHTTPServer.handle_error`` 就打成
``Exception occurred during processing of request`` 全屏 traceback，用户
在日志窗口看到只会以为「程序崩了」，然后去重启 —— 反而真的把采集停掉。

所以这两层必须锁住：
  1. 断连只记 debug，不出 ERROR/exception；
  2. 断连之后网关**还活着**，下一帧照样能收（别把服务线程带死）。
"""
from __future__ import annotations

import logging
import socket
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_android_gateway import TOKEN, GatewayCase        # noqa: E402

XML = ('<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0">'
       '<node text="测试"/>' '</hierarchy>')


class TestDisconnectIsQuiet(GatewayCase):
    """连接被重置 = debug 一条，不是 exception。"""

    def _raw_request(self, body_head: bytes, length: int) -> None:
        """发一个自称有这么长、却只写一点点就断开的请求。

        这就是 Android 端在写请求体中途被系统掐掉的样子，也是
        ``rfile.read(length)`` 抛 10054 的那个位置。
        """
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            req = (b"POST /screen HTTP/1.0\r\n"
                   b"Host: 127.0.0.1\r\n"
                   b"X-Bridge-Token: " + TOKEN.encode() + b"\r\n"
                   b"Content-Length: " + str(length).encode() + b"\r\n\r\n")
            sock.sendall(req + body_head)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                            b"\x01\x00\x00\x00\x00\x00\x00\x00")  # 立刻 RST
        finally:
            sock.close()

    # 网关启动时会有一条「本模式只负责收」的 WARNING（面向用户的提示），
    # 它和断连无关 —— 按内容排除，而不是放宽日志阈值（那会把真问题一起放过）。
    NOISE = "本模式"

    def test_reset_mid_body_does_not_log_traceback(self):
        import time
        with self.assertLogs("bridge.source.android", level=logging.DEBUG) as cap:
            self._raw_request(XML.encode()[:20], len(XML))
            time.sleep(0.4)          # 给服务线程一点时间把异常走完
        bad = [r for r in cap.records
               if self.NOISE not in (r.getMessage() or "")
               and (r.levelno >= logging.WARNING
                    or "Traceback" in (r.getMessage() or ""))]
        self.assertEqual([], [r.getMessage()[:120] for r in bad],
                         "断连打出了警告/异常日志，用户会以为程序坏了")

    def test_gateway_survives_disconnect(self):
        self._raw_request(XML.encode()[:20], len(XML))
        import time
        time.sleep(0.3)
        # 断了之后还能正常服务 —— 服务线程不能被一个 RST 带走
        self.assertTrue(self.source.wait_ready(2.0))
        data = self.get_json("/ping")
        self.assertTrue(data.get("ok"))

    def get_json(self, path: str, **kw) -> dict:
        import json
        import urllib.request
        req = urllib.request.Request(self.url(path, token=TOKEN))
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))


class TestDisconnectHandlersExist(unittest.TestCase):
    """源码约束：两个吞异常的入口都得在。

    为什么用源码断言而不是纯行为断言：``handle_one_request`` 只在
    「读请求行」那一步断连时抛（手机上更常见），构造这种时序要抢毫秒级窗口，
    测试会 flaky。而定住「有这两个覆写」就能防止以后有人把它们当死代码删掉。
    """

    def setUp(self):
        self.src = Path(__file__).resolve().parents[1].joinpath(
            "bridge", "sources", "android_gateway.py").read_text(
                encoding="utf-8")

    def test_handle_one_request_is_wrapped(self):
        self.assertIn("def handle_one_request(self)", self.src)

    def test_handle_is_wrapped(self):
        self.assertIn("    def handle(self)", self.src)

    def test_all_three_disconnect_errors_are_caught(self):
        for name in ("ConnectionResetError", "ConnectionAbortedError",
                     "BrokenPipeError"):
            self.assertIn(name, self.src, f"{name} 没被捕获，日志会出 traceback")

    def test_no_error_level_logging_on_disconnect(self):
        """断连的日志不许用 log.exception / log.error。"""
        import re
        for m in re.finditer(r"log\.(exception|error)\([^)]*", self.src):
            self.assertNotIn("断开", m.group(0),
                             "断连是日常事件，用 error 打会让人以为程序坏了")


if __name__ == "__main__":
    unittest.main()
