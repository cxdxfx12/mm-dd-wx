"""右侧「手机画面」面板：在转发桥界面里直接看手机、点手机。

为什么要它
----------
原来桥只看得到手机上报的控件树，看不到画面。钉钉一旦停在异常页
（附件预览空白页、搜索页、被别的东西挡住），人只能去猜，或者另开
一个 scrcpy 窗口来回切。这个面板把画面嵌在桥界面右侧，看到什么点什么。

怎么实现
--------
* 取画面：`adb exec-out screencap -p`（**只读**，不抢无障碍通道，
  和采集 App 完全共存；这一点和 uiautomator dump 不一样，dump 会顶掉
  App 的无障碍连接，别在面板里用）。
* 点画面：按下/抬起位移小于阈值算点击 → `input tap`；超过阈值算拖动
  → `input swipe`。右键 = 返回键。
* 所有 adb 调用都在后台线程里做，界面不会卡。

依赖：Pillow（桥的运行环境已装）、项目内 tools/platform-tools/adb.exe。
"""

from __future__ import annotations

import io
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

try:
    from PIL import Image, ImageTk
except Exception:                                    # noqa: BLE001
    Image = None
    ImageTk = None


# 与 gui.py 保持一致的配色（gui 会把它自己的常量传进来覆盖这里）
# adb.exe 是控制台程序：pythonw 下直接 subprocess 会**每调一次闪一个黑框**
# （截图 0.45 秒一次，屏幕就一直在闪）。CREATE_NO_WINDOW 让子进程完全不建
# 控制台窗口，同时把 stdin 接到 NUL，免得它等输入。
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _run_adb(cmd: list[str], timeout: float, capture: bool = True):
    return subprocess.run(
        cmd,
        capture_output=capture,
        timeout=timeout,
        creationflags=_NO_WINDOW,
        stdin=subprocess.DEVNULL,
    )


_DEFAULT_COLORS = dict(
    bg="#eef2f8", card="#ffffff", border="#d7dfec",
    text="#1f2937", muted="#7b869c", accent="#2563eb", red="#d92d20",
)


def find_adb() -> str | None:
    """按「程序目录 → PATH」的顺序找 adb。

    打包成 exe 后 ``__file__`` 指向 PyInstaller 的解压临时目录（_MEIPASS），
    从它往上推「项目根」推到的是 %TEMP%，什么都找不到 —— 冻结态必须改用
    **exe 自身所在目录**当根：安装版的 adb 在 ``<安装目录>\\tools\\platform-tools``，
    build.py 已把它打进载荷（2026-10-31 冒烟测试抓到面板永远停在
    「正在取第一帧…」的根因）。
    """
    if getattr(sys, "frozen", False):
        root = Path(sys.executable).resolve().parent      # <安装目录>
        cands = [
            root / "tools" / "platform-tools" / "adb.exe",
            root / "platform-tools" / "adb.exe",
        ]
    else:
        here = Path(__file__).resolve().parent            # .../app
        root = here.parent                                # 项目根
        cands = [
            root / "tools" / "platform-tools" / "adb.exe",
            here / "platform-tools" / "adb.exe",
            root / "platform-tools" / "adb.exe",
        ]
    for c in cands:
        if c.exists():
            return str(c)
    import shutil
    return shutil.which("adb")


