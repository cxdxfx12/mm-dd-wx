# -*- coding: utf-8 -*-
"""任意屏幕 / 任意缩放都能投递：几何层的模拟回归。

2026-11-06 事故：主屏 125% + 加副屏后 worker 没声明 DPI，截图与窗口
坐标差 25%。修完 DPI 又发现两个「换台机器就废」的硬编码：
  * 发送前无条件 ``MoveWindow(720, 1088)`` —— 小屏上窗口比屏幕还高；
  * ``TITLE_X0_MAX_PX = 316`` 是 100% 下的 DIP，缩放屏上切掉群名开头。

这里用假的显示器拓扑（不碰真桌面）把常见机型全演一遍，保证
``bridge.screen`` 与 ``wechat_ocr`` 的换算、夹取、可见性判断都成立。
"""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge import screen                                    # noqa: E402
from bridge.screen import Monitor                            # noqa: E402
from bridge.targets import wechat_ocr as m                   # noqa: E402
from bridge.targets.wechat_ocr import WeChatOcrTarget        # noqa: E402


def _mon(rect, work=None, dpi=96, primary=False) -> Monitor:
    return Monitor(rect=rect, work=work or rect, dpi=dpi, primary=primary)


# 常见机器配置（全部是虚拟桌面物理像素坐标）
MAIN_1080P = _mon((0, 0, 1920, 1080), (0, 0, 1920, 1040), 96, True)
MAIN_125 = _mon((0, 0, 2560, 1440), (0, 0, 2560, 1400), 120, True)
MAIN_4K150 = _mon((0, 0, 3840, 2160), (0, 0, 3840, 2100), 144, True)
LAPTOP_768 = _mon((0, 0, 1366, 768), (0, 0, 1366, 728), 96, True)
RIGHT_1080P = _mon((2560, 0, 4480, 1080), (2560, 0, 4480, 1040), 96)
LEFT_NEG = _mon((-1920, 0, 0, 1080), (-1920, 0, 0, 1040), 96)


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestMonitorBasics(unittest.TestCase):
    def test_scale_from_dpi(self):
        self.assertEqual(_mon((0, 0, 100, 100), dpi=96).scale, 1.0)
        self.assertEqual(_mon((0, 0, 100, 100), dpi=120).scale, 1.25)
        self.assertEqual(_mon((0, 0, 100, 100), dpi=144).scale, 1.5)

    def test_label_contains_scale_and_rect(self):
        s = MAIN_125.label()
        self.assertIn("125%", s)
        self.assertIn("(0,0)-(2560,1440)", s)
        self.assertIn("主屏", s)

    def test_monitor_for_rect_picks_largest_overlap(self):
        ms = [MAIN_1080P, RIGHT_1080P]
        with patch.object(screen, "monitors", return_value=ms):
            self.assertIs(screen.monitor_for_rect((100, 100, 500, 500)),
                          MAIN_1080P)
            self.assertIs(screen.monitor_for_rect((2600, 100, 3000, 500)),
                          RIGHT_1080P)
            # 跨屏窗口按重叠面积大的那一侧归属
            self.assertIs(screen.monitor_for_rect((1800, 100, 2600, 500)),
                          MAIN_1080P)
            self.assertIs(screen.monitor_for_rect((2400, 100, 2700, 500)),
                          RIGHT_1080P)

    def test_monitor_for_rect_handles_negative_x_secondary(self):
        ms = [MAIN_1080P, LEFT_NEG]
        with patch.object(screen, "monitors", return_value=ms):
            self.assertIs(screen.monitor_for_rect((-1500, 100, -900, 500)),
                          LEFT_NEG)

    def test_monitor_for_offscreen_rect_falls_back_to_nearest(self):
        ms = [MAIN_1080P, RIGHT_1080P]
        with patch.object(screen, "monitors", return_value=ms):
            # 拔掉副屏后窗口还留在 x=3000：应归到最近的副屏，不能崩
            self.assertIsNotNone(screen.monitor_for_rect((3000, 0, 3720, 1088)))

    def test_scale_for_window_uses_that_monitor(self):
        with patch.object(screen, "monitors", return_value=[MAIN_125, RIGHT_1080P]):
            self.assertEqual(screen.scale_for_window(0), 1.25)   # 无句柄→主屏

    def test_describe_reports_topology_and_awareness(self):
        with patch.object(screen, "monitors", return_value=[MAIN_125, LEFT_NEG]), \
             patch.object(screen, "awareness", return_value=2), \
             patch.object(screen, "virtual_rect", return_value=(-1920, 0, 2560, 1440)):
            text = "\n".join(screen.describe())
        self.assertIn("2 台显示器", text)
        self.assertIn("125%", text)
        self.assertIn("Per-Monitor V2", text)
        self.assertIn("各屏缩放不同", text)

    def test_describe_warns_when_dpi_not_declared(self):
        with patch.object(screen, "monitors", return_value=[MAIN_1080P]), \
             patch.object(screen, "awareness", return_value=0), \
             patch.object(screen, "virtual_rect", return_value=(0, 0, 1920, 1080)):
            text = "\n".join(screen.describe())
        self.assertIn("未声明", text)
        self.assertIn("坐标会错位", text)


