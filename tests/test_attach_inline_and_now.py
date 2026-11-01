# -*- coding: utf-8 -*-
"""附件字眼「夹在长行中间」+ 手工补抓接口（2026-11-01 12:08 真实漏抓）。

真实一条积压异常提醒（钉钉把多段拼进同一行）：

    @所有人积压异常提醒: 下载附件 ｜ 承包区编码: 311242 ｜ 承包区名称: 浙江萧山菜鸟服务点 ｜ …

`_attach_re` 要求行首是字眼、整行全等又不成立 → 这个按钮从来没下过单：
正文转发成功、附件永远不来。修法是加一道「行内含」判据，并留一个
`/attach-now` 运维接口补抓历史卡片（那条消息已入库，修好解析也不会重放）。
"""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.sources.android_gateway import (  # noqa: E402
    _Ctx, _Handler, _attach_names, _clean_attach_keys, _order_attach)

KEYS = ["附件：", "明细：", "明细附件", "查看完整", "下载附件"]

# 12:08:48 那条卡片的真实一行（截取，保留分隔符形态）
INLINE_ROW = ("@所有人积压异常提醒: 下载附件 ｜ 承包区编码: 311242 ｜ "
              "承包区名称: 浙江萧山菜鸟服务点 ｜ 1-2天积压量: 4 ｜ 累计积压量: 22")


class TestInlineAttachName(unittest.TestCase):
    def setUp(self):
        self.keys = _clean_attach_keys(KEYS)

    def test_button_middle_of_long_line(self):
        """事故原样：字眼夹在「: … ｜ …」中间。"""
        self.assertEqual(_attach_names(INLINE_ROW, self.keys), ["下载附件"])

    def test_narrative_sentence_not_matched(self):
        """叙述句里的「下载附件」不能当按钮 —— 否则每张卡都白跑一次抓取。"""
        self.assertEqual(_attach_names("请下载附件查看今天的明细", self.keys), [])
        self.assertEqual(_attach_names("已生成附件，请点击下载", self.keys), [])

    def test_wrapped_in_brackets(self):
        self.assertEqual(_attach_names("（下载附件）", self.keys), ["下载附件"])

    def test_short_key_not_inline_matched(self):
        """「附件：」这类标签只在行首有意义，行内出现不算按钮。"""
        self.assertEqual(_attach_names("备注 附件：闲话", self.keys), [])

    def test_existing_forms_unchanged(self):
        """行首形态与整行形态不能被新判据带跑偏。"""
        self.assertEqual(_attach_names("附件：运单明细", self.keys), ["运单明细"])
        self.assertEqual(_attach_names("明细：下载3天滞留运单明细", self.keys),
                         ["下载3天滞留运单明细"])
        self.assertEqual(_attach_names("查看完整月报明细", self.keys), ["月报明细"])

    def test_no_attach_row(self):
        self.assertEqual(_attach_names("@所有人积压异常提醒: 今天积压386票",
                                       self.keys), [])


class TestQueueAttachNow(unittest.TestCase):
    """`/attach-now` 背后的排队逻辑（handler 方法，不跑 BaseHTTPRequest）。"""

    def _handler(self):
        h = object.__new__(_Handler)
        ctx = _Ctx(token="t", chat_name="数字质控")
        ctx.attach_keys = _clean_attach_keys(KEYS)
        h.ctx = ctx
        return h, ctx

    def test_accepts_and_queues(self):
        h, ctx = self._handler()
        msg, ok = h._queue_attach_now("下载附件")
        self.assertTrue(ok, msg)
        self.assertEqual(ctx.attach_queue, ["下载附件"])

    def test_empty_name_rejected(self):
        h, ctx = self._handler()
        msg, ok = h._queue_attach_now("  ")
        self.assertFalse(ok)
        self.assertEqual(ctx.attach_queue, [])
        self.assertIn("name=", msg)

    def test_clears_cooldown(self):
        """不冷静却会把重试吞掉，表现成「点了没反应」。"""
        h, ctx = self._handler()
        ctx.attach_orders["下载附件"] = time.monotonic()
        h._queue_attach_now("下载附件")
        self.assertNotIn("下载附件", ctx.attach_orders)

    def test_duplicate_not_queued_again(self):
        h, ctx = self._handler()
        h._queue_attach_now("下载附件")
        msg, ok = h._queue_attach_now("下载附件")
        self.assertFalse(ok)
        self.assertEqual(ctx.attach_queue, ["下载附件"])
        self.assertIn("已在队列", msg)

    def test_same_name_as_running_rejected(self):
        h, ctx = self._handler()
        ctx.attach_active = "下载附件"
        msg, ok = h._queue_attach_now("下载附件")
        self.assertFalse(ok)
        self.assertIn("正在抓", msg)

    def test_disabled_switch_rejected(self):
        h, ctx = self._handler()
        ctx.attach_enabled = False
        msg, ok = h._queue_attach_now("下载附件")
        self.assertFalse(ok)
        self.assertEqual(ctx.attach_queue, [])
        self.assertIn("总开关", msg)

    def test_queued_name_is_dispatched_next_frame(self):
        """受理之后必须真能下发：`_order_attach` 从同一队列取。"""
        h, ctx = self._handler()
        h._queue_attach_now("下载附件")
        self.assertEqual(_order_attach(ctx, []), "下载附件")
        self.assertEqual(ctx.attach_active, "下载附件")

    def test_post_whitelist_includes_attach_now(self):
        """`do_POST` 有路径白名单，漏登记就是 404「未知路径」（本次真踩过）。"""
        import inspect
        src = inspect.getsource(_Handler.do_POST)
        self.assertIn('"/attach-now"', src, "do_POST 白名单里没有 /attach-now")


