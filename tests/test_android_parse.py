"""安卓节点树解析器（bridge/sources/android_parse.py）的回归测试。

这里锁的都是「真机上会悄悄出错、出错后很难看出来」的判据：
  · 时间分隔条不能被当消息（否则「09:12」会被转发进微信群）
  · 「昵称 + [图片]」要认成图片消息，昵称不能混进正文
  · 第一帧只建基线、一条不发（这是「不转发历史」的唯一保证）
  · 锚点丢失时宁可漏发，也不能把翻出来的历史灌进群
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from bridge.fingerprint import fp_of, norm_sender              # noqa: E402
from bridge.sources.android_parse import (                     # noqa: E402
    FrameDiffer, ParseError, Unit, parse_frame, units_to_messages,
)

PKG = "com.alibaba.android.rimet"


# ------------------------------------------------------------------ 造树工具

def row_simple(text: str, top: int, sender: str | None = None) -> str:
    """一条消息，结构按真机来：昵称是裸的 TextView，正文在气泡容器里。"""
    if sender is None:
        return (f'<node index="1" text="{text}" class="android.widget.TextView" '
                f'package="{PKG}" bounds="[80,{top}][1000,{top + 90}]"></node>')
    return (f'<node index="1" text="" class="android.widget.LinearLayout" package="{PKG}" '
            f'bounds="[60,{top}][1020,{top + 180}]">'
            f'<node index="2" text="{sender}" class="android.widget.TextView" '
            f'package="{PKG}" bounds="[80,{top}][420,{top + 60}]"></node>'
            f'<node index="3" text="" class="android.widget.LinearLayout" package="{PKG}" '
            f'bounds="[80,{top + 70}][1000,{top + 170}]">'
            f'<node index="4" text="{text}" class="android.widget.TextView" '
            f'package="{PKG}" bounds="[100,{top + 75}][980,{top + 165}]"></node>'
            f'</node></node>')


def row_flat(sender: str, body: str, top: int) -> str:
    """扁平结构：昵称与正文是同一层的两个叶子 —— 没有结构信号可依赖。"""
    return (f'<node index="1" text="" class="android.widget.LinearLayout" package="{PKG}" '
            f'bounds="[60,{top}][1020,{top + 180}]">'
            f'<node index="2" text="{sender}" class="android.widget.TextView" '
            f'package="{PKG}" bounds="[80,{top}][420,{top + 60}]"></node>'
            f'<node index="3" text="{body}" class="android.widget.TextView" '
            f'package="{PKG}" bounds="[80,{top + 70}][1000,{top + 170}]"></node>'
            f'</node>')


def row_time(text: str, top: int) -> str:
    return (f'<node index="1" text="{text}" class="android.widget.TextView" '
            f'package="{PKG}" bounds="[500,{top}][600,{top + 40}]"></node>')


def frame(*rows: str, pkg: str = PKG) -> str:
    body = "".join(rows)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<hierarchy rotation="0">'
        f'<node index="0" text="" class="android.widget.FrameLayout" package="{pkg}" '
        f'bounds="[0,0][1080,2340]">'
        f'<node index="1" text="" class="androidx.recyclerview.widget.RecyclerView" '
        f'package="{pkg}" scrollable="true" bounds="[0,200][1080,2100]">'
        f'{body}</node></node></hierarchy>'
    )


# ------------------------------------------------------------------ 解析

class TestParseFrame(unittest.TestCase):

    def test_time_separator_is_not_a_message(self):
        f = parse_frame(frame(row_time("09:12", 300),
                              row_simple("普通件滞留7天 96票", 400)))
        self.assertEqual(f.separators, ["09:12"])
        self.assertEqual([u.content for u in f.units], ["普通件滞留7天 96票"])

    def test_various_time_separators(self):
        for text in ("09:12", "昨天 21:30", "星期一", "10-16", "2026-10-16"):
            f = parse_frame(frame(row_time(text, 300), row_simple("正文", 400)))
            self.assertEqual(f.units and len(f.units), 1, text)
            self.assertEqual(f.separators, [text], text)

    def test_sender_split_above_content(self):
        f = parse_frame(frame(row_simple("普通件滞留7天 96票", 400,
                                         sender="签收跟单专员")))
        unit = f.units[0]
        self.assertEqual(unit.sender, "签收跟单专员")
        self.assertEqual(unit.content, "普通件滞留7天 96票")

    def test_single_node_row_has_no_sender(self):
        f = parse_frame(frame(row_simple("客户声音履约风险提醒", 400)))
        self.assertEqual(f.units[0].sender, "")
        self.assertEqual(f.units[0].content, "客户声音履约风险提醒")

    def test_image_with_sender_is_media_not_text(self):
        """昵称比「[图片]」长 —— 不能因此把昵称混进正文。"""
        f = parse_frame(frame(row_simple("[图片]", 400, sender="签收跟单专员")))
        unit = f.units[0]
        self.assertEqual(unit.kind, "image")
        self.assertEqual(unit.sender, "签收跟单专员")
        self.assertEqual(unit.content, "[图片]")

    def test_short_second_line_is_not_eaten_as_sender(self):
        """正文被拆成两行（第二行很短）时不能把第一行当昵称切掉。"""
        row = (f'<node index="1" text="" class="android.widget.LinearLayout" '
               f'package="{PKG}" bounds="[60,400][1020,600]">'
               f'<node index="2" text="客户声音履约风险提醒，以下订单存在投诉风险" '
               f'class="android.widget.TextView" package="{PKG}" '
               f'bounds="[80,400][1000,480]"></node>'
               f'<node index="3" text="374票" class="android.widget.TextView" '
               f'package="{PKG}" bounds="[80,490][300,560]"></node>'
               f'</node>')
        unit = parse_frame(frame(row)).units[0]
        self.assertEqual(unit.sender, "")
        self.assertEqual(unit.content,
                         "客户声音履约风险提醒，以下订单存在投诉风险\n374票")

    def test_flat_layout_with_short_body_keeps_nickname_in_content(self):
        """没有结构信号、正文又比昵称短时，**宁可把昵称留在正文里**。

        反过来（把正文第一行当昵称切掉）是真丢内容，比难看严重得多。
        这条锁住的是「保守优先」这个取舍，不是「这样最好看」。
        """
        unit = parse_frame(frame(row_flat("签收跟单专员", "374票", 400))).units[0]
        self.assertEqual(unit.sender, "")
        self.assertEqual(unit.content, "签收跟单专员\n374票")

    def test_units_are_ordered_old_to_new(self):
        xml = frame(row_simple("第三条", 900), row_simple("第一条", 300),
                    row_simple("第二条", 600))
        self.assertEqual([u.content for u in parse_frame(xml).units],
                         ["第一条", "第二条", "第三条"])

    def test_innermost_recycler_wins(self):
        """外层 ScrollView 套内层 RecyclerView 时，不能被外层空壳抢走。"""
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<hierarchy rotation="0"><node index="0" class="android.widget.FrameLayout" '
            f'package="{PKG}" bounds="[0,0][1080,2340]">'
            f'<node index="1" class="android.widget.ScrollView" scrollable="true" '
            f'package="{PKG}" bounds="[0,200][1080,2100]">'
            f'<node index="2" text="群公告" class="android.widget.TextView" package="{PKG}" '
            f'bounds="[0,200][1080,260]"></node>'
            f'<node index="3" class="androidx.recyclerview.widget.RecyclerView" scrollable="true" '
            f'package="{PKG}" bounds="[0,300][1080,2000]">'
            f'{row_simple("第一条", 320)}{row_simple("第二条", 600)}'
            f'</node></node></node></hierarchy>')
        f = parse_frame(xml)
        self.assertEqual(f.container, "RecyclerView")
        self.assertEqual([u.content for u in f.units], ["第一条", "第二条"])

    def test_empty_screen_warns_instead_of_raising(self):
        xml = (f'<hierarchy rotation="0"><node index="0" class="android.widget.FrameLayout" '
               f'package="{PKG}" bounds="[0,0][1080,2340]"></node></hierarchy>')
        f = parse_frame(xml)
        self.assertEqual(f.units, [])
        self.assertTrue(f.warnings)

    def test_wrong_package_warns(self):
        xml = ('<?xml version="1.0" encoding="UTF-8"?>'
               '<hierarchy rotation="0">'
               '<node index="0" class="android.widget.FrameLayout" '
               'package="com.huawei.android.launcher" bounds="[0,0][1080,2340]">'
               '<node index="1" text="桌面图标" class="android.widget.TextView" '
               'package="com.huawei.android.launcher" bounds="[80,400][400,480]"></node>'
               '</node></hierarchy>')
        f = parse_frame(xml)
        self.assertTrue(any("rimet" in w for w in f.warnings))

    def test_not_a_hierarchy_raises(self):
        for bad in ("", "{}", "<html><body>hi</body></html>"):
            with self.assertRaises(ParseError):
                parse_frame(bad)

    def test_malformed_xml_raises_parse_error(self):
        with self.assertRaises(ParseError):
            parse_frame('<hierarchy rotation="0"><node></hierarchy>')

    def test_no_container_returns_empty_with_warning(self):
        f = parse_frame(frame())          # 列表容器存在但没有任何子节点
        self.assertEqual(f.units, [])
        self.assertTrue(f.warnings)


class TestFingerprintDrift(unittest.TestCase):
    """解析器算的指纹必须与 OCR 路径**完全一致**，否则切换采集方式会重发。"""

    def test_same_fp_as_fingerprint_module(self):
        unit = Unit(sender="签收跟单专员 机器人", content="普通件滞留7天 96票")
        self.assertEqual(unit.fp, fp_of("签收跟单专员 机器人", "普通件滞留7天 96票"))

    def test_robot_suffix_does_not_change_fp(self):
        a = Unit(sender="签收跟单专员", content="x").fp
        b = Unit(sender="签收跟单专员 机器人", content="x").fp
        self.assertEqual(a, b)

    def test_norm_sender_only_strips_trailing_robot(self):
        self.assertEqual(norm_sender("机器人小助手"), "机器人小助手")


# ------------------------------------------------------------------ 帧差分

class TestFrameDiffer(unittest.TestCase):

    def units(self, *pairs: tuple[str, str]) -> list[Unit]:
        return [Unit(sender=s, content=c) for s, c in pairs]

    def test_first_frame_is_baseline_and_emits_nothing(self):
        d = FrameDiffer()
        diff = d.feed(self.units(("a", "1"), ("a", "2")))
        self.assertTrue(diff.baseline)
        self.assertEqual(diff.count, 0)

    def test_new_message_after_anchor_is_emitted(self):
        d = FrameDiffer()
        d.feed(self.units(("a", "1"), ("a", "2")))
        diff = d.feed(self.units(("a", "1"), ("a", "2"), ("a", "3")))
        self.assertEqual([u.content for u in diff.units], ["3"])

    def test_identical_frame_emits_nothing(self):
        d = FrameDiffer()
        base = self.units(("a", "1"), ("a", "2"))
        d.feed(base)
        self.assertEqual(d.feed(base).count, 0)

    def test_scrolling_back_does_not_replay_history(self):
        """用户往上翻：上面全是没转发过的旧消息，一条都不能发。"""
        d = FrameDiffer()
        d.feed(self.units(("a", "5"), ("a", "6")))
        diff = d.feed(self.units(("a", "1"), ("a", "2"), ("a", "3")))
        self.assertTrue(diff.anchor_lost)
        self.assertEqual(diff.count, 0)

    def test_anchor_rebuilt_after_loss(self):
        """锚点丢失后要重建，否则之后每帧都发不出去（永久静默）。"""
        d = FrameDiffer()
        d.feed(self.units(("a", "5"), ("a", "6")))
        d.feed(self.units(("a", "1"), ("a", "2")))          # 丢失 + 重建
        diff = d.feed(self.units(("a", "1"), ("a", "2"), ("a", "3")))
        self.assertEqual([u.content for u in diff.units], ["3"])

    def test_anchor_found_even_if_other_messages_left(self):
        """往上滚一点点：锚点仍在屏内，只发它下面的部分。"""
        d = FrameDiffer()
        d.feed(self.units(("a", "3"), ("a", "4")))
        diff = d.feed(self.units(("a", "1"), ("a", "3"), ("a", "4"), ("a", "5")))
        self.assertEqual([u.content for u in diff.units], ["5"])

    def test_reset_rebuilds_baseline(self):
        d = FrameDiffer()
        d.feed(self.units(("a", "1")))
        d.reset()
        diff = d.feed(self.units(("a", "1"), ("a", "2")))
        self.assertTrue(diff.baseline)
        self.assertEqual(diff.count, 0)

    def test_empty_frame_keeps_anchor(self):
        """一帧读空（切到别的 App）不能把锚点清掉。"""
        d = FrameDiffer()
        d.feed(self.units(("a", "1"), ("a", "2")))
        d.feed([])
        diff = d.feed(self.units(("a", "1"), ("a", "2"), ("a", "3")))
        self.assertEqual([u.content for u in diff.units], ["3"])


# ------------------------------------------------------------------ 单元 → 消息

class TestUnitsToMessages(unittest.TestCase):

    def test_basic_fields(self):
        msgs = units_to_messages(
            [Unit(sender="签收跟单专员", content="普通件滞留7天 96票")],
            chat_name="【数字质控】大江东", companies=["大江东"],
            now=datetime(2026, 10, 17, 9, 30))
        m = msgs[0]
        self.assertEqual(m.source, "android")
        self.assertEqual(m.chat_name, "【数字质控】大江东")
        self.assertEqual(m.sender_name, "签收跟单专员")
        self.assertEqual(m.msg_type, "text")
        self.assertEqual(m.company, "大江东")
        self.assertEqual(m.short_time, "09:30")

    def test_raw_id_stable_and_unique_per_duplicate(self):
        units = [Unit(sender="a", content="x"), Unit(sender="a", content="x")]
        msgs = units_to_messages(units, chat_name="c")
        self.assertEqual(msgs[0].raw_id, f"{fp_of('a', 'x')}:1")
        self.assertEqual(msgs[1].raw_id, f"{fp_of('a', 'x')}:2")
        self.assertNotEqual(msgs[0].uid, msgs[1].uid)

    def test_media_type_mapped(self):
        msgs = units_to_messages([Unit(sender="a", content="[图片]", kind="image")],
                                 chat_name="c")
        self.assertEqual(msgs[0].msg_type, "image")
        self.assertEqual(msgs[0].content, "[图片]")


if __name__ == "__main__":
    unittest.main()
