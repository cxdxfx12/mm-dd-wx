"""安装包「载荷组装」的排除规则测试（installer/build.py 的 copy_tree / want）。

为什么单独测这个
    构建脚本一度用 ``rglob`` 全量遍历后再按**名字**过滤，于是「排除 .git」
    只挡住了那个目录条目本身，``rglob`` 照样钻进对象库把文件一个个搬进
    payload —— 安装包被撑大，还因为 git 对象是只读的报 PermissionError，
    直接把构建搞挂。同样是 ``__pycache__`` 会被整体搬走。
    改成 ``os.walk`` 就地剪枝之后，这里用一棵模拟目录树把它钉住。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def _load_build_module():
    path = BASE_DIR / "installer" / "build.py"
    spec = importlib.util.spec_from_file_location("ddwx_build", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


build = _load_build_module()


class TestPayloadExclusions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.src = Path(self.tmp.name) / "src"
        self.dst = Path(self.tmp.name) / "dst"

    def _touch(self, rel: str) -> Path:
        p = self.src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
        return p

    def _tree(self) -> None:
        self._touch("run.py")
        self._touch("README.md")
        self._touch("config.yaml")
        self._touch("bridge/service.py")
        self._touch("bridge/__pycache__/service.cpython-312.pyc")
        self._touch("bridge/notify.pyc")
        self._touch("data/bridge.db")
        self._touch("data/logs/last.out")
        self._touch("app/_env.json")
        self._touch("app/gui.exe.spec")
        self._touch(".git/config")
        self._touch(".git/objects/02/3df18fb1976d471f30b884236caf86ba45a4d1")
        self._touch("tools/shots/dd.png")

    # ---- want() ----

    def test_want_filters_by_name_and_suffix(self):
        self._touch("a/__pycache__/x.cpython-312.pyc")
        self._touch("a/x.pyc")
        self._touch("a/x.pyo")
        self._touch("a/run.log")
        self._touch("a/_env.json")
        self._touch("a/喵喵.spec")
        for rel in ("a/__pycache__/x.cpython-312.pyc", "a/x.pyc", "a/x.pyo",
                    "a/run.log", "a/_env.json", "a/喵喵.spec"):
            with self.subTest(rel=rel):
                self.assertFalse(build.want(self.src / rel))
        self.assertTrue(build.want(self.src / "bridge/service.py"))

    # ---- copy_tree() ----

    def test_git_objects_are_not_copied(self):
        """核心回归：.git 下的文件绝不能进 payload。"""
        self._tree()
        copied = build.copy_tree(self.src, self.dst)
        self.assertFalse([c for c in copied if c.startswith(".git/")],
                         f".git 内容被复制了：{sorted(c for c in copied if c.startswith('.git/'))}")
        self.assertFalse((self.dst / ".git").exists())

    def test_pycache_is_pruned_not_just_skipped(self):
        self._tree()
        copied = build.copy_tree(self.src, self.dst)
        self.assertFalse([c for c in copied if "__pycache__" in c])
        self.assertFalse((self.dst / "bridge" / "__pycache__").exists())

    def test_running_data_and_caches_excluded(self):
        """data/ 这类运行期目录靠顶层排除清单挡掉（build_payload 会传进来）。"""
        self._tree()
        build.copy_tree(self.src, self.dst, top_excludes=build.EXCLUDE_TOP)
        self.assertFalse((self.dst / "data").exists())
        self.assertFalse((self.dst / "app" / "_env.json").exists())
        self.assertFalse((self.dst / "app" / "gui.exe.spec").exists())

    def test_program_files_are_copied(self):
        self._tree()
        copied = build.copy_tree(self.src, self.dst)
        for rel in ("run.py", "README.md", "bridge/service.py", "config.yaml"):
            with self.subTest(rel=rel):
                self.assertIn(rel, copied)
                self.assertTrue((self.dst / rel).is_file())

    def test_top_excludes_are_honoured(self):
        self._tree()
        copied = build.copy_tree(self.src, self.dst, top_excludes={"data"})
        self.assertFalse([c for c in copied if c.startswith("data/")])

    def test_readonly_files_are_copied(self):
        """只读文件要能复制过去（git 对象就是只读的，别在这里再炸一次）。"""
        import stat
        p = self._touch("bridge/readonly.py")
        p.chmod(p.stat().st_mode & ~stat.S_IWRITE)
        copied = build.copy_tree(self.src, self.dst)
        self.assertIn("bridge/readonly.py", copied)
        self.assertTrue((self.dst / "bridge" / "readonly.py").is_file())

    # ---- _force_unlink() ----

    def test_force_unlink_removes_readonly_file(self):
        import stat
        p = self._touch("only/read.py")
        p.chmod(p.stat().st_mode & ~stat.S_IWRITE)
        build._force_unlink(p)
        self.assertFalse(p.exists())

    # ---- 顶层排除清单 ----

    def test_critical_names_are_in_exclude_top(self):
        """这些一旦漏掉，要么泄露凭证、要么把 git 历史打进安装包。"""
        for name in (".git", "config.yaml", "data", "installer", "dist", "build"):
            with self.subTest(name=name):
                self.assertIn(name, build.EXCLUDE_TOP)
        self.assertIn(".git", build.EXCLUDE_DIR_NAMES)


if __name__ == "__main__":
    unittest.main()
