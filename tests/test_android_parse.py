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
    Diff, FrameDiffer, ParseError, Unit, clipped_units, needs_scroll,
    parse_frame, units_to_messages,
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
        f'resource-id="{PKG}:id/chat_bubble_container" '
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
        # 真机上聊天页的列表被这两层包着（实测 id 就是这两个语义名，不是混淆串）。
        # 造树必须带上 —— 这是页面判定里最耐改版的一条依据。
        f'<node index="49" text="" resource-id="{PKG}:id/list_and_tip_content" '
        f'class="android.widget.FrameLayout" package="{PKG}" '
        f'bounds="[0,265][1176,2018]">'
        f'<node index="48" text="" resource-id="{PKG}:id/list_content" '
        f'class="android.widget.LinearLayout" package="{PKG}" '
        f'bounds="[0,265][1176,2018]">'
        f'<node index="50" text="" resource-id="{PKG}:id/list_view" '
        f'class="android.widget.ListView" package="{PKG}" scrollable="true" '
        f'bounds="[0,265][1176,2018]">{"".join(items)}</node>'
        f'</node></node>'
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

    # --- 被滑到屏幕边界外、只露出一截的行 ---
    #
    # 真机第三帧（13:07）实测：屏幕上第一条消息只剩 38px 高，
    # 行内所有 bounds 都塌了（top 恒为 265、bottom 是负数）。当时解析器
    # 把它切成了「签收跟单专员 / 机器人」—— 昵称被当成正文，正文整段丢了。
    # 这类行不能发（内容不可信，而且多半早就发过了）。

    def clipped_item(self) -> str:
        """复刻真机那条只露 38px 的组：组高 38，行内 bounds 全塌。"""
        return (
            f'<node index="20" text="" class="android.widget.LinearLayout" package="{PKG}" '
            f'clickable="true" bounds="[0,265][1176,303]">'
            f'<node index="21" text="今天 08:42" class="android.widget.TextView" '
            f'resource-id="{PKG}:id/chatting_time_tv" package="{PKG}" '
            f'bounds="[473,265][702,-394]"></node>'
            f'<node index="22" text="" class="android.widget.RelativeLayout" package="{PKG}" '
            f'bounds="[0,265][1176,278]">'
            f'<node index="23" text="" class="android.widget.ImageView" package="{PKG}" '
            f'content-desc="签收跟单专员" bounds="[24,265][137,-247]"></node>'
            f'<node index="24" text="" class="android.view.ViewGroup" package="{PKG}" '
            f'bounds="[178,265][1006,-316]">'
            f'<node index="25" text="签收跟单专员" class="android.widget.TextView" '
            f'package="{PKG}" bounds="[178,265][406,-316]"></node>'
            f'<node index="26" text="机器人" class="android.widget.TextView" package="{PKG}" '
            f'bounds="[419,265][536,-316]"></node></node>'
            f'<node index="27" text="" class="android.widget.FrameLayout" package="{PKG}" '
            f'resource-id="{PKG}:id/chat_bubble_container" bounds="[174,265][1002,265]">'
            f'<node index="28" text="@所有人今日出仓进度提醒" class="android.widget.TextView" '
            f'package="{PKG}" bounds="[212,265][964,-200]"></node></node></node></node>')

    def test_clipped_row_is_skipped(self):
        """只露一截的行整条丢掉 —— 切出来的正文不可信，不能发。"""
        f = parse_frame(dt_frame(self.clipped_item(),
                                 dt_item("今天 13:07",
                                         dt_message("履约跟单专员", ["正文"], 1700))))
        self.assertEqual([u.content for u in f.units], ["正文"])
        self.assertNotIn("签收跟单专员", [u.content for u in f.units])
        # 时间条照留（它只是排障信息，本身就可能只有 40px 高）
        self.assertIn("今天 08:42", f.separators)

    # --- H5 卡片展开后的「收起」按钮 ---
    #
    # 真机 2026-10-18 17:40 那帧（《履约-客户声音晚报》1200 字）：钉钉把消息
    # 包成了「时间条 + chatting_content_view_container」，卡片展开后容器里
    # 多了一个 ``LinearLayout(content-desc="收起")`` 按钮，挂在卡片**底部**
    # （bounds 1880~1968，而气泡从 265 起）。当时的发送者就是这么变成「收起」的
    # —— 库里那条记录的 sender_name 至今是「收起」，转发到微信群也显示成它。

    def expanded_card_item(self, sender: str, lines: list[str]) -> str:
        """复刻真机形状：时间条 + 内容容器（头像/昵称/气泡/收起按钮）。"""
        top, h = 265, 1400
        body = "".join(
            f'<node index="{30 + i}" text="{t}" class="android.widget.TextView" '
            f'package="{PKG}" bounds="[212,{top + 200 + 70 * i}]'
            f'[964,{top + 260 + 70 * i}]"></node>'
            for i, t in enumerate(lines))
        return (
            f'<node index="20" text="" class="android.widget.LinearLayout" package="{PKG}" '
            f'bounds="[0,{top}][1176,{top + h}]">'
            # ① 时间条（真机上它和消息在同一层，且 bounds 会塌）
            f'<node index="21" text="" class="android.widget.LinearLayout" package="{PKG}" '
            f'bounds="[0,{top}][1176,{top + h}]">'
            f'<node index="22" text="" class="android.widget.LinearLayout" package="{PKG}" '
            f'resource-id="{PKG}:id/layout_chatting_split" bounds="[0,{top}][1176,-1322]">'
            f'<node index="23" text="今天 17:33" class="android.widget.TextView" '
            f'resource-id="{PKG}:id/chatting_time_tv" package="{PKG}" '
            f'bounds="[473,{top}][702,-1325]"></node></node>'
            # ② 内容容器 —— 真机上它从列表顶部一直到底部
            f'<node index="24" text="" class="android.widget.RelativeLayout" package="{PKG}" '
            f'resource-id="{PKG}:id/chatting_content_view_container" '
            f'bounds="[0,{top}][1176,{top + h}]">'
            # 「收起」按钮：排在头像**前面**，但位置在卡片底部
            f'<node index="25" text="" class="android.widget.LinearLayout" package="{PKG}" '
            f'content-desc="收起" clickable="true" bounds="[481,{top + 1155}]'
            f'[694,{top + 1243}]">'
            f'<node index="26" text="收起" class="android.widget.TextView" package="{PKG}" '
            f'bounds="[519,{top + 1167}][605,{top + 1230}]"></node>'
            f'<node index="27" text="&#xf3c3;" class="android.widget.TextView" package="{PKG}" '
            f'bounds="[618,{top + 1177}][662,{top + 1221}]"></node></node>'
            # 头像
            f'<node index="28" text="" class="android.widget.FrameLayout" package="{PKG}" '
            f'resource-id="{PKG}:id/avatar_signal_group" bounds="[9,{top}][153,-700]">'
            f'<node index="29" text="" class="android.widget.ImageView" package="{PKG}" '
            f'content-desc="{sender}" bounds="[24,{top}][137,-700]"></node></node>'
            # 昵称 + 机器人
            f'<node index="30" text="" class="android.view.ViewGroup" package="{PKG}" '
            f'resource-id="{PKG}:id/rl_title" bounds="[178,{top}][1006,-800]">'
            f'<node index="31" text="{sender}" class="android.widget.TextView" package="{PKG}" '
            f'resource-id="{PKG}:id/chatting_title" bounds="[178,{top}][406,-800]"></node>'
            f'<node index="32" text="机器人" class="android.widget.TextView" package="{PKG}" '
            f'bounds="[419,{top}][536,-800]"></node></node>'
            # 气泡
            f'<node index="33" text="" class="android.widget.FrameLayout" package="{PKG}" '
            f'resource-id="{PKG}:id/chat_bubble_container" bounds="[174,{top}]'
            f'[1002,{top + h}]">{body}</node>'
            f'</node></node></node>')

    def test_collapse_button_is_not_the_sender(self):
        """卡片里的「收起」按钮在气泡下方，不能顶替发送者。

        2026-10-18 实测事故：这条记录入库时 sender_name 就是「收起」。
        """
        f = parse_frame(dt_frame(self.expanded_card_item(
            "履约跟单专员", ["@所有人", "履约-客户声音晚报 · 10月18日"])))
        self.assertEqual(len(f.units), 1)
        unit = f.units[0]
        self.assertEqual(unit.sender, "履约跟单专员",
                         "发送者变成了「收起」—— 卡片底部那个按钮被当成昵称了")
        self.assertNotIn("收起", unit.content)
        self.assertEqual(unit.content, "@所有人\n履约-客户声音晚报 · 10月18日")

    def test_expanded_card_still_splits_after_time_strip(self):
        """「时间条 + 内容容器」这一层要穿过去，否则气泡会被认成整屏容器。"""
        f = parse_frame(dt_frame(
            self.expanded_card_item("履约跟单专员", ["正文一"]),
            dt_item("今天 17:40",
                    dt_message("拦截跟单专员", ["下一条正文"], 1900))))
        self.assertEqual([u.sender for u in f.units],
                         ["履约跟单专员", "拦截跟单专员"])
        self.assertEqual([u.content for u in f.units], ["正文一", "下一条正文"])

    def test_bubble_found_by_id_when_bounds_collapsed(self):
        """气泡按 resource-id 认，不看面积。

        行内 bounds 塌掉时，昵称块算出来的「面积」会比气泡大（气泡的
        top == bottom，面积 0），按面积挑就把正文换成了「昵称 / 机器人」。
        """
        collapsed = (
            f'<node index="1" text="" class="android.widget.RelativeLayout" package="{PKG}" '
            f'bounds="[0,600][1176,900]">'
            f'<node index="2" text="" class="android.view.ViewGroup" package="{PKG}" '
            f'bounds="[178,265][1006,-316]">'
            f'<node index="3" text="签收跟单专员" class="android.widget.TextView" '
            f'package="{PKG}" bounds="[178,265][406,-316]"></node>'
            f'<node index="4" text="机器人" class="android.widget.TextView" package="{PKG}" '
            f'bounds="[419,265][536,-316]"></node></node>'
            f'<node index="5" text="" class="android.widget.FrameLayout" package="{PKG}" '
            f'resource-id="{PKG}:id/chat_bubble_container" bounds="[174,265][1002,265]">'
            f'<node index="6" text="出仓进度提醒" class="android.widget.TextView" '
            f'package="{PKG}" bounds="[212,265][964,-200]"></node></node></node>')
        f = parse_frame(dt_frame(dt_item("今天 08:42", collapsed)))
        self.assertEqual(len(f.units), 1)
        self.assertEqual(f.units[0].content, "出仓进度提醒")
        self.assertEqual(f.units[0].sender, "签收跟单专员")

    def test_bubble_falls_back_to_area_without_id(self):
        """树里没带 resource-id 时，仍能靠面积找到气泡（老逻辑不能丢）。"""
        no_rid = (
            f'<node index="1" text="" class="android.widget.RelativeLayout" package="{PKG}" '
            f'bounds="[0,600][1176,900]">'
            f'<node index="2" text="签收跟单专员" class="android.widget.TextView" '
            f'package="{PKG}" bounds="[178,620][406,664]"></node>'
            f'<node index="5" text="" class="android.widget.FrameLayout" package="{PKG}" '
            f'bounds="[174,690][1002,880]">'
            f'<node index="6" text="出仓进度提醒" class="android.widget.TextView" '
            f'package="{PKG}" bounds="[212,700][964,760]"></node></node></node>')
        f = parse_frame(dt_frame(dt_item("今天 08:42", no_rid)))
        self.assertEqual([u.content for u in f.units], ["出仓进度提醒"])


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

    def test_chat_page_detected_by_layout_ids_without_input_area(self):
        """输入区读不到时，靠布局 id 链仍要认出聊天页。

        现场踩过：连续两帧的输入区提示语没读到（键盘顶起／上报落在动画中间态），
        整帧被判 unknown 丢掉 —— 那两帧里的新消息一条都不会转发，而且日志只有
        一句 INFO，比误转更难发现。布局 id 链不依赖界面文案、也不依赖动画时机。
        """
        xml = (f'<hierarchy rotation="0"><node index="0" class="android.widget.FrameLayout" '
               f'package="{PKG}" bounds="[0,0][1176,2480]">'
               f'<node index="49" resource-id="{PKG}:id/list_and_tip_content" '
               f'class="android.widget.FrameLayout" package="{PKG}" bounds="[0,265][1176,2018]">'
               f'<node index="48" resource-id="{PKG}:id/list_content" '
               f'class="android.widget.LinearLayout" package="{PKG}" bounds="[0,265][1176,2018]">'
               f'<node index="50" resource-id="{PKG}:id/list_view" class="android.widget.ListView" '
               f'package="{PKG}" scrollable="true" bounds="[0,265][1176,2018]">'
               f'{row_simple("正文", 320)}</node></node></node></node></hierarchy>')      # 无输入区
        f = parse_frame(xml)
        self.assertEqual(f.page, "chat")
        self.assertIn("list_content", f.page_reason)
        self.assertEqual([u.content for u in f.units], ["正文"])

    def test_single_generic_layout_id_is_not_enough(self):
        """只出现一个通用 id（如别处也叫 list_view 的列表）不足以采信。"""
        xml = (f'<hierarchy rotation="0"><node index="0" class="android.widget.FrameLayout" '
               f'package="{PKG}" bounds="[0,0][1176,2480]">'
               f'<node index="50" resource-id="{PKG}:id/list_view" class="android.widget.ListView" '
               f'package="{PKG}" scrollable="true" bounds="[0,265][1176,2018]">'
               f'{row_simple("张三", 320)}</node></node></hierarchy>')
        f = parse_frame(xml)
        self.assertEqual(f.page, "unknown")
        self.assertEqual(f.units, [])

    def test_at_panel_wins_over_layout_ids(self):
        """反向信号一票否决：@ 面板即使混进聊天页的布局 id 也不能放行。"""
        xml = (f'<hierarchy rotation="0"><node index="0" class="android.widget.FrameLayout" '
               f'package="{PKG}" bounds="[0,0][1176,2480]">'
               f'<node index="1" text="选择要@的人" class="android.widget.TextView" '
               f'package="{PKG}" bounds="[400,200][800,260]"></node>'
               f'<node index="49" resource-id="{PKG}:id/list_and_tip_content" '
               f'class="android.widget.FrameLayout" package="{PKG}" bounds="[0,265][1176,2018]">'
               f'<node index="48" resource-id="{PKG}:id/list_content" '
               f'class="android.widget.LinearLayout" package="{PKG}" bounds="[0,265][1176,2018]">'
               f'<node index="50" resource-id="{PKG}:id/list_view" class="android.widget.ListView" '
               f'package="{PKG}" scrollable="true" bounds="[0,265][1176,2018]">'
               f'{row_simple("服务诊断专员", 320)}</node></node></node></node></hierarchy>')
        f = parse_frame(xml)
        self.assertEqual(f.page, "other")
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

    # ---- 锚点的**近似**匹配 ------------------------------------------
    #
    # 真机事故（2026-10-17，一小时 46 次锚点丢失）：H5 卡片重绘会让同一条
    # 消息的正文变样 —— 表格有时横排「指标: 30 ｜ 21」、有时一格一行。
    # 而这条卡片往往就是屏幕最后一条 = 锚点本身。它一变，锚点立刻「丢失」，
    # 网关静默重建基线、这一屏不发。用户看到的是「消息没转过来，手动上翻
    # 一下又转了」—— 上翻让卡片重新渲染，正好蒙对了某一种形态。

    # 同一条卡片两种形态的真实正文（截取关键段，>30 字才会参与近似）
    CARD_A = ("@所有人拦截跟单日报\n时间 2026/10/17 · 浙江杭州萧山区大江东公司\n"
              "跟单概况\n指标\n10/13\n10/14\n10/15\n10/16\n10/17\n"
              "跟单任务数\n30\n21\n23\n20\n17\n完成率\n93.33%\n90.48%")
    CARD_B = ("@所有人拦截跟单日报\n时间 2026/10/17 · 浙江杭州萧山区大江东公司\n"
              "跟单概况\n指标\n跟单任务数\n10/13\n30\n10/14\n21\n10/15\n23\n"
              "10/16\n20\n10/17\n17\n完成率\n93.33%\n90.48%")

    def test_anchor_survives_card_reshape(self):
        """锚点卡片换了渲染形态，不能再判「锚点丢失」。"""
        d = FrameDiffer()
        d.feed(self.units(("拦截跟单专员", self.CARD_A)))
        diff = d.feed(self.units(("拦截跟单专员", self.CARD_B)))
        self.assertFalse(diff.anchor_lost, "卡片换形态被误判成锚点丢失")
        self.assertEqual(diff.count, 0, "换形态不该产生新消息")

    def test_reshape_then_new_message_still_emitted(self):
        """换形态之后紧跟的新消息要照常发 —— 这才是用户看到的「漏发」。"""
        d = FrameDiffer()
        d.feed(self.units(("拦截跟单专员", self.CARD_A)))
        new_msg = "@所有人 履约-客户声音晚报 · 10月17日，今天共处理 42 单，请核对。"
        diff = d.feed(self.units(("拦截跟单专员", self.CARD_B),
                                 ("履约跟单专员", new_msg)))
        self.assertFalse(diff.anchor_lost)
        self.assertEqual([u.content for u in diff.units], [new_msg])

    def test_history_scroll_still_blocked_for_long_messages(self):
        """近似匹配不能把「翻历史」放进来 —— 真正对不上时仍必须保护。"""
        d = FrameDiffer()
        d.feed(self.units(("a", "今天的最新消息，讲的是完全不同的内容，一共 42 单。"),
                          ("a", "另一条也不同的消息，说的是明天要处理的 17 票。"),
                          ("a", "再来一条不一样的，关于考核金额的说明与提醒事项。"),
                          ("a", "最后一条也不太一样，告诉大家好今天已经全部完成。")))
        diff = d.feed(self.units(("a", "很久以前的旧消息，内容是八月的积压情况一共 99 票。"),
                                 ("a", "更早的一条，说的是七月的派送异常与处理结果。"),
                                 ("a", "再早一点，六月的考核结果与奖惩说明写得比较长。")))
        self.assertTrue(diff.anchor_lost, "翻历史被近似匹配放行了")
        self.assertEqual(diff.count, 0)

    def test_short_messages_do_not_get_near_matching(self):
        """短消息不走近似 —— 否则「收到」「好的」会把锚点吸到错误的行上。"""
        d = FrameDiffer()
        d.feed(self.units(("a", "收到")))
        diff = d.feed(self.units(("a", "好的"), ("a", "收到"), ("a", "新的正文内容")))
        # 精确匹配应该命中「收到」这一条，而不是把「好的」误当锚点
        self.assertEqual([u.content for u in diff.units], ["新的正文内容"])

    # ---- 上翻探测：锚点只能往前挪一点点 --------------------------------

    def test_note_scroll_keeps_anchor_on_the_visible_bottom(self):
        """手机翻上去之后，锚点要留在「刚翻走的那一屏的最后一条」。

        这一条锁的是**丢消息**：锚点丢失时网关请手机翻一屏，回包那一帧既
        有漏掉的内容、也有列表底部露出来的新内容。如果照常 commit，锚点
        会爬到最新那条上，等手机翻回底部，那条新内容就跑到锚点**上方**，
        补全帧再也捡不到 —— 日志里一切正常，消息却永远丢了。
        """
        d = FrameDiffer()
        d.feed(self.units(("a", "旧1"), ("a", "旧2")))
        # 手机翻上去：屏幕上变成 旧1 / 旧2 / 长消息（列表底部露出了新内容）
        d.note_scroll(self.units(("a", "旧1"), ("a", "旧2"), ("a", "新3")))
        self.assertEqual(d._anchor_text, [("a", "旧1"), ("a", "旧2"), ("a", "新3")])

        # 翻回底部：新3 仍在锚点里 → 不重复发
        diff = d.feed(self.units(("a", "旧1"), ("a", "旧2"), ("a", "新3"),
                                 ("a", "新4")))
        self.assertEqual([u.content for u in diff.units], ["新4"])

    def test_note_scroll_ignores_frames_before_the_anchor(self):
        """翻过头（本帧里根本没有旧锚点）时锚点不动 —— 宁可多补一次。"""
        d = FrameDiffer()
        d.feed(self.units(("a", "旧1"), ("a", "旧2")))
        d.note_scroll(self.units(("a", "更早1"), ("a", "更早2")))
        self.assertEqual(d._anchor_text, [("a", "旧1"), ("a", "旧2")])

    def test_note_scroll_without_baseline_is_noop(self):
        d = FrameDiffer()
        d.note_scroll(self.units(("a", "1")))
        self.assertFalse(d.baseline_built)
        self.assertEqual(d._anchor, [])

    # ---- 新内容长在锚点**上方** ----------------------------------------
    #
    # 真机事故（2026-10-17 19:12）：用户报「标签履约周累计达成（1012-1015）
    # 又没采集到」，而库里确实一条都没有，日志也**一条错都不报**。抓下来的
    # 那一帧（data/screen-20261017_191838.xml）说明原因：卡片在屏幕上、解析
    # 完全正常（814 字、发送者对），但它渲染在锚点（那条采集波动告警）**上方**。
    # 只看 ``units[idx+1:]`` 的话后面永远是空的 —— 卡片既不算新、也不入库，
    # 就这么无声无息地丢了。
    #
    # 修法见 ``_above_anchor_is_new``：判据是「上沿紧贴列表顶」+「下沿紧贴
    # 锚点上沿」，且必须是**已经在锚点里的**才排除（否则同一条卡片每帧抛一次）。

    VIEW = (0, 265, 1176, 2018)         # 真机消息列表可视区

    def laid_out(self, *items: tuple[str, str, int, int]) -> list[Unit]:
        """(sender, content, y0, y1) → Unit，用来摆出真实的位置关系。"""
        return [Unit(sender=s, content=c, y0=y0, y1=y1) for s, c, y0, y1 in items]

    def card_above_alert(self) -> list[Unit]:
        """真机那一帧的形状：卡片在上（顶到列表顶），告警在下（= 锚点）。"""
        return self.laid_out(
            ("履约跟单专员", self.CARD_A, 265, 1251),
            ("服务诊断专员", "【系统】采集波动：无法确认新旧消息，本帧已跳过。", 1251, 1951),
        )

    def test_card_above_anchor_is_emitted(self):
        """卡片从屏幕顶挤进来（在锚点上方）也必须发出去 —— 这就是漏的那条。"""
        d = FrameDiffer()
        d.set_view(self.VIEW)
        d.feed(self.laid_out(
            ("服务诊断专员", "【系统】采集波动：无法确认新旧消息，本帧已跳过。", 900, 1600)))
        diff = d.feed(self.card_above_alert())
        self.assertFalse(diff.anchor_lost)
        self.assertEqual([u.sender for u in diff.units], ["履约跟单专员"],
                         "锚点上方的卡片没被当成新消息")

    def test_card_above_anchor_is_emitted_only_once(self):
        """同一条卡片连来三帧，只能发一次（真机曾 4 帧抛 4 次）。"""
        d = FrameDiffer()
        d.set_view(self.VIEW)
        d.feed(self.laid_out(
            ("服务诊断专员", "【系统】采集波动：无法确认新旧消息，本帧已跳过。", 900, 1600)))
        self.assertEqual(d.feed(self.card_above_alert()).count, 1)
        self.assertEqual(d.feed(self.card_above_alert()).count, 0)
        self.assertEqual(d.feed(self.card_above_alert()).count, 0)

    def test_alert_after_card_is_not_replayed(self):
        """卡片在上、告警在下：告警已经在锚点里，不能每帧重发一遍。"""
        d = FrameDiffer()
        d.set_view(self.VIEW)
        d.feed(self.card_above_alert())              # 第一帧 = 基线，都不发
        diff = d.feed(self.card_above_alert())
        self.assertEqual(diff.count, 0)

    def test_history_above_anchor_is_never_emitted(self):
        """用户往上翻历史：整屏都是没转发过的旧消息，一条都不能当新消息。"""
        d = FrameDiffer()
        d.set_view(self.VIEW)
        d.feed(self.laid_out(
            ("a", "今天最后一条消息，说的是晚上的交接安排，请各位留意。", 265, 700)))
        diff = d.feed(self.laid_out(
            ("a", "很久以前的旧消息，内容是八月的积压情况一共 99 票。", 265, 700),
            ("a", "更早的一条，说的是七月的派送异常与处理结果。", 700, 1200),
            ("a", "今天最后一条消息，说的是晚上的交接安排，请各位留意。", 1200, 1700)))
        self.assertEqual(diff.count, 0, "翻历史时把旧消息当新消息发出来了")

    def test_card_above_without_view_info_is_ignored(self):
        """拿不到可视区就不敢判 —— 宁可按老行为漏一次，也不注入历史。"""
        d = FrameDiffer()
        d.feed(self.laid_out(
            ("服务诊断专员", "【系统】采集波动：无法确认新旧消息，本帧已跳过。", 900, 1600)))
        diff = d.feed(self.card_above_alert())       # 没调 set_view
        self.assertEqual(diff.count, 0)

    def test_scrambled_flag_marks_frames_with_above_content(self):
        """``scrambled`` 是给网关看的：这种帧的锚点已经不可靠，要重建基线。"""
        d = FrameDiffer()
        d.set_view(self.VIEW)
        d.feed(self.laid_out(
            ("服务诊断专员", "【系统】采集波动：无法确认新旧消息，本帧已跳过。", 900, 1600)))
        diff = d.feed(self.card_above_alert())
        self.assertTrue(diff.scrambled)
        self.assertEqual(diff.above, 1)
        # above 是 units 的**前缀**：units = 上方的卡片 + 锚点之后的部分
        self.assertEqual(diff.count, diff.above, "above 应计入 units")

    def test_anchor_only_drops_the_above_section(self):
        """推进锚点时只该看「锚点及其之前」，否则新卡片会被当成锚点吞掉。"""
        d = FrameDiffer()
        d.set_view(self.VIEW)
        d.feed(self.laid_out(
            ("服务诊断专员", "【系统】采集波动：无法确认新旧消息，本帧已跳过。", 900, 1600)))
        frame = self.card_above_alert()
        diff = d.peek(frame)
        kept = diff.anchor_only(frame)
        self.assertEqual([u.sender for u in kept], ["履约跟单专员", "服务诊断专员"])

        # 没有上方内容时原样返回（不复制、不切片）
        plain = self.laid_out(
            ("履约跟单专员", self.CARD_A, 265, 1251),
            ("服务诊断专员", "【系统】采集波动：无法确认新旧消息，本帧已跳过。", 1251, 1951))
        self.assertIs(Diff().anchor_only(plain), plain)

    def test_peek_does_not_move_anchor(self):
        """peek 只判不提交 —— 网关要靠这一点在锚点丢失时保留旧锚点。"""
        d = FrameDiffer()
        d.set_view(self.VIEW)
        d.feed(self.laid_out(
            ("服务诊断专员", "【系统】采集波动：无法确认新旧消息，本帧已跳过。", 900, 1600)))
        before = list(d._anchor)
        d.peek(self.card_above_alert())
        self.assertEqual(d._anchor, before)
        d.commit(self.card_above_alert())
        self.assertNotEqual(d._anchor, before)


