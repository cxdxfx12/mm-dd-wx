"""「只收不转」网关工具（tools/android_capture.py）的测试。

这个工具的建配置函数最容易错，而错了以后的现场表现是「等了半天一帧没有」——
用户会以为是手机的问题，实际是端口或 token 读错了、甚至是不小心带上了路由，
把消息真的投进了微信群。所以这里重点锁三件事：

  · 配置抠得对不对（host/port/token 三项）
  · 抠出来的配置**必须**是无害的（routes 空、dry_run 真）
  · 地址挑选别把回环/链路本地地址推给手机

用 importlib 从路径加载 —— `tools/` 不是包，也不该为了测试能 import 而变成包。
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

_SPEC = importlib.util.spec_from_file_location(
    "android_capture", BASE / "tools" / "android_capture.py")
capture = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = capture
_SPEC.loader.exec_module(capture)               # type: ignore[union-attr]

from bridge.config import (Config, DingTalkConfig, RouteConfig,  # noqa: E402
                           StorageConfig, WeChatConfig)


def real_cfg(**kw) -> Config:
    """仿一份真实配置：mode 是 ocr、routes 有内容、dry_run 关着。"""
    dt = dict(mode="ocr", gateway_host="0.0.0.0", gateway_port=8765,
              gateway_token="tok-from-yaml",
              chat_names=["数字质控"], companies=["大江东"])
    dt.update(kw.pop("dingtalk", {}))
    return Config(
        dingtalk=DingTalkConfig(**dt),
        wechat=WeChatConfig(dry_run=False, interval=3.0, targets=["申通五星业务员培训群"]),
        routes=[RouteConfig(name="默认", targets=["申通五星业务员培训群"],
                            chat_names=["数字质控"])],
        storage=StorageConfig(db_path="data/bridge.db"),
        **kw,
    )


class TestBuildCaptureConfig(unittest.TestCase):

    def test_takes_gateway_values_from_real_config(self):
        cap = capture.build_capture_config(real_cfg())
        self.assertEqual(cap.dingtalk.gateway_host, "0.0.0.0")
        self.assertEqual(cap.dingtalk.gateway_port, 8765)
        self.assertEqual(cap.dingtalk.gateway_token, "tok-from-yaml")

    def test_overrides_win(self):
        cap = capture.build_capture_config(real_cfg(), host="127.0.0.1",
                                           port=8766, token="cli-token")
        self.assertEqual(cap.dingtalk.gateway_host, "127.0.0.1")
        self.assertEqual(cap.dingtalk.gateway_port, 8766)
        self.assertEqual(cap.dingtalk.gateway_token, "cli-token")

    def test_mode_forced_to_android(self):
        # 真实配置是 ocr 也要照跑 —— 否则网关起不来，工具就白做了。
        self.assertEqual(real_cfg().dingtalk.mode, "ocr")
        self.assertEqual(capture.build_capture_config(real_cfg()).dingtalk.mode,
                         "android")

    def test_result_is_harmless(self):
        """这是本文件最重要的一条：抠出来的配置绝不能真的往微信发东西。"""
        cap = capture.build_capture_config(real_cfg())
        self.assertEqual(cap.routes, [])                 # 没有路由 = 投不出去
        self.assertTrue(cap.wechat.dry_run)              # 双保险
        self.assertEqual(cap.storage.db_path, ":memory:")  # 不碰生产库

    def test_keeps_chat_names_and_companies(self):
        cap = capture.build_capture_config(real_cfg())
        self.assertEqual(cap.dingtalk.chat_names, ["数字质控"])
        self.assertEqual(cap.dingtalk.companies, ["大江东"])

    def test_defaults_when_yaml_lacks_gateway_keys(self):
        # 老配置里根本没有 gateway_*（这三个键是后加的）。
        cfg = real_cfg()
        cfg.dingtalk.gateway_host = ""
        cfg.dingtalk.gateway_port = 0
        cap = capture.build_capture_config(cfg)
        self.assertEqual(cap.dingtalk.gateway_host, "0.0.0.0")
        self.assertEqual(cap.dingtalk.gateway_port, 8765)

    def test_empty_token_survives_for_caller_to_report(self):
        # 不在这一层抛异常：调用方要给出「怎么生成 token」的提示文案。
        cfg = real_cfg()
        cfg.dingtalk.gateway_token = ""
        self.assertEqual(capture.build_capture_config(cfg).dingtalk.gateway_token, "")


class TestFirstLanIp(unittest.TestCase):

    def test_skips_loopback_and_link_local(self):
        self.assertEqual(
            capture.first_lan_ip(["127.0.0.1", "169.254.3.4", "192.168.1.209"]),
            "192.168.1.209")

    def test_falls_back_to_first_when_all_unusable(self):
        self.assertEqual(capture.first_lan_ip(["127.0.0.1"]), "127.0.0.1")

    def test_placeholder_when_empty(self):
        self.assertEqual(capture.first_lan_ip([]), "<本机IP>")


class TestMetaLine(unittest.TestCase):

    def test_renders_counts(self):
        text = capture.meta_line({"count": 7, "at": "22:56:14", "bytes": 990,
                                  "texts": 3, "device": "HUAWEI Mate Xs 2",
                                  "age_sec": 1})
        for want in ("第 7 帧", "990 字节", "3 条文本", "HUAWEI Mate Xs 2", "刚刚"):
            self.assertIn(want, text)

    def test_old_frame_says_how_old(self):
        text = capture.meta_line({"count": 1, "age_sec": 42})
        self.assertIn("42 秒前", text)


class TestCli(unittest.TestCase):

    def test_argparse_defaults(self):
        ns = capture.build_parser().parse_args([])
        self.assertEqual(ns.bind, "")            # 空 = 用 config.yaml 的 gateway_host
        self.assertEqual(ns.port, 0)             # 0 = 用 config.yaml
        self.assertEqual(ns.token, "")
        self.assertEqual(ns.timeout, 180.0)
        self.assertFalse(ns.now)
        self.assertFalse(ns.keep)
        self.assertFalse(ns.no_analyze)

    def test_bind_flag_parses(self):
        ns = capture.build_parser().parse_args(
            ["--bind", "127.0.0.1", "--port", "9101", "--token", "t",
             "--now", "--keep", "--timeout", "5"])
        self.assertEqual(ns.bind, "127.0.0.1")
        self.assertEqual(ns.port, 9101)
        self.assertEqual(ns.token, "t")
        self.assertTrue(ns.now)
        self.assertTrue(ns.keep)
        self.assertEqual(ns.timeout, 5.0)

    def test_config_default_points_at_project_root(self):
        ns = capture.build_parser().parse_args([])
        self.assertEqual(Path(ns.config).resolve(), (BASE / "config.yaml").resolve())


if __name__ == "__main__":
    unittest.main()
