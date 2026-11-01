"""积压超龄不转发的回归锁（用户 2026-11-01 要求）。

场景：手机断线 / 卡住一段时间再恢复，采集端会把**当前屏上能看见的历史消息**
一起补上来。这本来是为了不丢消息，但一口气十几条旧消息涌进微信群就是刷屏 ——
群里刚看到 18:34 的晚报，紧接着又冒出 17:05 的旧卡片，还挤掉当下的消息。

规则：源消息时间（``msg_time``）距现在超过 30 分钟 → 只入库留档、不投递。
要补发走 ``forward_recent.py``（那边走 ``force=True``，不受此限）。

三条边界必须同时锁住：
1. 超龄的**不能**发出去，但要**入库**（留档是这条规则存在的前提，
   不然旧消息就彻底消失了，事后连查都没得查）；
2. 正常消息（几秒内）必须照发 —— 这条规则是为了挡积压，不是给日常转发加闸；
3. ``force=True``（人工补发）必须不受限 —— 那时候"发旧消息"正是用户要的。
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


class FakeTarget(Target):
    name = "fake"

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send_text(self, target: str, text: str) -> SendResult:
        self.sent.append((target, text))
        return SendResult(True, target, "fake")


def msg(seq: int, age_min: float | None = 0.0, **kw) -> Message:
    """造一条消息。``age_min`` 是「源消息距今多少分钟」。"""
    base = dict(
        chat_id="c1", chat_name="钉钉群", sender_id="u1", sender_name="张三",
        company="示例科技", content=f"第 {seq} 条", msg_type="text",
        msg_time=None if age_min is None else datetime.now() - timedelta(minutes=age_min),
        raw_id=f"r{seq}",
    )
    base.update(kw)
    return Message(**base)


class TestBacklogAge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(
            storage=type("S", (), {"db_path": str(Path(self.tmp.name) / "s.db")})(),
            wechat=WeChatConfig(dry_run=True, interval=0.0, targets=["测试群"]),
            routes=[RouteConfig(name="t", companies=["示例科技"], targets=["测试群"],
                                template="{content}")],
        )
        self.cfg._base_dir = Path(self.tmp.name)
        self.target = FakeTarget()
        self.svc = BridgeService(self.cfg, target=self.target)

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    # ---------------------------------------------------------------- 拦
    def test_stale_message_is_not_forwarded(self):
        """超过 30 分钟的积压：不许进微信群。"""
        self.svc.handle(msg(1, age_min=MAX_BACKLOG_MIN + 5))
        self.assertEqual(self.target.sent, [])
        self.assertEqual(self.svc.counter["filtered"], 1)

    def test_stale_message_is_still_saved(self):
        """拦住不等于丢掉 —— 必须入库留档，否则事后查都没得查。"""
        self.svc.handle(msg(1, age_min=120))
        self.assertEqual(self.svc.storage.stats()["total_messages"], 1)

    def test_40_minutes_old_is_blocked(self):
        self.svc.handle(msg(1, age_min=40))
        self.assertEqual(len(self.target.sent), 0)

    # ---------------------------------------------------------------- 放
    def test_fresh_message_is_forwarded(self):
        """正常消息（几秒）必须照发，别把日常转发也拦了。"""
        self.svc.handle(msg(1, age_min=0.1))
        self.assertEqual(len(self.target.sent), 1)

    def test_just_under_the_line_is_forwarded(self):
        """29 分钟还在线内 —— 阈值是「超过」，不是「等于」。"""
        self.svc.handle(msg(1, age_min=MAX_BACKLOG_MIN - 1))
        self.assertEqual(len(self.target.sent), 1)

    def test_unknown_time_is_forwarded(self):
        """读不出时间条时宁可发 —— 时间格式一变就把消息全吃掉是最坏结果。"""
        self.svc.handle(msg(1, age_min=None))
        self.assertEqual(len(self.target.sent), 1)

    def test_future_time_is_forwarded(self):
        """手机时钟快了（源消息时间在未来）→ 不算积压，照发。"""
        self.svc.handle(msg(1, age_min=-10))
        self.assertEqual(len(self.target.sent), 1)

    # ---------------------------------------------------------------- force
    def test_force_bypasses_the_gate(self):
        """人工补发的意图就是「把这条旧消息再发一次」，不能被闸门挡住。"""
        self.svc.handle(msg(1, age_min=300), force=True)
        self.assertEqual(len(self.target.sent), 1)

    def test_gate_does_not_touch_dedup(self):
        """闸门与去重是两回事：超龄消息发了 force 之后，自动采集仍按 uid 去重。"""
        self.svc.handle(msg(1, age_min=300), force=True)
        self.assertFalse(self.svc.handle(msg(1, age_min=300)))
        self.assertEqual(len(self.target.sent), 1)


class TestBacklogMinutes(unittest.TestCase):
    """`_backlog_minutes` 的边界：它是整条规则的判据，单独锁一遍。"""

    def test_none_is_zero(self):
        self.assertEqual(BridgeService._backlog_minutes(msg(1, age_min=None)), 0.0)

    def test_future_is_zero(self):
        self.assertEqual(BridgeService._backlog_minutes(msg(1, age_min=-30)), 0.0)

    def test_past_is_positive(self):
        got = BridgeService._backlog_minutes(msg(1, age_min=45))
        self.assertTrue(44.0 < got < 46.0)


if __name__ == "__main__":
    unittest.main()
