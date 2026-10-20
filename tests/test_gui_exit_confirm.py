"""退出确认。

事故：菜单里「退出」紧挨着「开始运行」，空闲状态点「退出」直接关窗，
一点就没了；只有正在转发时才弹确认框。这里锁死「任何情况都要先问」。

不用真的建 Tk 窗口（无头环境跑不起来），只把 _on_close 从类上摘下来，
绑到最小替身上跑 —— 它只用到 self.runner / self.state / self._refresh_state /
self.destroy / self._log_fh，以及模块里的 messagebox。
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import gui  # noqa: E402


class _Runner:
    def __init__(self, alive):
        self._alive = alive
        self.stopped = []

    def alive(self):
        return self._alive

    def stop(self, grace=4.0):
        self.stopped.append(grace)


class _FakeApp:
    """只提供 _on_close 用到的那几个属性。"""

    def __init__(self, running):
        self.runner = _Runner(running)
        self.state = "running" if running else "idle"
        self._log_fh = None
        self.refreshed = 0
        self.destroyed = 0

    def _refresh_state(self):
        self.refreshed += 1

    def destroy(self):
        self.destroyed += 1


def _call(app):
    gui.App._on_close(app)


class TestExitAlwaysAsks(unittest.TestCase):

    def test_idle_still_asks(self):
        """空闲时点退出也必须问一次 —— 这是本次修复的正题。"""
        app = _FakeApp(running=False)
        with mock.patch.object(gui.messagebox, "askyesno", return_value=True) as ask:
            _call(app)
        self.assertEqual(ask.call_count, 1, "空闲退出没有弹确认框")
        self.assertEqual(app.destroyed, 1)

    def test_idle_cancel_keeps_window(self):
        app = _FakeApp(running=False)
        with mock.patch.object(gui.messagebox, "askyesno", return_value=False):
            _call(app)
        self.assertEqual(app.destroyed, 0, "点了取消却还是关了窗口")

    def test_running_asks_then_stops(self):
        app = _FakeApp(running=True)
        with mock.patch.object(gui.messagebox, "askyesno", return_value=True):
            _call(app)
        self.assertEqual(app.runner.stopped, [4.0], "确认后退出了但没停转发")
        self.assertEqual(app.destroyed, 1)

    def test_running_cancel_does_not_stop(self):
        app = _FakeApp(running=True)
        with mock.patch.object(gui.messagebox, "askyesno", return_value=False):
            _call(app)
        self.assertEqual(app.runner.stopped, [], "点了取消却把转发停了")
        self.assertEqual(app.destroyed, 0)

    def test_default_button_is_no(self):
        """回车 / 误触回车不能顺手确认掉。"""
        with mock.patch.object(gui.messagebox, "askyesno", return_value=False) as ask:
            _call(_FakeApp(running=False))
        self.assertEqual(ask.call_args.kwargs.get("default"), "no")

    def test_running_uses_warning_icon(self):
        """两条路径文案不同，图标也该不同：在跑用警告，空闲用问号。"""
        seen = {}
        with mock.patch.object(gui.messagebox, "askyesno", return_value=False) as ask:
            _call(_FakeApp(running=True))
            seen["run"] = ask.call_args.kwargs.get("icon")
            _call(_FakeApp(running=False))
            seen["idle"] = ask.call_args.kwargs.get("icon")
        self.assertEqual(seen, {"run": "warning", "idle": "question"})


if __name__ == "__main__":
    unittest.main()
