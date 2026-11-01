# -*- coding: utf-8 -*-
"""静态护栏：``except ... as exc`` 的变量不许被 lambda / 生成器带出去。

2026-11-01 21:0x 查出来的真 bug（app/gui.py 导出 CSV）：

    except Exception as exc:
        self.after(0, lambda: messagebox.showerror("导出失败", str(exc)))

Python 3 在 except 块结束时会 ``del exc``，而 ``after(0, ...)`` 的回调是**稍后**
在 Tk 主线程里跑的 —— 那时 exc 已经不在了，抛的是：

    NameError: cannot access free variable 'exc' where it is not associated
               with a value in enclosing scope

后果比原错误更糟：用户点「导出」，什么都没弹，错误只进了 stderr。GUI 里
没有任何提示，看起来就是「点了没反应」。

这类写法静态检查工具（pyflakes）能报，但项目没装那些工具，所以在这里用
AST 兜住：只要 except 块里的 lambda 引用了 except 变量，就判失败。
"""

import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCAN_DIRS = ("bridge", "app")


def _leaks(path: pathlib.Path) -> list[str]:
    """返回该文件里「except 变量逃进 lambda」的位置描述。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or not node.name:
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Lambda):
                continue
            used = {n.id for n in ast.walk(sub)
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
            if node.name in used:
                out.append(f"{path.name}:{sub.lineno} lambda 引用了 except 变量 "
                           f"{node.name!r}")
    return out


class ExceptVarLeakTest(unittest.TestCase):
    def test_no_except_var_captured_by_lambda(self):
        bad = []
        for d in SCAN_DIRS:
            for p in (ROOT / d).rglob("*.py"):
                bad.extend(_leaks(p))
        self.assertEqual(
            bad, [],
            "except 变量在块结束时会被删除，lambda 晚执行时拿不到它：" + "；".join(bad))

    def test_detector_catches_known_shape(self):
        """护栏本身要能抓到：否则「零告警」可能只是检查写错了。"""
        import tempfile

        src = (
            "def f():\n"
            "    try:\n"
            "        pass\n"
            "    except Exception as exc:\n"
            "        after(0, lambda: print(str(exc)))\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "sample.py"
            p.write_text(src, encoding="utf-8")
            self.assertTrue(_leaks(p), "护栏没抓到已知坏写法")

    def test_inline_use_is_fine(self):
        """在 except 块里当场求值（不是延后）是正确写法，不该误报。"""
        import tempfile

        src = (
            "def f():\n"
            "    try:\n"
            "        pass\n"
            "    except Exception as exc:\n"
            "        msg = str(exc)\n"
            "        after(0, lambda: print(msg))\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "ok.py"
            p.write_text(src, encoding="utf-8")
            self.assertEqual(_leaks(p), [])


if __name__ == "__main__":
    unittest.main()
