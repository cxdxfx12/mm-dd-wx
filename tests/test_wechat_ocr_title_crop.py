# -*- coding: utf-8 -*-
"""标题栏裁剪线在宽窗口下不得切掉群名开头（2026-10-31 22:02 投递失败）。

现象：用户把微信窗口拉宽到 972 后，往「申通五星业务员培训群」投递必败 ——
标题栏裁剪线取比例 0.40×972=388，而列表是**固定像素宽**（~310px）、标题
文字起点实测 x≈324，比例线把群名开头「申通五星」整段切掉，OCR 稳定读成
「业务员培训群(85)」，``_title_matches`` 永远不中。

修法：title 裁剪 x0 封顶 ``TITLE_X0_MAX_PX``（316），窄窗口仍用比例值。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.targets import wechat_ocr as m
from bridge.targets.wechat_ocr import WeChatOcrTarget  # noqa: E402

try:
    from PIL import Image
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False


@unittest.skipUnless(_HAS_PIL, "需要 Pillow")
class TestTitleCropX0Cap(unittest.TestCase):
    def setUp(self):
        self.tgt = WeChatOcrTarget(dry_run=True, focus_delay=0.0)
        # 不跑真 OCR：_ocr_region 只用到 box 计算和 crop，_ocr 返回空即可
        self.tgt._ocr = lambda arr: ([], None)

    def _title_x0(self, w, h=1088):
        img = Image.new("RGB", (w, h), (247, 247, 247))
        _items, box, _zoom = self.tgt._ocr_region(img, "title")
        return box[0]

    def test_wide_window_caps_to_fixed_px(self):
        # 972 宽（事故窗口）：0.40×972=388 会切掉起点 324 之前的群名开头，
        # 必须封顶到 TITLE_X0_MAX_PX
        self.assertEqual(self._title_x0(972), m.TITLE_X0_MAX_PX)

    def test_ultra_wide_window_also_capped(self):
        # 1440 宽同样封顶（0.40×1440=576 更离谱）
        self.assertEqual(self._title_x0(1440), m.TITLE_X0_MAX_PX)

    def test_narrow_window_keeps_ratio(self):
        # 720 宽（标准窗口）：0.40×720=288 < 316，行为与历史一致
        self.assertEqual(self._title_x0(720), 288)

    def test_cap_leaves_margin_before_title_start(self):
        # 封顶值必须落在实测标题起点（≈324）左侧，留出余量
        self.assertLess(m.TITLE_X0_MAX_PX, 324)

    def test_title_matches_still_rejects_truncated_title(self):
        # 修复前的病象：切掉「申通五星」后标题对不上 —— 必须继续拒绝，
        # 防止把标题不匹配的会话放行（发错群比投递失败严重）
        self.assertFalse(
            WeChatOcrTarget._title_matches(
                "申通五星业务员培训群", "业务员培训群(85) α"))
        # 修复后标题读全了就必须命中
        self.assertTrue(
            WeChatOcrTarget._title_matches(
                "申通五星业务员培训群", "申通五星业务员培训群(85)"))


if __name__ == "__main__":
    unittest.main()
