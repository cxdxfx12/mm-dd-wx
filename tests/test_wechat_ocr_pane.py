# -*- coding: utf-8 -*-
"""标题栏兜底判据（``_pane_is_open``）与已打开会话的短路。

来源：wxauto 4.x 复刻版（``yuan71058/wechatauto-replica``）的两个做法，
对应本机两个真实痛点（2026-10-18）：

1. **标题栏读不到 ≠ 会话没打开**
   历史日志里「点击会话后标题栏未确认」一天 17 次，其中大部分是
   界面重绘期间标题区 OCR 返回空，但会话其实已经切过去了 —— 于是白等
   一轮重试（有时还因为重试点到了别处）。
   复刻版的做法是「标题读不到就退而判面板非空白」。我们这里改成更强的
   判据：**消息区存在成片非背景像素**（气泡/头像/文字块），因为微信没打开
   任何会话时右侧也有 logo 和提示语，OCR 判「有文字」会一律放行。

2. **已打开就直接返回**
   复刻版已打开场景 45s → 2.7s。我们是反过来的：先全量扫列表
   （抓帧 + 3x OCR）再判「已在目标」。同一批连发多条时，除第一条外
   每次都在为一个已知答案付全价。改成先读标题栏（约 1/5 成本）。

⚠ 判据边界：**「标题读到但和目标对不上」绝不能兜底放行** ——
那说明点进了别的会话，放过去就是发错群，比投递失败严重得多。
这一条单独有用例锁住。
"""

import sys
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


def _blank(size=(720, 1088), bg=(247, 247, 247)):
    img = Image.new("RGB", size, bg)
    return img


def _with_bubbles(size=(720, 1088), bg=(247, 247, 247)):
    """画几个灰气泡块 + 深色文字条 —— 模拟「打开了一个有聊天记录的会话」。

    ⚠ 光有白气泡不够：真机量过（见 `_pane_is_open` 注释），气泡里的
    **文字/头像**才是占比大头（真实聊天 ≈53%，纯白气泡 ≈0.6%）。
    判据本来就不该把「只有气泡没有任何内容」的画面当已打开。
    """
    from bridge.targets.wechat_ocr import _LAYOUT
    img = _blank(size, bg)
    d = ImageDraw.Draw(img)
    W, H = size
    x0, y0, x1, y1 = _LAYOUT["message"]
    left, top = int(W * x0), int(H * y0)
    right = int(W * x1)
    for i in range(4):
        y = top + 40 + i * 90
        d.rectangle([left + 60, y, min(left + 320, right - 20), y + 46],
                    fill=(255, 255, 255), outline=(200, 200, 200))
        for k in range(2):
            d.rectangle([left + 75, y + 8 + k * 18, left + 250,
                         y + 20 + k * 18], fill=(60, 60, 60))
    return img


def _with_green_bubble(size=(720, 1088)):
    """只放一个绿气泡：`_has_outgoing_bubble` 应当直接命中。"""
    from bridge.targets.wechat_ocr import _LAYOUT
    img = _blank(size)
    d = ImageDraw.Draw(img)
    W, H = size
    x0, y0, x1, y1 = _LAYOUT["message"]
    left, top = int(W * x0), int(H * y0)
    right = int(W * x1)
    bw = int((right - left) * 0.55)
    d.rectangle([right - bw - 20, top + 400, right - 20, top + 700],
                fill=(149, 236, 105))                # #95EC69
    return img


