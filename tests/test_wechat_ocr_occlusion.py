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

import ctypes
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
                     "_occluded_points", "_ensure_unoccluded",
                     "_grab_foreground", "_topmost_pulse",
                     "_focus_and_check"):
            self.assertTrue(hasattr(WeChatOcrTarget, name), name)


class TestTopmostConstants(unittest.TestCase):
    """窗口层级常量必须是**模块级**的，不能写成 ``wt.HWND_TOPMOST``。

    真机 2026-10-18 21:03：投递直接失败，日志只有一句
    「激活微信窗口失败：module 'ctypes.wintypes' has no attribute
    'HWND_TOPMOST'」—— ``ctypes.wintypes`` 里根本没有这两个常量（它们是
    winuser.h 的宏，不是 wintypes 的成员）。写成 ``wt.`` 前缀会抛
    ``AttributeError``，被 ``_activate`` 的 except 吞掉后返回 False，
    表现为「无法把微信窗口切到前台」。

    这条用例就是那次事故的钉子：常量存在、类型是 c_void_p、值是 -1/-2。
    """

    def test_constants_exist_at_module_level(self):
        from bridge.targets import wechat_ocr as W
        self.assertTrue(hasattr(W, "HWND_TOPMOST"))
        self.assertTrue(hasattr(W, "HWND_NOTOPMOST"))

    def test_constant_values(self):
        """HWND_TOPMOST=(HWND)-1、HWND_NOTOPMOST=(HWND)-2（c_void_p 补码）。

        ⚠ ``c_void_p(-1).value`` 读出来是**无符号**的 ``2**64-1``，不是 -1
        —— 别按 -1 断言，会误以为常量写错了。
        """
        from bridge.targets import wechat_ocr as W
        self.assertEqual(W.HWND_TOPMOST.value, ctypes.c_void_p(-1).value)
        self.assertEqual(W.HWND_NOTOPMOST.value, ctypes.c_void_p(-2).value)

    def test_wintypes_has_no_such_attribute(self):
        """锁住事实：``ctypes.wintypes`` 里没有 HWND_TOPMOST —— 别改回 ``wt.``。"""
        import ctypes.wintypes as wt
        self.assertFalse(hasattr(wt, "HWND_TOPMOST"))
        self.assertFalse(hasattr(wt, "HWND_NOTOPMOST"))


class TestGrabForeground(unittest.TestCase):
    """``_grab_foreground``：抢不到要**如实返回 False**，不能假装成功。"""

    def test_returns_true_when_first_round_works(self):
        tgt = make_target()
        tgt._topmost_pulse = lambda h: None                   # type: ignore
        tgt._focus_and_check = lambda h, w: True              # type: ignore
        self.assertTrue(tgt._grab_foreground(1234))

    def test_returns_false_after_all_rounds_fail(self):
        """三招全失灵 → False（由调用方决定是拒发还是硬着头皮上）。"""
        tgt = make_target()
        tgt._topmost_pulse = lambda h: None                   # type: ignore
        tgt._focus_and_check = lambda h, w: False             # type: ignore
        self.assertFalse(tgt._grab_foreground(1234, rounds=1))

    def test_retries_up_to_rounds(self):
        tgt = make_target()
        calls = {"n": 0}

        def pulse(h):
            calls["n"] += 1

        tgt._topmost_pulse = pulse                            # type: ignore
        tgt._focus_and_check = lambda h, w: False             # type: ignore
        tgt._grab_foreground(1234, rounds=3)
        self.assertEqual(calls["n"], 3, "每一轮都要试一次置顶")


if __name__ == "__main__":
    unittest.main()