class TestFitRect(unittest.TestCase):
    """窗口矩形必须完整落在**同一台**显示器工作区里。"""

    def _fit(self, mon, rect, want_w, want_h):
        with patch.object(screen, "monitors", return_value=[mon]):
            return screen.fit_rect(rect, want_w, want_h)

    def test_1080p_clamps_height_into_work_area(self):
        r = self._fit(MAIN_1080P, (100, 20, 820, 1108), 720, 1088)
        self.assertEqual((r[2] - r[0], r[3] - r[1]), (720, 1024))
        self.assertGreaterEqual(r[0], 0)
        self.assertLessEqual(r[3], 1040)
        with patch.object(screen, "monitors", return_value=[MAIN_1080P]):
            self.assertTrue(screen.is_fully_visible(r))

    def test_125pct_keeps_proportional_window(self):
        # 125% 下的目标尺寸是 900×1360 物理像素，不是写死的 720×1088
        r = self._fit(MAIN_125, (100, 20, 964, 1354), 900, 1360)
        self.assertEqual((r[2] - r[0], r[3] - r[1]), (900, 1360))
        self.assertLessEqual(r[2], 2560)
        self.assertLessEqual(r[3], 1400)

    def test_4k_150pct_window_fits(self):
        r = self._fit(MAIN_4K150, (100, 20, 1180, 1652), 1080, 1632)
        self.assertEqual((r[2] - r[0], r[3] - r[1]), (1080, 1632))
        self.assertLessEqual(r[3], 2100)

    def test_small_laptop_never_pushes_input_box_offscreen(self):
        r = self._fit(LAPTOP_768, (10, 10, 730, 1098), 720, 1088)
        self.assertEqual((r[2] - r[0], r[3] - r[1]), (720, 712))
        self.assertLessEqual(r[2], 1366)
        self.assertLessEqual(r[3], 728)

    def test_secondary_on_right_stays_on_that_monitor(self):
        r = self._fit(RIGHT_1080P, (2700, 10, 3420, 1098), 720, 1088)
        self.assertGreaterEqual(r[0], 2560)
        self.assertLessEqual(r[2], 4480)
        self.assertLessEqual(r[3], 1040)

    def test_secondary_on_left_keeps_negative_x(self):
        r = self._fit(LEFT_NEG, (-1800, 10, -1080, 1098), 720, 1088)
        self.assertLess(r[0], 0, "副屏在左时不能被拽回主屏")
        self.assertGreaterEqual(r[0], -1920)
        self.assertLessEqual(r[2], 0)
        self.assertLessEqual(r[3], 1040)

    def test_offscreen_window_is_pulled_back(self):
        # 拔掉副屏后窗口留在已经不存在的屏幕位置
        r = self._fit(MAIN_1080P, (3000, 10, 3720, 1098), 720, 1088)
        self.assertLessEqual(r[2], 1920)
        self.assertLessEqual(r[3], 1040)
        self.assertGreaterEqual(r[0], 0)

    def test_is_fully_visible_boundary(self):
        with patch.object(screen, "monitors", return_value=[MAIN_1080P]):
            self.assertTrue(screen.is_fully_visible((0, 0, 1920, 1040)))
            self.assertFalse(screen.is_fully_visible((1800, 0, 2500, 900)))
            self.assertFalse(screen.is_fully_visible((100, 900, 800, 1500)))


