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

from bridge.fingerprint import fp_of, norm_sender, unit_fp
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


if __name__ == "__main__":
    unittest.main()
