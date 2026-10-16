"""服务编排：采集 → 去重落库 → 规则路由 → 格式化 → 限流 → 投递 → 记录。

另外负责「出事要有人知道」：投递连续失败、采集停滞、定时运行报告，
都通过 bridge/notify.py 的钉钉机器人发出去。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from datetime import datetime

from .config import Config
from .formatter import DEFAULT_TEMPLATE, render
from .models import Message
from .notify import DingTalkNotifier, in_hhmm_window, install_alert_handler
from .router import Router
from .storage import Storage
from .targets import build_target
from .targets.base import Target

log = logging.getLogger("bridge.service")

WATCHDOG_TICK = 30.0          # 看门狗巡检间隔（秒）


class BridgeService:
    def __init__(self, cfg: Config, target: Target | None = None):
        self.cfg = cfg
        self.storage = Storage(cfg.resolve(cfg.storage.db_path))
        self.router = Router(cfg.routes)
        self.target = target or build_target(cfg)
        self.counter: Counter = Counter()
        self.last_target_check: tuple[bool, str] = (True, "尚未检查")
        self._last_send = 0.0
        self._send_lock = threading.Lock()

        # ---- 通知相关 ----
        self.notifier: DingTalkNotifier | None = DingTalkNotifier.from_config(
            getattr(cfg, "notify", None))
        self._alert_handler: logging.Handler | None = None
        self._consec_fail = 0
        self._last_activity = time.time()
        self._last_msg_desc = ""
        self._idle_alerted = False
        self._watchdog: threading.Thread | None = None
        self._watchdog_stop = threading.Event()
        self._hour_key = datetime.now().strftime("%Y-%m-%d %H")
        self._day_key = datetime.now().strftime("%Y-%m-%d")
        self._started_at: datetime | None = None

    # ---------- 核心处理 ----------

    def handle(self, msg: Message) -> bool:
        """处理一条消息。返回 True 表示确认为新消息（无论是否真的转发成功）。"""
        if self.storage.is_duplicate(msg.uid):
            self.counter["duplicate"] += 1
            log.debug("重复消息，跳过：%s", msg.uid)
            return False

        self.storage.save_message(msg)
        self.counter["received"] += 1
        # 有新消息 = 采集端活着：解除停滞状态
        self._last_activity = time.time()
        self._last_msg_desc = f"{msg.sender_name}：{(msg.content or '')[:40]}"
        self._idle_alerted = False

        matched = self.router.match(msg)
        if not matched.targets:
            self.counter["filtered"] += 1
            log.info("未命中转发规则，仅入库：%s | %s", msg.company, msg.content[:30])
            return True

        text = render(msg, matched.template or DEFAULT_TEMPLATE)
        for tgt in matched.targets:
            self._throttle()
            result = self.target.send_text(tgt, text)
            self.storage.log_delivery(
                msg.uid, tgt, "ok" if result.ok else "fail", result.detail
            )
            if result.ok:
                self.counter["sent"] += 1
                self._consec_fail = 0
                log.info("已投递 → %s", tgt)
            else:
                self.counter["failed"] += 1
                self._consec_fail += 1
                log.error("投递失败 → %s | %s", tgt, result.detail)
                self._alert_delivery_fail(tgt, result.detail)
        return True

    def _alert_delivery_fail(self, target: str, detail: str) -> None:
        """连续失败达到阈值才告警 —— 偶发一次多半是微信弹窗抢焦点，不值得打扰人。"""
        n = max(1, int(getattr(self.cfg.notify, "fail_threshold", 3) or 3))
        if not (self.notifier and getattr(self.cfg.notify, "notify_fail", True)):
            return
        if self._consec_fail < n:
            return
        notified = self.notifier.alert(
            "deliver-fail",
            f"❌ 微信投递连续失败 {self._consec_fail} 次\n"
            f"最近失败目标：{target}\n"
            f"失败原因：{detail}\n\n"
            f"常见原因：微信没登录 / 群名与客户端不一致 / 微信窗口被最小化或收进"
            f"托盘（点最小化、按 Esc 都会这样）/ 窗口被弹窗挡住。\n"
            f"运行中的程序会自动唤回窗口；仍失败请人工点开微信看一眼。\n"
            f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        )
        if notified:
            self._consec_fail = 0

    def _throttle(self) -> None:
        """相邻两条发送之间保持最小间隔，降低微信风控风险。"""
        interval = max(0.0, float(self.cfg.wechat.interval))
        with self._send_lock:
            delta = time.time() - self._last_send
            if delta < interval:
                time.sleep(interval - delta)
            self._last_send = time.time()

    # ---------- 通知：生命周期 ----------

    def announce(self, text: str) -> bool:
        """发一条通知（不带冷却去重），用于开始/停止这类一次性事件。"""
        if not self.notifier:
            return False
        ok, _ = self.notifier.send_text(text)
        return ok

    def announce_start(self, source_desc: str = "") -> None:
        self._started_at = datetime.now()
        self._last_activity = time.time()
        self._idle_alerted = False
        if not (self.notifier and getattr(self.cfg.notify, "notify_start", True)):
            return
        mode = "试运行（不会真发）" if self.cfg.wechat.dry_run else "正式发送"
        routes = [f"{'/'.join(r.chat_names) or '不限'} → {'、'.join(r.targets)}"
                  for r in self.cfg.routes if r.targets]
        self.announce(
            f"▶️ 转发桥已启动\n"
            f"模式：{mode}\n"
            f"采集：{source_desc or self.cfg.dingtalk.mode}\n"
            f"规则：{'; '.join(routes) or '(无)'}\n"
            f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}")

    def announce_stop(self, reason: str = "") -> None:
        if not (self.notifier and getattr(self.cfg.notify, "notify_start", True)):
            return
        mins = 0
        if self._started_at:
            mins = int((datetime.now() - self._started_at).total_seconds() // 60)
        self.announce(
            f"⏹ 转发桥已停止{'（' + reason + '）' if reason else ''}\n"
            f"{self.summary()}\n"
            f"本次运行：{mins} 分钟\n"
            f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}")

    # ---------- 通知：运行报告 ----------

    def report_text(self, period: str = "运行报告") -> str:
        s = self.storage.stats()
        idle = int(time.time() - self._last_activity) // 60
        last = self._last_msg_desc or "（本次运行还没有消息）"
        return (
            f"📊 {period}\n"
            f"收到 {self.counter['received']} | 重复 {self.counter['duplicate']} | "
            f"未命中 {self.counter['filtered']}\n"
            f"已发送 {self.counter['sent']} | 失败 {self.counter['failed']}\n"
            f"累计入库 {s['total_messages']}\n"
            f"距上一条消息：{idle} 分钟\n"
            f"最近一条：{last}")

    def send_report(self, period: str = "手动查询") -> bool:
        if not self.notifier:
            return False
        ok, _ = self.notifier.send_text(self.report_text(period))
        return ok

    def db_report_text(self, period: str = "手动查询") -> str:
        """不依赖本次进程计数的报告 —— 给命令行单发报告时用。

        `report_text` 里的计数器是内存态，单独跑一个进程查历史时全是 0，
        那种报告没有意义，所以这里改成读数据库。
        """
        s = self.storage.stats()
        d = s.get("deliveries") or {}
        return (
            f"📊 {period}\n"
            f"累计入库消息：{s['total_messages']} 条\n"
            f"累计投递成功：{int(d.get('ok', 0))} | 失败：{int(d.get('fail', 0))}\n"
            f"通知通道：{self.notify_status()}\n"
            f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}")

    def send_db_report(self, period: str = "手动查询") -> bool:
        if not self.notifier:
            return False
        ok, _ = self.notifier.send_text(self.db_report_text(period))
        return ok

    # ---------- 看门狗 ----------

    def start_watchdog(self) -> None:
        """挂上日志告警 Handler 并启动巡检线程。正式运行时调用一次即可。"""
        if not self.notifier:
            log.info("未启用钉钉通知，跳过告警与巡检")
            return
        if self._watchdog and self._watchdog.is_alive():
            return
        if getattr(self.cfg.notify, "log_alerts", True):
            self._alert_handler = install_alert_handler(self.notifier)
        self._watchdog_stop.clear()
        self._watchdog = threading.Thread(
            target=self._watchdog_loop, name="bridge-watchdog", daemon=True)
        self._watchdog.start()
        log.info("已启用钉钉通知：%s", self.notifier.describe())

    def _watchdog_loop(self) -> None:
        while not self._watchdog_stop.wait(WATCHDOG_TICK):
            try:
                self._watchdog_tick()
            except Exception as exc:                  # 巡检绝不能把主流程带崩
                log.debug("看门狗巡检异常：%s", exc)

    def _watchdog_tick(self) -> None:
        """一次巡检。整体套了兜底：通知通道坏掉不能反过来影响转发主流程。

        `_watchdog_loop` 外面还有一层保护，这里是第二道 —— 巡检本身也值得
        单独防一手，因为它会被反复调用，一次抛异常就少一轮检查。
        """
        try:
            self._tick_impl()
        except Exception as exc:
            log.warning("看门狗巡检异常（不影响转发）：%s", exc)

    def _tick_impl(self) -> None:
        n = self.cfg.notify
        if not self.notifier:
            return
        now = datetime.now()

        # 1) 采集停滞：长时间没有新消息。只在「本该有人说话」的时段判定，
        #    否则夜里安静会被误报成故障。
        if getattr(n, "notify_idle", True) and not self._idle_alerted:
            limit = max(5, int(getattr(n, "idle_minutes", 120) or 120)) * 60
            if (in_hhmm_window(now, getattr(n, "idle_window", "08:00-21:00"))
                    and time.time() - self._last_activity >= limit):
                self._idle_alerted = True
                self.notifier.alert(
                    "idle",
                    f"⏸ 采集疑似停滞\n"
                    f"已连续 {limit // 60} 分钟没有识别到新消息。\n\n"
                    f"常见原因：钉钉窗口被切到别的会话 / 被最小化 / 被人滚动了聊天区。\n"
                    f"请把钉钉窗口切回目标群并保持不最小化。\n"
                    f"时间：{now:%Y-%m-%d %H:%M:%S}")

        # 2) 每小时报告
        if getattr(n, "report_hourly", False):
            key = now.strftime("%Y-%m-%d %H")
            if key != self._hour_key:
                self._hour_key = key
                self.send_report(f"运行报告（{now.hour - 1 if now.hour else 23}:00 时段）")

        # 3) 每日报告
        at = str(getattr(n, "report_daily_at", "") or "").strip()
        if at and ":" in at:
            try:
                hh, mm = (int(x) for x in at.split(":")[:2])
            except ValueError:
                hh = mm = -1
            # 只在当天的这一个 30 秒窗口内触发（看门狗 30s 一跳，命中一次）
            if (hh == now.hour and abs(mm - now.minute) <= 1
                    and self._day_key != now.strftime("%Y-%m-%d")):
                self._day_key = now.strftime("%Y-%m-%d")
                self.send_report(f"每日运行报告（{now:%m-%d}）")

    # ---------- 生命周期 ----------

    def preflight(self) -> list[str]:
        """启动自检，返回问题列表（空列表表示全部通过）。

        副产物：投递端的检查结论存入 `last_target_check`，
        供调用方打印（自检通过时也需要让用户看到投递端状态）。
        """
        problems: list[str] = []
        if not self.cfg.wechat.targets and not any(r.targets for r in self.cfg.routes):
            problems.append("没有任何转发目标：请在 routes[].targets 中配置微信好友或群名")
        ok, detail = self.target.health_check()
        if not ok:
            # 只对「窗口不可用」这类可自愈的问题做一次修复尝试：
            # 微信被最小化 / 收进托盘时，启动瞬间就该把它唤回来，
            # 而不是丢一句报错让用户自己找原因。
            ensure = getattr(self.target, "ensure_visible", None)
            if callable(ensure):
                fixed, why = ensure()
                if fixed:
                    log.info("微信窗口已自动恢复：%s", why)
                    ok, detail = self.target.health_check()
        self.last_target_check = (ok, detail)
        if not ok:
            problems.append(f"微信投递端不可用：{detail}")
        return problems

    def notify_status(self) -> str:
        """给自检输出用的一行说明。"""
        n = self.cfg.notify
        if not getattr(n, "enabled", False):
            return "未启用"
        if not self.notifier:
            return "已开启但没填 webhook —— 不会生效"
        return self.notifier.describe()

    def close(self) -> None:
        self._watchdog_stop.set()
        if self._watchdog and self._watchdog.is_alive():
            self._watchdog.join(timeout=2.0)
        if self._alert_handler is not None:
            try:
                logging.getLogger("bridge").removeHandler(self._alert_handler)
            except Exception:
                pass
            self._alert_handler = None
        self.target.close()
        self.storage.close()

    def summary(self) -> str:
        s = self.storage.stats()
        return (
            f"运行统计：收到 {self.counter['received']} | 重复 {self.counter['duplicate']} | "
            f"未命中规则 {self.counter['filtered']} | 已发送 {self.counter['sent']} | "
            f"失败 {self.counter['failed']} | 累计入库 {s['total_messages']}"
        )
