"""``bridge.table_image`` 与 service 层「表格转图片」的测试。

用 2026-10-17 真机卡片里那张拦截单表格当样板 —— 表头 3 列（小件员名称 /
票数 / 单号），这正是用户要求「转成图片发微信」的那类内容。
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.table_image import (CELL_SEP, MIN_CELLS, has_table, parse_rows,
                                render_message, render_table, save_png,
                                split_labelled, table_of)

TEXT = " ｜ ".join(["小件员名称: 周吉祥", "票数: 1票",
                    "单号: 777454064365026"])
TEXT_2ROWS = TEXT + "\n" + " ｜ ".join(["小件员名称: 李四", "票数: 3票",
                                        "单号: 777454064365027"])


class TestParse(unittest.TestCase):

    def test_labelled_row_is_parsed_with_head(self):
        head, rows = split_labelled(TEXT)
        self.assertEqual(head, ["小件员名称", "票数", "单号"])
        self.assertEqual(rows, [["周吉祥", "1票", "777454064365026"]])

    def test_plain_text_is_not_a_table(self):
        self.assertEqual(split_labelled("今天共拦截 37 票，辛苦大家"), ([], []))
        self.assertEqual(split_labelled(""), ([], []))
        # 两格的键值对不算表 —— 太常见，到处都是
        self.assertEqual(split_labelled("时间: 08:13 ｜ 网点: 大江东"), ([], []))

    def test_two_rows_same_columns_group_together(self):
        head, rows = table_of(TEXT_2ROWS)
        self.assertEqual(head, ["小件员名称", "票数", "单号"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1], ["李四", "3票", "777454064365027"])

    def test_row_with_fewer_cells_than_min_is_ignored(self):
        one = CELL_SEP.join(["a: 1", "b: 2"])           # 只有 2 格
        self.assertFalse(has_table(one))

    def test_bare_rows_fall_back_to_first_row_as_head(self):
        # 退化形态：没有「列名:」前缀的裸表格（真机周报有过）
        bare = " ｜ ".join(["名称", "票数", "单号"]) + "\n" \
            + " ｜ ".join(["周吉祥", "1票", "777454064365026"])
        head, rows = table_of(bare)
        self.assertEqual(head, ["名称", "票数", "单号"])
        self.assertEqual(rows, [["周吉祥", "1票", "777454064365026"]])

    def test_mixed_column_counts_keep_the_widest_group(self):
        mixed = TEXT_2ROWS + "\n" + " ｜ ".join(["甲: 1", "乙: 2", "丙: 3"])
        head, rows = table_of(mixed)
        self.assertEqual(head, ["小件员名称", "票数", "单号"])
        self.assertEqual(len(rows), 2)

    def test_full_message_with_prose_still_finds_the_table(self):
        # 真机卡片的形状：表格前后各有几段正文
        msg = ("@所有人 拦截单退单打印提醒\n今天还有1票没完成打印。\n"
               + TEXT + "\n大家辛苦～")
        head, rows = table_of(msg)
        self.assertEqual(head, ["小件员名称", "票数", "单号"])
        self.assertEqual(rows, [["周吉祥", "1票", "777454064365026"]])


class TestRender(unittest.TestCase):
    def setUp(self):
        try:
            import PIL                     # noqa: F401
        except ImportError:
            self.skipTest("本机没装 Pillow，渲染测试跳过")

    def test_render_produces_an_image(self):
        head, rows = table_of(TEXT_2ROWS)
        img = render_table(head, rows, title="拦截跟单专员")
        self.assertIsNotNone(img)
        self.assertGreater(img.size[0], 200)
        self.assertGreater(img.size[1], 80)

    def test_no_rows_returns_none(self):
        self.assertIsNone(render_table([], []))
        self.assertIsNone(render_message("没有表格的普通消息", out_dir="data/tmp"))

    def test_render_message_saves_png_and_is_idempotent(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            p1 = render_message(TEXT_2ROWS, out_dir=td, key="k1", title="表")
            p2 = render_message(TEXT_2ROWS, out_dir=td, key="k1", title="表")
            self.assertIsNotNone(p1)
            self.assertEqual(p1, p2, "同一内容两次渲染应得到同一个文件（幂等）")
            self.assertTrue(Path(p1).is_file())
            self.assertEqual(Path(p1).suffix, ".png")

    def test_render_message_survives_prose_only_text(self):
        self.assertIsNone(render_message("普通文字", out_dir="data/tmp"))

    def test_long_cell_wraps_instead_of_shrinking(self):
        head, rows = table_of(" ｜ ".join(
            ["小件员名称: " + "很长的名字" * 10, "票数: 1票",
             "单号: 777454064365026"]))
        img = render_table(head, rows)
        self.assertIsNotNone(img)
        self.assertLessEqual(img.size[0], 1200 + 4, "超宽内容应该折行而不是撑爆")


class TestServiceIntegration(unittest.TestCase):
    """service.handle 在文字之后补发表格图（不发图不给文字丢掉）。"""

    def setUp(self):
        try:
            import PIL                     # noqa: F401
        except ImportError:
            self.skipTest("本机没装 Pillow，渲染测试跳过"
                          "（生产 worker 环境有 PIL，功能不受影响）")

    def _service(self, tmp, send_image=None):
        from bridge.config import (Config, DingTalkConfig, RouteConfig,
                                   StorageConfig, WeChatConfig)
        from bridge.service import BridgeService
        from bridge.targets.base import SendResult, Target

        class FakeTarget(Target):
            name = "fake"

            def __init__(self):
                self.texts = []
                self.images = []

            def send_text(self, target, text):
                self.texts.append((target, text))
                return SendResult(True, target, "ok")

            def send_image(self, target, path):
                self.images.append((target, path))
                return SendResult(True, target, "ok")

        td = tempfile.mkdtemp(prefix="ddwx-table-test-")
        self.addCleanup(self._cleanup, td)
        cfg = Config(
            dingtalk=DingTalkConfig(mode="ocr"),
            wechat=WeChatConfig(dry_run=False, table_image=True,
                                targets=["测试群"]),
            routes=[RouteConfig(name="r1", targets=["测试群"])],
            storage=StorageConfig(db_path=str(Path(td) / "bridge.db")),
        )
        cfg.wechat.interval = 0
        t = FakeTarget()
        svc = BridgeService(cfg, target=t)
        self.addCleanup(svc.storage.close)
        return svc, t

    @staticmethod
    def _cleanup(td):
        import shutil
        import time
        for _ in range(5):
            try:
                shutil.rmtree(td)
                return
            except PermissionError:
                time.sleep(0.2)     # SQLite 句柄释放有延迟，Windows 上常见
        shutil.rmtree(td, ignore_errors=True)

    def test_table_message_sends_text_then_image(self):
        from bridge.models import Message

        svc, t = self._service(None)
        msg = Message(source="android", chat_name="数字质控",
                      sender_name="拦截跟单专员", content=TEXT,
                      raw_id="t1:1")
        self.assertTrue(svc.handle(msg))
        self.assertEqual(len(t.texts), 1, "文字没发")
        self.assertEqual(len(t.images), 1, "表格图没补发")
        self.assertTrue(t.images[0][1].endswith(".png"))

    def test_plain_message_sends_no_image(self):
        from bridge.models import Message

        svc, t = self._service(None)
        msg = Message(source="android", chat_name="数字质控",
                      sender_name="张三", content="普通文字",
                      raw_id="t2:1")
        self.assertTrue(svc.handle(msg))
        self.assertEqual(len(t.texts), 1)
        self.assertEqual(t.images, [], "普通消息不该补图")

    def test_image_failure_does_not_lose_the_text(self):
        from bridge.models import Message

        svc, t = self._service(None)

        def boom(target, path):
            raise RuntimeError("贴图失败")
        t.send_image = boom
        msg = Message(source="android", chat_name="数字质控",
                      sender_name="拦截跟单专员", content=TEXT,
                      raw_id="t3:1")
        self.assertTrue(svc.handle(msg))
        self.assertEqual(len(t.texts), 1, "图片抛异常把文字也搞丢了")


if __name__ == "__main__":
    unittest.main()
