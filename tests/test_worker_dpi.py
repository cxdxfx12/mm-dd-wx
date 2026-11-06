"""worker 进程 DPI 感知 + 多屏抓帧（2026-11-06 加第二块屏后的投递事故）。

事故现场
    双屏、主屏 125% 缩放。GUI 进程声明了 Per-Monitor V2，但 worker 走
    ``run.py`` 没声明 → ``GetWindowRect`` / ``SetCursorPos`` 拿到逻辑坐标，
    ``ImageGrab`` 抓物理像素，两者差 25%。截图偏左上、OCR 读到旁边的钉钉，
    点击跟着错位 —— 用户看到的是「只转了一条，另一条没按发送」。

    另有一个潜伏缺陷：``all_screens=False`` 只抓主屏，微信一旦被拖到副屏
    （本例副屏 x=2560 起）就抓回黑图/隔壁窗口。

这里钉住三件事：入口必须声明 DPI、抓屏必须跨全虚拟桌面、辅助函数本身可用。
"""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def read(rel: str) -> str:
    return (BASE / rel).read_text(encoding="utf-8")


class WorkerDpiTest(unittest.TestCase):

    def test_run_py_enables_dpi_before_importing_bridge(self):
        """必须赶在 bridge.config / 任何窗口 API 之前，晚了就不生效。"""
        src = read("run.py")
        tree = ast.parse(src)
        # 找 `_enable_dpi()` 与 `from bridge.config import ...` 的行号
        call_line = None
        import_line = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "_enable_dpi":
                call_line = node.lineno
            if isinstance(node, ast.ImportFrom) and node.module == "bridge.config":
                import_line = node.lineno
        self.assertIsNotNone(call_line, "run.py 没调用 _enable_dpi()")
        self.assertIsNotNone(import_line)
        self.assertLess(call_line, import_line,
                        "DPI 声明必须在 import bridge.config 之前")

    def test_wechat_ocr_grabs_all_screens(self):
        """两处 ImageGrab 调用都要 all_screens=True，退回 False 会让副屏抓瞎。

        只看**调用点**（AST），别拿全文字符串匹配 —— 文档注释里会留着
        ``all_screens=False`` 讲历史，那不是代码。
        """
        tree = ast.parse(read("bridge/targets/wechat_ocr.py"))
        flags = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "grab":
                for kw in node.keywords:
                    if kw.arg == "all_screens":
                        flags.append(bool(kw.value.value))
        self.assertEqual(len(flags), 2,
                         "抓帧有两条路径（常规 / 置顶强抓），都要显式声明")
        self.assertEqual(flags, [True, True],
                         "不许只抓主屏：微信拖到副屏会抓成黑图")

    def test_enable_really_changes_awareness(self):
        """函数不是摆设：调用后当前进程必须是 per-monitor aware。"""
        from bridge import win_dpi
        before = win_dpi.awareness()
        if sys.platform != "win32":
            self.assertFalse(win_dpi.enable())
            self.assertEqual(before, -1)
            return
        self.assertTrue(win_dpi.enable(), "三种 API 全失败，本机 DPI 无法声明")
        self.assertEqual(win_dpi.awareness(), 2,
                         "调用后仍不是 Per-Monitor V2，坐标还是会错")

    def test_enable_is_idempotent(self):
        """worker / 其它入口可能重复调用，第二次不能抛异常。"""
        from bridge import win_dpi
        win_dpi.enable()
        win_dpi.enable()          # 只要求不抛，Windows 会拒绝二次设置


if __name__ == "__main__":
    unittest.main()
