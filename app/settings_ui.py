"""设置中心（独立窗口，分页签）。

为什么单独一个模块
    界面逻辑集中在这里，「设置能改什么」一眼可见：
        转发规则   —— 哪个钉钉群 → 哪个（哪些）微信群，可自由增删
        时间范围   —— 不限 / 仅当天 / 只在指定时段
        采集与投递 —— 轮询间隔、发送间隔、试运行、两道防护
        消息格式   —— 转发文案模板（带实时预览）
        钉钉通知   —— 故障告警与运行报告（自定义机器人 webhook）
        其他       —— 开机自启、环境信息、快捷入口

    保存时会先备份 config.yaml，再按「保留注释的按行手术」写回
    （见 app/cfgfile.py），不会把配置文件里的说明注释洗掉。
"""
from __future__ import annotations

import re
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from pathlib import Path

import cfgfile

WINDOW_RE = re.compile(r"^\s*\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}\s*$")

BG = "#eef1f7"
CARD = "#ffffff"
BORDER = "#e2e7f0"
INPUT = "#f7f9fc"
TEXT = "#1c2333"
MUTED = "#69728c"
ACCENT = "#2f6be4"
RED = "#e5484d"
WARN_BG = "#fff5da"
WARN_FG = "#8a6100"

F_UI = ("Microsoft YaHei UI", 10)
F_SMALL = ("Microsoft YaHei UI", 9)
F_BOLD = ("Microsoft YaHei UI", 10, "bold")

SAMPLE = {
    "company": "数字质控·大江东",
    "sender": "张三",
    "chat": "【数字质控】浙江杭州萧山区大江东公司",
    "time": "14:32",
    "content": "今日订单 1200 单，已全部发出。",
    "type": "text",
}

TIME_MODES = [
    ("any", "不限时间", "钉钉窗口里看到的都转发（历史消息靠锚点防护兜底）"),
    ("today", "只转发当天消息", "丢弃钉钉标注为「昨天 / 星期X / 月日」的跨天消息（推荐）"),
    ("window", "只在指定时段转发", "在「只转发当天」的基础上，再加一层时段限制"),
]


# 「钉钉通知」页签里那个输入框，关掉设置再打开时把上次发过的文字还回来。
# 只存在内存里：一段随手写的通知不值得写进 config.yaml 污染配置。
# 同一份缓存也给主界面的「发送消息到钉钉」对话框用，两处入口内容互通。
_LAST_MESSAGE = ""
_DEFAULT_TEXT_CACHE: str | None = None


def last_message() -> str:
    return _LAST_MESSAGE


def remember_message(text: str) -> None:
    global _LAST_MESSAGE
    _LAST_MESSAGE = text or ""


def default_test_text() -> str:
    """默认测试文案，取自 bridge.notify —— 与命令行 --notify-test 同一份。"""
    global _DEFAULT_TEXT_CACHE
    if _DEFAULT_TEXT_CACHE is None:
        try:
            import sys
            root = str(Path(__file__).resolve().parent.parent)
            if root not in sys.path:
                sys.path.insert(0, root)
            from bridge.notify import DEFAULT_TEST_TEXT
            _DEFAULT_TEXT_CACHE = DEFAULT_TEST_TEXT
        except Exception:
            # 取不到文案不该让设置窗口打不开，退化成一句等价的提示
            _DEFAULT_TEXT_CACHE = "测试消息：喵喵钉钉-微信消息转发桥通知通道正常。"
    return _DEFAULT_TEXT_CACHE


def _card(parent, title: str = "") -> tk.Frame:
    box = tk.Frame(parent, bg=CARD, highlightbackground=BORDER,
                   highlightthickness=1)
    if title:
        tk.Label(box, text=title, bg=CARD, fg=MUTED, font=F_SMALL,
                 anchor="w").pack(fill="x", padx=14, pady=(10, 4))
    return box


def _tip(parent, text: str, bg: str = CARD, fg: str = MUTED,
         font=None) -> tk.Label:
    lab = tk.Label(parent, text=text, bg=bg, fg=fg, font=font or F_SMALL,
                   justify="left", anchor="w", wraplength=660)
    return lab


def _scroll_area(parent, bg: str = BG) -> tk.Frame:
    """返回一个可垂直滚动的内层 Frame；内容装得下时自动收起滚动条。

    滚轮绑定用「进入本区域才接管」的写法，不能用 ``bind_all`` ——
    多个滚动区共存时，后建的会把先建的顶掉，前面那些页签就滚不动了。
    """
    canvas = tk.Canvas(parent, bg=bg, highlightthickness=0)
    bar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
    inner = tk.Frame(canvas, bg=bg)
    window = canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=bar.set)
    canvas.pack(side="left", fill="both", expand=True)
    bar.pack(side="right", fill="y")

    def _resize(_evt=None):
        canvas.configure(scrollregion=canvas.bbox("all"))
        canvas.itemconfigure(window, width=canvas.winfo_width())
        # 内容没有溢出就把滚动条收起来，免得每个页签都拖一条没用的条
        try:
            over = inner.winfo_reqheight() > canvas.winfo_height() + 1
            if over and not bar.winfo_ismapped():
                bar.pack(side="right", fill="y")
            elif not over and bar.winfo_ismapped():
                bar.pack_forget()
        except Exception:
            pass

    def _wheel(evt):
        step = evt.delta // 120
        canvas.yview_scroll(-step if step else -1, "units")

    inner.bind("<Configure>", _resize)
    canvas.bind("<Configure>", _resize)
    canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _wheel))
    canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))
    return inner


