"""解释器候选列表的安全阀测试。

背景（真实事故）
    打包成 exe 后 ``sys.executable`` 指向的是**本程序自己**。原实现把它
    当成候选解释器，交给 ``subprocess`` 去执行 ``-c "..."``；而冻结版 exe
    会忽略命令行参数、直接再开一个主窗口 —— 每探测一次就多一个窗口，
    新实例又去探测，于是无限自我复制，表现为「不停地弹窗口」。

    这个文件锁死两条规则：
      1. 候选解释器必须看起来像 python（文件名含 python）；
      2. 冻结模式下绝不把 sys.executable 当解释器，也绝不用它兜底。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app import paths                                          # noqa: E402


class TestLooksLikePython(unittest.TestCase):
    def test_accepts_real_interpreters(self):
        for name in ("python.exe", "pythonw.exe", "python3.exe",
                     "python3.12.exe", "python312.exe", "python_d.exe",
                     "C:/Python312/python.exe",
                     "C:\\Program Files\\Python\\python.exe"):
            self.assertTrue(paths._looks_like_python(name), name)

    def test_rejects_non_python_exes(self):
        for name in ("喵喵钉钉-微信消息转发桥.exe", "uninstall.exe",
                     "WeChat.exe", "DingTalk.exe", "bash.exe",
                     "notpython.exe", "python-tool.exe", "mypython.exe"):
            self.assertFalse(paths._looks_like_python(name), name)


class TestCandidatePythons(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _fake(self, name: str) -> Path:
        p = self.root / name
        p.write_bytes(b"")
        return p

    def test_all_candidates_look_like_python(self):
        for exe in paths._candidate_pythons():
            self.assertTrue(paths._looks_like_python(exe), exe)

    def test_frozen_never_offers_itself(self):
        """冻结模式下 sys.executable == 本程序，必须从候选里消失。"""
        fake_self = self._fake("喵喵钉钉-微信消息转发桥.exe")
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", str(fake_self)):
            cands = paths._candidate_pythons()
        self.assertNotIn(str(fake_self), cands)
        self.assertNotIn(str(fake_self), [str(Path(c)) for c in cands])

    def test_env_override_still_works_for_real_python(self):
        fake_py = self._fake("python.exe")
        with mock.patch.dict("os.environ",
                             {"DDWX_WORKER_PYTHON": str(fake_py)}):
            self.assertIn(str(fake_py), paths._candidate_pythons())

    def test_env_override_rejects_non_python(self):
        fake = self._fake("notpython.exe")
        with mock.patch.dict("os.environ",
                             {"DDWX_WORKER_PYTHON": str(fake)}):
            self.assertNotIn(str(fake), paths._candidate_pythons())


class TestResolveNeverReturnsSelf(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache = self.root / "_env.json"

    def test_frozen_without_python_returns_empty_not_self(self):
        fake_self = self.root / "喵喵钉钉-微信消息转发桥.exe"
        fake_self.write_bytes(b"")
        with mock.patch.object(paths, "CACHE_FILE", self.cache), \
                mock.patch.object(paths, "_candidate_pythons", return_value=[]), \
                mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", str(fake_self)):
            info = paths.resolve(force=True)
        self.assertEqual(info["gui"], "")
        self.assertEqual(info["worker"], "")
        self.assertFalse(info["gui_ok"])
        self.assertFalse(info["worker_ok"])
        self.assertNotEqual(info["worker"], str(fake_self))

    def test_dev_mode_still_falls_back_to_sys_executable(self):
        """非冻结时保留原行为：当前解释器就是可以用的解释器。"""
        with mock.patch.object(paths, "CACHE_FILE", self.cache), \
                mock.patch.object(paths, "_candidate_pythons", return_value=[]):
            info = paths.resolve(force=True)
        self.assertEqual(info["gui"], sys.executable)
        self.assertEqual(info["worker"], sys.executable)

    def test_cache_roundtrip(self):
        with mock.patch.object(paths, "CACHE_FILE", self.cache):
            paths._save_cache({"gui": "g", "worker": "w"})
            self.assertEqual(json.loads(self.cache.read_text(encoding="utf-8")),
                             {"gui": "g", "worker": "w"})


if __name__ == "__main__":
    unittest.main()
