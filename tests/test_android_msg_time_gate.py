"""画面解析的时间条 → ``msg_time`` → 「30 分钟超龄不转发」闭环（2026-11-05）。

现场：翻历史 / 重建基线的那一帧把**昨天的卡片**转进了业务群，日志里没有任何
「积压超龄不转发」的记录。原因是 ``units_to_messages`` 把每条消息的
``msg_time`` 一律写成 ``now`` —— 手机画面这条路没有真实时刻，下游
``BridgeService.handle`` 的闸门（``msg_time`` 距现在超过 30 分钟就不投递）
于是永远判成「刚发的」，等于被焊死。

修法：时间条（``Unit.ts``，钉钉画的「今天 11:52」「昨天 20:01」）本来就跟着
每条消息一起解析出来了，让它落到 ``msg_time`` 上即可。这里锁住四件事：

1. 「今天 HH:MM」/「昨天 HH:MM」能变成正确的绝对时刻；
2. 读不出时刻（裸「今天」、「星期一」、「10-16」）退回 ``now`` —— 宁可多发
   一条，也不能因为时间条格式一变就把当下的消息全吃掉；
3. 昨天的消息**不能**进微信群（端到端过一遍 ``BridgeService``）；
4. 今天的正常消息照发 —— 这是闸门，不是给日常转发加的限制。
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
from bridge.service import BridgeService, MAX_BACKLOG_MIN            # noqa: E402
from bridge.sources.android_parse import (                            # noqa: E402
    Unit, ts_to_datetime, units_to_messages,
)
from bridge.targets.base import SendResult, Target                     # noqa: E402

NOW = datetime(2026, 11, 5, 13, 30, 0)


class FakeTarget(Target):
    name = "fake"

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send_text(self, target: str, text: str) -> SendResult:
        self.sent.append((target, text))
        return SendResult(True, target, "fake")


def unit(text: str, ts: str = "", sender: str = "签收跟单专员") -> Unit:
    return Unit(sender=sender, content=text, y0=0, y1=100, ts=ts)


class TestTsToDatetime(unittest.TestCase):
    def test_today(self):
        self.assertEqual(ts_to_datetime("今天 11:52", NOW),
                         datetime(2026, 11, 5, 11, 52))

    def test_yesterday(self):
        self.assertEqual(ts_to_datetime("昨天 20:01", NOW),
                         datetime(2026, 11, 4, 20, 1))

    def test_day_before(self):
        self.assertEqual(ts_to_datetime("前天 08:05", NOW),
                         datetime(2026, 11, 3, 8, 5))

    def test_unreadable_is_none(self):
        for bad in ("", "今天", "星期一", "10-16", "2026-10-16", "昨天"):
            self.assertIsNone(ts_to_datetime(bad, NOW), bad)

    def test_weekday(self):
        """「星期三 09:15」= 最近一个星期三（NOW 是周四 → 昨天）。"""
        got = ts_to_datetime("星期三 09:15", NOW)
        self.assertEqual(got, datetime(2026, 11, 4, 9, 15))
        self.assertEqual(ts_to_datetime("周三 09:15", NOW), got)

    def test_weekday_today_but_future_means_last_week(self):
        """同一天但时刻还没到 → 说的是上周那一天，不是今天未来。"""
        got = ts_to_datetime("星期四 14:00", NOW)      # NOW 是周四 13:30
        self.assertEqual(got, datetime(2026, 10, 29, 14, 0))

    def test_month_day(self):
        self.assertEqual(ts_to_datetime("10-16 14:30", NOW),
                         datetime(2026, 10, 16, 14, 30))

    def test_month_day_in_future_means_last_year(self):
        self.assertEqual(ts_to_datetime("12-25 08:00", NOW),
                         datetime(2025, 12, 25, 8, 0))

    def test_full_date(self):
        self.assertEqual(ts_to_datetime("2026-10-16 14:30", NOW),
                         datetime(2026, 10, 16, 14, 30))


class TestMsgTimeFromTimeBar(unittest.TestCase):
    def test_today_bar_becomes_msg_time(self):
        m = units_to_messages([unit("正常消息", "今天 13:25")], chat_name="群",
                              now=NOW)[0]
        self.assertEqual(m.msg_time, datetime(2026, 11, 5, 13, 25))

    def test_yesterday_bar_becomes_msg_time(self):
        m = units_to_messages([unit("昨天的卡片", "昨天 20:01")], chat_name="群",
                              now=NOW)[0]
        self.assertEqual(m.msg_time, datetime(2026, 11, 4, 20, 1))

    def test_missing_bar_falls_back_to_now(self):
        """时间条读不出绝对时刻 → 按本机时间，照发（不许把当下消息吃掉）。"""
        m = units_to_messages([unit("刚进来的", "")], chat_name="群", now=NOW)[0]
        self.assertEqual(m.msg_time, NOW)

    def test_top_unit_inherits_the_bar_below(self):
        """屏幕上最上面那条没有时间条：用下面第一条当**上界**（真实只会更早）。"""
        msgs = units_to_messages(
            [unit("顶部那条", ""), unit("下面那条", "昨天 20:01")],
            chat_name="群", now=NOW)
        self.assertEqual(msgs[0].msg_time, datetime(2026, 11, 4, 20, 1))
        self.assertEqual(msgs[1].msg_time, datetime(2026, 11, 4, 20, 1))


class TestGateOnScreenPath(unittest.TestCase):
    """端到端：画面解析出来的 Unit 过 ``BridgeService`` 时真的会被闸住。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(
            storage=type("S", (), {"db_path": str(Path(self.tmp.name) / "s.db")})(),
            wechat=WeChatConfig(dry_run=True, interval=0.0, targets=["测试群"]),
            routes=[RouteConfig(name="t", companies=[], targets=["测试群"],
                                template="{content}")],
        )
        self.cfg._base_dir = Path(self.tmp.name)
        self.target = FakeTarget()
        self.svc = BridgeService(self.cfg, target=self.target)

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def _feed(self, text: str, ts: str) -> None:
        for m in units_to_messages([unit(text, ts)], chat_name="钉钉群"):
            self.svc.handle(m)

    def test_yesterday_card_is_not_forwarded(self):
        self._feed("昨天的积压提醒", "昨天 20:01")
        self.assertEqual(self.target.sent, [])
        self.assertEqual(self.svc.counter["filtered"], 1)

    def test_just_over_the_line_is_not_forwarded(self):
        now = datetime.now()
        ts = (now - timedelta(minutes=MAX_BACKLOG_MIN + 2)).strftime("今天 %H:%M")
        self._feed("刚过线", ts)
        self.assertEqual(self.target.sent, [])

    def test_fresh_card_is_forwarded(self):
        ts = (datetime.now() - timedelta(minutes=2)).strftime("今天 %H:%M")
        self._feed("两分钟前的", ts)
        self.assertEqual(len(self.target.sent), 1)

    def test_unreadable_bar_still_forwards(self):
        self._feed("星期一那批", "星期一")
        self.assertEqual(len(self.target.sent), 1)

    def test_weekday_card_is_not_forwarded(self):
        """「星期三 09:15」这种一周内的旧卡片同样要被拦。"""
        self._feed("上周的卡片", "星期三 09:15")
        self.assertEqual(self.target.sent, [])

    def test_scroll_back_top_unit_is_not_forwarded(self):
        """翻上历史：顶部那条没有时间条，下面那条是昨天的 → 两条都不许发。"""
        for m in units_to_messages(
                [unit("更早的一条", ""), unit("昨天的卡片", "昨天 20:01")],
                chat_name="钉钉群"):
            self.svc.handle(m)
        self.assertEqual(self.target.sent, [])


if __name__ == "__main__":
    unittest.main()
