"""安装向导「升级时清掉旧版 APK」的测试（installer/setup_gui.py）。

为什么单独测这个
    2026-10-18 真机事故：用户升级到含多轮长消息补全的 0.3.0 之后，
    手机打开 ``http://<IP>:8765/app`` 看到的仍然是「下载并安装
    ddwx-collector-0.2.0.apk」。原因有两层：

      ① 网关 ``_apk_search_dirs()`` 用 ``sys.executable`` 推程序目录，
         而网关跑在 worker 子进程里 —— 程序目录**压根没进候选**；
      ② 安装向导升级是「解压覆盖」，**不删**新版没有的旧文件，于是
         ``android/dist/ddwx-collector-0.2.0.apk`` 一直躺着。

    ① 由 ``TestApkSearchDirs`` 钉住；这里钉 ②：装完必须只剩新版那一个。

    直接 import setup_gui 会连带拉起 tkinter，所以在文件里用 ast 摘出那个
    方法单独编译 —— 只测纯逻辑，不碰界面。

    python -m unittest tests.test_setup_stale_apk -v
"""
from __future__ import annotations

import ast
import sys
import tempfile
import types
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def _load_drop_method():
    """把 Wizard._drop_stale_apks 单独编译成一个可调用的函数。

    它只用到 ``self._ui(回调, 文本, 级别)``，用 SimpleNamespace 顶替即可。
    """
    src = (BASE_DIR / "installer" / "setup_gui.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Wizard":
            for item in node.body:
                if (isinstance(item, ast.FunctionDef)
                        and item.name == "_drop_stale_apks"):
                    mod = ast.Module(body=[item], type_ignores=[])
                    ast.fix_missing_locations(mod)
                    ns: dict = {"Path": Path}
                    exec(compile(mod, "setup_gui.py", "exec"), ns)
                    return ns["_drop_stale_apks"]
    raise AssertionError("setup_gui.py 里找不到 Wizard._drop_stale_apks")


drop_stale = _load_drop_method()


class TestDropStaleApks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.target = Path(self.tmp.name)
        self.logs: list[str] = []

        def _log(msg, tag="n"):
            self.logs.append(msg)

        def _ui(fn, text, level="info"):
            # 真类里 `self._log` 是绑定方法，`_ui` 的签名是
            # `_ui(callback, text, tag)` —— 桩要把两者的调用形状都给对，
            # 否则测的是「我随手写的桩」，不是向导的真实调用。
            fn(text, level)

        self.wizard = types.SimpleNamespace(_ui=_ui, _log=_log)

    def put(self, rel: str) -> Path:
        p = self.target / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"PK" + b"\0" * 8)
        return p

    def test_old_apk_in_android_dist_is_removed(self):
        """正是真机那次：老版装在 android/dist，新版在 app/。"""
        old = self.put("android/dist/ddwx-collector-0.2.0.apk")
        new = self.put("app/ddwx-collector-0.3.0.apk")
        drop_stale(self.wizard, self.target,
                   ["app/ddwx-collector-0.3.0.apk"])
        self.assertFalse(old.exists(), "旧包必须删掉，否则 /app 可能发回老版本")
        self.assertTrue(new.exists(), "新版包不能被误删")

    def test_old_apk_in_app_dir_is_removed(self):
        """老版也曾在 app/ 里躺着（早期安装包就放这里）。"""
        old = self.put("app/ddwx-collector-0.1.0.apk")
        self.put("app/ddwx-collector-0.3.0.apk")
        drop_stale(self.wizard, self.target,
                   ["app/ddwx-collector-0.3.0.apk"])
        self.assertFalse(old.exists())

    def test_keeps_apks_that_are_in_payload(self):
        """载荷里有的一个都不能删 —— 不论在哪个目录。"""
        a = self.put("app/ddwx-collector-0.3.0.apk")
        b = self.put("android/dist/ddwx-collector-0.3.0.apk")
        drop_stale(self.wizard, self.target,
                   ["app/ddwx-collector-0.3.0.apk",
                    "android/dist/ddwx-collector-0.3.0.apk"])
        self.assertTrue(a.exists() and b.exists())

    def test_does_not_touch_non_apk_files(self):
        """``data/`` 里是用户的消息库，绝不能碰。"""
        db = self.put("data/bridge.db")
        cfg = self.put("config.yaml")
        note = self.put("android/dist/说明.txt")
        drop_stale(self.wizard, self.target, ["app/ddwx-collector-0.3.0.apk"])
        for p in (db, cfg, note):
            self.assertTrue(p.exists(), f"{p.name} 不该被删")

    def test_missing_dirs_are_fine(self):
        """老版本没建过这些目录也不能炸。"""
        drop_stale(self.wizard, self.target, ["app/ddwx-collector-0.3.0.apk"])

    def test_idsig_is_not_treated_as_apk(self):
        """``.idsig`` 是 v4 签名附件，不在清理范围（也没人拿它下载）。"""
        sig = self.put("android/dist/ddwx-collector-0.2.0.apk.idsig")
        drop_stale(self.wizard, self.target, [])
        self.assertTrue(sig.exists())


if __name__ == "__main__":
    unittest.main()
