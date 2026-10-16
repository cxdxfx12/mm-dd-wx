"""OCR 引擎适配层与依赖探测的测试。

覆盖 2026-10-16 的引擎升级（rapidocr_onnxruntime 1.x → rapidocr 3.x）：
- ``_OcrEngine`` 把两代引擎的返回形状归一成 ``[(box, text, conf), ...]``；
- ``paths.missing_modules`` 支持「组内任一存在即算有」（OCR 新旧包二选一）。
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "app"))

from bridge.sources.dingtalk_ocr import _OcrEngine  # noqa: E402
import paths  # noqa: E402

BOX = [(0, 0), (10, 0), (10, 10), (0, 10)]


def legacy_out(res, elapse=0.1):
    """模拟 rapidocr_onnxruntime 1.x 的返回：``(result, elapse)``。"""
    return (res, elapse)


def v3_blocks():
    return [(BOX, "你好", 0.91), (BOX, "770351839142171", 0.88)]


class NumpyLike:
    """模拟 numpy 数组：多元素时 ``bool()`` 抛 ValueError（真值歧义）。

    RapidOCROutput.boxes 实际就是 numpy 数组 —— 曾经用 ``or`` 判空导致
    生产抓帧直接炸（ValueError: truth value of an array is ambiguous）。
    """

    def __init__(self, data):
        self._data = list(data)

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def __bool__(self):
        raise ValueError("The truth value of an array "
                         "with more than one element is ambiguous")


class FakeV6Output:
    """模拟 rapidocr 3.x 的 RapidOCROutput。"""

    def __init__(self, boxes=None, txts=None, scores=None):
        self.boxes = boxes
        self.txts = txts
        self.scores = scores


class TestOcrEngine(unittest.TestCase):
    def setUp(self):
        self.calls: list = []

    def engine(self, backend) -> _OcrEngine:
        def runner(arr):
            self.calls.append(arr)
            return backend
        return _OcrEngine(runner, "test")

    def test_legacy_tuple_shape_normalised(self):
        eng = self.engine(legacy_out(v3_blocks()))
        out = eng("img")
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0][1], "你好")
        self.assertEqual(out[1][2], 0.88)

    def test_legacy_none_result_becomes_empty(self):
        eng = self.engine(legacy_out(None))
        self.assertEqual(eng("img"), [])

    def test_v6_output_object_normalised(self):
        eng = self.engine(FakeV6Output(boxes=[BOX, BOX],
                                       txts=["你好", "世界"],
                                       scores=[0.95, 1.0]))
        out = eng("img")
        self.assertEqual([t for _, t, _ in out], ["你好", "世界"])
        self.assertEqual([c for _, _, c in out], [0.95, 1.0])
        self.assertEqual(out[0][0], BOX)

    def test_v6_missing_boxes_get_placeholder(self):
        eng = self.engine(FakeV6Output(txts=["你好"], scores=[0.9]))
        out = eng("img")
        self.assertEqual(out[0][0], ())
        self.assertEqual(out[0][1], "你好")

    def test_v6_missing_scores_default_full(self):
        eng = self.engine(FakeV6Output(txts=["你好"]))
        self.assertEqual(eng("img")[0][2], 1.0)

    def test_v6_numpy_like_fields_do_not_explode(self):
        """boxes 是 numpy 数组（真值歧义）时不得用 or 判空——生产炸过的点。"""
        eng = self.engine(FakeV6Output(boxes=NumpyLike([BOX, BOX]),
                                       txts=["你好", "世界"],
                                       scores=NumpyLike([0.9, 1.0])))
        out = eng("img")
        self.assertEqual([t for _, t, _ in out], ["你好", "世界"])
        self.assertEqual(out[0][0], BOX)

    def test_v6_empty_output(self):
        eng = self.engine(FakeV6Output(txts=[], scores=[]))
        self.assertEqual(eng("img"), [])

    def test_engine_name_kept_for_logging(self):
        eng = self.engine(legacy_out(None))
        self.assertEqual(eng.name, "test")

    def test_call_passes_array_through(self):
        eng = self.engine(legacy_out(None))
        eng("pixel-array")
        self.assertEqual(self.calls, ["pixel-array"])


class TestMissingModulesGroups(unittest.TestCase):
    """「组」语义：组内任一模块存在即算有（OCR 新旧包二选一）。"""

    def test_all_present(self):
        ok, miss = paths.missing_modules(sys.executable, ("os", "sys"))
        self.assertTrue(ok)
        self.assertEqual(miss, [])

    def test_group_with_one_hit_counts_as_present(self):
        ok, miss = paths.missing_modules(
            sys.executable, (("json", "definitely_not_a_module_xyz"),))
        self.assertTrue(ok)
        self.assertEqual(miss, [])

    def test_group_all_missing_reported_joined(self):
        ok, miss = paths.missing_modules(
            sys.executable, (("no_such_mod_a", "no_such_mod_b"),))
        self.assertFalse(ok)
        self.assertEqual(miss, ["no_such_mod_a|no_such_mod_b"])

    def test_mixed_strings_and_groups(self):
        ok, miss = paths.missing_modules(
            sys.executable, ("os", ("no_such_mod_a", "no_such_mod_b")))
        self.assertFalse(ok)
        self.assertEqual(miss, ["no_such_mod_a|no_such_mod_b"])

    def test_worker_modules_shape(self):
        """WORKER_MODULES 里必须有一个「组」（OCR 新旧包二选一），且都能被探测。"""
        groups = [m for m in paths.WORKER_MODULES if isinstance(m, tuple)]
        self.assertEqual(len(groups), 1)
        self.assertIn("rapidocr", groups[0])
        self.assertIn("rapidocr_onnxruntime", groups[0])

    def test_worker_env_detected_with_either_package(self):
        """worker 环境（新包已装）应能通过 WORKER_MODULES 探测。"""
        ok, miss = paths.missing_modules(sys.executable, paths.WORKER_MODULES)
        if ok:
            return          # 本解释器依赖齐全
        # 本解释器若缺 OCR 组，缺失报告必须是「组」而不是单个旧包名
        self.assertTrue(all("|" in m or m in ("yaml", "PIL", "numpy",
                                              "onnxruntime")
                            for m in miss))


if __name__ == "__main__":
    unittest.main()
