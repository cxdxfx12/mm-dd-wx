"""静默期后重建基线的「打捞」（_salvage_rebuild）与试探状态过期（ALIGN_STALE）。

现场（真机 2026-10-26 14:16→14:58，用户原话「一下推过来几条消息还是会漏掉，
刚才是我手动翻页才发到微信的」）：

    14:16:37 锚点丢失，试探额度一路用到第 6 次
    14:16:45 屏幕没有再变化 —— 先等手机翻完再判（最多 20 秒）
    （……42 分钟静默：灭屏 / 用户拿走手机，一帧都没来……）
    14:58:33 一轮翻了 6 次都没找回锚点 —— 重建基线
    （静默期里爆发的消息此刻正在屏上，被基线重建整波吞掉；
      用户手动翻页走补全通道才把它们捞回来）

两个独立缺陷：

  ① 试探状态**没有保质期**。挂了 42 分钟的旧额度在苏醒后的第一帧上瞬间
     判 give_up —— 连一次翻页救援都没有。修：ALIGN_STALE 过期直接 give_up
     （锚点早已远在好几屏之外，翻了也翻不回），但**允许打捞**。
  ② 重建基线 = 把屏上一切登记为「已知历史」，静默期里来的真新消息连同行
     一起被吞。修：只在**过期触发**的重建上打捞「证据之下、没投递过」的
     单元。活跃浏览（翻历史 / 换群）触发的重建绝不能捞 —— 把别的群的消息
     打进目标群是事故。

跑法：``python -m unittest tests.test_android_salvage_rebuild -v``
"""

import threading
import time
import unittest

from bridge.sources import android_gateway as G
from bridge.sources.android_gateway import _Ctx, _Handler, _deliver_ready
from bridge.sources.android_parse import Unit
from bridge.models import Message


def make_handler():
    """不跑 BaseHTTPRequestHandler.__init__，只挂上 ctx 用纯逻辑方法。"""
    h = object.__new__(_Handler)
    h.ctx = _Ctx(token="t", chat_name="数字质控")
    return h


def unit(sender, content):
    return Unit(sender=sender, content=content)


def frame_of(units):
    return type("F", (), {"units": units})()


class TestStaleGiveUp(unittest.TestCase):
    """试探状态过期 → 不再翻页、直接 give_up，并允许打捞。"""

    def test_stale_state_gives_up_without_probing(self):
        """挂了 42 分钟的旧额度不能带到现在用（缺陷 ① 的立身之本）。"""
        h = make_handler()
        h.ctx.align_at = time.monotonic() - 2520.0     # 42 分钟
        h.ctx.align_md5 = "AAA"
        h.ctx.align_tried = 3
        self.assertEqual(h._probe_decision("BBB"), "give_up")
        self.assertTrue(h.ctx.align_stale_gave_up, "过期触发的 give_up 才许打捞")
        self.assertEqual(h.ctx.align_tried, 0, "状态要清掉")

    def test_budget_exhausted_is_not_stale(self):
        """活跃翻满额度触发的 give_up **不许**打捞（翻历史保护）。"""
        h = make_handler()
        h.ctx.align_at = time.monotonic() - 5.0
        h.ctx.align_md5 = "AAA"
        h.ctx.align_tried = G.PROBE_MAX
        self.assertEqual(h._probe_decision("BBB"), "give_up")
        self.assertFalse(h.ctx.align_stale_gave_up)

    def test_wait_timeout_is_not_stale(self):
        """等超时（手机卡死兜底）触发的 give_up 同样不许打捞。"""
        h = make_handler()
        h.ctx.align_at = time.monotonic() - 0.3
        h.ctx.align_md5 = "AAA"
        h.ctx.align_wait_since = time.monotonic() - (G.PROBE_SETTLE_MAX + 1)
        self.assertEqual(h._probe_decision("BBB"), "give_up")
        self.assertFalse(h.ctx.align_stale_gave_up)

    def test_never_probed_is_not_stale(self):
        """align_at=0（从没试探过）不能被当成「远古状态」误判过期。"""
        h = make_handler()
        h.ctx.align_at = 0.0
        h.ctx.align_md5 = ""
        self.assertEqual(h._probe_decision("BBB"), "probe")
        self.assertEqual(h.ctx.align_tried, 1)

    def test_stale_window_longer_than_one_probe_episode(self):
        """保质期必须盖过一整轮试探的极限时长（6 次 × ~2s + 等待 20s）。"""
        self.assertGreater(G.ALIGN_STALE,
                           G.PROBE_MAX * 2.0 + G.PROBE_SETTLE_MAX)
        self.assertLess(G.ALIGN_STALE, 600.0)


