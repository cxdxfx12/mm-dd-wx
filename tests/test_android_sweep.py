"""基线打捞（_sweep_recent，0.41）与附件抓取超时（_attach_timeout_check）。

现场（2026-10-27）：
    · 用户原话「还是老是漏掉消息，改成 15 分钟巡检，手机和当前时间相差在
      30 分钟的消息要转发」。首帧建基线 / 锚点丢失重建都会把屏上一切登记
      成「已知历史」，程序重启、手机离线那一刻屏上的真新消息被吞且不入库。
    · 「明细附件：客户声音待履约汇总….xlsx」17:57 下发抓取指令后手机端
      一直没回传，PC 日志只有「发现附件」一行，冷却还占着，卡片再渲染也
      不会重新下单 —— 超时了必须告警并放行。
"""

import time
import unittest
from datetime import datetime
from types import SimpleNamespace

from bridge.sources import android_gateway as G
from bridge.sources.android_gateway import _Ctx, _Handler
from bridge.sources.android_parse import parse_frame, unit_age_seconds

PKG = "com.alibaba.android.rimet"

# 聊天页判据靠输入区（detect_page）—— 没有 它整帧被判「不是聊天页」。
INPUT_BAR = (
    f'<node index="70" text="" class="android.widget.LinearLayout" package="{PKG}" '
    f'bounds="[0,2180][1176,2300]">'
    f'<node index="71" text="发消息或按住说话..." class="android.widget.TextView" '
    f'package="{PKG}" bounds="[182,2191][981,2262]"></node></node>'
)


def make_handler(ctx: _Ctx | None = None) -> _Handler:
    """不跑 BaseHTTPRequestHandler.__init__，只挂上 ctx 用纯逻辑方法。"""
    h = object.__new__(_Handler)
    h.ctx = ctx or _Ctx(token="t", chat_name="数字质控")
    return h


def frame_of(*rows: str) -> str:
    body = "".join(rows)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<hierarchy rotation="0">'
        f'<node index="0" text="" class="android.widget.FrameLayout" package="{PKG}" '
        f'bounds="[0,0][1080,2340]">'
        f'<node index="1" text="" class="androidx.recyclerview.widget.RecyclerView" '
        f'package="{PKG}" scrollable="true" bounds="[0,200][1080,2100]">'
        f'{body}</node>{INPUT_BAR}</node></hierarchy>'
    )


def row_time(text: str, top: int) -> str:
    return (f'<node index="1" text="{text}" class="android.widget.TextView" '
            f'package="{PKG}" bounds="[500,{top}][600,{top + 40}]"></node>')


def row_msg(sender: str, text: str, top: int) -> str:
    return (f'<node index="1" text="" class="android.widget.LinearLayout" package="{PKG}" '
            f'bounds="[60,{top}][1020,{top + 180}]">'
            f'<node index="2" text="{sender}" class="android.widget.TextView" '
            f'package="{PKG}" bounds="[80,{top}][420,{top + 60}]"></node>'
            f'<node index="3" text="{text}" class="android.widget.TextView" '
            f'package="{PKG}" bounds="[80,{top + 70}][1000,{top + 170}]"></node>'
            f'</node>')


class TestUnitAgeSeconds(unittest.TestCase):

    def test_parses_today_yesterday(self):
        now = datetime(2026, 10, 27, 18, 0, 0)
        age = unit_age_seconds("今天 17:57", now)
        self.assertEqual(age, 3 * 60.0)
        age = unit_age_seconds("昨天 17:57", now)
        self.assertEqual(age, 24 * 3600.0 + 3 * 60.0)

    def test_unreadable_strips_return_none(self):
        """裸「今天」、星期、日期都没有时刻 —— 读不出就 None，绝不瞎猜。"""
        for ts in ("今天", "星期一", "10-16", "2026-10-16", "", None):
            self.assertIsNone(unit_age_seconds(ts))

    def test_clock_skew_clamped_to_zero(self):
        """手机时钟快几十秒时年龄是负数 —— 按 0 处理（刚到的新消息）。"""
        now = datetime(2026, 10, 27, 18, 0, 0)
        self.assertEqual(unit_age_seconds("今天 18:01", now), 0.0)