class PhonePanel(tk.Frame):
    """手机画面 + 触控面板。"""

    def __init__(self, master, *, adb_path: str | None = None,
                 colors: dict | None = None, width: int = 316,
                 interval: float = 0.45, on_log=None, **kw):
        self.C = {**_DEFAULT_COLORS, **(colors or {})}
        super().__init__(master, bg=self.C["card"],
                         highlightbackground=self.C["border"],
                         highlightthickness=1, **kw)
        self.on_log = on_log
        self.adb = adb_path or find_adb()
        self.interval = interval
        self._q: queue.Queue = queue.Queue(maxsize=2)
        self._stop = threading.Event()
        # ⚠ 实时开关不要用 tk.BooleanVar 在取帧线程里 .get()：打包后的 Tcl 是
        # threaded 构建，跨线程访问变量必抛 RuntimeError(main thread is not in
        # main loop)，而且这个调用在 try 之外 —— 线程会**静默死亡**，画面永远
        # 停在「正在取第一帧…」（2026-11-04 实机截图抓到的就是这个形态）。
        # 所以真正的开关用 threading.Event，Checkbutton 的 command 在主线程同步它。
        self._live_ev = threading.Event()
        self._live_ev.set()
        self._live = tk.BooleanVar(value=True)
        self._photo = None            # 必须留引用，否则被 GC 掉画面变空白
        self._last_img = None         # 上一帧原图，窗口缩放时重绘用
        self._full = (1176, 2480)     # 手机原分辨率（首帧修正）
        self._box = (0, 0, 1, 1)      # 画面在画布上的显示区域
        self._press = None
        self._last_ms = 0
        self._fps = 0.0
        self._status_var = tk.StringVar(value="正在连接…")

        self._build_ui(width)
        if not self.adb:
            # 画布中央那行「正在取第一帧…」是建界面时写死的，不更新的话
            # 用户会以为还在连，其实根本没起线程 —— 顺手把它改掉。
            self.canvas.itemconfigure(
                self._hint_id, text="未找到 adb（tools\\platform-tools）")
            self._set_status("未找到 adb：把 platform-tools 放到 tools/ 下", err=True)
        elif Image is None:
            self._set_status("缺少 Pillow，装一下 pillow 才能显示画面", err=True)
        else:
            threading.Thread(target=self._loop, daemon=True).start()
            self.after(150, self._pump)

    # ------------------------------------------------------------ 界面

    def _build_ui(self, width: int) -> None:
        c = self.C
        head = tk.Frame(self, bg=c["card"])
        head.pack(fill="x", padx=12, pady=(10, 6))
        tk.Label(head, text="手机画面", bg=c["card"], fg=c["text"],
                 font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
        # 同上：tk 控件，避开 vista 主题高 DPI 空白文字问题
        self.live_chk = tk.Checkbutton(head, text="实时", variable=self._live,
                                       command=self._sync_live,
                                       bg=c["card"], fg=c["muted"],
                                       activebackground=c["card"],
                                       font=("Microsoft YaHei UI", 9),
                                       highlightthickness=0, bd=0)
        self.live_chk.pack(side="right")

        self.canvas = tk.Canvas(self, bg="#fbfcfe", highlightthickness=0,
                                width=width - 24, height=440)
        self.canvas.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", lambda e: self._send("input keyevent KEYCODE_BACK", "返回"))
        self.canvas.bind("<Configure>", lambda e: self._redraw())

        self._img_id = self.canvas.create_image(0, 0, anchor="nw")
        self._hint_id = self.canvas.create_text(
            width // 2, 24, text="正在取第一帧…", fill=c["muted"],
            font=("Microsoft YaHei UI", 9), anchor="n")

        bar = tk.Frame(self, bg=c["card"])
        bar.pack(fill="x", padx=10, pady=(0, 6))
        # ⚠ 这里必须用 tk.Button 而不是 ttk.Button：vista 主题在 Per-Monitor
        # DPI 感知下会把 ttk 按钮文字渲染成空白（0.48/0.49 实机截图：五个
        # 只有边框没有字的按钮）。tk.Button 走 GDI 绘制，不受主题影响。
        for text, cmd in (("返回", lambda: self._send("input keyevent KEYCODE_BACK", "返回")),
                          ("主页", lambda: self._send("input keyevent KEYCODE_HOME", "主页")),
                          ("上翻", lambda: self._swipe_quick("up")),
                          ("下翻", lambda: self._swipe_quick("down"))):
            tk.Button(bar, text=text, command=cmd, width=5,
                      bg=c["card"], fg=c["text"], activebackground=c["bg"],
                      activeforeground=c["accent"], relief="groove",
                      font=("Microsoft YaHei UI", 9),
                      highlightthickness=0, bd=1).pack(side="left", padx=(0, 4))
        tk.Button(bar, text="存图", command=self.save_shot, width=5,
                  bg=c["card"], fg=c["text"], activebackground=c["bg"],
                  activeforeground=c["accent"], relief="groove",
                  font=("Microsoft YaHei UI", 9),
                  highlightthickness=0, bd=1).pack(side="right")

        foot = tk.Frame(self, bg=c["card"])
        foot.pack(fill="x", padx=12, pady=(0, 8))
        self._status_lbl = tk.Label(
            foot, textvariable=self._status_var, bg=c["card"], fg=c["muted"],
            font=("Microsoft YaHei UI", 8), anchor="w",
            justify="left", wraplength=width - 30)
        self._status_lbl.pack(fill="x")

    def _set_status(self, text: str, *, err: bool = False) -> None:
        self._status_var.set(text)
        try:
            self._status_lbl.configure(fg=self.C["red"] if err else self.C["muted"])
        except Exception:                                  # noqa: BLE001
            pass

    def _sync_live(self) -> None:
        """Checkbutton 的 command 只在主线程跑：在这里同步线程用的 Event。"""
        if self._live.get():
            self._live_ev.set()
        else:
            self._live_ev.clear()

    # ------------------------------------------------------------ 取帧

    def _adb(self, args: list[str], timeout: float = 12) -> bytes:
        if not self.adb:
            return b""
        p = _run_adb([self.adb, *args], timeout=timeout)
        return p.stdout

    def _loop(self) -> None:
        # 整个线程体套一层兜底：任何没料到的异常都要落到队列里显示出来，
        # 绝不允许线程悄悄死掉 —— 死了画面就永远「正在取第一帧…」，人没法排障。
        try:
            self._loop_inner()
        except Exception as e:                             # noqa: BLE001
            try:
                if self._q.full():
                    self._q.get_nowait()
                self._q.put((None, f"取帧线程异常退出：{type(e).__name__}: {e}\n"
                                   f"请把这一行截图反馈给开发者"))
            except Exception:                              # noqa: BLE001
                pass

    def _loop_inner(self) -> None:
        fails = 0
        while not self._stop.is_set():
            if not self._live_ev.is_set():
                time.sleep(0.15)
                continue
            t0 = time.monotonic()
            try:
                raw = self._adb(["exec-out", "screencap", "-p"])
                if not raw.startswith(b"\x89PNG"):
                    detail = raw[:80].decode("utf-8", "replace").strip()
                    raise RuntimeError(f"不是 PNG（adb 回了 {len(raw)} 字节：{detail}）")
                im = Image.open(io.BytesIO(raw))
                im.load()
                fails = 0
                if self._q.full():
                    try:
                        self._q.get_nowait()
                    except queue.Empty:
                        pass
                self._q.put((im, time.monotonic() - t0))
            except Exception as e:                         # noqa: BLE001
                fails += 1
                if fails in (1, 5, 20):
                    try:
                        if self._q.full():
                            self._q.get_nowait()
                        self._q.put((None, f"{type(e).__name__}: {e}"))
                    except Exception:                      # noqa: BLE001
                        pass
                time.sleep(min(2.0, 0.3 * fails))
            rest = self.interval - (time.monotonic() - t0)
            if rest > 0:
                time.sleep(rest)

    def _pump(self) -> None:
        """主线程：把后台取到的帧贴到画布上。"""
        if self._stop.is_set():
            return
        try:
            got = None
            try:
                got = self._q.get_nowait()
            except queue.Empty:
                pass
            if got:
                im, meta = got
                if im is None:
                    self._set_status(f"取画面失败：{meta}\n检查 USB 连接 / 手机是否授权调试", err=True)
                else:
                    self._show(im, meta)
        except Exception as e:                             # noqa: BLE001
            # 贴帧出错也不能断 after 链 —— 断了面板就永远不再刷新。
            self._set_status(f"显示帧失败：{type(e).__name__}: {e}", err=True)
        self.after(120, self._pump)

    def _show(self, im, dt: float) -> None:
        self._full = im.size
        self._last_img = im
        cw = max(60, self.canvas.winfo_width())
        ch = max(60, self.canvas.winfo_height())
        w, h = im.size
        scale = min(cw / w, ch / h)
        dw, dh = max(1, int(w * scale)), max(1, int(h * scale))
        ox, oy = (cw - dw) // 2, (ch - dh) // 2
        self._box = (ox, oy, dw, dh)
        thumb = im.resize((dw, dh), Image.LANCZOS) if scale < 1 else im
        if thumb.mode not in ("RGB", "RGBA"):
            thumb = thumb.convert("RGB")
        self._photo = ImageTk.PhotoImage(thumb)
        self.canvas.coords(self._img_id, ox, oy)
        self.canvas.itemconfig(self._img_id, image=self._photo)
        self.canvas.coords(self._hint_id, cw // 2, dh + oy + 14)
        self.canvas.itemconfig(self._hint_id, text="")
        if dt <= 0:
            return
        if dt > 0.05:
            self._fps = 0.9 * self._fps + 0.1 * (1.0 / dt) if self._fps else 1.0 / dt
        self._last_ms = int(dt * 1000)
        self._set_status(f"{self._full[0]}×{self._full[1]} · 约 {self._fps:.1f} 帧/秒 · "
                         f"单帧 {self._last_ms} ms · 点画面=点手机，拖动=滑动，右键=返回")

    def _redraw(self) -> None:
        """画布尺寸变了（拉窗口）时按新尺寸重贴上一帧，不必等下一帧。"""
        if self._last_img is not None:
            try:
                self._show(self._last_img, 0)
            except Exception:                              # noqa: BLE001
                pass

    # ------------------------------------------------------------ 触控

    def _to_phone(self, ev) -> tuple[int, int] | None:
        ox, oy, dw, dh = self._box
        if not (ox <= ev.x <= ox + dw and oy <= ev.y <= oy + dh):
            return None
        px = int((ev.x - ox) / dw * self._full[0])
        py = int((ev.y - oy) / dh * self._full[1])
        return max(0, min(self._full[0] - 1, px)), max(0, min(self._full[1] - 1, py))

    def _on_press(self, ev) -> None:
        pt = self._to_phone(ev)
        self._press = (ev.x, ev.y, time.monotonic(), pt)

    def _on_release(self, ev) -> None:
        if not self._press:
            return
        x0, y0, t0, p0 = self._press
        self._press = None
        p1 = self._to_phone(ev)
        if p0 is None or p1 is None:
            return
        dx, dy = ev.x - x0, ev.y - y0
        if abs(dx) < 10 and abs(dy) < 10:
            self._send(f"input tap {p1[0]} {p1[1]}", f"点击 {p1}")
        else:
            ms = int(min(900, max(120, (time.monotonic() - t0) * 1000)))
            self._send(f"input swipe {p0[0]} {p0[1]} {p1[0]} {p1[1]} {ms}",
                       f"滑动 {p0}→{p1}")

    def _swipe_quick(self, direction: str) -> None:
        w, h = self._full
        x = w // 2
        if direction == "up":                 # 看更早的消息：手指从上往下拖
            a, b = int(h * 0.30), int(h * 0.72)
        else:
            a, b = int(h * 0.72), int(h * 0.30)
        self._send(f"input swipe {x} {a} {x} {b} 350", f"{direction} 翻屏")

    def _send(self, cmd: str, note: str = "") -> None:
        if not self.adb:
            self._set_status("没有 adb，点不了", err=True)
            return
        if self.on_log:
            self.on_log(f"[手机] {note or cmd}", "tool")

        def work():
            try:
                _run_adb([self.adb, "shell", cmd], timeout=15)
            except Exception as e:                         # noqa: BLE001
                if self.on_log:
                    self.on_log(f"[手机] 执行失败：{e}", "warn")

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ 其它

    def save_shot(self) -> Path | None:
        if self._photo is None:
            return None
        out = Path(__file__).resolve().parent.parent / "tmpd" / "phone"
        out.mkdir(parents=True, exist_ok=True)
        f = out / time.strftime("gui-%Y%m%d-%H%M%S.png")
        try:
            raw = self._adb(["exec-out", "screencap", "-p"])
            f.write_bytes(raw)
            if self.on_log:
                self.on_log(f"[手机] 已存图 {f}", "tool")
            return f
        except Exception as e:                             # noqa: BLE001
            if self.on_log:
                self.on_log(f"[手机] 存图失败：{e}", "warn")
            return None

    def stop(self) -> None:
        self._stop.set()