# ------------------------------------------------------------------ 表格消息

def dt_text(label: str, left: int, top: int, right: int, bottom: int) -> str:
    """卡片正文里的一段普通文字（left 恒为 212，right 被裁到屏宽）。"""
    return (f'<node index="70" text="{label}" class="android.widget.TextView" '
            f'package="{PKG}" bounds="[{left},{top}][{right},{bottom}]"></node>')


def dt_table(rows: list[list[str]], *, top: int, left: int = 212,
             cell: int = 452, cols: list[int] | None = None) -> str:
    """气泡里的横滑表格（真机 2026-10-17 16:31 那一帧的形状）。

    真机的两个反直觉之处，这里都照实复现：

    1. 每个格子的 bounds 是 ``[left,y][964,y+h]`` —— **right 恒等于屏幕宽度**
       （被裁剪），left 才是真正的列坐标（250 / 702 / 1154 …）。
       拿 min/max 归一化会把 left 变成 964，列序全乱、数值对错列名。
    2. 表头与数据行的 y 区间不整齐（659~770 / 810~869），同一行的格子
       垂直区间重叠但中心不相等。
    3. 格子前面夹着 8px 宽的装饰 View（无文本）。

    ``cols`` 给的是**绝对 x 列表**：真机的列宽并不相等（三列那张是
    250 / 548 / 714，不是等距），等距模型只适合横向铺开的宽表。
    """
    idx = 80
    parts = []
    for r, cells in enumerate(rows):
        y = top + 151 * r
        for c, text in enumerate(cells):
            x = cols[c] if cols else left + 38 + c * cell
            if c:     # 列分隔装饰
                parts.append(
                    f'<node index="{idx}" text="" class="android.view.View" '
                    f'package="{PKG}" bounds="[{x - 8},{y}][964,{y + 149}]"></node>')
                idx += 1
            h = 59 if r == 0 else 111
            parts.append(
                f'<node index="{idx}" text="" class="android.widget.LinearLayout" '
                f'package="{PKG}" bounds="[{x},{y}][964,{y + h}]">'
                f'<node index="{idx + 1}" text="{text}" '
                f'class="android.widget.TextView" package="{PKG}" '
                f'bounds="[{x},{y}][964,{y + 59}]"></node></node>')
            idx += 2
    height = 151 * len(rows) + 20
    return ('<node index="70" text="" class="android.widget.HorizontalScrollView" '
            f'package="{PKG}" scrollable="true" bounds="[{left},{top}]'
            f'[964,{top + height}]">'
            f'<node index="71" text="" class="android.widget.FrameLayout" '
            f'package="{PKG}" bounds="[{left},{top}][964,{top + height}]">'
            + "".join(parts) + '</node></node>')


