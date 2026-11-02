"""群名身份证据（frame.title）：锚点被挤出屏时的快速通道。

现场（真机 2026-11-02 16:51~16:56，用户原话「刚才新来一条消息，是我上翻
手机才转发的」+「一次来几条长消息的时候往往也不行」）：

    16:51:45 建基线（当前屏只有 1 条 —— 基线薄）
    16:54:54 H5 卡片的锚点对不上 —— 已请手机往上翻一屏（累积第 1 次）
    16:55:43 又一轮翻页试探
    16:55:49 这才收到 401 字那条消息，16:56:00 投出

根因：基线薄时新卡片（H5 大卡、长消息）会把锚点**整条挤出屏幕** —— 屏上
没有已知消息（evidence=-1）、也没有未读徽标（hints 空），旧逻辑：
    · 锚点丢失分支：只能翻页试探（每轮 30~90 秒，翻页期间一帧都不解析）；
    · 打捞层：evidence<0 且 hints 空 → 「宁可不捞」一条不出。
消息就卡在两条死路之间，直到用户手动上翻把锚点翻回来。

修法：给 Frame 提取**顶部群名**（``:id/tv_title``，真机 dump 实证）。群名
对得上电脑端盯的群 = 没换群；hints 空 = 没在翻历史。两个证据齐了：
    · 锚点丢失 → 不翻页，就地重建基线；
    · 打捞层 → 整屏按候选处理（dup_check 持久去重 + 30 分钟时间条 +
      夹断护栏照旧）。
换群/翻历史场景标题对不上或 hints 非空，仍走原路 —— 「把别的群灌进目标
群」的事故通道没有打开。
"""

import json
import time
import unittest
import urllib.request
from types import SimpleNamespace

from bridge.sources import android_gateway as G
from bridge.sources.android_gateway import _Ctx, _Handler
from bridge.sources.android_parse import parse_frame

from tests.test_android_gateway import GatewayCase, TOKEN, make_cfg
from tests.test_android_sweep import make_handler

PKG = "com.alibaba.android.rimet"
GROUP = "【数字质控】浙江杭州萧山区大江东公司"


# ---------------------------------------------------------------- 解析层

def title_row(text: str) -> str:
    return (f'<node index="22" text="{text}" class="android.widget.TextView" '
            f'package="{PKG}" resource-id="{PKG}:id/tv_title" '
            f'bounds="[302,143][868,194]"></node>')


def nickname_row() -> str:
    """消息卡片里的昵称块：id 相近但**不是**顶部标题（真机两者并存）。"""
    return (f'<node text="" class="android.widget.TextView" package="{PKG}" '
            f'resource-id="{PKG}:id/chatting_title" '
            f'bounds="[178,265][406,240]"></node>')


def input_bar() -> str:
    return (f'<node text="发消息或按住说话..." class="android.widget.TextView" '
            f'package="{PKG}" bounds="[182,2191][981,2262]"></node>')


def frame_xml(*rows: str) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0">'
            f'<node class="android.widget.FrameLayout" package="{PKG}" '
            f'bounds="[0,0][1080,2340]">'
            f'<node class="androidx.recyclerview.widget.RecyclerView" '
            f'package="{PKG}" scrollable="true" bounds="[0,200][1080,2100]">'
            + "".join(rows) + f'</node>{input_bar()}</node></hierarchy>')


def msg_row(sender: str, text: str, top: int) -> str:
    return (f'<node text="" class="android.widget.LinearLayout" package="{PKG}" '
            f'bounds="[60,{top}][1020,{top + 180}]">'
            f'<node text="{sender}" class="android.widget.TextView" '
            f'package="{PKG}" bounds="[80,{top}][420,{top + 60}]"></node>'
            f'<node text="{text}" class="android.widget.TextView" '
            f'package="{PKG}" bounds="[80,{top + 70}][1000,{top + 170}]"></node>'
            f'</node>')


def garbled_rows() -> str:
    """凑满 GARBLED_MIN(4) 个坏坐标文本节点 → H5 帧。"""
    return "".join(
        f'<node text="坏{i}" class="android.widget.TextView" package="{PKG}" '
        f'bounds="[100,{10 + i}][{50 + i},{5 + i}]"></node>'
        for i in range(5))


class TestChatTitle(unittest.TestCase):

    def test_title_extracted(self):
        f = parse_frame(frame_xml(title_row(GROUP),
                                  msg_row("甲", "正文", 400)))
        self.assertEqual(f.title, GROUP)

    def test_nickname_block_is_not_title(self):
        """``chatting_title`` 是卡片昵称块，不能当群名。"""
        f = parse_frame(frame_xml(title_row(GROUP), nickname_row(),
                                  msg_row("甲", "正文", 400)))
        self.assertEqual(f.title, GROUP)

    def test_no_title_is_empty(self):
        f = parse_frame(frame_xml(msg_row("甲", "正文", 400)))
        self.assertEqual(f.title, "")


class TestFrameTitleOk(unittest.TestCase):

    def _h(self, watch="【数字质控】浙江杭州萧山区大江东公司"):
        h = make_handler()
        h.ctx.watch_group = watch
        return h

    def test_exact_match(self):
        f = SimpleNamespace(title=GROUP)
        self.assertTrue(self._h()._frame_title_ok(f))

    def test_suffix_variant_still_matches(self):
        """标题带「(13)」之类后缀时子串互认仍然成立。"""
        f = SimpleNamespace(title=GROUP + "(13)")
        self.assertTrue(self._h()._frame_title_ok(f))

    def test_other_group_rejected(self):
        f = SimpleNamespace(title="【数字质控】宁波慈溪公司")
        self.assertFalse(self._h()._frame_title_ok(f))

    def test_no_title_no_match(self):
        self.assertFalse(self._h()._frame_title_ok(SimpleNamespace(title="")))
        # watch_group 与 chat_name 都没配 → 没有身份基准，绝不放行
        h = make_handler()
        h.ctx.watch_group = ""
        h.ctx.chat_name = ""
        self.assertFalse(h._frame_title_ok(SimpleNamespace(title=GROUP)))


