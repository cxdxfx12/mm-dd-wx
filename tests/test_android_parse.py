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

# 聊天页底部的输入区。**页面判定靠它**：@ 面板、群信息、搜索页这些「也是带一堆
# 文本的滚动列表」的页面没有输入框，靠这一条把它们与聊天页区分开。
# 造树工具一律带上它 —— 不带就不是聊天页，解析器（正确地）一帧都不切。
INPUT_BAR = (
    f'<node index="70" text="" class="android.widget.LinearLayout" package="{PKG}" '
    f'bounds="[0,2180][1176,2300]">'
    f'<node index="71" text="发消息或按住说话..." class="android.widget.TextView" '
    f'package="{PKG}" bounds="[182,2191][981,2262]"></node></node>'
)


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
        f'{body}</node>'
        f'{INPUT_BAR}</node></hierarchy>'
    )


# ------------------------------------------------------ 真机结构（Mate Xs 2 实测）
# 下面这套造树工具复刻的是**真机截图**里的结构，与上面的合成树刻意不同：
#   ListView › 组(时间条 + 若干条消息) › RelativeLayout 消息行
#                                   › [头像, CheckBox, 昵称块, 气泡, 零宽时间]
# 这一层假设随钉钉升级变，所以必须有回归锁 —— 变的当天就要能立刻看出来。

def dt_message(sender: str, lines: list[str], top: int, *,
               hidden_time: str = "", broken_bounds: bool = False) -> str:
    """一条消息行。``broken_bounds`` 复刻真机上深层子节点 bounds 失真的情况。

    实测规律：行的 bounds 是准的，但行内子节点会出现「top 恒等于列表顶部、
    bottom 是负数」的乱码（例如 ``[212,265][964,-929]``）。解析不能依赖它们。
    """
    h = 120 + 60 * len(lines)

    def bbox(left: int, y: int, right: int, bottom: int) -> str:
        if broken_bounds:
            return f'[212,265][964,{y - 1000}]'
        return f'[{left},{y}][{right},{bottom}]'

    bubble = "".join(
        f'<node index="9" text="{t}" class="android.widget.TextView" package="{PKG}" '
        f'bounds="{bbox(212, top + 90 + 60 * i, 964, top + 150 + 60 * i)}"></node>'
        for i, t in enumerate(lines))
    hidden = (
        f'<node index="10" text="{hidden_time}" class="android.widget.TextView" '
        f'package="{PKG}" bounds="[1176,{top + 40}][1176,{top + 80}]"></node>'
        if hidden_time else "")
    return (
        f'<node index="1" text="" class="android.widget.RelativeLayout" package="{PKG}" '
        f'bounds="[0,{top}][1176,{top + h}]">'
        f'<node index="2" text="" class="android.widget.FrameLayout" package="{PKG}" '
        f'bounds="[9,{top}][153,{top + 144}]">'
        f'<node index="3" text="" class="android.widget.ImageView" package="{PKG}" '
        f'content-desc="{sender}" bounds="[24,{top}][137,{top + 113}]"></node></node>'
        f'<node index="4" text="" class="android.view.ViewGroup" package="{PKG}" '
        f'bounds="[178,{top + 20}][1006,{top + 64}]">'
        f'<node index="5" text="{sender}" class="android.widget.TextView" package="{PKG}" '
        f'bounds="[178,{top + 20}][406,{top + 64}]"></node>'
        f'<node index="6" text="机器人" class="android.widget.TextView" package="{PKG}" '
        f'bounds="[419,{top + 20}][536,{top + 64}]"></node></node>'
        f'<node index="7" text="" class="android.widget.FrameLayout" package="{PKG}" '
        f'bounds="[174,{top + 80}][1002,{top + h - 10}]">{bubble}'
        f'<node index="8" text="" class="android.view.View" package="{PKG}" '
        f'content-desc="复制" bounds="[541,{top + h - 40}][635,{top + h - 5}]"></node>'
        f'</node>{hidden}</node>')


