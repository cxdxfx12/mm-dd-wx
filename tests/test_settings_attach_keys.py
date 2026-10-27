"""设置中心「附件转发」页签的纯逻辑部分（不建窗口）。

界面本身没法在这里点，但**显示/回落规则**必须测：用户手工把配置写成
YAML 列表、写成中文逗号、或者干脆清空，设置窗口打开时都得显示成人能看懂
的样子；而默认值一旦和网关那边不一致，就会出现「界面上写着 A、实际按 B
在跑」这种最难解释的现象。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# settings_ui 里是 `import cfgfile`（同目录裸导入），所以 app/ 也要在路径上。
sys.path.insert(0, str(ROOT / "app"))

import settings_ui  # noqa: E402  必须在 sys.path 之后


class TestAttachKeysText(unittest.TestCase):

    def test_list_form(self):
        self.assertEqual(settings_ui.attach_keys_text(["附件：", "附件:"]),
                         "附件：,附件:")

    def test_string_form(self):
        self.assertEqual(settings_ui.attach_keys_text("附件：,明细："),
                         "附件：,明细：")

    def test_chinese_comma_and_spaces(self):
        """用户手写「附件：， 明细：」也要认 —— 中文逗号是默认输入法产物。"""
        self.assertEqual(settings_ui.attach_keys_text("附件：， 明细： "),
                         "附件：,明细：")

    def test_empty_falls_back_to_default(self):
        """空值回落默认，不能显示成空输入框。

        显示空的会让人以为「附件转发已关闭」，而网关拿到空值也是回落默认
        —— 也就是说它照常在抓。让人误以为关掉了，比不提供开关更糟。
        """
        for raw in ("", "  ", [], None, ",", "，，"):
            self.assertEqual(settings_ui.attach_keys_text(raw),
                             "附件：,附件:", f"输入 {raw!r}")

    def test_default_matches_gateway(self):
        """界面默认值必须与网关/手机端实际使用的默认值逐字相同。"""
        from bridge.sources.android_gateway import DEFAULT_ATTACH_KEYS
        self.assertEqual(tuple(settings_ui.DEFAULT_ATTACH_KEYS),
                         tuple(DEFAULT_ATTACH_KEYS))


class TestSettingsTabSource(unittest.TestCase):
    """确认「附件转发」这两个控件真的长在设置中心里（防回退）。"""

    def test_tab_builds_attach_widgets(self):
        src = (ROOT / "app" / "settings_ui.py").read_text(encoding="utf-8")
        for needle in ('self.st["attach_on"]', 'self.st["attach_keys"]',
                       "启用附件自动下载与转发", "恢复默认"):
            self.assertIn(needle, src)

    def test_save_writes_both_keys(self):
        src = (ROOT / "app" / "settings_ui.py").read_text(encoding="utf-8")
        self.assertIn('S("dingtalk", "attach_enabled"', src)
        self.assertIn('S("dingtalk", "attach_keys"', src)


if __name__ == "__main__":
    unittest.main()