class TestParseFrameAssignsTs(unittest.TestCase):

    def test_units_inherit_nearest_strip_above(self):
        f = parse_frame(frame_of(
            row_msg("甲", "时间条之前", 250),
            row_time("今天 17:57", 500),
            row_msg("乙", "时间条之后", 600),
        ))
        self.assertEqual([u.ts for u in f.units], ["", "今天 17:57"])

    def test_old_date_strip_assigned_too(self):
        """「星期一」也原样归属 —— 消费方解不出时刻自然不捞。"""
        f = parse_frame(frame_of(
            row_time("星期一", 250),
            row_msg("甲", "正文", 400),
        ))
        self.assertEqual(f.units[0].ts, "星期一")


class TestSweepRecent(unittest.TestCase):

    def _h(self, known_uids=()):
        h = make_handler()
        h.ctx.dup_check = lambda uid: uid in set(known_uids)
        return h

    @staticmethod
    def _u(sender, content, ts, clipped_below=0):
        """测试单元：y0/y1 必须落在可视区里，否则会被当成「顶边被夹」排除。"""
        return G.Unit(sender=sender, content=content, ts=ts,
                      y0=500, y1=600, clipped_below=clipped_below)

    def _frame(self, units, hints=()):
        return SimpleNamespace(units=units, view=(0, 200, 1080, 2100),
                               hints=hints)

    def test_no_evidence_means_no_sweep(self):
        """屏上没有一条「见过」的 = 换了群/全新安装，一条都不能捞。"""
        h = self._h(known_uids=[])
        u = self._u("甲", "新消息", "今天 17:57")
        self.assertEqual(h._sweep_recent(self._frame([u])), [])

    def test_ui_hints_substitute_for_evidence(self):
        """没有任何已入库消息，但屏上有「不在最新处」特征（假底部）——
        整屏都是打捞候选（2026-10-30 大江东漏消息的修复）。"""
        now = datetime.now()
        new = self._u("乙", "新的没见过", now.strftime("今天 %H:%M"))
        h = self._h(known_uids=[])
        swept = h._sweep_recent(
            self._frame([new], hints=("99+未读徽标",)))
        self.assertEqual([u.fp for u in swept], [new.fp])

    def test_ui_hints_do_not_lift_age_guard(self):
        """UI 特征只补「证据」，不放宽时间护栏：超龄的一律不捞。"""
        stale = self._u("乙", "昨天的消息", "昨天 08:00")
        h = self._h(known_uids=[])
        self.assertEqual(h._sweep_recent(
            self._frame([stale], hints=("查看更多/回到最新",))), [])

    def test_sweeps_unknown_recent_below_evidence(self):
        """证据之下的 30 分钟内新消息要捞出来 —— 这就是用户要的机制。"""
        now = datetime.now()
        old = self._u("甲", "老的已入库", "今天 08:00")
        new = self._u("乙", "新的没见过", now.strftime("今天 %H:%M"))
        h = self._h(known_uids=[f"android:{old.fp}:1"])
        swept = h._sweep_recent(self._frame([old, new]))
        self.assertEqual([u.fp for u in swept], [new.fp])

    def test_old_and_untimed_units_are_not_swept(self):
        """超龄（昨天 08:00）与读不出时刻（星期一）的一律不捞 —— 翻历史
        触发的重建靠这条不把旧消息灌进群。"""
        known = self._u("甲", "老的已入库", "今天 08:00")
        stale = self._u("乙", "昨天的消息", "昨天 08:00")
        untimed = self._u("丙", "上周的消息", "星期一")
        h = self._h(known_uids=[f"android:{known.fp}:1"])
        swept = h._sweep_recent(self._frame([known, stale, untimed]))
        self.assertEqual(swept, [])

    def test_known_units_are_not_swept(self):
        """已入库的（哪怕 1 分钟前）不再捞 —— 重启后第一屏不重发。"""
        now = datetime.now()
        seen = self._u("甲", "发过的", now.strftime("今天 %H:%M"))
        h = self._h(known_uids=[f"android:{seen.fp}:1"])
        self.assertEqual(h._sweep_recent(self._frame([seen])), [])

    def test_clipped_unit_is_not_swept(self):
        """被屏幕下沿夹掉的消息内容是错的 —— 交给长消息补全通道，不捞。"""
        now = datetime.now()
        known = self._u("甲", "老的已入库", "今天 08:00")
        cut = self._u("乙", "被夹的", now.strftime("今天 %H:%M"),
                      clipped_below=300)
        h = self._h(known_uids=[f"android:{known.fp}:1"])
        swept = h._sweep_recent(self._frame([known, cut]))
        self.assertEqual(swept, [])


