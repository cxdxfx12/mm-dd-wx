"""「改或插」写配置的回归锁。

背景（2026-10-16 线上实录）
    老用户的 config.yaml 是旧版本生成的，新版本往 ``dingtalk`` 段加了
    ``time_window`` 这个键。设置中心保存时走的是 ``set_scalar`` ——
    它只会改**已存在**的键，一遇到缺失的键就报
    「以下配置项没能写入：dingtalk.time_window」，整个设置存不下来。

    修法是给 cfgfile 补上「键不存在就插进去」的能力
    （:func:`ensure_scalar` / :func:`set_or_insert_scalar` /
    :func:`ensure_list_key`），插入位置跟着样板文件里的先后顺序走。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "app")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bridge.config import load_config                       # noqa: E402
from cfgfile import (ensure_list_key, ensure_scalar,        # noqa: E402
                     read_rules, set_or_insert_scalar)

OLD_CONFIG = """\
storage:
  db_path: "data/bridge.db"

dingtalk:
  mode: "ocr"
  chat_names:
    - "【数字质控】浙江杭州萧山区大江东公司"
  ui_poll_interval: 5.0
  title_check: true
  title_ratio: 0.55
  time_filter: "today"
  # 注意：旧版本没有 time_window / anchor_guard 这两个键

wechat:
  dry_run: true
  interval: 3.0
  wxauto_path: "../wxauto-main"

notify:
  enabled: true
  webhook: "https://oapi.dingtalk.com/robot/send?access_token=oldrobot"
  secret: ""
  keyword: ""
  at_mobiles: []
  at_all: false
  cooldown: 300

routes:
  - name: "质控群"
    chat_names: ["数字质控"]
    targets: ["申通五星业务员培训群"]
    template: "{content}"
"""


class TestSetOrInsertScalar(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ddwx_ensure_"))
        self.path = self.tmp / "config.yaml"
        self.original = OLD_CONFIG
        self.path.write_text(self.original, encoding="utf-8")

    def test_missing_key_is_inserted(self):
        """就是线上那个 bug：老配置缺 dingtalk.time_window，必须插进去。"""
        self.assertTrue(set_or_insert_scalar(self.path, "dingtalk",
                                             "time_window", "08:00-21:00"))
        self.assertEqual(load_config(self.path).dingtalk.time_window,
                         "08:00-21:00")

    def test_insert_lands_right_after_anchor(self):
        set_or_insert_scalar(self.path, "dingtalk", "time_window",
                             "08:00-21:00", after_key="time_filter")
        lines = self.path.read_text(encoding="utf-8").splitlines()
        idx = {s.strip().split(":")[0]: i for i, s in enumerate(lines)
               if s.strip() and ":" in s and not s.strip().startswith("-")}
        self.assertLess(idx["time_filter"], idx["time_window"])
        self.assertLess(idx["time_window"], idx["wechat"])

    def test_inserted_value_is_indented_into_its_section(self):
        """曾经踩过：缩进丢了会让这一行变成顶层键，进而误删后面的配置。"""
        set_or_insert_scalar(self.path, "dingtalk", "anchor_guard", True,
                             after_key="time_filter")
        line = next(s for s in self.path.read_text(encoding="utf-8").splitlines()
                    if s.strip().startswith("anchor_guard"))
        self.assertTrue(line.startswith("  "),
                        f"anchor_guard 必须带段内缩进，实际是 {line!r}")
        self.assertEqual(read_rules(self.path)[0]["targets"],
                         ["申通五星业务员培训群"])

    def test_existing_key_is_updated_in_place(self):
        before = self.path.read_text(encoding="utf-8")
        self.assertTrue(set_or_insert_scalar(self.path, "dingtalk",
                                             "ui_poll_interval", 8.0))
        cfg = load_config(self.path)
        self.assertEqual(cfg.dingtalk.ui_poll_interval, 8.0)
        # 注释与其余内容不许被动过
        self.assertIn("旧版本没有", self.path.read_text(encoding="utf-8"))
        self.assertEqual(
            len(before.splitlines()), len(self.path.read_text(encoding="utf-8").splitlines()))

    def test_chained_inserts_keep_template_order(self):
        """一连串键都缺时，按调用顺序逐个插到前一个后面，不会堆成一团。"""
        for key, value, after in (("title_check", True, "ui_poll_interval"),
                                  ("title_ratio", 0.6, "title_check"),
                                  ("time_filter", "today", "title_ratio"),
                                  ("time_window", "08:00-21:00", "time_filter"),
                                  ("anchor_guard", True, "time_window")):
            self.assertTrue(set_or_insert_scalar(self.path, "dingtalk", key,
                                                 value, after_key=after))
        cfg = load_config(self.path)
        self.assertTrue(cfg.dingtalk.title_check)
        self.assertEqual(cfg.dingtalk.title_ratio, 0.6)
        self.assertEqual(cfg.dingtalk.time_filter, "today")
        self.assertEqual(cfg.dingtalk.time_window, "08:00-21:00")
        self.assertTrue(cfg.dingtalk.anchor_guard)
        self.assertEqual(read_rules(self.path)[0]["targets"],
                         ["申通五星业务员培训群"])

    def test_existing_key_not_duplicated_by_ensure(self):
        ensure_scalar(self.path, "dingtalk", "ui_poll_interval", 9.0)
        body = self.path.read_text(encoding="utf-8")
        self.assertEqual(body.count("ui_poll_interval:"), 1)
        self.assertEqual(load_config(self.path).dingtalk.ui_poll_interval, 5.0,
                         "键已存在时 ensure_scalar 不该改值")

    def test_missing_section_returns_false(self):
        p = self.tmp / "none.yaml"
        p.write_text("storage:\n  db_path: x\n", encoding="utf-8")
        self.assertFalse(set_or_insert_scalar(p, "dingtalk", "time_window", ""))

    def test_missing_file_returns_false(self):
        self.assertFalse(set_or_insert_scalar(self.tmp / "nope.yaml",
                                              "dingtalk", "time_window", ""))


class TestEnsureListKey(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ddwx_ensure_"))
        self.path = self.tmp / "config.yaml"
        self.path.write_text(OLD_CONFIG, encoding="utf-8")

    def test_webhooks_inserted_into_old_notify(self):
        """老 notify 段没有 webhooks 键 —— 必须插到 webhook 后面。"""
        url = "https://oapi.dingtalk.com/robot/send?access_token=aaaa"
        self.assertTrue(ensure_list_key(self.path, "notify", "webhooks",
                                        [f"{url} | SECxxx | 转发桥"],
                                        after_key="webhook"))
        lines = self.path.read_text(encoding="utf-8").splitlines()
        idx = {s.strip().split(":")[0]: i for i, s in enumerate(lines)
               if s.strip() and ":" in s and not s.strip().startswith("-")}
        self.assertLess(idx["webhook"], idx["webhooks"])
        self.assertLess(idx["webhooks"], idx["secret"])
        cfg = load_config(self.path)
        self.assertEqual(cfg.notify.webhooks, [f"{url} | SECxxx | 转发桥"])

    def test_routes_survives(self):
        ensure_list_key(self.path, "notify", "webhooks", [], after_key="webhook")
        self.assertEqual(read_rules(self.path)[0]["targets"],
                         ["申通五星业务员培训群"])

    def test_roundtrip_is_stable(self):
        ensure_list_key(self.path, "notify", "webhooks", ["a", "b"],
                        after_key="webhook")
        once = self.path.read_text(encoding="utf-8")
        ensure_list_key(self.path, "notify", "webhooks", ["a", "b"],
                        after_key="webhook")
        self.assertEqual(once, self.path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
