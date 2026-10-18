"""三种「长消息发不出去」的现场，都是从真机 2026-10-18 19:5x 的帧里扒出来的。

用户原话：「发长消息还是有点问题，我手动把消息再翻一遍就能转发出去」，
随后补了一句「翻了也不行」。三条独立的缺陷叠在一起，缺一条都复现不出来：

  ① **贴边仍被切**（``clipped_units`` ③）：卡片外层气泡的底边被钉在可视区
     下沿，①②「越界」和③「撑满」全判不出，于是不补全、静默 3 秒就发半截；
  ② **翻页后补全成果被丢**（锚点丢失 → commit 重建前没投递 long_head）：
     手机按指示翻了一屏，锚点找不回，试探额度用完就重建基线，刚补全的全文
     跟着作废 —— 这正是「翻了也不行」；
  ③ **按钮名冒充发送者**：卡片底部的「查看全部」贴着气泡上沿，被当成昵称，
     库里那些记录 sender 就叫「查看全部」/ 图标字符 / 空。

跑法：``python -m unittest tests.test_long_message_clipped -v``
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from bridge.sources import android_parse as A          # noqa: E402

PKG = "com.alibaba.android.rimet"
VIEW_TOP, VIEW_BOTTOM = 265, 2013      # 真机 19:57 那一帧的可视区


def _screen(rows: str) -> str:
    """整屏 XML。可视区 [0,265][1176,2013]，底部留输入区提示语（判页面要用）。"""
    bar = (f'<node class="android.widget.TextView" package="{PKG}" '
           f'text="发消息或按住说话..." bounds="[182,2191][981,2262]"></node>')
    return ('<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0">'
            f'<node class="android.widget.FrameLayout" package="{PKG}" '
            f'bounds="[0,0][1176,2480]">'
            f'<node class="androidx.recyclerview.widget.RecyclerView" '
            f'package="{PKG}" scrollable="true" '
            f'bounds="[0,{VIEW_TOP}][1176,{VIEW_BOTTOM}]">'
            f'{rows}</node>{bar}</node></hierarchy>')


def clipped_card(name: str, top: int, bubble_top: int, bubble_bottom: int,
                 body: str, *, real_bottom: int, tag: str = "查看全部") -> str:
    """一张**底边贴住可视区下沿、但内容其实没露完**的卡片。

    真机形状（19:57）::

        气泡外层    bounds = [60,417][1020,2013]   ← 底边正好钉在下沿
        正文叶子    bounds = [80,487][1000,2013]   ← 同样被夹
        '1人未读'   bounds = [842,5015][977,2013]  ← top 是真位置，倒挂！

    那个倒挂的标签就是唯一的证据：它说明这张卡片真实延伸到 ``real_bottom``。
    ``tag`` 是卡片底部那条「查看全部」按钮 —— 它贴着气泡上沿，会被误当昵称。
    """
    return (
        f'<node class="android.widget.LinearLayout" package="{PKG}" '
        f'bounds="[20,{top}][1060,{bubble_bottom}]">'
        # 卡片底部按钮：top 贴着气泡上沿 → 老逻辑会把它当昵称
        f'<node text="{tag}" class="android.widget.TextView" package="{PKG}" '
        f'bounds="[60,{top - 2}][660,{top + 40}]"></node>'
        f'<node text="{name}" class="android.widget.TextView" package="{PKG}" '
        f'bounds="[60,{top + 40}][400,{top + 100}]"></node>'
        f'<node class="android.widget.LinearLayout" package="{PKG}" '
        f'bounds="[60,{bubble_top}][1020,{bubble_bottom}]">'
        f'<node text="{body}" class="android.widget.TextView" package="{PKG}" '
        f'bounds="[80,{bubble_top + 20}][1000,{bubble_bottom}]"></node>'
        f'</node>'
        # 被夹的「1人未读」：top 是真实位置，bottom 被夹到下沿
        f'<node text="1人未读" class="android.widget.TextView" package="{PKG}" '
        f'bounds="[842,{real_bottom}][977,{bubble_bottom}]"></node>'
        f'</node>')


def plain_row(text: str, top: int, sender: str) -> str:
    return (f'<node class="android.widget.LinearLayout" package="{PKG}" '
            f'bounds="[60,{top}][1020,{top + 180}]">'
            f'<node text="{sender}" class="android.widget.TextView" '
            f'package="{PKG}" bounds="[80,{top}][420,{top + 60}]"></node>'
            f'<node class="android.widget.LinearLayout" package="{PKG}" '
            f'bounds="[80,{top + 70}][1000,{top + 170}]">'
            f'<node text="{text}" class="android.widget.TextView" '
            f'package="{PKG}" bounds="[100,{top + 75}][980,{top + 165}]"></node>'
            f'</node></node>')


class TestClippedButFlush(unittest.TestCase):
    """① 底边贴着可视区下沿，但内容其实被切掉了。"""

    def frame(self):
        xml = _screen(clipped_card(
            "标签履约月累计达成", top=417, bubble_top=417, bubble_bottom=VIEW_BOTTOM,
            body="各位好，本月累计达成如下…… 查看完整月报明细（Exce",
            real_bottom=5015))
        return A.parse_frame(xml)

    def test_clamp_depth_is_recorded(self):
        f = self.frame()
        self.assertEqual(len(f.units), 1)
        self.assertGreater(f.units[0].clipped_below, 0,
                           "被夹断的卡片必须记下夹断高度")

    def test_needs_scroll_fires(self):
        why = A.needs_scroll(self.frame())
        self.assertTrue(why, "底边贴着下沿但内容没露完，必须请求补全")
        self.assertIn("切掉", why)

    def test_clipped_units_includes_it(self):
        f = self.frame()
        self.assertEqual(len(A.clipped_units(f)), 1)


class TestScrolledToTopIsNotClipped(unittest.TestCase):
    """反面：整行滚到列表顶端时 bounds 也会倒挂，但内容是全的，不能误报。

    真机存档 screen-20261017_121851 就是这种形状，第一版判据把它误判成截断，
    ``test_every_real_frame_is_clean`` 当场抓到。
    """

    def test_real_archive_frame_is_clean(self):
        path = BASE_DIR / "data" / "screen-20261017_121851.xml"
        if not path.is_file():
            self.skipTest("缺少真机存档")
        f = A.parse_frame(path.read_text(encoding="utf-8", errors="replace"))
        self.assertTrue(f.units)
        for u in f.units:
            self.assertEqual(u.clipped_below, 0,
                             f"误报截断：{u.sender} / {u.content[:20]}")
        self.assertEqual(A.clipped_units(f), [])
        self.assertEqual(A.needs_scroll(f), "")

    def test_top_flush_collapsed_bounds(self):
        """整行被压到上沿、叶子的 bottom 全变负数 —— 不许判截断。"""
        row = (
            f'<node class="android.widget.LinearLayout" package="{PKG}" '
            f'bounds="[60,{VIEW_TOP}][1020,1118]">'
            f'<node text="拦截跟单专员" class="android.widget.TextView" '
            f'package="{PKG}" bounds="[80,{VIEW_TOP}][420,-1045]"></node>'
            f'<node class="android.widget.TextView" package="{PKG}" '
            f'text="内容完整的一条" bounds="[80,{VIEW_TOP}][1000,-318]"></node>'
            f'</node>')
        f = A.parse_frame(_screen(row))
        self.assertEqual([u.clipped_below for u in f.units], [0],
                         "top 落在可视区内 = 滚到顶，不是夹断")
        self.assertEqual(A.needs_scroll(f), "")


class TestButtonIsNotSender(unittest.TestCase):
    """③ 卡片底部的「查看全部」贴着气泡上沿，不能当昵称。"""

    def test_view_all_is_not_a_sender(self):
        f = self.frame_with_tag("查看全部")
        self.assertNotEqual(f.units[0].sender, "查看全部")

    def test_collapse_is_not_a_sender(self):
        self.assertNotEqual(self.frame_with_tag("收起").units[0].sender, "收起")

    def test_icon_font_is_not_a_sender(self):
        """图标字体（U+F339）读出来是一个私有区字符，也不是名字。"""
        self.assertNotEqual(self.frame_with_tag("\uf339").units[0].sender,
                            "\uf339")

    def frame_with_tag(self, tag: str):
        return A.parse_frame(_screen(clipped_card(
            "标签履约月累计达成", top=417, bubble_top=417,
            bubble_bottom=VIEW_BOTTOM, body="正文", real_bottom=5015, tag=tag)))

    def test_noise_labels_are_complete(self):
        for w in ("查看全部", "收起", "展开"):
            self.assertIn(w, A.NOISE_LABELS)


class TestIconRegex(unittest.TestCase):
    def test_private_use_chars_match(self):
        for ch in ("\ue000", "\uf339", "\uf8ff"):
            self.assertTrue(A.ICON_ONLY_RE.match(ch), repr(ch))

    def test_real_names_do_not_match(self):
        for name in ("履约跟单专员", "签收跟单专员", "A", "张三"):
            self.assertFalse(A.ICON_ONLY_RE.match(name), name)


if __name__ == "__main__":
    unittest.main()
