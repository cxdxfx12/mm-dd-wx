"""「多久以前的消息不再转发」的配置旋钮（2026-11-05 用户要求搬进设置中心）。

原来阈值是 ``bridge/service.MAX_BACKLOG_MIN`` 一个常量（30 分钟），改一次要
动代码、重打安装包。现在落到 ``wechat.max_backlog_min``，设置中心 → 时间范围
里选档。这里锁四件事：

1. 档位文字 ↔ 分钟数互转，0 表示「不限制」；
2. 配置里写了个非标准值（比如 45）时，界面**原样显示**，绝不悄悄改成 30；
3. 服务端按配置生效：60 分钟的阈值不能把 45 分钟前的消息拦掉；
4. 0 = 关闭闸门；配置写坏（None / "abc"）回落到默认 30，闸门不能静默失效。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bridge.config import Config, RouteConfig, WeChatConfig          # noqa: E402
from bridge.models import Message                                     # noqa: E402
from bridge.service import BridgeService, MAX_BACKLOG_MIN             # noqa: E402
from bridge.targets.base import SendResult, Target                    # noqa: E402

sys.path.insert(0, str(ROOT / "app"))
from settings_ui import (                                             # noqa: E402
    BACKLOG_CHOICES, BACKLOG_LABELS, DEFAULT_BACKLOG_MIN,
    _backlog_label, backlog_minutes,
)


class FakeTarget(Target):
    name = "fake"

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send_text(self, target: str, text: str) -> SendResult:
        self.sent.append((target, text))
        return SendResult(True, target, "fake")


class TestLabels(unittest.TestCase):
    def test_round_trip_all_choices(self):
        for v in BACKLOG_CHOICES:
            label = BACKLOG_LABELS[v]
            self.assertEqual(_backlog_label(v), label)
            self.assertEqual(backlog_minutes(label), float(v))

    def test_default_is_30(self):
        self.assertEqual(DEFAULT_BACKLOG_MIN, 30)
        self.assertEqual(_backlog_label(MAX_BACKLOG_MIN), BACKLOG_LABELS[30])

    def test_zero_means_unlimited(self):
        self.assertEqual(_backlog_label(0), BACKLOG_LABELS[0])
        self.assertEqual(backlog_minutes(BACKLOG_LABELS[0]), 0.0)

    def test_custom_value_shown_as_is(self):
        """配置里是 45 → 界面显示「45 分钟」，不许悄悄改成 30。"""
        self.assertEqual(_backlog_label(45), "45 分钟")
        self.assertEqual(backlog_minutes("45 分钟"), 45.0)

    def test_garbage_falls_back_to_default(self):
        for bad in (None, "", "abc", []):
            self.assertEqual(_backlog_label(bad), BACKLOG_LABELS[30])
        self.assertEqual(backlog_minutes("乱写"), 30.0)


class TestServiceHonoursConfig(unittest.TestCase):
    def _svc(self, minutes, tmp) -> tuple[BridgeService, FakeTarget]:
        cfg = Config(
            storage=type("S", (), {"db_path": str(Path(tmp) / "s.db")})(),
            wechat=WeChatConfig(dry_run=True, interval=0.0, targets=["测试群"],
                                max_backlog_min=minutes),
            routes=[RouteConfig(name="t", companies=[], targets=["测试群"],
                                template="{content}")],
        )
        cfg._base_dir = Path(tmp)
        tgt = FakeTarget()
        return BridgeService(cfg, target=tgt), tgt

    def _msg(self, age_min: float) -> Message:
        return Message(chat_name="钉钉群", sender_name="张三", content="x",
                       msg_type="text", raw_id=f"r{age_min}",
                       msg_time=datetime.now() - timedelta(minutes=age_min))

    def test_sixty_minutes_allows_forty_five(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, tgt = self._svc(60, tmp)
            try:
                svc.handle(self._msg(45))
                self.assertEqual(len(tgt.sent), 1)
            finally:
                svc.close()

    def test_thirty_minutes_blocks_forty_five(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, tgt = self._svc(30, tmp)
            try:
                svc.handle(self._msg(45))
                self.assertEqual(tgt.sent, [])
            finally:
                svc.close()

    def test_zero_disables_the_gate(self):
        """0 = 不限制：三小时前的也照发（用户明确关掉闸门时）。"""
        with tempfile.TemporaryDirectory() as tmp:
            svc, tgt = self._svc(0, tmp)
            try:
                svc.handle(self._msg(180))
                self.assertEqual(len(tgt.sent), 1)
            finally:
                svc.close()

    def test_broken_value_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, tgt = self._svc("abc", tmp)
            try:
                self.assertEqual(svc.backlog_limit(), MAX_BACKLOG_MIN)
                svc.handle(self._msg(45))
                self.assertEqual(tgt.sent, [])
            finally:
                svc.close()


if __name__ == "__main__":
    unittest.main()
