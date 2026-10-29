"""锚点试探必须**单调累积**、且用内容签名判「翻没翻动」（2026-10-29 真机）。

现场（用户原话「老是锚点对不上」「不跳来回跳怎么找锚点呢」）：

    20:41:51 ~ 20:43:20  一连 40 多条：
    安卓画面解析：H5 卡片的锚点对不上 —— 已请手机翻一屏试着找回（第 1 次）

三个独立缺陷叠在一起：

1. **位置不累积**：锚点试探当时借用的是长消息补全的 ``scroll`` 指令，而手机端
   执行它的是「往上翻一屏 → 报帧 → **立刻翻回底部**」（补全必须回位，否则基线
   错位）。于是每一轮都从底部出发、只能看到底部上方**同一屏**，翻 14 次等于
   翻 1 次。日志实证：连续多帧字节数都是 180558/180567（同一屏）。
2. **判据被 H5 重绘骗过**：H5 卡片每重绘一次整批节点指纹都变，帧 md5 跟着变，
   于是「屏幕在动 → 继续翻」永远成立，试探永不收敛（一口气 175 次）。
3. **额度被反复清零**：补全帧路径里有一句「锚点找到就 align_tried = 0」，只清
   计数不清 align_at/align_md5 → 日志永远「第 1 次」，PROBE_MAX 形同虚设。

现在：试探走独立指令 ``probe``（只上翻、不回位，位置累积），判「翻没翻动」看
**内容签名**（发送者+正文），认输时下发 ``to_bottom`` 一次性开回最新再重建基线。
"""

import time
import unittest
from pathlib import Path

from bridge.sources import android_gateway as G
from bridge.sources.android_gateway import _Ctx, _Handler


def make_handler():
    h = object.__new__(_Handler)
    h.ctx = _Ctx(token="t", chat_name="数字质控")
    return h


class _U:
    """最小可用的画面单元：签名只看 sender + content。"""

    def __init__(self, sender, content):
        self.sender = sender
        self.content = content


class TestFrameSig(unittest.TestCase):
    """内容签名：H5 重绘（坐标/形态变）不该改变它。"""

    def test_same_content_same_sig(self):
        a = [_U("服务诊断专员", "异常出库件 8 条"), _U("签收跟单专员", "签收率 88.19%")]
        b = [_U("服务诊断专员", "异常出库件 8 条"), _U("签收跟单专员", "签收率 88.19%")]
        self.assertEqual(_Handler._frame_sig(a), _Handler._frame_sig(b))

    def test_content_change_changes_sig(self):
        a = [_U("服务诊断专员", "异常出库件 8 条")]
        b = [_U("服务诊断专员", "异常出库件 9 条")]
        self.assertNotEqual(_Handler._frame_sig(a), _Handler._frame_sig(b))

    def test_empty_frame_is_stable(self):
        """空帧不能和「有内容」撞签名（否则空帧会被当成「翻不动」）。"""
        self.assertNotEqual(_Handler._frame_sig([]),
                            _Handler._frame_sig([_U("", "")]))


