"""设置中心（独立窗口，分页签）。

为什么单独一个模块
    界面逻辑集中在这里，「设置能改什么」一眼可见：
        转发规则   —— 哪个钉钉群 → 哪个（哪些）微信群，可自由增删
        时间范围   —— 不限 / 仅当天 / 只在指定时段
        采集与投递 —— 轮询间隔、发送间隔、试运行、两道防护
        消息格式   —— 转发文案模板（带实时预览）
        其他       —— 开机自启、环境信息、快捷入口

    保存时会先备份 config.yaml，再按「保留注释的按行手术」写回
    （见 app/cfgfile.py），不会把配置文件里的说明注释洗掉。
"""
from __future__ import annotations

import re
import tkinter as tk
from tkinter import messagebox, ttk
from pathlib import Path

import cfgfile

WINDOW_RE = re.compile(r"^\s*\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}\s*$")

BG = "#eef1f7"
CARD = "#ffffff"
BORDER = "#e2e7f0"
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


def _scroll_area(parent) -> tk.Frame:
    """返回一个可垂直滚动的内层 Frame。"""
    canvas = tk.Canvas(parent, bg=CARD, highlightthickness=0)
    bar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
    inner = tk.Frame(canvas, bg=CARD)
    window = canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=bar.set)
    canvas.pack(side="left", fill="both", expand=True)
    bar.pack(side="right", fill="y")

    def _resize(_evt=None):
        canvas.configure(scrollregion=canvas.bbox("all"))
        canvas.itemconfigure(window, width=canvas.winfo_width())

    inner.bind("<Configure>", _resize)
    canvas.bind("<Configure>", _resize)
    canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(
        -1 * (e.delta // 120), "units"))
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
        self.win.geometry("840x660")
        self.win.minsize(760, 560)
        self.win.configure(bg=BG)
        self.win.transient(app)

        self._build()
        self.win.grab_set()
        self.win.focus_force()

    # ------------------------------------------------------------ 布局

    def _build(self):
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
        self._tab_misc(nb)

        bar = tk.Frame(self.win, bg=BG)
        bar.pack(fill="x", padx=18, pady=(4, 14))
        tk.Button(bar, text="保 存", command=self.save, bg=ACCENT, fg="#ffffff",
                  activebackground="#2456bd", activeforeground="#ffffff",
                  relief="flat", bd=0, font=("Microsoft YaHei UI", 10, "bold"),
                  padx=22, pady=8, cursor="hand2").pack(side="right")
        ttk.Button(bar, text="取消", command=self.win.destroy).pack(side="right",
                                                                   padx=8)
        ttk.Button(bar, text="还原本次修改", command=self._reload).pack(side="left")

    def _page(self, nb, title: str) -> tk.Frame:
        page = tk.Frame(nb, bg=BG)
        nb.add(page, text=f"   {title}   ")
        return page

    # ------------------------------------------------------------ 转发规则

    def _tab_rules(self, nb):
        page = self._page(nb, "转发规则")
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
        inner = _scroll_area(holder)

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

        if problems:
            messagebox.showerror(
                "保存失败",
                "以下配置项没能写入：\n" + "\n".join(problems) +
                "\n\n配置可能被手工改成了非标准结构，请打开 config.yaml 检查。",
                parent=self.win)
            return

        self.app.set_autostart_enabled(bool(self.st["autostart"].get()))
        self.app.reload_after_settings()
        messagebox.showinfo(
            "已保存",
            "设置已保存（原配置已备份到 data/backup/）。\n\n"
            + ("当前仍是【试运行】模式，不会真实发送。" if
               self.st["dry_run"].get() else "当前为【正式发送】模式。")
            + "\n\n若转发任务正在运行，请停止后重新开始。",
            parent=self.win)
        self.win.destroy()


def open_dialog(app, config_path: Path, cfg: dict, env: dict) -> SettingsDialog:
    return SettingsDialog(app, config_path, cfg, env)