class TestSweepWithTitleEvidence(unittest.TestCase):

    def _h(self, known_uids=()):
        h = make_handler()
        h.ctx.dup_check = lambda uid: uid in set(known_uids)
        h.ctx.watch_group = GROUP
        return h

    @staticmethod
    def _u(sender, content, ts):
        return G.Unit(sender=sender, content=content, ts=ts,
                      y0=500, y1=600, clipped_below=0)

    def _frame(self, units, hints=(), title=GROUP):
        return SimpleNamespace(units=units, view=(0, 200, 1080, 2100),
                               hints=hints, title=title)

    def test_title_evidence_sweeps_full_screen(self):
        """无已知消息、无徽标，但群名对上 → 整屏按候选处理。"""
        h = self._h()
        new = self._u("服务诊断专员", "刚来的长消息", "今天 16:55")
        swept = h._sweep_recent(self._frame([new]))
        self.assertEqual([u.content for u in swept], ["刚来的长消息"])

    def test_foreign_title_still_no_sweep(self):
        """标题对不上 = 换了群：整屏都不能捞（防灌群事故）。"""
        h = self._h()
        u = self._u("别群的人", "别群的消息", "今天 16:55")
        self.assertEqual(h._sweep_recent(self._frame([u], title="别的群")), [])

    def test_no_title_no_sweep(self):
        """标题读不出来（旧版手机端没这个能力）→ 维持「宁可不捞」。"""
        h = self._h()
        u = self._u("甲", "新消息", "今天 16:55")
        self.assertEqual(h._sweep_recent(self._frame([u], title="")), [])

    def test_known_units_still_filtered(self):
        """整屏候选不等于整屏都发：持久库里见过的照样过滤。"""
        fp = self._u("甲", "发过的", "今天 16:50").fp
        h = self._h(known_uids=[f"android:{fp}:1"])
        seen = self._u("甲", "发过的", "今天 16:50")
        fresh = self._u("乙", "新的", "今天 16:55")
        swept = h._sweep_recent(self._frame([seen, fresh]))
        self.assertEqual([u.content for u in swept], ["新的"])

    def test_stale_units_still_filtered(self):
        """超龄/读不出时刻的照旧不捞（时间护栏不因新证据放宽）。"""
        h = self._h()
        old = self._u("甲", "一小时前的", "今天 15:00")
        bare = self._u("乙", "裸今天", "今天")
        swept = h._sweep_recent(self._frame([old, bare]))
        self.assertEqual(swept, [])


class TestAnchorFlushedByTitle(GatewayCase):
    """端到端：锚点被挤出屏 + 屏上无已知消息 → 群名证据直接重建并转发。

    旧行为是这一帧返回「锚点丢失，往上翻一屏找」（body["note"]），要等
    手机翻完、试探找回锚点才发 —— 消息被拖一两分钟，用户只能手动上翻。
    """

    parse = True

    def post_frame(self, xml: str, reason: str = ""):
        req = urllib.request.Request(self.url("/screen"), data=xml.encode("utf-8"),
                                     method="POST")
        req.add_header("X-Bridge-Token", TOKEN)
        req.add_header("Content-Type", "text/plain; charset=utf-8")
        if reason:
            req.add_header("X-Ddwx-Reason", reason)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_flushed_anchor_with_title_delivers(self):
        # 帧 1：基线（屏上只有 1 条）
        body = self.post_frame(frame_xml(
            title_row(GROUP), msg_row("签收跟单专员", "基线那条", 400)))
        self.assertEqual(body["note"], "已建立基线")
        # 帧 2：基线那条被 H5 大卡挤出屏幕，屏上全是新内容；无徽标、群名对上。
        # 时间条必须有：打捞层的时间护栏要求读得出「今天 HH:MM」。
        body = self.post_frame(frame_xml(
            title_row(GROUP), garbled_rows(),
            msg_row("服务诊断专员", "刚来的长消息一", 400),
            msg_row("履约跟单专员", "刚来的长消息二", 700),
            f'<node text="今天 16:55" class="android.widget.TextView" '
            f'package="{PKG}" bounds="[500,300][600,340]"></node>'))
        self.assertEqual(body["messages"], 2,
                         f"note={body.get('note')}")
        self.settle()
        got = sorted(m.content for m in self.received)
        self.assertEqual(got, ["刚来的长消息一", "刚来的长消息二"])

    def test_flushed_anchor_with_foreign_title_does_not_deliver(self):
        """换了群（标题对不上）→ 维持翻页试探，绝不把别群消息灌进来。"""
        other = "【数字质控】宁波慈溪公司"
        self.post_frame(frame_xml(title_row(GROUP),
                                  msg_row("签收跟单专员", "基线那条", 400)))
        body = self.post_frame(frame_xml(
            title_row(other), garbled_rows(),
            msg_row("别群的人", "别群的消息", 400)))
        self.assertNotEqual(body["messages"], 2)
        self.settle()
        self.assertEqual(self.received, [])


if __name__ == "__main__":
    unittest.main()