class SettingsDialog:
    """设置中心窗口。``app`` 需要提供：cfg / env / reload_cfg / apply_cfg /
    config_path / log / set_autostart_enabled。"""

    def __init__(self, app, config_path: Path, cfg: dict, env: dict):
        self.app = app
        self.path = Path(config_path)
        self.cfg = cfg or {}
        self.env = env or {}
        self.st: dict[str, object] = {}
        self.rule_rows: list[dict] = []

        self.win = tk.Toplevel(app)
        self.win.title("设置中心 — 钉钉 → 微信 消息转发桥")
        # 默认给高一点，但**不能超过屏幕** —— 窗口比屏幕还高时，钉在底部的
        # 「保存」按钮会跑到屏幕外，又变成点不着。所以按屏幕高度夹一下。
        screen_h = self.win.winfo_screenheight()
        height = max(520, min(700, screen_h - 140))
        self.win.geometry(f"840x{height}")
        self.win.minsize(760, 520)
        self.win.configure(bg=BG)
        self.win.transient(app)

        self._build()
        self.win.grab_set()
        self.win.focus_force()

    # ------------------------------------------------------------ 布局

    def _build(self):
        # ⚠ pack 顺序即「分配顺序」：底部按钮栏必须**先**占位（side=bottom），
        #   否则页签内容一旦比窗口高，排最后的按钮栏会被挤成 0 高度 ——
        #   「保存」按钮直接消失，用户就没法保存设置了。
        bar = tk.Frame(self.win, bg=BG)
        bar.pack(side="bottom", fill="x", padx=18, pady=(4, 14))
        tk.Button(bar, text="保 存", command=self.save, bg=ACCENT, fg="#ffffff",
                  activebackground="#2456bd", activeforeground="#ffffff",
                  relief="flat", bd=0, font=("Microsoft YaHei UI", 10, "bold"),
                  padx=22, pady=8, cursor="hand2").pack(side="right")
        ttk.Button(bar, text="取消", command=self.win.destroy).pack(side="right",
                                                                   padx=8)
        ttk.Button(bar, text="还原本次修改", command=self._reload).pack(side="left")

        head = tk.Frame(self.win, bg=BG)
        head.pack(fill="x", padx=18, pady=(14, 6))
        tk.Label(head, text="设置中心", bg=BG, fg=TEXT,
                 font=("Microsoft YaHei UI", 13, "bold")).pack(side="left")
        tk.Label(head, text="改完点右下角「保存」，会先备份原配置",
                 bg=BG, fg=MUTED, font=F_SMALL).pack(side="left", padx=12)

        nb = ttk.Notebook(self.win)
        nb.pack(fill="both", expand=True, padx=14, pady=6)

        self._tab_rules(nb)
        self._tab_time(nb)
        self._tab_engine(nb)
        self._tab_format(nb)
        self._tab_notify(nb)
        self._tab_misc(nb)

    def _page(self, nb, title: str, scroll: bool = True) -> tk.Frame:
        """建一个页签页。默认套一层滚动区，内容再长也不会够不着底部。

        「转发规则」自己带一个滚动列表（要跟着窗口一起拉伸），所以传
        scroll=False，避免两层滚动互相干扰。
        """
        page = tk.Frame(nb, bg=BG)
        nb.add(page, text=f"   {title}   ")
        if not scroll:
            return page
        return _scroll_area(page, bg=BG)

    # ------------------------------------------------------------ 转发规则

    def _tab_rules(self, nb):
        page = self._page(nb, "转发规则", scroll=False)
        box = _card(page, "钉钉群  →  微信群（一条规则一行，可加多条）")
        box.pack(fill="both", expand=True, padx=12, pady=(12, 6))
        _tip(box, "「钉钉群」填群名里能认出的片段即可（做子串匹配，例如「数字质控」）；\n"
                  "「微信群」必须与微信客户端里的名字完全一致。\n"
                  "一个钉钉群可以同时转发到多个微信群：把群名用英文分号 ; 隔开，例如\n"
                  "    申通五星业务员培训群; 大江东转运中心; 质控管理群\n"
                  "反过来也行：多条规则命中同一个钉钉群时，目标会自动合并去重。",
             bg=CARD).pack(fill="x", padx=14, pady=(0, 6))

        holder = tk.Frame(box, bg=CARD)
        holder.pack(fill="both", expand=True, padx=8, pady=4)
        inner = _scroll_area(holder, bg=CARD)      # 规则列表自己滚（贴着卡片底色）

        for r in cfgfile.read_rules(self.path) or []:
            self._add_rule_row(inner, "; ".join(r.get("sources") or []),
                               "; ".join(r.get("targets") or []))
        if not self.rule_rows:
            self._add_rule_row(inner, "", "")

        btns = tk.Frame(box, bg=CARD)
        btns.pack(fill="x", padx=14, pady=(2, 12))
        ttk.Button(btns, text="+ 添加一条规则",
                   command=lambda: self._add_rule_row(inner, "", "")
                   ).pack(side="left")
        ttk.Button(btns, text="打开 config.yaml",
                   command=lambda: self.app.open_config_file()
                   ).pack(side="left", padx=8)
        _tip(btns, "运行中的任务需要重新开始才会用上新规则", bg=CARD
             ).pack(side="right", pady=4)

    def _add_rule_row(self, inner, sources: str, targets: str):
        row = tk.Frame(inner, bg=CARD)
        row.pack(fill="x", pady=4, padx=6)
        sv, tv = tk.StringVar(value=sources), tk.StringVar(value=targets)
        tk.Label(row, text="钉钉群", bg=CARD, fg=MUTED, font=F_SMALL
                 ).pack(side="left")
        ttk.Entry(row, textvariable=sv).pack(side="left", fill="x", expand=True,
                                             padx=(6, 8))
        tk.Label(row, text="→", bg=CARD, fg=ACCENT, font=F_BOLD
                 ).pack(side="left")
        tk.Label(row, text="微信群", bg=CARD, fg=MUTED, font=F_SMALL
                 ).pack(side="left", padx=(8, 0))
        ttk.Entry(row, textvariable=tv).pack(side="left", fill="x", expand=True,
                                             padx=(6, 8))
        entry = {"frame": row, "src": sv, "dst": tv}
        ttk.Button(row, text="删除", width=5,
                   command=lambda: self._del_rule_row(entry)).pack(side="left")
        self.rule_rows.append(entry)

    def _del_rule_row(self, entry: dict):
        self.rule_rows = [r for r in self.rule_rows if r is not entry]
        entry["frame"].destroy()

    # ------------------------------------------------------------ 时间范围

    def _tab_time(self, nb):
        page = self._page(nb, "时间范围")
        d = self.cfg.get("dingtalk") or {}
        current = str(d.get("time_filter", "today")).lower()
        if current not in {m[0] for m in TIME_MODES}:
            current = "today"
        self.st["time_filter"] = tk.StringVar(value=current)

        box = _card(page, "什么时候才转发（只影响「钉钉这边采集到的新消息」）")
        box.pack(fill="x", padx=12, pady=(12, 8))
        for value, label, desc in TIME_MODES:
            line = tk.Frame(box, bg=CARD)
            line.pack(fill="x", padx=14, pady=3)
            ttk.Radiobutton(line, text=label, value=value,
                            variable=self.st["time_filter"],
                            command=self._sync_window_state).pack(side="left")
            _tip(line, desc, bg=CARD).pack(side="left", padx=12)

        win_box = _card(page, "时段（选了「只在指定时段转发」才生效）")
        win_box.pack(fill="x", padx=12, pady=(0, 8))
        raw = str(d.get("time_window", "08:00-18:00"))
        start, _, end = raw.partition("-")
        self.st["win_start"] = tk.StringVar(value=(start or "08:00").strip())
        self.st["win_end"] = tk.StringVar(value=(end or "18:00").strip() or "18:00")
        line = tk.Frame(win_box, bg=CARD)
        line.pack(fill="x", padx=14, pady=10)
        tk.Label(line, text="从", bg=CARD, fg=TEXT, font=F_UI).pack(side="left")
        self.e_start = ttk.Entry(line, textvariable=self.st["win_start"], width=8)
        self.e_start.pack(side="left", padx=6)
        tk.Label(line, text="到", bg=CARD, fg=TEXT, font=F_UI).pack(side="left")
        self.e_end = ttk.Entry(line, textvariable=self.st["win_end"], width=8)
        self.e_end.pack(side="left", padx=6)
        self.win_hint = tk.Label(line, text="", bg=CARD, fg=MUTED, font=F_SMALL)
        self.win_hint.pack(side="left", padx=12)
        _tip(win_box, "格式 HH:MM，支持跨零点（22:00-06:00 = 夜里转、白天不转）。\n"
                      "注意：钉钉对「最新几条消息」不显示时间戳，这类消息按【当前时刻】判定，"
                      "所以这个设置的实际效果是「只在上班时段转发」。",
             bg=CARD).pack(fill="x", padx=14, pady=(0, 12))

        self._sync_window_state()

    def _sync_window_state(self):
        on = self.st["time_filter"].get() == "window"
        state = "normal" if on else "disabled"
        for w in (self.e_start, self.e_end):
            w.configure(state=state)
        self.win_hint.configure(
            text="已启用" if on else "当前未启用（选「只在指定时段转发」后可编辑）")

    # ------------------------------------------------------------ 采集与投递

    def _tab_engine(self, nb):
        page = self._page(nb, "采集与投递")
        d = self.cfg.get("dingtalk") or {}
        w = self.cfg.get("wechat") or {}

        self.st["dry_run"] = tk.BooleanVar(value=bool(w.get("dry_run", True)))
        self.st["interval"] = tk.StringVar(value=str(w.get("interval", 3)))
        self.st["hotkey"] = tk.StringVar(
            value=str(w.get("show_hotkey") or "{Ctrl}{Alt}w"))
        self.st["poll"] = tk.StringVar(value=str(d.get("ui_poll_interval", 5)))
        self.st["anchor"] = tk.BooleanVar(value=bool(d.get("anchor_guard", True)))
        self.st["title"] = tk.BooleanVar(value=bool(d.get("title_check", True)))
        self.st["ratio"] = tk.StringVar(value=str(d.get("title_ratio", 0.55)))

        box = _card(page, "投递")
        box.pack(fill="x", padx=12, pady=(12, 8))
        ttk.Checkbutton(box, text="试运行模式：只打印日志，不真的发到微信",
                        variable=self.st["dry_run"]).pack(anchor="w", padx=14,
                                                          pady=(10, 2))
        _tip(box, "首次使用建议先开着（当前配置就是），确认识别到的内容无误后取消勾选，"
                  "再点「开始运行」就是真实转发了。").pack(fill="x", padx=14)
        grid = tk.Frame(box, bg=CARD)
        grid.pack(fill="x", padx=14, pady=(8, 12))
        self._spin(grid, 0, "钉钉轮询间隔（秒）", self.st["poll"], 3, 60,
                   "每隔多久截一次钉钉窗口做识别。越小越不容易漏消息，"
                   "但别低于 5 秒（OCR 每帧约 2 秒）。")
        self._spin(grid, 1, "微信发送间隔（秒）", self.st["interval"], 1, 60,
                   "相邻两条微信之间的最小间隔，防风控，不建议调小。")

        line = tk.Frame(box, bg=CARD)
        line.pack(fill="x", padx=14, pady=(0, 4))
        tk.Label(line, text="微信热键（显示/隐藏主窗口）", bg=CARD, fg=TEXT,
                 font=F_UI).pack(side="left")
        ttk.Entry(line, textvariable=self.st["hotkey"], width=16).pack(side="left",
                                                                      padx=8)
        _tip(line, "默认 Ctrl+Alt+W。微信窗口被最小化或收进托盘时，程序用这个"
                   "热键把窗口唤回来；在微信「设置 → 快捷键」里改过就要同步改这里。"
                   ).pack(side="left", padx=6)
        _tip(box, "投递时需要微信窗口可用：被别的窗口挡住没关系（程序会切到前台），"
                  "但请别把微信最小化 —— 万一最小化或收进托盘，程序会自动把它唤回来。"
             ).pack(fill="x", padx=14, pady=(0, 12))

        box2 = _card(page, "采集防护（建议都保持开启）")
        box2.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Checkbutton(box2, text="锚点防护：聊天区被滚动时，不转发滚进来的历史消息",
                        variable=self.st["anchor"]).pack(anchor="w", padx=14,
                                                         pady=(10, 2))
        ttk.Checkbutton(box2, text="标题护栏：钉钉当前窗口不是配置里的群时，整帧丢弃",
                        variable=self.st["title"]).pack(anchor="w", padx=14, pady=2)
        line = tk.Frame(box2, bg=CARD)
        line.pack(fill="x", padx=14, pady=(2, 12))
        tk.Label(line, text="标题匹配阈值（0.3-1.0，越低越宽松）", bg=CARD,
                 fg=TEXT, font=F_UI).pack(side="left")
        ttk.Entry(line, textvariable=self.st["ratio"], width=8).pack(side="left",
                                                                     padx=8)
        _tip(line, "OCR 会把群名认错几个字，例：实测误读「综学国控」相似度 0.65 仍算通过，"
                   "其他群约 0.3 会被拦下。").pack(side="left", padx=8)

    def _spin(self, parent, row: int, label: str, var, lo: int, hi: int,
              note: str):
        blk = tk.Frame(parent, bg=CARD)
        blk.grid(row=row, column=0, sticky="w", pady=6)
        tk.Label(blk, text=label, bg=CARD, fg=TEXT, font=F_UI).pack(side="left")
        ttk.Spinbox(blk, from_=lo, to=hi, textvariable=var, width=6
                    ).pack(side="left", padx=8)
        _tip(blk, note, bg=CARD).pack(side="left")

    # ------------------------------------------------------------ 消息格式

    def _tab_format(self, nb):
        page = self._page(nb, "消息格式")
        routes = self.cfg.get("routes") or [{}]
        tpl = routes[0].get("template") or "{content}"
        self.st["template"] = tk.StringVar(value=tpl)

        box = _card(page, "转发到微信时的文案模板")
        box.pack(fill="x", padx=12, pady=(12, 8))
        self.tpl_text = tk.Text(box, height=4, font=F_UI, relief="flat",
                                bg="#fbfcfe", highlightbackground=BORDER,
                                highlightthickness=1)
        self.tpl_text.insert("1.0", tpl)
        self.tpl_text.pack(fill="x", padx=14, pady=(10, 4))
        self.tpl_text.bind("<KeyRelease>", lambda e: self._preview())
        _tip(box, "可用占位符：{company} 来源  {sender} 发送人  {chat} 钉钉群名  "
                  "{time} 时间  {content} 正文  {type} 类型\n换行直接按回车；"
                  "普通文字原样保留。").pack(fill="x", padx=14, pady=(0, 6))

        prev = _card(page, "预览（用一条示例消息渲染）")
        prev.pack(fill="x", padx=12, pady=(0, 8))
        self.prev_lab = tk.Label(prev, text="", bg="#f4f6fb", fg=TEXT, font=F_UI,
                                 justify="left", anchor="w", padx=12, pady=10,
                                 wraplength=640)
        self.prev_lab.pack(fill="x", padx=14, pady=12)
        self._preview()

    def _preview(self):
        tpl = self.tpl_text.get("1.0", "end-1c") if hasattr(self, "tpl_text") else ""
        try:
            out = tpl.format(**SAMPLE).strip()
        except KeyError as exc:
            out = f"⚠ 模板里有无法识别的占位符：{exc}"
        except Exception as exc:
            out = f"⚠ 模板无法渲染：{exc}"
        try:
            self.prev_lab.configure(text=out or "（模板为空）")
        except Exception:
            pass

    # ------------------------------------------------------------ 钉钉通知

    def _tab_notify(self, nb):
        page = self._page(nb, "钉钉通知")
        n = self.cfg.get("notify") or {}

        self.st["n_enabled"] = tk.BooleanVar(value=bool(n.get("enabled", False)))
        self.st["n_webhook"] = tk.StringVar(value=str(n.get("webhook") or ""))
        self.st["n_secret"] = tk.StringVar(value=str(n.get("secret") or ""))
        self.st["n_keyword"] = tk.StringVar(value=str(n.get("keyword") or ""))
        self.st["n_at_mobiles"] = tk.StringVar(
            value="; ".join(str(m) for m in (n.get("at_mobiles") or [])))
        self.st["n_at_all"] = tk.BooleanVar(value=bool(n.get("at_all", False)))
        self.st["n_log_alerts"] = tk.BooleanVar(value=bool(n.get("log_alerts", True)))
        self.st["n_start"] = tk.BooleanVar(value=bool(n.get("notify_start", True)))
        self.st["n_fail"] = tk.BooleanVar(value=bool(n.get("notify_fail", True)))
        self.st["n_threshold"] = tk.StringVar(value=str(n.get("fail_threshold", 3)))
        self.st["n_idle"] = tk.BooleanVar(value=bool(n.get("notify_idle", True)))
        self.st["n_idle_min"] = tk.StringVar(value=str(n.get("idle_minutes", 120)))
        self.st["n_idle_win"] = tk.StringVar(
            value=str(n.get("idle_window") or "08:00-21:00"))
        self.st["n_cooldown"] = tk.StringVar(value=str(n.get("cooldown", 300)))
        self.st["n_hourly"] = tk.BooleanVar(value=bool(n.get("report_hourly", False)))
        self.st["n_daily"] = tk.StringVar(value=str(n.get("report_daily_at") or ""))

        # ---- 凭证 ----
        box = _card(page, "机器人凭证（钉钉群 → 群设置 → 智能群助手 → 添加机器人 → 自定义）")
        box.pack(fill="x", padx=12, pady=(12, 8))
        ttk.Checkbutton(box, text="启用钉钉通知（故障告警 + 运行报告）",
                        variable=self.st["n_enabled"]).pack(anchor="w", padx=14,
                                                            pady=(10, 4))
        self._field(box, "Webhook", self.st["n_webhook"], 74)
        self._field(box, "加签密钥", self.st["n_secret"], 34,
                    "机器人开了「加签」才需要填，SEC 开头的字符串")
        self._field(box, "自定义关键词", self.st["n_keyword"], 20,
                    "机器人开了「自定义关键词」才需要填；内容里缺这个词会自动补一行")

        # ---- 自定义消息 ----
        # 一个能直接打字的输入框：想发什么就写什么，不必先保存设置。
        # 用途不只是连通性测试 —— 换班、交接口径变了、临时通知群里的同事，
        # 都可以直接用这个框（这条通道本来就只有「发」这一个方向）。
        tk.Label(box, text="发到群里的内容（想发什么就打在这里）：", bg=CARD,
                 fg=MUTED, font=F_SMALL).pack(anchor="w", padx=14, pady=(8, 2))
        wrap = tk.Frame(box, bg=BORDER)
        wrap.pack(fill="x", padx=14)
        self._composer = tk.Text(wrap, height=4, width=60, wrap="word",
                                 font=F_UI, bg=INPUT, fg=TEXT,
                                 relief="flat", padx=8, pady=6,
                                 insertbackground=TEXT,
                                 highlightthickness=0)
        self._composer.pack(fill="x", padx=1, pady=1)
        if last_message():                   # 上一次发过的内容，接着用
            self._composer.insert("1.0", last_message())
        self._composer.bind("<Control-Return>", self._send_by_hotkey)
        _tip(box, "Ctrl+Enter 直接发送。留空会提示；想验证通道是否配对，"
                  "点「填入测试文案」再发即可。", bg=CARD).pack(anchor="w",
                                                             padx=16, pady=(2, 6))

        line = tk.Frame(box, bg=CARD)
        line.pack(fill="x", padx=14, pady=(2, 12))
        self._test_btn = ttk.Button(line, text="发送到钉钉群",
                                    command=self._test_notify)
        self._test_btn.pack(side="left")
        ttk.Button(line, text="填入测试文案",
                   command=self._fill_test_text).pack(side="left", padx=(8, 0))
        ttk.Button(line, text="清空",
                   command=lambda: self._composer.delete("1.0", "end")
                   ).pack(side="left", padx=(6, 0))
        self._test_lab = tk.Label(line, text="", bg=CARD, fg=MUTED, font=F_SMALL)
        self._test_lab.pack(side="left", padx=10)

        _tip(page, "注意：这个机器人只能往钉钉群里发消息，收不到群里的消息，"
                   "所以它不参与「钉钉 → 微信」的转发，只负责把运行状况告诉你。",
             bg=BG).pack(fill="x", padx=16, pady=(0, 8))

        # ---- 告警场景 ----
        box2 = _card(page, "什么情况下发告警")
        box2.pack(fill="x", padx=12, pady=(0, 8))
        ttk.Checkbutton(box2, text="采集端异常（找不到钉钉窗口 / 窗口被最小化 / 当前不是目标群）",
                        variable=self.st["n_log_alerts"]).pack(anchor="w", padx=14,
                                                               pady=(10, 2))
        ttk.Checkbutton(box2, text="开始、停止运行时各发一条",
                        variable=self.st["n_start"]).pack(anchor="w", padx=14, pady=2)
        row = tk.Frame(box2, bg=CARD)
        row.pack(fill="x", padx=14, pady=2)
        ttk.Checkbutton(row, text="微信投递连续失败达到",
                        variable=self.st["n_fail"]).pack(side="left")
        ttk.Spinbox(row, from_=1, to=50, textvariable=self.st["n_threshold"],
                    width=5).pack(side="left", padx=6)
        tk.Label(row, text="次时告警", bg=CARD, fg=TEXT, font=F_UI).pack(side="left")
        row2 = tk.Frame(box2, bg=CARD)
        row2.pack(fill="x", padx=14, pady=2)
        ttk.Checkbutton(row2, text="采集停滞：连续",
                        variable=self.st["n_idle"]).pack(side="left")
        ttk.Spinbox(row2, from_=10, to=1440, textvariable=self.st["n_idle_min"],
                    width=6).pack(side="left", padx=6)
        tk.Label(row2, text="分钟没有新消息时告警，仅在",
                 bg=CARD, fg=TEXT, font=F_UI).pack(side="left")
        ttk.Entry(row2, textvariable=self.st["n_idle_win"], width=14
                  ).pack(side="left", padx=6)
        tk.Label(row2, text="时段内判定", bg=CARD, fg=TEXT, font=F_UI).pack(side="left")
        _tip(box2, "采集窗口被切走时每轮 OCR 都会报错，所以同类告警有冷却时间：",
             bg=CARD).pack(fill="x", padx=14, pady=(6, 0))
        row3 = tk.Frame(box2, bg=CARD)
        row3.pack(fill="x", padx=14, pady=(2, 12))
        ttk.Spinbox(row3, from_=30, to=3600, increment=30,
                    textvariable=self.st["n_cooldown"], width=7).pack(side="left")
        tk.Label(row3, text="秒内同一类告警只发一次（防止刷屏）",
                 bg=CARD, fg=TEXT, font=F_UI).pack(side="left", padx=6)

        # ---- 报告与提醒 ----
        box3 = _card(page, "运行报告与提醒方式")
        box3.pack(fill="x", padx=12, pady=(0, 8))
        ttk.Checkbutton(box3, text="每小时发一次运行报告",
                        variable=self.st["n_hourly"]).pack(anchor="w", padx=14,
                                                           pady=(10, 2))
        row4 = tk.Frame(box3, bg=CARD)
        row4.pack(fill="x", padx=14, pady=2)
        tk.Label(row4, text="每天定时发报告：", bg=CARD, fg=TEXT,
                 font=F_UI).pack(side="left")
        ttk.Entry(row4, textvariable=self.st["n_daily"], width=8).pack(side="left",
                                                                       padx=6)
        _tip(row4, "填 HH:MM，留空表示不发", bg=CARD).pack(side="left")
        row5 = tk.Frame(box3, bg=CARD)
        row5.pack(fill="x", padx=14, pady=(4, 2))
        tk.Label(row5, text="告警时 @ 手机号：", bg=CARD, fg=TEXT,
                 font=F_UI).pack(side="left")
        ttk.Entry(row5, textvariable=self.st["n_at_mobiles"], width=40
                  ).pack(side="left", padx=6)
        _tip(row5, "多个用英文分号 ; 隔开，留空表示不 @ 任何人",
             bg=CARD).pack(side="left")
        ttk.Checkbutton(box3, text="告警时 @ 所有人",
                        variable=self.st["n_at_all"]).pack(anchor="w", padx=14,
                                                           pady=(4, 12))

    def _field(self, box, label: str, var, width: int, note: str = ""):
        row = tk.Frame(box, bg=CARD)
        row.pack(fill="x", padx=14, pady=3)
        tk.Label(row, text=label, bg=CARD, fg=MUTED, font=F_SMALL,
                 width=12, anchor="w").pack(side="left")
        ttk.Entry(row, textvariable=var, width=width).pack(side="left", padx=(4, 8))
        if note:
            _tip(row, note, bg=CARD).pack(side="left")

    # ---- 自定义消息输入框的两个辅助 ----

    def _fill_test_text(self):
        """把默认测试文案灌进输入框（只填不发，发不发由人决定）。"""
        self._composer.delete("1.0", "end")
        self._composer.insert("1.0", default_test_text())
        self._composer.focus_set()
        self._test_lab.configure(text="已填入测试文案，点「发送到钉钉群」发出",
                                 fg=MUTED)

    def _send_by_hotkey(self, _evt=None):
        """Ctrl+Enter 直接发送，并挡掉这次按键带出来的换行。"""
        self._test_notify()
        return "break"

    def _test_notify(self):
        """把输入框里的内容发到钉钉群 —— 直接用表单里的值，不必先保存。

        刻意不调 config.yaml：用户往往正是想边填边试。
        bridge/notify.py 只依赖标准库，界面解释器也导入得动。
        """
        url = self.st["n_webhook"].get().strip()
        if not url:
            messagebox.showwarning(
                "还没填 webhook",
                "请先填入自定义机器人的 webhook 地址。\n\n"
                "获取位置：钉钉群 → 群设置 → 智能群助手 → 添加机器人 → 自定义（Webhook）",
                parent=self.win)
            return
        content = self._composer.get("1.0", "end").strip()
        if not content:
            messagebox.showwarning(
                "还没写内容",
                "请在「发到群里的内容」输入框里写上要发送的文字。\n\n"
                "只想验证通道是否配对的话，点「填入测试文案」再发送即可。",
                parent=self.win)
            self._composer.focus_set()
            return
        self._test_btn.configure(state="disabled")
        self._test_lab.configure(text="正在发送…", fg=MUTED)
        vals = (url, self.st["n_secret"].get().strip(),
                self.st["n_keyword"].get().strip(), content)

        def work():
            try:
                import sys
                root = str(Path(__file__).resolve().parent.parent)
                if root not in sys.path:
                    sys.path.insert(0, root)
                from bridge.notify import DingTalkNotifier
                nt = DingTalkNotifier(webhook=vals[0], secret=vals[1],
                                      keyword=vals[2])
                ok, detail = nt.send_custom(vals[3])
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            self.win.after(0, lambda: self._test_done(ok, detail))

        remember_message(content)            # 下次打开设置时还记得这段文字
        threading.Thread(target=work, daemon=True).start()

    def _test_done(self, ok: bool, detail: str):
        try:
            self._test_btn.configure(state="normal")
        except Exception:
            return
        if ok:
            self._test_lab.configure(text="✓ 已发送，请到钉钉群确认", fg="#0b6b3a")
            messagebox.showinfo("发送成功", "消息已发到钉钉群。", parent=self.win)
        else:
            self._test_lab.configure(text=f"✗ 发送失败：{detail}", fg=RED)
            messagebox.showwarning(
                "发送失败",
                f"{detail}\n\n"
                "errcode=310000 表示机器人的「安全设置」不匹配：\n"
                "· 开了「自定义关键词」→ 把关键词填到上面的输入框\n"
                "· 开了「加签」       → 把 SEC 开头的密钥填上去\n"
                "网络类错误则多半是本机出网受限，或被 IP 白名单拦下。",
                parent=self.win)

    # ------------------------------------------------------------ 其他

    def _tab_misc(self, nb):
        page = self._page(nb, "其他")
        self.st["autostart"] = tk.BooleanVar(
            value=bool(self.app.autostart_enabled()))

        box = _card(page, "启动")
        box.pack(fill="x", padx=12, pady=(12, 8))
        ttk.Checkbutton(box, text="开机自动启动本程序（登录 Windows 后自动打开界面）",
                        variable=self.st["autostart"]).pack(anchor="w", padx=14,
                                                            pady=12)

        box2 = _card(page, "运行环境")
        box2.pack(fill="x", padx=12, pady=(0, 8))
        gridbox = tk.Frame(box2, bg=CARD)
        gridbox.pack(fill="x", padx=14, pady=(4, 12))
        info = [("界面解释器（需要 tkinter）", self.env.get("gui") or "-"),
                ("工作解释器（跑采集与投递）", self.env.get("worker") or "-"),
                ("配置文件", str(self.path)),
                ("数据目录", str(self.path.parent / "data"))]
        for i, (k, v) in enumerate(info):
            tk.Label(gridbox, text=k, bg=CARD, fg=MUTED, font=F_SMALL, anchor="w"
                     ).grid(row=i, column=0, sticky="w", pady=3)
            tk.Label(gridbox, text=v, bg=CARD, fg=TEXT, font=F_SMALL, anchor="w"
                     ).grid(row=i, column=1, sticky="w", padx=8, pady=3)
            gridbox.columnconfigure(1, weight=1)
        miss = self.env.get("worker_missing") or []
        if miss:
            tk.Label(gridbox, text="缺少依赖：" + "、".join(miss), bg=CARD, fg=RED,
                     font=F_SMALL).grid(row=len(info), column=0, columnspan=2,
                                        sticky="w", pady=(6, 2))
        btns = tk.Frame(page, bg=BG)
        btns.pack(fill="x", padx=12, pady=6)
        ttk.Button(btns, text="重新检测环境", command=self.app.re_detect_env
                   ).pack(side="left")
        ttk.Button(btns, text="打开数据目录", command=self.app.open_data_dir
                   ).pack(side="left", padx=8)
        ttk.Button(btns, text="打开日志目录", command=self.app.open_log_dir
                   ).pack(side="left")

    # ------------------------------------------------------------ 读写

    def _reload(self):
        self.win.destroy()
        self.app.open_settings()

    def save(self):
        rules_in = []
        for r in self.rule_rows:
            src = r["src"].get().strip()
            dst = [x.strip() for x in r["dst"].get().replace("，", ";").split(";")
                   if x.strip()]
            if not src and not dst:
                continue
            if not dst:
                messagebox.showwarning(
                    "有规则没填完", f"「{src or '(空)'}」这一行没有填微信群名。",
                    parent=self.win)
                return
            rules_in.append({
                "name": f"{src or '不限来源'} → {'、'.join(dst)}",
                "sources": [s.strip() for s in src.replace("，", ";").split(";")
                            if s.strip()],
                "targets": dst,
                "template": self.tpl_text.get("1.0", "end-1c").strip() or "{content}",
            })
        if not rules_in:
            messagebox.showwarning("至少要有一条规则",
                                   "请至少填写一条「钉钉群 → 微信群」的规则。",
                                   parent=self.win)
            return

        tf = str(self.st["time_filter"].get())
        window = f"{self.st['win_start'].get().strip()}-{self.st['win_end'].get().strip()}"
        if tf == "window" and not WINDOW_RE.match(window):
            messagebox.showwarning("时段格式不对",
                                   "时段要写成 HH:MM-HH:MM，例如 08:00-18:00 或 22:00-06:00。",
                                   parent=self.win)
            return
        try:
            interval = max(1.0, float(self.st["interval"].get()))
            poll = max(3.0, float(self.st["poll"].get()))
            ratio = min(1.0, max(0.1, float(self.st["ratio"].get())))
        except ValueError:
            messagebox.showwarning("数值不对", "间隔与阈值必须是数字。", parent=self.win)
            return

        # 微信「显示/隐藏主窗口」热键：SendKeys 语法，例如 {Ctrl}{Alt}w。
        # 填错不会报错、只会在需要唤回窗口时静默失效，所以这里卡一道。
        hotkey = self.st["hotkey"].get().strip() or "{Ctrl}{Alt}w"
        if any(ch.isspace() for ch in hotkey) or not re.search(r"[A-Za-z0-9]", hotkey):
            messagebox.showwarning(
                "微信热键格式不对",
                "请按 SendKeys 语法填写，例如 {Ctrl}{Alt}w（默认值）。\n"
                "内容不要有空格，且至少包含一个字母或数字。\n\n"
                "这个热键要和微信「设置 → 快捷键 → 显示/隐藏主窗口」里的一致。",
                parent=self.win)
            return

        # ---- 钉钉通知相关校验 ----
        try:
            n_cooldown = max(30, int(float(self.st["n_cooldown"].get())))
            n_threshold = max(1, int(float(self.st["n_threshold"].get())))
            n_idle_min = max(5, int(float(self.st["n_idle_min"].get())))
        except ValueError:
            messagebox.showwarning("数值不对",
                                   "钉钉通知里的冷却时间、失败次数、停滞分钟数必须是数字。",
                                   parent=self.win)
            return
        n_daily = self.st["n_daily"].get().strip()
        if n_daily and not re.match(r"^\d{1,2}:\d{2}$", n_daily):
            messagebox.showwarning("每日报告时间格式不对",
                                   "请填 HH:MM，例如 18:00；不想发就留空。",
                                   parent=self.win)
            return
        n_idle_win = self.st["n_idle_win"].get().strip() or "08:00-21:00"
        if not WINDOW_RE.match(n_idle_win):
            messagebox.showwarning("停滞判定时段格式不对",
                                   "请填 HH:MM-HH:MM，例如 08:00-21:00。",
                                   parent=self.win)
            return
        n_enabled = bool(self.st["n_enabled"].get())
        n_webhook = self.st["n_webhook"].get().strip()
        if n_enabled and not n_webhook:
            messagebox.showwarning(
                "启用了通知但没填 webhook",
                "请把钉钉机器人的 webhook 地址填上，或者取消「启用钉钉通知」。\n\n"
                "获取位置：钉钉群 → 群设置 → 智能群助手 → 添加机器人 → 自定义（Webhook）",
                parent=self.win)
            return
        if n_enabled and not cfgfile.has_section(self.path, "notify"):
            messagebox.showwarning(
                "配置文件里没有 notify 段",
                "保存「钉钉通知」的配置需要 config.yaml 里存在 notify: 段。\n\n"
                "请用记事本打开 config.yaml，在 routes: 这一行**之前**插入 notify 段"
                "（可以直接参考同目录的 config.example.yaml），保存后再重新打开设置中心。",
                parent=self.win)
            return
        n_mobiles = [x.strip() for x in
                     self.st["n_at_mobiles"].get().replace("，", ";").split(";")
                     if x.strip()]

        cfgfile.backup(self.path)
        problems: list[str] = []

        def S(section, key, value):
            if not cfgfile.set_scalar(self.path, section, key, value):
                problems.append(f"{section}.{key}")

        def L(section, key, items):
            if not cfgfile.set_list(self.path, section, key, items):
                problems.append(f"{section}.{key}")

        if not cfgfile.set_routes(self.path, rules_in):
            problems.append("routes")

        sources: list[str] = []
        for r in rules_in:
            for s in r["sources"]:
                if s not in sources:
                    sources.append(s)
        L("dingtalk", "chat_names", sources)
        S("dingtalk", "time_filter", tf)
        S("dingtalk", "time_window", window)
        S("dingtalk", "ui_poll_interval", poll)
        S("dingtalk", "anchor_guard", bool(self.st["anchor"].get()))
        S("dingtalk", "title_check", bool(self.st["title"].get()))
        S("dingtalk", "title_ratio", ratio)
        S("wechat", "dry_run", bool(self.st["dry_run"].get()))
        S("wechat", "interval", interval)
        S("wechat", "show_hotkey", hotkey)

        # ---- 钉钉通知 ----
        S("notify", "enabled", n_enabled)
        S("notify", "webhook", n_webhook)
        S("notify", "secret", self.st["n_secret"].get().strip())
        S("notify", "keyword", self.st["n_keyword"].get().strip())
        L("notify", "at_mobiles", n_mobiles)
        S("notify", "at_all", bool(self.st["n_at_all"].get()))
        S("notify", "cooldown", n_cooldown)
        S("notify", "log_alerts", bool(self.st["n_log_alerts"].get()))
        S("notify", "notify_start", bool(self.st["n_start"].get()))
        S("notify", "notify_fail", bool(self.st["n_fail"].get()))
        S("notify", "fail_threshold", n_threshold)
        S("notify", "notify_idle", bool(self.st["n_idle"].get()))
        S("notify", "idle_minutes", n_idle_min)
        S("notify", "idle_window", n_idle_win)
        S("notify", "report_hourly", bool(self.st["n_hourly"].get()))
        S("notify", "report_daily_at", n_daily)

        if problems:
            messagebox.showerror(
                "保存失败",
                "以下配置项没能写入：\n" + "\n".join(problems) +
                "\n\n配置可能被手工改成了非标准结构，请打开 config.yaml 检查。",
                parent=self.win)
            return

        self.app.set_autostart_enabled(bool(self.st["autostart"].get()))
        self.app.reload_after_settings()
        notify_line = ("钉钉通知：已启用，故障会发到群里。"
                       if n_enabled else "钉钉通知：未启用。")
        messagebox.showinfo(
            "已保存",
            "设置已保存（原配置已备份到 data/backup/）。\n\n"
            + ("当前仍是【试运行】模式，不会真实发送。" if
               self.st["dry_run"].get() else "当前为【正式发送】模式。")
            + "\n" + notify_line
            + "\n\n若转发任务正在运行，请停止后重新开始。",
            parent=self.win)
        self.win.destroy()


def open_dialog(app, config_path: Path, cfg: dict, env: dict) -> SettingsDialog:
    return SettingsDialog(app, config_path, cfg, env)
