# -*- coding: utf-8 -*-
"""附件抓取失败后的**自动重试**（2026-11-05）。

事故：抓取失败以前等于永久丢失 —— 超时只「释放冷却」，而重新下单的唯一
触发是「这张卡片又作为新消息出现」；卡片早滚出屏幕、也不会再被当成新单元。
当天 5 次下单只成 1 次，其中 14:22 / 14:24 / 15:35 三次都是「35 秒无文件」，
之后再没有任何动作；而 15:35 那份同名附件在 16:24 重下一遍就成功了 ——
说明只要再试一次就能救回来。

现在：超时 → 排一次有界重试（ATTACH_RETRY_DELAYS）→ 到点回到队列 →
手机端自己上翻找行、重新点。抓到就清账，重试次数用完就放弃并留日志。
"""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.sources.android_gateway import (  # noqa: E402
    ATTACH_FETCH_TIMEOUT, ATTACH_RETRY_DELAYS, _Ctx, _attach_timeout_check,
    _order_attach)


class _Unit:
    def __init__(self, content):
        self.content = content


class TestAutoRetry(unittest.TestCase):

    def setUp(self):
        self.ctx = _Ctx(token="t", chat_name="群")
        self.ctx.attach_keys = ["附件：", "附件:", "明细：", "明细:", "明细附件",
                                "查看完整", "明细运单", "运单明细", "下载附件"]
        self.unit = _Unit("明细附件：拦截风险明细.xlsx")

    def _time_out(self, name="拦截风险明细.xlsx"):
        """把下单时刻推过超时线，然后跑一次超时检查（每帧都会跑）。"""
        self.ctx.attach_orders[name] = time.monotonic() - ATTACH_FETCH_TIMEOUT - 1
        _attach_timeout_check(self.ctx)

    def test_timeout_schedules_retry(self):
        self.assertEqual(_order_attach(self.ctx, [self.unit]), "拦截风险明细.xlsx")
        self._time_out()
        self.assertEqual(self.ctx.attach_active, "")
        self.assertIn("拦截风险明细.xlsx", self.ctx.attach_retry_at)
        self.assertEqual(self.ctx.attach_tries["拦截风险明细.xlsx"], 1)

    def test_retry_not_dispatched_before_due(self):
        """刚排上就派 = 手机还在上一趟的收尾里，只会被它当场丢掉。"""
        _order_attach(self.ctx, [self.unit])
        self._time_out()
        self.assertEqual(_order_attach(self.ctx, []), "")

    def test_retry_dispatched_when_due(self):
        _order_attach(self.ctx, [self.unit])
        self._time_out()
        # 把排期拨到过去，模拟 2 分钟过去
        self.ctx.attach_retry_at["拦截风险明细.xlsx"] = time.monotonic() - 1
        self.assertEqual(_order_attach(self.ctx, []), "拦截风险明细.xlsx")
        self.assertEqual(self.ctx.attach_active, "拦截风险明细.xlsx")

    def test_retry_survives_card_leaving_screen(self):
        """重试不依赖「卡片再次出现在新消息里」—— 这正是以前漏掉的根因。"""
        _order_attach(self.ctx, [self.unit])
        self._time_out()
        self.ctx.attach_retry_at["拦截风险明细.xlsx"] = time.monotonic() - 1
        # 一帧「没有新消息」的帧（pending 为空）也必须能把重试派出去
        self.assertEqual(_order_attach(self.ctx, []), "拦截风险明细.xlsx")

    def test_retry_is_bounded(self):
        """不能无限重试：次数用完就放弃，并清掉状态。"""
        _order_attach(self.ctx, [self.unit])
        for i in range(len(ATTACH_RETRY_DELAYS)):
            self._time_out()
            self.ctx.attach_retry_at["拦截风险明细.xlsx"] = time.monotonic() - 1
            self.assertEqual(_order_attach(self.ctx, []), "拦截风险明细.xlsx")
        self._time_out()          # 第 N+1 次超时
        self.assertNotIn("拦截风险明细.xlsx", self.ctx.attach_retry_at)
        self.assertNotIn("拦截风险明细.xlsx", self.ctx.attach_tries)
        # 放弃之后不会再有新的排期：再过多少帧都派不出去了
        self.assertEqual([_order_attach(self.ctx, []) for _ in range(3)],
                         ["", "", ""])
        self.assertEqual(self.ctx.attach_queue, [])

    def test_retry_respects_cooldown(self):
        """这一份刚抓到过（冷却期内）就别再点 —— 重试不能绕过冷却。"""
        _order_attach(self.ctx, [self.unit])
        self._time_out()
        # 模拟「期间其实抓到了」：冷却仍在
        self.ctx.attach_orders["拦截风险明细.xlsx"] = time.monotonic()
        self.ctx.attach_retry_at["拦截风险明细.xlsx"] = time.monotonic() - 1
        self.assertEqual(_order_attach(self.ctx, []), "")
        self.assertNotIn("拦截风险明细.xlsx", self.ctx.attach_retry_at)

    def test_disable_switch_stops_retries(self):
        _order_attach(self.ctx, [self.unit])
        self._time_out()
        self.ctx.attach_enabled = False
        self.ctx.attach_retry_at["拦截风险明细.xlsx"] = time.monotonic() - 1
        self.assertEqual(_order_attach(self.ctx, []), "")

    def test_backoff_still_blocks_card_rescan(self):
        """退避期内卡片重新出现也不许立刻重派（老规矩，别被重试功能带跑）。"""
        from bridge.sources.android_gateway import ATTACH_FAIL_BACKOFF
        _order_attach(self.ctx, [self.unit])
        self._time_out()
        self.ctx.attach_fail_at["拦截风险明细.xlsx"] = time.monotonic()
        self.assertLess(ATTACH_FAIL_BACKOFF, 10 ** 6)
        self.assertEqual(_order_attach(self.ctx, [self.unit]), "")


if __name__ == "__main__":
    unittest.main()