class TestLayoutBounds(unittest.TestCase):
    """_layout_bounds 把 100% 标定常量换算成本机像素。"""

    @staticmethod
    def _bounds(mon, scale):
        with patch.object(m.screen, "scale_for_window", return_value=scale), \
             patch.object(m.screen, "monitor_for_rect", return_value=mon), \
             patch.object(m.screen, "primary_monitor", return_value=mon):
            return WeChatOcrTarget._layout_bounds(1, (0, 0, 100, 100))

    def test_100pct_unchanged_semantics(self):
        b = self._bounds(MAIN_1080P, 1.0)
        self.assertEqual((b["want_w"], b["min_w"], b["max_w"], b["min_h"]),
                         (720, 520, 1100, 800))

    def test_125pct_scales_all_constants(self):
        b = self._bounds(MAIN_125, 1.25)
        self.assertEqual(b["want_w"], 900)
        self.assertEqual(b["want_h"], 1360)
        self.assertEqual(b["min_w"], 650)
        self.assertEqual(b["max_w"], 1375)
        self.assertEqual(b["min_h"], 1000)

    def test_150pct_scales_all_constants(self):
        b = self._bounds(MAIN_4K150, 1.5)
        self.assertEqual((b["want_w"], b["want_h"]), (1080, 1632))
        self.assertEqual(b["max_w"], 1650)

    def test_small_screen_clamps_even_the_minimum(self):
        # 1366×768 的工作区只有 728 高：不能还要求窗口 ≥800 高，
        # 否则每次发送都会去 MoveWindow，窗口一直跳。
        b = self._bounds(LAPTOP_768, 1.0)
        self.assertEqual(b["want_h"], 712)
        self.assertEqual(b["min_h"], 712)
        self.assertLessEqual(b["min_h"], b["want_h"])

    def test_no_monitor_falls_back_to_legacy_numbers(self):
        with patch.object(m.screen, "scale_for_window", return_value=1.0), \
             patch.object(m.screen, "monitor_for_rect", return_value=None), \
             patch.object(m.screen, "primary_monitor", return_value=None):
            b = WeChatOcrTarget._layout_bounds(1, (0, 0, 100, 100))
        self.assertEqual((b["want_w"], b["want_h"]), (720, 1088))


