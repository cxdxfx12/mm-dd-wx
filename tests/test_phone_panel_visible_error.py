"""「手机画面」面板：失败必须看得见、Pillow 必须进包。

2026-11-06 现场事故（不是猜测，是实机截图 + 归档检查）：
  · 构建 venv 只装了 pyinstaller，没装 Pillow → 冻结主程序 PYZ 里
    PIL 模块数 = 0 → phone_panel 里 Image=None → **取帧线程根本不启动**；
  · 而失败原因只写在底部状态行，那行又被 expand 的 canvas 挤出可视区，
    于是画布永远「正在取第一帧…」，用户看到「没画面、也没报错」；
  · 同时 5037 端口没有任何连接，证实面板一次 adb 都没调过。
这组测试把两道防线钉死：构建期自检 + 运行期可见错误。
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PANEL = BASE / "app" / "phone_panel.py"
BUILD = BASE / "installer" / "build.py"


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def method(src: str, cls: str, name: str):
    """从源码里摘一个方法出来单独跑，不 import tkinter。"""
    tree = ast.parse(src)
    klass = next(n for n in tree.body
                 if isinstance(n, ast.ClassDef) and n.name == cls)
    fn = next(n for n in klass.body
              if isinstance(n, ast.FunctionDef) and n.name == name)
    ns: dict = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]),  # noqa: S102
                 "<ast>", "exec"), ns)
    return ns[name]


def method_src(src: str, cls: str, name: str) -> str:
    """取方法**原始源码文本**（不 unparse，免得引号样式被重写）。"""
    tree = ast.parse(src)
    klass = next(n for n in tree.body
                 if isinstance(n, ast.ClassDef) and n.name == cls)
    fn = next(n for n in klass.body
              if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(src, fn) or ""


class BuildGuards(unittest.TestCase):
    """构建机换人/换机器也不能再产出一个没有 Pillow 的主程序。"""

    def setUp(self):
        self.src = read(BUILD)

    def test_requires_pillow_before_build(self):
        self.assertIn('find_spec("PIL")', self.src,
                      "构建前必须检查构建解释器装了 Pillow")

    def test_requires_tkinter_too(self):
        self.assertIn('find_spec("tkinter")', self.src)

    def test_collect_all_pil(self):
        self.assertIn('"--collect-all", "PIL"', self.src,
                      "PIL 的 _imaging 扩展必须随包收集")

    def test_reads_back_the_frozen_pyz(self):
        for token in ("_assert_frozen_pil", "CArchiveReader",
                      "ZlibArchiveReader", "PIL.Image", "PIL.ImageTk",
                      "_imaging"):
            self.assertIn(token, self.src, f"缺归档自检要素：{token}")

    def test_build_calls_the_guard(self):
        body = self.src.split("def build_app_exe()", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("_assert_frozen_pil(out)", body)

    def test_docstring_warns_about_pillow(self):
        self.assertIn("pillow", self.src[:1200].lower())


class PanelVisibility(unittest.TestCase):
    """失败信息必须画在画布中央，不能只写底部状态行。"""

    def setUp(self):
        self.src = read(PANEL)

    def test_show_error_exists(self):
        self.assertIn("def _show_error", self.src)

    def test_show_error_covers_the_hint(self):
        code = method_src(self.src, "PhonePanel", "_show_error")
        for token in ("_hint_id", 'anchor="center"', 'self.C["red"]',
                      'image=""', "width="):
            self.assertIn(token, code, f"_show_error 少了 {token}")

    def test_init_both_failure_branches_show_error(self):
        init = method_src(self.src, "PhonePanel", "__init__")
        self.assertGreaterEqual(init.count("self._show_error("), 2,
                                "没 adb 和缺 Pillow 两条分支都要在画布上报错")

    def test_pump_shows_error_when_frame_is_none(self):
        pump = method_src(self.src, "PhonePanel", "_pump")
        self.assertIn("self._show_error(", pump)
        self.assertIn("转发走 WiFi", pump,
                      "要告诉用户消息转发不受影响，别让人以为整个桥坏了")

    def test_success_restores_hint_style(self):
        show = method_src(self.src, "PhonePanel", "_show")
        for token in ('text=""', 'anchor="n"', "width=0"):
            self.assertIn(token, show, "出图成功后要把红色错误样式复原")

    def test_bottom_rows_pack_before_canvas(self):
        ui = method_src(self.src, "PhonePanel", "_build_ui")
        self.assertIn('side="bottom"', ui,
                      "底部状态行要先占位，否则被 expand 的 canvas 挤没")
        i_bottom = ui.find("bottom.pack")
        i_canvas = ui.find("self.canvas.pack")
        self.assertGreater(i_canvas, i_bottom,
                           "canvas 必须最后 pack，否则 foot 又会被挤掉")

    def test_capture_keeps_stderr(self):
        loop = method_src(self.src, "PhonePanel", "_loop_inner")
        self.assertIn("_adb_capture", loop)
        self.assertIn("_device_problem", loop)
        self.assertIn("err", loop, "adb 把「没设备」写在 stderr，别丢")

    def test_error_text_has_actionable_hint(self):
        self.assertIn("允许 USB 调试", self.src)
        self.assertIn("adb connect", self.src)


class FakePanel:
    """只提供 _device_problem 需要的 _adb。"""

    def __init__(self, out: str):
        self.out = out

    def _adb(self, args, timeout=12):        # noqa: ARG002
        return self.out.encode("utf-8")


class DeviceProblemParsing(unittest.TestCase):
    """adb devices 的四种形态要给人话结论，不是「不是 PNG（0 字节）」。"""

    def setUp(self):
        self.fn = method(read(PANEL), "PhonePanel", "_device_problem")

    def test_device_online(self):
        out = "List of devices attached\nPAL-AL00\tdevice product:PAL\n\n"
        self.assertIsNone(self.fn(FakePanel(out)))

    def test_no_device(self):
        out = "List of devices attached\n\n"
        msg = self.fn(FakePanel(out))
        self.assertIn("没找到手机", msg)
        self.assertIn("USB", msg)

    def test_unauthorized(self):
        out = "List of devices attached\nPAL-AL00\tunauthorized\n\n"
        self.assertIn("允许", self.fn(FakePanel(out)))

    def test_offline(self):
        out = "List of devices attached\nPAL-AL00\toffline\n\n"
        self.assertIn("offline", self.fn(FakePanel(out)))


if __name__ == "__main__":
    unittest.main()
