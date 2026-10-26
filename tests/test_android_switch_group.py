"""电脑端「切到钉钉群」按钮 → 手机切群的指令通路测试。

这条链路是**跨进程 + 跨设备**的，任何一环断了用户看到的现象都一模一样：
「点了按钮，什么都没发生」。所以这里要钉死四件事：

  1. ``POST /watch-now`` 真的把指令挂上去（序号 +1），不是只回个 ok。
  2. 指令挂在 **/ping（心跳）** 上一次性取走 —— 不能每帧都发，否则用户
     回个微信消息就被程序抢走前台，每 60 秒一次。
  3. 抓取帧（/screen）只带 ``watch_now_pending`` 提醒，不带 ``watch_now``
     —— 它几百毫秒一次，用来把手机的 60 秒心跳提前戳醒。
  4. 没配目标群时**不挂指令**，而是明确回一句话：手机不知道该切到哪个群，
     挂上去只会让它在搜索里空转。

第 2、3 条为什么必须分开：合成一条的话，要么切群指令每帧都下发（疯狂抢
前台），要么 60 秒才响应一次（用户以为按钮坏了）。两者都是真机踩过的坑。
"""
from __future__ import annotations

import json
import sys
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.sources.android_gateway import AndroidGatewaySource  # noqa: E402

from tests.test_android_gateway import FRAME_1, TOKEN, free_port, make_cfg  # noqa: E402


class _Base(unittest.TestCase):
    chat_names = ("数字质控",)

    def setUp(self):
        self.port = free_port()
        cfg = make_cfg(self.port, TOKEN, chat_names=list(self.chat_names))
        self.source = AndroidGatewaySource(cfg)
        self.thread = threading_thread(self.source)
        if not self.source.wait_ready(5.0):
            self.fail("网关没能在 5 秒内就绪")

    def tearDown(self):
        self.source.stop()
        self.thread.join(timeout=5.0)

    # ---- HTTP 小工具 ----

    def _post(self, path: str, body: str = "{}") -> dict:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=body.encode("utf-8"), method="POST")
        req.add_header("X-Bridge-Token", TOKEN)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def queue_switch(self) -> dict:
        return self._post("/watch-now")

    def heartbeat(self) -> dict:
        return self._post("/ping")

    def frame(self) -> dict:
        return self._post("/screen", FRAME_1)


def threading_thread(source):
    import threading
    t = threading.Thread(target=source.run, args=(lambda m: None,), daemon=True)
    t.start()
    return t


class TestQueueCommand(_Base):

    def test_queue_reports_ok_and_target_group(self):
        r = self.queue_switch()
        self.assertTrue(r.get("ok"))
        self.assertEqual(r.get("chat_name"), "数字质控")
        self.assertTrue(r.get("pending"))

    def test_queue_twice_increments_seq(self):
        """连点两下，第二下也必须生效。

        用布尔而不是序号的话，第一次点完、指令还没取走时再点一次会被吃掉 ——
        用户看到的就是「点了没反应」。
        """
        ctx = self.source._ctx
        before = ctx.watch_now_seq
        self.queue_switch()
        self.queue_switch()
        self.assertEqual(ctx.watch_now_seq, before + 2)


class TestHeartbeatCarriesIt(_Base):

    def test_heartbeat_delivers_once(self):
        """心跳里带 watch_now=1，且**只带一次**。

        每次都带的话，手机每 60 秒被切一次群 —— 用户回个消息就被抢前台，
        这正是巡检要「定时而不是立刻」的同一条理由。
        """
        self.queue_switch()
        first = self.heartbeat()
        self.assertEqual(first.get("watch_now"), 1)
        second = self.heartbeat()
        self.assertNotIn("watch_now", second)

    def test_no_command_means_no_field(self):
        """没点按钮时心跳里不能有这个字段 —— 否则手机每次心跳都跑一轮巡检。"""
        body = self.heartbeat()
        self.assertNotIn("watch_now", body)

    def test_second_click_after_served_delivers_again(self):
        self.queue_switch()
        self.assertEqual(self.heartbeat().get("watch_now"), 1)
        self.queue_switch()
        self.assertEqual(self.heartbeat().get("watch_now"), 1)


