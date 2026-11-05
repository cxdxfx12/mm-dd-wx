"""漏转对账：拿钉钉 PC 本地库当基准，查桥有没有漏消息。

为什么要有它
    手机端采集（无障碍 + OCR）看不到的东西，日志里也没有 —— 卡片被滑走、
    重绘间隙、附件抓取超时，表现都是「群里安静地少了一条」。用户没法知道
    少了哪条，只能凭印象怀疑。
    钉钉自己的库里每条消息都有毫秒时间戳和卡片标题，正好当**对账基准**：
    库里有的卡片，桥在 ±tolerance 分钟内一条都没入库 → 疑似漏转。

边界（必须清楚，否则会误判）
    1. **只有标题和时间戳**。2950 互动卡片的正文/表格/附件在服务端，本地
       只落一行 ``interactiveCardLastMessage``。所以判据只能是「标题对得上」
       或「时间对得上」，不能比正文。
    2. **只覆盖本机登录的那个钉钉账号**。手机采集用的账号与 PC 登录账号不是
       同一个时，这里一条都查不到 —— 这种情况会明确告警，不会假装「没有漏」。
    3. 桥把一张卡片拆成「正文 + 表格图 + 附件」多条属正常，反向的「多」不报。

告警去重
    同一张卡片只报一次（``reconcile-state.json`` 记已报过的 ``cid:ts``），
    否则每 10 分钟重报一遍历史，比不报还烦。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import dingtalk_db

log = logging.getLogger("bridge.reconcile")

STATE_NAME = "reconcile-state.json"
STATE_KEEP_DAYS = 7
DEFAULT_INTERVAL_MIN = 10.0
DEFAULT_LOOKBACK_MIN = 40.0
DEFAULT_TOLERANCE_MIN = 30.0
DEFAULT_MAX_GAP_H = 12.0
FIRST_DELAY_S = 90.0            # 启动后先缓一下：别跟采集/投递抢启动资源
MIN_GAP_BETWEEN_RUNS = 60.0     # 手动触发与自动巡检之间的最小间隔


def _norm(text: str) -> str:
    """比对用：去掉空白、标点、装饰符号，只留实义字符。"""
    return re.sub(r"[\s@#*【】\[\]（）()，。,.：:！!～~\-—|｜]", "", text or "")


def _fmt(ts: float) -> str:
    return time.strftime("%m-%d %H:%M:%S", time.localtime(ts))


@dataclass
class Finding:
    ts: float                 # 秒
    title: str


@dataclass
class Result:
    ok: bool = False
    reason: str = ""                       # ok=False 的原因（给日志/命令行看）
    chat_title: str = ""                   # 钉钉侧的群名
    cid: str = ""
    cards: int = 0                         # 窗口内钉钉卡片数
    forwarded: int = 0                     # 窗口内桥已入库条数
    missing: list[Finding] = field(default_factory=list)   # 本轮新发现的漏转
    db_latest: float = 0.0                 # 库里最新一条的时间（秒）
    window_min: float = 0.0
    elapsed: float = 0.0

    def lines(self, limit: int = 8) -> list[str]:
        out = [f"  {_fmt(f.ts)}  {f.title or '(无标题)'}" for f in self.missing[:limit]]
        if len(self.missing) > limit:
            out.append(f"  …另有 {len(self.missing) - limit} 条")
        return out


class Reconciler:
    """对账器。一个实例管一个群，线程安全（``run_once`` 内部加锁）。"""

    def __init__(self, cfg, bridge_db_path: str | Path,
                 notifier=None, work_dir: str | Path | None = None):
        self.cfg = cfg
        rc = getattr(cfg, "reconcile", None)
        self.rc = rc
        self.bridge_db = Path(bridge_db_path)
        self.notifier = notifier
        self.work_dir = Path(work_dir) if work_dir else self.bridge_db.parent / "dingtalk-db"
        self.state_path = self.bridge_db.parent / STATE_NAME

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_run = 0.0
        self._disabled_reason = ""         # 一旦确定不可用就记住，别每轮重复算
        self._warned: set[str] = set()
        self.last_result: Result | None = None

    # ---------- 配置 ----------

    def _num(self, name: str, default: float, lo: float = 0.0) -> float:
        raw = getattr(self.rc, name, default) if self.rc else default
        try:
            val = float(raw)
        except (TypeError, ValueError):
            log.warning("reconcile.%s=%r 不是数字，按默认 %g 处理", name, raw, default)
            return default
        return max(lo, val)

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.rc, "enabled", False)) if self.rc else False

    def _chat_keywords(self) -> tuple[str, str]:
        """（钉钉侧群名关键词, 桥侧群名关键词）。留空都取采集目标群。"""
        default = ""
        names = getattr(getattr(self.cfg, "dingtalk", None), "chat_names", None) or []
        if names:
            default = str(names[0])
        dt = (getattr(self.rc, "dingtalk_chat", "") or "").strip() or default
        br = (getattr(self.rc, "bridge_chat", "") or "").strip() or default
        return dt, br

    # ---------- 状态文件 ----------

    def _load_state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {"last_run": 0.0, "reported": {}}

    def _save_state(self, state: dict) -> None:
        cutoff = time.time() - STATE_KEEP_DAYS * 86400
        reported = {k: v for k, v in (state.get("reported") or {}).items()
                    if isinstance(v, (int, float)) and v >= cutoff}
        state["reported"] = reported
        tmp = self.state_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError as exc:
            log.debug("对账状态写不进去：%s", exc)

    # ---------- 读桥侧 ----------

    def _load_forwarded(self, chat_kw: str, since: float) -> list[tuple[float, str]]:
        """桥侧已入库消息 ``[(时间戳, 去换行正文)]``。

        用只读连接直连 bridge.db：Storage 是长连接 + WAL，开第二把只读锁不会
        影响写入；反过来走 Storage 会把对账拖进主流程的锁里。
        """
        if not self.bridge_db.is_file():
            return []
        try:
            conn = sqlite3.connect(f"file:{self.bridge_db}?mode=ro", uri=True, timeout=5)
        except sqlite3.DatabaseError:
            return []
        try:
            rows = conn.execute(
                "select msg_time, content from messages where chat_name like ?",
                (f"%{chat_kw}%" if chat_kw else "%",)).fetchall()
        except sqlite3.DatabaseError:
            return []
        finally:
            conn.close()

        out: list[tuple[float, str]] = []
        for stamp, content in rows:
            try:
                ts = time.mktime(time.strptime(str(stamp)[:19], "%Y-%m-%d %H:%M:%S"))
            except (TypeError, ValueError):
                continue
            if ts >= since:
                out.append((ts, (content or "").replace("\n", " ")))
        return out

    # ---------- 主流程 ----------

    def _resolve_disabled(self) -> str:
        """不可用就返回原因（只在第一次真正付出代价，之后走缓存）。"""
        if self._disabled_reason:
            return self._disabled_reason
        ok, why = dingtalk_db.available()
        if not ok:
            self._disabled_reason = why
        return self._disabled_reason

    def run_once(self, force: bool = False) -> Result:
        """跑一轮对账。``force=True`` 忽略「距上次太近」的节流。"""
        with self._lock:
            if self._disabled_reason:
                return Result(ok=False, reason=self._disabled_reason)
            why = self._resolve_disabled()
            if why:
                return Result(ok=False, reason=why)
            if not force and time.time() - self._last_run < MIN_GAP_BETWEEN_RUNS:
                return self.last_result or Result(ok=False, reason="距上次对账太近，跳过")
            return self._run_locked()

    def _run_locked(self) -> Result:
        started = time.time()
        state = self._load_state()
        tolerance = self._num("tolerance_min", DEFAULT_TOLERANCE_MIN, 1.0) * 60
        lookback = self._num("lookback_min", DEFAULT_LOOKBACK_MIN, 5.0) * 60
        max_gap = self._num("max_gap_hours", DEFAULT_MAX_GAP_H, 1.0) * 3600

        # 窗口：常规看最近 lookback 分钟；但停机/挂起之后要能一次补齐，
        # 所以取「上次跑到哪」继续，上限 max_gap（默认 12 小时）。
        now = time.time()
        since = now - lookback
        last_run = float(state.get("last_run") or 0.0)
        if 0 < last_run < now:
            since = min(since, last_run - 300)      # 与上轮重叠 5 分钟，防边界漏
        since = max(since, now - max_gap)

        dt_kw, br_kw = self._chat_keywords()
        if not dt_kw:
            return self._finish(Result(ok=False, reason="没配置要核对的群"
                                       "（reconcile.dingtalk_chat 或 dingtalk.chat_names）"),
                                state, started)

        data_dir = Path((getattr(self.rc, "dingtalk_path", "") or "").strip()) \
            if (getattr(self.rc, "dingtalk_path", "") or "").strip() \
            else dingtalk_db.find_data_dir()
        if not data_dir or not (data_dir / "DBFiles" / "dingtalk.db").is_file():
            return self._finish(Result(ok=False, reason="没找到钉钉数据目录"), state, started)

        try:
            info = dingtalk_db.sync(data_dir, self.work_dir)
        except dingtalk_db.DingTalkDbError as exc:
            self._disabled_reason = str(exc)
            return self._finish(Result(ok=False, reason=f"解密失败：{exc}"), state, started)
        except Exception as exc:                     # noqa: BLE001
            return self._finish(Result(ok=False, reason=f"解密异常：{exc}"), state, started)

        try:
            conn = sqlite3.connect(f"file:{info['db']}?mode=ro", uri=True, timeout=5)
        except sqlite3.DatabaseError as exc:
            return self._finish(Result(ok=False, reason=f"打开解密库失败：{exc}"),
                                state, started)
        try:
            hit = dingtalk_db.find_cid(conn, dt_kw)
            if not hit:
                msg = (f"当前 PC 钉钉账号的库里没有「{dt_kw}」这个群"
                       "（手机采集用的可能是另一个账号）")
                self._warn_once("nochat", msg)
                return self._finish(Result(ok=False, reason=msg), state, started)
            cid, title = hit
            cards = dingtalk_db.load_cards(conn, cid, int(since * 1000))
            db_latest = dingtalk_db.latest_ts(conn, cid) / 1000.0
        finally:
            conn.close()

        forwarded = self._load_forwarded(br_kw, since - tolerance)

        # ---- 配对：标题优先，时间兜底 ----
        used = [False] * len(forwarded)
        missing: list[Finding] = []
        for ts_ms, card in cards:
            sec = ts_ms / 1000.0
            needle = _norm(card)
            hit_idx = None
            if len(needle) >= 5:
                for i, (_fts, content) in enumerate(forwarded):
                    if not used[i] and needle in _norm(content):
                        hit_idx = i
                        break
            if hit_idx is None:
                near = [(abs(f[0] - sec), i) for i, f in enumerate(forwarded)
                        if not used[i] and abs(f[0] - sec) <= tolerance]
                if near:
                    hit_idx = min(near)[1]
            if hit_idx is None:
                missing.append(Finding(ts=sec, title=card))
            else:
                used[hit_idx] = True

        # ---- 去重：同一张卡片只报一次 ----
        reported = state.setdefault("reported", {})
        fresh = [f for f in missing if f"{cid}:{int(f.ts) * 1000}" not in reported]

        result = Result(
            ok=True, chat_title=title, cid=cid, cards=len(cards),
            forwarded=len(forwarded), missing=fresh, db_latest=db_latest,
            window_min=(now - since) / 60, elapsed=time.time() - started)

        self._report(result, len(missing))
        for f in missing:
            reported[f"{cid}:{int(f.ts) * 1000}"] = time.time()
        return self._finish(result, state, started)

    def _finish(self, result: Result, state: dict, started: float) -> Result:
        result.elapsed = time.time() - started
        if result.ok:
            state["last_run"] = time.time()
            self._last_run = time.time()
        self._save_state(state)
        self.last_result = result
        return result

    def _report(self, result: Result, total_missing: int) -> None:
        n = len(result.missing)
        if not n:
            if result.cards == 0:
                # 窗口内一张卡片都没有：可能群里真的没人说话，也可能 PC 钉钉
                # 早就没在收消息了（登出 / 断网 / 换了账号）。这两种在数据上
                # 长得一模一样，所以只能说「没得判」，不能说「没漏」——
                # 2026-11-05 实测：库里 20:56 之后就停在原点，窗口里 0 条，
                # 报告却写着「对账通过」，等于给了个假的安心。
                log.info("对账无可判数据：窗口 %.0f 分钟内钉钉侧 0 条卡片"
                         "（库内最新 %s）—— 若库里迟迟不更新，说明电脑端钉钉"
                         "没在收消息，对账此刻不代表没有漏转",
                         result.window_min,
                         _fmt(result.db_latest) if result.db_latest else "未知")
                return
            log.info("对账通过：钉钉 %d 条 / 桥 %d 条（近 %.0f 分钟），没有新漏转",
                     result.cards, result.forwarded, result.window_min)
            return

        head = (f"⚠ 疑似漏转 {n} 条（钉钉「{result.chat_title or result.cid}」↔ 微信，"
                f"近 {result.window_min:.0f} 分钟）")
        body = "\n".join(result.lines())
        log.warning("%s\n%s", head, body)

        if not (self.notifier and getattr(self.rc, "alert", True)):
            return
        more = ""
        if total_missing > n:
            more = f"\n（另有 {total_missing - n} 条之前已报过，未重复列出）"
        self.notifier.alert(
            "reconcile",
            f"{head}\n{body}{more}\n\n"
            f"本轮核对：钉钉 {result.cards} 条 / 桥已入库 {result.forwarded} 条\n"
            f"常见原因：采集停摆、卡片被滑走、附件抓取失败、超龄闸门拦下。\n"
            f"要补发：python tools/forward_recent.py -n {max(1, n)} --ask\n"
            f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}")

    def _warn_once(self, key: str, text: str) -> None:
        if key in self._warned:
            log.debug("%s", text)
            return
        self._warned.add(key)
        log.warning("%s", text)

    # ---------- 线程 ----------

    def start(self) -> bool:
        """起巡检线程。返回是否真的起来了。"""
        if not self.enabled:
            log.info("漏转对账未启用（reconcile.enabled = false）")
            return False
        if self._thread and self._thread.is_alive():
            return True
        interval = self._num("interval_min", DEFAULT_INTERVAL_MIN, 1.0) * 60
        # 启动前先探一次能力：缺依赖/没装钉钉就别白起线程，日志里说清为什么
        why = self._resolve_disabled()
        if why:
            log.info("漏转对账已跳过：%s", why)
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, args=(interval,),
                                        name="ddwx-reconcile", daemon=True)
        self._thread.start()
        dt_kw, br_kw = self._chat_keywords()
        log.info("漏转对账已启用：每 %.0f 分钟核对「%s」，窗口 %g 分钟",
                 interval / 60, dt_kw or "(未配置)",
                 self._num("lookback_min", DEFAULT_LOOKBACK_MIN, 5.0))
        return True

    def _loop(self, interval: float) -> None:
        if self._stop.wait(FIRST_DELAY_S):
            return
        while True:
            try:
                res = self.run_once(force=True)
                if not res.ok:
                    log.debug("对账跳过：%s", res.reason)
            except Exception as exc:                  # noqa: BLE001
                log.warning("对账异常（不影响转发）：%s", exc)
            if self._stop.wait(interval):
                return

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)


# ---------------------------------------------------------------- 命令行

def describe(result: Result) -> str:
    """给 `run.py --reconcile` 的人读输出。"""
    if not result.ok:
        return f"对账未执行：{result.reason}"
    out = [
        f"钉钉群    : {result.chat_title}（cid={result.cid}）",
        f"窗口      : 近 {result.window_min:.0f} 分钟",
        f"钉钉卡片  : {result.cards} 条（库内最新 {_fmt(result.db_latest) if result.db_latest else '-'}）",
        f"桥已入库  : {result.forwarded} 条",
        f"耗时      : {result.elapsed:.1f} 秒",
    ]
    if not result.missing:
        if result.cards == 0:
            out.append("结论      : ⚠ 本轮无数据可判 —— 窗口内钉钉侧一条卡片都没有")
            if result.db_latest:
                out.append(f"            库内最新消息在 {_fmt(result.db_latest)}，"
                           "若这个时间很久没动，说明电脑端钉钉没在收消息"
                           "（登出 / 断网 / 与手机不是同一账号）")
        else:
            out.append("结论      : ✅ 没有新的疑似漏转")
        return "\n".join(out)
    out.append(f"结论      : ⚠ 新发现疑似漏转 {len(result.missing)} 条")
    out += result.lines(limit=20)
    return "\n".join(out)


def run_cli(cfg, db_path: str | Path, notifier=None) -> int:
    """手动跑一次对账（run.py --reconcile）。返回进程退出码。"""
    rec = Reconciler(cfg, db_path, notifier=notifier)
    if not rec.enabled:
        print("漏转对账未启用。到「设置中心 → 钉钉通知」勾上「漏转对账」，"
              "或在 config.yaml 里设 reconcile.enabled: true")
        return 1
    result = rec.run_once(force=True)
    print(describe(result))
    return 0 if result.ok else 1


def env_hint() -> str:
    """启动自检里展示的一行状态（不触发解密，只探能力）。"""
    ok, why = dingtalk_db.available()
    if not ok:
        return f"不可用（{why}）"
    d = dingtalk_db.find_data_dir()
    return f"可用（钉钉库：{d}）" if d else "不可用（没找到钉钉数据目录）"


__all__ = ["Reconciler", "Result", "Finding", "describe", "run_cli",
           "env_hint", "STATE_NAME"]
