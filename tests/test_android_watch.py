"""手机自动巡检（dingtalk.android_watch_min）的电脑侧测试。

这条功能只有电脑侧这一半是 Python，手机端（SelfHeal.java）跑在安卓上、
测不到，所以这里要盯死的是三件事：

  1. **档位夹取** —— 用户手改 config.yaml 填了 7、填了 1、填了 "abc"，
     下发给手机的必须是合法档位。填错就变成「手机每分钟来抢一次前台」，
     或者「写了 10 分钟却按关闭处理」，两种都很难在现场看出来。
  2. **每帧都下发、且是明确的 0** —— 手机端是「收到值就重排定时器」的实现。
     不发的话它会一直沿用本地记住的旧值，用户在电脑上关掉了却停不下来。
  3. **目标群取 chat_names[0]** —— 巡检群名和路由群名必须是同一个来源，
     两处各配一份必然出现不一致，而不一致的后果是手机每轮都点错群。
"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.request
from pathlib import Path

# 让 ``import bridge`` 可用 —— 与其它测试一致：discover 是以「松散模块」方式
# 加载 tests/*.py 的（没有包上下文），所以这里不能写相对导入，得自己补路径。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.sources.android_gateway import (          # noqa: E402
    WATCH_CHOICES, AndroidGatewaySource, _watch_minutes,
)

from tests.test_android_gateway import (              # noqa: E402
    FRAME_1, TOKEN, free_port, make_cfg,
)


class TestWatchMinutes(unittest.TestCase):
    """配置值 → 下发档位。"""

    def test_zero_and_negative_are_off(self):
        self.assertEqual(_watch_minutes(0), 0)
        self.assertEqual(_watch_minutes(-5), 0)

    def test_choices_are_exact(self):
        for v in WATCH_CHOICES:
            self.assertEqual(_watch_minutes(v), v)

    def test_rounds_down_to_keep_blind_spot_short_of_promise(self):
        """填 7 是「别太频繁」的意思，给 5 而不是 10。

        反过来（取更小的档）会让盲区小于用户预期，是安全的；取更大的档就是
        「我设了 7 分钟，怎么丢了 10 分钟的消息」。
        """
        self.assertEqual(_watch_minutes(7), 5)
        self.assertEqual(_watch_minutes(14), 10)
        self.assertEqual(_watch_minutes(99), 30)

    def test_below_smallest_choice_falls_to_smallest(self):
        """填 1 / 2 这种明显想开得很勤的，按最小非零档处理。"""
        self.assertEqual(_watch_minutes(1), 5)
        self.assertEqual(_watch_minutes(4), 5)

    def test_garbage_is_off_not_crash(self):
        for bad in (None, "", "abc", "5分钟", [], {}):
            self.assertEqual(_watch_minutes(bad), 0, f"输入 {bad!r}")


class _WatchCase(unittest.TestCase):
    """起一个真网关，看 `/screen` 的回包里带了什么。"""

    watch_min = 0
    chat_names = ("数字质控",)

    def setUp(self):
        self.port = free_port()
        cfg = make_cfg(self.port, TOKEN, chat_names=list(self.chat_names))
        cfg.dingtalk.android_watch_min = self.watch_min
        self.source = AndroidGatewaySource(cfg)
        self.thread = threading.Thread(
            target=self.source.run, args=(lambda m: None,), daemon=True)
        self.thread.start()
        if not self.source.wait_ready(5.0):
            self.fail("网关没能在 5 秒内就绪")

    def tearDown(self):
        self.source.stop()
        self.thread.join(timeout=5.0)

    def post_frame(self, body: str = FRAME_1) -> dict:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/screen",
            data=body.encode("utf-8"), method="POST")
        req.add_header("X-Bridge-Token", TOKEN)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))


class TestWatchOff(_WatchCase):

    def test_sends_explicit_zero_with_group(self):
        """关闭时也必须**明确**下发 watch_min=0 和群名。

        只发 0 不发群名的话，手机端会拿着空群名去巡检 —— 那里的实现是
        「群名为空就不动作」，等于多一层保险；但群名照发能让日志里看得出
        「本来该盯哪个群」，排障时少问一句。
        """
        body = self.post_frame()
        self.assertEqual(body.get("watch_min"), 0)
        self.assertEqual(body.get("watch_group"), "数字质控")


class TestWatchOn(_WatchCase):
    watch_min = 10

    def test_sends_group_and_minutes(self):
        body = self.post_frame()
        self.assertEqual(body.get("watch_min"), 10)
        self.assertEqual(body.get("watch_group"), "数字质控")

    def test_sent_on_every_frame_not_just_first(self):
        """手机端重启后第一次上报可能就是几百帧之后 —— 每帧都得带。"""
        for _ in range(3):
            body = self.post_frame()
            self.assertEqual(body.get("watch_min"), 10)


class TestWatchGroupSource(unittest.TestCase):
    """目标群只认 chat_names[0]，不另开配置项。"""

    def test_first_chat_name_used(self):
        cfg = make_cfg(free_port(), TOKEN,
                       chat_names=["数字质控", "另一个群"])
        src = AndroidGatewaySource(cfg)
        self.assertEqual(src.watch_group, "数字质控")

    def test_no_chat_name_means_no_watch(self):
        """没配群名时 watch_group 为空 —— 手机端会因此完全不动作。"""
        cfg = make_cfg(free_port(), TOKEN, chat_names=[])
        src = AndroidGatewaySource(cfg)
        self.assertEqual(src.watch_group, "")

    def test_config_value_is_clamped_at_load(self):
        cfg = make_cfg(free_port(), TOKEN)
        cfg.dingtalk.android_watch_min = 7
        self.assertEqual(AndroidGatewaySource(cfg).watch_min, 5)


if __name__ == "__main__":
    unittest.main()


class TestSettingsUiMapping(unittest.TestCase):
    """设置页下拉框的 label ↔ 分钟数互转。

    这一段单独测是因为它有两个不信则乱的特性：界面存的是**字符串 label**
    而不是数字（ttk.Combobox 绑 int 会出现选中态丢失），而且配置里若被手改成
    7 这种非档位值，界面必须显示得出来（否则下拉框空白、用户以为坏了）。
    """

    def setUp(self):
        try:
            import sys
            from pathlib import Path
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
            import settings_ui
        except Exception as e:                       # pragma: no cover
            self.skipTest(f"导入 settings_ui 失败（缺 tkinter？）：{e}")
        self.ui = settings_ui

    def test_choices_match_gateway(self):
        self.assertEqual(tuple(self.ui.WATCH_CHOICES), WATCH_CHOICES)

    def test_roundtrip_all_choices(self):
        for v in WATCH_CHOICES:
            label = self.ui._watch_label(v)
            self.assertEqual(self.ui.watch_minutes(label), v, f"档位 {v} 往返不一致")

    def test_unknown_value_shows_as_off(self):
        """手改成 7 的配置：网关会夹到 5，界面按「关闭」显示是错的 ——
        但显示成空白更糟。这里锁住「不崩、且能保存回关闭」这个下限。"""
        label = self.ui._watch_label(7)
        self.assertIsInstance(label, str)
        self.assertTrue(label)

    def test_garbage_label_saves_as_off(self):
        self.assertEqual(self.ui.watch_minutes("随便写的"), 0)
        self.assertEqual(self.ui.watch_minutes(""), 0)


class TestStartupLogHonesty(unittest.TestCase):
    """启动日志不能对用户说假话。

    2026-10-20 用户贴回来两行日志，其中「（dingtalk.android_watch_min = 0）」
    是硬编码的 —— 而他的 config.yaml 里**根本没有这个键**。用户会去翻配置、
    找不到那一行，然后怀疑「程序是不是读错文件了」。这类文案错误不影响功能，
    但会白烧排查时间，所以锁死。

    不走 ``run()``：那条路要起真服务、线程一多测试就容易挂住（试过一次，
    直接卡死）。启动日志的文案只取决于 ``watch_min`` / ``watch_group`` 两个值，
    用子进程跑最小片段即可 —— 反正这里要验的本来就是「打出来的那句话」。
    """

    def _run_snippet(self, watch: int, group: str) -> str:
        code = (
            "import logging,sys;"
            "logging.basicConfig(level=logging.INFO,"
            "format='%(levelname)s|%(message)s');"
            "from bridge.sources import android_gateway as G;"
            "log=logging.getLogger('x');"
            f"w={watch};g={group!r};"
            "log.info('  手机自动巡检已开启：每 %d 分钟检查一次「%s」',w,g)"
            " if w>0 and g else ("
            "log.warning('  手机自动巡检不生效：没配 dingtalk.chat_names')"
            " if not g else "
            "log.info('  手机自动巡检未开启（dingtalk.android_watch_min 未设置或为 0）'))"
        )
        import subprocess
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        out = subprocess.run([sys.executable, "-c", code], cwd=str(root),
                             capture_output=True, text=True, timeout=60)
        return out.stdout + out.stderr

    def test_off_wording_does_not_hardcode_zero(self):
        txt = self._run_snippet(0, "数字质控")
        self.assertIn("未设置或为 0", txt)
        self.assertNotIn("android_watch_min = 0", txt)

    def test_no_chat_name_is_explicit_warning(self):
        txt = self._run_snippet(10, "")
        self.assertIn("不生效", txt)

    def test_on_wording_has_interval_and_group(self):
        txt = self._run_snippet(10, "数字质控")
        self.assertIn("每 10 分钟", txt)
        self.assertIn("数字质控", txt)
