"""锚点丢失时的「翻一屏试探」状态机（probe / wait / give_up）。

现场（2026-10-19 14:11，「拦截单退单打印提醒」漏转）：

    14:11:55 上一帧的锚点在本帧里找不到 —— 已请手机翻一屏试着找回（第 1 次）
    14:11:57 锚点丢失，基线已重建到当前画面

之间只隔 2 秒，落在 ``PROBE_COOLDOWN=3.0`` 里，于是**手机翻完送回来的第一帧
被当成「不该再翻」直接认输**。认输 = commit 重建基线 = 把屏上一切登记成
「已知历史」，那条把锚点挤出屏幕的长卡片连同基线一起被吞掉，既没转发也
没入库，``forward_recent.py`` 都补不回来。

根因不是冷却期太长，是**状态不够**：旧实现只有「翻」和「认输」两态，
「手机正在翻、先别动」这件事没有地方表达。现在拆成三态，本文件锁住它。
"""

import time
import unittest

from bridge.sources import android_gateway as G
from bridge.sources.android_gateway import _Ctx, _Handler


def make_handler():
    """不跑 BaseHTTPRequestHandler.__init__，只挂上 ctx 用纯逻辑方法。"""
    h = object.__new__(_Handler)
    h.ctx = _Ctx(token="t", chat_name="数字质控")
    return h


