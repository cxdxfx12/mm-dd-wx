# -*- coding: utf-8 -*-
"""附件名提取的两个「取错名」根因（2026-11-05 现场）。

当天 5 次下单只成功 1 次，日志里 13:03/13:05 两次的名字是**卡片小标题**：

    发现附件「以下为签收进度最低的TOP5承包区（按签收进度从低到高排序）：」

真因：``_attach_re`` 里的分隔符写的是 ``[\\s：:]*``，而 ``\\s`` 含换行 ——
「下载附件」独占一行时会**跨行**把下一条标题当文件名吃掉。手机端按这个
名字 contains() 找不到节点，每个候选白等 35 秒（一次下单实测烧 130~170 秒），
队列被堵住，同一张卡里真正的按钮反而没进候选链（第 2 步命中就 return 了）。

第二个坑同源：标题/章节号/说明句在形态上跟按钮一样「短、不换行」，只靠
正则永远分不出来 —— 现在按「像不像可点行」再筛一道（_plausible_attach_name）。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.sources.android_gateway import (  # noqa: E402
    _attach_cands, _attach_name, _attach_names, _clean_attach_keys,
    _plausible_attach_name)

KEYS = _clean_attach_keys("附件：,附件:,明细：,明细:,明细附件,查看完整,"
                          "明细运单,运单明细,下载附件")

# 2026-11-05 13:03 那张「一阶段签收进度播报」的真实尾巴（节选，逐行保留）
CARD = "\n".join([
    "@所有人一阶段签收进度播报",
    "所有小件员未签信息我已经整理成Excel附件，请立即催签！",
    "下载附件",
    "以下为签收进度最低的TOP5承包区（按签收进度从低到高排序）：",
    "表格",
    "统计日期",
    "2026-11-04",
    "所有承包区签收明细我已经整理成Excel附件：",
    "下载附件",
    "✅ 3. 后续行动",
    "● 已通知上述 相关小件员 本次不达标情况。",
])


class TestNoCrossLineCapture(unittest.TestCase):
    """名字绝不允许跨行 —— 跨一行的代价是 35 秒 × 候选数。"""

    def test_heading_after_button_is_not_a_name(self):
        content = ("下载附件\n"
                   "以下为签收进度最低的TOP5承包区（按签收进度从低到高排序）：")
        self.assertEqual(_attach_names(content, KEYS),
                         ["下载附件"],
                         "跨行把标题吃成名字 → 手机端永远找不到")

    def test_real_card_names(self):
        """真卡片：两个「下载附件」按钮 + 两段标题 + 一句说明。"""
        self.assertEqual(_attach_names(CARD, KEYS), ["下载附件"])
        self.assertEqual(_attach_name(CARD, KEYS), "下载附件")

    def test_section_heading_not_a_name(self):
        self.assertEqual(_attach_names("下载附件\n✅ 3. 后续行动", KEYS),
                         ["下载附件"])
        self.assertEqual(_attach_names("下载附件\n📊1. 二阶段签收情况", KEYS),
                         ["下载附件"])

    def test_same_line_still_works(self):
        """同行的正常形态不能跟着变严 —— 这是绝大多数卡片的形态。"""
        self.assertEqual(_attach_names("附件：运单明细", KEYS), ["运单明细"])
        self.assertEqual(_attach_names("明细附件：拦截风险明细.xlsx", KEYS),
                         ["拦截风险明细.xlsx"])
        self.assertEqual(_attach_names("明细：下载3天滞留运单明细", KEYS),
                         ["下载3天滞留运单明细"])

    def test_key_alone_then_name_next_line(self):
        """「附件：」独占一行、名字在下一行 —— 这个跨行是合法的。"""
        self.assertEqual(_attach_names("附件：\n未及时转出明细.xlsx", KEYS),
                         ["未及时转出明细.xlsx"])
        self.assertEqual(_attach_names("附件：\n\n运单明细", KEYS), ["运单明细"])

    def test_key_alone_then_heading_rejected(self):
        """独占一行的字眼下一条是标题 → 不猜（宁可不下单也别白等两分钟）。"""
        self.assertEqual(_attach_names("附件：\n以下为签收进度最低的TOP5承包区：",
                                       KEYS), [])


class TestPlausibleName(unittest.TestCase):
    """「像不像屏上能点到的那一行」。"""

    def test_rejects_headings(self):
        for bad in ("以下为签收进度最低的TOP5承包区（按签收进度从低到高排序）：",
                    "所有承包区签收明细我已经整理成Excel附件：",
                    "✅ 3. 后续行动",
                    "📊1. 二阶段签收情况",
                    "今日巡检发现异常，麻烦你结合下方明细判断一下"):
            self.assertFalse(_plausible_attach_name(bad), bad)

    def test_accepts_buttons(self):
        for ok in ("下载附件", "运单明细", "查看完整月报明细（Excel）",
                   "明细附件：拦截风险明细.xlsx", "下载3天滞留运单明细",
                   "📦 猫超件明细（12 票）"):
            self.assertTrue(_plausible_attach_name(ok), ok)


class TestRealButtonAlwaysInCandidates(unittest.TestCase):
    """真按钮必须永远在候选链里（2026-11-05 的「名字取错」正是把它挤掉了）。"""

    def test_download_button_appended(self):
        cands = _attach_cands(CARD, KEYS)
        self.assertEqual(cands[0], "下载附件")
        self.assertIn("下载附件", cands)

    def test_more_specific_candidate_first(self):
        """更具体的候选仍排前面：泛按钮只做兜底，不能抢先点错行。"""
        card = "查看完整履约失败明细（Excel）\n下载附件"
        cands = _attach_cands(card, KEYS)
        self.assertEqual(cands[0], "查看完整履约失败明细（Excel）")
        self.assertIn("下载附件", cands)

    def test_no_button_no_extra_candidate(self):
        cands = _attach_cands("附件：运单明细", KEYS)
        self.assertEqual(cands, ["运单明细"])


if __name__ == "__main__":
    unittest.main()
