# -*- coding: utf-8 -*-
"""``WeChatOcrTarget._find_window``：最小化 / 托盘态的微信必须找得到。

真机 2026-10-20 用户报「21:51:01 投递失败 → 申通五星业务员培训群 |
未找到微信主窗口，请确认微信已登录且在桌面上」。

现场去量，微信**根本没退出**：

    (67596, '微信', 'Qt51514QWindowIcon', visible=True, iconic=True,
     -32000, -32000, -31840, -31972)

旧实现有**两条**路径把它丢掉，任一成立就报「未找到微信主窗口」：

1. 枚举回调里 ``if not IsWindowVisible(h): return True`` —— 托盘态
   （``IsWindowVisible=0``、句柄和标题都在）被整个跳过；
2. 尺寸闸 ``(r[2]-r[0]) > 400`` —— 最小化窗口的矩形是 ``(-32000,...)``
   这种假坐标，宽只有 **160**，被当成「登录小窗外挂」判掉。

所以断言必须**让旧算法失败**（项目铁律），不能只演「新算法能找到」。
测试把 Win32 调用替换在**实例/模块函数**一层，不 mock ``_user32`` DLL
对象（那会 0xC0000005，2026-10-18 踩过）。
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.targets import wechat_ocr  # noqa: E402
from bridge.targets.wechat_ocr import WeChatOcrTarget  # noqa: E402

MINIMIZED_RECT = (-32000, -32000, -31840, -31972)   # 真机量到的值，宽 160
NORMAL_RECT = (100, 100, 820, 1188)                 # 720×1088，已标定尺寸
LOGIN_RECT = (600, 300, 776, 499)                   # 登录小窗 176×199


def make_target() -> WeChatOcrTarget:
    return WeChatOcrTarget(dry_run=True, focus_delay=0.0)


def _key(h):
    """``wt.HWND(67596)`` 传进替身时是个 c_void_p，取回整数再查表。"""
    return int(getattr(h, "value", h) or 0)


class FakeWin32:
    """按句柄答 Win32 查询的替身；只实现 _find_window 用到的几个。

    ``wins``: hwnd -> dict(title, cls, visible, iconic, rect)
    """

    def __init__(self, wins):
        self.wins = {int(k): v for k, v in wins.items()}
        self.enumerated = 0

    # 枚举：回调签名保持 (hwnd, lparam) -> bool
    def EnumWindows(self, cb, _lparam):
        self.enumerated += 1
        for h in list(self.wins):
            if not cb(h, 0):
                break
        return 1

    def GetWindowTextLengthW(self, h):
        return len(self.wins.get(_key(h), {}).get("title", ""))

    def GetWindowTextW(self, h, buf, _n):
        buf.value = self.wins.get(_key(h), {}).get("title", "")
        return len(buf.value)

    def IsWindowVisible(self, h):
        return bool(self.wins.get(_key(h), {}).get("visible", False))

    def IsIconic(self, h):
        return bool(self.wins.get(_key(h), {}).get("iconic", False))

    def IsWindow(self, h):
        return _key(h) in self.wins

    def GetWindowRect(self, h, ref):
        r = self.wins.get(_key(h), {}).get("rect")
        if r is None:
            return 0
        v = ref._obj
        v.left, v.top, v.right, v.bottom = r
        return 1

    def GetClassNameW(self, h, buf, _n):
        buf.value = self.wins.get(_key(h), {}).get("cls", "")
        return len(buf.value)


def run_find(wins, cached=None):
    """用假 Win32 跑一遍 ``_find_window``，返回句柄。"""
    tgt = make_target()
    tgt._hwnd = cached
    fake = FakeWin32(wins)
    with mock.patch.object(wechat_ocr, "user32", fake):
        return tgt._find_window()


class TestMinimizedWindowIsFound(unittest.TestCase):
    """最小化 = 假矩形 = 旧实现的尺寸闸会判掉它。"""

    def test_minimized_wechat_is_found(self):
        """⚠ 旧实现返回 0 并报「未找到微信主窗口」，这条就是要它红。

        真机事故原样复现：句柄 67596，标题「微信」，最小化，
        矩形 (-32000,-32000,-31840,-31972)。
        """
        wins = {67596: {"title": "微信", "cls": "Qt51514QWindowIcon",
                        "visible": True, "iconic": True,
                        "rect": MINIMIZED_RECT}}
        self.assertEqual(run_find(wins), 67596)

    def test_visible_window_still_preferred_by_first_hit(self):
        """正常窗口照旧能找到（尺寸闸不能因为修 bug 被整段删掉）。"""
        wins = {1: {"title": "微信", "cls": "Qt51514QWindowIcon",
                    "visible": True, "iconic": False, "rect": NORMAL_RECT}}
        self.assertEqual(run_find(wins), 1)

    def test_login_small_window_still_excluded(self):
        """登录小窗（176×199，可见、未最小化）**仍然要排除**，别把闸拆了。"""
        wins = {2: {"title": "微信", "cls": "Qt51514QWindowIcon",
                    "visible": True, "iconic": False, "rect": LOGIN_RECT}}
        self.assertEqual(run_find(wins), 0)


class TestTrayWindowIsFound(unittest.TestCase):
    """托盘态（IsWindowVisible=0、非最小化）= 旧实现的可见性闸跳过它。"""

    def test_hidden_wechat_is_found(self):
        """⚠ 旧实现里 ``if not IsWindowVisible: return True`` 直接跳过。

        收进托盘的微信句柄和标题都在，``_activate()`` 一句 SW_SHOW 就能
        唤回 —— 在 _find_window 里丢掉它，等于让 _activate 永远收不到活干。
        """
        wins = {3: {"title": "微信", "cls": "Qt51514QWindowIcon",
                    "visible": False, "iconic": False, "rect": NORMAL_RECT}}
        self.assertEqual(run_find(wins), 3)

    def test_other_titled_hidden_window_ignored(self):
        """只认主窗口标题，隐藏的别的东西别乱认。"""
        wins = {4: {"title": "微信登录", "cls": "Chrome_WidgetWin_1",
                    "visible": False, "iconic": False, "rect": NORMAL_RECT}}
        self.assertEqual(run_find(wins), 0)


class TestCachedHandleShortCircuit(unittest.TestCase):
    """缓存句柄短路：最小化的缓存句柄不能因为宽度 160 被判「无效重枚举」。"""

    def test_cached_minimized_handle_is_reused(self):
        tgt = make_target()
        tgt._hwnd = 67596
        fake = FakeWin32({67596: {"title": "微信", "visible": True,
                                  "iconic": True, "rect": MINIMIZED_RECT}})
        with mock.patch.object(wechat_ocr, "user32", fake):
            self.assertEqual(tgt._find_window(), 67596)
        self.assertEqual(fake.enumerated, 0, "命中缓存就不该再枚举一遍")

    def test_cached_dead_handle_falls_back_to_enum(self):
        """句柄已失效（微信重启过）→ 重新枚举，不能抱着死句柄不放。"""
        tgt = make_target()
        tgt._hwnd = 999
        fake = FakeWin32({5: {"title": "微信", "visible": True,
                              "iconic": False, "rect": NORMAL_RECT}})
        with mock.patch.object(wechat_ocr, "user32", fake):
            self.assertEqual(tgt._find_window(), 5)


class TestFailureDetailStillHonest(unittest.TestCase):
    """真找不到时，那句「请确认微信已登录」必须还在。

    合并到 _activate 之后，唯一能报出这句话的机会就是 _find_window 返回 0。
    如果哪天有人给 _find_window 加了个「找不到就返回缓存」的兜底，
    用户会收到下游一句「无法把微信窗口切到前台」，把排障带偏。
    """

    def test_empty_desktop_returns_zero(self):
        wins = {6: {"title": "文档.txt - 记事本", "cls": "Notepad",
                    "visible": True, "iconic": False, "rect": NORMAL_RECT}}
        self.assertEqual(run_find(wins), 0)


class TestHealthCheckWakesWindow(unittest.TestCase):
    """``health_check`` 找到句柄后必须自己唤回，不能直接去抓图。

    这是 ``_find_window`` 放宽的**必然连带后果**：它现在连最小化/托盘态的
    窗口一起返回，而探活真要抓画面（PrintWindow 对不可见窗口返回黑图）。
    不在这里唤回，自检会在微信好好待在托盘里的时候报一句
    「抓不到微信窗口画面」，把用户推去手动点任务栏。
    """

    def _run(self, activates):
        tgt = make_target()
        tgt._hwnd = 67596
        fake = FakeWin32({67596: {"title": "微信", "visible": False,
                                  "iconic": False, "rect": NORMAL_RECT}})

        def boom(_h):
            raise RuntimeError("不该走到抓图：窗口还没唤回")

        tgt._activate = lambda h: activates.append(h) or True   # type: ignore
        tgt._grab = boom                                       # type: ignore
        with mock.patch.object(wechat_ocr, "user32", fake):
            return tgt.health_check()

    def test_activate_is_called_before_grab(self):
        called = []
        ok, detail = self._run(called)
        # 抓到爆（说明确实往下走了），但 _activate 已经先被调到
        self.assertFalse(ok)
        self.assertEqual(called, [67596], "探活前必须先唤回窗口")
        self.assertIn("抓取微信窗口失败", detail)

    def test_no_window_reports_login_not_tray(self):
        """真找不到窗口时，别再叫用户「点任务栏图标」—— 那是句错话。"""
        tgt = make_target()
        tgt._hwnd = 0
        with mock.patch.object(wechat_ocr, "user32", FakeWin32({})):
            ok, detail = tgt.health_check()
        self.assertFalse(ok)
        self.assertIn("未找到微信主窗口", detail)
        self.assertNotIn("收进托盘", detail)


if __name__ == "__main__":
    unittest.main()
