"""phone_panel.find_adb() 在「打包成 exe」形态下也能找到 adb。

背景
    2026-10-31 重新打包 Setup 后冒烟：界面自检全绿，但右侧手机画面
    永远停在「正在取第一帧…」。根因是 find_adb() 用 ``__file__`` 往上推
    项目根 —— 冻结态 ``__file__`` 指向 PyInstaller 的解压临时目录
    （%TEMP%\\_MEIxxxx），推出来的根是 %TEMP%，自然找不到
    ``tools/platform-tools/adb.exe``。修复：冻结态改用 exe 所在目录当根。

    tkinter 在无显示环境会 import 失败，所以只测纯函数 find_adb：
    用 importlib 从源文件里把这一个函数摘出来编译执行，不拉起面板。
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SRC = BASE_DIR / "app" / "phone_panel.py"


def _load_find_adb():
    """只编译 find_adb 这一个函数（不 import 整个模块，避开 tkinter）。"""
    src = SRC.read_text(encoding="utf-8")
    tree = ast_parse(src)
    ns: dict = {}
    # find_adb 依赖 sys / Path / shutil.which / __file__ —— 给够真实的
    ns["sys"] = sys
    ns["Path"] = Path
    ns["__file__"] = str(SRC)
    import shutil
    ns["shutil"] = shutil
    exec(compile(tree, str(SRC), "exec"), ns)            # noqa: S102
    return ns["find_adb"]


def ast_parse(src: str):
    import ast
    mod = ast.parse(src)
    keep = []
    for node in mod.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == "find_adb":
            keep.append(node)
    if not keep:
        raise AssertionError("phone_panel.py 里找不到 find_adb")
    return ast.Module(body=keep, type_ignores=[])


find_adb = _load_find_adb()


class TestFindAdbFrozen(unittest.TestCase):
    def _make_root(self, tmp: Path) -> Path:
        adb = tmp / "tools" / "platform-tools" / "adb.exe"
        adb.parent.mkdir(parents=True, exist_ok=True)
        adb.write_bytes(b"fake adb")
        return adb

    def test_frozen_uses_exe_dir(self):
        """冻结态：adb 在 exe 同级的 tools/platform-tools 下必须能找到。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            adb = self._make_root(root)
            fake = types.SimpleNamespace(frozen=True)
            orig = sys.executable
            try:
                sys.frozen = True                        # type: ignore[attr-defined]
                sys.executable = str(root / "喵喵钉钉-微信消息转发桥.exe")
                self.assertEqual(find_adb(), str(adb))
            finally:
                sys.executable = orig
                del sys.frozen                           # type: ignore[attr-defined]
                del fake

    def test_frozen_without_adb_falls_back(self):
        """冻结态找不到 adb 时不许抛异常 —— 返回 None/PATH 结果。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "empty"
            root.mkdir()
            orig = sys.executable
            try:
                sys.frozen = True                        # type: ignore[attr-defined]
                sys.executable = str(root / "app.exe")
                r = find_adb()
                self.assertTrue(r is None or Path(r).exists())
            finally:
                sys.executable = orig
                del sys.frozen                           # type: ignore[attr-defined]

    def test_source_layout_still_works(self):
        """源码态（本仓库）必须仍然找到 tools/platform-tools/adb.exe。"""
        r = find_adb()
        if (BASE_DIR / "tools" / "platform-tools" / "adb.exe").exists():
            self.assertIsNotNone(r)
            self.assertIn("platform-tools", str(r))


if __name__ == "__main__":
    unittest.main()
