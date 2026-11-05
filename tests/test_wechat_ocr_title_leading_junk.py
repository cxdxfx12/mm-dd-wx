# -*- coding: utf-8 -*-
"""标题栏前导杂字符不得挡住截断感知判据（2026-11-05 14:22 投递连挂）。

现象：目标群「申通五星业务员群（重点抓散单）」明明已切过去，标题栏 OCR
稳定读成「X 申通五星业务员群 (重... (86)」—— 微信标题栏左侧的 ×（关闭/
清除按钮）被 OCR 读成前导 X，``head_of`` 只剥尾部残渣，前缀比对从第一
个字符就错位，每次判失败，重试循环空转半小时。

修法：head_of 同时剥**头部**杂字符（x/×/+ 等）；t[k:]（k≤2）放宽对齐，
防「群名以双 Latin 字母开头且杂字符被剥」的连带场景。
防误配红线：**培训群必须继续被拒**（发错群比投递失败严重）。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.targets.wechat_ocr import WeChatOcrTarget

MATCH = WeChatOcrTarget._title_matches
TGT = "申通五星业务员群（重点抓散单）"


class TestLeadingJunkTitle(unittest.TestCase):
    def test_x_prefix_truncated_title_is_target(self):
        """事故原串：前导 X + 截断 + 人数后缀 —— 必须命中。"""
        self.assertTrue(MATCH(TGT, "X 申通五星业务员群 (重... (86)"))

    def test_x_prefix_count_ocr_as_1(self):
        """同轮事故变体：人数 (86) 被 OCR 读成 1。"""
        self.assertTrue(MATCH(TGT, "X 申通五星业务员群 (重... 1"))

    def test_no_x_prefix_baseline_still_matches(self):
        self.assertTrue(MATCH(TGT, "申通五星业务员群 (重... (86)"))

    def test_wrong_chat_with_x_prefix_rejected(self):
        """前导 X 剥掉后名字对不上 → 必须继续拒绝（防发错群）。"""
        self.assertFalse(MATCH(TGT, "X 申通五星业务员培训群 (12)"))
        self.assertFalse(MATCH(TGT, "申通五星业务员培训群"))

    def test_other_chat_rows_rejected(self):
        """事故日志里点偏的那两条 —— 永远不许放行。"""
        self.assertFalse(MATCH(TGT, "快递查件(25) +"))
        self.assertFalse(MATCH(TGT, "+ 陈信艳"))

    def test_x_prefixed_short_group_rejected(self):
        """x 开头的短群名（剥掉后不足 6 字门槛）保持拒绝，不误配。"""
        self.assertFalse(MATCH(TGT, "x小分队 (8)"))

    def test_double_latin_prefix_group(self):
        """群名以 XX 开头：头部杂字符被剥后 t[2:] 接住。"""
        self.assertTrue(MATCH("XX快运合作群（散单）",
                              "XX快运合作群（散... (45)"))
        self.assertTrue(MATCH("XX快运合作群（散单）",
                              "X X快运合作群（散... (45)"))

    def test_latin_prefix_group_not_confused_with_shorter_name(self):
        """剥杂字符不许把「XX快运合作群」混同于「快运合作群」。"""
        self.assertFalse(MATCH("XX快运合作群（散单）", "快运合作群 (12)"))

    def test_empty_title_rejected(self):
        self.assertFalse(MATCH(TGT, "标题栏无文字"))
        self.assertFalse(MATCH(TGT, ""))


if __name__ == "__main__":
    unittest.main()