def dt_card_message(sender: str, body: str, top: int) -> str:
    """卡片消息行：气泡里包着一个 ``card_view_container``，表格在卡片里。

    真机结构（2026-10-17 16:31）：
        RelativeLayout(行) › [头像, 昵称块, 气泡 › card_view_container › 表格]
    """
    h = 1200
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
        f'bounds="[178,{top + 20}][406,{top + 64}]"></node></node>'
        f'<node index="7" text="" class="android.widget.FrameLayout" package="{PKG}" '
        f'resource-id="{PKG}:id/chat_bubble_container" '
        f'bounds="[174,{top + 80}][1002,{top + h - 10}]">'
        f'<node index="12" text="" class="android.widget.FrameLayout" package="{PKG}" '
        f'resource-id="{PKG}:id/card_view_container" '
        f'bounds="[174,{top + 80}][1002,{top + h - 10}]">{body}</node>'
        f'</node></node>')


class TestCardTable(unittest.TestCase):
    """卡片里的表格必须按行还原，而不是一竖排。

    真机事故（2026-10-17 15:16 之后）：群里来了一张表格，它在树里是个
    ``HorizontalScrollView``（占屏 40%、被切成 1 条「消息」），把真正的
    ``ListView`` 挤了下去 —— 锚点于是每帧失效，**整屏消息停止转发**，
    日志刷出一串「上一帧的锚点在本帧里找不到」。

    修好容器之后还有第二层问题：表格内容被读成**列优先**的一竖排
    （10 个列名 + 每个网点 9 个值），看不出哪个数是哪个指标。
    """

    ROWS = [["网点名称", "打标量", "履约量", "履约率"],
            ["浙江杭州萧山区大江东公司", "3855", "3840", "99.61%"],
            ["浙江萧山区衙前镇营业部(311209)", "4538", "4327", "95.35%"]]

    def test_table_does_not_steal_the_message_list(self):
        item = dt_item("今天 16:31",
                       dt_card_message("履约跟单专员",
                                       dt_table(self.ROWS, top=640), 320),
                       dt_message("签收跟单专员", ["新的一条"], 1560))
        f = parse_frame(dt_frame(item))
        self.assertEqual(f.container, "ListView")
        self.assertEqual(len(f.units), 2)
        self.assertEqual(f.units[0].sender, "履约跟单专员")
        self.assertEqual(f.units[1].content, "新的一条")

    def test_table_is_rendered_row_by_row(self):
        """列名与数值必须一一对上 —— 这是这次最核心的断言。"""
        item = dt_item("今天 16:31",
                       dt_card_message("履约跟单专员",
                                       dt_table(self.ROWS, top=640), 320))
        body = parse_frame(dt_frame(item)).units[0].content
        self.assertIn("网点名称: 浙江杭州萧山区大江东公司 ｜ 打标量: 3855 "
                      "｜ 履约量: 3840 ｜ 履约率: 99.61%", body)
        self.assertIn("履约率: 95.35%", body)
        # 每个网点一行
        self.assertEqual(body.count("网点名称:"), 2)

    def test_plain_multiline_message_is_not_turned_into_a_table(self):
        """普通多行正文（每行一个文本、left 相同）不能被误判成表格。"""
        item = dt_item("今天 16:31",
                       dt_message("签收跟单专员", ["第一行", "第二行", "第三行"], 320))
        body = parse_frame(dt_frame(item)).units[0].content
        self.assertEqual(body, "第一行\n第二行\n第三行")

    def test_anchor_survives_a_persistent_table(self):
        """表格一直挂在屏上时，连续几帧都不能判锚点丢失。"""
        item = dt_item("今天 16:31",
                       dt_card_message("履约跟单专员",
                                       dt_table(self.ROWS, top=640), 320))
        d = FrameDiffer()
        for i in range(4):
            diff = d.feed(parse_frame(dt_frame(item)).units)
            self.assertFalse(diff.anchor_lost, f"第 {i + 1} 帧误判锚点丢失")
            if i == 0:
                self.assertTrue(diff.baseline)

    def test_list_id_beats_a_bigger_inner_scroller(self):
        """语义 id 优先于「条数 / 面积」——这是这次事故的直接修法。"""
        item = dt_item("今天 16:31",
                       dt_card_message("履约跟单专员",
                                       dt_table(self.ROWS, top=640), 320))
        f = parse_frame(dt_frame(item))
        self.assertEqual(f.container, "ListView")
        self.assertEqual(len(f.units), 1)

    def test_table_without_list_id_still_excluded(self):
        """id 改版（没有 list_view）时，靠「水平溢出」也要能把表格剔掉。"""
        item = dt_item("今天 16:31",
                       dt_card_message("履约跟单专员",
                                       dt_table(self.ROWS, top=640), 320))
        xml = dt_frame(item).replace(":id/list_view", ":id/some_future_id")
        f = parse_frame(xml)
        self.assertNotEqual(f.container, "HorizontalScrollView")

    # --- 2026-10-17 16:42 第二次事故：三列表 + 头顶挤成一行 ---

    SMALL = [["小件员名称", "票数", "单号"],
             ["周吉祥", "1票", "777454064365026"]]
    SMALL_COLS = [250, 548, 714]        # 真机 08:13 那张三列表的实际列坐标

    def test_header_not_stolen_by_the_title_row(self):
        """三列表只有两行时，气泡顶部那一排（头像/昵称/角标/首段）列数更多，
        不能被当成表头 —— 一旦被当成表头，真正的表头就被截掉，整张表判成
        「不是表格」，连带正文压成一大坨纯文本。"""
        item = dt_item("今天 08:13",
                       dt_card_message("拦截跟单专员",
                                       dt_table(self.SMALL, top=640,
                                                cols=self.SMALL_COLS), 320))
        body = parse_frame(dt_frame(item)).units[0].content
        self.assertIn("小件员名称: 周吉祥 ｜ 票数: 1票 ｜ 单号: 777454064365026", body)

    def test_body_around_the_table_is_kept(self):
        """表格只替换自己的格子，表格前后的正文必须原样留在原位。"""
        cells = (dt_text("表格上面的一句话", 212, 600, 964, 660)
                 + dt_table(self.SMALL, top=700, cols=self.SMALL_COLS)
                 + dt_text("表格下面的一句话", 212, 1000, 964, 1060))
        item = dt_item("今天 08:13",
                       dt_card_message("拦截跟单专员", cells, 320))
        body = parse_frame(dt_frame(item)).units[0].content
        lines = body.split("\n")
        self.assertEqual(lines[0], "表格上面的一句话")
        self.assertIn("小件员名称: 周吉祥", lines[1])
        self.assertEqual(lines[-1], "表格下面的一句话")


