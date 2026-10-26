"""「立即查找要转发的群」按钮必须真的接到实现上（2026-10-20）。

背景两条，都是真机踩出来的：

1. `CollectorService.watchNow()` 早就写好了，但**界面上没有任何按钮调用它** ——
   于是用户想当场验证「群名配对没配对」时，只能干等一个间隔（最长 30 分钟）。
   这种事光看 Java 编译通过是发现不了的：方法没人调也是合法代码。

2. 后来按钮接上了，却仍被 `watchMinutes <= 0` 挡在门外。
   用户的心智是「我点一下，它就该去找群」，**跟定时巡检开没开无关**。
   现在拆成两个按钮、分两条路径，且 findGroupNow() 不判间隔。

所以这里锁四件事：
  1. 两个按钮的 id 与字符串都在；
  2. 新按钮真的调了 findGroupNow()，且**没有**被 watchMinutes 拦住；
  3. 旧按钮（立即巡检）也不许再被 watchMinutes 拦住；
  4. 服务端两个方法都存在，且共用同一把防重入锁（否则两个线程会互相点掉页面）。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ANDROID = Path(__file__).resolve().parents[1] / "android"


def read(rel: str) -> str:
    return (ANDROID / rel).read_text(encoding="utf-8")


def handler_body(activity: str, btn_id: str) -> str:
    """取出某个按钮 id 那段 onClick 处理代码（到下一个按钮绑定为止）。"""
    tail = activity.split(btn_id, 1)[1]
    # 下一个 findViewById 就是下一段；找不到就取到文件末尾
    nxt = tail.find("findViewById(R.id.btn_")
    return tail if nxt < 0 else tail[:nxt]


class TestFindGroupButton(unittest.TestCase):
    """用户点名要的那个按钮：点一下就去找钉钉要转发的群。"""

    def setUp(self):
        self.layout = read("res/layout/activity_main.xml")
        self.strings = read("res/values/strings.xml")
        self.activity = read("src/com/hbdxm/ddwx/MainActivity.java")
        self.service = read("src/com/hbdxm/ddwx/CollectorService.java")

    def test_layout_has_button(self):
        self.assertIn('android:id="@+id/btn_find_group"', self.layout)
        self.assertIn("@string/btn_find_group", self.layout)

    def test_string_is_defined(self):
        m = re.search(r'<string name="btn_find_group">([^<]*)</string>', self.strings)
        self.assertIsNotNone(m, "btn_find_group 没有文案")
        # 必须说清它干什么，别写成「确定」这种看不出来的
        self.assertIn("群", m.group(1))

    def test_button_sits_above_the_old_one(self):
        """新按钮要在「立即巡检」上面，用户不用滚着找。"""
        self.assertLess(
            self.layout.index("@+id/btn_find_group"),
            self.layout.index("@+id/btn_watch"),
            "新按钮排在「立即巡检」下面了，得往下滚才看得见",
        )

    def test_click_handler_calls_find_group_now(self):
        body = handler_body(self.activity, "R.id.btn_find_group")
        self.assertIn("findGroupNow()", body, "按钮点了却没调起找群")

    def test_not_blocked_by_watch_interval(self):
        """巡检关着（间隔 0）也必须能用 —— 这正是旧实现的老毛病。

        2026-10-20：按钮借用了 watchNow，而 watchNow 上游有
        `watchMinutes <= 0` 的判断，于是「巡检关闭」时点按钮完全没反应。
        用户当时的话是「所有开关都已打开，按巡检还是不跳转」。
        """
        body = handler_body(self.activity, "R.id.btn_find_group")
        self.assertNotRegex(
            body, r"watchMinutes\s*<=?\s*0",
            "找群按钮又被巡检间隔拦住了：关闭巡检时用户就验证不了群名",
        )

    def test_service_method_exists(self):
        self.assertRegex(self.service, r"public\s+void\s+findGroupNow\s*\(")


class TestWatchButtonWired(unittest.TestCase):
    """原来的「立即巡检一次」按钮。"""

    def setUp(self):
        self.layout = read("res/layout/activity_main.xml")
        self.strings = read("res/values/strings.xml")
        self.activity = read("src/com/hbdxm/ddwx/MainActivity.java")

    def test_layout_has_button(self):
        self.assertIn('android:id="@+id/btn_watch"', self.layout)
        self.assertIn("@string/btn_watch", self.layout)

    def test_string_is_defined(self):
        self.assertIn('name="btn_watch"', self.strings)

    def test_click_handler_reaches_a_find_group_path(self):
        body = handler_body(self.activity, "R.id.btn_watch")
        self.assertRegex(
            body, r"(watchNow|findGroupNow)\(\)",
            "按钮点了却没调起巡检",
        )

    def test_no_longer_blocked_by_interval(self):
        """旧实现：巡检关着就只弹一句提示、不做事。

        现在改成两个按钮各管一件事，这个按钮也不该再被间隔拦住 ——
        用户要的是「点一下它就去把钉钉切到群里」，不是「点一下它告诉我
        定时巡检是关的」。
        """
        body = handler_body(self.activity, "R.id.btn_watch")
        self.assertNotRegex(
            body, r"watchMinutes\s*<=?\s*0",
            "巡检关着时点了会静默无反应，必须真的去切群",
        )


class TestServiceEntryPoints(unittest.TestCase):
    """两条入口必须共用防重入锁，否则会互相点掉对方的页面。"""

    def setUp(self):
        self.service = read("src/com/hbdxm/ddwx/CollectorService.java")

    def test_both_methods_exist(self):
        self.assertRegex(self.service, r"public\s+void\s+watchNow\s*\(")
        self.assertRegex(self.service, r"public\s+void\s+findGroupNow\s*\(")

    def test_both_go_through_run_watch_now(self):
        """都走 runWatchNow → 同一把 watching 锁 + 同一个执行线程。

        如果 findGroupNow 自己另起一条线程，两个按钮同时点就会两路
        dispatchGesture 打架：搜索框被写两遍、结果行被点两下，
        表现是「有时能进去有时进不去」。
        """
        for name in ("watchNow", "findGroupNow"):
            tail = self.service.split(f"public void {name}(", 1)[1]
            body = tail[: tail.index("}")]
            self.assertIn("runWatchNow(", body, f"{name} 没走统一入口")


if __name__ == "__main__":
    unittest.main()
