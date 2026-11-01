"""OCR 投递端「发原文件」的回归。

背景（2026-10-31 用户要求）：附件一律发原文件，不再渲染成长图。
微信 4.x 的控件树不可用，文件只能走「CF_HDROP 放剪贴板 → Ctrl+V → 发送」
这条路，所以这几条用例锁住三件事：
  1. dry-run 不得真碰剪贴板/窗口；
  2. 文件不存在要**报错而不是静默成功**（否则附件悄悄丢了没人知道）；
  3. 回读校验的弱判据（消息区底部变化）别写成恒真/恒假。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.targets.wechat_ocr import WeChatOcrTarget  # noqa: E402

try:
    from PIL import Image, ImageDraw
    _HAS_PIL = True
except ImportError:                                  # 测试用 Python 无 Pillow
    _HAS_PIL = False


def make_target() -> WeChatOcrTarget:
    return WeChatOcrTarget(dry_run=True, focus_delay=0.0)


class TestSendFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_dry_run_does_not_touch_clipboard(self):
        f = Path(self.tmp.name) / "周报.xlsx"
        f.write_bytes(b"PK\x03\x04fake")
        r = make_target().send_file("测试群", str(f))
        self.assertTrue(r.ok)
        self.assertIn("dry-run", r.detail)

    def test_missing_file_fails_loudly(self):
        r = make_target().send_file("测试群", str(Path(self.tmp.name) / "nope.xlsx"))
        self.assertFalse(r.ok)
        self.assertIn("文件不存在", r.detail)

    def test_empty_target_rejected(self):
        f = Path(self.tmp.name) / "a.xlsx"
        f.write_bytes(b"x")
        r = make_target().send_file("", str(f))
        self.assertFalse(r.ok)
        self.assertIn("目标为空", r.detail)

    def test_target_overrides_base_send_file(self):
        """基类的 send_file 是「不支持」的兜底 —— 子类必须真的覆盖掉它。"""
        from bridge.targets.base import Target

        self.assertIsNot(WeChatOcrTarget.send_file, Target.send_file)


@unittest.skipUnless(_HAS_PIL, "需要 Pillow")
class TestBottomChanged(unittest.TestCase):
    """`_bottom_changed`：文件卡片落地的弱证据，不能恒真也不能恒假。"""

    def _blank(self, h=400, w=300):
        return Image.new("L", (w, h), 247)

    def test_identical_frames_are_not_a_change(self):
        img = self._blank()
        self.assertFalse(WeChatOcrTarget._bottom_changed(img, img.copy()))

    def test_new_card_at_bottom_is_a_change(self):
        before = self._blank()
        after = self._blank()
        d = ImageDraw.Draw(after)
        d.rectangle([20, 340, 280, 390], fill=60)     # 底部落了一张深色卡片
        self.assertTrue(WeChatOcrTarget._bottom_changed(before, after))

    def test_change_only_at_top_is_ignored(self):
        """顶部变化（比如标题栏闪了一下）不算 —— 只看底部 18%。"""
        before = self._blank()
        after = self._blank()
        ImageDraw.Draw(after).rectangle([0, 0, 299, 30], fill=60)
        self.assertFalse(WeChatOcrTarget._bottom_changed(before, after))


if __name__ == "__main__":
    unittest.main()
