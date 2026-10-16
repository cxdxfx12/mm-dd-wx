"""OCR 解析层离线单元测试：不依赖截屏、不依赖 OCR 引擎，纯函数验证。"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.sources.dingtalk_ocr import (   # noqa: E402
    classify_block, merge_lines, group_units, DIGIT_GAP_RE,
    is_cross_day, unit_fp, pick_new_units, norm_title, partial_ratio,
)


def blk(x0, y0, x1, y1, text, kind="content"):
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "text": text,
            "kind": kind, "conf": 0.9}


def line(x0, y0, x1, y1, text, kind="content"):
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "text": text, "kind": kind}


class TestClassify(unittest.TestCase):
    AREA_W = 1087

    def test_dark_text_is_content(self):
        # 实测正文色 RGB(24,28,31)
        self.assertEqual(classify_block("你好", 27.7, (24, 28, 31),
                                        (100, 200, 400, 220), self.AREA_W), "content")

    def test_link_is_content(self):
        # 蓝色链接 RGB(0,120,215)
        self.assertEqual(classify_block("查看明细", 111.7, (0, 120, 215),
                                        (100, 200, 400, 220), self.AREA_W), "content")

    def test_orange_button_dropped(self):
        # 实测「编辑」按钮 RGB(236,104,0)
        self.assertEqual(classify_block("编辑", 113.3, (236, 104, 0),
                                        (359, 29, 394, 48), self.AREA_W), "drop")

    def test_banner_dropped(self):
        # 实测安全提醒横幅 RGB(242,81,12)
        self.assertEqual(classify_block("安全提醒", 111.7, (242, 81, 12),
                                        (423, 57, 664, 73), self.AREA_W), "drop")

    def test_copy_button_dropped_even_if_dark(self):
        # 「复制」按钮实测文字偏深，靠 UI 词表兜底
        self.assertEqual(classify_block("复制", 60, (60, 60, 60),
                                        (362, 534, 400, 550), self.AREA_W), "drop")

    def test_sender_gray_left(self):
        # 实测发送者名 RGB(110,114,116)，靠左
        self.assertEqual(classify_block("华煜", 113.3, (110, 114, 116),
                                        (58, 126, 89, 144), self.AREA_W), "sender")

    def test_time_gray_centered(self):
        # 居中 + 形似时间
        self.assertEqual(classify_block("今天11:25", 113.3, (110, 114, 116),
                                        (510, 451, 572, 468), self.AREA_W), "time")
        self.assertEqual(classify_block("28分钟前", 113.3, (110, 114, 116),
                                        (512, 593, 570, 610), self.AREA_W), "time")

    def test_gray_centered_but_not_time_shape_is_sender(self):
        # 居中但不是时间样式的灰字不能当时间戳（防止中部灰字误判）
        self.assertEqual(classify_block("未按【送货上门】签收", 113.0, (110, 114, 116),
                                        (354, 26, 504, 44), self.AREA_W), "sender")

    def test_light_meta_dropped(self):
        # 实测元数据 RGB(163,164,165)
        self.assertEqual(classify_block("未查收", 164.0, (163, 164, 165),
                                        (68, 30, 117, 48), self.AREA_W), "drop")
        self.assertEqual(classify_block("59.7KB", 164.0, (163, 164, 165),
                                        (117, 183, 200, 201), self.AREA_W), "drop")

    def test_digit_gap_fix(self):
        self.assertEqual(DIGIT_GAP_RE.sub("", "7703518547221 96"), "770351854722196")


class TestMergeLines(unittest.TestCase):
    def test_same_row_blocks_merged_sorted_by_x(self):
        blocks = [
            blk(354, 24, 504, 44, "未按签收", "content"),
            blk(88, 26, 200, 44, "李保喜", "content"),
            blk(206, 25, 350, 45, "770351839142171", "content"),
        ]
        lines = merge_lines(blocks)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["text"], "李保喜 770351839142171 未按签收")

    def test_different_rows_not_merged(self):
        blocks = [blk(88, 26, 200, 44, "A"), blk(88, 100, 200, 118, "B")]
        self.assertEqual(len(merge_lines(blocks)), 2)


class TestGroupUnits(unittest.TestCase):
    def test_time_marker_splits_messages(self):
        lines = [
            line(88, 26, 504, 44, "甲 770351839142171 未按签收", "content"),
            line(512, 100, 570, 118, "28分钟前", "time"),
            line(58, 140, 96, 158, "晟约跟单专员 机器人", "sender"),
            line(72, 175, 900, 193, "@所有人 风险提醒", "content"),
        ]
        units = group_units(lines, 1087)
        self.assertEqual(len(units), 2)
        self.assertEqual(units[0]["content"], "甲 770351839142171 未按签收")
        self.assertEqual(units[1]["sender"], "晟约跟单专员 机器人")
        self.assertEqual(units[1]["time"], "28分钟前")
        self.assertFalse(units[1]["is_self"])

    def test_sender_after_content_splits(self):
        lines = [
            line(58, 10, 200, 28, "张三", "sender"),
            line(72, 40, 400, 58, "第一条", "content"),
            line(58, 90, 200, 108, "李四", "sender"),
            line(72, 120, 400, 138, "第二条", "content"),
        ]
        units = group_units(lines, 1087)
        self.assertEqual(len(units), 2)
        self.assertEqual(units[0]["sender"], "张三")
        self.assertEqual(units[1]["sender"], "李四")

    def test_self_message_by_indent(self):
        lines = [line(700, 10, 1000, 28, "我发的", "content")]
        units = group_units(lines, 1087)
        self.assertTrue(units[0]["is_self"])
        self.assertEqual(units[0]["sender"], "")

    def test_unit_without_content_dropped(self):
        lines = [line(512, 10, 570, 28, "10:00", "time")]
        self.assertEqual(group_units(lines, 1087), [])


class TestUnitFp(unittest.TestCase):
    def test_same_content_same_fp(self):
        a = {"sender": "张三", "content": "你好"}
        b = {"sender": "张三", "content": "你好"}
        self.assertEqual(unit_fp(a), unit_fp(b))

    def test_different_sender_differs(self):
        a = {"sender": "张三", "content": "你好"}
        b = {"sender": "李四", "content": "你好"}
        self.assertNotEqual(unit_fp(a), unit_fp(b))


def unit(name, sender="张三", time=""):
    """造一个消息单元用于锚点测试。"""
    return {"sender": sender, "content": name, "is_self": False,
            "time": time, "y0": 0}


def fps_of(units):
    return [unit_fp(u) for u in units]


def contents(units, idx):
    return [units[i]["content"] for i in idx]


class TestAnchorGuard(unittest.TestCase):
    """防线 A：锚点定位。

    核心诉求：聊天区被滚动后，历史消息进入视口**绝不能**被当成新消息转发。
    """

    def test_new_message_below_anchor(self):
        prev = [unit("C"), unit("D"), unit("E")]
        cur = [unit("C"), unit("D"), unit("E"), unit("F")]
        picked, _ = pick_new_units(cur, fps_of(cur), fps_of(prev))
        self.assertEqual(contents(cur, picked), ["F"])

    def test_rolled_in_history_is_ignored(self):
        # 向上滚一点：历史消息 B 进入视口，其余不变
        prev = [unit("C"), unit("D"), unit("E")]
        cur = [unit("B"), unit("C"), unit("D"), unit("E")]
        picked, _ = pick_new_units(cur, fps_of(cur), fps_of(prev))
        self.assertEqual(picked, [])          # B 不得被当作新消息

    def test_rolled_history_plus_real_new(self):
        prev = [unit("C"), unit("D"), unit("E")]
        cur = [unit("B"), unit("C"), unit("D"), unit("E"), unit("F")]
        picked, _ = pick_new_units(cur, fps_of(cur), fps_of(prev))
        self.assertEqual(contents(cur, picked), ["F"])

    def test_large_scroll_discards_whole_frame(self):
        prev = [unit("C"), unit("D"), unit("E")]
        cur = [unit("X"), unit("Y"), unit("Z")]     # 视口内容完全换了
        picked, _ = pick_new_units(cur, fps_of(cur), fps_of(prev))
        self.assertIsNone(picked)                   # 整帧丢弃，宁可漏也不误发

    def test_ocr_jitter_still_anchors(self):
        # 最后一条因 OCR 抖动指纹变了 → 退用倒数第二条锚定，不整帧丢弃
        prev = [unit("C"), unit("D"), unit("E")]
        cur = [unit("C"), unit("D"), unit("E-ocr"), unit("F")]
        picked, _ = pick_new_units(cur, fps_of(cur), fps_of(prev))
        self.assertEqual(contents(cur, picked), ["E-ocr", "F"])

    def test_repeated_identical_content_detected(self):
        # 连发同样内容：第三条应被识别为新消息，而不是被锚点吞掉
        prev = [unit("同样的话"), unit("同样的话")]
        cur = [unit("同样的话"), unit("同样的话"), unit("同样的话")]
        picked, _ = pick_new_units(cur, fps_of(cur), fps_of(prev))
        self.assertEqual(picked, [2])

    def test_guard_off_would_misfire(self):
        # 关掉防护 = 纯增量差分：滚动进来的历史 B 会被误发（说明防护的必要性）
        prev = [unit("C"), unit("D"), unit("E")]
        cur = [unit("B"), unit("C"), unit("D"), unit("E")]
        picked, _ = pick_new_units(cur, fps_of(cur), fps_of(prev), anchor_guard=False)
        self.assertEqual(contents(cur, picked), ["B"])

    def test_empty_frame(self):
        picked, _ = pick_new_units([], [], fps_of([unit("C")]))
        self.assertEqual(picked, [])


class TestCrossDay(unittest.TestCase):
    """防线 B：跨天时间戳过滤。

    钉钉语义：同一天只显示 hh:mm，跨天才带日期前缀 → 带前缀的即非当天。
    """

    def test_no_marker_passes(self):
        self.assertFalse(is_cross_day(""))
        self.assertFalse(is_cross_day(None))

    def test_same_day_passes(self):
        for t in ("今天 11:25", "今天11:25", "11:25", "刚刚", "33分钟前", "5小时前"):
            self.assertFalse(is_cross_day(t, datetime(2026, 10, 16, 12, 0)), t)

    def test_yesterday_dropped(self):
        self.assertTrue(is_cross_day("昨天 20:09"))
        self.assertTrue(is_cross_day("昨天20:09"))
        self.assertTrue(is_cross_day("前天 09:00"))

    def test_weekday_and_date_dropped(self):
        self.assertTrue(is_cross_day("星期三 15:20"))
        self.assertTrue(is_cross_day("星期日 15:20"))
        self.assertTrue(is_cross_day("10月12日"))
        self.assertTrue(is_cross_day("10月12日 08:30"))

    def test_hours_ago_depends_on_now(self):
        # 中午看「3小时前」= 今天 09:00，仍属当天
        self.assertFalse(is_cross_day("3小时前", datetime(2026, 10, 16, 12, 0)))
        # 凌晨 1:30 看「3小时前」= 昨天 22:30，属跨天
        self.assertTrue(is_cross_day("3小时前", datetime(2026, 10, 16, 1, 30)))
        self.assertFalse(is_cross_day("1小时前", datetime(2026, 10, 16, 1, 30)))


class TestTitleMatch(unittest.TestCase):
    """标题护栏匹配（2026-10-16 线上故障的回归测试）。

    故障：标题条里群名行下面还有一行「归属于 XX集团」，整条 OCR 时
    两行被检测框合并，「【数字质控】浙江杭州萧山区大江东公司」被读成
    「学国控）新江州山区大江东公司」，整串相似度只剩 0.12，
    护栏连续 2 分钟拦掉所有帧，转发整体停摆。
    修法：多候选读法 + partial_ratio 片段匹配。
    下列乱码全部取自当日真实 OCR 输出。
    """

    NAME = "数字质控"

    def _match(self, cands):
        """用最小桩走一遍 _title_matches 的判定逻辑（不依赖窗口/OCR）。"""
        names = [self.NAME]
        cands = [c for c in (norm_title(t) for t in cands) if c]
        best = 0.0
        for n in names:
            if any(n in c for c in cands):
                return True, 1.0
            best = max(best, max((partial_ratio(n, c) for c in cands),
                                 default=0.0))
        return best >= 0.55, best

    def test_perfect_title_passes(self):
        ok, r = self._match(["【数字质控】浙江杭州萧山区大江东公司 ③ 5"])
        self.assertTrue(ok)
        self.assertEqual(r, 1.0)

    def test_typoed_title_passes(self):
        # 质→教、【】→［】、漏「萧」：片段相似度 0.75 应当通过
        ok, r = self._match(["［教字质控1浙江杭州山区大江东公司员5闵"])
        self.assertTrue(ok, f"相似度 {r:.2f}")
        self.assertGreaterEqual(r, 0.75)

    def test_two_row_merge_garble_alone_fails(self):
        # 两行合并读法的乱码：连「数」「字」都认错，单看它就该拦
        ok, r = self._match(["学国控）新江州山区大江东公司"])
        self.assertFalse(ok)
        self.assertLess(r, 0.55)

    def test_multi_candidate_picks_the_readable_one(self):
        # 同一帧的多个候选里只要有一个读对了就放行——这是修复的核心
        ok, r = self._match([
            "学国控）新江州山区大江东公司",
            "［数字质控1浙江杭州山区大江东公司员5闵",
        ])
        self.assertTrue(ok)
        self.assertEqual(r, 1.0)

    def test_unrelated_chat_fails(self):
        ok, r = self._match(["张三", "产品评审会"])
        self.assertFalse(ok)
        self.assertEqual(r, 0.0)

    def test_empty_candidates_fail(self):
        ok, _ = self._match(["", "   "])
        self.assertFalse(ok)

    def test_partial_ratio_basics(self):
        self.assertEqual(partial_ratio("数字质控", "数字质控1浙江"), 1.0)
        self.assertEqual(partial_ratio("数字质控", "教字质控】浙江"), 0.75)
        # 「国」不在配置名里，公共块只剩「控」
        self.assertEqual(partial_ratio("数字质控", "国控"), 0.25)
        self.assertEqual(partial_ratio("数字质控", "张三的会话"), 0.0)
        self.assertEqual(partial_ratio("", "任意"), 0.0)
        self.assertEqual(partial_ratio("任意", ""), 0.0)

    def test_norm_title_strips_decorations(self):
        self.assertEqual(norm_title("【数字质控】浙江-杭州 大江东"),
                         "数字质控浙江杭州大江东")


if __name__ == "__main__":
    unittest.main()
