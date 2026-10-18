"""微信 4.x 投递端的护栏单元测试（离线，不碰真实微信）。

这里锁住的是 10-16 那次投递失败事故修出来的几条规则 ——
它们都是「实测踩出来的」，一旦被改回去就会静默复发：

  1. 搜索兜底路径**绝不能按 Esc** —— Esc 会把微信主窗口整个收进托盘；
  2. 控件矩形为 0×0 时不能点击（后台/未渲染完时 uia 的 Click 会点错地方）；
  3. 会话列表找不到目标时先归位到**顶部**再向下找（只往下滚会永久失败）；
  4. 微信被最小化 / 收进托盘时必须先唤回或还原，不能照着陈旧坐标操作。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.targets.wechat4 import (   # noqa: E402
    CLICK_RETRIES, MIN_WINDOW_SIDE, WeChat4Target,
)


class FakeRect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom

    def height(self):
        return self.bottom - self.top

    def __repr__(self):
        return f"({self.left},{self.top},{self.right},{self.bottom})"


class FakeControl:
    def __init__(self, name="", rect=None, cls="mmui::ChatSessionCell",
                 ctype="ListItemControl", exists=True, no_rect=False):
        self.Name = name
        self.ClassName = cls
        self.ControlTypeName = ctype
        self._rect = rect
        self._exists = exists
        self._no_rect = no_rect
        self.clicks = 0

    def Exists(self, timeout=0):
        return self._exists

    @property
    def BoundingRectangle(self):
        if self._no_rect:
            raise LookupError("Find Control Timeout")
        return self._rect

    def Click(self, **kwargs):
        self.clicks += 1

    def GetChildren(self):
        return getattr(self, "children", [])


class FakeTable(FakeControl):
    def __init__(self, rect, cells, exists=True, no_rect=False):
        super().__init__(rect=rect, exists=exists, no_rect=no_rect)
        self.children = cells
        self.wheel_up = 0
        self.wheel_down = 0

    def WheelUp(self, wheelTimes=1):
        self.wheel_up += wheelTimes

    def WheelDown(self, wheelTimes=1):
        self.wheel_down += wheelTimes


class FakeWnd:
    """按 ClassName 分发 ListControl / EditControl 的假窗口。"""

    def __init__(self, table=None, search_box=None, message_list=None, rect=None):
        self.table = table
        self.search_box = search_box
        self.message_list = message_list
        self.rect = rect or FakeRect(12, 2, 698, 1320)

    @property
    def BoundingRectangle(self):
        return self.rect

    def ListControl(self, ClassName="", searchDepth=0):
        if ClassName == WeChat4Target.MESSAGE_LIST:
            return self.message_list or FakeControl(exists=False)
        return self.table or FakeControl(exists=False)

    def EditControl(self, ClassName="", searchDepth=0, **kw):
        if ClassName == WeChat4Target.SEARCH_BOX:
            return self.search_box or FakeControl(exists=False)
        return self.search_box or FakeControl(exists=False)

    def ButtonControl(self, ClassName="", Name="", searchDepth=0):
        return FakeControl(exists=False)


class FakeUia:
    """记录所有 SendKeys 的假 UIA，用来断言「没有发过 Esc」。"""

    def __init__(self):
        self.sent: list[str] = []
        self.clicks: list[tuple[int, int]] = []
        self.clipboard = ""

    def SendKeys(self, keys, *a, **kw):
        self.sent.append(keys)

    def SetClipboardText(self, text):
        self.clipboard = text

    def Click(self, x, y):
        self.clicks.append((x, y))

    def SetForegroundWindow(self, hwnd):
        return True

    def WindowControl(self, **kw):
        return FakeControl(exists=False)


def make_target(**kw) -> WeChat4Target:
    """构造一个不碰真实 UIA 的实例（uia 用假对象顶替）。

    Win32 那一层也要一并顶掉 —— 测试机上微信常常是开着的，真去 FindWindow
    会摸到真窗口，于是用例会去 ShowWindow 用户的微信（隐蔽副作用）。
    """
    tgt = WeChat4Target(wxauto_path=".", dry_run=True, **kw)
    fake = FakeUia()
    tgt._load_uia = lambda: fake          # type: ignore[assignment]
    tgt._fake_uia = fake                  # type: ignore[attr-defined]
    tgt._hwnd_by_class = lambda: 0        # type: ignore[assignment]
    tgt._show_hidden = lambda h: None     # type: ignore[assignment]
    return tgt


class TestRectGuard(unittest.TestCase):
    """控件矩形护栏：0×0 或取不到都不能点。"""

    def test_rect_of_rejects_degenerate(self):
        tgt = make_target()
        self.assertIsNone(tgt._rect_of(FakeControl(rect=FakeRect(10, 10, 10, 10))))
        self.assertIsNone(tgt._rect_of(FakeControl(rect=FakeRect(10, 10, 11, 40))))
        self.assertIsNone(tgt._rect_of(FakeControl(no_rect=True)))

    def test_rect_of_accepts_valid(self):
        tgt = make_target()
        r = FakeRect(0, 0, 100, 40)
        self.assertIs(tgt._rect_of(FakeControl(rect=r)), r)

    def test_click_safely_skips_when_rect_invalid(self):
        tgt = make_target()
        for ctrl in (FakeControl(rect=FakeRect(5, 5, 5, 5)),
                     FakeControl(no_rect=True)):
            self.assertFalse(tgt._click_safely(ctrl, "测试控件"))
            self.assertEqual(ctrl.clicks, 0, "矩形无效时绝不能点")

    def test_click_safely_clicks_when_valid(self):
        tgt = make_target()
        ctrl = FakeControl(rect=FakeRect(0, 0, 100, 40))
        self.assertTrue(tgt._click_safely(ctrl, "测试控件"))
        self.assertEqual(ctrl.clicks, 1)


class TestSessionScan(unittest.TestCase):
    """会话列表扫描：安全校验 + 滚动方向。"""

    def _wnd(self, cells, table_rect=FakeRect(0, 0, 300, 600)):
        return FakeWnd(table=FakeTable(table_rect, cells))

    def test_finds_visible_cell(self):
        tgt = make_target()
        cell = FakeControl("目标群", FakeRect(0, 100, 300, 160))
        self.assertIs(tgt._find_session(self._wnd([cell]), "目标群"), cell)

    def test_ignores_cell_outside_table(self):
        """单元格在可视区外时必须拒绝 —— 否则点击会落到别的窗口上。"""
        tgt = make_target()
        cell = FakeControl("目标群", FakeRect(0, 900, 300, 960))
        self.assertIsNone(tgt._find_session(self._wnd([cell]), "目标群", scroll_tries=0))

    def test_ignores_degenerate_cell(self):
        tgt = make_target()
        cell = FakeControl("目标群", FakeRect(0, 100, 300, 101))
        self.assertIsNone(tgt._find_session(self._wnd([cell]), "目标群", scroll_tries=0))

    def test_resets_to_top_before_scrolling_down(self):
        """首次没找到要**先归位到顶部**；只往下滚会让目标永远找不到。"""
        tgt = make_target()
        table = FakeTable(FakeRect(0, 0, 300, 600), [])
        with mock.patch("bridge.targets.wechat4.time.sleep"):
            self.assertIsNone(tgt._find_session(FakeWnd(table=table), "目标群",
                                                scroll_tries=2))
        self.assertEqual(table.wheel_up, WeChat4Target.SCROLL_TO_TOP_TURNS)
        self.assertEqual(table.wheel_down, 3, "归位之后才逐屏往下找")

    def test_no_wheel_when_found_first(self):
        tgt = make_target()
        cell = FakeControl("目标群", FakeRect(0, 100, 300, 160))
        table = FakeTable(FakeRect(0, 0, 300, 600), [cell])
        self.assertIs(tgt._find_session(FakeWnd(table=table), "目标群"), cell)
        self.assertEqual((table.wheel_up, table.wheel_down), (0, 0))

    def test_rejects_table_without_rect(self):
        tgt = make_target()
        table = FakeTable(None, [FakeControl("目标群", FakeRect(0, 0, 300, 60))],
                          no_rect=True)
        self.assertIsNone(tgt._find_session(FakeWnd(table=table), "目标群"))


class TestSearchResultsPick(unittest.TestCase):
    """搜索面板是分组的，必须挑中「聊天条目」那一行，否则点了不切会话。"""

    def _wnd(self, cells):
        return FakeWnd(table=FakeTable(FakeRect(0, 0, 400, 300), cells))

    def test_prefers_exact_name(self):
        tgt = make_target()
        loose = FakeControl("目标群（2）", FakeRect(0, 105, 400, 170),
                            cls="mmui::SearchContentCellView")
        exact = FakeControl("目标群", FakeRect(0, 70, 400, 100),
                            cls="mmui::XTableCell")
        self.assertIs(tgt._search_results_cell(self._wnd([loose, exact]), "目标群"), exact)

    def test_prefers_chat_row_over_text_row(self):
        tgt = make_target()
        text_row = FakeControl("目标群", FakeRect(0, 105, 400, 139),
                              cls="mmui::XTableCell")
        chat_row = FakeControl("目标群", FakeRect(0, 139, 400, 203),
                               cls="mmui::SearchContentCellView")
        self.assertIs(tgt._search_results_cell(self._wnd([text_row, chat_row]), "目标群"),
                      chat_row)

    def test_returns_none_when_absent(self):
        tgt = make_target()
        other = FakeControl("别的群", FakeRect(0, 105, 400, 169))
        self.assertIsNone(tgt._search_results_cell(self._wnd([other]), "目标群"))


class TestSearchPanel(unittest.TestCase):
    """搜索面板开着时会话列表会被顶掉，必须先识别出来。"""

    def _wnd(self, names):
        cells = [FakeControl(n, FakeRect(0, 70 + i * 64, 400, 134 + i * 64))
                 for i, n in enumerate(names)]
        return FakeWnd(table=FakeTable(FakeRect(0, 0, 400, 300), cells))

    def test_detects_open_panel(self):
        tgt = make_target()
        self.assertTrue(tgt._search_panel_open(self._wnd(["搜索网络结果"])))
        self.assertTrue(tgt._search_panel_open(self._wnd(["最常使用", "目标群"])))

    def test_normal_list_is_not_panel(self):
        tgt = make_target()
        self.assertFalse(tgt._search_panel_open(self._wnd(["目标群", "别的群"])))

    def test_dismiss_clicks_chat_area(self):
        tgt = make_target()
        lst = FakeControl(rect=FakeRect(320, 100, 690, 1200), cls="mmui::RecyclerListView")
        wnd = FakeWnd(table=FakeTable(FakeRect(0, 0, 400, 300), []), message_list=lst)
        with mock.patch("bridge.targets.wechat4.time.sleep"):
            tgt._dismiss_search_panel(wnd)
        self.assertEqual(tgt._fake_uia.clicks, [(505, 650)])


class TestOpenChat(unittest.TestCase):
    """切会话主流程。"""

    def _tgt(self, cells, title_seq=None, panel_cells=None):
        tgt = make_target()
        table = FakeTable(FakeRect(0, 0, 300, 600), cells)
        wnd = FakeWnd(table=table)
        tgt._window = lambda: wnd                       # type: ignore[assignment]
        tgt._activate = lambda w: None                  # type: ignore[assignment]
        tgt.ensure_visible = lambda: (True, "窗口正常")  # type: ignore[assignment]
        tgt._search_panel_open = lambda w: False        # type: ignore[assignment]
        titles = list(title_seq or [])
        tgt._chat_title = lambda w: titles.pop(0) if titles else ""   # type: ignore
        return tgt, table

    def test_returns_false_when_window_unavailable(self):
        tgt, _ = self._tgt([])
        tgt.ensure_visible = lambda: (False, "微信主窗口收进托盘了")   # type: ignore
        self.assertFalse(tgt.open_chat("目标群"))

    def test_switches_on_first_click(self):
        cell = FakeControl("目标群", FakeRect(0, 100, 300, 160))
        tgt, _ = self._tgt([cell], title_seq=["目标群"])
        with mock.patch("bridge.targets.wechat4.time.sleep"):
            self.assertTrue(tgt.open_chat("目标群"))
        self.assertEqual(cell.clicks, 1)
        self.assertEqual(tgt._current, "目标群")

    def test_retries_click_when_title_not_switched(self):
        """首次点击可能被系统当成「激活点击」吞掉 —— 必须重试而不是直接失败。"""
        cell = FakeControl("目标群", FakeRect(0, 100, 300, 160))
        tgt, _ = self._tgt([cell])
        # 第一次等标题等不到，第二次点击后才切过来
        answers = [False, True]
        tgt._wait_title = lambda w, t, timeout=0: answers.pop(0)   # type: ignore
        with mock.patch("bridge.targets.wechat4.time.sleep"):
            self.assertTrue(tgt.open_chat("目标群"))
        self.assertEqual(cell.clicks, 2)

    def test_gives_up_after_retries(self):
        cell = FakeControl("目标群", FakeRect(0, 100, 300, 160))
        tgt, _ = self._tgt([cell])
        tgt._wait_title = lambda w, t, timeout=0: False            # type: ignore
        with mock.patch("bridge.targets.wechat4.time.sleep"):
            self.assertFalse(tgt.open_chat("目标群"))
        self.assertEqual(cell.clicks, CLICK_RETRIES)

    def test_falls_back_to_search_when_not_in_list(self):
        tgt, _ = self._tgt([])
        called = []
        tgt._open_chat_by_search = lambda w, t: (called.append(t), True)[1]  # type: ignore
        self.assertTrue(tgt.open_chat("目标群"))
        self.assertEqual(called, ["目标群"])

    def test_dismisses_search_panel_before_scanning(self):
        cell = FakeControl("目标群", FakeRect(0, 100, 300, 160))
        tgt, _ = self._tgt([cell], title_seq=["目标群"])
        seen = []
        tgt._search_panel_open = lambda w: True                      # type: ignore
        tgt._dismiss_search_panel = lambda w: (seen.append(1), True)[1]   # type: ignore
        with mock.patch("bridge.targets.wechat4.time.sleep"):
            self.assertTrue(tgt.open_chat("目标群"))
        self.assertEqual(seen, [1])


class TestSearchPathNeverEscapes(unittest.TestCase):
    """核心回归防线：搜索兜底路径绝不能按 Esc（会把微信收进托盘）。"""

    def _tgt(self, cells):
        tgt = make_target()
        box = FakeControl("搜索", FakeRect(0, 20, 300, 60), cls=WeChat4Target.SEARCH_BOX)
        wnd = FakeWnd(table=FakeTable(FakeRect(0, 0, 400, 300), cells), search_box=box)
        tgt._window = lambda: wnd                       # type: ignore[assignment]
        tgt._wait_title = lambda w, t, timeout=0: True  # type: ignore[assignment]
        return tgt, wnd

    def test_success_path_sends_no_esc(self):
        cell = FakeControl("目标群", FakeRect(0, 105, 400, 169),
                           cls="mmui::SearchContentCellView")
        tgt, wnd = self._tgt([cell])
        with mock.patch("bridge.targets.wechat4.time.sleep"):
            self.assertTrue(tgt._open_chat_by_search(wnd, "目标群"))
        self.assertNotIn("{Esc}", tgt._fake_uia.sent)
        self.assertEqual(cell.clicks, 1)
        self.assertEqual(tgt._current, "目标群")

    def test_failure_path_also_sends_no_esc(self):
        """搜索结果为空时同样不能按 Esc —— 上一版就是在这里把微信关进托盘的。"""
        tgt, wnd = self._tgt([])
        with mock.patch("bridge.targets.wechat4.time.sleep"):
            self.assertFalse(tgt._open_chat_by_search(wnd, "目标群"))
        self.assertNotIn("{Esc}", tgt._fake_uia.sent)
        self.assertTrue(any("Ctrl" in k or "v" in k for k in tgt._fake_uia.sent),
                        "应该往搜索框里粘过搜索词")

    def test_types_target_into_search_box(self):
        cell = FakeControl("目标群", FakeRect(0, 105, 400, 169),
                           cls="mmui::SearchContentCellView")
        tgt, wnd = self._tgt([cell])
        with mock.patch("bridge.targets.wechat4.time.sleep"):
            tgt._open_chat_by_search(wnd, "目标群")
        self.assertEqual(tgt._fake_uia.clipboard, "目标群")


class TestVisibilityRecovery(unittest.TestCase):
    """微信被收进托盘 / 被最小化时的自愈。"""

    def test_restores_from_tray_with_hotkey(self):
        tgt = make_target()
        state = {"present": False}

        def send_keys(keys, *a, **kw):
            tgt._fake_uia.sent.append(keys)
            state["present"] = True          # 发过热键之后窗口回来了

        tgt._fake_uia.SendKeys = send_keys
        tgt._wnd_present = lambda: state["present"]      # type: ignore[assignment]
        tgt._window = lambda: FakeWnd()                  # type: ignore[assignment]
        tgt._is_minimized = lambda w: False              # type: ignore[assignment]
        tgt._wait_rendered = lambda timeout=0: True      # type: ignore[assignment]
        with mock.patch("bridge.targets.wechat4.time.sleep"):
            ok, why = tgt.ensure_visible()
        self.assertTrue(ok, why)
        self.assertEqual(tgt._fake_uia.sent, [WeChat4Target.DEFAULT_SHOW_HOTKEY])

    def test_restores_hidden_window_by_hwnd(self):
        """★ 首选项：按窗口类名拿句柄直接 ShowWindow，不发热键。

        实测（2026-10-17）：把微信主窗口隐藏后，发 {Ctrl}{Alt}w **唤不回来**
        （SendKeys 依赖输入队列/前台，热键又是开关，发错一次反而把窗口收起来）。
        而 FindWindow 对隐藏窗口照样有效，ShowWindow 一次就亮出来。
        """
        tgt = make_target()
        state = {"present": False}
        shown = []

        tgt._wnd_present = lambda: state["present"]        # type: ignore[assignment]
        tgt._hwnd_by_class = lambda: 0x1234                # type: ignore[assignment]

        def show(h):
            shown.append(h)
            state["present"] = True                        # ShowWindow 之后就看得见了

        tgt._show_hidden = show                            # type: ignore[assignment]
        tgt._window = lambda: FakeWnd()                    # type: ignore[assignment]
        tgt._is_minimized = lambda w: False                # type: ignore[assignment]
        tgt._wait_rendered = lambda timeout=0: True        # type: ignore[assignment]
        with mock.patch("bridge.targets.wechat4.time.sleep"):
            ok, why = tgt.ensure_visible()
        self.assertTrue(ok, why)
        self.assertEqual(shown, [0x1234], "隐藏窗口必须按句柄唤出")
        self.assertEqual(tgt._fake_uia.sent, [], "句柄能唤回时不应再发热键（它是开关）")

    def test_falls_back_to_hotkey_when_show_useless(self):
        """按句柄 ShowWindow 没用（窗口被销毁/换了类名）时才发热键。"""
        tgt = make_target()
        state = {"present": False}

        tgt._hwnd_by_class = lambda: 0x1234                # type: ignore[assignment]
        tgt._show_hidden = lambda h: None                  # type: ignore[assignment]
        tgt._wnd_present = lambda: state["present"]        # type: ignore[assignment]

        def send_keys(keys, *a, **kw):
            tgt._fake_uia.sent.append(keys)
            state["present"] = True

        tgt._fake_uia.SendKeys = send_keys
        tgt._window = lambda: FakeWnd()                    # type: ignore[assignment]
        tgt._is_minimized = lambda w: False                # type: ignore[assignment]
        tgt._wait_rendered = lambda timeout=0: True        # type: ignore[assignment]
        with mock.patch("bridge.targets.wechat4.time.sleep"):
            ok, why = tgt.ensure_visible()
        self.assertTrue(ok, why)
        self.assertEqual(tgt._fake_uia.sent, [WeChat4Target.DEFAULT_SHOW_HOTKEY])

    def test_reports_failure_when_hotkey_useless(self):
        tgt = make_target()
        tgt._wnd_present = lambda: False                 # type: ignore[assignment]
        with mock.patch("bridge.targets.wechat4.time.sleep"), \
                mock.patch("bridge.targets.wechat4.time.time",
                           side_effect=[0, 0, 0, 0, 99]):
            ok, why = tgt.ensure_visible()
        self.assertFalse(ok)
        self.assertIn("热键", why)

    def test_restores_minimized_window(self):
        tgt = make_target()
        calls = []
        state = {"min": True}

        def restore(w):
            calls.append(1)
            state["min"] = False        # 还原之后就正常了

        tgt._wnd_present = lambda: True                  # type: ignore[assignment]
        tgt._window = lambda: FakeWnd()                  # type: ignore[assignment]
        tgt._is_minimized = lambda w: state["min"]       # type: ignore[assignment]
        tgt._restore_window = restore                    # type: ignore[assignment]
        tgt._wait_rendered = lambda timeout=0: True      # type: ignore[assignment]
        with mock.patch("bridge.targets.wechat4.time.sleep"):
            ok, why = tgt.ensure_visible()
        self.assertTrue(ok, why)
        self.assertEqual(calls, [1], "最小化时必须还原窗口")

    def test_custom_hotkey_from_config(self):
        tgt = WeChat4Target(wxauto_path=".", show_hotkey="{Ctrl}{Alt}q")
        self.assertEqual(tgt.show_hotkey, "{Ctrl}{Alt}q")

    def test_default_hotkey(self):
        self.assertEqual(WeChat4Target(wxauto_path=".").show_hotkey, "{Ctrl}{Alt}w")


class TestSelfDrawnShell(unittest.TestCase):
    """自绘空壳 vs 收进托盘 —— 必须分开判，否则排障方向全错。

    实测（2026-10-18 现场）：微信某次启动后主窗口 ``Qt51514QWindowIcon`` 下只有
    ``Weixin`` 与 ``MMUIRenderSubWindowHW`` 两个 Pane，整棵桌面树 ``mmui::`` 命中 0。
    窗口明明可见、程序却报「多半收进了托盘」，用户按热键、点托盘折腾 20 秒才看到
    真正原因（要完全退出微信重开）。
    """

    def test_visible_window_without_mmui_is_self_drawn(self):
        tgt = make_target()
        tgt._hwnd_by_class = lambda: 0x1234               # type: ignore[assignment]
        tgt._qt_has_mmui = lambda h: False                # type: ignore[assignment]
        tgt._is_window_visible = lambda h: True           # type: ignore[assignment]
        self.assertEqual(tgt._shell_handle(), 0x1234)

    def test_hidden_window_is_not_self_drawn(self):
        """收进托盘的窗口**句柄和标题都在**，不能当成自绘空壳。"""
        tgt = make_target()
        tgt._hwnd_by_class = lambda: 0x1234               # type: ignore[assignment]
        tgt._qt_has_mmui = lambda h: False                # type: ignore[assignment]
        tgt._is_window_visible = lambda h: False          # type: ignore[assignment]
        self.assertEqual(tgt._shell_handle(), 0)

    def test_visible_window_with_mmui_is_healthy(self):
        tgt = make_target()
        tgt._hwnd_by_class = lambda: 0x1234               # type: ignore[assignment]
        tgt._qt_has_mmui = lambda h: True                 # type: ignore[assignment]
        tgt._is_window_visible = lambda h: True           # type: ignore[assignment]
        self.assertEqual(tgt._shell_handle(), 0)

    def test_ensure_visible_fails_fast_without_hotkey(self):
        """自绘空壳下**不该再去按热键**（热键是开关，可能把窗口收起来）。"""
        tgt = make_target()
        tgt._wnd_present = lambda: False                  # type: ignore[assignment]
        tgt._qt_has_mmui = lambda h: False                # type: ignore[assignment]
        tgt._is_window_visible = lambda h: True           # type: ignore[assignment]
        tgt._hwnd_by_class = lambda: 0x1234               # type: ignore[assignment]
        with mock.patch("bridge.targets.wechat4.time.sleep"):
            ok, why = tgt.ensure_visible()
        self.assertFalse(ok)
        self.assertIn("自绘空壳", why)
        self.assertEqual(tgt._fake_uia.sent, [], "自绘空壳不该发热键")
        self.assertNotIn("唤回", why, "自绘空壳不该让用户去按热键唤窗口")
        self.assertIn("完全退出微信", why)

    def test_health_check_reports_hidden_not_self_drawn(self):
        """托盘里的时候要提示「点开窗口」，不是「退出微信重开」。"""
        tgt = make_target()
        tgt._wnd_present = lambda: False                  # type: ignore[assignment]
        tgt._qt_has_mmui = lambda h: False                # type: ignore[assignment]
        tgt._is_window_visible = lambda h: False          # type: ignore[assignment]
        tgt._hwnd_by_class = lambda: 0x1234               # type: ignore[assignment]
        ok, why = tgt.health_check()
        self.assertFalse(ok)
        self.assertIn("隐藏", why)
        self.assertNotIn("自绘空壳", why)


class TestConfigPlumbing(unittest.TestCase):
    """`wechat.show_hotkey` 要能从配置文件一路传到投递端。"""

    def test_load_and_build(self):
        import tempfile
        from bridge.config import load_config
        from bridge.targets import build_target

        yaml_text = (
            "wechat:\n"
            "  wxauto_path: '.'\n"
            "  driver: ui4\n"
            "  show_hotkey: \"{Ctrl}{Alt}q\"\n"
            "routes:\n"
            "  - name: r1\n"
            "    targets: [\"某个群\"]\n"
        )
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text(yaml_text, encoding="utf-8")
            cfg = load_config(p)
            self.assertEqual(cfg.wechat.show_hotkey, "{Ctrl}{Alt}q")
            tgt = build_target(cfg)
            self.assertIsInstance(tgt, WeChat4Target)
            self.assertEqual(tgt.show_hotkey, "{Ctrl}{Alt}q")

    def test_missing_key_falls_back_to_default(self):
        import tempfile
        from bridge.config import load_config

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text("wechat:\n  wxauto_path: '.'\n"
                         "routes:\n  - name: r1\n    targets: [\"某个群\"]\n",
                         encoding="utf-8")
            cfg = load_config(p)
            self.assertEqual(cfg.wechat.show_hotkey, WeChat4Target.DEFAULT_SHOW_HOTKEY)


class TestHealthCheckDiagnosis(unittest.TestCase):
    """探活必须把「环境不对」和「微信窗口不在」分开报。

    现场踩过（2026-10-17）：用缺 comtypes 的解释器启动，UIA 加载失败被当成
    「找不到窗口」，于是报「读不到微信主窗口，请把窗口从托盘唤回」。窗口明明
    就在屏幕上，人照着点托盘、按热键折腾了二十分钟才找到真正原因。
    错误信息的指向性本身就是可用性的一部分。
    """

    def test_missing_comtypes_is_reported_as_environment(self):
        tgt = make_target()

        def boom():
            raise ModuleNotFoundError("No module named 'comtypes'")

        tgt._load_uia = boom                       # type: ignore[assignment]
        ok, why = tgt.health_check()
        self.assertFalse(ok)
        self.assertIn("comtypes", why)
        self.assertIn("环境", why)
        self.assertNotIn("托盘", why, "缺依赖被说成「窗口在托盘」会把排障带偏")

    def test_find_window_does_not_swallow_import_error(self):
        """_find_window 只吞「找不到窗口」，不能把 ImportError 一起吞掉。"""
        tgt = make_target()

        def boom():
            raise ModuleNotFoundError("No module named 'comtypes'")

        tgt._load_uia = boom                       # type: ignore[assignment]
        with self.assertRaises(ModuleNotFoundError):
            tgt._find_window()


class TestConstants(unittest.TestCase):
    def test_sane_constants(self):
        self.assertGreater(MIN_WINDOW_SIDE, 50)
        self.assertGreaterEqual(CLICK_RETRIES, 2)
        self.assertGreater(WeChat4Target.SCROLL_TO_TOP_TURNS, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
