# -*- coding: utf-8 -*-
"""转发出去的文件名要干净（2026-11-01 用户：「原文件名太乱了，理一下」）。

乱在两处：
  · 落盘名带 12 位 sha 前缀，微信里显示的就是 `6e16038a834e-查看完整月报明细（Excel）`；
  · 手机端上报名常带「（Excel）」这类格式后缀，既不是真扩展名也不好看。

整理只影响**显示与发送**：磁盘上的原文件保持 `sha前缀-原名` 不动，去重靠它。
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.service import pretty_file_name, stage_media_file  # noqa: E402


class TestPrettyFileName(unittest.TestCase):
    def test_strip_sha_prefix(self):
        self.assertEqual(
            pretty_file_name("6e16038a834e-查看完整月报明细（Excel）",
                             "C:/data/attachments/6e16038a834e-查看完整月报明细（Excel）"),
            "查看完整月报明细.xlsx")

    def test_format_tag_becomes_extension(self):
        self.assertEqual(
            pretty_file_name("查看完整月报明细（Excel）", "C:/d/a/x.xlsx"),
            "查看完整月报明细.xlsx")
        self.assertEqual(pretty_file_name("客户声音报告（PDF）", "C:/d/a/x.pdf"),
                         "客户声音报告.pdf")

    def test_real_extension_wins(self):
        """名字写 Excel、实际是 pdf —— 按真文件类型走，别骗人。"""
        got = pretty_file_name("明细（Excel）", "C:/d/a/x.pdf")
        self.assertTrue(got.endswith(".pdf"), got)
        # 「明细」本身是通用按钮名，按通用名规则补时间戳
        self.assertTrue(got.startswith("明细-"), got)

    def test_generic_name_gets_timestamp(self):
        """「下载附件」这种通用名一天两份会互相覆盖，也分不清是谁。"""
        got = pretty_file_name("下载附件", "C:/d/attachments/111111111111-下载附件")
        self.assertTrue(got.startswith("下载附件-"), got)
        self.assertTrue(got.endswith(".xlsx"), got)
        self.assertRegex(got, r"-\d{8}-\d{4}\.xlsx$")

    def test_illegal_chars_replaced(self):
        self.assertEqual(pretty_file_name("a/b:c*d?e", "C:/d/a/x.xlsx"),
                         "a_b_c_d_e.xlsx")

    def test_long_english_name_kept(self):
        """英文长名（74 字符）截了反而更难认 —— 阈值要放得下它。"""
        name = ("2026-11-01_311392_intercept_risk_center_not_arrived_"
                "1793491732445.xlsx")
        self.assertEqual(pretty_file_name(name, "C:/d/a/x.xlsx"), name)

    def test_overlong_name_truncated_with_ext(self):
        got = pretty_file_name("长" * 200, "C:/d/a/x.xlsx")
        self.assertTrue(got.endswith(".xlsx"))
        self.assertLessEqual(len(got), 80)

    def test_missing_extension_filled(self):
        self.assertEqual(pretty_file_name("运单明细", "C:/d/attachments/2-运单明细"),
                         "运单明细.xlsx")


class TestCardNameWins(unittest.TestCase):
    """消息里那条附件行写的是什么，文件名就用什么（2026-11-01 用户要求）。

    手机回传的是钉钉服务端生成的英文串
    （``2026-11-01_311392_intercept_risk_center_not_arrived_….xlsx``），
    而群里那条消息写着「明细附件：拦截风险明细.xlsx」—— 用户要后者。
    """

    PHONE = ("2026-11-01_311392_intercept_risk_center_not_arrived_"
             "1793513190555.xlsx")

    def test_label_prefix_stripped(self):
        self.assertEqual(
            pretty_file_name(self.PHONE, "C:/d/attachments/" + self.PHONE,
                             prefer="明细附件：拦截风险明细.xlsx"),
            "拦截风险明细.xlsx")

    def test_bare_card_name_used(self):
        self.assertEqual(
            pretty_file_name(self.PHONE, "C:/d/a/x.xlsx",
                             prefer="拦截风险明细.xlsx"),
            "拦截风险明细.xlsx")

    def test_action_verb_stripped(self):
        self.assertEqual(
            pretty_file_name(self.PHONE, "C:/d/a/x.xlsx",
                             prefer="下载3天滞留运单明细"),
            "3天滞留运单明细.xlsx")
        self.assertEqual(
            pretty_file_name(self.PHONE, "C:/d/a/x.xlsx",
                             prefer="查看完整月报明细"),
            "月报明细.xlsx")

    def test_pure_button_text_falls_back(self):
        """「下载附件」这种纯按钮文案当文件名只会更乱 —— 退回手机名规则。"""
        for bad in ("下载附件", "附件：", "明细", "点击查看", "明细附件"):
            got = pretty_file_name(self.PHONE, "C:/d/a/x.xlsx", prefer=bad)
            self.assertEqual(got, self.PHONE, bad)

    def test_real_extension_wins_over_card(self):
        """卡片写 .xlsx、实际下来的是 .csv —— 按真文件类型走。"""
        got = pretty_file_name("x.bin", "C:/d/a/real.csv",
                               prefer="拦截风险明细.xlsx")
        self.assertEqual(got, "拦截风险明细.csv")

    def test_no_prefer_keeps_old_behaviour(self):
        self.assertEqual(pretty_file_name("运单明细", "C:/d/a/2-运单明细"),
                         "运单明细.xlsx")


class TestStageMediaFile(unittest.TestCase):
    def test_copy_gets_clean_name_and_same_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            att = Path(td) / "attachments"
            att.mkdir()
            src = att / "abc123def456-明细（Excel）"
            src.write_bytes(b"hello-xlsx")
            out = stage_media_file(str(src), "明细.xlsx")
            self.assertEqual(Path(out).name, "明细.xlsx")
            self.assertEqual(Path(out).read_bytes(), b"hello-xlsx")
            # 原文件必须原地不动（去重路径靠它）
            self.assertTrue(src.is_file())
            self.assertEqual(Path(out).parent.name, "outbox")


if __name__ == "__main__":
    unittest.main()
