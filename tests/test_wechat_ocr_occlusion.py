# -*- coding: utf-8 -*-
"""遮挡检测与前台抢夺（``WeChatOcrTarget._ensure_unoccluded``）。

真机 2026-10-18 用户报「微信被挡住了一点就投递失败」：

- ``_grab`` 走 ``ImageGrab.grab(bbox=窗口矩形)``，抓的是**屏幕像素**；
  窗口被压住一块，那块抓到的就是别人的画面 —— 日志里的形状是
  「会话列表首屏没找到目标（最高相似度 0.14 < 0.62）」「当前可见会话：
  essag」，看起来像 OCR 识别问题，实际是**抓到了错误的内容**。
- 旧 ``_activate`` 只调 ``SetForegroundWindow`` 且不校验结果：Windows 的
  前台锁会**静默拒绝**跨进程抢前台（任务栏闪一下就完），`IsWindowVisible`
  又只回答「没隐藏」，不回答「没被压」。

测试策略（⚠ 项目铁律：**禁止 mock.patch 模块级 ``_user32``** —— 那是
ctypes DLL 对象，没有真实属性可还原，撤销时 0xC0000005 段错误，2026-10-18
真踩过）。所以把 Win32 调用替换在**实例方法**一层：
``_rect`` / ``_occluded_points`` / ``_activate`` / ``_is_foreground`` 都是
普通方法，直接在实例上挂 lambda 顶替即可。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.targets.wechat_ocr import WeChatOcrTarget  # noqa: E402


def make_target() -> WeChatOcrTarget:
    """不碰真实 Win32/UIA 的实例（每个用例自己往实例上挂桩）。"""
    return WeChatOcrTarget(dry_run=True, focus_delay=0.0)


class TestOccludedPoints(unittest.TestCase):
    """``_occluded_points``：窗口矩形里被别的窗口压住的采样点。

    ⚠ ``WindowFromPoint`` 的完整交互在单测里演不了（要碰 user32，违反铁律），
    完整路径靠真机冒烟验证 —— 2026-10-18 实机：微信被压住 2 个采样点时
    检测出 ``[(1876, 142), (1876, 578)]``，顶回前台后归零。这里锁的是
    **退化路径不抛异常、不摸屏幕**。
    """

    def test_rect_none_means_no_probe(self):
        """窗口矩形取不到（``_rect`` 返回 None）→ 不了了之，不许抛异常。"""
        tgt = make_target()
        tgt._rect = lambda h: None                           # type: ignore
        self.assertEqual(tgt._occluded_points(1234), [])

    def test_zero_size_window_means_no_probe(self):
        """0×0 的窗口（GetWindowRect 失败后的残值）也别去点屏幕。"""
        tgt = make_target()
        tgt._rect = lambda h: (100, 100, 100, 100)           # type: ignore
        self.assertEqual(tgt._occluded_points(1234), [])


class TestEnsureUnoccluded(unittest.TestCase):
    """``_ensure_unoccluded``：挡了就再顶一次，还不行就如实报错。"""

    def test_passes_when_never_occluded(self):
        tgt = make_target()
        tgt._occluded_points = lambda h: []                  # type: ignore
        ok, why = tgt._ensure_unoccluded(1234)
        self.assertTrue(ok)
        self.assertEqual(why, "")

    def test_retry_then_pass(self):
        """第一次有遮挡 → 触发一次 ``_activate`` → 第二次干净 → 放行。"""
        tgt = make_target()
        calls = {"occluded": 0, "activate": 0}

        def fake_occluded(h):
            calls["occluded"] += 1
            return [] if calls["occluded"] >= 2 else [(300, 400)]

        tgt._occluded_points = fake_occluded                 # type: ignore
        tgt._activate = lambda h: calls.__setitem__(         # type: ignore
            "activate", calls["activate"] + 1) or True
        ok, why = tgt._ensure_unoccluded(1234)
        self.assertTrue(ok)
        self.assertEqual(calls["activate"], 1, "该再顶一次")

    def test_fails_honestly_when_still_occluded(self):
        """顶了还不干净 → 拒发，错误信息里要有坐标（用户能照着找）。"""
        tgt = make_target()
        tgt._occluded_points = lambda h: [(300, 400), (500, 600)]  # type: ignore
        tgt._activate = lambda h: True                       # type: ignore
        ok, why = tgt._ensure_unoccluded(1234)
        self.assertFalse(ok)
        self.assertIn("遮挡", why)
        self.assertIn("(300, 400)", why, "错误信息要给出具体坐标")


class TestActivateVerifiesForeground(unittest.TestCase):
    """``_activate`` 的新约定：SetForegroundWindow 之后**必须回读校验**。

    完整的 Win32 交互没法在单测里演（碰 user32 违反铁律），这里锁的是
    「校验函数存在且语义正确」+ 真机冒烟的结论。2026-10-18 实机验证：
    微信在屏幕右侧被压住 2 个采样点时，``_ensure_unoccluded`` 把它顶回
    前台，遮挡清零、``_grab`` 抓到完整画面（修复前 OCR 只读到「essag」）。
    """

    def test_is_foreground_rejects_zero(self):
        """前台句柄为 0（桌面无人持有）→ 不算在前台。"""
        self.assertFalse(WeChatOcrTarget._is_foreground(0))

    def test_activate_is_checkable(self):
        """``_activate`` / ``_is_foreground`` / ``_check_visible`` 都在。"""
        for name in ("_activate", "_is_foreground", "_check_visible",
                     "_occluded_points", "_ensure_unoccluded"):
            self.assertTrue(hasattr(WeChatOcrTarget, name), name)


if __name__ == "__main__":
    unittest.main()
