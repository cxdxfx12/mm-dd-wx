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


def _bridge_module():
    """按需导入 bridge 包（界面进程的 sys.path 里未必有仓库根）。"""
    import sys
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    import bridge.notify as m
    return m


def default_test_text() -> str:
    """默认测试文案，取自 bridge.notify —— 与命令行 --notify-test 同一份。"""
    global _DEFAULT_TEXT_CACHE
    if _DEFAULT_TEXT_CACHE is None:
        try:
            _DEFAULT_TEXT_CACHE = _bridge_module().DEFAULT_TEST_TEXT
        except Exception:
            # 取不到文案不该让设置窗口打不开，退化成一句等价的提示
            _DEFAULT_TEXT_CACHE = "测试消息：喵喵钉钉-微信消息转发桥通知通道正常。"
    return _DEFAULT_TEXT_CACHE


def robot_rows_from_config(n: dict) -> list[tuple[str, str, str]]:
    """把 notify 段读成 ``[(地址, 加签密钥, 关键词), …]``。

    解析规则只在 bridge/notify.py 里有一份，这里复用，避免两处各写一套
    后来改歪。导入失败就退化成「只认老的单个 webhook」，不让设置窗口打不开。
    """
    try:
        m = _bridge_module()
        items = list(n.get("webhooks") or [])
        lines = items if items else [n.get("webhook") or ""]
        specs = m.robots_from_lines(lines, str(n.get("secret") or ""),
                                    str(n.get("keyword") or ""))
        return [(s.url, s.secret, s.keyword) for s in specs]
    except Exception:
        url = str(n.get("webhook") or "").strip()
        if not url:
            return []
        return [(url, str(n.get("secret") or ""), str(n.get("keyword") or ""))]


def robot_lines_for_config(specs: list[tuple[str, str, str]]) -> list[str]:
    """反向：把界面上的机器人行拼成写回 config.yaml 的单行写法。"""
    try:
        m = _bridge_module()
        return [m.RobotSpec(u, s, k).as_line() for u, s, k in specs]
    except Exception:
        return ["|".join([u, s, k]).rstrip("| ") for u, s, k in specs]


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


# ---------------------------------------------------------------- 自动巡检档位
#
# 档位与 bridge/sources/android_gateway.WATCH_CHOICES 必须一致（那边夹值、
# 这边给选项）。界面上用字符串 label 存、保存时再换回分钟数 ——
# ttk.Combobox 直接绑 int 会出现「5」和 5 不相等、选中态丢掉的怪事。

WATCH_CHOICES = (0, 5, 10, 15, 30)
WATCH_LABELS = {0: "关闭（不巡检）", 5: "每 5 分钟", 10: "每 10 分钟",
                15: "每 15 分钟", 30: "每 30 分钟"}


def _watch_label(minutes) -> str:
    """配置里的分钟数 → 下拉框显示的文字。认不出的值一律按「关闭」显示。"""
    try:
        v = int(minutes)
    except (TypeError, ValueError):
        return WATCH_LABELS[0]
    return WATCH_LABELS.get(v, WATCH_LABELS[0])