class TestSalvage(unittest.TestCase):
    """打捞判据：证据之下、没投过、认不出会话宁可不捞。"""

    def setUp(self):
        self.h = make_handler()
        self.good = [unit("签收跟单专员", "旧消息甲"),
                     unit("签收跟单专员", "旧消息乙"),
                     unit("拦截跟单专员", "旧消息丙")]
        self.h.ctx.last_good_units = list(self.good)

    def test_burst_below_overlap_is_salvaged(self):
        """静默期里爆发的消息在旧内容之下 —— 捞出来。"""
        f = frame_of(self.good + [unit("履约跟单专员", "新消息一"),
                                  unit("履约跟单专员", "新消息二")])
        out = self.h._salvage_rebuild(f)
        self.assertEqual([u.content for u in out], ["新消息一", "新消息二"])

    def test_evidence_via_delivered_text_when_fp_changed(self):
        """H5 卡片重渲染换指纹时，靠已投递记录认出「还是同一个会话」。"""
        self.h.ctx.delivered_texts.extend(
            [("签收跟单专员", u.content) for u in self.good])
        # 重渲染后 fp 全变，但正文不变
        rendered = [unit("签收跟单专员", "旧消息甲（重排后）"),
                    unit("签收跟单专员", "旧消息乙")]
        rendered[0].content = "旧消息甲"
        rendered[1].content = "旧消息乙"
        f = frame_of(rendered + [unit("履约跟单专员", "新消息一")])
        out = self.h._salvage_rebuild(f)
        self.assertEqual([u.content for u in out], ["新消息一"])

    def test_no_evidence_means_no_salvage(self):
        """换群 / 深翻远史：屏上没有一条认识的 —— 宁可不捞。"""
        f = frame_of([unit("别群的张三", "压根没见过的内容甲"),
                      unit("别群的李四", "压根没见过的内容乙")])
        self.assertEqual(self.h._salvage_rebuild(f), [])

    def test_history_above_evidence_is_never_taken(self):
        """往上翻出的旧内容在证据**之上**，不许碰。"""
        hist = [unit("签收跟单专员", "更早的历史一"),
                unit("签收跟单专员", "更早的历史二")]
        f = frame_of(hist + self.good)
        self.assertEqual(self.h._salvage_rebuild(f), [])

    def test_already_delivered_below_is_skipped(self):
        """证据之下但已经投过的（换渲染形态同文）跳过，不重发。"""
        self.h.ctx.delivered_texts.append(("履约跟单专员", "已发过的消息"))
        f = frame_of(self.good + [unit("履约跟单专员", "已发过的消息"),
                                  unit("履约跟单专员", "真新消息")])
        out = self.h._salvage_rebuild(f)
        self.assertEqual([u.content for u in out], ["真新消息"])

    def test_salvage_is_capped(self):
        """一次最多捞 SALVAGE_MAX_UNITS 条，限制爆炸半径。"""
        burst = [unit("履约跟单专员", "爆发 %02d" % i) for i in range(20)]
        f = frame_of(self.good + burst)
        out = self.h._salvage_rebuild(f)
        self.assertEqual(len(out), G.SALVAGE_MAX_UNITS)
        self.assertEqual(out[-1].content, "爆发 19")

    def test_empty_frame_is_noop(self):
        self.assertEqual(self.h._salvage_rebuild(frame_of([])), [])


class TestDeliveredMemory(unittest.TestCase):
    """已投递记录是打捞的去重依据，投递点必须记一笔。"""

    def test_deliver_ready_records_content(self):
        h = make_handler()
        h.ctx.on_message = lambda m: None
        h.ctx.lock = threading.Lock()
        h.ctx.count = {"accepted": 0, "rejected": 0}
        h.ctx.recent_keys = {}
        msg = Message(source="dingtalk", sender_name="履约跟单专员",
                      content="静默期里来的一条", raw_id="x1")
        _deliver_ready(h.ctx, [msg])
        self.assertEqual([c for _, c in h.ctx.delivered_texts],
                         ["静默期里来的一条"])


# 真机 2026-10-26 23:50→00:02 的原始现场：同一条「当日签」卡片被翻屏重渲染，
# 「当前情况」块与 top10 表格行顺序互换 —— 字面片段集合完全一样，只是排列不同。
_CARD_A = (
    "【当日签履约情况】\n"
    "我开始帮你跟进今天的当日签啦。\n"
    "目前已接入 691 票当日签任务，涉及 102 名小件员、4 个承包区。\n"
    "当前情况：\n已签收：668票\n待签收：23票\n履约率：96.67%\n"
    "以下是top10尚未履约票数的小件员：\n"
    "小件员编码: 3112099924 ｜ 小件员: 刘未 ｜ 票数: 2\n"
    "附件：运单明细")
