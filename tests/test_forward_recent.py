"""「手动转发最近 N 条」的回归锁。

这个功能由三块新能力拼起来，每块单独锁一遍：

1. `Storage.recent()` —— 补发取数。三条约束：按**发生顺序**（旧 → 新）返回、
   `only_failed` 只挑「最近一次投递失败」的、能按会话名过滤。
   顺序错了群里的人会倒着读；`only_failed` 判成 `EXISTS(fail)` 的话，
   已经重发成功的消息每点一次都会被再发一遍。
2. `row_to_message()` —— 库行还原成 Message。`uid` 必须与当初入库时完全一致，
   否则补发的投递记录会挂到另一条「新消息」上，事后对不上账。
3. `BridgeService.handle(force=True)` —— 人工触发的转发必须绕过去重。
   否则用户点了「发送」，得到的是「重复，跳过」，等于什么都没干。
   但**不传 force 时去重必须照旧生效**（自动采集全靠它）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bridge.config import Config, RouteConfig, WeChatConfig          # noqa: E402
from bridge.models import Message                                     # noqa: E402
from bridge.service import BridgeService                              # noqa: E402
from bridge.storage import Storage, row_to_message                    # noqa: E402
from bridge.targets.base import SendResult, Target                    # noqa: E402
from forward_recent import units_to_messages                          # noqa: E402


class FakeTarget(Target):
    name = "fake"

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send_text(self, target: str, text: str) -> SendResult:
        self.sent.append((target, text))
        return SendResult(True, target, "fake")


def msg(seq: int, **kw) -> Message:
    base = dict(
        chat_id="c1", chat_name="钉钉群", sender_id="u1", sender_name="张三",
        company="示例科技", content=f"第 {seq} 条", msg_type="text",
        msg_time=datetime(2026, 10, 16, 9, 0) + timedelta(minutes=seq),
        raw_id=f"r{seq}",
    )
    base.update(kw)
    return Message(**base)


class TestRecentQuery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Storage(Path(self.tmp.name) / "t.db")
        for i in range(1, 6):
            self.st.save_message(msg(i))

    def tearDown(self):
        self.st.close()
        self.tmp.cleanup()

    def test_returns_in_chronological_order(self):
        """补发必须按当初发生的顺序发 —— 倒着发群里读起来是乱的。"""
        rows = self.st.recent(3)
        self.assertEqual([r["content"] for r in rows],
                         ["第 3 条", "第 4 条", "第 5 条"])

    def test_respects_limit(self):
        self.assertEqual(len(self.st.recent(2)), 2)
        self.assertEqual(len(self.st.recent(50)), 5)

    def test_takes_the_newest_not_the_oldest(self):
        rows = self.st.recent(2)
        self.assertEqual(rows[-1]["content"], "第 5 条")

    def test_chat_name_filter(self):
        self.st.save_message(msg(99, chat_name="别的群", raw_id="r99"))
        rows = self.st.recent(10, chat_name="别的群")
        self.assertEqual([r["content"] for r in rows], ["第 99 条"])

    def test_only_failed_picks_the_failed_one(self):
        self.st.log_delivery("dingtalk:r2", "测试群", "ok", "")
        self.st.log_delivery("dingtalk:r4", "测试群", "fail", "窗口不可用")
        rows = self.st.recent(10, only_failed=True)
        self.assertEqual([r["content"] for r in rows], ["第 4 条"])

    def test_only_failed_ignores_already_retried_success(self):
        """失败过但后来重发成功的，不该再算成待补发 —— 否则每点一次重发一遍。"""
        self.st.log_delivery("dingtalk:r4", "测试群", "fail", "窗口不可用")
        self.st.log_delivery("dingtalk:r4", "测试群", "ok", "已发送")
        self.assertEqual(self.st.recent(10, only_failed=True), [])

    def test_only_failed_empty_when_all_ok(self):
        for i in range(1, 6):
            self.st.log_delivery(f"dingtalk:r{i}", "测试群", "ok", "")
        self.assertEqual(self.st.recent(10, only_failed=True), [])

    def test_never_delivered_is_not_failed(self):
        """从未投递过的消息（只是入库）不算「投递失败」，不该被当成补发目标。"""
        self.assertEqual(self.st.recent(10, only_failed=True), [])


class TestRowToMessage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Storage(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.st.close()
        self.tmp.cleanup()

    def test_uid_roundtrip(self):
        """uid 必须原样还原，补发的投递记录才挂得回同一条消息。"""
        original = msg(1)
        self.st.save_message(original)
        restored = row_to_message(self.st.recent(1)[0])
        self.assertEqual(restored.uid, original.uid)

    def test_fields_roundtrip(self):
        original = msg(2, company="示例科技", sender_name="李四", msg_type="text")
        self.st.save_message(original)
        restored = row_to_message(self.st.recent(1)[0])
        self.assertEqual(restored.sender_name, "李四")
        self.assertEqual(restored.company, "示例科技")
        self.assertEqual(restored.content, "第 2 条")
        self.assertEqual(restored.chat_name, "钉钉群")
        self.assertEqual(restored.msg_time, original.msg_time)

    def test_time_is_parsed_tolerantly(self):
        """时间解不出来也不能炸 —— 内容照发，只是渲染时少个时间占位。"""
        self.st.save_message(msg(3))
        with self.st._lock:
            self.st._conn.execute("UPDATE messages SET msg_time = ? WHERE uid = ?",
                                  ("不是时间", "dingtalk:r3"))
            self.st._conn.commit()
        restored = row_to_message(self.st.recent(1)[0])
        self.assertIsNone(restored.msg_time)
        self.assertEqual(restored.content, "第 3 条")


class TestHandleForce(unittest.TestCase):
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

    def test_without_force_still_dedups(self):
        """自动采集这条路不能被改动波及 —— 去重照旧。"""
        self.svc.handle(msg(1))
        self.assertFalse(self.svc.handle(msg(1)))
        self.assertEqual(len(self.target.sent), 1)

    def test_force_sends_again(self):
        """手动补发：同一条再发一次，必须真的发出去。"""
        self.svc.handle(msg(1))
        self.svc.handle(msg(1), force=True)
        self.assertEqual(len(self.target.sent), 2)
        self.assertEqual(self.svc.counter["duplicate"], 0)

    def test_force_does_not_duplicate_the_row(self):
        """force 只跳过去重，不改变入库的幂等性。"""
        self.svc.handle(msg(1), force=True)
        self.svc.handle(msg(1), force=True)
        self.assertEqual(self.svc.storage.stats()["total_messages"], 1)

    def test_force_records_every_delivery(self):
        """补发过的事实要留痕，事后才对得上账。"""
        self.svc.handle(msg(1), force=True)
        self.svc.handle(msg(1), force=True)
        self.assertEqual(self.svc.storage.stats()["deliveries"].get("ok"), 2)

    def test_force_still_respects_routing(self):
        """force 绕过的是去重，不是路由 —— 不命中规则照样不发。"""
        self.svc.handle(msg(1, company="别家公司"), force=True)
        self.assertEqual(len(self.target.sent), 0)
        self.assertEqual(self.svc.counter["filtered"], 1)


class TestUnitsToMessages(unittest.TestCase):
    """屏幕单元 → Message。顺序与发送者是最容易搞错的两处。"""

    def setUp(self):
        self.cfg = SimpleNamespace(dingtalk=SimpleNamespace(companies=[]))

    def test_order_preserved(self):
        units = [
            {"sender": "甲", "content": "第一条", "is_self": False,
             "time": "09:10", "y0": 10, "y1": 30},
            {"sender": "乙", "content": "第二条", "is_self": False,
             "time": "09:11", "y0": 40, "y1": 60},
        ]
        out = units_to_messages(units, self.cfg, "钉钉群")
        self.assertEqual([m.content for m in out], ["第一条", "第二条"])

    def test_time_taken_from_clock(self):
        units = [{"sender": "甲", "content": "x", "is_self": False,
                  "time": "09:10", "y0": 10, "y1": 30}]
        out = units_to_messages(units, self.cfg, "钉钉群")
        self.assertEqual((out[0].msg_time.hour, out[0].msg_time.minute), (9, 10))

    def test_unparsable_time_falls_back_to_now(self):
        units = [{"sender": "甲", "content": "x", "is_self": False,
                  "time": "不是时间", "y0": 10, "y1": 30}]
        out = units_to_messages(units, self.cfg, "钉钉群")
        self.assertIsNotNone(out[0].msg_time)

    def test_self_message_marked(self):
        units = [{"sender": "", "content": "我发的", "is_self": True,
                  "time": "", "y0": 10, "y1": 30}]
        out = units_to_messages(units, self.cfg, "钉钉群")
        self.assertEqual(out[0].sender_name, "(我)")

    def test_uid_matches_ocr_source_scheme(self):
        """与 `DingTalkOCRSource._emit` 同一套指纹，两条路径不会各写一条 uid。"""
        from bridge.sources.dingtalk_ocr import unit_fp
        u = {"sender": "签收跟单专员 机器人", "content": "@所有人 播报",
             "is_self": False, "time": "09:10", "y0": 10, "y1": 30}
        out = units_to_messages([u], self.cfg, "钉钉群")
        self.assertEqual(out[0].uid, f"dingtalk:ocr:{unit_fp(u)}:1")

    def test_same_content_twice_gets_distinct_uids(self):
        """同屏两条一模一样的消息是两条消息，不能因为正文相同就合成一条。"""
        u = {"sender": "甲", "content": "同上", "is_self": False,
             "time": "", "y0": 10, "y1": 30}
        out = units_to_messages([u, dict(u, y0=40, y1=60)], self.cfg, "钉钉群")
        self.assertNotEqual(out[0].uid, out[1].uid)

    def test_sender_suffix_jitter_does_not_split_uids(self):
        """机器人昵称后缀抖动（有时读得出「机器人」）不该产生两条消息。"""
        a = {"sender": "签收跟单专员", "content": "播报", "is_self": False,
             "time": "", "y0": 10, "y1": 30}
        b = dict(a, sender="签收跟单专员 机器人")
        out = units_to_messages([a, b], self.cfg, "钉钉群")
        # 指纹一致 → 靠序号区分，而不是被当成互不相关的两条
        self.assertEqual(out[0].uid.rsplit(":", 1)[0],
                         out[1].uid.rsplit(":", 1)[0])
        self.assertEqual((out[0].uid.rsplit(":", 1)[1],
                          out[1].uid.rsplit(":", 1)[1]), ("1", "2"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