@unittest.skipUnless(_HAS_PIL, "本机没装 Pillow，画面类测试跳过")
class TestPaneIsOpen(unittest.TestCase):
    """``_pane_is_open``：光看聊天面板能不能判断「会话打开了吗」。"""

    def test_blank_pane_is_not_open(self):
        """纯背景 → 没打开（此时微信右侧展示的是 logo，不构成会话）。"""
        tgt = make_target()
        ok, why = tgt._pane_is_open(_blank())
        self.assertFalse(ok, why)

    def test_pane_with_message_bubbles_is_open(self):
        tgt = make_target()
        ok, why = tgt._pane_is_open(_with_bubbles())
        self.assertTrue(ok, why)
        self.assertIn("非空白", why)

    def test_green_bubble_alone_is_open(self):
        """绿气泡是硬证据：刚发过消息的会话必然算已打开。"""
        tgt = make_target()
        ok, why = tgt._pane_is_open(_with_green_bubble())
        self.assertTrue(ok, why)
        self.assertIn("发出气泡", why)

    def test_dark_theme_pane_is_detected(self):
        """深色主题：背景近黑，判据靠「和边缘色不同」而不是写死白色。"""
        tgt = make_target()
        img = Image.new("RGB", (720, 1088), (32, 32, 32))
        from bridge.targets.wechat_ocr import _LAYOUT
        d = ImageDraw.Draw(img)
        W, H = img.size
        x0, y0, x1, y1 = _LAYOUT["message"]
        left, top = int(W * x0), int(H * y0)
        for i in range(4):
            y = top + 40 + i * 90
            d.rectangle([left + 60, y, left + 320, y + 46],
                        fill=(70, 70, 70))
        ok, why = tgt._pane_is_open(img)
        self.assertTrue(ok, why)

    def test_tiny_crop_does_not_raise(self):
        """窗口极小（裁剪退化）→ 返回 False，不许抛。"""
        tgt = make_target()
        ok, why = tgt._pane_is_open(Image.new("RGB", (12, 12)))
        self.assertFalse(ok)
        self.assertTrue(why)


@unittest.skipUnless(_HAS_PIL, "本机没装 Pillow，画面类测试跳过")
class TestOpenChatFallback(unittest.TestCase):
    """``_open_chat`` 的两段式判据 + 短路。全部在实例方法层打桩。"""

    def _target(self, title_seq, pane_img, hits=None, clicks=None):
        """造一个只跑 `_open_chat` 逻辑的实例。

        - ``title_seq``：`_current_chat_title` 依次返回的 (ok, title)
          元组序列（越界后复用最后一个）
        - ``pane_img``：`_grab` 返回的画面
        - ``hits``：`_best_match` 固定返回的 (item, score)，默认命中「目标群」
        - ``clicks``：记录点击坐标
        """
        tgt = make_target()
        seq = list(title_seq)
        calls = {"n": 0}

        def fake_title(hwnd):
            i = min(calls["n"], len(seq) - 1)
            calls["n"] += 1
            return seq[i]

        tgt._current_chat_title = fake_title                # type: ignore
        tgt._grab = lambda hwnd: pane_img                   # type: ignore
        tgt._activate = lambda hwnd: True                   # type: ignore
        item = {"text": "目标群", "kind": "session",
                "cx": 100.0, "cy": 300.0,
                "x0": 20.0, "x1": 180.0, "y0": 280.0, "y1": 320.0}
        tgt._best_match = lambda t, items: (                    # type: ignore
            (hits or (item, 1.0)))
        tgt._snapshot = lambda hwnd: (pane_img, [item])    # type: ignore
        tgt._click_abs = lambda hwnd, cx, cy: (             # type: ignore
            (clicks.append((cx, cy)) if clicks is not None else None) or True)
        tgt._find_window = lambda: 1234                     # type: ignore
        return tgt, calls

    def test_short_circuit_skips_list_scan(self):
        """标题栏已经是目标 → 直接成功，一次列表扫描都不做。"""
        tgt, calls = self._target(((True, "目标群"),), _with_bubbles())
        scanned = {"n": 0}

        def boom(hwnd):
            scanned["n"] += 1
            raise AssertionError("已打开目标时不该再扫会话列表")

        tgt._snapshot = boom                                # type: ignore
        ok, why = tgt._open_chat(1234, "目标群")
        self.assertTrue(ok, why)
        self.assertEqual(scanned["n"], 0)
        self.assertIn("已在目标会话", why)
        self.assertEqual(calls["n"], 1, "短路只该读一次标题栏")

    def test_title_empty_but_pane_open_is_success(self):
        """标题读不到、面板有内容 → 按成功处理（不再白重试一轮）。"""
        tgt, _ = self._target(((False, "标题栏无文字"),), _with_bubbles())
        ok, why = tgt._open_chat(1234, "目标群")
        self.assertTrue(ok, why)
        self.assertIn("聊天面板判据确认", why)

    def test_title_empty_and_pane_blank_still_fails(self):
        """标题读不到、面板也空 → 仍然算失败（不能因为兜底就永远成功）。"""
        tgt, _ = self._target(((False, "标题栏无文字"),), _blank())
        ok, why = tgt._open_chat(1234, "目标群")
        self.assertFalse(ok, why)
        self.assertIn("标题栏未出现目标会话", why)

    def test_title_readable_but_wrong_never_falls_back(self):
        """⚠ 最关键的一条：标题读到但**是别的群** → 绝不兜底放行。

        走到这里说明点击点进了错误的会话，放过去就是**把消息发错群**。
        面板有内容也不能救 —— 那时面板里显示的是别人的聊天记录。
        """
        tgt, _ = self._target(((True, "另一个业务群"),), _with_bubbles())
        ok, why = tgt._open_chat(1234, "目标群")
        self.assertFalse(ok, why)
        self.assertIn("标题栏未出现目标会话", why)
        self.assertIn("另一个业务群", why)

    def test_other_chat_open_before_blocks_pane_fallback(self):
        """⚠ 点击前就开着别的群 + 点击后标题偶发读不到 → 也不许兜底。

        场景：右侧开着「另一个业务群」，点击目标被微信吞掉，标题栏又
        恰好读不到 —— 此时「面板非空」只能证明**原来那个群还开着**，
        证明不了目标打开了。放行 = 发错群。
        标题序列：先读到别的群（fast + 点击前状态），点击后读不到。
        """
        tgt, _ = self._target(((True, "另一个业务群"),
                               (False, "标题栏无文字")), _with_bubbles())
        ok, why = tgt._open_chat(1234, "目标群")
        self.assertFalse(ok, why)
        self.assertIn("标题栏未出现目标会话", why)