class TestAutoKeywords(unittest.TestCase):
    """万能字眼：短行 + 名词 + 动作（用户 2026-11-01 要求）。

    边界刻意收窄 —— 「正文里出现『明细』就抓」会让手机每张卡都白跑一次，
    真正要抓的是「屏幕上那句让你去下载的短文案」。
    """

    KEYS = ["附件：", "明细："]

    def _names(self, text, auto=True):
        return _attach_names(text, _clean_attach_keys(self.KEYS), auto)

    def test_action_plus_noun(self):
        self.assertEqual(self._names("下载附件"), ["下载附件"])
        self.assertEqual(self._names("点击查看表格"), ["点击查看表格"])
        self.assertEqual(self._names("导出明细"), ["导出明细"])
        self.assertEqual(self._names("查看Excel"), ["查看Excel"])

    def test_noun_without_action_ignored(self):
        self.assertEqual(self._names("明细如下"), [])

    def test_action_without_noun_ignored(self):
        self.assertEqual(self._names("点击查看详情"), [])

    def test_long_sentence_ignored(self):
        self.assertEqual(
            self._names("所有超过2天积压数据我已经整理成Excel附件，请立即催签！"), [])

    def test_explicit_keys_still_win(self):
        """精确字眼优先，不会被万能规则改掉名字。"""
        self.assertEqual(self._names("附件：运单明细"), ["运单明细"])

    def test_switch_off(self):
        self.assertEqual(self._names("点击查看表格", auto=False), [])
        # 关掉万能字眼不影响精确字眼
        self.assertEqual(self._names("附件：运单明细", auto=False), ["运单明细"])


class TestFileNameOnlyLine(unittest.TestCase):
    """整行就是一个文件名（2026-11-01 17:22 拦截跟单日报真实漏抓）。

    卡片底部直接写着「未及时转出明细.xlsx」，不以任何前缀开头、也没有
    「下载/查看」动词 —— 前缀规则、整行全等、行内含、万能字眼四条全不中，
    附件从来没被下过单。用户当时的原话：「干脆要加个字眼 .xlsx」。
    """

    def setUp(self):
        self.keys = _clean_attach_keys(KEYS)

    def test_bare_filename_line(self):
        self.assertEqual(
            _attach_names("未及时转出明细.xlsx", self.keys),
            ["未及时转出明细.xlsx"])

    def test_filename_at_end_of_card(self):
        card = ("考核预警（近5日）\n预计考核金额\n3 元\n"
                "未及时转出 TOP5 快递员\n附件\n未及时转出明细.xlsx")
        self.assertEqual(_attach_names(card, self.keys), ["未及时转出明细.xlsx"])

    def test_other_extensions(self):
        for name in ("积压明细.csv", "月度报表.pdf", "客户声音汇总.xls",
                     "台账.docx", "打包结果.zip"):
            self.assertEqual(_attach_names(name, self.keys), [name], name)

    def test_sentence_mentioning_file_not_matched(self):
        """正文里提到文件名（带空格/句号）不是按钮。"""
        self.assertEqual(
            _attach_names("请把 未及时转出明细.xlsx 发给网点，注意核对。", self.keys), [])
        self.assertEqual(
            _attach_names("详见附件：未及时转出明细.xlsx 以及相关说明", self.keys), [])

    def test_prefix_rules_still_win(self):
        """「附件：xxx」这种带前缀的，名字仍然只取冒号后面那段。"""
        self.assertEqual(
            _attach_names("附件：未及时转出明细.xlsx", self.keys),
            ["未及时转出明细.xlsx"])

    def test_fetch_keys_carry_the_stem(self):
        """下发给手机的找行字眼必须带上这一行的开头，否则手机点不到。

        手机端是 startsWith(字眼) 找行的：只发配置里的「附件：」等前缀，
        「未及时转出明细.xlsx」永远匹配不上。
        """
        from bridge.sources.android_gateway import _fetch_keys_for

        class _C:
            attach_keys = _clean_attach_keys(KEYS)

        got = _fetch_keys_for(_C(), "未及时转出明细.xlsx")
        self.assertIn("未及时转出明细", got.split(","))
        # 原有字眼一个都不能丢（否则别的卡片又抓不到了）
        for k in KEYS:
            self.assertIn(k, got.split(","))

    def test_fetch_keys_without_extension_also_added(self):
        """不带扩展名的名字也要下发（2026-11-01 18:0x 补）。

        旧行为是「没有 .xlsx 就原样返回」，靠的是配置里配了「查看完整」这种
        前缀碰巧能命中；一旦文案模板换了个开头（「下载完整…」），手机端就
        只能靠 fallback 瞎点一行。名字本身就是屏上文本，startsWith 必然命中自己。
        """
        from bridge.sources.android_gateway import _fetch_keys_for

        class _C:
            attach_keys = _clean_attach_keys(KEYS)

        got = _fetch_keys_for(_C(), "运单明细").split(",")
        self.assertIn("运单明细", got)
        for k in KEYS:                      # 原有字眼一个都不能丢
            self.assertIn(k, got)


