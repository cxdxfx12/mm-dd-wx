"""锚点丢失 / 新消息漏转 的离线回归测试。

背景（2026-10-16 线上）：
    群里的消息以「整张表格」为主，一屏只装得下 1~2 条。新消息一到，上一条被
    **整条顶出视口** → 前后两帧内容零重叠 → 锚点找不到 → 旧逻辑整帧丢弃并
    重建基线 → 这条新消息变成「历史」，**永久不再转发**。日志表现为
    「锚点丢失（聊天区可能被滚动）：本帧不转发，已重建基线」反复刷屏。

修复思路：
    1. 钉钉消息区滚动条（ddesign::SmoothScrollBar）通过 RangeValuePattern 暴露
       精确滚动偏移，贴底时 ``Value == Maximum`` —— 据此判断「屏幕上是不是
       最新消息」，零重叠时不再盲丢；
    2. 两帧都贴底时，底部下移的像素数 Δ 就是「新到达内容的高度」，
       新内容占当前可见内容最下面的 Δ 像素，据此切出新消息；
    3. 锚点比对从「指纹精确相等」升级为「相似度匹配」，容忍 OCR 错字，
       但**数字必须完全一致**（否则两张单号不同的相似表格会被吞掉）；
    4. 视口未贴底（用户在翻历史）时跳过本帧且**不覆盖基线**。

本文件只测离线纯逻辑与 `_poll_once` 的编排，不依赖真实窗口/OCR。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.sources.dingtalk_ocr import (        # noqa: E402
    DingTalkOCRSource, at_bottom, scroll_offset, units_match,
    pick_new_units, unit_fp, _pick_with_units, UNIT_GAP, AT_BOTTOM_TOL,
)


# ---------------------------------------------------------------- 造数据


def unit(content, sender="张三", y0=0, y1=None):
    return {"sender": sender, "content": content, "is_self": False,
            "time": "", "y0": y0, "y1": y0 + 30 if y1 is None else y1}


def fps_of(units):
    return [unit_fp(u) for u in units]


def pick(units, prev, **kw):
    """走生产路径（带模糊容错）。"""
    return pick_new_units(units, fps_of(units), None, prev_units=prev, **kw)


def blk(x0, y0, x1, y1, text, kind="content"):
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "text": text,
            "kind": kind, "conf": 0.9}


def frame_of(msgs, scroll=(0.0, 0.0), area_h=744, area_w=1088,
             title=("【数字质控】浙江杭州萧山区大江东公司",)):
    """用 (sender, content, y0) 列表拼一个 _poll_once 能吃的帧。

    每条消息 = 一个发送者行 + 一个正文行；消息之间留足间距以触发分组。
    """
    blocks = []
    y = 20
    for sender, content, y0 in msgs:
        if sender:
            blocks.append(blk(40, y0, 200, y0 + 16, sender, "sender"))
            blocks.append(blk(60, y0 + 26, 1000, y0 + 44, content, "content"))
        else:
            blocks.append(blk(60, y0, 1000, y0 + 18, content, "content"))
        y = y0 + 100
    return {"area_w": area_w, "area_h": area_h, "blocks": blocks,
            "title": list(title), "scroll": scroll}


def make_source(**over):
    dt = SimpleNamespace(chat_names=["数字质控"], ui_poll_interval=5.0,
                         title_check=False, title_ratio=0.55,
                         time_filter="none", time_window="08:00-18:00",
                         anchor_guard=True, companies=[])
    dt.__dict__.update(over)
    cfg = SimpleNamespace(dingtalk=dt, wechat=SimpleNamespace(wxauto_path="."))
    return DingTalkOCRSource(cfg)


def run_poll(src, frame):
    """跑一帧，返回被转发出去的消息内容列表。"""
    out: list[str] = []
    src._grab_frame = lambda: frame                    # type: ignore[assignment]
    src._poll_once(lambda m: out.append(m.content))
    return out


# ---------------------------------------------------------------- 滚动条语义


class TestScrollSemantics(unittest.TestCase):
    """贴底判定 —— 整套修复的地基，必须先钉死。"""

    def test_at_bottom_when_value_equals_max(self):
        # 实测：贴底 -488 / -488
        self.assertTrue(at_bottom((-488.0, -488.0)))

    def test_not_at_bottom_after_scrolling_up(self):
        # 实测：向上滚 5 格后 -1088 / -488
        self.assertFalse(at_bottom((-1088.0, -488.0)))

    def test_tolerance_for_subpixel(self):
        self.assertTrue(at_bottom((-488.0, -490.0)))
        self.assertFalse(at_bottom((-488.0 - AT_BOTTOM_TOL - 1, -488.0)))

    def test_missing_scroll_is_not_bottom(self):
        # 读不到滚动条时不能当作「贴底」，否则会走几何兜底算出错误的边界
        self.assertFalse(at_bottom(None))

    def test_offset_is_positive_distance_from_top(self):
        self.assertEqual(scroll_offset((-1088.0, -488.0)), 1088.0)
        self.assertIsNone(scroll_offset(None))


# ---------------------------------------------------------------- 相似度


class TestUnitsMatch(unittest.TestCase):
    """模糊锚点的安全边界：容忍错字，但绝不把两张不同的表格混为一条。"""

    def test_identical(self):
        self.assertTrue(units_match(unit("你好"), unit("你好")))

    def test_ocr_misread_chinese_char_still_matches(self):
        # 实测 OCR 把「萧山」读成「第山」、把「质控」读成「国控」
        a = unit("【数字质控】浙江杭州萧山区大江东公司 今日票量 1234 票")
        b = unit("【数字质控】浙江杭州第山区大江东公司 今日票量 1234 票")
        self.assertTrue(units_match(a, b))

    def test_different_waybill_never_matches(self):
        """核心防吞护栏：单号（数字）不同 = 不同消息。

        群里连续发的都是「结构一样、只差单号」的表格，纯相似度会把它们
        判成同一条，然后把新表格静默吞掉 —— 比重复转发严重得多。
        """
        a = unit("770351878256570 未按【送货上门】签收\n刘阳(1票)")
        b = unit("770352047266080 未按【送货上门】签收\n刘阳(1票)")
        self.assertFalse(units_match(a, b))

    def test_needs_longest_digit_sequence_to_match(self):
        a = unit("网点汇总 1736/1645/91 履约率 94.76%")
        b = unit("网点汇总 1736/1645/92 履约率 94.76%")
        self.assertFalse(units_match(a, b))

    def test_different_sender_lowers_similarity(self):
        a = unit("今天的报表已发出，请查收", sender="张三")
        b = unit("今天的报表已发出，请查收", sender="李四")
        self.assertFalse(units_match(a, b))     # 内容全同但发送者不同 = 不同消息

    def test_very_different_content(self):
        self.assertFalse(units_match(unit("报表已发出"), unit("请尽快处理超时件")))

    def test_truncated_ocr_is_tolerated(self):
        a = unit("以下1票包裹已完成退单打印任务，中心尚未到件，请及时转出，避免超时！")
        b = unit("以下1票包裹已完成退单打印任务，中心尚未到件，请及时转出")
        self.assertTrue(units_match(a, b))


# ---------------------------------------------------------------- 模糊锚点


class TestFuzzyAnchor(unittest.TestCase):
    def test_zero_overlap_returns_none(self):
        prev = [unit("C"), unit("D"), unit("E")]
        cur = [unit("X"), unit("Y"), unit("Z")]
        picked, _ = pick(cur, prev)
        self.assertIsNone(picked)

    def test_new_message_below_anchor(self):
        prev = [unit("报表A 1234"), unit("报表B 5678")]
        cur = prev + [unit("报表C 9012")]
        picked, _ = pick(cur, prev)
        self.assertEqual([cur[i]["content"] for i in picked], ["报表C 9012"])

    def test_rolled_in_history_ignored(self):
        prev = [unit("报表B 5678"), unit("报表C 9012")]
        cur = [unit("报表A 1234")] + prev
        picked, _ = pick(cur, prev)
        self.assertEqual(picked, [])

    def test_jitter_on_newest_does_not_duplicate(self):
        """OCR 抖动改写了最后一条的一个汉字：它不该被当成新消息再发一次。"""
        prev = [unit("报表B 5678"), unit("【数字质控】浙江杭州萧山区大江东公司 今日汇总")]
        cur = [unit("报表B 5678"),
               unit("【数字质控】浙江杭州第山区大江东公司 今日汇总"),
               unit("报表D 3456")]
        picked, _ = pick(cur, prev)
        self.assertEqual([cur[i]["content"] for i in picked], ["报表D 3456"])

    def test_all_jittered_still_anchors(self):
        prev = [unit("报表B 5678"), unit("报表C 9012")]
        cur = [unit("报表B 5678 "), unit("报表C 9012 ")]
        picked, _ = pick(cur, prev)
        self.assertEqual(picked, [])

    def test_repeated_identical_content(self):
        prev = [unit("同样的话"), unit("同样的话")]
        cur = [unit("同样的话"), unit("同样的话"), unit("同样的话")]
        picked, _ = pick(cur, prev)
        self.assertEqual(picked, [2])

    def test_fuzzy_path_agrees_with_exact_path(self):
        """与老的精确版结论一致 —— 保证不是「换了套逻辑各说各话」。"""
        cases = [
            ([unit("C"), unit("D"), unit("E")], [unit("C"), unit("D"), unit("E"), unit("F")]),
            ([unit("C"), unit("D"), unit("E")], [unit("B"), unit("C"), unit("D"), unit("E")]),
            ([unit("C"), unit("D"), unit("E")], [unit("B"), unit("C"), unit("D"), unit("E"), unit("F")]),
            ([unit("X"), unit("X")], [unit("X"), unit("X"), unit("X")]),
        ]
        for prev, cur in cases:
            exact, _ = pick_new_units(cur, fps_of(cur), fps_of(prev))
            fuzzy, _ = _pick_with_units(cur, fps_of(cur), prev)
            self.assertEqual(exact, fuzzy, f"{prev} -> {cur}")

    def test_guard_off_is_pure_diff(self):
        prev = [unit("报表B 5678"), unit("报表C 9012")]
        cur = [unit("报表A 1234")] + prev
        picked, _ = pick(cur, prev, anchor_guard=False)
        self.assertEqual([cur[i]["content"] for i in picked], ["报表A 1234"])


# ---------------------------------------------------------------- 零重叠恢复


class TestRecoverLostAnchor(unittest.TestCase):
    """零重叠时靠滚动条几何切分新内容。"""

    def _src_with_baseline(self, msgs, scroll, area_h=744):
        src = make_source()
        f = frame_of(msgs, scroll=scroll, area_h=area_h)
        src._grab_frame = lambda: f                      # type: ignore[assignment]
        src._poll_once(lambda m: None)                   # 建立基线
        return src

    def test_tall_new_message_fills_viewport(self):
        """本群主要故障：新表格高到把上一条整条顶出视口（零重叠）。"""
        src = self._src_with_baseline([("甲", "旧表格 1111", 20)], (-488.0, -488.0))
        # 新内容高 700px → 底部下移 700 → 可见内容几乎整屏都是新的
        f = frame_of([("乙", "新表格 2222", 20)], scroll=(-1188.0, -1188.0))
        out = run_poll(src, f)
        self.assertEqual(out, ["新表格 2222"])

    def test_only_bottom_delta_is_new(self):
        """Δ=200px → 只有最下面 200px 的内容算新。"""
        src = self._src_with_baseline([("甲", "老消息A 1111", 20),
                                       ("乙", "老消息B 2222", 120)], (-488.0, -488.0))
        # 视口底部为 664：老消息B 上移到 380~404，新消息占 620~664
        f = frame_of([("乙", "老消息B 2222", 380),
                      ("丙", "新消息C 3333", 620)], scroll=(-688.0, -688.0))
        out = run_poll(src, f)
        self.assertEqual(out, ["新消息C 3333"])

    def test_zero_growth_forwards_nothing(self):
        src = self._src_with_baseline([("甲", "老消息A 1111", 20)], (-488.0, -488.0))
        f = frame_of([("甲", "老消息A 1111", 20)], scroll=(-488.0, -488.0))
        self.assertEqual(run_poll(src, f), [])

    def test_scroll_up_is_skipped_and_keeps_baseline(self):
        """用户往上翻历史：不转发，且基线保留 —— 后续滚回底部仍能算出新消息。"""
        src = self._src_with_baseline([("甲", "老消息A 1111", 20)], (-488.0, -488.0))
        # 向上滚：Value 变 -1088，Maximum 不变（实测语义）
        hist = frame_of([("甲", "历史消息Z 9999", 20)], scroll=(-1088.0, -488.0))
        self.assertEqual(run_poll(src, hist), [])
        # 滚回底部，期间新增 300px 内容 → 应把漏掉的新消息补发出来
        back = frame_of([("甲", "历史消息Z 9999", 20),
                         ("乙", "新消息D 4444", 430)], scroll=(-788.0, -788.0))
        self.assertEqual(run_poll(src, back), ["新消息D 4444"])

    def test_area_resize_invalidates_reference(self):
        src = self._src_with_baseline([("甲", "老消息A 1111", 20)], (-488.0, -488.0))
        f = frame_of([("乙", "新消息 2222", 20)], scroll=(-1188.0, -1188.0), area_h=900)
        self.assertEqual(run_poll(src, f), [])           # 高度变了 → 不冒险

    def test_negative_growth_invalidates_reference(self):
        """内容反而变短（消息被撤回 / 滚动条语义跳变）：参照失效，不冒险。"""
        src = self._src_with_baseline([("甲", "老消息A 1111", 20)], (-800.0, -800.0))
        f = frame_of([("乙", "新消息 2222", 20)], scroll=(-300.0, -300.0))
        self.assertEqual(run_poll(src, f), [])


# ------------------------------------------------------- _poll_once 端到端


class TestPollOnceIntegration(unittest.TestCase):
    def test_baseline_then_new_message(self):
        src = make_source()
        f1 = frame_of([("甲", "第一条 1111", 20)], scroll=(-488.0, -488.0))
        self.assertEqual(run_poll(src, f1), [])          # 基线帧不倒灌
        f2 = frame_of([("甲", "第一条 1111", 20),
                       ("乙", "第二条 2222", 120)], scroll=(-588.0, -588.0))
        self.assertEqual(run_poll(src, f2), ["第二条 2222"])

    def test_lost_anchor_no_longer_swallows_message(self):
        """线上回归：新表格把上一条顶出视口后，新消息必须仍然转发。"""
        src = make_source()
        run_poll(src, frame_of([("甲", "旧表格 1111", 20)], scroll=(-488.0, -488.0)))
        out = run_poll(src, frame_of([("乙", "新表格 2222", 20)],
                                     scroll=(-1188.0, -1188.0)))
        self.assertEqual(out, ["新表格 2222"])

    def test_no_duplicate_when_nothing_changes(self):
        src = make_source()
        f = frame_of([("甲", "老消息 1111", 20)], scroll=(-488.0, -488.0))
        run_poll(src, f)
        self.assertEqual(run_poll(src, f), [])
        self.assertEqual(run_poll(src, f), [])

    def test_missing_scrollbar_degrades_gracefully(self):
        """读不到滚动条时退回老逻辑：有重叠照常工作，零重叠仍保守丢弃。"""
        src = make_source()
        f1 = frame_of([("甲", "第一条 1111", 20)], scroll=None)
        self.assertEqual(run_poll(src, f1), [])
        f2 = frame_of([("甲", "第一条 1111", 20), ("乙", "第二条 2222", 120)],
                      scroll=None)
        self.assertEqual(run_poll(src, f2), ["第二条 2222"])
        f3 = frame_of([("丙", "完全换了 3333", 20)], scroll=None)
        self.assertEqual(run_poll(src, f3), [])          # 保守：不冒险

    def test_self_messages_still_forwarded(self):
        src = make_source()
        run_poll(src, frame_of([("甲", "别人 1111", 20)], scroll=(-488.0, -488.0)))
        f = frame_of([("甲", "别人 1111", 20),
                      ("", "我发的 2222", 700)], scroll=(-588.0, -588.0),
                     area_w=1088)
        out = run_poll(src, f)
        self.assertEqual(out, ["我发的 2222"])

    def test_empty_frame_does_not_clear_baseline(self):
        """OCR 全空的帧（整屏图片/表情）不能清空基线 —— 否则下一帧整屏会被倒灌。"""
        src = make_source()
        run_poll(src, frame_of([("甲", "老消息 1111", 20)], scroll=(-488.0, -488.0)))
        empty = {"area_w": 1088, "area_h": 744, "blocks": [], "title": [],
                 "scroll": (-488.0, -488.0)}
        self.assertEqual(run_poll(src, empty), [])
        self.assertEqual(run_poll(src, frame_of([("甲", "老消息 1111", 20)],
                                                scroll=(-488.0, -488.0))), [])


class TestChatSwitchRecover(unittest.TestCase):
    """用户跑去别的群一圈再切回：离开期间到达的消息要补回来。

    线上日志里的真实序列：18:13:31 标题读到「浙江散单质控专项群」（切走了）
    → 18:15:01 标题重新匹配（切回来）→ 基线重建。旧逻辑在这一刻把离开期间
    到达的消息全判成「历史」，一条都不发。
    """

    TARGET_TITLE = ("【数字质控】浙江杭州萧山区大江东公司",)
    OTHER_TITLE = ("浙江散单质控专项群",)

    def _src(self):
        src = make_source(title_check=True)
        return src

    def test_missed_messages_recovered_after_switch_back(self):
        src = self._src()
        f1 = frame_of([("甲", "老消息 1111", 400)], scroll=(-488.0, -488.0),
                      title=self.TARGET_TITLE)
        self.assertEqual(run_poll(src, f1), [])            # 基线

        away = frame_of([("乙", "别的群在聊 2222", 20)], scroll=(-900.0, -900.0),
                        title=self.OTHER_TITLE)
        self.assertEqual(run_poll(src, away), [])          # 标题护栏拦下

        # 切回目标群：期间新增 300px 内容 → 应补发新消息、且不重发老消息
        back = frame_of([("甲", "老消息 1111", 100),
                         ("乙", "离开期间的新消息 3333", 600)],
                        scroll=(-788.0, -788.0), title=self.TARGET_TITLE)
        self.assertEqual(run_poll(src, back), ["离开期间的新消息 3333"])

    def test_nothing_new_after_switch_back(self):
        src = self._src()
        run_poll(src, frame_of([("甲", "老消息 1111", 400)], scroll=(-488.0, -488.0),
                               title=self.TARGET_TITLE))
        run_poll(src, frame_of([("乙", "别的群在聊 2222", 20)], scroll=(-900.0, -900.0),
                               title=self.OTHER_TITLE))
        back = frame_of([("甲", "老消息 1111", 400)], scroll=(-488.0, -488.0),
                        title=self.TARGET_TITLE)
        self.assertEqual(run_poll(src, back), [])          # 底部没下移 → 什么都不发

    def test_first_frame_never_backfills_history(self):
        """程序刚启动时绝不能把当前屏幕当新消息倒灌。"""
        src = self._src()
        f = frame_of([("甲", "历史消息 1111", 400)], scroll=(-2000.0, -488.0),
                     title=self.TARGET_TITLE)
        self.assertEqual(run_poll(src, f), [])


if __name__ == "__main__":
    unittest.main()
