"""版本号：唯一来源、显示在底部、格式「0.xx」。

用户 2026-10-19 的原话：「在软件的底部加上版本号，以便每次更新或者修复以后
知道是哪个版本，每次更新加 0.01」。

真正要守住的是**只有一处真相**。版本号一旦在三四个文件里各写一份，迟早
出现「界面显示 0.31、控制面板显示 1.0.0」这种错位 —— 用户报版本时报错，
排查方向直接跑偏。所以这里既测显示，也测「别的地方没有硬编码」。
"""

import importlib.util
import re
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (BASE / rel).read_text(encoding="utf-8")


class TestVersionSource(unittest.TestCase):

    def test_format_is_two_decimals(self):
        """「每次加 0.01」意味着必须是两位小数，0.3 和 0.31 不能混着用。"""
        from bridge import __version__
        self.assertRegex(__version__, r"^\d+\.\d{2}$",
                         "版本号必须写成 0.xx（两位小数），否则加 0.01 会漂")

    def test_build_reads_the_same_value(self):
        """installer/build.py 注入安装包时读的是同一个文件。"""
        from bridge import __version__
        spec = importlib.util.spec_from_file_location(
            "_ddwx_build", BASE / "installer" / "build.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)          # 模块级只有常量，不会真去打包
        self.assertEqual(mod.app_version(), __version__)

    def test_setup_fallback_matches(self):
        """安装向导的兜底 VERSION 与主程序一致。

        build.py 打包时会覆盖这一行，但手工单独跑 setup_gui.py 调试时用的是
        兜底值 —— 不一致就会显示成旧的。
        """
        from bridge import __version__
        m = re.search(r'^VERSION = "([^"]+)"',
                      read("installer/setup_gui.py"), re.M)
        self.assertIsNotNone(m, "setup_gui.py 里找不到 VERSION")
        self.assertEqual(m.group(1), __version__)


class TestVersionOnScreen(unittest.TestCase):
    """主程序底部必须看得到版本号。"""

    def test_gui_imports_instead_of_hardcoding(self):
        src = read("app/gui.py")
        self.assertIn("from bridge import __version__ as APP_VERSION", src)
        self.assertNotRegex(src, r'APP_VERSION\s*=\s*"',
                            "gui.py 不许自己写死版本号")

    def test_status_bar_shows_version(self):
        src = read("app/gui.py")
        self.assertIn('text=f"版本 {APP_VERSION}"', src,
                      "底部状态栏没显示版本号")

    def test_version_label_is_on_the_right_of_status_bar(self):
        """放右侧：左边那格是运行状态，随时会变，不能跟它抢位置。"""
        src = read("app/gui.py")
        idx = src.index("self.ver_label = tk.Label")
        tail = src[idx:idx + 400]
        self.assertIn('pack(side="right"', tail)

    def test_about_dialog_shows_version(self):
        self.assertIn("版本 {APP_VERSION}", read("app/gui.py"),
                      "关于对话框里也要有版本（截图报障时最常看这里）")


if __name__ == "__main__":
    unittest.main()