class TestAttachTimeout(unittest.TestCase):

    def test_stale_order_released_and_flagged(self):
        """下单 120 秒没收到文件 → 撤冷却 + 告警。17:57 那次就是这种。"""
        h = make_handler()
        h.ctx.attach_orders["客户声音待履约汇总_311392_20261027.xlsx"] = \
            time.monotonic() - (G.ATTACH_FETCH_TIMEOUT + 5)
        h.ctx.attach_file_at = 0.0
        G._attach_timeout_check(h.ctx)
        self.assertEqual(h.ctx.attach_orders, {})

    def test_file_received_since_order_is_silent(self):
        """期间收到过文件 = 链路活着，撤冷却但不告警（不吓人）。"""
        h = make_handler()
        at = time.monotonic() - (G.ATTACH_FETCH_TIMEOUT + 5)
        h.ctx.attach_orders["a.xlsx"] = at
        h.ctx.attach_file_at = at + 30
        G._attach_timeout_check(h.ctx)
        self.assertEqual(h.ctx.attach_orders, {})

    def test_fresh_order_untouched(self):
        """冷却期内（<120s）不下手 —— 正常链路 8~12 秒，别误伤。"""
        h = make_handler()
        h.ctx.attach_orders["a.xlsx"] = time.monotonic() - 30
        G._attach_timeout_check(h.ctx)
        self.assertIn("a.xlsx", h.ctx.attach_orders)

    def test_timeout_warning_carries_last_phone_status(self):
        """告警要带手机端最后一句结论 —— 否则分不清「没收到」「点了没成」。"""
        h = make_handler()
        h.ctx.last_attach = "收到，这就去下载"
        h.ctx.attach_orders["运单明细"] = \
            time.monotonic() - (G.ATTACH_FETCH_TIMEOUT + 5)
        with self.assertLogs(level="WARNING") as cm:
            G._attach_timeout_check(h.ctx)
        self.assertIn("收到，这就去下载", "\n".join(cm.output))

    def test_attach_pending_exposes_waiting_orders(self):
        """「下单了但没到」的订单要能在 /ping 上看到挂了多久（排查用）。"""
        h = make_handler()
        h.ctx.attach_orders["运单明细"] = time.monotonic() - 42
        pending = G._attach_pending(h.ctx)
        self.assertEqual(list(pending), ["运单明细"])
        self.assertGreaterEqual(pending["运单明细"], 42)


class TestUiHints(unittest.TestCase):
    """「视口不在最新处」的 UI 特征识别（99+ 徽标 / 查看更多消息 / 回到最新）。"""

    def _xml(self, *extra_nodes: str) -> str:
        return frame_of(row_msg("甲", "普通消息", 300), *extra_nodes)

    def test_badge_and_more_marker_detected(self):
        f = parse_frame(self._xml(
            '<node index="5" text="99+" class="android.widget.TextView" '
            f'package="{PKG}" bounds="[500,900][560,940]"></node>',
            '<node index="6" text="查看更多消息" class="android.widget.TextView" '
            f'package="{PKG}" bounds="[400,1200][680,1260]"></node>',
        ))
        self.assertIn("99+未读徽标", f.hints)
        self.assertIn("查看更多/回到最新", f.hints)
        self.assertTrue(f.not_at_latest)

    def test_text_inside_message_content_is_ignored(self):
        """正文里恰好有「99+」/「查看更多消息」不能算屏上特征。"""
        f = parse_frame(frame_of(
            row_msg("甲", "本群已有 99+ 人，查看更多消息请联系管理员", 300),
        ))
        self.assertEqual(f.hints, ())
        self.assertFalse(f.not_at_latest)

    def test_bubble_with_exact_marker_text_is_ignored(self):
        """有人真把「回到最新」当消息发出来（带发送者）也不能算特征。"""
        f = parse_frame(frame_of(row_msg("甲", "回到最新", 300)))
        self.assertEqual(f.hints, ())

    def test_plain_chat_frame_has_no_hints(self):
        f = parse_frame(self._xml())
        self.assertEqual(f.hints, ())


