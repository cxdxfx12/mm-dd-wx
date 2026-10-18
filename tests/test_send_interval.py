# -*- coding: utf-8 -*-
"""发送间隔随机化（防风控）的单元测试。

背景：2026-10-18 起投递方式改为 OCR + 模拟键鼠，操作轨迹和真人一样，
所以「固定 N 秒一条」这种机械节奏成了被风控的重点特征。`_throttle()`
因此改成两层随机：基础间隔抖动 + 周期性长停顿。

这些测试把「打桩时钟 + 打桩 sleep」封装成一个夹具，只验证**等待时长的
分布特征**，不碰真实时钟、不碰微信。
"""
from __future__ import annotations

import threading
import unittest
from unittest import mock

from bridge.service import BridgeService


class _FakeClock:
    """替身 time 模块：time.time() 走虚拟时钟，sleep() 只记账不真等。"""

    def __init__(self, start: float = 1000.0):
        self.now = start
        self.waits: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, sec: float) -> None:
        self.waits.append(round(float(sec), 3))
        self.now += float(sec)


def _make_service(cfg_wechat):
    """绕过 __init__ 组装一个只带节流所需字段的 service。"""
    svc = BridgeService.__new__(BridgeService)
    svc.cfg = mock.Mock()
    svc.cfg.wechat = cfg_wechat
    svc._send_lock = threading.Lock()
    svc._send_count = 0
    svc._last_send = 0.0
    return svc


def _wechat_cfg(**kw):
    defaults = dict(
        dry_run=True,
        interval=5.0,
        interval_jitter_long=True,
        interval_long_every=3,
        interval_long_range=[8.0, 20.0],
    )
    defaults.update(kw)
    return mock.Mock(**defaults)


class TestThrottleJitter(unittest.TestCase):
    def _run(self, cfg_wechat, times: int) -> list[float]:
        """跑 times 次节流，返回每次实际等待秒数。

        ⚠ 第一次调用不产生等待：`_last_send` 初值是 0，虚拟时钟在 1000，
        delta 巨大 → 判定「距上次发送已经很久，不用等」。这是**正确行为**
        （进程刚启动时第一条本来就该直接发），所以预热一次再统计。
        """
        svc = _make_service(cfg_wechat)
        clock = _FakeClock()
        with mock.patch("bridge.service.time", clock):
            svc._throttle()                       # 预热，不计数
            clock.waits.clear()
            for _ in range(times):
                svc._throttle()
        return clock.waits

    def test_base_interval_within_jitter_band(self):
        """基础间隔必须落在 [interval*0.8, interval*1.6]，不能退回固定值。"""
        cfg = _wechat_cfg(interval_jitter_long=False, interval=5.0)
        waits = self._run(cfg, 20)
        self.assertEqual(len(waits), 20)
        for w in waits:
            self.assertGreaterEqual(w, 4.0 - 0.01)
            self.assertLessEqual(w, 8.0 + 0.01)
        # 有抖动：20 次不能全都同一个值
        self.assertGreater(len(set(waits)), 1, f"间隔没有抖动：{waits}")

    def test_long_pause_every_n(self):
        """每 interval_long_every 条要出现一次明显更长的等待。"""
        cfg = _wechat_cfg(interval=5.0, interval_long_every=3,
                          interval_long_range=[8.0, 20.0])
        waits = self._run(cfg, 9)
        # 第 3、6、9 条带长停顿：总等待应 > 基础上限(8) 
        long_idx = [i for i, w in enumerate(waits) if w > 8.0 + 0.01]
        self.assertEqual(len(long_idx), 3, f"长停顿次数不对：{waits} (index {long_idx})")
        # 长停顿后的总等待不超过 8+20=28 秒
        for i in long_idx:
            self.assertLessEqual(waits[i], 28.0 + 0.01)

    def test_long_pause_can_be_disabled(self):
        """关掉长停顿后，任何一条都不应超过基础间隔上限。"""
        cfg = _wechat_cfg(interval=5.0, interval_jitter_long=False,
                          interval_long_every=3, interval_long_range=[8.0, 20.0])
        waits = self._run(cfg, 12)
        self.assertTrue(all(w <= 8.0 + 0.01 for w in waits), waits)

    def test_zero_interval_does_not_sleep(self):
        """interval=0 是「不节流」，必须一条都不等（老配置兼容）。"""
        cfg = _wechat_cfg(interval=0.0)
        self.assertEqual(self._run(cfg, 5), [])

    def test_respects_already_elapsed_time(self):
        """消息本身间隔很久时不该再补等——真实场景消息是陆续到达的。"""
        cfg = _wechat_cfg(interval=5.0, interval_jitter_long=False)
        svc = _make_service(cfg)
        clock = _FakeClock()
        with mock.patch("bridge.service.time", clock):
            svc._last_send = clock.now - 60.0      # 上一条是 60 秒前发的
            svc._throttle()
        self.assertEqual(clock.waits, [])

    def test_bad_long_range_falls_back(self):
        """interval_long_range 配错（非法/反序）不能崩，要能兜底。"""
        cfg = _wechat_cfg(interval_long_every=1, interval_long_range=[20.0, 8.0])
        waits = self._run(cfg, 3)
        self.assertEqual(len(waits), 3)
        cfg2 = _wechat_cfg(interval_long_every=1, interval_long_range=None)
        self.assertEqual(len(self._run(cfg2, 3)), 3)


if __name__ == "__main__":
    unittest.main()
