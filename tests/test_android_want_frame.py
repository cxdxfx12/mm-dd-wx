"""电脑侧主动「请手机补一帧」（want_frame，0.42）。

现场（2026-10-27，19:19:36 → 19:52:23）
    用户重启程序之后 **33 分钟零帧**，日志里连一条「手机画面已累计」都没有，
    直到他自己翻了一下手机，画面 md5 变了，才突然补上一整屏并转发出去。
    原话：「又是我手翻手机才触发转发的」。

    根因是两端各自的正确设计撞在一起：
      · 手机端「画面 md5 没变就不上报」—— 省流量，对；
      · 电脑端「第一帧只建基线，之后只认新来的」—— 防刷群，对。
    但电脑重启后手上**没有基线**，手机又觉得「跟上次一模样，没什么可报的」，
    于是谁也不动 —— 群里来的消息就这么干等着，只有人手动翻屏才能破局。

    修法：电脑在每次心跳回包里说「我现在要一帧」（``want_frame`` = 电脑的
    墙上时间秒），手机看到比自记记录大的编号就清掉 md5 指纹、立刻重发一次。

    ⚠ 编号必须是**墙上时间**而不是自增计数：电脑重启后计数从 0 开始，会小于
    手机记住的旧值，手机当成「处理过的老请求」忽略掉 —— 死锁原样复现。
"""
from __future__ import annotations

import json
import sys
import time
import unittest
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from bridge.sources import android_gateway as G          # noqa: E402
from bridge.sources.android_gateway import _Ctx, _want_frame   # noqa: E402
from bridge.sources.android_parse import FrameDiffer     # noqa: E402
from tests.test_android_gateway import GatewayCase       # noqa: E402


def ctx(parse: bool = True) -> _Ctx:
    c = _Ctx(token="t", chat_name="数字质控")
    if parse:
        c.parse = True
        c.differ = FrameDiffer()
    return c


class TestWantFrameLogic(unittest.TestCase):

    def test_asks_while_no_baseline(self):
        """没有基线 = 屏上有什么我都看不见，必须求一帧。"""
        c = ctx()
        self.assertFalse(c.differ.baseline_built)
        self.assertGreater(_want_frame(c), 0,
                           "电脑侧没有基线却不求帧 —— 手机画面不变就永远不发，"
                           "用户只能靠自己翻手机破局")

    def test_throttled_within_gap(self):
        """刚求过就给同一个编号：手机已经处理过，不会重复发帧。"""
        c = ctx()
        first = _want_frame(c)
        second = _want_frame(c)
        self.assertEqual(first, second,
                         "同一个求帧请求在节流窗口内换了编号 —— 手机每 60 秒"
                         "心跳都会被要求重发一整屏（几百 KB）")

    def test_new_number_after_gap(self):
        """过了节流窗口还没基线 → 换新编号，让手机再试一次。"""
        c = ctx()
        # 模拟「上一次求帧发生在 61 秒前」：时间往后拨，编号也跟着变小
        c.frame_req = int(time.time()) - 100
        c.frame_req_at = time.time() - (G.WANT_FRAME_MIN_GAP + 1)
        old = c.frame_req
        got = _want_frame(c)
        self.assertGreater(got, old,
                           "节流窗口过了却不换编号 —— 手机以为自己处理过这个"
                           "请求，不会再发帧，电脑就一直瞎着")

    def test_quiet_when_baseline_fresh(self):
        """基线建好、画面又是刚收到的 → 不要打扰手机。"""
        c = ctx()
        c.differ.baseline_built = True
        c.screen = {"ts": time.time(), "count": 3}
        self.assertEqual(_want_frame(c), 0)

    def test_asks_after_long_silence(self):
        """半小时一帧都没有 → 求一帧自证「我还能看见」。"""
        c = ctx()
        c.differ.baseline_built = True
        c.screen = {"ts": time.time() - (G.WANT_FRAME_IDLE_S + 60), "count": 3}
        self.assertGreater(
            _want_frame(c), 0,
            "基线在、但半小时零帧 —— 分不清「群里真没人说话」和「采集瞎了」"
            "（灭屏挂起 / 无障碍被关 / App 被杀），必须求一帧来自证")

    def test_number_is_wall_clock_not_counter(self):
        """编号必须是墙上时间：电脑重启后不能比手机记住的旧值小。"""
        c = ctx()
        got = _want_frame(c)
        self.assertGreater(got, 1_600_000_000,
                           "编号看着像自增计数 —— 电脑重启后会归零，手机把新请求"
                           "当成老请求忽略，死锁会原样复现")
        self.assertLessEqual(got, int(time.time()) + 5)

    def test_survives_restart(self):
        """模拟电脑重启：新 ctx 的编号也要比手机记住的旧值大。

        编号是**墙上时间秒**，所以即使内存状态全丢，新值天然大于旧值。
        改成自增计数就会在此处失败：重启后从 1 开始，手机（持久化记着
        1793102294）会把这个新请求当成处理过的老请求丢掉 —— 死锁复现。
        """
        c = ctx()
        old = _want_frame(c) - 5              # 假设手机记住的是 5 秒前的那个编号
        c2 = ctx()                            # 重启 = 所有内存状态归零
        new = _want_frame(c2)
        self.assertGreater(new, old,
                           "重启后的编号不比重启前大 —— 手机端是持久化记住编号的，"
                           "会把这个新建的请求当成老请求丢掉")

    def test_no_differ_does_not_crash(self):
        """只收不转模式（android_parse=false）没有 differ，不能崩。"""
        c = ctx(parse=False)
        self.assertEqual(_want_frame(c), 0)


class PingCarriesWantFrame(GatewayCase):
    """真网关：心跳回包必须带得动这个请求。"""

    parse = True

    def op(self):
        # 本机若有系统代理，urllib 会把 127.0.0.1 当外网发出去（见 MEMORY 的 502）
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def post_ping(self) -> dict:
        req = urllib.request.Request(
            self.url("/ping"), data=b"{}", method="POST",
            headers={"X-Bridge-Token": self.token,
                     "X-Ddwx-Device": "PAL-AL00",
                     "Content-Type": "application/json"})
        return json.loads(self.op().open(req, timeout=6).read().decode())

    def test_heartbeat_carries_want_frame(self):
        """刚起网关、一帧都没收到 → 心跳里必须求帧。"""
        self.assertGreater(
            self.post_ping().get("want_frame", 0), 0,
            "网关刚起来没有任何画面，却不在心跳里求帧 —— 手机画面不变就"
            "永远不发，用户重启程序后要等到自己翻手机才有消息")

    def test_get_ping_shows_request_number(self):
        """GET /ping（人工自测）能看出电脑最近求过帧、手机理没理。"""
        self.post_ping()
        req = urllib.request.Request(self.url("/ping"),
                                     headers={"X-Bridge-Token": self.token})
        got = json.loads(self.op().open(req, timeout=6).read().decode())
        self.assertIn("want_frame", got)
        self.assertGreater(got["want_frame"], 0)


if __name__ == "__main__":
    unittest.main()
