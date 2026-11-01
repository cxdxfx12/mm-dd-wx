"""安卓端不许「就地」新建线程池（android/src/**/*.java）。

真机背景（0.5.33 → 0.5.34）
    ``AttachmentFetcher.takeShot()`` 每次都
    ``svc.takeScreenshot(display, Executors.newSingleThreadExecutor(), cb)``
    —— 回调用的 Executor 是现建的，而且**从不 shutdown**。
    抓一次附件会截十几到几十屏（蓝字兜底、绿色下载按钮、精滚复核都调它），
    屏屏一条线程，线程数无上限地涨。线程栈按 1MB 估，几十屏就是几十 MB
    虚拟内存，只增不减，跑久了 OOM 或被 ROM 判异常收掉无障碍服务。

    ``CollectorService`` 里还有两处同款：抓取兜底池和巡检兜底池，
    注释写着「抓完就关」，代码里其实没关。

规则
    线程池只能**建成字段**（``private static final ExecutorService X = Executors.new…``，
    或者 ``private ExecutorService pool;`` 声明 + 方法里 ``pool = Executors.new…``），
    因为字段的生命周期是显式的、可被 stop 方法统一 shutdown；
    写在方法参数里或赋给局部变量，就等于「谁也不知道它什么时候死」。

    这条规则不是要禁止建池，是要禁止**匿名地、每调用一次就建一个**。

为什么用文本扫描而不是 AST
    工程是 aapt2 + javac 手工编译、没有 Gradle，也就没有 Java 侧的单测框架。
    这几行正则足够抓住「出现在实参位置」这个唯一的事故形态，且不会因为
    编译器版本或格式变化而失效。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
ANDROID_SRC = BASE_DIR / "android" / "src"

# `Executors.newXxx(` 或全限定名 `java.util.concurrent.Executors.newXxx(`
NEW_POOL = re.compile(r"(?:[\w.]+\.)?Executors\s*\.\s*new\w+\s*\(")

# 字段声明：private [static] [final] <Type> NAME;  —— 只关心 Executor 家族
FIELD_DECL = re.compile(
    r"^\s*(?:private|public|protected)?\s*(?:static\s+)?(?:final\s+)?"
    r"((?:[\w.]+\.)?(?:[\w.]*ExecutorService|[\w.]*Executor))\s+(\w+)\s*;"
)
# 同一行就带初始化的字段声明
FIELD_DECL_INIT = re.compile(
    r"^\s*(?:private|public|protected)\s+(?:static\s+)?(?:final\s+)?"
    r"[\w.<>\[\]]+\s+(\w+)\s*=\s*$|"
    r"^\s*(?:private|public|protected)\s+(?:static\s+)?(?:final\s+)?"
    r"[\w.<>\[\]]+\s+(\w+)\s*=\s*\S"
)

IDENT_AT_END = re.compile(r"(\w+)\s*=\s*$")


def _java_files() -> list[Path]:
    if not ANDROID_SRC.is_dir():
        return []
    return sorted(ANDROID_SRC.rglob("*.java"))


def _code_lines(text: str) -> list[tuple[int, str, bool]]:
    """返回 (行号, 内容, 是否注释行)。块注释整段标记掉，避免注释里的示例代码误报。"""
    out = []
    in_block = False
    for i, raw in enumerate(text.splitlines(), start=1):
        s = raw.strip()
        if in_block:
            if "*/" in s:
                in_block = False
            out.append((i, raw, True))
            continue
        if s.startswith("/*"):
            if "*/" not in s:
                in_block = True
            out.append((i, raw, True))
            continue
        if s.startswith("//") or s.startswith("*"):
            out.append((i, raw, True))
            continue
        out.append((i, raw, False))
    return out


def _field_names(lines: list[tuple[int, str, bool]]) -> set[str]:
    names: set[str] = set()
    for _no, ln, is_comment in lines:
        if is_comment:
            continue
        m = FIELD_DECL.match(ln)
        if m:
            names.add(m.group(2))
            continue
        m = FIELD_DECL_INIT.match(ln)
        if m:
            names.add(m.group(1) or m.group(2))
    return names


def find_violations(path: Path) -> list[str]:
    """在单个文件里找「线程池不是建在字段上」的位置，返回人类可读的说明。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = _code_lines(text)
    fields = _field_names(lines)
    bad: list[str] = []

    for idx, (no, ln, is_comment) in enumerate(lines):
        if is_comment:
            continue
        m = NEW_POOL.search(ln)
        if not m:
            continue
        left = ln[:m.start()].rstrip()
        owner = None
        if left == "":
            # 跨行赋值：声明在上一行，以 "=" 结尾
            prev = lines[idx - 1][1].rstrip() if idx > 0 else ""
            idm = IDENT_AT_END.search(prev + " ") if prev else None
            owner = idm.group(1) if idm else None
        else:
            idm = IDENT_AT_END.search(left + " ")
            if idm:
                owner = idm.group(1)
            # 否则是实参位置（如 svc.takeScreenshot(display, Executors.new…, cb)）

        if owner is None:
            bad.append(
                f"{path.name}:{no} 线程池被建在实参位置/匿名处 —— {ln.strip()[:100]}"
            )
        elif owner not in fields:
            # 赋给局部变量：owner 不是本文件的 Executor 字段
            bad.append(
                f"{path.name}:{no} 线程池被赋给局部变量 {owner}（不是字段，没人负责 shutdown）"
                f" —— {ln.strip()[:100]}"
            )
    return bad


