# -*- coding: utf-8 -*-
"""文件投递校验不得把输入框当成聊天区（2026-11-05 16:24 附件假成功）。

现象：日志「附件已投递 …（回读到文件名）」，微信群里却只有
「以下为附件：重点商家运单明细.xlsx」的文字，文件卡片没有。

根因：`_LAYOUT["message"]` y1=0.94 把输入框（y≈0.885 起）罩进了 OCR 范围
—— 回车早了、文件芯片晚挂上时，「回读到文件名」读到的是**输入框里挂着的
附件芯片**，假成功；像素兜底 `_bottom_changed` 直接比整帧底部 18%
（y 0.82-1.0），同样被输入框污染。

修法：新增 `chat` 裁剪区（y1=0.875，输入框上沿之上）；文件校验 OCR 与
像素判据都只在 chat 区内做；校验不过先补点一次发送（芯片多半还挂在输入
框里）、再完整重粘一轮，都失败才如实报失败。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.targets.wechat_ocr import _LAYOUT, WeChatOcrTarget  # noqa: E402

try:
    from PIL import Image
    import numpy as np  # noqa: F401
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

W, H = 714, 1067          # 实测窗口尺寸量级
WHITE = (247, 247, 247)


def _frame():
    return Image.new("RGB", (W, H), WHITE)


@unittest.skipUnless(_HAS_PIL, "需要 Pillow/numpy")
class TestChatRegionLayout(unittest.TestCase):
    def test_chat_region_excludes_input_box(self):
        """chat 区下沿必须压在输入框上沿（0.885）之上。"""
        self.assertLess(_LAYOUT["chat"][3], 0.885)
        # message 区仍然罩着输入框（历史行为，别的地方还在用），别误改
        self.assertGreater(_LAYOUT["message"][3], 0.885)


@unittest.skipUnless(_HAS_PIL, "需要 Pillow/numpy")
class TestBottomChangedChatCrop(unittest.TestCase):
    def setUp(self):
        self.t = WeChatOcrTarget(dry_run=True)

    @staticmethod
    def _paint(img, y_from_ratio, color):
        px = img.load()
        y0 = int(H * y_from_ratio)
        for y in range(y0, H):
            for x in range(int(W * 0.5), W):
                px[x, y] = color
        return img

    def test_change_in_input_area_is_not_chat_change(self):
        """变化只发生在输入框高度 → 裁剪后必须判「没变化」。"""
        before = _frame()
        after = self._paint(_frame(), 0.90, (200, 200, 200))
        self.assertFalse(self.t._bottom_changed(
            self.t._crop_chat(before), self.t._crop_chat(after)))

    def test_change_in_chat_bottom_is_a_change(self):
        """变化发生在聊天历史区底部 → 判「有变化」。"""
        before = _frame()
        after = self._paint(_frame(), 0.80, (200, 200, 200))
        self.assertTrue(self.t._bottom_changed(
            self.t._crop_chat(before), self.t._crop_chat(after)))

    def test_raw_full_frame_change_in_input_band_triggers(self):
        """不裁剪直接比整帧（旧行为）会被输入框骗到 —— 保留此对照，
        防止有人把调用点的 _crop_chat 摘掉。"""
        before = _frame()
        after = self._paint(_frame(), 0.90, (200, 200, 200))
        self.assertTrue(WeChatOcrTarget._bottom_changed(before, after))


@unittest.skipUnless(_HAS_PIL, "需要 Pillow/numpy")
class TestVerifyFileSentRegion(unittest.TestCase):
    def setUp(self):
        self.t = WeChatOcrTarget(dry_run=True)

    def test_chip_in_input_area_is_not_evidence(self):
        """文件名只出现在输入框高度（芯片没发出去）→ 不得判成功。

        用假 _grab 返回一张只在输入框区域画了文件名主干「重点商家运单」
        的帧 —— 但 OCR 是真跑不动的，这里退一步：只验证 OCR 区域裁剪
        （用 _ocr_region 的 box 输出间接验证超范围，太重）。
        改为直接验证 _verify_file_sent 在「OCR 永远返回空」时走像素
        兜底、且兜底被 chat 裁剪挡住（输入框变化不算数）。
        """
        self.t._ocr = lambda arr: ([], None)   # OCR 永远空
        self.t._ocr_region = lambda img, name, zoom=0: ([], None, 1)
        before = _frame()
        after = self._paint_input_band()
        self.t._grab = lambda hwnd: after
        ok, how, _f = self.t._verify_file_sent(hwnd=0, path=self._fake_xlsx(),
                                               before=before)
        self.assertFalse(ok, f"输入框里的芯片被当成了发送证据：{how}")

    def _paint_input_band(self):
        img = _frame()
        px = img.load()
        for y in range(int(H * 0.90), H):
            for x in range(int(W * 0.5), W):
                px[x, y] = (200, 200, 200)
        return img

    @staticmethod
    def _fake_xlsx():
        from bridge.targets.wechat_ocr import Path as _P  # noqa
        import tempfile, os
        f = os.path.join(tempfile.gettempdir(), "重点商家运单明细.xlsx")
        Path(f).write_bytes(b"x")
        return Path(f)

    def tearDown(self):
        p = self._fake_xlsx()
        p.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
