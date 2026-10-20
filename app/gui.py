"""「钉钉 → 微信 消息转发桥」桌面主程序（标准 Windows 窗口界面）。

架构
    GUI 进程（本文件）只依赖标准库 + tkinter：
        * 负责 **界面、按钮、日志展示、统计、设置**；
        * 所有采集/投递的活儿都交给 ``app/bridge_worker.py`` 子进程
          （由带重依赖的解释器运行），GUI 通过管道实时读它的日志。

    这样界面永远秒开，重依赖装在哪儿、装不装得上，都不影响窗口本身。

启动方式
    python app/gui.py                      # 任意解释器（没 tkinter 会自动换）
    pythonw app/gui.py                     # 无黑框，推荐
    双击 「启动消息转发桥.bat」 / 桌面快捷方式
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import paths                                            # noqa: E402

APP_DIR = paths.APP_DIR
BASE_DIR = paths.BASE_DIR
for _p in (str(APP_DIR), str(BASE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 版本号**只有一个来源**：bridge/__init__.py。
# 安装包里的版本也由 installer/build.py 从同一处注入，别在这个文件里写死。
from bridge import __version__ as APP_VERSION          # noqa: E402


def _notify_robots(n: dict) -> list[str]:
    """配置段 -> 已填地址的机器人清单（只用于界面上的「配了没」判断）。

    真正的解析与发送在 bridge/notify.py，这里刻意做得宽松：
    ``webhooks`` 列表（新的多机器人写法）优先，为空时回落到单个 ``webhook``。
    """
    items = [x for x in (n.get("webhooks") or []) if str(x or "").strip()]
    if items:
        return items
    one = str(n.get("webhook") or "").strip()
    return [one] if one else []


def _ensure_tkinter():
    """当前解释器不带 tkinter 时，找一个带的并重启自己（自愈）。"""
    try:
        import tkinter  # noqa: F401
        return
    except ImportError:
        pass
    if getattr(sys, "frozen", False):
        # 打包版内置了 tkinter，走到这里说明安装包本身有问题
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, "程序自带的图形界面库加载失败，安装包可能已损坏。\n\n"
                   "请用原安装程序重新安装一次。",
                "喵喵钉钉-微信消息转发桥", 0x10)
        except Exception:
            pass
        sys.exit(2)
    info = paths.resolve(force=True)
    exe = paths.windowed(info["gui"]) if info.get("gui_ok") else ""
    if exe and Path(exe).resolve() != Path(sys.executable).resolve():
        subprocess.Popen([exe, str(Path(__file__).resolve()), *sys.argv[1:]],
                         cwd=str(BASE_DIR))
        sys.exit(0)
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0, "本机没有找到带 tkinter（图形界面）的 Python。\n\n"
               "请安装 Python 官方安装包（3.10 及以上，默认自带 tkinter）后重试。",
            "消息转发桥", 0x10)
    except Exception:
        pass
    sys.exit(2)


_ensure_tkinter()

import tkinter as tk                                    # noqa: E402
from tkinter import messagebox, filedialog, ttk         # noqa: E402
from tkinter import font as tkfont                       # noqa: E402

import cfgfile                                          # noqa: E402
import icon                                             # noqa: E402
from proc import ProcRunner, parse_line                 # noqa: E402

CONFIG_PATH = paths.config_path()
LOG_DIR = paths.data_dir("logs")

# ----------------------------------------------------------------- 主题

BG = "#eef1f7"
CARD = "#ffffff"
BORDER = "#e2e7f0"
TEXT = "#1c2333"
MUTED = "#69728c"
ACCENT = "#2f6be4"
ACCENT_DARK = "#2456bd"
GREEN = "#0f9d58"
AMBER = "#d99000"
RED = "#e5484d"
HEADER_BG = "#1e2a4a"
HEADER_BG2 = "#2b3a67"

# ---- 主按钮（开始运行 / 停止）的禁用态 ----
# 设计原则：**能不能点，靠「实心 vs 空心」区分，不靠颜色深浅**。
#   可用   → 实心彩底 + 白字（唯一强调色，视线落点）
#   不可用 → 浅灰底 + 中灰字 + 细边框（"按钮存在但按不动"）
# 早先的做法是「把颜色调淡」（浅蓝 #9db8e8 / 浅粉 #eeb4b6）当禁用态，
# 结果两个按钮同屏都是彩色实心块，既没有主次、也看不出谁不能点。
BTN_DISABLED_BG = "#f1f3f7"
BTN_DISABLED_FG = "#9aa3b8"
BTN_DISABLED_BORDER = "#dde2ec"

F_UI = ("Microsoft YaHei UI", 10)
F_SMALL = ("Microsoft YaHei UI", 9)
F_COPY = ("Microsoft YaHei UI", 8)
F_TITLE = ("Microsoft YaHei UI", 16, "bold")
F_SUB = ("Microsoft YaHei UI", 9)
F_CARD_NUM = ("Microsoft YaHei UI", 17, "bold")
F_LOG = ("Microsoft YaHei UI", 9)

# 版权信息（底部信息条 / 关于对话框 / 安装程序共用）
COPYRIGHT = "杭州喵喵至家网络有限公司  www.hbdxm.com"
APP_TITLE = "喵喵钉钉-微信消息转发桥"

# 数据来源（= config.yaml 的 dingtalk.mode）。这是给用户看的两个选项，
# 顺序即下拉框顺序。
SOURCE_LABELS = {
    "ocr": "PC 钉钉 · 截图识别",
    "android": "手机钉钉 · 无障碍",
}


def source_label(mode: str) -> str:
    """把 mode 翻成人话；不认识的值原样显示（别把旧配置藏起来）。"""
    return SOURCE_LABELS.get(mode, mode or "-")


def label_source(label: str) -> str:
    for mode, text in SOURCE_LABELS.items():
        if text == label:
            return mode
    return ""


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1180x720")
        self.minsize(1000, 620)
        self.configure(bg=BG)
        self._set_taskbar_icon()

        self.env: dict = {}
        self.cfg: dict = {}
        self.state = "idle"            # idle|checking|running|stopping
        self.counters = dict(recognized=0, sent=0, failed=0, skipped=0,
                             dup=0, filtered=0)
        self.db_totals: dict = {}
        self._busy = False             # 一次性任务（自检/预览等）占用标志
        self._log_fh = None
        self._icon_img = None
        self._settings_win = None

        self.runner = ProcRunner(on_line=self._on_worker_line,
                                 on_exit=self._on_worker_exit)
        self._build_style()
        self._build_menu()
        self._build_ui()
        self._open_log_file()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(250, self._boot)

    # ================================================== 启动

    def _boot(self):
        self._log("正在检测运行环境（解释器与依赖）…", "sys")
        threading.Thread(target=self._boot_worker, daemon=True).start()

    def _boot_worker(self, force: bool = False, with_check: bool = True):
        info = paths.resolve(force=force)
        self.env = info
        cfg = self._load_cfg()
        if cfg is not None:
            self.cfg = cfg
        self.after(0, lambda: self._after_env(with_check))

    def _after_env(self, with_check: bool = True):
        gui_ok = bool(self.env.get("gui_ok"))
        worker_miss = self.env.get("worker_missing") or []
        self._log(f"界面解释器：{self.env.get('gui')}", "sys")
        self._log(f"工作解释器：{self.env.get('worker')}", "sys")
        if worker_miss:
            miss = "、".join(worker_miss)
            self._log(f"⚠ 工作解释器缺少依赖：{miss}"
                      f"（采集/投递功能会不可用）", "warn")
        self._refresh_env_chips()
        self._apply_cfg_to_ui()
        self._start_db_loop()
        # 启动即刷新一次按钮态：默认 state=idle，此时「开始运行」应为实心蓝、
        # 「停止」应为灰色空心。不刷的话首帧停在 `_accent_btn()` 给的实心红 —
        # 用户会看到一个「停止」可点但点了没用的假象，正好是本次要修的那个毛病。
        self._refresh_state()
        if not gui_ok:
            self._log("⚠ 未找到带 tkinter 的解释器（不应出现，界面仍在运行）", "warn")
        if with_check:
            self.log_message("首次自检开始（只读，不发送任何消息）")
            self.run_check(silent=True)

    # ================================================== 样式与界面

    def _set_taskbar_icon(self):
        try:
            ico = APP_DIR / "app.ico"
            if ico.exists():
                self.iconbitmap(str(ico))
            else:
                icon.build()
                self.iconbitmap(str(ico))
        except Exception:
            pass

    def _build_style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("vista")
        except Exception:
            pass
        st.configure("TButton", font=F_UI, padding=(14, 8))
        st.configure("Accent.TButton", font=("Microsoft YaHei UI", 10, "bold"),
                     foreground="#ffffff", background=ACCENT, padding=(18, 9))
        st.map("Accent.TButton",
               background=[("disabled", "#9db8e8"), ("pressed", ACCENT_DARK),
                           ("active", ACCENT_DARK)],
               foreground=[("disabled", "#eef2fb")])
        st.configure("Danger.TButton", font=("Microsoft YaHei UI", 10, "bold"),
                     foreground="#ffffff", background=RED, padding=(18, 9))
        st.map("Danger.TButton",
               background=[("disabled", "#eeb4b6"), ("pressed", "#c93a3f"),
                           ("active", "#c93a3f")])
        st.configure("Card.TFrame", background=CARD)
        st.configure("Card.TLabel", background=CARD, foreground=TEXT, font=F_UI)
        st.configure("CardMuted.TLabel", background=CARD, foreground=MUTED,
                     font=F_SMALL)
        st.configure("CardNum.TLabel", background=CARD, foreground=TEXT,
                     font=F_CARD_NUM)
        st.configure("BG.TFrame", background=BG)
        st.configure("BG.TLabel", background=BG, foreground=TEXT, font=F_UI)

    def _accent_btn(self, parent, text: str, command, color: str,
                    active: str, width: int = 0) -> tk.Button:
        """实心彩色按钮（可用态）。

        不用 ttk.Button：vista 主题会忽略 background，样式配置出来是白底白字。

        禁用态由 `_paint_btn()` 统一绘制（浅灰底 + 中灰字 + 细边框），
        这里只负责可用态的外观。

        `width>0` 时锁死按钮像素宽，避免换文案（▶→··）时按钮伸缩抽搐。
        注意 tk.Button 的 width 单位是「字符」，且 tk 内部按「字宽预算」而非
        实际渲染宽度换算 —— 未挂到父容器前 winfo_reqwidth() 拿到的不是像素。
        所以这里不二次校正，直接按 (px − 固定内边距) / 实测单字宽 求字符数。
        """
        btn = tk.Button(parent, text=text, command=command, bg=color, fg="#ffffff",
                        activebackground=active, activeforeground="#ffffff",
                        relief="flat", bd=0, font=("Microsoft YaHei UI", 10, "bold"),
                        padx=15, pady=8, cursor="hand2",
                        disabledforeground=BTN_DISABLED_FG)
        if width:
            try:
                unit = tkfont.Font(font=("Microsoft YaHei UI", 10, "bold")
                                   ).measure("0") or 1
                inner = width - 15 * 2 - 2      # 减 padx*2 与边框/highlight
                btn.configure(width=max(1, round(inner / unit)))
            except Exception:
                pass
        return btn

    @staticmethod
    def _paint_btn(btn: tk.Button, enabled: bool, color: str,
                   active: str) -> None:
        """按可用/禁用重绘主按钮。

        ⚠ tk.Button 的 `disabled` 只管住点击，不改变底色 —— 必须手动重绘，
        否则禁用按钮仍是实心彩色，用户以为能点（点下去没反应，更困惑）。

        ⚠ 几何量（bd / highlightthickness / padx / pady）**两态必须完全一致**：
        tk 按「文字 + padx*2 + bd*2 + highlightthickness*2」算请求尺寸，任何一项
        在两态之间变化，按钮就会跳一下。所以两态都用 bd=1 + highlightthickness=1，
        只是禁用态把 highlight 环染成边框色（视觉上当 1px 描边用）。
        """
        if enabled:
            btn.configure(bg=color, fg="#ffffff", activebackground=active,
                          activeforeground="#ffffff", relief="flat", bd=1,
                          highlightthickness=1, highlightbackground=color,
                          highlightcolor=color, cursor="hand2",
                          padx=15, pady=8)
        else:
            btn.configure(bg=BTN_DISABLED_BG, fg=BTN_DISABLED_FG,
                          activebackground=BTN_DISABLED_BG,
                          activeforeground=BTN_DISABLED_FG,
                          relief="solid", bd=1, cursor="arrow",
                          highlightthickness=1,
                          highlightbackground=BTN_DISABLED_BORDER,
                          highlightcolor=BTN_DISABLED_BORDER,
                          padx=15, pady=8)

    def _build_menu(self):
        """顶部菜单栏：让「设置」等入口更符合 Windows 软件的使用习惯。"""
        bar = tk.Menu(self)

        m_run = tk.Menu(bar, tearoff=0)
        m_run.add_command(label="开始运行", accelerator="F5", command=self.start_live)
        m_run.add_command(label="停止运行", accelerator="F6", command=self.stop_live)
        m_run.add_separator()
        m_run.add_command(label="自检（采集端 + 投递端）", command=self.run_check)
        m_run.add_separator()
        m_run.add_command(label="退出", command=self._on_close)
        bar.add_cascade(label="运行", menu=m_run)

        m_set = tk.Menu(bar, tearoff=0)
        m_set.add_command(label="设置中心…", accelerator="Ctrl+,", command=self.open_settings)
        m_set.add_separator()
        m_set.add_command(label="打开 config.yaml", command=self.open_config_file)
        m_set.add_command(label="重新检测运行环境", command=self.redetect_env)
        bar.add_cascade(label="设置", menu=m_set)

        m_tool = tk.Menu(bar, tearoff=0)
        m_tool.add_command(label="钉钉窗口预览（只读）", command=self.dingtalk_preview)
        m_tool.add_command(label="转发一条（预览后确认）", command=self.forward_one_dialog)
        m_tool.add_command(label="转发最近 N 条（预览后确认）…",
                           command=self.forward_recent_dialog)
        m_tool.add_separator()
        m_tool.add_command(label="测试钉钉通知", command=self.notify_test)
        m_tool.add_command(label="发送消息到钉钉群…", command=self.notify_say)
        m_tool.add_command(label="发送运行报告到钉钉", command=self.notify_report)
        m_tool.add_separator()
        m_tool.add_command(label="消息记录", command=self.show_records)
        m_tool.add_command(label="导出 CSV", command=self.export_csv)
        m_tool.add_separator()
        m_tool.add_command(label="打开数据目录", command=self._open_data_dir)
        m_tool.add_command(label="打开日志目录", command=self._open_log_dir)
        bar.add_cascade(label="工具", menu=m_tool)

        m_help = tk.Menu(bar, tearoff=0)
        m_help.add_command(label="使用说明（README）", command=self._open_readme)
        m_help.add_command(label="关于", command=self._about)
        bar.add_cascade(label="帮助", menu=m_help)

        self.configure(menu=bar)
        self.bind("<F5>", lambda e: self.start_live())
        self.bind("<F6>", lambda e: self.stop_live())
        self.bind("<Control-comma>", lambda e: self.open_settings())

    def _open_readme(self):
        for name in ("README.md", "README-使用说明.md", "README.txt"):
            p = BASE_DIR / name
            if p.exists():
                self._open_path(p)
                return
        messagebox.showinfo("提示", "没有找到说明文档。", parent=self)

    def _about(self):
        messagebox.showinfo(
            "关于",
            f"喵喵钉钉-微信消息转发桥　版本 {APP_VERSION}\n\n"
            "采集：钉钉窗口截屏 + 离线 OCR（不依赖开放平台权限）\n"
            "投递：微信 4.x 控件自动化\n\n"
            f"配置文件：{CONFIG_PATH}\n"
            f"数据目录：{paths.data_dir()}\n"
            f"工作解释器：{self.env.get('worker') or '-'}\n\n"
            f"{COPYRIGHT}",
            parent=self)

    def _card(self, parent, title: str) -> tuple[ttk.Frame, ttk.Frame]:
        box = tk.Frame(parent, bg=CARD, highlightbackground=BORDER,
                       highlightthickness=1)
        head = tk.Label(box, text=title, bg=CARD, fg=MUTED, font=F_SMALL,
                        anchor="w")
        head.pack(fill="x", padx=14, pady=(10, 2))
        body = tk.Frame(box, bg=CARD)
        body.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        return box, body

    def _stat_row(self, body: ttk.Frame, label: str, row: int) -> tk.Label:
        lab = tk.Label(body, text=label, bg=CARD, fg=MUTED, font=F_SMALL)
        lab.grid(row=row, column=0, sticky="w", pady=2)
        val = tk.Label(body, text="0", bg=CARD, fg=TEXT,
                       font=("Microsoft YaHei UI", 11, "bold"))
        val.grid(row=row, column=1, sticky="e", pady=2)
        body.columnconfigure(1, weight=1)
        return val

    def _chip(self, parent, label: str) -> tuple[tk.Canvas, tk.Label]:
        dot = tk.Canvas(parent, width=12, height=12, bg=HEADER_BG,
                        highlightthickness=0)
        dot.create_oval(2, 2, 10, 10, fill="#8b93a8", outline="")
        txt = tk.Label(parent, text=label, bg=HEADER_BG, fg="#c9d2e8", font=F_SUB)
        return dot, txt

    def _set_chip(self, dot: tk.Canvas, txt: tk.Label, kind: str, text: str):
        color = {"ok": "#4ade80", "warn": "#fbbf24", "err": "#f87171",
                 "none": "#8b93a8"}.get(kind, "#8b93a8")
        dot.itemconfigure(1, fill=color)
        txt.configure(text=text)

    def _build_ui(self):
        # ---------------- 顶栏 ----------------
        header = tk.Frame(self, bg=HEADER_BG, height=86)
        header.pack(fill="x")
        header.pack_propagate(False)

        png = APP_DIR / "icon_64.png"
        if png.exists():
            try:
                self._icon_img = tk.PhotoImage(file=str(png))
                tk.Label(header, image=self._icon_img, bg=HEADER_BG
                         ).pack(side="left", padx=(18, 12), pady=12)
            except Exception:
                pass

        title_box = tk.Frame(header, bg=HEADER_BG)
        title_box.pack(side="left", pady=14)
        tk.Label(title_box, text=APP_TITLE, bg=HEADER_BG,
                 fg="#ffffff", font=F_TITLE).pack(anchor="w")
        self.subtitle = tk.Label(title_box, text="正在加载配置…", bg=HEADER_BG,
                                 fg="#93a1c4", font=F_SUB)
        self.subtitle.pack(anchor="w", pady=(3, 0))

        chips = tk.Frame(header, bg=HEADER_BG)
        chips.pack(side="right", padx=18)
        self.run_dot, self.run_txt = self._chip(chips, "● 已停止")
        self.src_dot, self.src_txt = self._chip(chips, "● 采集端 待检测")
        self.dst_dot, self.dst_txt = self._chip(chips, "● 投递端 待检测")
        for d, t in ((self.run_dot, self.run_txt), (self.src_dot, self.src_txt),
                     (self.dst_dot, self.dst_txt)):
            d.pack(side="right", padx=(6, 0), pady=8)
            t.pack(side="right", pady=8)

        # ---------------- 试运行提示条 ----------------
        self.drybar = tk.Label(self, text="", bg="#fff5da", fg="#8a6100",
                               font=("Microsoft YaHei UI", 10, "bold"),
                               anchor="w", padx=18, pady=7)

        # ---------------- 主体 ----------------
        self.main_frame = main = tk.Frame(self, bg=BG)
        main.pack(fill="both", expand=True)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        left_outer = tk.Frame(main, bg=BG)
        left_outer.grid(row=0, column=0, sticky="nsw", padx=(14, 8), pady=14)
        # 左列做成可滚动：窗口不够高时配置卡片也不会被截掉
        left_canvas = tk.Canvas(left_outer, bg=BG, highlightthickness=0, width=252)
        left_bar = ttk.Scrollbar(left_outer, orient="vertical",
                                 command=left_canvas.yview)
        left = tk.Frame(left_canvas, bg=BG)
        _win = left_canvas.create_window((0, 0), window=left, anchor="nw")
        left_canvas.configure(yscrollcommand=left_bar.set)
        left_canvas.pack(side="left", fill="both", expand=True)
        left_bar.pack(side="right", fill="y")
        left.bind("<Configure>", lambda e: left_canvas.configure(
            scrollregion=left_canvas.bbox("all")))
        left_canvas.bind("<Enter>", lambda e: left_canvas.bind_all(
            "<MouseWheel>", lambda ev: left_canvas.yview_scroll(
                -1 * (ev.delta // 120), "units")))
        left_canvas.bind("<Leave>", lambda e: left_canvas.unbind_all("<MouseWheel>"))

        box_run, body_run = self._card(left, "本次运行")
        box_run.pack(fill="x", pady=(0, 12))
        self.v_rec = self._stat_row(body_run, "识别新消息", 0)
        self.v_sent = self._stat_row(body_run, "已投递微信", 1)
        self.v_fail = self._stat_row(body_run, "投递失败", 2)
        self.v_skip = self._stat_row(body_run, "跳过跨天旧消息", 3)
        self.v_dup = self._stat_row(body_run, "重复（去重拦下）", 4)
        self.v_filt = self._stat_row(body_run, "未命中规则", 5)
        self.v_elapsed = self._stat_row(body_run, "已运行", 6)

        box_db, body_db = self._card(left, "累计统计")
        box_db.pack(fill="x", pady=(0, 12))
        self.v_db_msg = self._stat_row(body_db, "累计入库消息", 0)
        self.v_db_ok = self._stat_row(body_db, "累计投递成功", 1)
        self.v_db_bad = self._stat_row(body_db, "累计投递失败", 2)

        box_cfg, body_cfg = self._card(left, "当前配置")
        box_cfg.pack(fill="x")
        self.cfg_rows: dict[str, tuple[tk.Label, tk.Label]] = {}
        for i, key in enumerate(("监控会话", "转发目标", "轮询间隔", "发送间隔",
                                 "时间过滤")):
            lab = tk.Label(body_cfg, text=key, bg=CARD, fg=MUTED, font=F_SMALL)
            lab.grid(row=i, column=0, sticky="w", pady=2)
            val = tk.Label(body_cfg, text="-", bg=CARD, fg=TEXT, font=F_SMALL,
                           anchor="e", wraplength=150, justify="right")
            val.grid(row=i, column=1, sticky="e", pady=2)
            body_cfg.columnconfigure(1, weight=1)
            self.cfg_rows[key] = (lab, val)

        # 数据来源做成**可切换的下拉**而不是一行只读文字：这是「数据从哪来」
        # 的唯一开关，藏在设置中心里没人找得到（只能手改 config.yaml 时，
        # 用户根本不知道有这个选项）。
        row = 5
        tk.Label(body_cfg, text="数据来源", bg=CARD, fg=MUTED,
                 font=F_SMALL).grid(row=row, column=0, sticky="w", pady=2)
        self.src_var = tk.StringVar()
        self.src_combo = ttk.Combobox(
            body_cfg, textvariable=self.src_var, state="readonly", width=17,
            values=list(SOURCE_LABELS.values()), font=F_SMALL)
        self.src_combo.grid(row=row, column=1, sticky="e", pady=1)
        self.src_combo.bind("<<ComboboxSelected>>", self._on_source_change)

        lab = tk.Label(body_cfg, text="钉钉通知", bg=CARD, fg=MUTED, font=F_SMALL)
        lab.grid(row=6, column=0, sticky="w", pady=2)
        val = tk.Label(body_cfg, text="-", bg=CARD, fg=TEXT, font=F_SMALL,
                       anchor="e", wraplength=150, justify="right")
        val.grid(row=6, column=1, sticky="e", pady=2)
        self.cfg_rows["钉钉通知"] = (lab, val)

        # ---------------- 日志区 ----------------
        right = tk.Frame(main, bg=CARD, highlightbackground=BORDER,
                         highlightthickness=1)
        right.grid(row=0, column=1, sticky="nsew", padx=(0, 14), pady=14)
        head = tk.Frame(right, bg=CARD)
        head.pack(fill="x", padx=14, pady=(10, 6))
        tk.Label(head, text="运行日志", bg=CARD, fg=TEXT,
                 font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
        self.autoscroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(head, text="自动滚动", variable=self.autoscroll,
                        style="Card.TCheckbutton").pack(side="right", padx=6)
        ttk.Button(head, text="重新检测环境", width=12,
                   command=self.redetect_env).pack(side="right", padx=4)
        ttk.Button(head, text="清空", width=6,
                   command=self._clear_log).pack(side="right", padx=4)
        ttk.Button(head, text="打开日志文件", width=12,
                   command=self._open_log_dir).pack(side="right", padx=4)

        wrap = tk.Frame(right, bg=CARD)
        wrap.pack(fill="both", expand=True, padx=(14, 14), pady=(0, 14))
        self.log_txt = tk.Text(wrap, bg="#fbfcfe", fg=TEXT, font=F_LOG,
                               relief="flat", wrap="word", state="disabled",
                               padx=12, pady=10, cursor="arrow",
                               selectbackground="#d6e4ff")
        bar = ttk.Scrollbar(wrap, command=self.log_txt.yview)
        self.log_txt.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.log_txt.pack(side="left", fill="both", expand=True)
        for tag, color in (("info", TEXT), ("warn", "#8a6100"), ("error", RED),
                           ("debug", "#9aa3b8"), ("sys", ACCENT),
                           ("tool", "#0b6b3a")):
            self.log_txt.tag_configure(tag, foreground=color)
        self.log_txt.tag_configure("time", foreground="#9aa3b8")

        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="复制", command=self._copy_log_sel)
        menu.add_command(label="全选", command=lambda: self.log_txt.tag_add("sel", "1.0", "end"))
        menu.add_separator()
        menu.add_command(label="清空日志", command=self._clear_log)
        self.log_menu = menu

        def _popup(ev):
            try:
                self.log_menu.tk_popup(ev.x_root, ev.y_root)
            finally:
                self.log_menu.grab_release()

        self.log_txt.bind("<Button-3>", _popup)
        self.log_txt.bind("<Control-c>", lambda e: self._copy_log_sel())

        # ---------------- 工具栏 ----------------
        bar2 = tk.Frame(self, bg=CARD, highlightbackground=BORDER,
                        highlightthickness=1)
        # ⚠ 必须插在主区域之前分配空间：pack 的顺序就是分配顺序，
        #   主区域是 expand 的，后 pack 的底栏会被挤没（踩过）。
        bar2.pack(fill="x", side="bottom", before=self.main_frame)
        inner = tk.Frame(bar2, bg=CARD)
        inner.pack(padx=14, pady=12)

        # width 锁死像素宽：启动/停止的过渡态要换文案（▶→··），不锁会抽一下。
        self.btn_start = self._accent_btn(inner, "▶  开始运行", self.start_live,
                                          ACCENT, ACCENT_DARK, width=112)
        self.btn_stop = self._accent_btn(inner, "■  停止", self.stop_live, RED,
                                         "#c93a3f", width=88)
        self.btn_check = ttk.Button(inner, text="自 检", command=self.run_check)
        self.btn_preview = ttk.Button(inner, text="钉钉预览",
                                      command=self.dingtalk_preview)
        self.btn_forward = ttk.Button(inner, text="转发一条…",
                                      command=self.forward_one_dialog)
        self.btn_settings = ttk.Button(inner, text="设 置", command=self.open_settings)
        self.btn_records = ttk.Button(inner, text="消息记录", command=self.show_records)
        self.btn_export = ttk.Button(inner, text="导出 CSV", command=self.export_csv)
        self.btn_folder = ttk.Button(inner, text="数据目录", command=self._open_data_dir)

        for b in (self.btn_start, self.btn_stop):
            b.pack(side="left", padx=(0, 10))
        ttk.Separator(inner, orient="vertical").pack(side="left", fill="y",
                                                     padx=(0, 10))
        for b in (self.btn_check, self.btn_preview, self.btn_forward,
                  self.btn_settings, self.btn_records, self.btn_export,
                  self.btn_folder):
            b.pack(side="left", padx=(0, 6))

        # 底部信息条：左边是运行状态，右边是版本号与版权。
        #
        # ⚠ pack(side="right") 的先后顺序决定左右：先 pack 的贴最右。所以
        #   先把版权打上去，版本号自然落在它左边、离状态文字更近。
        foot = tk.Frame(self, bg=BG)
        foot.pack(fill="x", side="bottom", before=self.main_frame)
        self.statusbar = tk.Label(foot, text="就绪", bg=BG, fg=MUTED,
                                  font=F_SMALL, anchor="w", padx=16, pady=4)
        self.statusbar.pack(side="left")
        tk.Label(foot, text=COPYRIGHT, bg=BG, fg="#9aa3b8", font=F_COPY,
                 anchor="e", padx=16, pady=4).pack(side="right")
        # 版本号要看得到、但不抢眼：比版权深一档的灰，字重正常。
        # 用户 2026-10-19：「每次更新或者修复以后知道是哪个版本」。
        self.ver_label = tk.Label(foot, text=f"版本 {APP_VERSION}", bg=BG,
                                  fg=MUTED, font=F_COPY, anchor="e",
                                  pady=4, padx=0)
        self.ver_label.pack(side="right", padx=(0, 12))
        self._refresh_state()

    # ================================================== 配置

    def _worker_python(self) -> str | None:
        """本机可用的工作解释器；没有就返回 None。

        ⚠ 绝对不能用 ``sys.executable`` 兜底：打包成 exe 后它指向**本程序
        自己**，拿它去跑 bridge_worker.py 等于又启动一个主窗口，会无限弹窗。
        """
        exe = (self.env or {}).get("worker") or ""
        return exe if exe and Path(exe).is_file() else None

    def _warn_no_worker(self) -> None:
        self._log("未找到可用的工作解释器（需要 Python 3.10+ 与 pyyaml / "
                  "pillow / numpy / rapidocr）。", "error")
        messagebox.showwarning(
            "缺少运行环境",
            "本机没有找到能跑采集与投递的 Python 环境。\n\n"
            "请安装 Python 3.10 以上版本，并在这个 Python 里执行：\n"
            "    pip install -r requirements.txt -i "
            "https://pypi.tuna.tsinghua.edu.cn/simple\n\n"
            "装好后回到本界面点「重新检测环境」。", parent=self)

    def _worker_argv(self, *args: str) -> list[str] | None:
        exe = self._worker_python()
        if not exe:
            return None
        return [exe, str(BASE_DIR / "app" / "bridge_worker.py"), *args]

    def _load_cfg(self) -> dict | None:
        exe = self.env.get("worker")
        if not exe or not Path(exe).is_file():
            return None
        try:
            r = subprocess.run(
                [exe, str(BASE_DIR / "app" / "bridge_worker.py"),
                 "--dump-config", str(CONFIG_PATH)],
                cwd=str(BASE_DIR), capture_output=True, text=True, timeout=90,
                encoding="utf-8", errors="replace",
                creationflags=0x08000000)
        except Exception as exc:
            self._log(f"读取配置失败：{exc}", "warn")
            return None
        for line in (r.stdout or "").splitlines():
            if line.startswith("@@WORKER@@"):
                try:
                    data = json.loads(line[10:])
                except Exception:
                    continue
                if data.get("kind") == "config":
                    return data.get("data") or {}
                if data.get("kind") == "config-error":
                    self.after(0, lambda m=data.get("message", ""):
                               self._log(f"配置错误：{m}", "error"))
        if r.returncode != 0:
            self._log(f"读取配置失败：{(r.stderr or r.stdout or '').strip()[:200]}",
                      "warn")
        return None

    def _apply_cfg_to_ui(self):
        d = self.cfg.get("dingtalk") or {}
        w = self.cfg.get("wechat") or {}
        n = self.cfg.get("notify") or {}
        routes = self.cfg.get("routes") or []
        r0 = routes[0] if routes else {}
        chats = "、".join(d.get("chat_names") or []) or "-"
        targets = "、".join(r0.get("targets") or w.get("targets") or []) or "-"
        if not n.get("enabled"):
            notify = "未启用"
        elif not _notify_robots(n):
            notify = "缺 webhook"
        else:
            cnt = len(_notify_robots(n))
            notify = "已启用" if cnt == 1 else f"已启用（{cnt} 个机器人）"
        vals = {
            "监控会话": chats,
            "转发目标": targets,
            "轮询间隔": f"{d.get('ui_poll_interval', 5)} 秒",
            "发送间隔": f"{w.get('interval', 3)} 秒",
            "时间过滤": "只转发今天" if d.get("time_filter") == "today" else "不限时间",
            "钉钉通知": notify,
        }
        for k, v in vals.items():
            if k in self.cfg_rows:
                self.cfg_rows[k][1].configure(text=v)
        # 数据来源是下拉框，值不能写进 Label
        if getattr(self, "src_var", None) is not None:
            self.src_var.set(source_label(str(d.get("mode") or "")))
        self.subtitle.configure(
            text=f"{chats}   →   {targets}" if chats != "-" else "尚未配置监控会话")
        if w.get("dry_run"):
            self.drybar.configure(
                text="⚠ 当前是【试运行】模式：只会打印、不会真的发到微信。"
                     "确认无误后到「设置」里取消试运行。")
            self.drybar.pack(fill="x", before=self.main_frame)
        else:
            self.drybar.pack_forget()

    # ================================================== 数据来源切换

    def _on_source_change(self, _ev=None):
        """主界面下拉切换数据来源（PC 截图识别 ↔ 手机无障碍上报）。

        写盘而不是只改内存：真正采集的是子进程（`bridge_worker --live`），
        它启动时重新读 config.yaml，界面里的内存值它看不到。也正因为它
        只在启动时读，切换后必须**重新开始运行**才生效。
        """
        mode = label_source(self.src_var.get())
        if not mode:
            return
        if not isinstance(self.cfg, dict):
            messagebox.showwarning("配置尚未加载", "请先点「重新检测环境」。",
                                   parent=self)
            return
        d = self.cfg.setdefault("dingtalk", {})
        old = str(d.get("mode") or "")
        if old == mode:
            return

        ok = cfgfile.set_or_insert_scalar(CONFIG_PATH, "dingtalk", "mode", mode,
                                          after_key="chat_names")
        if not ok:
            messagebox.showerror(
                "切换失败", f"没能把数据来源写进配置文件：\n{CONFIG_PATH}\n\n"
                            "请检查文件是否只读，或用「设置 → 采集与投递」再试一次。",
                parent=self)
            self.src_var.set(source_label(old))
            return

        d["mode"] = mode
        self._log(f"数据来源已切换为「{source_label(mode)}」", "sys")

        running = self.runner.alive()
        body = (f"数据来源：{source_label(mode)}\n\n"
                "这个设置要重新「开始运行」才会生效"
                + ("——当前正在运行，请先点停止。" if running else "。"))
        if mode == "android":
            tok = str(d.get("gateway_token") or "").strip()
            body += ("\n\n手机端要一直停在目标群、屏幕不能黑，"
                     "并在 App 里填本机地址与 token。")
            watch = int(d.get("android_watch_min") or 0)
            if watch > 0:
                body += (f"\n\n自动巡检已开启（每 {watch} 分钟）：钉钉被切走会自动"
                         "点回目标群；这段时间的用户操作会被打断。")
            else:
                body += ("\n\n自动巡检是关的 —— 手机被切到别的群或钉钉崩溃后"
                         "不会自己恢复，只能靠人发现。需要的话到"
                         "「设置 → 采集与投递」打开（可选 5/10/15 分钟）。")
            if not tok:
                body += "\n\n⚠ 网关 token 还是空的，手机连不上 —— " \
                        "先到「设置 → 采集与投递」填一个。"
            if not d.get("android_parse"):
                body += "\n\n⚠ 「把手机报来的画面解析成消息」还没开，手机模式下" \
                        "不会转发任何消息 —— 请到「设置 → 采集与投递」勾上。"
        messagebox.showinfo("已保存", body, parent=self)

    # ================================================== 日志

    def _open_log_file(self):
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            path = LOG_DIR / f"gui-{datetime.now():%Y%m%d}.log"
            self._log_fh = path.open("a", encoding="utf-8", buffering=1)
        except Exception:
            self._log_fh = None

    def _log(self, text: str, tag: str = "info"):
        now = datetime.now().strftime("%H:%M:%S")
        line = f"[{now}] {text}"
        try:
            self.log_txt.configure(state="normal")
            self.log_txt.insert("end", line + "\n", tag)
            lines = int(self.log_txt.index("end-1c").split(".")[0])
            if lines > 4000:
                self.log_txt.delete("1.0", f"{lines - 3500}.0")
            if self.autoscroll.get():
                self.log_txt.see("end")
            self.log_txt.configure(state="disabled")
        except Exception:
            pass
        if self._log_fh:
            try:
                self._log_fh.write(line + "\n")
            except Exception:
                pass

    def _copy_log_sel(self):
        try:
            text = self.log_txt.get("sel.first", "sel.last")
        except Exception:
            text = ""
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)

    def _clear_log(self):
        self.log_txt.configure(state="normal")
        self.log_txt.delete("1.0", "end")
        self.log_txt.configure(state="disabled")

    def _open_path(self, p: Path):
        try:
            os.startfile(str(p))            # noqa: S606
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc), parent=self)

    def _open_log_dir(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._open_path(LOG_DIR)

    def _open_data_dir(self):
        d = paths.data_dir()
        d.mkdir(parents=True, exist_ok=True)
        self._open_path(d)

    # ================================================== 工作进程事件

    _RE_SENT = re.compile(r"已投递 → (.+)")
    _RE_FAIL = re.compile(r"投递失败 → ")
    _RE_NEW = re.compile(r"识别到 (\d+) 条新消息")
    _RE_SKIP = re.compile(r"丢弃跨天消息")
    _RE_DUP = re.compile(r"重复消息，跳过")
    _RE_FILT = re.compile(r"未命中转发规则")

    def _on_worker_line(self, raw: str):
        info = parse_line(raw)
        if info:
            level = str(info.get("level", "INFO"))
            msg = str(info.get("msg", ""))
            tag = {"WARNING": "warn", "ERROR": "error",
                   "DEBUG": "debug"}.get(level, "info")
            self._log(msg, tag)
            if self._RE_SENT.search(msg):
                self.counters["sent"] += 1
            if self._RE_FAIL.search(msg):
                self.counters["failed"] += 1
            m = self._RE_NEW.search(msg)
            if m:
                self.counters["recognized"] += int(m.group(1))
            if self._RE_SKIP.search(msg):
                self.counters["skipped"] += 1
            if self._RE_DUP.search(msg):
                self.counters["dup"] += 1
            if self._RE_FILT.search(msg):
                self.counters["filtered"] += 1
            self.after(0, self._refresh_counters)
            return
        if raw.startswith("@@WORKER@@"):
            try:
                data = json.loads(raw[10:])
            except Exception:
                return
            kind = data.get("kind")
            if kind == "crash":
                self._log("工作进程异常退出：", "error")
                for ln in (data.get("traceback") or "").splitlines()[-12:]:
                    self._log("  " + ln, "error")
            return
        line = raw.strip()
        if line:
            self._log("  " + line, "tool")

    def _on_worker_exit(self, code: int):
        if self.state == "stopping":
            self._log("已停止转发。", "sys")
        elif self.state == "running":
            if code == -999:
                self._log("已停止转发。", "sys")
            elif code == 0:
                self._log("工作进程正常退出。", "sys")
            else:
                self._log(f"工作进程异常退出（exit={code}），转发已中断。", "error")
        else:
            return
        self.state = "idle"
        self.after(0, self._refresh_state)

    def _refresh_counters(self):
        self.v_rec.configure(text=str(self.counters["recognized"]))
        self.v_sent.configure(text=str(self.counters["sent"]))
        self.v_fail.configure(text=str(self.counters["failed"]))
        self.v_skip.configure(text=str(self.counters["skipped"]))
        self.v_dup.configure(text=str(self.counters["dup"]))
        self.v_filt.configure(text=str(self.counters["filtered"]))
        if self.runner.alive():
            secs = int(time.time() - self.runner.started_at)
            self.v_elapsed.configure(text=f"{secs // 60:02d}:{secs % 60:02d}")

    def _refresh_state(self):
        """刷新按钮可用态与运行指示灯。

        按钮的视觉规则（三态）：
        - 已停止：开始=实心蓝（唯一强调色）、停止=灰色空心（不可用）
        - 运行中：开始=灰色空心（不可用）、停止=实心红（唯一强调色）
        - 启动/停止中：两个都灰（此时正在切换，都不该点）

        即**同一时刻只有一个实心彩色按钮**，用户不用思考就知道现在该点哪个。

        文字也跟着换符号（▶→··／■→··），否则两个灰按钮同屏、指示灯又小，
        用户点完会觉得「没反应」。像素宽已锁死（见 `_build_ui`），换字不伸缩。
        """
        running = self.state == "running"
        busy = self._busy
        # 「检测中 / 正在停止」也算过渡态：此时点「开始」等于在旧进程还没退干净时
        # 再起一个，故一并禁掉。旧代码只判 running，切状态那几百毫秒里按钮仍可点。
        transient = self.state in ("checking", "stopping")
        can_start = not running and not busy and not transient
        can_stop = running and not busy

        self.btn_start.configure(state="normal" if can_start else "disabled")
        self.btn_stop.configure(state="normal" if can_stop else "disabled")
        for b in (self.btn_check, self.btn_preview, self.btn_forward,
                  self.btn_settings, self.btn_records, self.btn_export):
            b.configure(state="disabled" if (running or busy) else "normal")

        # 过渡态文案：让「按不动」有解释（正在启动/停止 vs 真的不能点）。
        # ⚠ 换文案就会改字宽 → 按钮抽搐。试过若干同宽符号都不稳（实测
        #   ▶ 109 / ⏳ 117 / ·· 110 / ■ 80 / ·· 84，字体度量不可控），
        #   干脆在创建时用 width= 锁死像素宽（见 _build_ui），文案随便换。
        self.btn_start.configure(
            text="··  开始运行" if self.state == "checking" else "▶  开始运行")
        self.btn_stop.configure(
            text="··  停止" if self.state == "stopping" else "■  停止")

        color = {"idle": "#8b93a8", "running": "#4ade80",
                 "checking": "#fbbf24", "stopping": "#f87171"}[self.state]
        label = {"idle": "● 已停止", "running": "● 运行中", "checking": "● 检测中",
                 "stopping": "● 正在停止"}[self.state]
        self.run_dot.itemconfigure(1, fill=color)
        self.run_txt.configure(text=label)

        # 实心 vs 空心，一眼分辨能不能点（见模块顶部 BTN_DISABLED_* 注释）
        self._paint_btn(self.btn_start, can_start, ACCENT, ACCENT_DARK)
        self._paint_btn(self.btn_stop, can_stop, RED, "#c93a3f")
        if not running:
            self.v_elapsed.configure(text="00:00")
    def _refresh_env_chips(self, src: tuple[bool, str] | None = None,
                           dst: tuple[bool, str] | None = None):
        if src is not None:
            self._set_chip(self.src_dot, self.src_txt,
                           "ok" if src[0] else "err",
                           "● 采集端 正常" if src[0] else "● 采集端 异常")
        if dst is not None:
            self._set_chip(self.dst_dot, self.dst_txt,
                           "ok" if dst[0] else "err",
                           "● 投递端 正常" if dst[0] else "● 投递端 异常")

    # ================================================== 数据库统计

    def _db_path(self) -> Path:
        raw = ((self.cfg.get("storage") or {}).get("db_path") or "data/bridge.db")
        p = Path(raw)
        return p if p.is_absolute() else (paths.DATA_ROOT / p)

    def _poll_db(self):
        def work():
            out = {}
            db = self._db_path()
            try:
                conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
                cur = conn.execute("SELECT COUNT(*) FROM messages")
                out["messages"] = cur.fetchone()[0]
                out["deliveries"] = {}
                for status, n in conn.execute(
                        "SELECT status, COUNT(*) FROM deliveries GROUP BY status"):
                    out["deliveries"][status] = n
                conn.close()
            except Exception:
                pass
            self.db_totals = out
            self.after(0, self._refresh_db)
        threading.Thread(target=work, daemon=True).start()

    def _refresh_db(self):
        d = self.db_totals or {}
        self.v_db_msg.configure(text=str(d.get("messages", 0)))
        dl = d.get("deliveries") or {}
        self.v_db_ok.configure(text=str(dl.get("ok", 0)))
        self.v_db_bad.configure(text=str(dl.get("fail", 0)))

    # ================================================== 动作

    @staticmethod
    def _clean_tool_output(text: str) -> str:
        """把工作进程/工具输出的机器可读行转成给人看的文本。"""
        out: list[str] = []
        for line in (text or "").splitlines():
            if line.startswith("@@WORKER@@"):
                continue
            if line.startswith("@@LOG@@"):
                try:
                    d = json.loads(line[7:])
                    out.append(f"[{d.get('t', '')}] [{d.get('level', '')}] "
                               f"{d.get('msg', '')}")
                except Exception:
                    pass
                continue
            out.append(line)
        return "\n".join(out)

    def _set_busy(self, busy: bool, text: str = ""):
        self._busy = busy
        self.statusbar.configure(text=text or ("就绪" if not busy else "处理中…"))
        self._refresh_state()

    def run_check(self, silent: bool = False):
        if self.runner.alive() or self._busy:
            if not silent:
                messagebox.showinfo("提示", "有任务正在运行，请先停止。", parent=self)
            return
        self.state = "checking"
        self._refresh_state()
        self.statusbar.configure(text="正在自检（采集端 + 投递端）…")
        if not silent:
            self.log_message("手动自检开始")

        result: dict = {}

        def work():
            exe = self._worker_python()
            if not exe:
                self.after(0, lambda: (result.update(out="（未找到工作解释器）"),
                                       self._check_done(result, silent)))
                return
            try:
                r = subprocess.run(
                    [exe, str(BASE_DIR / "app" / "bridge_worker.py"),
                     "--check", "--config", str(CONFIG_PATH)],
                    cwd=str(BASE_DIR), capture_output=True, text=True,
                    timeout=180, encoding="utf-8", errors="replace",
                    creationflags=0x08000000)
                result["out"] = r.stdout or ""
                result["err"] = r.stderr or ""
                result["code"] = r.returncode
            except Exception as exc:
                result["out"] = f"自检执行失败：{exc}"
                result["code"] = 1
            self.after(0, lambda: self._check_done(result, silent))

        threading.Thread(target=work, daemon=True).start()

    def _check_done(self, result: dict, silent: bool):
        out = self._clean_tool_output(result.get("out") or "").strip()
        ok_src = ok_dst = False
        for line in out.splitlines():
            s = line.strip()
            if not s:
                continue
            self._log("  " + s, "sys" if s.startswith(("采集端", "投递端")) else "tool")
            if s.startswith("采集端"):
                ok_src = "通过" in s
            if s.startswith("投递端") or s.startswith("微信连接"):
                ok_dst = "通过" in s
        if result.get("code") != 0:
            for line in (result.get("err") or "").splitlines()[-6:]:
                if line.strip():
                    self._log("  " + line.strip(), "error")
        self._refresh_env_chips((ok_src, ""), (ok_dst, ""))
        ok = result.get("code") == 0
        self.state = "idle"
        self._refresh_state()
        self.statusbar.configure(
            text="自检通过" if ok else "自检未通过——请看日志里标红的条目")
        if not silent:
            if ok:
                messagebox.showinfo("自检通过", "采集端与投递端均正常，可以开始运行。",
                                    parent=self)
            else:
                messagebox.showwarning(
                    "自检未通过", "存在问题，请在日志区查看详情。\n\n"
                    "常见原因：钉钉/微信没开、窗口被最小化、没打开对应会话。",
                    parent=self)

    def log_message(self, text: str):
        self._log(text, "sys")

    # ---------------- 钉钉通知 ----------------

    def notify_test(self):
        """发一条钉钉测试消息，确认 webhook / 关键词 / 加签是否配对。"""
        self._notify_cmd("--notify-test", "测试通知")

    def notify_report(self):
        """把累计统计发到钉钉群。"""
        self._notify_cmd("--notify-report", "运行报告")

    def notify_say(self):
        """弹出输入框，把随手写的内容发到钉钉群（不必先进设置里改配置）。

        这条通道与「钉钉 → 微信」的转发完全无关：它只负责把话送到钉钉群。
        """
        nc = self.cfg.get("notify") or {}
        if not _notify_robots(nc):
            messagebox.showwarning(
                "钉钉通知还没配好",
                "请先到「工具 → 设置中心 → 钉钉通知」里填入自定义机器人的 webhook。",
                parent=self)
            return
        if not nc.get("enabled"):
            messagebox.showwarning(
                "钉钉通知未启用",
                "「工具 → 设置中心 → 钉钉通知」里勾选「启用钉钉通知」并保存后再试。",
                parent=self)
            return
        if self.runner.alive() or self._busy:
            messagebox.showinfo("提示", "有任务正在运行，请先停止。", parent=self)
            return

        win = tk.Toplevel(self)
        win.title("发送消息到钉钉群")
        win.geometry("580x300")
        win.configure(bg=CARD)
        win.transient(self)
        frm = tk.Frame(win, bg=CARD)
        frm.pack(fill="both", expand=True, padx=18, pady=16)
        ttk.Label(frm, text="要发到钉钉群里的内容：", style="Card.TLabel"
                  ).pack(anchor="w")
        text = tk.Text(frm, height=8, font=F_UI, relief="flat", bg="#fbfcfe",
                       highlightbackground=BORDER, highlightthickness=1,
                       padx=8, pady=6, wrap="word", insertbackground=TEXT)
        text.pack(fill="both", expand=True, pady=(6, 4))
        try:
            from settings_ui import last_message
            text.insert("1.0", last_message())
        except Exception:
            pass
        text.focus_set()

        tip = tk.Label(frm, text="这条只发到钉钉群，不会影响钉钉 → 微信的转发。",
                       bg=CARD, fg=MUTED, font=F_SMALL, anchor="w")
        tip.pack(anchor="w")

        bar = tk.Frame(frm, bg=CARD)
        bar.pack(fill="x", pady=(10, 0))

        def send(*_a):
            body = text.get("1.0", "end").strip()
            if not body:
                tip.configure(text="请先输入要发送的内容。", fg=RED)
                text.focus_set()
                return "break"
            if len(body) > 3000:
                # 走的是命令行参数，Windows 命令行总长有限；这条通道本就是发
                # 短通知用的，真要发长文请用「发送运行报告」或设置里那个框。
                tip.configure(text=f"内容太长了（{len(body)} 字，上限 3000），"
                                   "请精简后再发。", fg=RED)
                return "break"
            try:
                from settings_ui import remember_message
                remember_message(body)
            except Exception:
                pass
            self._notify_cmd(f"--notify-say={body}", "自定义消息")
            win.destroy()
            return "break"

        ttk.Button(bar, text="发送到钉钉群", command=send).pack(side="left")
        ttk.Button(bar, text="取消", command=win.destroy).pack(side="left", padx=8)
        ttk.Label(bar, text="Ctrl+Enter 发送", style="CardMuted.TLabel"
                  ).pack(side="left", padx=6)
        text.bind("<Control-Return>", send)
        win.bind("<Escape>", lambda _e: win.destroy())

    def _notify_cmd(self, flag: str, title: str):
        if self.runner.alive() or self._busy:
            messagebox.showinfo("提示", "有任务正在运行，请先停止。", parent=self)
            return
        self._set_busy(True, f"正在发送钉钉{title}…")
        self._log(f"正在发送钉钉{title}…", "sys")
        result: dict = {}

        def work():
            argv = self._worker_argv(flag, "--config", str(CONFIG_PATH))
            if not argv:
                self.after(0, lambda: (self._set_busy(False),
                                       self._warn_no_worker()))
                return
            try:
                r = subprocess.run(argv, cwd=str(BASE_DIR), capture_output=True,
                                   text=True, timeout=60, encoding="utf-8",
                                   errors="replace", creationflags=0x08000000)
                result["out"] = r.stdout or ""
                result["err"] = r.stderr or ""
                result["code"] = r.returncode
            except Exception as exc:
                result["out"] = f"执行失败：{exc}"
                result["code"] = 1
            self.after(0, lambda: self._notify_done(result, title))

        threading.Thread(target=work, daemon=True).start()

    def _notify_done(self, result: dict, title: str):
        self._set_busy(False)
        for line in self._clean_tool_output(result.get("out") or "").splitlines():
            if line.strip():
                self._log("  " + line.strip(), "tool")
        ok = result.get("code") == 0
        if not ok:
            for line in (result.get("err") or "").splitlines()[-6:]:
                if line.strip():
                    self._log("  " + line.strip(), "error")
        self._log(f"钉钉{title}：{'发送成功' if ok else '发送失败'}",
                  "sys" if ok else "error")
        if ok:
            messagebox.showinfo(f"{title}已发送",
                                "消息已经发到钉钉群，请到群里确认。", parent=self)
        else:
            messagebox.showwarning(
                f"{title}发送失败",
                "失败原因见日志区。\n\n"
                "最常见的原因是机器人的「安全设置」不匹配：\n"
                "· 开了「自定义关键词」→ 把关键词填到 设置 → 钉钉通知\n"
                "· 开了「加签」       → 把 SEC 开头的密钥填进去\n\n"
                "也可能是本机出网受限，或被机器人的 IP 白名单拦下。",
                parent=self)

    def redetect_env(self):
        """强制重新扫描解释器与配置（换过 Python、装过依赖后用）。"""
        if self.runner.alive() or self._busy:
            messagebox.showinfo("提示", "有任务正在运行，请先停止。", parent=self)
            return
        self._log("重新检测运行环境…", "sys")
        threading.Thread(target=self._boot_worker,
                         kwargs={"force": True, "with_check": False},
                         daemon=True).start()

    # ---------------- 正式运行 ----------------

    def start_live(self):
        if self.runner.alive():
            return
        if not self.cfg:
            messagebox.showwarning("提示", "配置尚未加载成功，请先重新检测环境。",
                                   parent=self)
            return
        w = self.cfg.get("wechat") or {}
        if w.get("dry_run"):
            if not messagebox.askyesno(
                    "试运行确认",
                    "当前处于【试运行】模式：只会打印识别到的消息，不会真的发到微信。\n\n"
                    "要继续以试运行方式启动吗？\n"
                    "（想真实发送：请到「设置」里取消试运行）", parent=self):
                return
        else:
            targets = "、".join((self.cfg.get("routes") or [{}])[0].get("targets") or [])
            if not messagebox.askyesno(
                    "正式发送确认",
                    "即将进入正式运行：识别到的新消息会真实发送到微信。\n\n"
                    f"转发目标：{targets}\n\n确定开始吗？", parent=self):
                return
        self._reset_counters()
        argv_live = self._worker_argv(
            "--live", "--config", str(CONFIG_PATH),
            "--stop-file", str(self._stop_file()),
            # 让工作进程盯着本界面：界面被强杀/直接关掉时，它能自己退出，
            # 不会变成偷偷转发的"孤儿进程"
            "--parent-pid", str(os.getpid()))
        if not argv_live:
            self._warn_no_worker()
            self._refresh_state()
            return
        self.state = "running"
        self._refresh_state()
        self.log_message("启动转发服务…")
        try:
            self.runner.start(
                argv_live,
                cwd=str(BASE_DIR),
                stop_file=self._stop_file())
        except Exception as exc:
            self.state = "idle"
            self._refresh_state()
            messagebox.showerror("启动失败", str(exc), parent=self)
            return
        self.statusbar.configure(text="运行中——识别到新消息会自动转发")
        self._start_db_loop()

    def _start_db_loop(self):
        """每 5 秒刷新一次累计统计（只读打开数据库，不影响写入方）。"""
        self._poll_db()
        self.after(5000, self._maybe_db_loop)

    def _maybe_db_loop(self):
        self._poll_db()
        if self.runner.alive():
            self._refresh_counters()
        self.after(5000, self._maybe_db_loop)

    def _stop_file(self) -> Path:
        """停止哨兵文件：GUI 写它，工作进程的看门狗读到就优雅退出。"""
        d = paths.data_dir()
        d.mkdir(parents=True, exist_ok=True)
        return d / "run.stop"

    def _reset_counters(self):
        for k in self.counters:
            self.counters[k] = 0
        self._refresh_counters()

    def stop_live(self):
        if not self.runner.alive():
            return
        self.state = "stopping"
        self._refresh_state()
        self.statusbar.configure(text="正在停止…")
        threading.Thread(target=self._stop_work, daemon=True).start()

    def _stop_work(self):
        self.runner.stop()
        self.after(0, lambda: self.statusbar.configure(text="已停止"))


    # ---------------- 钉钉预览 ----------------

    def dingtalk_preview(self):
        if self.runner.alive() or self._busy:
            messagebox.showinfo("提示", "有任务正在运行，请先停止。", parent=self)
            return
        self._set_busy(True, "正在截取钉钉消息区并识别…")
        result: dict = {}

        def work():
            exe = self._worker_python()
            if not exe:
                self.after(0, lambda: (result.update(out="（未找到工作解释器）"),
                                       self._preview_done(result)))
                return
            try:
                r = subprocess.run(
                    [exe, str(BASE_DIR / "tools" / "test_dingtalk_ocr.py"),
                     "--config", str(CONFIG_PATH)],
                    cwd=str(BASE_DIR), capture_output=True, text=True,
                    timeout=180, encoding="utf-8", errors="replace",
                    creationflags=0x08000000)
                result["out"] = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
            except Exception as exc:
                result["out"] = f"预览执行失败：{exc}"
            self.after(0, lambda: self._preview_done(result))

        threading.Thread(target=work, daemon=True).start()

    def _preview_done(self, result: dict):
        self._set_busy(False, "钉钉预览完成")
        win = tk.Toplevel(self)
        win.title("钉钉消息预览（只读，不转发）")
        win.geometry("720x520")
        win.configure(bg=CARD)
        win.transient(self)
        txt = tk.Text(win, bg="#fbfcfe", fg=TEXT, font=("Microsoft YaHei UI", 10),
                      relief="flat", wrap="word", padx=14, pady=12)
        sb = ttk.Scrollbar(win, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", self._clean_tool_output(result.get("out")) or "（无输出）")
        txt.configure(state="disabled")

        def refresh():
            win.destroy()
            self.after(200, self.dingtalk_preview)

        bar = tk.Frame(win, bg=CARD)
        bar.pack(fill="x", padx=14, pady=(0, 12))
        ttk.Button(bar, text="重新截取", command=refresh).pack(side="right")
        ttk.Button(bar, text="关闭", command=win.destroy).pack(side="right", padx=8)

    # ---------------- 转发一条 ----------------

    def forward_one_dialog(self):
        if self.runner.alive() or self._busy:
            messagebox.showinfo("提示", "有任务正在运行，请先停止。", parent=self)
            return
        win = tk.Toplevel(self)
        win.title("转发一条（先预览，再决定是否发送）")
        win.geometry("560x330")
        win.configure(bg=CARD)
        win.transient(self)
        frm = tk.Frame(win, bg=CARD)
        frm.pack(fill="both", expand=True, padx=20, pady=18)

        ttk.Label(frm, text="从最下面往上数第几条：", style="Card.TLabel"
                  ).grid(row=0, column=0, sticky="w", pady=6)
        idx = tk.StringVar(value="1")
        ttk.Spinbox(frm, from_=1, to=30, textvariable=idx, width=6
                    ).grid(row=0, column=1, sticky="w", padx=8)

        ttk.Label(frm, text="或自定义内容（留空则抓钉钉最新消息）：",
                  style="Card.TLabel").grid(row=1, column=0, columnspan=2,
                                            sticky="w", pady=(10, 2))
        text = tk.Text(frm, height=4, font=("Microsoft YaHei UI", 10),
                       relief="flat", bg="#fbfcfe", highlightbackground=BORDER,
                       highlightthickness=1)
        text.grid(row=2, column=0, columnspan=2, sticky="ew", pady=4)
        frm.columnconfigure(0, weight=1)

        out = tk.Text(frm, height=7, font=("Microsoft YaHei UI", 9),
                      relief="flat", bg="#f4f6fb", state="disabled",
                      highlightbackground=BORDER, highlightthickness=1)
        out.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(12, 0))
        frm.rowconfigure(3, weight=1)

        def show(s: str):
            out.configure(state="normal")
            out.delete("1.0", "end")
            out.insert("1.0", s)
            out.configure(state="disabled")

        state = {"argv": None}

        def run_cmd(send: bool, callback):
            exe = self._worker_python()
            if not exe:
                self._warn_no_worker()
                return
            base = [exe,
                    str(BASE_DIR / "tools" / "forward_one.py"),
                    "--config", str(CONFIG_PATH)]
            custom = text.get("1.0", "end").strip()
            if custom:
                base += ["--text", custom]
            else:
                base += ["--index", idx.get() or "1"]
            if send:
                base += ["--yes"]
            state["argv"] = base
            self._set_busy(True, "正在执行转发测试…")

            def work():
                try:
                    r = subprocess.run(base, cwd=str(BASE_DIR),
                                       capture_output=True, text=True, timeout=300,
                                       encoding="utf-8", errors="replace",
                                       creationflags=0x08000000)
                    res = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
                except Exception as exc:
                    res = f"执行失败：{exc}"
                cleaned = self._clean_tool_output(res) or "（无输出）"
                self.after(0, lambda: (self._set_busy(False, "完成"),
                                       show(cleaned), callback(cleaned)))

            threading.Thread(target=work, daemon=True).start()

        def on_preview(_res):
            pass

        def do_preview():
            show("正在抓取并渲染，请稍候…（约 3-10 秒）")
            run_cmd(False, on_preview)

        def do_send():
            if not messagebox.askyesno(
                    "确认真实发送",
                    "即将操作微信，把上面这条内容真实发送出去。\n\n确定发送吗？",
                    parent=win):
                return
            run_cmd(True, lambda res: self._poll_db())

        bar = tk.Frame(win, bg=CARD)
        bar.pack(fill="x", padx=20, pady=(0, 16))
        ttk.Button(bar, text="预 览", command=do_preview).pack(side="right")
        ttk.Button(bar, text="真实发送…", command=do_send).pack(side="right", padx=8)
        ttk.Button(bar, text="关 闭", command=win.destroy).pack(side="left")

    # ---------------- 转发最近 N 条 ----------------

    def forward_recent_dialog(self):
        """手动转发最近 N 条 —— 一次补几条，来源可选钉钉画面或已入库记录。

        与「转发一条」的区别只有「条数」和「来源」两件事，其余安全设计一致：
        默认只预览，真实发送前再确认一次。

        `db` 来源是给补发用的：消息当时已入库但投递失败/漏发，
        从库里取出来重发，不必去钉钉窗口上重新找。
        """
        if self.runner.alive() or self._busy:
            messagebox.showinfo("提示", "有任务正在运行，请先停止。", parent=self)
            return

        win = tk.Toplevel(self)
        win.title("转发最近 N 条（先预览，再决定是否发送）")
        win.geometry("620x480")
        win.configure(bg=CARD)
        win.transient(self)

        # 固定高度的底部按钮栏必须最先 pack：Tk 按调用顺序分配空间，
        # 内容比窗口高时最后 pack 的先被挤成 0 高（保存按钮消失过一次）。
        bar = tk.Frame(win, bg=CARD)
        bar.pack(fill="x", side="bottom", padx=20, pady=(0, 16))

        frm = tk.Frame(win, bg=CARD)
        frm.pack(fill="both", expand=True, padx=20, pady=(18, 8))

        ttk.Label(frm, text="转发最近", style="Card.TLabel"
                  ).grid(row=0, column=0, sticky="w", pady=6)
        count = tk.StringVar(value="3")
        ttk.Spinbox(frm, from_=1, to=50, textvariable=count, width=6
                    ).grid(row=0, column=1, sticky="w", padx=(4, 0))
        ttk.Label(frm, text="条（1~50）", style="Card.TLabel"
                  ).grid(row=0, column=2, sticky="w", padx=(4, 0))

        ttk.Label(frm, text="来源", style="Card.TLabel"
                  ).grid(row=1, column=0, sticky="w", pady=6)
        SRC_SCREEN = "钉钉当前画面（最下面 N 条）"
        SRC_DB = "已入库记录（补发用）"
        src = tk.StringVar(value=SRC_SCREEN)
        src_box = ttk.Combobox(frm, textvariable=src, state="readonly", width=30,
                               values=(SRC_SCREEN, SRC_DB))
        src_box.grid(row=1, column=1, columnspan=2, sticky="w", padx=(4, 0))

        failed = tk.IntVar(value=0)
        failed_btn = ttk.Checkbutton(frm, text="只补发「最近一次投递失败」的",
                                     variable=failed)
        failed_btn.grid(row=2, column=1, columnspan=2, sticky="w", padx=(4, 0))

        banner = tk.IntVar(value=1)
        ttk.Checkbutton(frm, text="每条约加一行「【手动补发】」提示",
                        variable=banner).grid(row=3, column=1, columnspan=2,
                                              sticky="w", padx=(4, 0))

        frm.columnconfigure(0, weight=0)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="预览内容", style="Card.TLabel"
                  ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(12, 2))
        out = tk.Text(frm, height=14, font=("Microsoft YaHei UI", 9),
                      relief="flat", bg="#f4f6fb", state="disabled", wrap="word",
                      highlightbackground=BORDER, highlightthickness=1)
        out.grid(row=5, column=0, columnspan=3, sticky="nsew", pady=4)
        frm.rowconfigure(5, weight=1)

        def on_src_change(*_):
            # 「只看失败」只对 db 来源有意义 —— 画面上的消息没有投递历史
            failed_btn.state(["!disabled"] if src.get() == SRC_DB else ["disabled"])

        src_box.bind("<<ComboboxSelected>>", on_src_change)
        on_src_change()

        def show(s: str):
            out.configure(state="normal")
            out.delete("1.0", "end")
            out.insert("1.0", s)
            out.configure(state="disabled")

        def build_argv(send: bool) -> list[str] | None:
            exe = self._worker_python()
            if not exe:
                self._warn_no_worker()
                return None
            argv = [exe, str(BASE_DIR / "tools" / "forward_recent.py"),
                    "--config", str(CONFIG_PATH), "-n", count.get() or "3"]
            if src.get() == SRC_DB:
                argv += ["--from", "db"]
                if failed.get():
                    argv.append("--only-failed")
            if not banner.get():
                argv.append("--plain")
            if send:
                argv.append("--yes")
            return argv

        def run_cmd(send: bool, callback):
            argv = build_argv(send)
            if argv is None:
                return
            self._set_busy(True, "正在执行手动转发…")

            def work():
                try:
                    r = subprocess.run(argv, cwd=str(BASE_DIR), capture_output=True,
                                       text=True, timeout=600, encoding="utf-8",
                                       errors="replace", creationflags=0x08000000)
                    res = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
                except Exception as exc:
                    res = f"执行失败：{exc}"
                cleaned = self._clean_tool_output(res) or "（无输出）"
                self.after(0, lambda: (self._set_busy(False, "完成"),
                                       show(cleaned), callback(cleaned)))

            threading.Thread(target=work, daemon=True).start()

        def do_preview():
            show("正在读取消息并渲染，请稍候…")
            run_cmd(False, lambda _res: None)

        def do_send():
            if not messagebox.askyesno(
                    "确认真实发送",
                    f"即将操作微信，把最近 {count.get()} 条消息真实发送出去。\n\n"
                    "发送时会重新读一次消息（保证发的是最新状态），"
                    "因此若这期间钉钉来了新消息，实际发送的会是从那时起最新的 N 条 —— "
                    "发送结果会显示在下面的框里。\n\n"
                    "确定发送吗？",
                    parent=win):
                return
            run_cmd(True, lambda _res: self._poll_db())

        ttk.Button(bar, text="预 览", command=do_preview).pack(side="right")
        ttk.Button(bar, text="真实发送…", command=do_send).pack(side="right", padx=8)
        ttk.Button(bar, text="关 闭", command=win.destroy).pack(side="left")

    # ---------------- 设置 ----------------

    def open_settings(self):
        """打开设置中心（转发规则 / 时间范围 / 采集投递 / 消息格式 / 其他）。"""
        if not self.cfg:
            messagebox.showwarning("提示", "配置尚未加载，无法打开设置。", parent=self)
            return
        if self._settings_win is not None:
            try:
                self._settings_win.win.deiconify()
                self._settings_win.win.lift()
                return
            except Exception:
                self._settings_win = None
        from settings_ui import open_dialog
        self._settings_win = open_dialog(self, CONFIG_PATH, self.cfg, self.env)

    # ---- 设置中心回调 ----

    def open_config_file(self):
        try:
            os.startfile(str(CONFIG_PATH))          # noqa: S606
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc), parent=self)

    def re_detect_env(self):
        self.redetect_env()

    def open_data_dir(self):
        self._open_data_dir()

    def open_log_dir(self):
        self._open_log_dir()

    def reload_after_settings(self):
        """设置保存后：重读配置、刷新概览、同步试运行提示条。"""
        self._settings_win = None
        cfg = self._load_cfg()
        if cfg is not None:
            self.cfg = cfg
        self._apply_cfg_to_ui()
        self._log("设置已生效，界面概览已刷新。", "sys")

    def autostart_enabled(self):
        return self._autostart_enabled()

    def set_autostart_enabled(self, enable: bool):
        self._set_autostart(bool(enable))

    # ---------------- 开机自启 ----------------

    _RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    _RUN_NAME = "DingTalkWeChatBridge"

    def _autostart_enabled(self) -> bool:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._RUN_KEY) as k:
                winreg.QueryValueEx(k, self._RUN_NAME)
                return True
        except Exception:
            return False

    def _set_autostart(self, enable: bool):
        try:
            import winreg
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self._RUN_KEY, 0,
                                    winreg.KEY_SET_VALUE) as k:
                if enable:
                    if getattr(sys, "frozen", False):
                        # 打包版：直接指向本程序自己
                        val = f'"{sys.executable}"'
                    else:
                        exe = paths.windowed(self.env.get("gui") or sys.executable)
                        val = f'"{exe}" "{APP_DIR / "gui.py"}"'
                    winreg.SetValueEx(k, self._RUN_NAME, 0, winreg.REG_SZ, val)
                    self._log(f"已设置开机自启：{val}", "sys")
                else:
                    try:
                        winreg.DeleteValue(k, self._RUN_NAME)
                        self._log("已取消开机自启。", "sys")
                    except FileNotFoundError:
                        pass
        except Exception as exc:
            messagebox.showwarning("开机自启设置失败", str(exc), parent=self)

    # ---------------- 记录 / 导出 ----------------

    def show_records(self):
        # ⚠ 不能拦「正在运行」：想看消息记录的时刻，基本就是程序在跑的时刻。
        # 只读打开数据库，运行中读也安全。
        win = tk.Toplevel(self)
        win.title("消息记录")
        win.geometry("1000x560")
        win.transient(self)

        # 顶部筛选条 —— 「没发出去的」是这里最有用的一栏：漏发、投递失败、
        # 被规则拦下的都从它捞回来。
        bar = ttk.Frame(win)
        bar.pack(side="top", fill="x", padx=10, pady=(10, 0))
        ttk.Label(bar, text="显示：").pack(side="left")
        scope = tk.StringVar(value="全部")
        for text in ("全部", "最近 30 分钟", "最近 24 小时", "没有发出的"):
            ttk.Radiobutton(bar, text=text, value=text, variable=scope,
                            command=lambda: load()).pack(side="left", padx=(0, 8))
        count = ttk.Label(bar, text="")
        count.pack(side="right")

        cols = ("time", "state", "chat", "sender", "content")
        tree = ttk.Treeview(win, columns=cols, show="headings")
        for cid, text, width, anchor in (
                ("time", "时间", 130, "w"), ("state", "投递", 70, "center"),
                ("chat", "会话", 150, "w"), ("sender", "发送者", 110, "w"),
                ("content", "内容", 470, "w")):
            tree.heading(cid, text=text)
            tree.column(cid, width=width, anchor=anchor)
        sb = ttk.Scrollbar(win, command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", pady=10, padx=(0, 10))
        tree.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
        tree.tag_configure("bad", foreground="#c0392b")
        tree.tag_configure("ok", foreground="#27ae60")

        def load():
            sel = scope.get()
            where, params = "", []
            if sel == "最近 30 分钟":
                where = "WHERE m.msg_time >= ?"
                params = [(datetime.now() - timedelta(minutes=30))
                          .strftime("%Y-%m-%d %H:%M:%S")]
            elif sel == "最近 24 小时":
                where = "WHERE m.msg_time >= ?"
                params = [(datetime.now() - timedelta(hours=24))
                          .strftime("%Y-%m-%d %H:%M:%S")]
            elif sel == "没有发出的":
                # 「没有成功投递」= 一次都没发成。被规则拦下的、发失败的、
                # 以及压根没走到投递的，全在这一栏里。
                # ⚠ 状态值是 ``ok`` / ``fail``（见 service.log_delivery 的调用处），
                # 不是 ``successful`` —— 写错的话这一栏会把已发的也列出来。
                where = ("WHERE NOT EXISTS (SELECT 1 FROM deliveries d "
                         "WHERE d.uid = m.uid AND d.status = 'ok')")

            def work():
                rows = []
                try:
                    conn = sqlite3.connect(f"file:{self._db_path()}?mode=ro",
                                           uri=True, timeout=2)
                    sql = (
                        "SELECT m.msg_time, m.chat_name, m.sender_name, m.content, "
                        "  (SELECT d.status FROM deliveries d WHERE d.uid = m.uid "
                        "   ORDER BY d.id DESC LIMIT 1) AS st "
                        f"FROM messages m {where} ORDER BY m.msg_time DESC LIMIT 300")
                    for r in conn.execute(sql, params):
                        rows.append(["" if v is None else str(v) for v in r])
                    conn.close()
                except Exception as exc:
                    rows = [[str(datetime.now()), "", "", f"读取失败：{exc}", ""]]
                self.after(0, lambda: render(rows))

            def render(rows):
                tree.delete(*tree.get_children())
                for t, chat, sender, content, st in rows:
                    st_txt = {"ok": "已发", "fail": "失败"}.get(st or "", "未发")
                    tag = "ok" if st == "ok" else "bad"
                    tree.insert("", "end", tag=tag,
                                values=(t, st_txt, chat, sender,
                                        content.replace("\n", " ")[:120]))
                count.config(text=f"共 {len(rows)} 条")

            threading.Thread(target=work, daemon=True).start()

        load()

    def export_csv(self):
        if self._busy or self.runner.alive():
            messagebox.showinfo("提示", "有任务正在运行，请先停止。", parent=self)
            return
        import csv
        name = f"messages-{datetime.now():%Y%m%d-%H%M%S}.csv"
        path = filedialog.asksaveasfilename(
            parent=self, initialfile=name, initialdir=str(BASE_DIR),
            defaultextension=".csv",
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")])
        if not path:
            return

        def work():
            try:
                conn = sqlite3.connect(f"file:{self._db_path()}?mode=ro", uri=True,
                                       timeout=2)
                cur = conn.execute(
                    "SELECT msg_time, company, chat_name, sender_name, msg_type,"
                    " content, uid FROM messages ORDER BY msg_time DESC")
                with open(path, "w", newline="", encoding="utf-8-sig") as f:
                    wtr = csv.writer(f)
                    wtr.writerow(["msg_time", "company", "chat_name",
                                  "sender_name", "msg_type", "content", "uid"])
                    wtr.writerows(cur)
                conn.close()
                self.after(0, lambda: (self._log(f"已导出到：{path}", "sys"),
                                       self._open_path(Path(path).parent)))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror(
                    "导出失败", str(exc), parent=self))

        threading.Thread(target=work, daemon=True).start()

    # ---------------- 关闭 ----------------

    def _on_close(self):
        if self.runner.alive():
            if not messagebox.askyesno(
                    "退出确认", "转发任务正在运行，退出会停止转发。\n\n确定停止并退出吗？",
                    parent=self):
                return
            self.state = "stopping"
            self._refresh_state()
            self.runner.stop(grace=4.0)
        try:
            if self._log_fh:
                self._log_fh.close()
        except Exception:
            pass
        self.destroy()


def main() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
