"""手机巡检的两个真机事故，用源码约束锁住。

背景（2026-10-20，都是实测撞到的，不是推测）：

1. **停在网页上就再也回不去**
   用户装完 App，钉钉打开了内置浏览器的「喵喵采集端 · 安装」页
   （节点：``webview_root`` / ``webview_frame_layout``，整棵树只有 3 个文本）。
   巡检从这一页出发找不到会话列表、找不到搜索框，报出来的是
   「搜索里没找到『群名』」—— 离真正原因（要先退回聊天页）很远。
   现在 ensureGroup 里多一步：认出 webview 就先按返回键退出去。

2. **watch_runs 重启归零，看起来像「定时器没转」**
   用户装完新包，在「服务刚重开、首轮巡检（连上 5 秒）还没到」的窗口里查
   ``/ping``，看到 ``watch_runs: 0``，判定成「定时器根本没跑」。
   现在上报的是 Prefs 里持久累计的值，跨重启单调递增。

为什么用源码字符串断言：这两条都依赖真机界面（WebView 容器 id、服务重启时序），
在离线测试里没法复现。锁住「关键实现还在」能有效防止以后重构时把它们删掉。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ANDROID = Path(__file__).resolve().parents[1] / "android"
SRC = ANDROID / "src" / "com" / "hbdxm" / "ddwx"


def read(name: str) -> str:
    return (SRC / name).read_text(encoding="utf-8")


class TestWebviewEscape(unittest.TestCase):
    """停在网页页面上时，巡检要先退出去，而不是直接去搜索。"""

    def setUp(self):
        self.selfheal = read("SelfHeal.java")

    def test_blocking_page_detects_webview_container(self):
        self.assertIn("blockingPage(", self.selfheal)
        # 判据必须是布局 id / 类名，不能是标题文字（标题是业务内容，会变）
        body = self.selfheal.split("private static String blockingPage", 1)[1]
        body = body.split("private static boolean back(", 1)[0]
        self.assertTrue("getViewIdResourceName" in body or "WebView" in body,
                        "blockingPage 必须靠布局 id 或 WebView 类名判定")

    def test_ensure_group_escapes_before_search(self):
        """顺序必须对：先退网页，再搜索。反了就等于没修。"""
        ensure = self.selfheal.split("static Result ensureGroup", 1)[1]
        ensure = ensure.split("// ------------------------------------------------------------ 前台与拉起", 1)[0]
        i_escape = ensure.find("blockingPage(svc)")
        i_search = ensure.find("searchAndOpen(svc, group)")
        self.assertGreater(i_escape, 0, "ensureGroup 里没有退出网页这一步")
        self.assertGreater(i_search, 0, "ensureGroup 里没有搜索")
        self.assertLess(i_escape, i_search, "必须先退网页再搜索")

    def test_escape_is_bounded(self):
        """返回键最多按几下 —— 按太多会把钉钉退到桌面，变成另一类故障。"""
        ensure = self.selfheal.split("blockingPage(svc)", 1)[1]
        ensure = ensure.split("searchAndOpen(svc, group)", 1)[0]
        m = re.search(r"for \(int i = 0; i < (\d+); i\+\+\)", ensure)
        self.assertIsNotNone(m, "没有找到有界的 for 循环")
        self.assertLessEqual(int(m.group(1)), 3,
                             "返回键按太多次会把钉钉退到桌面")

    def test_back_uses_global_action(self):
        body = self.selfheal.split("private static boolean back(", 1)[1]
        self.assertIn("GLOBAL_ACTION_BACK", body)


class TestWatchRunsPersist(unittest.TestCase):
    """上报的轮次必须是持久值。"""

    def setUp(self):
        self.svc = read("CollectorService.java")
        self.prefs = read("Prefs.java")

    def test_counter_is_persisted(self):
        self.assertIn("K_WATCH_RUNS", self.prefs)
        self.assertIn("static long watchRuns(Context c)", self.prefs)
        self.assertIn("static long bumpWatchRuns(Context c)", self.prefs)

    def test_do_watch_bumps_persistent_counter(self):
        body = self.svc.split("private void doWatch(String why)", 1)[1]
        body = body.split("// ------------------------------------------------------------------ 心跳", 1)[0]
        self.assertIn("Prefs.bumpWatchRuns(this)", body,
                      "doWatch 必须把持久计数器 +1，否则重启后又归零")

    def test_heartbeat_reports_persistent_value(self):
        body = self.svc.split("private void heartbeatOnce()", 1)[1]
        body = body.split("// ------------------------------------------------------------------ 抓取", 1)[0]
        self.assertIn("watchTotalRuns()", body,
                      "心跳上报读内存计数器，重启后 /ping 会显示 0")
        self.assertNotRegex(body, r"lastWatch,\s*watchRuns\)",
                            "还在传内存里的 watchRuns")


if __name__ == "__main__":
    unittest.main()
