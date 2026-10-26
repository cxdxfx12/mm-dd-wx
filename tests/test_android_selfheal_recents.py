"""拉不起钉钉时，必须有一条不依赖 startActivity 的路（2026-10-20 真机）。

## 现场

华为 PAL-AL00，用户把「后台弹出界面」「应用启动管理」三个开关**全开了**，
`startActivity` 依然抛异常：

    17:18:19 · 手动 · 失败：拉不起钉钉（系统拒绝了启动请求）

说明华为对「无障碍服务启动第三方应用」有**独立于那个权限开关**的拦截策略。
让用户继续去找权限已经没意义了 —— 该换通道。

## 解法

`AccessibilityService.performGlobalAction(GLOBAL_ACTION_RECENTS)` 是
**系统替无障碍服务执行**的，不经过「应用启动」检查。打开最近任务 → 点钉钉卡片，
这条路真机上有效。

⚠ 点卡片必须**按名字认**（content-desc/text 含「钉钉」），绝不点「最像的那个」：
点错等于把用户切到别的应用，比不动更糟。
"""
from __future__ import annotations

import unittest
from pathlib import Path


class TestRecentsFallback(unittest.TestCase):
    def setUp(self):
        self.src = (Path(__file__).resolve().parents[1] / "android" / "src"
                    / "com" / "hbdxm" / "ddwx" / "SelfHeal.java").read_text(
                        encoding="utf-8")

    def test_uses_global_action_recents(self):
        self.assertIn("GLOBAL_ACTION_RECENTS", self.src,
                      "没有这条兜底，华为机型永远拉不起钉钉")

    def test_recents_is_tried_after_direct_launch(self):
        """顺序：先直接启动（便宜、能恢复上次页面），失败再走最近任务。"""
        body = self.src.split("if (!isDingTalkForeground(svc))", 1)[1]
        body = body.split("// ② 读标题栏", 1)[0]
        i_launch = body.find("launchDingTalk(svc)")
        i_recents = body.find("viaRecents(svc)")
        self.assertGreater(i_launch, 0)
        self.assertGreater(i_recents, 0)
        self.assertLess(i_launch, i_recents)

    def test_card_matched_by_name_only(self):
        body = self.src.split("private static AccessibilityNodeInfo findDingTalkCard", 1)[1]
        body = body.split("// ------------------------------------------------------------", 1)[0]
        self.assertIn("钉钉", body)
        self.assertNotIn("get(0)", body, "不许兜底点第一张卡 —— 会切到别的应用")

    def test_verify_after_recents(self):
        body = self.src.split("private static boolean viaRecents", 1)[1]
        body = body.split("private static AccessibilityNodeInfo findDingTalkCard", 1)[0]
        self.assertIn("isDingTalkForeground(svc)", body,
                      "点完卡片必须回读确认，不能假设点中了")


if __name__ == "__main__":
    unittest.main()