class TestZoomUnified(unittest.TestCase):
    """放大倍率统一走 `OCR_ZOOM`，别再各处写死 2x / 3x。"""

    def test_zoom_is_three(self):
        """锁住 wxauto 复刻版的实测结论：中文 3x 最优，超 5x 整块返回空。"""
        self.assertEqual(WeChatOcrTarget.OCR_ZOOM, 3)

    def test_ocr_region_restores_coordinates(self):
        """`_ocr_region` 返回的坐标是放大后的，调用方除以 zoom 得回原图。

        这里直接验算术：造一个只有 PIL 的假图，桩掉 `_ocr` 让它返回
        已知坐标，检查 `_ocr_sessions` 能还原到整图坐标系。
        """
        if not _HAS_PIL:
            self.skipTest("本机没装 Pillow")

        from bridge.targets.wechat_ocr import _LAYOUT

        tgt = make_target()
        img = _blank()
        W, H = img.size
        x0, y0, _x1, _y1 = _LAYOUT["list"]
        zoom = WeChatOcrTarget.OCR_ZOOM

        # 在裁切图里的 (10, 20)，放大后应为 (10*zoom, 20*zoom)
        pts = [[10.0 * zoom, 20.0 * zoom],
               [60.0 * zoom, 20.0 * zoom],
               [60.0 * zoom, 34.0 * zoom],
               [10.0 * zoom, 34.0 * zoom]]
        tgt._ocr = lambda arr: [(pts, "某群名", 0.99)]      # type: ignore
        items = tgt._ocr_sessions(img)
        sess = [it for it in items if it["kind"] == "session"]
        self.assertEqual(len(sess), 1)
        s = sess[0]
        self.assertAlmostEqual(s["x0"], int(W * x0) + 10.0, delta=1.0)
        self.assertAlmostEqual(s["y0"], int(H * y0) + 20.0, delta=1.0)


if __name__ == "__main__":
    unittest.main()