def dt_item(strip: str, *msgs: str, top: int = 265, height: int = 878) -> str:
    """ListView 的一个子节点 = 一组：时间分隔条 + 若干条消息。

    ``layout_chatting_split`` 那一层的 bounds 在真机上是坏的（``[0,265][1176,-1189]``），
    这里刻意也做成坏的 —— 时间条是靠「文本全是时间 + 有宽度」认出来的，不看坐标。
    """
    return (
        f'<node index="20" text="" class="android.widget.LinearLayout" package="{PKG}" '
        f'clickable="true" bounds="[0,{top}][1176,{top + height}]">'
        f'<node index="21" text="" resource-id="{PKG}:id/layout_chatting_split" '
        f'class="android.widget.LinearLayout" package="{PKG}" '
        f'bounds="[0,{top}][1176,{top - height - 200}]">'
        f'<node index="22" text="{strip}" class="android.widget.TextView" package="{PKG}" '
        f'bounds="[473,{top}][702,{top - height - 203}]"></node></node>'
        f'{"".join(msgs)}</node>')


def dt_frame(*items: str, menu: bool = True) -> str:
    """整屏：一个列表 + 底部功能条（功能条故意做成很难缠的对手）。"""
    menu_xml = ""
    if menu:
        menu_xml = (
            f'<node index="60" text="" resource-id="{PKG}:id/native_menu_bar" '
            f'class="androidx.recyclerview.widget.RecyclerView" package="{PKG}" '
            f'scrollable="true" bounds="[13,2018][1163,2130]">' + "".join(
                f'<node index="61" text="" class="android.widget.RelativeLayout" '
                f'package="{PKG}" bounds="[13,{2018 + i * 28}][163,{2046 + i * 28}]">'
                f'<node index="62" text="{t}" class="android.widget.TextView" '
                f'package="{PKG}" bounds="[13,{2018 + i * 28}][163,{2046 + i * 28}]">'
                f'</node></node>'
                for i, t in enumerate(["员工档案", "任务进度", "使用帮助", "Group Check in"]))
            + '</node>')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<hierarchy rotation="0">'
        f'<node index="0" text="" class="android.widget.FrameLayout" package="{PKG}" '
        f'bounds="[0,0][1176,2480]">'
        f'<node index="50" text="" resource-id="{PKG}:id/list_view" '
        f'class="android.widget.ListView" package="{PKG}" scrollable="true" '
        f'bounds="[0,265][1176,2018]">{"".join(items)}</node>'
        f'{menu_xml}{INPUT_BAR}</node></hierarchy>')


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
            f'</node></node>{INPUT_BAR}</node></hierarchy>')
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