class TestEnsureLayoutSafe(unittest.TestCase):
    """窗口本来就合适时绝不乱动；不合适时才按本机几何调整。"""

    @staticmethod
    def _run(rect, scale, mon, visible=True, maximized=False):
        tgt = WeChatOcrTarget(dry_run=True)
        style = 0x01000000 if maximized else 0
        with patch.object(WeChatOcrTarget, "_rect", return_value=rect), \
             patch.object(m.screen, "scale_for_window", return_value=scale), \
             patch.object(m.screen, "monitor_for_rect", return_value=mon), \
             patch.object(m.screen, "primary_monitor", return_value=mon), \
             patch.object(m.screen, "is_fully_visible", return_value=visible), \
             patch.object(m.user32, "GetWindowLongW", return_value=style), \
             patch.object(m.user32, "ShowWindow") as show, \
             patch.object(m.user32, "MoveWindow") as move, \
             patch.object(m.time, "sleep", lambda *_: None):
            tgt._ensure_layout_safe(1)
        return move, show

    def test_125pct_current_window_left_untouched(self):
        # 现场真值：125% 主屏，微信 (1679,14)-(2543,1348)，864×1334
        move, _ = self._run((1679, 14, 2543, 1348), 1.25, MAIN_125)
        move.assert_not_called()

    def test_normal_100pct_window_left_untouched(self):
        move, _ = self._run((100, 20, 820, 1020), 1.0, MAIN_1080P)
        move.assert_not_called()

    def test_maximized_is_restored_then_resized(self):
        move, show = self._run((0, 0, 2560, 1400), 1.25, MAIN_125,
                              maximized=True)
        show.assert_called()
        move.assert_called_once()

    def test_oversize_window_gets_resized_into_work_area(self):
        move, _ = self._run((10, 10, 1800, 1100), 1.0, MAIN_1080P)
        move.assert_called_once()
        args = move.call_args.args
        # MoveWindow(hwnd, l, t, w, h, repaint)
        self.assertEqual((args[3], args[4]), (720, 1024))
        self.assertGreaterEqual(args[1], 0)
        self.assertLessEqual(args[2] + args[4], 1040)

    def test_offscreen_window_is_brought_back(self):
        move, _ = self._run((3000, 10, 3720, 1098), 1.0, MAIN_1080P,
                            visible=False)
        move.assert_called_once()
        self.assertLessEqual(move.call_args.args[1] + move.call_args.args[3], 1920)

    def test_legacy_720x1088_is_never_used_on_125pct(self):
        move, _ = self._run((10, 10, 1800, 1100), 1.25, MAIN_125)
        self.assertNotEqual(move.call_args.args[3], 720)
        self.assertEqual(move.call_args.args[3], 900)


