"""APK 版本号必须真的进包（android/build.py + AndroidManifest.xml）。

真机事故（2026-10-20）
    用户反馈「装了 0.4.1 的采集端，为什么手机上还是显示版本 0.30」。
    查下来：产物文件名是 ``ddwx-collector-0.4.1.apk``，可包里
    ``versionName='0.3.0'``、``versionCode='3'`` —— 0.4.0 和 0.4.1 两轮改动
    **都没有进包**。

根因
    ``build.py`` 把 ``--version-code`` / ``--version-name`` 传给了 aapt2 link，
    但 aapt2 实际**以清单为准**：清单里写了具体值，参数被完全忽略；
    清单里留空（versionCode="0" versionName="0"），则产出一个
    ``versionCode=''`` 的畸形包。也就是说这两个参数从来没起过作用。

    连带后果：versionCode 一直是 3，而手机上装的可能也是 3 ——
    Android 拒绝同版本号覆盖安装，用户「装了新包」其实没装上。

修法
    ① 清单里写真实值（这才是进包的那份）；
    ② build.py 打完包 ``aapt2 dump badging`` 回读，与常量比对，对不上构建失败。

这里钉住的是「两处不许漂移」和「校验函数真的会拦」，不是去跑完整构建
（那要 aapt2/d8/apksigner，几十秒，不适合放进单元测试）。
"""
from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

ANDROID = BASE_DIR / "android"
BUILD_PY = ANDROID / "build.py"
MANIFEST = ANDROID / "AndroidManifest.xml"


def _build_constants() -> dict:
    """从 build.py 的源码里取 VERSION_CODE / VERSION_NAME（不 import，免得执行构建逻辑）。"""
    tree = ast.parse(BUILD_PY.read_text(encoding="utf-8"))
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            if isinstance(tgt, ast.Name) and tgt.id in ("VERSION_CODE", "VERSION_NAME"):
                out[tgt.id] = ast.literal_eval(node.value)
    return out


def _manifest_versions() -> dict:
    """取 <manifest ...> 起始标签上的 versionCode / versionName。

    不能简单按 ">" 切第一个 —— 清单开头有一大段注释，注释里出现 ">" 就会把
    切点提前。所以先剥掉所有 XML 注释，再取第一个起始标签。
    """
    text = MANIFEST.read_text(encoding="utf-8")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    m_head = re.search(r"<manifest\b[^>]*>", text, flags=re.S)
    head = m_head.group(0) if m_head else ""
    code = re.search(r'android:versionCode="([^"]*)"', head)
    name = re.search(r'android:versionName="([^"]*)"', head)
    return {
        "VERSION_CODE": code.group(1) if code else None,
        "VERSION_NAME": name.group(1) if name else None,
    }


class TestVersionSingleSourceOfTruth(unittest.TestCase):

    def test_manifest_has_real_version(self):
        """清单里的值就是进包的值，不能留占位 0/0。"""
        m = _manifest_versions()
        self.assertIsNotNone(m["VERSION_NAME"], "清单里没有 versionName")
        self.assertNotEqual(m["VERSION_NAME"], "0",
                            "清单 versionName 是占位值 —— aapt2 会原样打进包，"
                            "手机上显示 0，且 PackageManager 也读到 0")
        self.assertIsNotNone(m["VERSION_CODE"], "清单里没有 versionCode")
        self.assertNotEqual(m["VERSION_CODE"], "0",
                            "清单 versionCode 是占位值 —— 会打出 versionCode='' 的畸形包")

    def test_manifest_matches_build_py(self):
        """清单与 build.py 常量必须一致，否则文件名和包内版本会各说一套。"""
        b = _build_constants()
        m = _manifest_versions()
        self.assertEqual(
            m["VERSION_NAME"], b.get("VERSION_NAME"),
            f"版本号漂移：AndroidManifest.xml 是 {m['VERSION_NAME']}，"
            f"build.py 是 {b.get('VERSION_NAME')}。"
            f"改版本必须两处同改 —— 文件名跟 build.py 走，装到手机上的跟清单走。")
        self.assertEqual(
            m["VERSION_CODE"], b.get("VERSION_CODE"),
            f"versionCode 漂移：清单 {m['VERSION_CODE']}，build.py {b.get('VERSION_CODE')}")

    def test_version_code_is_int_like(self):
        """versionCode 必须是正整数且随版本前进 —— 同版本号 Android 拒绝覆盖安装。"""
        m = _manifest_versions()
        self.assertTrue(m["VERSION_CODE"].isdigit(),
                        f"versionCode 不是整数：{m['VERSION_CODE']!r}")
        self.assertGreater(int(m["VERSION_CODE"]), 0)


class TestVerifyVersionCatchesDrift(unittest.TestCase):
    """verify_version() 必须真的会拦 —— 它是那次事故唯一能自动发现的地方。"""

    def _load(self):
        """摘出 verify_version 单独编译，顺带给它 die / VERSION_* 这些全局量。"""
        src = BUILD_PY.read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next((n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == "verify_version"), None)
        self.assertIsNotNone(fn, "build.py 里没有 verify_version —— 回读校验被删了？")
        mod = ast.Module(body=[fn], type_ignores=[])
        ns = {}

        class _Die(Exception):
            pass

        def die(msg):
            raise _Die(msg)

        class _Runner:
            def __init__(self, out):
                self._out = out

            def capture(self, args):
                return self._out

        ns.update({"re": re, "die": die, "print": lambda *a, **k: None,
                   "VERSION_CODE": "4", "VERSION_NAME": "0.4.1",
                   "Path": Path, "Runner": _Runner, "_Runner": _Runner})
        exec(compile(mod, str(BUILD_PY), "exec"), ns)   # noqa: S102
        return ns["verify_version"], ns["_Runner"], _Die

    GOOD = ("package: name='com.hbdxm.ddwx' versionCode='4' versionName='0.4.1' "
            "platformBuildVersionName='13'")

    def test_passes_when_matching(self):
        fn, Runner, _ = self._load()
        fn(Runner(self.GOOD), Path("."), Path("x.apk"))     # 不抛就是通过

    def test_catches_old_version_name(self):
        """这正是 2026-10-20 的现场：文件名 0.4.1，包里 0.3.0。"""
        fn, Runner, Die = self._load()
        bad = self.GOOD.replace("versionName='0.4.1'", "versionName='0.3.0'")
        with self.assertRaises(Die) as cm:
            fn(Runner(bad), Path("."), Path("x.apk"))
        self.assertIn("0.3.0", str(cm.exception))

    def test_catches_empty_version_code(self):
        """清单留占位时 aapt2 会产出 versionCode='' —— 必须拦。"""
        fn, Runner, Die = self._load()
        bad = self.GOOD.replace("versionCode='4'", "versionCode=''")
        with self.assertRaises(Die):
            fn(Runner(bad), Path("."), Path("x.apk"))


if __name__ == "__main__":
    unittest.main()