class TestMonotonicProbe(unittest.TestCase):
    """probe 必须累积，且下发的是 probe 而不是 scroll。"""

    def test_probe_sets_monotonic_counter(self):
        h = make_handler()
        h.ctx.align_md5 = "m0"
        for i in range(1, 5):
            h.ctx.align_at = time.monotonic() - 5.0
            sig = "sig%d" % i
            self.assertEqual(h._probe_decision("m%d" % i, sig), "probe")
            self.assertEqual(h.ctx.want_probe, i,
                             "下发轮次要逐轮递增（手机端据此累积上翻）")
            self.assertEqual(h.ctx.align_sig, sig, "要记住本轮内容，下一轮比对")

    def test_same_screen_with_new_md5_gives_up(self):
        """H5 重绘让 md5 每帧都变，但内容一模一样 → 翻不动，必须收敛。

        这是今天死循环的直接复现：旧实现只看 md5，永远判「屏幕在动」。
        现在的收敛动作是「请手机回到底部」（want_bottom / await_bottom），
        真正重建基线发生在回底那一帧。
        """
        h = make_handler()
        h.ctx.align_md5 = "m0"
        h.ctx.align_sig = "SAME"
        md5_i = 0
        verdicts = []
        for _ in range(G.PROBE_STUCK_MAX + 2):
            h.ctx.align_at = time.monotonic() - 5.0
            md5_i += 1
            verdicts.append(h._probe_decision("md5-%d" % md5_i, "SAME"))
        self.assertNotIn("probe", verdicts[-1:],
                         "同一屏（内容不变、md5 变）必须收敛，不能无限翻")
        self.assertTrue(h.ctx.want_bottom, "翻不动就要请手机回到底部")

    def test_give_up_asks_phone_to_go_bottom(self):
        """认输不能就地重建基线 —— 视口可能停在历史中间，必须先回最新。"""
        h = make_handler()
        h.ctx.align_md5 = "m0"
        h.ctx.align_tried = G.PROBE_MAX
        h.ctx.align_at = time.monotonic() - 5.0
        self.assertEqual(h._probe_decision("m1", "sig1"), "wait")
        self.assertTrue(h.ctx.want_bottom, "要下发 to_bottom")
        self.assertTrue(h.ctx.await_bottom, "等回底那一帧再重建基线")
        # 回底那一帧（手机端 reason=probe-bottom）才真正 commit
        h.ctx.frame_reason = "probe-bottom"
        self.assertEqual(h._probe_decision("m2", "sig2"), "give_up")

    def test_await_bottom_consumed_once(self):
        h = make_handler()
        h.ctx.await_bottom = True
        h.ctx.await_bottom_at = time.monotonic()
        h.ctx.align_md5 = "m0"
        # 回底路上来的普通帧：不许重建基线（会建在历史中间）
        self.assertEqual(h._probe_decision("m1", "sig1"), "wait")
        self.assertTrue(h.ctx.await_bottom)
        h.ctx.frame_reason = "probe-bottom"
        self.assertEqual(h._probe_decision("m2", "sig2"), "give_up")
        self.assertFalse(h.ctx.await_bottom)
        # 之后的锚点丢失恢复正常试探
        h.ctx.align_at = time.monotonic() - 5.0
        self.assertEqual(h._probe_decision("m3", "sig3"), "probe")

    def test_stale_state_also_goes_bottom(self):
        """试探状态过期 = 视口很可能不在最新处，同样先回底再重建。"""
        h = make_handler()
        h.ctx.align_at = time.monotonic() - (G.ALIGN_STALE + 1)
        self.assertEqual(h._probe_decision("m1", "sig1"), "wait")
        self.assertTrue(h.ctx.want_bottom)


class TestBudgetNotResetByFill(unittest.TestCase):
    """补全帧不许再偷偷把试探额度清零（今天「永远第 1 次」的直接原因）。"""

    def test_ingest_fill_does_not_touch_align_tried(self):
        src = Path(__file__).resolve().parents[1] / "bridge" / "sources" / "android_gateway.py"
        text = src.read_text(encoding="utf-8")
        i = text.index("def _ingest_fill")
        j = text.index("def ", i + 10)
        body = text[i:j]
        # 只看真正的代码行（注释里会引用这句话作说明）
        code = [ln.strip() for ln in body.splitlines()
                if ln.strip() and not ln.strip().startswith("#")]
        self.assertFalse(any(ln.startswith("self.ctx.align_tried = 0")
                             for ln in code),
                         "补全帧只清计数会留下 align_at/align_md5，导致日志永远"
                         "「第 1 次」、额度永远给不出来；要清就走 _reset_probe()")


class TestProbeProtocol(unittest.TestCase):
    """手机端要认的两条新指令，以及它们与 scroll 的分工。"""

    def test_phone_implements_probe_and_bottom(self):
        src = (Path(__file__).resolve().parents[1] / "android" / "src" / "com"
               / "hbdxm" / "ddwx" / "CollectorService.java").read_text(encoding="utf-8")
        self.assertIn("maybeProbeScroll", src)
        self.assertIn("maybeScrollBottom", src)
        # probe 是「只上翻不回位」：必须用 up=false 的那次 swipe，且**不许**
        # 出现「再反方向滑一次」的复位（那是 scroll 补全的做法）。
        i = src.index("private void maybeProbeScroll")
        j = src.index("private void maybeScrollBottom")
        body = src[i:j]
        self.assertIn("swipe(this, list, false)", body)
        self.assertNotIn("swipe(this, list2, true)", body,
                         "锚点试探一旦回位就变成原地打转（2026-10-29 真机）")

    def test_pc_sends_probe_not_scroll_for_anchor_loss(self):
        src = (Path(__file__).resolve().parents[1] / "bridge" / "sources"
               / "android_gateway.py").read_text(encoding="utf-8")
        self.assertIn('body["probe"] = self.ctx.want_probe', src)
        self.assertIn('body["to_bottom"] = 1', src)


if __name__ == "__main__":
    unittest.main()
