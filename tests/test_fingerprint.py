"""指纹模块测试 —— 重点是「两条采集路径必须用同一份实现」。

为什么这个测试比普通的单元测试重要
    指纹漂移的后果不是报错，而是**静默地重复转发**：同一条消息在 OCR 路径
    下算出 uid A、在安卓网关下算出 uid B，去重表里对不上，于是切一次采集
    方式就把旧消息又发一遍。这个仓库已经因为「机器人昵称读法抖动」踩过一次
    同类问题（`data/bridge.db` 里两条正文 md5 相同、发送者一个带「机器人」
    后缀一个不带）。所以这里有一条断言专门盯着「两边是同一个函数对象」。
"""
from __future__ import annotations

import unittest

from bridge.fingerprint import (content_pieces, fp_of, is_same_message,
                                norm_sender, unit_fp)
from bridge.sources import dingtalk_ocr


class TestNormSender(unittest.TestCase):

    def test_strips_all_whitespace(self):
        self.assertEqual(norm_sender("签收跟单专员 机器人"), "签收跟单专员")
        self.assertEqual(norm_sender("  签收跟单专员\t机器人\n"), "签收跟单专员")

    def test_robot_suffix_optional(self):
        """实测抖动：同一帧里同一机器人，昵称有时带「机器人」有时不带。"""
        self.assertEqual(norm_sender("签收跟单专员 机器人"),
                         norm_sender("签收跟单专员"))

    def test_keeps_distinct_robots_distinct(self):
        """只去后缀，不去前缀 —— 两个不同机器人不能被混成一个。"""
        self.assertNotEqual(norm_sender("签收跟单专员 机器人"),
                            norm_sender("拦截跟单专员 机器人"))

    def test_empty_and_none_safe(self):
        self.assertEqual(norm_sender(""), "")
        self.assertEqual(norm_sender(None), "")

    def test_robot_only_name(self):
        self.assertEqual(norm_sender("机器人"), "")


class TestFingerprint(unittest.TestCase):

    def test_fp_of_is_deterministic(self):
        self.assertEqual(fp_of("张三", "你好"), fp_of("张三", "你好"))

    def test_content_difference_changes_fp(self):
        self.assertNotEqual(fp_of("张三", "你好"), fp_of("张三", "你好啊"))

    def test_sender_noise_does_not_change_fp(self):
        self.assertEqual(fp_of("签收跟单专员 机器人", "内容 1234"),
                         fp_of("签收跟单专员", "内容 1234"))

    def test_unit_fp_delegates_to_fp_of(self):
        unit = {"sender": "播报助手 机器人", "content": "【提醒】374票"}
        self.assertEqual(unit_fp(unit), fp_of("播报助手 机器人", "【提醒】374票"))

    def test_fp_length(self):
        self.assertEqual(len(fp_of("a", "b")), 32)


class TestNoDriftBetweenPaths(unittest.TestCase):
    """防漂移：两条采集路径共用同一份实现。"""

    def test_ocr_module_reexports_same_objects(self):
        self.assertIs(dingtalk_ocr.norm_sender, norm_sender)
        self.assertIs(dingtalk_ocr.unit_fp, unit_fp)

    def test_ocr_units_match_uses_shared_norm(self):
        """OCR 的相似度判定也得走同一份归一化，否则锚点比对会因昵称抖动误判。"""
        a = {"sender": "签收跟单专员 机器人", "content": "同一段正文"}
        b = {"sender": "签收跟单专员", "content": "同一段正文"}
        self.assertTrue(dingtalk_ocr.units_match(a, b))
        self.assertEqual(dingtalk_ocr.unit_fp(a), dingtalk_ocr.unit_fp(b))


