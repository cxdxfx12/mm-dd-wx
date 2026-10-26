"""拉起钉钉：`startActivity` 成功 ≠ 到了前台（2026-10-20 真机）。

## 现场

用户在 App 里点「立即巡检」，手机没有任何跳转动作，电脑侧收到：

    15:30:26 · 手动 · 失败：拉不起钉钉（可能被系统自启动管理拦了）

关键是：**手机屏幕上什么都没发生**。也就是说 `startActivity` 没抛异常，
意图发出去了，被系统静默丢掉了 —— 华为的「后台弹出界面」权限一关就是这样。

旧代码只要 `startActivity` 不抛就 `return true`，接着去读标题栏，
读到的是**上一个应用**的标题 → 后面的失败原因全跑偏（「读不到标题栏」
或「搜索里没找到」），真正的原因（根本没切过去）被埋掉。

## 修法

1. 拉完必须回读 `isDingTalkForeground`，没到就明确报「启动请求发出去了
   但前台仍不是钉钉」，并告诉用户去开哪个权限；
2. 兜底再试一次「从最近任务切回」—— 它不是启动而是切回，很多 ROM
   对这两种的拦截策略不同（注意：钉钉被杀掉时这条无效，属已知限制）。
"""
from __future__ import annotations

import unittest
from pathlib import Path


class TestLaunchVerified(unittest.TestCase):
    def setUp(self):
        self.src = (Path(__file__).resolve().parents[1] / "android" / "src"
                    / "com" / "hbdxm" / "ddwx" / "SelfHeal.java").read_text(
                        encoding="utf-8")

    def test_launch_is_verified_by_readback(self):
        body = self.src.split("if (!isDingTalkForeground(svc))", 1)[1]
        body = body.split("// ② 读标题栏", 1)[0]
        self.assertIn("isDingTalkForeground(svc)", body,
                      "拉完没回读确认，前台没变也会当作成功")
        self.assertIn("启动请求发出去了但前台仍不是钉钉", body,
                      "必须把「静默失败」和「拒绝启动」分开报出来")

    def test_failure_message_tells_user_what_to_do(self):
        """失败文案必须给出「下一步做什么」，而不是只说「失败了」。

        0.5.1 起提示词从「去开后台弹出界面」改成「去开应用启动管理」——
        前者已被真机证伪：权限开关全开也拦得住，所以不能继续让用户去开它。
        """
        body = self.src.split("if (!isDingTalkForeground(svc))", 1)[1]
        body = body.split("// ② 读标题栏", 1)[0]
        self.assertIn("应用启动管理", body)

    def test_recent_tasks_fallback_exists(self):
        self.assertIn("getRecentTasks", self.src)
        self.assertIn("tryStart", self.src)

    def test_settle_before_readback(self):
        """回读前必须给系统一点时间，否则必然读到旧应用。"""
        body = self.src.split("String why =", 1)[1].split("// ② 读标题栏", 1)[0]
        self.assertIn("LAUNCH_SETTLE_MS", body)


if __name__ == "__main__":
    unittest.main()
