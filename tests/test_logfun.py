"""日志俏皮话（bridge/logfun.py，2026-10-27 用户要求）。

三条规矩必须锁住：

  ① 只加在 INFO 上 —— WARNING / ERROR 一个字都不许贫（告警要一眼看懂）；
  ② 事实在前、尾巴在后 —— 界面计数器（app/gui.py 的 ``_RE_SENT`` 等）和
     tools/ 里的 grep 全靠前缀；
  ③ 每类事件多备几条 —— 天天看同一句照样烦。

第 ①② 条用源码扫描锁：警告/错误行里出现 tail( 就算破规矩。
"""

import re
import unittest

from bridge import logfun

BASE = __import__("pathlib").Path(__file__).resolve().parent.parent


class TestTailPools(unittest.TestCase):

    def test_every_pool_has_variants(self):
        """每类至少 2 条 —— 只有一条就成了「固定文案」，不如不加。"""
        for kind, pool in logfun._TAILS.items():
            self.assertGreaterEqual(len(pool), 2, kind)
            self.assertEqual(len(set(pool)), len(pool), f"{kind} 有重复文案")

    def test_tail_starts_with_separator(self):
        """尾巴自带前导分隔符，调用点拼在句尾才读得通。"""
        for kind in logfun._TAILS:
            self.assertIn(logfun.tail(kind)[0], "，（")

    def test_unknown_kind_is_empty_not_crash(self):
        """文案表漏了 key 也绝不能把主流程炸掉。"""
        self.assertEqual(logfun.tail("不存在的类别"), "")

    def test_variants_actually_vary(self):
        """多抽几次应该见到不止一种 —— 随机没接上就等于写死了。"""
        seen = {logfun.tail("sent") for _ in range(200)}
        self.assertGreater(len(seen), 1)


class TestRules(unittest.TestCase):
    """源码级规矩：WARNING/ERROR 不许接尾巴。"""

    @staticmethod
    def _calls(src: str):
        """把 ``log.xxx(`` 到配对右括号之间的整段抠出来（跨行算一条）。

        按行判会把多行调用的续行误当成「孤零零的 tail(」—— 第一版测试就
        是这么误报 10 处的。按括号配对切才作数。
        """
        for m in re.finditer(r"log\.(info|warning|error|exception)\(", src):
            i = m.end()
            depth = 1
            while i < len(src) and depth:
                if src[i] == "(":
                    depth += 1
                elif src[i] == ")":
                    depth -= 1
                i += 1
            yield m.group(1), src[m.start():i]

    def test_no_tail_on_warning_or_error(self):
        bad = []
        for path in (BASE / "bridge").rglob("*.py"):
            src = path.read_text(encoding="utf-8")
            for level, call in self._calls(src):
                if level in ("warning", "error", "exception") \
                        and "tail(" in call:
                    bad.append(f"{path.name}: {call.splitlines()[0][:60]}")
        self.assertEqual([], bad,
                         "规矩①破了：警告/错误日志不许加俏皮话（会稀释告警）")

    def test_info_does_get_tails(self):
        """反方向也要锁：加了机制却没人用，等于没加。"""
        used = [call for level, call in
                self._calls((BASE / "bridge" / "service.py")
                            .read_text(encoding="utf-8")) if "tail(" in call]
        self.assertTrue(used, "service.py 的投递日志没接上尾巴")

    def test_gui_counted_prefixes_untouched(self):
        """界面计数器靠这些前缀，一个字都不能动。"""
        gui = (BASE / "app" / "gui.py").read_text(encoding="utf-8")
        prefixes = re.findall(r'_RE_\w+ = re\.compile\(r"([^"]+)"', gui)
        self.assertTrue(prefixes, "gui.py 里的计数正则没抓到，测试本身失效了")
        src = "\n".join(p.read_text(encoding="utf-8")
                        for p in (BASE / "bridge").rglob("*.py"))
        for p in prefixes:
            # 正则里的捕获组不算字面量：只取组前面的字面片段来核对
            key = re.split(r"\(|\\d|\.\+", p)[0].strip()
            self.assertTrue(key, f"这条前缀没有字面部分，测试无从核对：{p}")
            self.assertIn(key, src, f"日志前缀被改了，界面计数会失灵：{key}")


if __name__ == "__main__":
    unittest.main()
