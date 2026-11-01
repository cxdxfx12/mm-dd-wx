# -*- coding: utf-8 -*-
"""附件重发的三道闸 + 重启后的陈旧状态误报（2026-11-01 14:0x~14:2x 现场）。

三件实事：

1. **失败退避**：14:14~14:18，卡片一直停在屏上，手机端每趟都卡在 UC 的
   「立即下载」弹窗上 20 秒超时，超时释放冷却之后下一帧就重新下单 —— 连着
   派了三次，页面来回跳。退避期（ATTACH_FAIL_BACKOFF）内不许自动重派。
2. **附件「重复」要看有没有真的发出去**：14:21 那份 xlsx 落了盘、消息也
   入库了，投递线程卡死没发成；之后重传同一份文件，网关按「文件已存在」、
   service 按「uid 已入库」两头挡住，这条就永远发不出去。
3. **陈旧状态不算故障**：worker 14:08 重启把 last_attach 清空，手机端
   lastStatus 还是 12:31 那次手动实验的「屏上找不到附件行（下载附件）」，
   重启后第一次心跳就被当成新故障告警 —— 用户以为又坏了。
"""

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.models import Message  # noqa: E402
from bridge.sources.android_gateway import (  # noqa: E402
    ATTACH_FAIL_BACKOFF, _Ctx, _order_attach, _attach_timeout_check)
from bridge.storage import Storage  # noqa: E402


class _Unit:
    """_order_attach 只读 pending 里每个单元的 content。"""

    def __init__(self, content):
        self.content = content


class TestFailBackoff(unittest.TestCase):
    def setUp(self):
        self.ctx = _Ctx(token="t", chat_name="群")
        self.ctx.attach_keys = ["附件：", "附件:", "明细：", "明细:", "明细附件",
                                       "查看完整", "明细运单", "运单明细", "下载附件"]

    def _order(self, content="明细附件：拦截风险明细.xlsx"):
        return _order_attach(self.ctx, [_Unit(content)])

    def test_timeout_sets_backoff_and_blocks_reorder(self):
        """超时之后卡片还在屏上：下一帧不许立刻重新下单。"""
        self.assertEqual(self._order(), "拦截风险明细.xlsx")
        # 手动把下单时刻推到「已超时」，模拟 20 秒没拿到文件
        self.ctx.attach_orders["拦截风险明细.xlsx"] = time.monotonic() - 999
        _attach_timeout_check(self.ctx)
        self.assertEqual(self.ctx.attach_active, "")
        self.assertIn("拦截风险明细.xlsx", self.ctx.attach_fail_at,
                      "超时必须记退避时刻，否则下一帧就重派")
        self.assertEqual(self._order(), "",
                         "退避期内重复下单 = 无限 20 秒空等")

    def test_backoff_expires_and_allows_retry(self):
        """退避过了就能再抓（不是永久拉黑）。"""
        self.ctx.attach_fail_at["拦截风险明细.xlsx"] = (
            time.monotonic() - ATTACH_FAIL_BACKOFF - 1)
        self.assertEqual(self._order(), "拦截风险明细.xlsx")

    def test_manual_order_ignores_backoff(self):
        """/attach-now 是人工补抓，不该被自动退避挡住。"""
        self.ctx.attach_fail_at["拦截风险明细.xlsx"] = time.monotonic()
        self.assertEqual(self._order(), "",
                         "自动路径应当被挡住（对照组）")


class TestFileDedupRequiresDelivery(unittest.TestCase):
    """附件的「重复」= 入过库 **且** 发成功过。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Storage(str(Path(self.dir) / "t.db"))
        self.msg = Message(source="dingtalk", chat_name="群", sender_name="附件",
                           content="【附件】拦截风险明细.xlsx", msg_type="file",
                           media_path="p", raw_id="attach-" + "a" * 40)

    def test_not_seen_at_all(self):
        self.assertFalse(self.store.is_duplicate(self.msg.uid, need_delivered=True))

    def test_in_db_but_never_delivered_counts_as_new(self):
        """事故原样：入库了、投递卡死 —— 重传必须能再发一次。"""
        self.store.save_message(self.msg)
        self.assertFalse(self.store.is_duplicate(self.msg.uid, need_delivered=True))

    def test_delivered_counts_as_duplicate(self):
        self.store.save_message(self.msg)
        self.store.log_delivery(self.msg.uid, "群", "ok", "已发送文件")
        self.assertTrue(self.store.is_duplicate(self.msg.uid, need_delivered=True))

    def test_failed_delivery_is_not_duplicate(self):
        self.store.save_message(self.msg)
        self.store.log_delivery(self.msg.uid, "群", "fail", "标题栏未确认")
        self.assertFalse(self.store.is_duplicate(self.msg.uid, need_delivered=True),
                         "发失败过的不算发过 —— 这正是需要重发的那种")

    def test_text_message_keeps_old_semantics(self):
        """文本消息入库即算处理完（可能命中规则被过滤），行为不能变。"""
        txt = Message(source="dingtalk", chat_name="群", sender_name="机器人",
                      content="日报", msg_type="text", raw_id="r1")
        self.store.save_message(txt)
        self.assertTrue(self.store.is_duplicate(txt.uid))
        self.assertTrue(self.store.is_duplicate(txt.uid, need_delivered=False))


if __name__ == "__main__":
    unittest.main()
