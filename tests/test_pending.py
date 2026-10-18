"""``bridge.sources.pending`` 的单元测试。

这一层是「先攒后发」的核心：H5 卡片分批渲染出来，每帧只看到一部分，
必须按 (发送者, 时间窗) 归并、只发最全的那一版。它挡掉的是 2026-10-18
那次真机事故（「拦截跟单日报」「按需派送履约风险提醒」都没转发）。
"""

import time
import unittest

from bridge.fingerprint import norm_sender
from bridge.sources.android_gateway import _join_fragments, _rebuild_message
from bridge.models import Message
from bridge.sources.pending import PendingBuffer


class TestPendingBuffer(unittest.TestCase):

    def test_single_message_waits_for_quiet_then_goes_out(self):
        buf = PendingBuffer(quiet=0.05, max_wait=10, span=10)
        self.assertEqual(buf.offer("张三", "你好", join=_join_fragments), "new")
        self.assertEqual(buf.drain(), [], "刚收到就发了 —— 没攒")
        time.sleep(0.08)
        ready = buf.drain()
        self.assertEqual([e.content for e in ready], ["你好"])

    def test_growing_card_keeps_only_the_fullest(self):
        """卡片一批批长出来：只留最全的那版，不是每批都发一条。"""
        buf = PendingBuffer(quiet=10, max_wait=20, span=30)
        stages = ["拦截跟单日报",
                  "拦截跟单日报 今天共拦截 37 票",
                  "拦截跟单日报 今天共拦截 37 票 明细：马伟豪 17 票"]
        actions = [buf.offer("机器人", s, join=_join_fragments) for s in stages]
        self.assertEqual(actions, ["new", "grow", "grow"])
        self.assertEqual(len(buf), 1, "同一张卡片被拆成了多条")
        self.assertEqual(buf.flush_all()[0].content, stages[-1])

    def test_same_content_again_does_not_extend_or_duplicate(self):
        """重绘后内容没变（只是指纹变了）—— 不能变成第二条。"""
        buf = PendingBuffer(quiet=10, max_wait=20, span=30)
        buf.offer("机器人", "同样的一段话", join=_join_fragments)
        self.assertEqual(buf.offer("机器人", "同样的一段话",
                                   join=_join_fragments), "same")
        self.assertEqual(len(buf), 1)

    def test_shorter_version_never_overwrites_the_longer_one(self):
        """卡片重绘后内容变短了 —— 不能拿短的盖掉已经攒到的长的。"""
        buf = PendingBuffer(quiet=10, max_wait=20, span=30)
        buf.offer("机器人", "第一段 第二段", join=_join_fragments)
        self.assertEqual(buf.offer("机器人", "第一段", join=_join_fragments),
                         "same")
        self.assertEqual(buf.flush_all()[0].content, "第一段 第二段")

    def test_unjoinable_content_becomes_a_separate_message(self):
        """同一发送者连发两条不一样的话 —— 不能硬拼成一条。"""
        buf = PendingBuffer(quiet=10, max_wait=20, span=30)
        buf.offer("张三", "收到", join=_join_fragments)
        self.assertEqual(buf.offer("张三", "另外一件事", join=_join_fragments),
                         "new")
        self.assertEqual(len(buf), 2)

    def test_max_wait_releases_a_card_that_never_stops_growing(self):
        """内容一直在变（病态卡片）也不能永远不发。"""
        buf = PendingBuffer(quiet=100, max_wait=0.05, span=30)
        buf.offer("机器人", "第一段", join=_join_fragments)
        self.assertEqual(buf.drain(), [])
        time.sleep(0.08)
        self.assertEqual([e.content for e in buf.drain()], ["第一段"])

    def test_sender_with_robot_suffix_merges_with_plain_name(self):
        """「签收跟单专员 机器人」与「签收跟单专员」是同一个人 —— 必须归并。"""
        buf = PendingBuffer(quiet=10, max_wait=20, span=30)
        buf.offer("签收跟单专员 机器人", "第一段", join=_join_fragments)
        self.assertEqual(
            buf.offer("签收跟单专员", "第一段 第二段", join=_join_fragments),
            "grow")
        self.assertEqual(len(buf), 1)
        self.assertEqual(norm_sender(buf.flush_all()[0].sender),
                         norm_sender("签收跟单专员"))

    def test_old_entry_outside_span_is_not_merged(self):
        """窗口外的同发送者消息是**另一条**，不能并到上一条上。"""
        buf = PendingBuffer(quiet=10, max_wait=20, span=0.02)
        buf.offer("机器人", "第一段", join=_join_fragments)
        time.sleep(0.05)
        self.assertEqual(buf.offer("机器人", "第一段 第二段",
                                   join=_join_fragments), "new")
        self.assertEqual(len(buf), 2)

    def test_flush_all_empties_the_buffer(self):
        buf = PendingBuffer(quiet=10, max_wait=20, span=30)
        buf.offer("张三", "一", join=_join_fragments)
        buf.offer("李四", "二", join=_join_fragments)
        self.assertEqual(len(buf.flush_all()), 2)
        self.assertEqual(len(buf), 0)

    def test_default_join_is_conservative(self):
        """没传拼接函数时只认包含关系 —— 宁可分开两条，也别乱拼。"""
        self.assertEqual(PendingBuffer._default_join("abc", "abcdef"), "abcdef")
        self.assertEqual(PendingBuffer._default_join("abcdef", "abc"), "abcdef")
        self.assertIsNone(PendingBuffer._default_join("abc", "xyz"))


class TestRebuildMessage(unittest.TestCase):
    """投递时必须用**缓冲里最全的那版**正文，不能拿首次见到的半成品。"""

    def test_content_comes_from_the_buffer_not_the_first_message(self):
        base = Message(source="android", chat_name="数字质控",
                       sender_name="机器人", content="拦截跟单日报",
                       raw_id="abc:1")
        entry = type("E", (), {
            "content": "拦截跟单日报 今天共拦截 37 票",
            "sender": "机器人", "chat_name": "数字质控",
            "extra": {"message": base},
        })()
        msg = _rebuild_message(entry)
        self.assertEqual(msg.content, "拦截跟单日报 今天共拦截 37 票")

    def test_uid_and_time_are_kept_so_dedup_still_works(self):
        base = Message(source="android", chat_name="数字质控",
                       sender_name="机器人", content="半截",
                       raw_id="abc:1")
        entry = type("E", (), {
            "content": "半截 补全的后半截", "sender": "机器人",
            "chat_name": "数字质控", "extra": {"message": base},
        })()
        msg = _rebuild_message(entry)
        self.assertEqual(msg.raw_id, "abc:1",
                         "换了 uid 会让下游 is_duplicate 挡不住重复投递")
        self.assertEqual(msg.uid, base.uid)

    def test_missing_template_returns_none(self):
        entry = type("E", (), {"content": "x", "sender": "a",
                               "chat_name": "", "extra": {}})()
        self.assertIsNone(_rebuild_message(entry))


if __name__ == "__main__":
    unittest.main()