class TestIsSameMessage(unittest.TestCase):
    """同一张钉钉机器人卡片，手机上会渲染成两种形态 —— 要判成同一条。

    真机事故（2026-10-17「拦截跟单日报」）：同一张卡片的两次上报，一次表格
    解析成「列名: 值 ｜ 列名: 值」横排，一次退化成一格一行。指纹不同 → uid
    不同 → 下游 is_duplicate 挡不住 → 微信群被灌了两遍。
    用户的原话是「我手机往上翻页，会发重复的」。

    这里的夹具就是那两次上报的**真实正文**（截取关键段）。
    """

    # 形态 A：表格识别成功，横排
    FORM_A = (
        "@所有人拦截跟单日报\n"
        "时间 2026/10/17 · 浙江杭州萧山区大江东公司\n"
        "跟单概况\n"
        "指标\n"
        "10/13\n10/14\n10/15\n10/16\n10/17\n"
        "跟单任务数\n30\n21\n23\n20\n17\n"
        "任务完成数\n28\n19\n22\n19\n3\n"
        "完成率\n93.33%\n90.48%\n95.65%\n95.00%\n17.65%\n"
        "拦截成效\n"
        "考核预警（近5日）\n未及时转出量\n0 票\n预计考核金额\n0 元"
    )
    # 形态 B：表格识别失败，一格一行，且列顺序被打散
    FORM_B = (
        "@所有人拦截跟单日报\n"
        "时间 2026/10/17 · 浙江杭州萧山区大江东公司\n"
        "跟单概况\n"
        "指标\n"
        "跟单任务数\n10/13\n30\n10/14\n21\n10/15\n23\n10/16\n20\n10/17\n17\n"
        "指标\n"
        "任务完成数\n10/13\n28\n10/14\n19\n10/15\n22\n10/16\n19\n10/17\n3\n"
        "完成率\n93.33%\n90.48%\n95.65%\n95.00%\n17.65%\n"
        "拦截成效\n"
        "考核预警（近5日）\n未及时转出量\n0 票\n预计考核金额\n0 元"
    )

    def test_two_forms_of_same_card_are_duplicates(self):
        self.assertTrue(is_same_message(self.FORM_A, self.FORM_B))
        self.assertTrue(is_same_message(self.FORM_B, self.FORM_A))

    def test_identical_content_is_duplicate(self):
        text = "【普通件滞留3天】374票\n截至20261016，大江东公司有374票普通件滞留满3天。"
        self.assertTrue(is_same_message(text, text))

    def test_different_numbers_are_not_duplicates(self):
        """隔天的日报格式一样、数字不同 —— 绝不能判成重复（会永久漏发）。"""
        a = self.FORM_A
        b = self.FORM_A.replace("93.33%", "88.10%").replace("30", "41")
        self.assertFalse(is_same_message(a, b))

    def test_short_messages_are_never_duplicates(self):
        """「收到」「好的」这种短消息重复出现是正常的，不能去重。"""
        self.assertFalse(is_same_message("收到", "收到"))
        self.assertFalse(is_same_message("好的 收到", "收到 好的"))

    def test_partial_is_not_whole_duplicate(self):
        """补全帧只露出长消息的半截时，不能把整条判成重复（否则补不上）。"""
        half = self.FORM_A[: len(self.FORM_A) // 3]
        self.assertFalse(is_same_message(self.FORM_A, half))

    def test_one_contained_in_other_is_not_duplicate(self):
        """单向包含不算重复：A 是 B 的前半段时，B 还带着 A 没有的信息。"""
        a = "今天还有1票拦截单没完成退单打印，辛苦在今天18点前完成任务，避免超时扣罚。"
        b = a + "\n小件员名称: 周吉祥 ｜ 票数: 1票 ｜ 单号: 777454064365026"
        self.assertFalse(is_same_message(a, b))

    def test_content_pieces_drops_separators(self):
        self.assertEqual(content_pieces("指标: 30 ｜ 21\n23"),
                         ["指标", "30", "21", "23"])


if __name__ == "__main__":
    unittest.main()
