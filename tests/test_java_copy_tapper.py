"""「复制」自动点击器的静态护栏。

用户 2026-11-02：群里的「复制」是考核数据，看到就要点，但不许粘贴出来。
这个功能全是副作用（会真的点屏幕），所以护栏钉的不是「能不能点」，
而是几条「绝对不能越界」的线：

1. 只认「复制」两个字，别把「复制链接」「复制单号」这类也算上；
2. 必须去重（同一张卡片一轮巡检会出现在几十帧里，不去重就是一秒点十下）；
3. 必须有节流（间隔 + 每帧上限），别把抓帧拖慢；
4. 附件抓取在跑时不许点（手势同一条通道，抢了会打断抓取）；
5. 调用必须排在 md5 比对**之前** —— 点完画面文字没变，放后面等于没加；
6. 不许碰剪贴板（用户的原话：不用粘贴出来）。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "android" / "src" / "com" / "hbdxm" / "ddwx"
TAPPER = SRC / "CopyTapper.java"
SERVICE = SRC / "CollectorService.java"


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


class TestCopyTapperExists(unittest.TestCase):
    def test_file_exists(self):
        self.assertTrue(TAPPER.is_file(), f"找不到 {TAPPER}")

    def test_only_literal_copy(self):
        """只认「复制」两个字：别的按钮文案一概不点。"""
        text = TAPPER.read_text(encoding="utf-8")
        self.assertIn('"复制".equals(desc)', text)
        self.assertIn('"复制".equals(text)', text)
        # 不许出现「包含复制就算」的写法
        self.assertNotRegex(text, r'desc\.contains\(\s*"复制"')
        self.assertNotRegex(text, r'text\.contains\(\s*"复制"')

    def test_no_clipboard(self):
        """用户明确说过：不用粘贴出来。所以这个类不许碰剪贴板。"""
        text = TAPPER.read_text(encoding="utf-8")
        for bad in ("ClipboardManager", "getPrimaryClip", "setPrimaryClip"):
            self.assertNotIn(bad, text,
                             f"复制按钮只该点一下，不该动剪贴板：{bad}")


class TestCopyTapperGuards(unittest.TestCase):
    def test_dedup_persisted(self):
        """已点指纹必须落盘，重启 App 不能重头再点一遍。"""
        text = TAPPER.read_text(encoding="utf-8")
        self.assertIn("SharedPreferences", text)
        self.assertIn("PREF_KEY", text)
        self.assertRegex(text, r"static\s+void\s+save\(")
        self.assertRegex(text, r"static\s+Set<String>\s+load\(")

    def test_throttle(self):
        text = TAPPER.read_text(encoding="utf-8")
        self.assertRegex(text, r"MIN_GAP_MS\s*=\s*\d+L")
        self.assertRegex(text, r"MAX_PER_FRAME\s*=\s*\d+")
        self.assertIn("now - lastTapAt < MIN_GAP_MS", text)

    def test_skips_while_attach_busy(self):
        text = TAPPER.read_text(encoding="utf-8")
        self.assertRegex(text, r"if\s*\(svc == null \|\| root == null \|\| busy\)")

    def test_offscreen_guard(self):
        """视口外的节点不许点（bounds 有效但会打到别的控件）。"""
        text = TAPPER.read_text(encoding="utf-8")
        self.assertIn("getDisplayMetrics", text)
        self.assertIn("r.centerX() >= screenW", text)


class TestCopyTapperWiring(unittest.TestCase):
    def test_called_before_md5_check(self):
        """调用必须在 md5 短路之前，否则画面没变就永远走不到。"""
        lines = _lines(SERVICE)
        call_at = md5_at = -1
        for i, ln in enumerate(lines):
            if "CopyTapper.tapNew" in ln and call_at < 0:
                call_at = i
            if "md5.equals(sentMd5)" in ln and md5_at < 0:
                md5_at = i
        self.assertGreater(call_at, 0, "CollectorService 里没调用 CopyTapper.tapNew")
        self.assertGreater(md5_at, 0, "找不到 md5 短路那一行，测试需要更新")
        self.assertLess(call_at, md5_at,
                        "CopyTapper 必须在 md5 比对之前调用（点完画面不变会短路）")

    def test_passes_attach_busy(self):
        text = SERVICE.read_text(encoding="utf-8")
        self.assertRegex(text, r"CopyTapper\.tapNew\([^)]*attachBusy\.get\(\)",
                         "附件抓取进行中必须让路，不能抢手势")


if __name__ == "__main__":
    unittest.main()
