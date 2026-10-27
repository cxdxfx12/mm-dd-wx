# -*- coding: utf-8 -*-
"""投递失败补投：瞬态失败 30 秒后原样重发一次，只补一次。

背景（2026-10-27 17:19 真机）：微信主窗口退化成幽灵窗 → 遮挡检测拦下
投递（fail 一次）→ uid 已入库、后续帧被去重跳过 → 656 字的消息**永久
漏发**。补投就是给这类「过一会儿自己就好」的失败一次重来的机会。
"""
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from bridge.models import Message
from bridge.service import BridgeService
from bridge.targets.base import SendResult


def free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _FlakyTarget:
    """前 N 次 send_text 失败，之后成功；记录每次调用的目标。"""

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls: list[str] = []

    def send_text(self, target: str, text: str):
        self.calls.append(target)
        if len(self.calls) <= self.fail_times:
            return SendResult(False, "fake", "窗口被遮挡（模拟）")
        return SendResult(True, "fake", "已发送（模拟）")

    def send_image(self, target: str, path: str):
        return SendResult(True, "fake")

    def close(self):
        pass


def make_svc(target):
    """建一个真 Service（临时库 + 假投递端），返回 (svc, db路径)。"""
    from bridge.config import (Config, DingTalkConfig, RouteConfig,
                               StorageConfig, WeChatConfig)
    tmp = tempfile.mkdtemp()
    dt = DingTalkConfig(mode="android", gateway_host="127.0.0.1",
                        gateway_port=free_port(), gateway_token="t",
                        chat_names=["数字质控"])
    cfg = Config(
        dingtalk=dt,
        storage=StorageConfig(db_path=str(Path(tmp) / "bridge.db")),
        wechat=WeChatConfig(targets=["测试群"], table_image=False,
                            interval=0),
        routes=[RouteConfig(chat_names=["数字质控"], targets=["测试群"])],
    )
    svc = BridgeService(cfg, target=target)
    svc.retry_delay = 0.05          # 测试不等 30 秒
    return svc, str(Path(tmp) / "bridge.db")


def deliveries(db_path: str) -> list[tuple]:
    db = sqlite3.connect(db_path)
    try:
        return db.execute(
            "select status, error from deliveries order by rowid").fetchall()
    finally:
        db.close()


def _msg(content="测试消息"):
    return Message(content=content, sender_name="张三", chat_name="数字质控")


def wait_calls(tgt, n, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline and len(tgt.calls) < n:
        time.sleep(0.05)


class TestDeliveryRetry(unittest.TestCase):
    def test_fail_then_retry_succeeds(self):
        """失败一次 → 补投成功：两条投递记录，第二条 ok 且带补投标记。"""
        tgt = _FlakyTarget(fail_times=1)
        svc, db = make_svc(tgt)
        self.addCleanup(svc.close)
        self.assertTrue(svc.handle(_msg()))
        wait_calls(tgt, 2)
        self.assertEqual(len(tgt.calls), 2, "失败后必须补投一次")
        rec = deliveries(db)
        self.assertEqual([r[0] for r in rec], ["fail", "ok"])
        self.assertIn("窗口被遮挡", rec[0][1])
        self.assertIn("失败补投", rec[1][1])

    def test_retry_only_once(self):
        """补投仍失败就认输：总共恰好 2 次调用，不无限刷群。"""
        tgt = _FlakyTarget(fail_times=99)
        svc, _db = make_svc(tgt)
        self.addCleanup(svc.close)
        self.assertTrue(svc.handle(_msg()))
        wait_calls(tgt, 2)
        time.sleep(0.4)                 # 再等等，确认没有第 3 次
        self.assertEqual(len(tgt.calls), 2)

    def test_success_not_retried(self):
        """成功的投递绝不进补投队列。"""
        tgt = _FlakyTarget(fail_times=0)
        svc, _db = make_svc(tgt)
        self.addCleanup(svc.close)
        self.assertTrue(svc.handle(_msg()))
        time.sleep(0.4)
        self.assertEqual(len(tgt.calls), 1)

    def test_retry_covers_uid_dedup_loss(self):
        """复现 17:19 漏发链路：失败后同 uid 再来被去重挡掉，补投兜住。"""
        tgt = _FlakyTarget(fail_times=99)
        svc, _db = make_svc(tgt)
        self.addCleanup(svc.close)
        m = _msg()
        self.assertTrue(svc.handle(m))
        self.assertFalse(svc.handle(m))     # 同 uid 第二次：被去重跳过
        wait_calls(tgt, 2)
        self.assertEqual(len(tgt.calls), 2,
                         "去重挡掉重放，所以失败那次只能靠补投")


if __name__ == "__main__":
    unittest.main()
