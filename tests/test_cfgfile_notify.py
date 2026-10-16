"""配置段的「升级补齐」能力测试（app/cfgfile.py 的 section_text / ensure_section）。

为什么值得单独测
    安装包刻意不再夹带 config.yaml（否则会把构建机的群名和机器人 token
    一起分发出去，升级时还会覆盖用户改过的配置）。代价是：从老版本升级上来的
    config.yaml 里没有 notify 段，那「设置中心 → 钉钉通知」就存不进去。
    补齐逻辑一旦写错，用户升级后就没法配置告警了，所以要有测试兜住。

另外一条硬约束：routes 必须是最后一个顶层段（set_routes 依赖这一点），
所以补进来的段只能插在 routes **之前**。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
for p in (str(BASE_DIR), str(BASE_DIR / "app")):
    if p not in sys.path:
        sys.path.insert(0, p)

import cfgfile                                        # noqa: E402
from bridge.config import load_config                 # noqa: E402

SAMPLE = BASE_DIR / "config.example.yaml"

# 模拟一个老版本（没有 notify 段）的用户配置
LEGACY = """\
storage:
  db_path: "data/bridge.db"

dingtalk:
  mode: "ocr"
  chat_names:
    - "数字质控"

wechat:
  dry_run: false
  wxauto_path: "C:/old/wxauto-main"

routes:
  - name: "老规则"
    chat_names:
      - "数字质控"
    targets:
      - "申通五星业务员培训群"
    template: "{content}"
"""


class TestSectionHelpers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.yaml"

    def _write(self, text: str) -> Path:
        self.path.write_text(text, encoding="utf-8")
        return self.path

    # ---- section_text ----

    def test_sample_notify_text_contains_section_and_comments(self):
        text = cfgfile.section_text(SAMPLE, "notify")
        self.assertIn("notify:", text)
        self.assertIn("enabled:", text)
        self.assertNotIn("routes:", text, "不该把后面的段也吞进来")
        # 段头上方的说明注释要一起带走，否则升级补进来的段没有上下文
        self.assertIn("只能往钉钉群里发消息", text)
        self.assertTrue(text.lstrip().startswith("#"),
                        "应当以段头注释开头")

    def test_missing_section_returns_empty(self):
        self._write(LEGACY)
        self.assertEqual(cfgfile.section_text(self.path, "notify"), "")

    def test_missing_file_returns_empty(self):
        self.assertEqual(cfgfile.section_text(Path(self.tmp.name) / "无.yaml", "notify"), "")

    # ---- has_section ----

    def test_has_section(self):
        self._write(LEGACY)
        self.assertTrue(cfgfile.has_section(self.path, "routes"))
        self.assertFalse(cfgfile.has_section(self.path, "notify"))

    def test_has_section_on_missing_file(self):
        self.assertFalse(cfgfile.has_section(Path(self.tmp.name) / "无.yaml", "notify"))

    # ---- ensure_section ----

    def test_inserts_before_routes(self):
        self._write(LEGACY)
        block = cfgfile.section_text(SAMPLE, "notify")
        self.assertTrue(cfgfile.ensure_section(self.path, "notify", block))

        text = self.path.read_text(encoding="utf-8")
        self.assertLess(text.index("\nnotify:"), text.index("\nroutes:"),
                        "notify 必须插在 routes 之前，否则 set_routes 会拒绝重写")

    def test_inserted_section_is_loadable(self):
        self._write(LEGACY)
        cfgfile.ensure_section(self.path, "notify",
                               cfgfile.section_text(SAMPLE, "notify"))
        cfg = load_config(self.path)
        self.assertFalse(cfg.notify.enabled, "样板里默认是关闭的，不该默认开")
        self.assertEqual(cfg.notify.cooldown, 300.0)
        # 关键：补段不能影响原有内容
        self.assertEqual(cfg.dingtalk.chat_names, ["数字质控"])
        self.assertFalse(cfg.wechat.dry_run)
        self.assertEqual(cfg.routes[0].targets, ["申通五星业务员培训群"])

    def test_routes_still_last_section(self):
        """补段之后 routes 仍是最后一个顶层段，界面才能重建它。"""
        self._write(LEGACY)
        cfgfile.ensure_section(self.path, "notify",
                               cfgfile.section_text(SAMPLE, "notify"))
        rules = cfgfile.read_rules(self.path)
        self.assertEqual(len(rules), 1)
        self.assertTrue(cfgfile.set_routes(self.path, rules),
                        "补段后 set_routes 仍应可用")

    def test_is_idempotent(self):
        self._write(LEGACY)
        block = cfgfile.section_text(SAMPLE, "notify")
        cfgfile.ensure_section(self.path, "notify", block)
        once = self.path.read_text(encoding="utf-8")
        cfgfile.ensure_section(self.path, "notify", block)
        cfgfile.ensure_section(self.path, "notify", block)
        self.assertEqual(self.path.read_text(encoding="utf-8"), once,
                         "重复执行不该插入多份")

    def test_existing_section_is_untouched(self):
        """用户已经自己配好了 notify 段时，绝不能覆盖。"""
        self._write(LEGACY.replace("routes:", """notify:
  enabled: true
  webhook: "https://用户自己填的"