def watch_minutes(label: str) -> int:
    """下拉框文字 → 分钟数。"""
    for k, v in WATCH_LABELS.items():
        if v == label:
            return k
    return 0


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
        self.st["table_image"] = tk.BooleanVar(
            value=bool(w.get("table_image", True)))
        self.st["interval"] = tk.StringVar(value=str(w.get("interval", 3)))
        self.st["hotkey"] = tk.StringVar(
            value=str(w.get("show_hotkey") or "{Ctrl}{Alt}w"))
        self.st["poll"] = tk.StringVar(value=str(d.get("ui_poll_interval", 5)))
        self.st["anchor"] = tk.BooleanVar(value=bool(d.get("anchor_guard", True)))
        self.st["title"] = tk.BooleanVar(value=bool(d.get("title_check", True)))
        self.st["ratio"] = tk.StringVar(value=str(d.get("title_ratio", 0.55)))

        # ---- 数据来源（dingtalk.mode）----
        # 这是「消息从哪来」的总开关，放最上面：它决定下面一堆参数有没有意义。
        mode_now = str(d.get("mode") or "ocr")
        self.st["src"] = tk.StringVar(value=mode_now)
        self.st["gw_host"] = tk.StringVar(value=str(d.get("gateway_host") or "0.0.0.0"))
        self.st["gw_port"] = tk.StringVar(value=str(d.get("gateway_port") or 8765))
        self.st["gw_token"] = tk.StringVar(value=str(d.get("gateway_token") or ""))
        self.st["gw_parse"] = tk.BooleanVar(value=bool(d.get("android_parse")))
        self.st["gw_offline"] = tk.StringVar(
            value=str(d.get("android_offline_after", 300)))
        # 自动巡检（2026-10-20）：下拉选档，不给自由填 —— 这个数字决定
        # 「手机被切走后最多丢多久的群消息」，可选项多了用户反而不会算。
        self.st["gw_watch"] = tk.StringVar(value=_watch_label(
            d.get("android_watch_min", 0)))

        srcbox = _card(page, "数据来源：消息从哪儿读")
        srcbox.pack(fill="x", padx=12, pady=(12, 8))

        ttk.Radiobutton(
            srcbox, text="PC 钉钉（截屏 + 文字识别）",
            value="ocr", variable=self.st["src"],
            command=self._on_source_radio).pack(anchor="w", padx=14,
                                                pady=(10, 0))
        _tip(srcbox, "电脑上开着钉钉、窗口停在目标群即可。窗口可以被别的窗口挡住，"
                     "但不能最小化；识别有极小概率认错字。").pack(fill="x", padx=30)

        ttk.Radiobutton(
            srcbox, text="手机钉钉（手机 App 无障碍上报）",
            value="android", variable=self.st["src"],
            command=self._on_source_radio).pack(anchor="w", padx=14,
                                                pady=(8, 0))
        _tip(srcbox, "需要一台安卓手机：装好采集 App、开着无障碍、停在目标群且屏幕不黑。"
                     "读到的是精确原文，没有识别误差，比截图识别可靠；代价是这台手机不能"
                     "再干别的。").pack(fill="x", padx=30)

        grid2 = tk.Frame(srcbox, bg=CARD)
        grid2.pack(fill="x", padx=14, pady=(8, 4))
        self._entry_row(grid2, 0, "监听地址", self.st["gw_host"], 16,
                        "默认 0.0.0.0（局域网都能连，手机才连得上）。只在本机自测时改 127.0.0.1。")
        self._entry_row(grid2, 1, "监听端口", self.st["gw_port"], 8,
                        "手机端填 http://<本机IP>:这个端口")
        self._entry_row(grid2, 2, "访问 token", self.st["gw_token"], 24,
                        "手机端必须填一样的。能往这个端口发东西就等于能往你的微信群发消息，"
                        "所以不允许留空。")

        ttk.Checkbutton(srcbox, text="把手机报来的画面解析成消息并转发",
                        variable=self.st["gw_parse"]).pack(anchor="w", padx=14,
                                                           pady=(6, 0))
        _tip(srcbox, "手机模式下**必须**勾选，否则网关只收不转、一条消息都不会发出去。"
                     "（默认不勾是安全考虑：先确认解析结果对不对。）"
             ).pack(fill="x", padx=30)

        # 失联告警：手机被系统清理、锁屏断网之后，电脑侧收不到任何异常，
        # 表现就是「程序安安静静在跑、一条都不转」。这一项是唯一的发现手段。
        self._spin(grid2, 3, "失联告警（秒）", self.st["gw_offline"], 0, 3600,
                   "手机超过这么久没心跳就发钉钉告警（0 = 不检测）。"
                   "手机端 0.2.0 起每 60 秒发一次心跳，填 300（5 分钟）即可；"
                   "旧版 APK 没有心跳，此项对它不生效。")
        _tip(srcbox, "关掉失联告警的后果：手机端挂了以后不会有任何提示，"
                     "只能靠「群里怎么没消息了」自己察觉 —— 而那通常很久之后才会被注意到。"
             ).pack(fill="x", padx=30)

        # ---- 自动巡检 ----
        watch_row = tk.Frame(srcbox, bg=CARD)
        watch_row.pack(fill="x", padx=14, pady=(8, 0))
        tk.Label(watch_row, text="自动巡检", bg=CARD, fg=TEXT,
                 font=F_UI).pack(side="left")
        wb = ttk.Combobox(watch_row, textvariable=self.st["gw_watch"],
                          state="readonly", width=14, font=F_UI,
                          values=[WATCH_LABELS[k] for k in WATCH_CHOICES])
        wb.pack(side="left", padx=8)
        _tip(watch_row, "到点检查一次：钉钉不在目标群就自动点回去。",
             bg=CARD).pack(side="left")
        _tip(srcbox, "「关闭」时手机被切走不会自己恢复，只能靠人在手机上看一眼。"
                     "打开后手机会按选定的间隔把钉钉拉回目标群。"
             ).pack(fill="x", padx=30)
        _tip(srcbox, "⚠ 两个代价，开之前想清楚：① 到点会抢前台 —— 手机上不管在干什么"
                     "都会被切到钉钉，选 5 分钟就基本干不了别的；"
                     "② 有盲区 —— 手机被切走那段时间的群消息根本没读到，补不回来，"
                     "间隔越大盲区越大。",
             bg=WARN_BG, fg=WARN_FG).pack(fill="x", padx=30, pady=(0, 12))

        box = _card(page, "投递")
        box.pack(fill="x", padx=12, pady=(12, 8))
        ttk.Checkbutton(box, text="试运行模式：只打印日志，不真的发到微信",
                        variable=self.st["dry_run"]).pack(anchor="w", padx=14,
                                                          pady=(10, 2))
        _tip(box, "首次使用建议先开着（当前配置就是），确认识别到的内容无误后取消勾选，"
                  "再点「开始运行」就是真实转发了。").pack(fill="x", padx=14)
        ttk.Checkbutton(box, text="钉钉卡片里的表格转成图片发到微信",
                        variable=self.st["table_image"]).pack(anchor="w", padx=14,
                                                              pady=(2, 2))
        _tip(box, "微信没有表格消息类型。开着时，识别到表格（如「小件员名称: xx ｜ "
                  "票数: 1票 ｜ 单号: xx」）会把表格画成一张图，在文字之后补发。"
                  "文字照常发，图片失败不影响文字。").pack(fill="x", padx=14)
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

    def _on_source_radio(self):
        """选中「手机钉钉」时自动勾上后台解析。

        不自动勾的话，用户切到手机模式、保存、开始运行 —— 网关只收不转，
        一条消息都发不出去，而且日志里只有一句 INFO。这个坑比「自动勾了」
        危险得多，所以默认帮用户勾上；真想关，保存时会二次确认。
        """
        if self.st["src"].get() == "android" and not self.st["gw_parse"].get():
            self.st["gw_parse"].set(True)

    def _entry_row(self, parent, row: int, label: str, var, width: int,
                   note: str = ""):
        """标签 + 输入框 + 说明。用于地址/端口/token 这类不能是数字的项。"""
        blk = tk.Frame(parent, bg=CARD)
        blk.grid(row=row, column=0, sticky="w", pady=4)
        tk.Label(blk, text=label, bg=CARD, fg=TEXT, font=F_UI,
                 width=8, anchor="w").pack(side="left")
        ttk.Entry(blk, textvariable=var, width=width).pack(side="left",
                                                           padx=(4, 8))
        if note:
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

        # ---- 机器人清单 ----
        # 一个群里常常不止一个机器人（每个上游系统挂一个），通知要能同时
        # 发给他们，所以这里做成可增删的多行：一行一个机器人，
        # 各带自己的加签密钥 / 关键词 —— 加签是每机器人一套的，不能只留全局一个。
        box = _card(page, "机器人凭证（钉钉群 → 群设置 → 智能群助手 → 添加机器人 → 自定义）")
        box.pack(fill="x", padx=12, pady=(12, 8))
        ttk.Checkbutton(box, text="启用钉钉通知（故障告警 + 运行报告）",
                        variable=self.st["n_enabled"]).pack(anchor="w", padx=14,
                                                            pady=(10, 4))
        tk.Label(box, text="机器人（一个机器人一行，可以加多个；通知会发给每一个）：",
                 bg=CARD, fg=MUTED, font=F_SMALL).pack(anchor="w", padx=14,
                                                       pady=(4, 0))
        self._robots_box = tk.Frame(box, bg=CARD)
        self._robots_box.pack(fill="x", padx=14, pady=(2, 2))
        self._robot_rows: list[dict] = []
        for _u, _s, _k in robot_rows_from_config(n):
            self._add_robot_row(_u, _s, _k)
        if not self._robot_rows:                # 一条都没有时给个空行，免得没处下手
            self._add_robot_row()
        ttk.Button(box, text="＋ 添加一个机器人",
                   command=lambda: self._add_robot_row()).pack(anchor="w",
                                                               padx=14,
                                                               pady=(2, 2))
        _tip(box, "「加签密钥」填 SEC 开头那串，机器人开了「加签」才需要；"
                  "「关键词」机器人开了「自定义关键词」才需要（内容里缺这个词"
                  "会自动补一行）。两个都是**每个机器人各自**的设置，留空即不用。",
             bg=CARD).pack(anchor="w", padx=16, pady=(0, 8))

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

    # ---- 「钉钉通知」里的机器人行 ----

    def _add_robot_row(self, url: str = "", secret: str = "",
                       keyword: str = "") -> None:
        """加一行「一个机器人」。地址留空的行保存时会被忽略。

        行内容刻意做成独立的 StringVar，而不是共享的大 Text ——
        用户点「＋ 添加」时新行直接出现在下面，删行也不会影响其它行。
        """
        row = tk.Frame(self._robots_box, bg=CARD)
        row.pack(fill="x", pady=2)
        v_url, v_sec, v_kw = (tk.StringVar(value=url), tk.StringVar(value=secret),
                              tk.StringVar(value=keyword))
        tk.Label(row, text="地址", bg=CARD, fg=MUTED, font=F_SMALL,
                 width=4, anchor="w").pack(side="left")
        ttk.Entry(row, textvariable=v_url).pack(side="left", fill="x",
                                                expand=True, padx=(2, 8))
        tk.Label(row, text="加签密钥", bg=CARD, fg=MUTED,
                 font=F_SMALL).pack(side="left")
        ttk.Entry(row, textvariable=v_sec, width=17).pack(side="left", padx=(2, 8))
        tk.Label(row, text="关键词", bg=CARD, fg=MUTED,
                 font=F_SMALL).pack(side="left")
        ttk.Entry(row, textvariable=v_kw, width=9).pack(side="left", padx=(2, 8))
        ttk.Button(row, text="删除", width=5,
                   command=lambda r=row: self._del_robot_row(r)).pack(side="left")
        self._robot_rows.append({"row": row, "url": v_url,
                                 "secret": v_sec, "keyword": v_kw})

    def _del_robot_row(self, row) -> None:
        for item in list(self._robot_rows):
            if item["row"] is row:
                self._robot_rows.remove(item)
        try:
            row.destroy()
        except Exception:
            pass

    def _robot_specs_now(self) -> list[tuple[str, str, str]]:
        """当前界面上的机器人（丢掉空地址、按地址去重）。"""
        out: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for item in self._robot_rows:
            try:
                url = item["url"].get().strip()
            except Exception:               # 控件已销毁
                continue
            if not url or url in seen:
                continue
            seen.add(url)
            out.append((url, item["secret"].get().strip(),
                        item["keyword"].get().strip()))
        return out

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
        specs = self._robot_specs_now()
        if not specs:
            messagebox.showwarning(
                "还没填 webhook",
                "请先在「机器人」那一行填入自定义机器人的 webhook 地址。\n\n"
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
        self._test_lab.configure(text=f"正在发送到 {len(specs)} 个机器人…",
                                 fg=MUTED)
        vals = (list(specs), content)

        def work():
            try:
                m = _bridge_module()
                # 每个机器人用自己那套加签 / 关键词 —— 所以逐个构造后一起发
                nt = m.DingTalkNotifier(
                    robots=[m.RobotSpec(u, s, k) for u, s, k in vals[0]])
                ok, detail = nt.send_custom(vals[1])
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
            self._test_lab.configure(text=f"✓ 已发送（{detail}），请到钉钉群确认",
                                     fg="#0b6b3a")
            # detail 不是 "ok" 就说明有机器人没发成功 —— 得让人看见是哪一个
            if detail == "ok":
                messagebox.showinfo("发送成功", "消息已发到钉钉群。",
                                    parent=self.win)
            else:
                messagebox.showwarning(
                    "部分机器人没发出去",
                    f"{detail}\n\n"
                    "已经发成功的那几个是正常的；没成功的请对着它自己的"
                    "「加签密钥 / 关键词」检查一遍。",
                    parent=self.win)
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

        # ---- 数据来源 + 手机网关 ----
        src_mode = str(self.st["src"].get() or "ocr")
        gw_host = self.st["gw_host"].get().strip() or "0.0.0.0"
        gw_token = self.st["gw_token"].get().strip()
        gw_parse = bool(self.st["gw_parse"].get())
        try:
            gw_port = int(float(self.st["gw_port"].get()))
            if not (1 <= gw_port <= 65535):
                raise ValueError
        except ValueError:
            messagebox.showwarning(
                "监听端口不对", "端口要填 1–65535 之间的整数，例如 8765。",
                parent=self.win)
            return
        try:
            gw_offline = int(float(self.st["gw_offline"].get() or 300))
        except ValueError:
            gw_offline = 300
        # 0 = 关闭（合法的「我知道风险，不检测」）；负数按关闭处理，
        # 而不是变成一个极小值——那会让告警每 10 秒刷一次。
        gw_offline = max(0, min(3600, gw_offline))
        if src_mode == "android":
            # 这两个是「选了手机却一条都收不到」的头号原因，保存时就拦住，
            # 别让用户回去对着日志猜。
            if not gw_token:
                messagebox.showwarning(
                    "还缺访问 token",
                    "手机模式下必须填一个 token，且与手机 App 里填的完全一致。\n\n"
                    "能往这个端口发东西就等于能往你的微信群发消息，所以不允许留空。\n"
                    "随便一串随机字符即可。",
                    parent=self.win)
                return
            if not gw_parse and not messagebox.askyesno(
                    "手机模式但没开解析",
                    "你选了「手机钉钉」，但没勾「把手机报来的画面解析成消息并转发」。\n\n"
                    "这样网关只会收画面、**一条消息都不会发出去**（这是刻意的安全默认值）。\n\n"
                    "确定就这样保存吗？", parent=self.win):
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
        n_robots = self._robot_specs_now()          # [(地址, 加签密钥, 关键词), …]
        if n_enabled and not n_robots:
            messagebox.showwarning(
                "启用了通知但没填 webhook",
                "请至少填一个钉钉机器人的 webhook 地址，或者取消「启用钉钉通知」。\n\n"
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

        def S(section, key, value, after=""):
            # 「改或插」：老配置里没有新版本加的键（比如 dingtalk.time_window）
            # 时，set_scalar 只会改已存在的键、直接报写入失败 —— 那会让整个
            # 设置存不下来。所以这里统一走 set_or_insert_scalar。
            if not cfgfile.set_or_insert_scalar(self.path, section, key, value,
                                                after_key=after):
                problems.append(f"{section}.{key}")

        def L(section, key, items, after=""):
            if not (cfgfile.set_list(self.path, section, key, items)
                    or cfgfile.ensure_list_key(self.path, section, key, items,
                                               after_key=after)):
                problems.append(f"{section}.{key}")

        if not cfgfile.set_routes(self.path, rules_in):
            problems.append("routes")

        # 各键的 after= 描述的是「样板文件里的先后顺序」：老配置缺哪个键，
        # 就地补在它该在的位置，而不是堆到段尾让人看不懂。
        sources: list[str] = []
        for r in rules_in:
            for s in r["sources"]:
                if s not in sources:
                    sources.append(s)
        L("dingtalk", "chat_names", sources)
        # mode 在样板文件里是 dingtalk 段的第一个键，所以插入点留空（段首）。
        S("dingtalk", "mode", src_mode)
        S("dingtalk", "ui_poll_interval", poll, after="chat_names")
        S("dingtalk", "title_check", bool(self.st["title"].get()),
          after="ui_poll_interval")
        S("dingtalk", "title_ratio", ratio, after="title_check")
        S("dingtalk", "time_filter", tf, after="title_ratio")
        S("dingtalk", "time_window", window, after="time_filter")
        S("dingtalk", "anchor_guard", bool(self.st["anchor"].get()),
          after="time_window")
        # 手机网关参数：老的 config.yaml 里没有这些键，所以走「改或插」。
        S("dingtalk", "gateway_host", gw_host, after="anchor_guard")
        S("dingtalk", "gateway_port", gw_port, after="gateway_host")
        S("dingtalk", "gateway_token", gw_token, after="gateway_port")
        S("dingtalk", "android_parse", gw_parse, after="gateway_token")
        S("dingtalk", "android_offline_after", gw_offline, after="android_parse")
        # 自动巡检。老配置里没有这个键，走「改或插」；插入点排在
        # android_offline_after 之后，保持这一组安卓参数挨在一起。
        S("dingtalk", "android_watch_min",
          watch_minutes(str(self.st["gw_watch"].get())),
          after="android_offline_after")
        S("wechat", "dry_run", bool(self.st["dry_run"].get()), after="interval")
        S("wechat", "interval", interval, after="dry_run")
        S("wechat", "show_hotkey", hotkey, after="wxauto_path")
        S("wechat", "table_image", bool(self.st["table_image"].get()),
          after="show_hotkey")

        # ---- 钉钉通知 ----
        S("notify", "enabled", n_enabled)
        # 多个机器人写进 notify.webhooks（一个机器人一项，可带自己的加签/关键词）。
        # 同时把第一个地址回填到老的 notify.webhook：
        #   · 万一有人拿旧版本 exe 读这份配置，仍然能发得出去
        #   · 手工打开 config.yaml 时也一眼能看到主地址
        # 采集侧只认 webhooks 非空就用它，所以不会重复发送。
        L("notify", "webhooks", robot_lines_for_config(n_robots),
          after="webhook")
        S("notify", "webhook", n_robots[0][0] if n_robots else "",
          after="webhooks")
        L("notify", "at_mobiles", n_mobiles, after="keyword")
        S("notify", "at_all", bool(self.st["n_at_all"].get()),
          after="at_mobiles")
        S("notify", "cooldown", n_cooldown, after="at_all")
        S("notify", "log_alerts", bool(self.st["n_log_alerts"].get()),
          after="cooldown")
        S("notify", "notify_start", bool(self.st["n_start"].get()),
          after="log_alerts")
        S("notify", "notify_fail", bool(self.st["n_fail"].get()),
          after="notify_start")
        S("notify", "fail_threshold", n_threshold, after="notify_fail")
        S("notify", "notify_idle", bool(self.st["n_idle"].get()),
          after="fail_threshold")
        S("notify", "idle_minutes", n_idle_min, after="notify_idle")
        S("notify", "idle_window", n_idle_win, after="idle_minutes")
        S("notify", "report_hourly", bool(self.st["n_hourly"].get()),
          after="idle_window")
        S("notify", "report_daily_at", n_daily, after="report_hourly")

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
