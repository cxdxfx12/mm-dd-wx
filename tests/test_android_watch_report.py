"""巡检状态必须能从心跳里看到（android_gateway 的 /ping）。

背景（2026-10-20 真机）
    用户装完 0.4.2，钉钉停在内置浏览器的下载页（WebView）里没被切回群。
    查的时候发现：``lastWatch`` / ``watchRuns`` / ``watchHeals`` 这几个字段
    在手机上只是 ``public static volatile``，**从来没上报过**。
    于是「巡检到底跑没跑、结果是什么」电脑侧一点痕迹都没有 ——
    和「读不到画面静默 return」是同一类毛病：失败了，但没人知道。

    ``watch_runs`` 不涨尤其能说明问题：定时器根本没在转
    （APK 没装对、服务没连上、定时器被重启打回）。

编码要点：HTTP 头只能是 ISO-8859-1，巡结果里带中文群名，手机端用
URLEncoder 编码，网关解回来。**解不开也必须能继续**——诊断信息不该让心跳 500，
否则「巡检失败」会被升级成「手机失联」，一个假故障变两个。

    python -m unittest tests.test_android_watch_report -v
"""
from __future__ import annotations

import json
import sys
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from tests.test_android_gateway import TOKEN, GatewayCase  # noqa: E402


class WatchCase(GatewayCase):

    def op(self):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def post_ping(self, watch=None, runs=None, ver="0.4.2"):
        headers = {"X-Bridge-Token": self.token, "X-Ddwx-Device": "PAL-AL00",
                   "X-Ddwx-Ver": ver, "Content-Type": "application/json"}
        if watch is not None:
            headers["X-Ddwx-Watch"] = urllib.parse.quote(watch, safe="")
        if runs is not None:
            headers["X-Ddwx-Watch-Runs"] = str(runs)
        req = urllib.request.Request(self.url("/ping"), data=b"{}",
                                     headers=headers, method="POST")
        self.op().open(req, timeout=6).read()

    def get_ping(self) -> dict:
        req = urllib.request.Request(self.url("/ping"),
                                     headers={"X-Bridge-Token": self.token})
        return json.loads(self.op().open(req, timeout=6).read().decode())


class TestWatchState(WatchCase):

    def test_watch_text_roundtrips_chinese(self):
        """中文群名要能原样到达 —— 否则看到的是乱码，等于没报。"""
        msg = "12:52:51 · 定时 · 成功：已在目标群「【数字质控】浙江杭州萧山区大江东公司」"
        self.post_ping(watch=msg, runs=1)
        self.assertEqual(self.get_ping().get("watch"), msg)

    def test_watch_runs_counts_up(self):
        """runs 是判断「定时器真的在转吗」的唯一证据。"""
        self.post_ping(watch="第一次", runs=1)
        self.assertEqual(self.get_ping().get("watch_runs"), 1)
        self.post_ping(watch="第二次", runs=2)
        self.assertEqual(self.get_ping().get("watch_runs"), 2)

    def test_never_watched_says_so(self):
        """从没巡检过时要明确说，不能给个空串让人以为是没读到。"""
        self.post_ping(ver="0.4.2")            # 不带 watch 头
        self.assertEqual(self.get_ping().get("watch"), "(还没巡检过)")

    def test_empty_watch_is_a_real_value(self):
        """空串是「刚启动、还没巡检」，不能被当成「没带」而把旧值留着。"""
        self.post_ping(watch="上一轮的结果", runs=3)
        self.post_ping(watch="", runs=3)
        self.assertEqual(self.get_ping().get("watch"), "(还没巡检过)",
                         "手机端明确报空串时，网关不该继续显示上一轮的旧结果")

    def test_broken_encoding_does_not_kill_heartbeat(self):
        """编码坏了也只是这一句没有，心跳必须照常 ——
        否则「巡检失败」会变成「心跳失败」，「手机失联」的假告警就来了。"""
        headers = {"X-Bridge-Token": self.token, "X-Ddwx-Device": "PAL-AL00",
                   "X-Ddwx-Watch": "%zz%", "Content-Type": "application/json"}
        req = urllib.request.Request(self.url("/ping"), data=b"{}",
                                     headers=headers, method="POST")
        resp = self.op().open(req, timeout=6)
        self.assertEqual(resp.status, 200)
        d = self.get_ping()
        self.assertTrue(d.get("online"), "心跳没被记上")
        self.assertEqual(d.get("app_ver"), "(旧版未上报)")  # 本次没带 ver，保留旧值

    def test_version_and_watch_together(self):
        """两个字段同一次心跳里都要生效（别只传上一个）。"""
        self.post_ping(watch="跑完了", runs=7, ver="0.4.2")
        d = self.get_ping()
        self.assertEqual(d.get("app_ver"), "0.4.2")
        self.assertEqual(d.get("watch"), "跑完了")
        self.assertEqual(d.get("watch_runs"), 7)


if __name__ == "__main__":
    unittest.main()


class TestWatchHeaderEvidence(GatewayCase):
    """「心跳里带没带巡检状态」必须能从 /ping 一眼看出来。

    这一项是被一次真实排查逼出来的（2026-10-20）：`/ping` 里根本找不到
    ``watch`` 键时，有两种完全不同的原因 —— 手机端没发这个头，或者发了
    但巡检一次都没跑。光看「字段缺失」分不开，只能跑去拿手机看设置页。
    """

    def ping(self) -> dict:
        import json
        import urllib.request
        req = urllib.request.Request(self.url("/ping", token=TOKEN))
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_never_seen_before_any_heartbeat(self):
        self.assertEqual("never", self.ping().get("watch_header"))

    def test_old_apk_without_header_says_no(self):
        import json
        import urllib.request
        req = urllib.request.Request(
            self.url("/ping", token=TOKEN), data=b"{}", method="POST",
            headers={"X-Ddwx-Ver": "0.4.2", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
        self.assertEqual("no", self.ping().get("watch_header"))

    def test_heartbeat_with_header_says_yes(self):
        import urllib.request
        from urllib.parse import quote
        req = urllib.request.Request(
            self.url("/ping", token=TOKEN), data=b"{}", method="POST",
            headers={"X-Ddwx-Ver": "0.4.6",
                     "X-Ddwx-Watch": quote("13:22:05 · 定时 · 正常：已切回目标群"),
                     "X-Ddwx-Watch-Runs": "3",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
        d = self.ping()
        self.assertEqual("yes", d.get("watch_header"))
        self.assertEqual(3, d.get("watch_runs"))
        self.assertIn("已切回目标群", d.get("watch") or "")

    def test_empty_watch_is_still_yes(self):
        """带了但为空（刚启动没巡检过）—— 仍然是「带了」，别记成 no。"""
        import urllib.request
        req = urllib.request.Request(
            self.url("/ping", token=TOKEN), data=b"{}", method="POST",
            headers={"X-Ddwx-Watch": "", "X-Ddwx-Watch-Runs": "0",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
        d = self.ping()
        self.assertEqual("yes", d.get("watch_header"))
        self.assertEqual("(还没巡检过)", d.get("watch"))