_CARD_B = (
    "【当日签履约情况】\n"
    "我开始帮你跟进今天的当日签啦。\n"
    "目前已接入 691 票当日签任务，涉及 102 名小件员、4 个承包区。\n"
    "小件员编码: 3112099924 ｜ 小件员: 刘未 ｜ 票数: 2\n"
    "当前情况：\n已签收：668票\n待签收：23票\n履约率：96.67%\n"
    "以下是top10尚未履约票数的小件员：\n"
    "附件：运单明细")


class TestLongWindowDedup(unittest.TestCase):
    """NEAR_DUP_WINDOW 过期后，正常投递路径也要对照无时限的 delivered_texts。

    现场真机日志：
        23:50:05 已投递（当日晚签卡片，形态 A）
        23:56:05 同一卡片换了渲染形态（357 秒前已发过同一条），跳过  ← 窗口内，拦住
        00:02:26 已投递（同一卡片形态 B）                          ← 12 分钟，窗口已过，漏了
    """

    def _ready_handler(self):
        h = make_handler()
        seen = []
        h.ctx.on_message = seen.append
        h.ctx.lock = threading.Lock()
        h.ctx.count = {"accepted": 0, "rejected": 0}
        h.ctx.recent_keys = {}
        return h, seen

    def _age_recent_keys(self, h, age):
        """把 recent_keys 里所有记录的时间戳拨回 age 秒前，模拟窗口过期。"""
        for k, (s, c, _) in list(h.ctx.recent_keys.items()):
            h.ctx.recent_keys[k] = (s, c, time.monotonic() - age)

    def test_reordered_rerender_after_window_is_skipped(self):
        """12 分钟后的换形态重渲染必须被 delivered_texts 拦住（本次事故本体）。

        旧算法下：dup_age 为 None → 照发 → 微信群里同一张卡出现两遍。
        """
        from bridge.fingerprint import is_same_message
        self.assertTrue(is_same_message(_CARD_A, _CARD_B))   # 前提：判据认得这是同一张卡
        h, seen = self._ready_handler()
        _deliver_ready(h.ctx, [Message(source="dingtalk",
                                       sender_name="签收跟单专员",
                                       content=_CARD_A, raw_id="a1")])
        self.assertEqual(len(seen), 1)
        self._age_recent_keys(h, G.NEAR_DUP_WINDOW + 60)     # 12 分钟 > 10 分钟窗口
        emitted = _deliver_ready(h.ctx, [Message(source="dingtalk",
                                                 sender_name="签收跟单专员",
                                                 content=_CARD_B, raw_id="a2")])
        self.assertEqual(emitted, 0)                          # 重复的不许再投
        self.assertEqual(len(seen), 1)

    def test_genuinely_new_card_after_window_still_delivered(self):
        """数字变了的下一期卡片不是重渲染，必须照发 —— 宁可多发不漏发。"""
        h, seen = self._ready_handler()
        _deliver_ready(h.ctx, [Message(source="dingtalk",
                                       sender_name="签收跟单专员",
                                       content=_CARD_A, raw_id="a1")])
        self._age_recent_keys(h, G.NEAR_DUP_WINDOW + 60)
        changed = _CARD_A.replace("已签收：668票", "已签收：670票")
        emitted = _deliver_ready(h.ctx, [Message(source="dingtalk",
                                                 sender_name="签收跟单专员",
                                                 content=changed, raw_id="a3")])
        self.assertEqual(emitted, 1)
        self.assertEqual(len(seen), 2)

    def test_short_message_repeat_after_window_still_delivered(self):
        """短消息不受长窗兜底约束 —— delivered_texts 无时限，若把「收到」
        这类正常重复发言吃掉就是永久漏发（fingerprint.py 定死的方针）。"""
        h, seen = self._ready_handler()
        _deliver_ready(h.ctx, [Message(source="dingtalk",
                                       sender_name="张三", content="收到",
                                       raw_id="s1")])
        self._age_recent_keys(h, G.NEAR_DUP_WINDOW + 60)
        emitted = _deliver_ready(h.ctx, [Message(source="dingtalk",
                                                 sender_name="张三", content="收到",
                                                 raw_id="s2")])
        self.assertEqual(emitted, 1)
        self.assertEqual(len(seen), 2)


if __name__ == "__main__":
    unittest.main()
