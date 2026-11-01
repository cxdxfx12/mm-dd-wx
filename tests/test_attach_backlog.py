"""附件也要受「积压超龄只入库不转发」约束的回归锁。

来源：用户 2026-11-01 20:42 ——
    「钉钉里面超过当前时间30分钟以上的消息只入库不转发有没落实进来」

查下来：正文落实了（service.handle 里 MAX_BACKLOG_MIN），**附件这条路是漏的**
—— 附件回到电脑侧时是一条凭 sha1 拼出来的 Message，``msg_time`` 天生为空，
``_backlog_minutes`` 于是返回 0（= 不拦），两小时前那张卡片的明细照样会刷进
微信群。今晚正在补抓的 18:34 晚报明细就属于这一类。

修法：网关下单时记下附件行所在消息的**时间条原文**（``attach_src_ts``），
文件回传时换算成 datetime 交给 ``handle_attachment``，让它跟正文走同一道闸门。

两类豁免（都是刻意的）：
1. 人工 ``/attach-now``：人主动要的补发，等同于 forward_recent 的 force；
2. 时间读不出绝对时刻（「星期一」「昨天」裸的）：这套规则挡陈年积压，
   不做时间校验，宁可漏一条也不能把当下消息全吃掉。
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
from bridge.sources.android_gateway import (                          # noqa: E402
    _Ctx, _order_attach, _take_attach_src_time,
)
from bridge.sources.android_parse import Unit                         # noqa: E402
from bridge.targets.base import SendResult, Target                    # noqa: E402

BIZ = "申通五星业务员培训群"


def ts_ago(minutes: float, now: datetime | None = None) -> str:
    """「今天 HH:MM」形式、距今 minutes 分钟的时间条（向下取整到分钟）。"""
    now = now or datetime.now()
    at = now - timedelta(minutes=minutes)
    if at.date() == now.date():
        return at.strftime("今天 %H:%M")
    return at.strftime("昨天 %H:%M")


class TestTakeSrcTime(unittest.TestCase):
    """网关侧：时间条 → 源消息 datetime。"""

    def ctx(self) -> _Ctx:
        return _Ctx(token="t", chat_name="群")

    def test_old_message_gives_old_datetime(self):
        c = self.ctx()
        c.attach_src_ts["明细附件：运单明细.xlsx"] = ts_ago(45)
        got = _take_attach_src_time(c, "明细附件：运单明细.xlsx")
        self.assertIsNotNone(got)
        age = (datetime.now() - got).total_seconds() / 60
        self.assertAlmostEqual(age, 45, delta=1.5)
        self.assertGreater(age, MAX_BACKLOG_MIN)   # 会被闸门拦住

    def test_fresh_message_stays_under_limit(self):
        c = self.ctx()
        c.attach_src_ts["附件：当日明细.xlsx"] = ts_ago(2)
        got = _take_attach_src_time(c, "附件：当日明细.xlsx")
        self.assertIsNotNone(got)
        self.assertLess((datetime.now() - got).total_seconds() / 60,
                        MAX_BACKLOG_MIN)

    def test_manual_fetch_is_exempt(self):
        """人工 /attach-now 补抓的，不受积压闸门限制。"""
        c = self.ctx()
        c.attach_src_ts["查看完整履约失败明细（Excel）"] = ts_ago(120)
        c.attach_manual.add("查看完整履约失败明细（Excel）")
        self.assertIsNone(_take_attach_src_time(c, "查看完整履约失败明细（Excel）"))
        # 豁免是一次性的，不能漏到同名的下一条自动单上
        self.assertFalse(c.attach_manual)

    def test_unknown_clock_and_weekday_are_not_blocked(self):
        """读不出绝对时刻 → 不拦（挡积压用，不做时间校验）。"""
        for ts in ("星期一", "昨天", "10-16", ""):
            c = self.ctx()
            c.attach_src_ts["附件：x.xlsx"] = ts
            self.assertIsNone(_take_attach_src_time(c, "附件：x.xlsx"), ts)

    def test_keys_are_cleared_even_when_not_found(self):
        """取过就清：同名附件明天再出现时不能被今天的时间条误判成「一天前」。"""
        c = self.ctx()
        c.attach_src_ts["A"] = ts_ago(10)
        c.attach_src_ts["B"] = ts_ago(10)
        _take_attach_src_time(c, "不存在的名字", "A")
        self.assertNotIn("A", c.attach_src_ts)
        self.assertEqual(_take_attach_src_time(c, "A"), None)   # 残留已清
        self.assertIn("B", c.attach_src_ts)                     # 没点的别动


class TestOrderAttachRecordsTs(unittest.TestCase):
    """下单那一刻就要把时间条记下来，否则文件回来时无从判断。"""

    def test_queue_records_source_ts(self):
        c = _Ctx(token="t", chat_name="群")
        u = Unit(sender="机器人", content="附件：运单明细.xlsx", ts=ts_ago(50))
        name = _order_attach(c, [u])
        # 网关会把「附件：」前缀剥掉，名字以它给的为准
        self.assertTrue(name)
        self.assertEqual(c.attach_src_ts.get(name), u.ts)
        self.assertIsNotNone(_take_attach_src_time(c, name))

    def test_rerender_keeps_first_ts(self):
        """卡片重渲染几十次，第一次看到的时间条才最接近真实时刻。"""
        c = _Ctx(token="t", chat_name="群")
        first = ts_ago(50)
        u1 = Unit(sender="机器人", content="附件：运单明细.xlsx", ts=first)
        name = _order_attach(c, [u1])
        # 第二帧时它已经在抓/冷却中，不再入队 —— 正是我们要的「保第一次」
        u2 = Unit(sender="机器人", content="附件：运单明细.xlsx", ts=ts_ago(1))
        _order_attach(c, [u2])
        self.assertEqual(c.attach_src_ts[name], first)


class FakeTarget(Target):
    name = "fake"

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send_text(self, target: str, text: str) -> SendResult:
        self.sent.append((target, text))
        return SendResult(True, target, "fake")

    def send_file(self, target: str, path: str) -> SendResult:
        # 附件按 media_path 投递；真实目标的实现细节与本组测试无关
        self.sent.append((target, path))
        return SendResult(True, target, "fake")


class TestAttachmentGate(unittest.TestCase):
    """端到端：handle_attachment 带上源时间后，闸门要真的生效。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Config(
            storage=type("S", (), {"db_path": str(Path(self.tmp.name) / "s.db")})(),
            wechat=WeChatConfig(dry_run=True, interval=0.0, targets=[BIZ]),
            routes=[RouteConfig(name="t", companies=[], targets=[BIZ],
                                template="{content}")],
        )
        cfg._base_dir = Path(self.tmp.name)
        self.target = FakeTarget()
        self.svc = BridgeService(cfg, target=self.target)
        self.file = Path(self.tmp.name) / "明细.xlsx"
        self.file.write_bytes(b"fake-xlsx-bytes")
        self.caught: list[Message] = []

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def run_attach(self, msg_time, name="明细附件：运单明细.xlsx") -> bool:
        """走真实 handle_attachment，只把最后一步的 handle 拦下来看 Message。"""
        self.svc.handle = lambda m, force=False: self.caught.append(m) or True
        return self.svc.handle_attachment(
            str(self.file), name, "钉钉群", name, msg_time)

    def test_stale_attachment_is_recorded_but_not_forwarded(self):
        old = datetime.now() - timedelta(minutes=85)
        self.assertTrue(self.run_attach(old))
        self.assertEqual(self.caught[0].msg_time, old)
        # 直接交给真实 handle：应当只入库、不投递
        self.svc.handle = BridgeService.handle.__get__(self.svc, BridgeService)
        self.svc.handle(self.caught[0])
        self.assertEqual(self.target.sent, [])
        self.assertEqual(self.svc.counter["filtered"], 1)

    def test_fresh_attachment_is_forwarded(self):
        new = datetime.now() - timedelta(minutes=2)
        self.assertTrue(self.run_attach(new, name="明细附件：新单.xlsx"))
        self.svc.handle = BridgeService.handle.__get__(self.svc, BridgeService)
        self.svc.handle(self.caught[0])
        # 附件是「一条提示文字 + 一个文件本体」，两个都发到业务群
        self.assertTrue(self.target.sent)
        self.assertEqual({t for t, _ in self.target.sent}, {BIZ})

    def test_missing_time_still_forwards(self):
        """判断不出来（人工补抓 / 时间条读不出）照常转发，不做保守拦截。"""
        self.assertTrue(self.run_attach(None, name="明细附件：手工单.xlsx"))
        self.svc.handle = BridgeService.handle.__get__(self.svc, BridgeService)
        self.svc.handle(self.caught[0])
        self.assertTrue(self.target.sent)
        self.assertEqual({t for t, _ in self.target.sent}, {BIZ})


if __name__ == "__main__":
    unittest.main()