# ------------------------------------------------------------------ 截断检测

class TestClipped(unittest.TestCase):
    """一屏装不下的长消息要能被认出来 —— 否则发出去的就是半截话。

    ``getRootInActiveWindow()`` 只给可见节点，超出屏幕的部分在树里**不存在**。
    这不是解析 bug，是采集方式的物理上限；能做的只是「认出它不完整」，
    再让手机端往上翻页补全。
    """

    def _frame(self, n_lines: int):
        lines = [f"第 {i} 段正文，说的是同一件事的不同细节" for i in range(1, n_lines + 1)]
        return parse_frame(dt_frame(dt_item(
            "今天 16:31", dt_message("履约跟单专员", lines, 300))))

    def test_long_message_is_detected(self):
        """40 行的消息会顶出列表下沿（可视区 265~2018），必须报出来。"""
        f = self._frame(40)
        cut = clipped_units(f)
        self.assertEqual(len(cut), 1)
        self.assertIn("被屏幕边界切掉", needs_scroll(f))

    def test_short_message_is_not_flagged(self):
        """14 行还在屏内（底边 1260 < 2018），绝不能误报 —— 误报会让手机白白滑动。"""
        f = self._frame(14)
        self.assertEqual(clipped_units(f), [])
        self.assertEqual(needs_scroll(f), "")

    def test_unknown_view_never_flags(self):
        """拿不到列表可视范围时一律不猜（宁可不补，也不要每帧都滚）。"""
        f = self._frame(40)
        f.view = (0, 0, 0, 0)
        self.assertEqual(clipped_units(f), [])
        self.assertEqual(needs_scroll(f), "")

    # 现场存档里**确实**被屏幕切掉的那几帧。判据见 clipped_units：
    # 121909 那条消息 h=1753，正好等于可视区高度（265~2018）——
    # 一条消息占满整屏，在手机上不可能「一屏正好装下」，它必然是被切开的。
    # 旧用例断言「所有存档帧都不该报截断」，等于把这个真实缺陷锁进了测试里。
    KNOWN_CLIPPED = {"screen-20261017_121909.xml"}

    def test_every_real_frame_is_clean(self):
        """现场存档帧：该报的报、不该报的一个都不许误报。

        误报的代价是手机会白滑一屏（还打乱锚点）；漏报的代价是长消息只转
        后半截 —— 后者是用户实际反馈的问题（「内容特别长的消息转发还是有问题」），
        所以两个方向都要守住。
        """
        for path in sorted((BASE / "data").glob("screen-*.xml")):
            raw = path.read_text(encoding="utf-8", errors="replace")
            f = parse_frame(raw)
            if path.name in self.KNOWN_CLIPPED:
                self.assertTrue(clipped_units(f),
                                f"{path.name} 是满屏长消息，应当报截断")
                continue
            self.assertEqual(clipped_units(f), [], f"{path.name} 误报截断")