class TestTitleCropScale(unittest.TestCase):
    """标题裁剪线上限必须跟着窗口所在屏的缩放走。"""

    def setUp(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("需要 Pillow")
        self.Image = Image
        self.tgt = WeChatOcrTarget(dry_run=True, focus_delay=0.0)
        self.tgt._ocr = lambda arr: ([], None)

    def _x0(self, width, scale):
        img = self.Image.new("RGB", (width, 1088), (247, 247, 247))
        self.tgt._hwnd = 1
        with patch.object(m.screen, "scale_for_window", return_value=scale):
            return self.tgt._ocr_region(img, "title")[1][0]

    def test_100pct_keeps_legacy_cap(self):
        self.assertEqual(self._x0(1440, 1.0), m.TITLE_X0_MAX_PX)

    def test_125pct_cap_is_scaled(self):
        # 0.40×1440=576；316 会切掉群名开头，395 才在标题起点(405)左侧
        self.assertEqual(self._x0(1440, 1.25), int(316 * 1.25))

    def test_150pct_cap_is_scaled(self):
        self.assertEqual(self._x0(1440, 1.5), int(316 * 1.5))

    def test_narrow_window_still_uses_ratio(self):
        self.assertEqual(self._x0(720, 1.25), 288)

    def test_no_hwnd_falls_back_to_1x(self):
        self.tgt._hwnd = 0
        img = self.Image.new("RGB", (1440, 1088), (247, 247, 247))
        self.assertEqual(self.tgt._ocr_region(img, "title")[1][0], 316)


class TestDingTalkCoordinateNormalize(unittest.TestCase):
    """UIA 矩形与 PrintWindow 位图坐标系不一致时要归一化。"""

    def _source(self):
        from bridge.sources.dingtalk_ocr import DingTalkOCRSource
        return DingTalkOCRSource.__new__(DingTalkOCRSource)

    def test_ratio_one_when_both_physical(self):
        from bridge.sources.dingtalk_ocr import DingTalkOCRSource
        wnd = type("W", (), {"BoundingRectangle": type(
            "R", (), {"left": 0, "top": 0, "right": 900, "bottom": 1200})()})()
        frame = type("F", (), {"width": 900, "height": 1200})()
        self.assertEqual(DingTalkOCRSource._frame_coord_scale(wnd, frame), 1.0)

    def test_ratio_125_when_uia_is_logical(self):
        from bridge.sources.dingtalk_ocr import DingTalkOCRSource
        wnd = type("W", (), {"BoundingRectangle": type(
            "R", (), {"left": 0, "top": 0, "right": 720, "bottom": 960})()})()
        frame = type("F", (), {"width": 900, "height": 1200})()
        self.assertAlmostEqual(
            DingTalkOCRSource._frame_coord_scale(wnd, frame), 1.25)

    def test_absurd_ratio_is_ignored(self):
        from bridge.sources.dingtalk_ocr import DingTalkOCRSource
        wnd = type("W", (), {"BoundingRectangle": type(
            "R", (), {"left": 0, "top": 0, "right": 10, "bottom": 10})()})()
        frame = type("F", (), {"width": 900, "height": 1200})()
        self.assertEqual(DingTalkOCRSource._frame_coord_scale(wnd, frame), 1.0)

    def test_read_title_crops_by_frame_width_not_uia_width(self):
        """UIA 宽 720、位图宽 900 时，裁剪右边界必须是 900。"""
        from bridge.sources.dingtalk_ocr import DingTalkOCRSource
        src = DingTalkOCRSource.__new__(DingTalkOCRSource)
        wnd = type("W", (), {"BoundingRectangle": type(
            "R", (), {"left": 0, "top": 0, "right": 720, "bottom": 960})()})()
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("需要 Pillow")
        frame = Image.new("RGB", (900, 1200), (255, 255, 255))
        src._last_frame = frame
        src._get_ocr = lambda: (lambda arr: ())
        texts = src._read_title(wnd, (300, 200, 900, 1000), 1.25)
        # 不抛异常且返回列表即可；真正要防的是 crop 右边界取 720 把标题切掉
        self.assertIsInstance(texts, list)
        src_read = read("bridge/sources/dingtalk_ocr.py")
        tree = ast.parse(src_read)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_read_title")
        fn_src = ast.get_source_segment(src_read, fn) or ""
        self.assertIn("self._last_frame.width", fn_src)
        self.assertNotIn("wr.right - wr.left", fn_src)


class TestEveryEntryDeclaresDpi(unittest.TestCase):
    """所有入口都得经过 DPI 声明，不能只修 worker 一个。"""

    def test_bridge_package_init_enables_dpi(self):
        tree = ast.parse(read("bridge/__init__.py"))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        self.assertTrue(any(isinstance(c.func, ast.Name)
                            and c.func.id == "_enable_dpi" for c in calls),
                        "bridge/__init__.py 必须在包初始化时声明 DPI，"
                        "这样 tools/*.py 子进程也自动覆盖")

    def test_run_py_still_calls_enable_early(self):
        src = read("run.py")
        i_dpi = src.index("_enable_dpi()")
        i_cfg = src.index("from bridge.config import load_config")
        self.assertLess(i_dpi, i_cfg, "DPI 必须早于任何 bridge 业务模块")

    def test_screen_command_wired(self):
        src = read("run.py")
        self.assertIn("--screen", src)
        self.assertIn("screen.describe()", src)

    def test_check_reports_alignment(self):
        src = read("run.py")
        self.assertIn("screen.check_alignment(hwnd)", src)
        self.assertIn("屏幕坐标不对齐", src)

    def test_grab_still_all_screens(self):
        # 只看调用点：文档注释里会留着 all_screens=False 讲历史，那不是代码
        tree = ast.parse(read("bridge/targets/wechat_ocr.py"))
        flags = [bool(kw.value.value)
                 for node in ast.walk(tree)
                 if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)
                 and node.func.attr == "grab"
                 for kw in node.keywords if kw.arg == "all_screens"]
        self.assertEqual(flags, [True, True])

    def test_gui_geometry_is_clamped(self):
        src = read("app/gui.py")
        self.assertIn("winfo_screenwidth()", src)
        self.assertIn("min(1520", src)
        self.assertNotIn('self.geometry("1520x780")', src)

    def test_installer_center_is_clamped(self):
        src = read("installer/setup_gui.py")
        self.assertIn("w = min(680, sw - 40)", src)


if __name__ == "__main__":
    unittest.main()
