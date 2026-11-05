"""安装向导「升级时补齐可选组件」的测试（installer/setup_gui.py）。

为什么单独测这个
    2026-11-05 实测事故：机器上从 0.59 升级装 0.60（复用已有 runtime），
    安装日志一路「环境配置完成」，装完却 ``import Crypto`` 失败，
    「漏转对账」静默降级成「不可用」。根因是：

      ``_find_existing_worker`` 只检查 ``paths.WORKER_MODULES``（主流程必需
      项：yaml / PIL / numpy / onnxruntime / OCR），命中就直接判「环境完整」
      并跳过配置页；而 pycryptodome 是 0.60 才引入的**可选**组件，不在那份
      清单里，于是永远补不上。

    修法：命中老环境后仍走一遍 ``_ensure_optional``，按需补装
    pycryptodome / windows-capture，失败只降级。

    ``_ensure_optional`` 只用到 ``self._ui / _log / _vendor_bundle /
    _env_pycryptodome / _env_wgc / install_dir``，用 SimpleNamespace 顶替即可，
    不启动 tkinter。

    python -m unittest tests.test_setup_optional_env -v
"""
from __future__ import annotations

import ast
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

SETUP_SRC = (BASE_DIR / "installer" / "setup_gui.py")


def _load(name: str):
    """把 setup_gui 里的单个方法摘出来编译成普通函数（不 import 整个模块）。"""
    tree = ast.parse(SETUP_SRC.read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == name), None)
    if fn is None:
        raise AssertionError(f"setup_gui.py 里找不到方法 {name}")
    mod = ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[
            ast.alias(name="annotations", asname=None)], level=0),
        fn,
    ], type_ignores=[])
    ast.fix_missing_locations(mod)
    ns: dict = {"sys": sys, "Path": Path, "time": __import__("time"),
                "_dbg": lambda msg: None}
    exec(compile(mod, str(SETUP_SRC), "exec"), ns)   # noqa: S102
    return ns[name]


class _Harness:
    """假 self：记录被调用的补装方法，不碰界面。"""

    def __init__(self, missing, bundle=("zip", Path("wheels"))):
        self.missing = missing
        self.bundle = bundle
        self.logs: list[tuple[str, str]] = []
        self.calls: list[tuple[str, object]] = []
        self.install_dir = BASE_DIR
        self._ensure_optional = _load("_ensure_optional")

    # --- 被 _ensure_optional 用到的方法 ---
    def _ui(self, fn, *a):
        fn(*a)

    def _log(self, msg, tag="n"):
        self.logs.append((msg, tag))

    def _vendor_bundle(self):
        return self.bundle

    def _env_pycryptodome(self, py, wheels_dir=None):
        self.calls.append(("pycryptodome", wheels_dir))

    def _env_wgc(self, py, wheels_dir=None):
        self.calls.append(("wgc", wheels_dir))

    def run(self):
        # 摘出来的是普通函数，挂在实例上不会自动绑定 self
        self._ensure_optional(self, Path("python.exe"))
        return self


class EnsureOptionalTest(unittest.TestCase):
    def setUp(self):
        self._orig = sys.modules.get("paths")
        self.fake_paths = types.ModuleType("paths")
        sys.modules["paths"] = self.fake_paths

    def tearDown(self):
        if self._orig is None:
            sys.modules.pop("paths", None)
        else:
            sys.modules["paths"] = self._orig

    def _fake_missing(self, missing):
        self.fake_paths.missing_modules = lambda exe, mods: (False, list(missing))

    def test_补装缺失的pycryptodome(self):
        self._fake_missing(["Crypto"])
        h = _Harness(["Crypto"]).run()
        self.assertEqual(h.calls, [("pycryptodome", Path("wheels"))])
        self.assertIn("Crypto", h.logs[0][0])

    def test_齐全时什么都不做(self):
        self._fake_missing([])
        h = _Harness([]).run()
        self.assertEqual(h.calls, [])
        self.assertEqual(h.logs, [])

    def test_两个都缺就都补(self):
        self._fake_missing(["Crypto", "windows_capture"])
        h = _Harness(["Crypto", "windows_capture"]).run()
        self.assertEqual([c[0] for c in h.calls], ["pycryptodome", "wgc"])

    def test_没有离线包时传None让它联网(self):
        self._fake_missing(["Crypto"])
        h = _Harness(["Crypto"], bundle=None).run()
        self.assertEqual(h.calls, [("pycryptodome", None)])

    def test_查依赖异常不影响安装(self):
        def boom(exe, mods):
            raise OSError("解释器跑不起来")
        self.fake_paths.missing_modules = boom
        h = _Harness([]).run()
        self.assertEqual(h.calls, [])


class EnvLogFallbackTest(unittest.TestCase):
    """复用老环境时没建环境页，``self.env_log`` 不存在 —— 日志要落到主日志。"""

    def test_没有环境页时写主日志(self):
        env_log = _load("_env_log")
        logs: list[tuple[str, str]] = []

        def _ui(fn, *a):
            fn(*a)

        fake = SimpleNamespace(
            _ui=_ui,
            _log=lambda msg, tag="n": logs.append((msg, tag)),
            _dbg=lambda msg: None,
            after=lambda ms, fn: fn(),
        )
        env_log(fake, "补齐可选组件：Crypto", "dim")
        self.assertEqual(logs, [("补齐可选组件：Crypto", "dim")])

    def test_有环境页时走日志框(self):
        env_log = _load("_env_log")
        seen: list[str] = []

        class _Text:
            def configure(self, **kw):
                pass

            def insert(self, *a):
                seen.append(a[-1])

            def tag_add(self, *a):
                pass

            def tag_config(self, *a, **kw):
                pass

            def see(self, *a):
                pass

        fake = SimpleNamespace(
            _ui=lambda fn, *a: fn(*a),
            _log=lambda msg, tag="n": None,
            _dbg=lambda msg: None,
            after=lambda ms, fn: fn(),
            env_log=_Text(),
        )
        env_log(fake, "安装依赖…", "dim")
        self.assertEqual(len(seen), 1)
        self.assertIn("安装依赖", seen[0])


if __name__ == "__main__":
    unittest.main()
