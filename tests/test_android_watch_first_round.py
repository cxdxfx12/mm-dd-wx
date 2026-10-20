"""采集端的两个行为：巡检首轮时机、以及「读不到画面」必须让电脑侧知道。

背景（2026-10-20 真机）
    用户要求「程序自动定位钉钉并切换到相应群聊」。手机装了真 0.4.1、心跳正常、
    版本号也报上来了，但网关一直显示 ``screen.received=false（还没收到任何画面）``。

    查下来是两件事叠加：

    ① ``startWatch()`` 的**初始延迟也写成了一个间隔**（10 分钟）。手机是专用采集机，
       服务重开时钉钉往往不在目标群 —— 那 10 分钟里一帧都抓不到。用户定的改法是
       「连上后 5 秒就跑首轮，之后每 10 分钟」。

    ② ``doCapture()`` 在三种「读不到」的情况下**静默 return**（拿不到根节点 /
       前台不是钉钉 / 读到 0 条文本）。电脑侧于是只看到「还没收到任何画面」，
       而这个状态同时对应「钉钉不在前台」「无障碍没开」「App 被杀」「网络断了」
       四种完全不同的情况，排查只能靠猜。现在每次「瞎了」都发一个极小的诊断帧。

    这里测的是**源码里的约束**，不是跑真机：Java 代码没法在 Python 单测里执行，
    但这两条都是「写错就会静默失效」的规则，值得用字符串断言钉住。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
SRC = BASE_DIR / "android" / "src" / "com" / "hbdxm" / "ddwx" / "CollectorService.java"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


class TestWatchFirstRound(unittest.TestCase):
    """巡检首轮必须很快跑，不能等满一个间隔。"""

    def test_first_delay_constant_is_short(self):
        m = re.search(r"FIRST_WATCH_DELAY_S\s*=\s*(\d+)L", _src())
        self.assertIsNotNone(m, "找不到 FIRST_WATCH_DELAY_S 常量")
        delay = int(m.group(1))
        self.assertLessEqual(
            delay, 30,
            f"首轮延迟是 {delay} 秒，太久了。这台手机是专用采集机，"
            f"服务重开时钉钉常常不在目标群，等太久这期间的消息全丢。"
            f"（用户 2026-10-20 定：连上就接管）")

    def test_scheduler_uses_first_delay(self):
        """scheduleWithFixedDelay 的第 2 个参数必须是 FIRST_WATCH_DELAY_S，
        不能又写成 minutes * 60 —— 那是这次事故的原形。"""
        body = _src()
        m = re.search(
            r"scheduleWithFixedDelay\s*\(\s*new\s+Runnable\s*\(\s*\)\s*\{(.*?)\}\s*\}\s*,\s*([^,]+),\s*([^,]+),\s*TimeUnit\.SECONDS\s*\)",
            body, re.S)
        self.assertIsNotNone(m, "没找到 startWatch 里的 scheduleWithFixedDelay 调用")
        initial = m.group(2).strip()
        period = m.group(3).strip()
        self.assertEqual(
            initial, "FIRST_WATCH_DELAY_S",
            f"初始延迟是 {initial!r}，应当是 FIRST_WATCH_DELAY_S。"
            f"写成一个间隔 = 服务重开后前 10 分钟完全抓不到东西")
        self.assertIn("minutes", period, "间隔应当仍按配置的分钟数")


class TestBlindReport(unittest.TestCase):
    """读不到画面时必须留下可查的痕迹。"""

    def test_no_silent_blank_returns(self):
        """doCapture 里那三处「读不到」都必须调 reportBlind，不能只赋值 lastResult。"""
        body = _src()
        start = body.index("private void doCapture(")
        end = body.index("private void reportBlind(", start) if "private void reportBlind(" in body[start:] else len(body)
        # doCapture 可能在 reportBlind 之后定义，取两种情况的并集
        seg = body[start:] if "reportBlind" not in body[start:end:1] else body[start:end]
        for reason in ('"no-root"', '"not-dingtalk:"', '"empty-texts"'):
            self.assertIn(reason, body, f"reportBlind 没有覆盖 {reason}")
        # 三个「先赋值 lastResult 再 return」的点，后面都应紧跟 reportBlind
        for prev in ("拿不到窗口根节点", "不是钉钉，跳过", "读到 0 条文本"):
            idx = body.index(prev)
            tail = body[idx:idx + 300]
            self.assertIn(
                "reportBlind(", tail,
                f"「{prev}」之后没有 reportBlind —— 电脑侧只会看到"
                f"「还没收到任何画面」，分不清是哪一种故障")

    def test_diag_package_is_not_dingtalk(self):
        """诊断帧的包名必须**不是**钉钉的，否则会被网关当成业务画面解析进基线。"""
        body = _src()
        m = re.search(r'<diag package=\\?"([^"\\]+)', body)
        self.assertIsNotNone(m, "找不到诊断帧的 package 字段")
        pkg = m.group(1)
        self.assertTrue(pkg.startswith("com.hbdxm.ddwx"),
                        f"诊断帧包名 {pkg!r} 不像本程序的包名")
        self.assertNotIn("alibaba", pkg.lower(),
                         "诊断帧包名不能像钉钉，否则网关会当业务画面解析")
        self.assertTrue(pkg.endswith(".diag"),
                        f"诊断帧包名应以 .diag 结尾以示区分，实际 {pkg!r}")

    def test_diag_is_throttled(self):
        """抓取是几百毫秒一次，不节流会把电脑侧刷满。"""
        m = re.search(r"BLIND_REPORT_MS\s*=\s*(\d+)L", _src())
        self.assertIsNotNone(m, "找不到 BLIND_REPORT_MS")
        self.assertGreaterEqual(int(m.group(1)), 30000,
                                "诊断帧节流时间太短，会刷屏")

    def test_diag_does_not_touch_sent_md5(self):
        """诊断帧不能写 sentMd5 —— 否则用户切回钉钉后第一帧会被当成「未变化」跳过。"""
        body = _src()
        start = body.index("private void reportBlind(")
        seg = body[start:start + 1800]
        self.assertNotIn("sentMd5 =", seg,
                         "reportBlind 里改了 sentMd5 —— 切回钉钉后的第一帧会被跳过")


if __name__ == "__main__":
    unittest.main()