class TestFakeBottomGuard(unittest.TestCase):
    """建基线前的「假底部」护栏（_guard_fake_bottom）。"""

    def _h(self):
        return make_handler()

    def test_bails_and_asks_bottom_when_hints_present(self):
        h = self._h()
        frame = SimpleNamespace(units=[], hints=("99+未读徽标",))
        self.assertTrue(h._guard_fake_bottom(frame))
        self.assertTrue(h.ctx.want_bottom)       # 会向手机下发 to_bottom
        self.assertTrue(h.ctx.await_bottom)

    def test_second_frame_passes(self):
        """已在等回底：不再拦（否则 H5 容器特征清不掉会死循环）。"""
        h = self._h()
        frame = SimpleNamespace(units=[], hints=("99+未读徽标",))
        h._guard_fake_bottom(frame)
        self.assertFalse(h._guard_fake_bottom(frame))
        self.assertFalse(h.ctx.await_bottom)     # 标志一次性用掉

    def test_bottom_frame_passes(self):
        h = self._h()
        h.ctx.frame_reason = "probe-bottom"
        frame = SimpleNamespace(units=[], hints=("99+未读徽标",))
        self.assertFalse(h._guard_fake_bottom(frame))

    def test_no_hints_means_no_op(self):
        h = self._h()
        frame = SimpleNamespace(units=[], hints=())
        self.assertFalse(h._guard_fake_bottom(frame))
        self.assertFalse(h.ctx.want_bottom)

    def test_await_bottom_timeout_sets_give_up_pass(self):
        """回底等超时 → 认输那条路必须留放行牌，否则被自己的护栏打回。"""
        h = self._h()
        h.ctx.await_bottom = True
        h.ctx.await_bottom_at = time.monotonic() - (G.PROBE_SETTLE_MAX + 5)
        self.assertEqual(h._probe_decision("md5", "sig"), "give_up")
        self.assertTrue(h.ctx.bottom_gave_up)
        self.assertFalse(h.ctx.await_bottom)     # _reset_probe 清掉的

    def test_give_up_pass_is_not_reblocked(self):
        """真机 2026-10-31 09:31 死循环：认输后又被 hints 拦下 → 57 秒一轮。"""
        h = self._h()
        frame = SimpleNamespace(units=[], hints=("99+未读徽标",))
        self.assertTrue(h._guard_fake_bottom(frame))    # 第一次：请回底
        h.ctx.await_bottom_at = time.monotonic() - (G.PROBE_SETTLE_MAX + 5)
        self.assertEqual(h._probe_decision("md5", "sig"), "give_up")
        self.assertFalse(h._guard_fake_bottom(frame))   # 放行：就地建基线
        self.assertFalse(h.ctx.bottom_gave_up)          # 牌一次性用掉
        self.assertFalse(h.ctx.await_bottom)            # 不再等回底

    def test_give_up_pass_does_not_stick(self):
        """放行牌只用一次：下一次锚点丢失照常回底，不变成「永不回底」。"""
        h = self._h()
        frame = SimpleNamespace(units=[], hints=("99+未读徽标",))
        h.ctx.bottom_gave_up = True
        self.assertFalse(h._guard_fake_bottom(frame))
        self.assertTrue(h._guard_fake_bottom(frame))    # 又要求回底了
        self.assertTrue(h.ctx.await_bottom)


if __name__ == "__main__":
    unittest.main()