class TestRealDeviceLayout(unittest.TestCase):
    """真机结构（Mate Xs 2 / 钉钉 7.x，2026-10-17 抓的一帧）。

    这一组锁的是「按合成树写出来的假设」与真机之间的差距。真机上一屏长这样：

        ListView(list_view)
          └ LinearLayout            ← **一组**，不是一条消息
              ├ layout_chatting_split  › 「今天 08:13」
              ├ RelativeLayout      ← 一条消息
              │   ├ 头像 FrameLayout(content-desc=昵称)
              │   ├ CheckBox
              │   ├ ViewGroup › [昵称, 机器人]
              │   ├ 气泡 FrameLayout › 若干 TextView + 「复制」按钮
              │   └ 零宽 TextView「08:13」
              └ RelativeLayout      ← 同组的第二条
    """

    def test_one_item_can_hold_several_messages(self):
        """一组里两条消息要拆成两条，不能拼成一条。"""
        xml = dt_frame(dt_item("今天 08:13",
                               dt_message("拦截跟单专员", ["第一条正文"], 320),
                               dt_message("签收跟单专员", ["第二条正文"], 700)))
        f = parse_frame(xml)
        self.assertEqual(f.container, "ListView")
        self.assertEqual([u.content for u in f.units], ["第一条正文", "第二条正文"])
        self.assertEqual(f.separators, ["今天 08:13"])

    def test_bottom_menu_bar_is_not_the_message_list(self):
        """底部功能条也是一个 RecyclerView，且有 4 个带文本的子节点 —— 不能赢。"""
        xml = dt_frame(dt_item("今天 08:13",
                               dt_message("拦截跟单专员", ["正文"], 320)))
        f = parse_frame(xml)
        self.assertEqual(f.container, "ListView")
        self.assertNotIn("员工档案", [u.content for u in f.units])

    def test_sender_comes_from_avatar_and_robot_tag_stays_out(self):
        f = parse_frame(dt_frame(dt_item("今天 08:13",
                                         dt_message("签收跟单专员", ["正文"], 320))))
        unit = f.units[0]
        self.assertEqual(unit.sender, "签收跟单专员")
        self.assertEqual(unit.content, "正文")

    def test_copy_button_and_hidden_time_stay_out_of_body(self):
        """气泡上的「复制」按钮、右侧零宽时间戳都不是消息内容。"""
        f = parse_frame(dt_frame(dt_item(
            "今天 08:13",
            dt_message("签收跟单专员", ["正文第一行", "正文第二行"], 320,
                       hidden_time="08:13"))))
        self.assertEqual(len(f.units), 1)
        unit = f.units[0]
        self.assertEqual(unit.content, "正文第一行\n正文第二行")
        self.assertNotIn("复制", unit.content)
        self.assertEqual(f.separators, ["今天 08:13"])

    def test_single_line_message_body_is_not_the_nickname_block(self):
        """只有一行的消息：昵称块的文本数与正文一样多，不能挑错容器。"""
        f = parse_frame(dt_frame(dt_item("今天 08:13",
                                         dt_message("拦截跟单专员", ["374票"], 320))))
        self.assertEqual(f.units[0].content, "374票")

    def test_deep_garbage_bounds_do_not_break_splitting(self):
        """行内子节点 bounds 乱掉（top 恒为列表顶部、bottom 是负数）时仍能切对。"""
        xml = dt_frame(dt_item("今天 08:13",
                               dt_message("拦截跟单专员", ["第一行", "第二行"], 320,
                                          broken_bounds=True),
                               dt_message("签收跟单专员", ["第三行"], 900,
                                          broken_bounds=True)))
        f = parse_frame(xml)
        self.assertEqual([u.content for u in f.units], ["第一行\n第二行", "第三行"])
        self.assertEqual([u.sender for u in f.units], ["拦截跟单专员", "签收跟单专员"])

    def test_hidden_zero_width_time_is_not_a_separator(self):
        """零宽时间戳（chatting_item_hidden_time）不能被当成分隔条。"""
        f = parse_frame(dt_frame(dt_item(
            "今天 08:13", dt_message("签收跟单专员", ["正文"], 320,
                                     hidden_time="08:13"))))
        self.assertEqual(f.separators, ["今天 08:13"])
        self.assertEqual(len(f.units), 1)


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


# ------------------------------------------------------------------ 页面判定

def at_panel_frame() -> str:
    """「选择要@的人」面板 —— 真机第二帧的还原。

    这一屏与聊天页几乎一样：一个 RecyclerView，里面一排排带文本的行。差别只在
    顶部是「取消 / 选择要@的人 / 多选」+ 搜索框，**没有输入区**。不判页面就会把
    这些人名和「机器人」「群主」身份标签当成 9 条群消息转发出去。
    """
    mem = ""
    for i, (name, tag) in enumerate([
        ("浙江杭州萧山区大江东公司-刘思卫", "群主"),
        ("服务诊断专员", "机器人"),
        ("签收跟单专员", "机器人"),
        ("浙江杭州萧山区大江东公司-屈雅平", ""),
    ]):
        top = 626 + i * 188
        mem += (f'<node index="{10 + i}" text="" class="android.widget.LinearLayout" '
                f'package="{PKG}" bounds="[0,{top}][1176,{top + 188}]">'
                f'<node index="{20 + i}" text="{name}" class="android.widget.TextView" '
                f'package="{PKG}" bounds="[151,{top + 40}][901,{top + 98}]"></node>'
                f'<node index="{30 + i}" text="{tag}" class="android.widget.TextView" '
                f'package="{PKG}" bounds="[969,{top + 39}][1101,{top + 94}]"></node></node>')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<hierarchy rotation="0">'
        f'<node index="0" text="" class="android.widget.FrameLayout" package="{PKG}" '
        f'bounds="[0,0][1176,2480]">'
        f'<node index="1" text="取消" class="android.widget.TextView" package="{PKG}" '
        f'bounds="[50,332][138,420]"></node>'
        f'<node index="2" text="选择要@的人" class="android.widget.TextView" package="{PKG}" '
        f'bounds="[430,341][747,412]"></node>'
        f'<node index="3" text="多选" class="android.widget.TextView" package="{PKG}" '
        f'bounds="[972,334][1126,397]"></node>'
        f'<node index="4" text="搜索名字或群身份" class="android.widget.AutoCompleteTextView" '
        f'package="{PKG}" bounds="[156,482][1126,594]"></node>'
        f'<node index="5" text="" class="androidx.recyclerview.widget.RecyclerView" '
        f'scrollable="true" package="{PKG}" bounds="[0,626][1176,2480]">{mem}</node>'
        f'</node></hierarchy>')