class TestGarbled(unittest.TestCase):
    """坏坐标文本节点 = 钉钉 H5 卡片的标志，网关要靠它「扣住待稳」。

    真机实测（2026-10-17 拦截跟单日报）：H5 卡片里**所有**文字节点 bounds
    都是 ``[x,265][x,负数]``，right 会小于 left。但屏幕底边被裁掉的原生行
    也会贡献 1~3 个（121843 帧=3 个）—— 所以阈值是 ≥4（GARBLED_MIN）。
    """

    def _xml_with(self, bounds: str, n: int = 1) -> str:
        """在一条正常消息的气泡里塞 n 个指定 bounds 的 TextView。"""
        raw = dt_frame(dt_item("今天 16:31", dt_message(
            "履约跟单专员", ["正文一句"], 300)))
        badge = "".join(
            f'<node text="坏节点{i}" class="android.widget.TextView" '
            f'package="{PKG}" bounds="{bounds}"></node>'
            for i in range(n))
        return raw.replace(
            '</node></node></node></hierarchy>',
            badge + '</node></node></node></hierarchy>', 1)

    def test_webview_card_bounds_are_flagged(self):
        f = parse_frame(self._xml_with('[0,265][100,-50]', n=5))
        self.assertGreaterEqual(f.garbled_count, 4)
        self.assertTrue(f.garbled)

    def test_crossed_right_left_is_flagged(self):
        f = parse_frame(self._xml_with('[1416,265][964,-1418]', n=5))
        self.assertTrue(f.garbled)

    def test_few_garbled_nodes_are_not_h5(self):
        """屏幕底边裁掉的原生行（1~3 个坏文本节点）不算 H5。

        夹具自己的时间条也贡献 1 个坏文本节点，所以塞 2 个凑成 3 个 ——
        正好是实测原生帧的上限。
        """
        f = parse_frame(self._xml_with('[238,2517][794,2480]', n=2))
        self.assertLess(f.garbled_count, 4)
        self.assertFalse(f.garbled)

    def test_normal_frame_is_clean(self):
        f = parse_frame(self._xml_with('[0,265][100,315]'))
        self.assertFalse(f.garbled)


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