class TestProbeDecision(unittest.TestCase):

    def test_frame_during_cooldown_waits_instead_of_giving_up(self):
        """⚠ 本文件的立身之本：冷却期内的新帧 → wait，不是 give_up。

        旧实现这里返回 False，调用方直接 commit 重建基线 —— 漏转就是这么来的。
        """
        h = make_handler()
        h.ctx.align_at = time.monotonic() - 0.5      # 刚发出翻页指令 0.5 秒
        h.ctx.align_md5 = "AAA"
        verdict = h._probe_decision("BBB")           # 手机翻完，新帧回来了
        self.assertEqual(verdict, "wait")
        self.assertEqual(h.ctx.align_tried, 0, "等不应该消耗翻页额度")

    def test_unchanged_screen_waits(self):
        """md5 没变 = 屏幕一动没动，再发指令也是同一屏，等。"""
        h = make_handler()
        h.ctx.align_at = time.monotonic() - 10.0     # 早就过了冷却期
        h.ctx.align_md5 = "AAA"
        self.assertEqual(h._probe_decision("AAA"), "wait")

    def test_settled_new_frame_probes(self):
        """冷却期外 + 屏幕变了 → 请手机再翻一屏，额度 +1。"""
        h = make_handler()
        h.ctx.align_at = time.monotonic() - 5.0
        h.ctx.align_md5 = "AAA"
        self.assertEqual(h._probe_decision("BBB"), "probe")
        self.assertEqual(h.ctx.align_tried, 1)
        self.assertEqual(h.ctx.align_md5, "AAA",
                         "align_md5 由调用点更新，这里不该动")

    def test_repeated_intermediate_frames_do_not_burn_budget(self):
        """连翻六屏的过程中会来很多中间态帧，它们只能消耗时间，不能消耗额度。"""
        h = make_handler()
        h.ctx.align_md5 = "f0"
        for i in range(G.PROBE_MAX * 3):
            h.ctx.align_at = time.monotonic() - 0.3  # 每帧都在冷却期内
            self.assertEqual(h._probe_decision("f%d" % (i + 1)), "wait")
        self.assertEqual(h.ctx.align_tried, 0)

    def test_wait_times_out_to_give_up(self):
        """手机没手势权限 / 页面卡死时，不能永远等下去。

        2026-10-29 起超时不是就地 commit：先请手机回到底部（``to_bottom``），
        回底那一帧才重建基线 —— 视口停在历史中间时重建＝把历史当新起点。
        """
        h = make_handler()
        h.ctx.align_at = time.monotonic() - 0.3
        h.ctx.align_md5 = "AAA"
        h.ctx.align_wait_since = time.monotonic() - (G.PROBE_SETTLE_MAX + 1)
        self.assertEqual(h._probe_decision("BBB"), "wait")
        self.assertEqual(h.ctx.align_wait_since, 0.0, "认输后等待状态要清掉")
        self.assertTrue(h.ctx.want_bottom, "要请手机回到底部")
        # 手机没手势权限时回不来：等够时间就就地重建，不能永远卡着
        h.ctx.await_bottom_at = time.monotonic() - (G.PROBE_SETTLE_MAX + 1)
        self.assertEqual(h._probe_decision("CCC"), "give_up")

    def test_budget_exhausted_gives_up(self):
        """一轮真翻满了仍对不上 → 认输（多半是在翻历史/换群）。"""
        h = make_handler()
        h.ctx.align_at = time.monotonic() - 5.0
        h.ctx.align_md5 = "AAA"
        h.ctx.align_tried = G.PROBE_MAX
        self.assertEqual(h._probe_decision("BBB"), "wait")
        self.assertTrue(h.ctx.await_bottom, "认输要先回到底部再重建基线")

    def test_give_up_resets_budget_for_next_round(self):
        """认输要清额度：否则一次长卡片之后，今天剩余时间都不再有救援机会。

        2026-10-29 起认输会**先请手机回到底部**（``await_bottom``）：那一帧
        才是重建基线的时机；消费掉这个标志之后，额度确实没有被永久吃掉。
        """
        h = make_handler()
        h.ctx.align_at = time.monotonic() - 5.0
        h.ctx.align_md5 = "AAA"
        h.ctx.align_tried = G.PROBE_MAX
        self.assertEqual(h._probe_decision("BBB"), "wait")
        self.assertEqual(h.ctx.align_tried, 0)
        self.assertTrue(h.ctx.await_bottom, "认输要请手机回到底部")
        # 手机回到底后送来的那一帧：重建基线
        h.ctx.frame_reason = "probe-bottom"
        self.assertEqual(h._probe_decision("CCC"), "give_up")
        self.assertFalse(h.ctx.await_bottom, "回底只许消费一次")
        # 再往后锚点又对不上 → 重新拥有完整额度
        h.ctx.align_at = time.monotonic() - 5.0
        self.assertEqual(h._probe_decision("DDD"), "probe")

    def test_normal_frame_clears_everything(self):
        """锚点回来了 = 这一轮结束，额度和等待起点都归零。"""
        h = make_handler()
        h.ctx.align_tried = 3
        h.ctx.align_wait_since = time.monotonic() - 4
        h._reset_probe()
        self.assertEqual(h.ctx.align_tried, 0)
        self.assertEqual(h.ctx.align_wait_since, 0.0)

    def test_stale_give_up_clears_align_state(self):
        """过期 give_up 后丢失时刻也必须清掉。

        现场（2026-10-27 16:40/16:54）：16:40 过期重建基线后 ``align_at``
        还挂着 15:11 的老时间戳；16:54 锚点再对不上时立刻又命中「过期」
        分支，日志打出「已挂 6184 秒」（其实距上一帧只有 14 分钟），
        还白白跳过了一次翻页找回的机会。
        """
        h = make_handler()
        h.ctx.align_at = time.monotonic() - (G.ALIGN_STALE + 1)
        h.ctx.align_md5 = "OLD"
        h.ctx.align_tried = 2
        self.assertEqual(h._probe_decision("NEW"), "wait")
        self.assertEqual(h.ctx.align_at, 0.0, "过期认输后丢失时刻要清掉")
        self.assertEqual(h.ctx.align_md5, "")
        # 过期后先请手机回底；回底那一帧才重建（2026-10-29）
        h.ctx.frame_reason = "probe-bottom"
        self.assertEqual(h._probe_decision("NEW1"), "give_up")
        # 清干净之后，锚点再对不上应从 probe 重新开始，而不是再 give_up
        self.assertEqual(h._probe_decision("NEW2"), "probe")


class TestProbeConstants(unittest.TestCase):

    def test_cooldown_shorter_than_real_swipe_roundtrip(self):
        """冷却期必须短于「手机翻完一屏并把新帧送回来」的实测耗时（约 2 秒）。

        真机 14:11:55 → 14:11:57。旧值 3.0 秒把回帧整个盖住，才会一帧就被判
        认输。1.2 秒既短于回帧耗时，又长于中间态帧（SWIPE_MS+SETTLE_MS 约
        700ms），刚好卡在中间。
        """
        self.assertLess(G.PROBE_COOLDOWN, 2.0)
        self.assertGreater(G.PROBE_COOLDOWN, 0.7)

    def test_budget_covers_multi_screen_card(self):
        """长卡片可能占两三屏，翻一次露不出锚点。"""
        self.assertGreaterEqual(G.PROBE_MAX, 4)

    def test_settle_timeout_is_bounded(self):
        """等必须有上限，否则手机卡死时基线永远不更新，后续消息全丢。"""
        self.assertGreater(G.PROBE_SETTLE_MAX, 5.0)
        self.assertLess(G.PROBE_SETTLE_MAX, 60.0)


if __name__ == "__main__":
    unittest.main()