class TestPageGuard(unittest.TestCase):
    """不是聊天页就一帧都不解析 —— 这类误判的后果是把人名当消息发出去。"""

    def test_at_panel_is_not_chat(self):
        f = parse_frame(at_panel_frame())
        self.assertEqual(f.page, "other")
        self.assertEqual(f.units, [], "「选择要@的人」面板被切成了消息")
        self.assertIn("选择要@的人", f.page_reason)

    def test_chat_page_detected_by_input_hint(self):
        f = parse_frame(frame(row_simple("hi", 300)))
        self.assertEqual(f.page, "chat")
        self.assertIn("发消息或按住说话", f.page_reason)

    def test_chat_page_detected_without_hint_when_toolbar_full(self):
        """输入区提示语的文案被改掉时，靠底部工具条仍能认出聊天页。"""
        bars = "".join(
            f'<node index="{80 + i}" text="{t}" class="android.widget.TextView" '
            f'package="{PKG}" bounds="[{100 + i * 100},2300][{180 + i * 100},2380]"></node>'
            for i, t in enumerate(["表情", "图片", "更多"]))
        xml = (f'<hierarchy rotation="0"><node index="0" class="android.widget.FrameLayout" '
               f'package="{PKG}" bounds="[0,0][1080,2340]">'
               f'<node index="1" class="androidx.recyclerview.widget.RecyclerView" '
               f'scrollable="true" package="{PKG}" bounds="[0,200][1080,2100]">'
               f'{row_simple("hi", 300)}</node>{bars}</node></hierarchy>')
        f = parse_frame(xml)
        self.assertEqual(f.page, "chat")
        self.assertEqual([u.content for u in f.units], ["hi"])

    def test_unknown_page_does_not_split(self):
        """既没输入区也没识别出页面 → 按「不是聊天页」处理（安全优先）。"""
        xml = (f'<hierarchy rotation="0"><node index="0" class="android.widget.FrameLayout" '
               f'package="{PKG}" bounds="[0,0][1080,2340]">'
               f'<node index="1" class="androidx.recyclerview.widget.RecyclerView" '
               f'scrollable="true" package="{PKG}" bounds="[0,200][1080,2100]">'
               f'{row_simple("hi", 300)}</node></node></hierarchy>')
        f = parse_frame(xml)
        self.assertEqual(f.page, "unknown")
        self.assertEqual(f.units, [])

    def test_group_info_page_is_not_chat(self):
        xml = (f'<hierarchy rotation="0"><node index="0" class="android.widget.FrameLayout" '
               f'package="{PKG}" bounds="[0,0][1080,2340]">'
               f'<node index="1" text="搜索名字或群身份" class="android.widget.AutoCompleteTextView" '
               f'package="{PKG}" bounds="[50,300][1030,400]"></node>'
               f'<node index="2" class="androidx.recyclerview.widget.RecyclerView" '
               f'scrollable="true" package="{PKG}" bounds="[0,420][1080,2100]">'
               f'{row_simple("张三", 500)}</node></node></hierarchy>')
        f = parse_frame(xml)
        self.assertEqual(f.page, "other")
        self.assertEqual(f.units, [])


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