class TestScreenOnlyPokes(_Base):
    """抓取帧只负责「快」，不负责「取走」。"""

    def test_frame_sets_pending_flag(self):
        self.queue_switch()
        body = self.frame()
        self.assertEqual(body.get("watch_now_pending"), 1)
        # 关键：抓取帧**不能**带 watch_now，否则手机在这里就执行了，
        # 而抓取帧几百毫秒一次 —— 等于每帧都切群。
        self.assertNotIn("watch_now", body)

    def test_pending_flag_clears_after_heartbeat(self):
        """指令被心跳取走后，抓取帧就不该再催了。

        ⚠ 顺序不能反：必须「挂指令 → 心跳取走 → 再看帧」。
        先看帧再心跳的话，那时指令还在队列里，标记本来就该在 ——
        断言会误报，而且掩盖真正的问题（取走之后还在催）。
        """
        self.queue_switch()
        self.assertEqual(self.frame().get("watch_now_pending"), 1)   # 还没取走
        self.heartbeat()                                             # 取走
        self.assertNotIn("watch_now_pending", self.frame())           # 不再催

    def test_no_flag_when_nothing_queued(self):
        self.assertNotIn("watch_now_pending", self.frame())


class TestNoGroupConfigured(unittest.TestCase):
    """没配群名时不挂指令，而是明确说原因。"""

    def setUp(self):
        self.port = free_port()
        cfg = make_cfg(self.port, TOKEN, chat_names=[])
        self.source = AndroidGatewaySource(cfg)
        self.thread = threading_thread(self.source)
        if not self.source.wait_ready(5.0):
            self.fail("网关没能在 5 秒内就绪")

    def tearDown(self):
        self.source.stop()
        self.thread.join(timeout=5.0)

    def test_refuses_and_explains(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/watch-now", data=b"{}", method="POST")
        req.add_header("X-Bridge-Token", TOKEN)
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        self.assertFalse(body.get("pending"))
        self.assertIn("chat_names", body.get("message", ""))
        # 没挂上指令 = 心跳里不该出现字段
        self.assertEqual(self.source._ctx.watch_now_seq, 0)


class TestProbeSurvivesTheTrip(_Base):
    """巡检失败现场（X-Ddwx-Probe）要能送到电脑侧并出现在 /ping 里。

    为什么值得钉死：失败原因常常只有一句「读不到钉钉标题栏」，而它背后
    至少有三种情况（钉钉被拉到桌面前 / 停在没标题的页 / 根本不在钉钉里）。
    现场快照是唯一能分开它们的证据，而它只能搭 HTTP 头 —— 中文必须编码，
    编错了就会被 HttpURLConnection 拒掉，整条心跳跟着变成「失败」，
    于是「巡检看不出来」伪装成「手机掉线」。这是真机上出现过的误导。
    """

    def ping_with_probe(self, probe: str) -> dict:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/ping", data=b"{}", method="POST")
        req.add_header("X-Bridge-Token", TOKEN)
        req.add_header("Content-Type", "application/json")
        # 和手机上 Uploader.encodeHeader 一致：URL 编码，只留 ASCII
        req.add_header("X-Ddwx-Probe", urllib.parse.quote(probe, safe=""))
        req.add_header("X-Ddwx-Watch", urllib.parse.quote("失败：读不到标题栏", safe=""))
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_chinese_probe_roundtrips(self):
        quote_body = self.ping_with_probe("pkg=com.alibaba.android.rimet · title=空")
        self.assertIn("title=空", quote_body.get("probe", ""))
        # 顺带确认没把巡检结果挤掉：两个头各归各的
        self.assertIn("读不到标题栏", quote_body.get("watch", ""))

    def test_probe_absent_when_never_failed(self):
        """没失败过就不该有这个键的值 —— 留给界面判断「要不要显示现场」。"""
        self.assertEqual(self.heartbeat().get("probe"), "")

    def test_old_behaviour_would_be_empty(self):
        """老实现的 /ping 里根本没有 probe 字段，界面就永远只能显示一句结论。"""
        body = self.ping_with_probe("pkg=x")
        self.assertIn("probe", body)


class TestOldBehaviourWouldFail(_Base):
    """让「老实现」在这一组断言下明确失败。

    老实现（没有 watch-now 通路）的表现是：
      · POST /watch-now → 404
      · /ping 回包里永远没有 watch_now
    这两条只要有一条还成立，就是链路没接通。
    """

    def test_endpoint_exists(self):
        # 老实现这里会抛 HTTPError 404
        try:
            self.queue_switch()
        except urllib.error.HTTPError as exc:      # pragma: no cover
            self.fail(f"/watch-now 不存在（HTTP {exc.code}）—— 按钮点了会是死路")

    def test_ping_field_appears_after_queue(self):
        # 老实现的 /ping 里永远没有 watch_now
        self.assertNotIn("watch_now", self.heartbeat())
        self.queue_switch()
        self.assertEqual(self.heartbeat().get("watch_now"), 1)


if __name__ == "__main__":
    unittest.main()