class ExecutorLeakGuard(unittest.TestCase):
    def test_android_src_exists(self):
        self.assertTrue(ANDROID_SRC.is_dir(), f"找不到安卓源码目录：{ANDROID_SRC}")
        self.assertTrue(_java_files(), "android/src 下没有 .java")

    def test_no_inline_executor(self):
        problems: list[str] = []
        for f in _java_files():
            problems.extend(find_violations(f))
        self.assertEqual(
            problems, [],
            "以下位置在方法里现建线程池、且没有回收路径，长跑必然泄漏线程：\n  "
            + "\n  ".join(problems)
            + "\n改法：提成 private static final 字段（daemon 线程），进程内复用。",
        )

    def test_shot_uses_shared_pool(self):
        """takeScreenshot 的回调池必须是字段，不能是当场 new 的那个。"""
        f = ANDROID_SRC / "com" / "hbdxm" / "ddwx" / "AttachmentFetcher.java"
        self.assertTrue(f.is_file(), f"找不到 {f}")
        lines = _code_lines(f.read_text(encoding="utf-8"))
        for idx, (no, ln, is_comment) in enumerate(lines):
            if is_comment or ".takeScreenshot(" not in ln:
                continue
            # 实参可能换行，往后看两行
            following = " ".join(x[1] for x in lines[idx: idx + 3])
            self.assertNotIn(
                "Executors.new", following,
                f"takeScreenshot 又用现建的线程池了（第 {no} 行）：每截一屏漏一条线程，"
                f"应传静态字段 SHOT_POOL",
            )
            self.assertIn(
                "SHOT_POOL", following,
                f"takeScreenshot（第 {no} 行）应当传共享的 SHOT_POOL",
            )

    def test_guard_catches_bad_snippet(self):
        """自检：护栏本身要能抓住事故形态，否则「全绿」毫无意义。"""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "Bad.java"
            p.write_text(
                "class Bad {\n"
                "    private static final ExecutorService GOOD =\n"
                "            Executors.newSingleThreadExecutor();\n"
                "    void shot(AccessibilityService svc) {\n"
                "        svc.takeScreenshot(1, Executors.newSingleThreadExecutor(), cb);\n"
                "    }\n"
                "    void other() {\n"
                "        ExecutorService tmp = Executors.newSingleThreadExecutor();\n"
                "    }\n"
                "}\n",
                encoding="utf-8",
            )
            bad = find_violations(p)
            self.assertEqual(len(bad), 2, f"应当抓到 2 处，实际：{bad}")
            self.assertTrue(any("实参位置" in b for b in bad), bad)
            self.assertTrue(any("局部变量" in b for b in bad), bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