routes:"""))
        before = self.path.read_text(encoding="utf-8")
        self.assertTrue(cfgfile.ensure_section(self.path, "notify",
                                               cfgfile.section_text(SAMPLE, "notify")))
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        self.assertEqual(load_config(self.path).notify.webhook, "https://用户自己填的")

    def test_empty_block_is_refused(self):
        self._write(LEGACY)
        self.assertFalse(cfgfile.ensure_section(self.path, "notify", ""))
        self.assertFalse(cfgfile.ensure_section(self.path, "notify", "   \n"))
        self.assertNotIn("notify", self.path.read_text(encoding="utf-8"))

    def test_appends_when_no_routes(self):
        self._write("storage:\n  db_path: \"data/bridge.db\"\n")
        self.assertTrue(cfgfile.ensure_section(
            self.path, "notify", cfgfile.section_text(SAMPLE, "notify")))
        self.assertTrue(cfgfile.has_section(self.path, "notify"))

    def test_missing_file_is_refused(self):
        self.assertFalse(cfgfile.ensure_section(
            Path(self.tmp.name) / "无.yaml", "notify",
            cfgfile.section_text(SAMPLE, "notify")))

    # ---- 升级之后真的能保存 ----

    def test_settings_can_save_notify_after_upgrade(self):
        """端到端：老配置 -> 补段 -> 走一遍设置中心的写入 -> 读回。"""
        self._write(LEGACY)
        cfgfile.ensure_section(self.path, "notify",
                               cfgfile.section_text(SAMPLE, "notify"))

        values = {
            "enabled": True, "webhook": "https://oapi.dingtalk.com/robot/send?access_token=T",
            "secret": "SECx", "keyword": "转发桥", "at_all": False,
            "cooldown": 120, "log_alerts": True, "notify_start": False,
            "notify_fail": True, "fail_threshold": 5, "notify_idle": True,
            "idle_minutes": 90, "idle_window": "07:30-19:30",
            "report_hourly": True, "report_daily_at": "18:00",
        }
        for key, val in values.items():
            with self.subTest(key=key):
                self.assertTrue(cfgfile.set_scalar(self.path, "notify", key, val),
                                f"{key} 应该能写进去")
        self.assertTrue(cfgfile.set_list(self.path, "notify", "at_mobiles",
                                         ["13800000000", "13900000000"]))

        n = load_config(self.path).notify
        self.assertTrue(n.enabled)
        self.assertEqual(n.webhook, values["webhook"])
        self.assertEqual(n.at_mobiles, ["13800000000", "13900000000"])
        self.assertEqual(n.fail_threshold, 5)
        self.assertEqual(n.report_daily_at, "18:00")

    def test_comments_survive_round_trip(self):
        self._write(LEGACY)
        cfgfile.ensure_section(self.path, "notify",
                               cfgfile.section_text(SAMPLE, "notify"))
        before = sum(1 for l in self.path.read_text(encoding="utf-8").splitlines()
                     if l.strip().startswith("#"))
        cfgfile.set_scalar(self.path, "notify", "cooldown", 999)
        cfgfile.set_scalar(self.path, "notify", "cooldown", 300)
        after = sum(1 for l in self.path.read_text(encoding="utf-8").splitlines()
                    if l.strip().startswith("#"))
        self.assertEqual(after, before, "写入不该洗掉注释")
        self.assertGreater(before, 10, "补进来的段应带有样板里的说明注释")

if __name__ == "__main__":
    unittest.main()