class TestFormatMarkedButton(unittest.TestCase):
    """2026-11-01 18:06 履约晚报：`查看完整履约失败明细（Excel）`。

    这是一张**超长 H5 卡片**（约 3 屏高）底部的下载按钮。名字原本被键
    「查看完整」吃掉前缀、只剩「履约失败明细（Excel）」—— 屏上没有以它开头
    的一行，日志/去重也都按残缺名做键。修法：名字中间夹格式标记时退回整行。
    """

    CARD = "\n".join([
        "各位好，我是履约跟单专员，今日晚报来啦～",
        "📦 签收总量： 625 票",
        "⚠️ 履约失败： 10 票",
        "履约失败明细（10票）",
        "员工（票数）｜运单号｜失败原因",
        "近七天低履约率小件员（末尾 5%)及夜间签收管控命中小件员",
        "查看完整履约失败明细（Excel）",
    ])

    def setUp(self):
        self.keys = _clean_attach_keys(KEYS)

    def test_keeps_full_button_text(self):
        self.assertEqual(_attach_names(self.CARD, self.keys),
                         ["查看完整履约失败明细（Excel）"])

    def test_title_row_kept_but_ranked_last(self):
        """「履约失败明细（10票）」是表格标题 —— 仍收下，但排在真按钮之后。

        2026-11-01 用户：「只要带明细字眼的都点开转发原文件」。标题行点了
        多半没反应，可万一某种卡片的附件就是长成标题样（「📦 猫超件明细
        （12 票）」），放过它就等于永远抓不到。所以：**收，但排最后** ——
        手机端按候选链依次点，前一个拿到文件就收工，标题行根本轮不到。
        """
        self.assertEqual(
            _attach_names("履约失败明细（10票）", self.keys),
            ["履约失败明细（10票）"])

    def test_cands_put_real_button_before_title(self):
        """候选链顺序：真按钮（带动作词）→ 标题行。"""
        from bridge.sources.android_gateway import _attach_cands

        cands = _attach_cands(self.CARD, self.keys)
        self.assertEqual(cands[0], "查看完整履约失败明细（Excel）")
        self.assertIn("履约失败明细（10票）", cands)
        self.assertLess(cands.index("查看完整履约失败明细（Excel）"),
                        cands.index("履约失败明细（10票）"))

    def test_narrative_line_never_a_candidate(self):
        """「麻烦你结合下方明细判断一下…」是叙述句，不点。"""
        from bridge.sources.android_gateway import _attach_cands

        line = "今日巡检发现异常，麻烦你结合下方明细判断一下，看是否需要跟进处理哦 👀"
        self.assertEqual(_attach_names(line, self.keys), [])
        self.assertEqual(_attach_cands(line, self.keys), [])

    def test_prefixed_lines_not_duplicated_in_cands(self):
        """「明细：xxx」已按名字收下，就别再把整行也塞进候选（同一行点两次）。"""
        from bridge.sources.android_gateway import _attach_cands

        card = "明细：普通件滞留催提AI代打明细\n明细：生鲜件滞留催提AI代打明细"
        self.assertEqual(_attach_cands(card, self.keys),
                         ["普通件滞留催提AI代打明细", "生鲜件滞留催提AI代打明细"])

    def test_mingxi_header_alone_still_caught(self):
        """整卡只有一个「明细」标题、没有别的附件行 → 也收（用户要求）。"""
        from bridge.sources.android_gateway import _attach_cands

        self.assertEqual(_attach_cands("📦 猫超件明细（12 票）", self.keys),
                         ["📦 猫超件明细（12 票）"])

    def test_monthly_report_still_works(self):
        """老形态「查看完整月报明细（Excel）」不能被改坏。"""
        self.assertEqual(
            _attach_names("查看完整月报明细（Excel）", self.keys),
            ["查看完整月报明细（Excel）"])

    def test_prefixed_attachment_unchanged(self):
        """「附件：xxx.xlsx」这种真文件名里没有格式标记，名字照旧只取冒号后。"""
        self.assertEqual(
            _attach_names("附件：运单明细.xlsx", self.keys), ["运单明细.xlsx"])


if __name__ == "__main__":
    unittest.main()
