"""服务编排：采集 → 去重落库 → 规则路由 → 格式化 → 限流 → 投递 → 记录。

另外负责「出事要有人知道」：投递连续失败、采集停滞、定时运行报告，
都通过 bridge/notify.py 的钉钉机器人发出去。
"""
from __future__ import annotations

import logging
import queue
import random
import re
import threading
import time
from collections import Counter
from datetime import datetime

from .config import Config
from .formatter import DEFAULT_TEMPLATE, render
from .logfun import tail
from .models import Message
from .notify import (DingTalkNotifier, SYSTEM_PREFIX, in_hhmm_window,
                     install_alert_handler)
from .router import Router
from .storage import Storage
from .table_image import render_message
from .targets import build_target
from .targets.base import Target
from .xlsx_reader import read_rows

log = logging.getLogger("bridge.service")

WATCHDOG_TICK = 30.0          # 看门狗巡检间隔（秒）
RETRY_DELAY_S = 30.0          # 投递失败后的补投间隔（秒）
RETRY_NOTE = "失败补投"        # 补投投递记录里的标记

# 积压超龄不转发（用户 2026-11-01 定：「超过 30 分钟的积压不能再转发了」）。
#
# 手机断线/卡住一段时间再恢复时，采集端会把**当前屏上能看见的历史消息**
# 一起补上来 —— 这本来是好事（不丢消息），但一口气十几条旧消息涌进微信群
# 就是刷屏：群里看到的是「18:34 的晚报」在 19:20 才冒出来，还挤掉了真正
# 当下的消息。所以按**源消息时间**而不是接收时间判：超过这个分钟数只入库
# 留档、不投递，要补发用 forward_recent.py（那里走 force，不受此限）。
#
# 注意判据取自 msg_time（手机读消息头顶的时间条）。读不出时 _parse_time
# 会退回本机时间，age≈0 —— 宁可多发一条，也不因为时间条格式变化把消息吃掉。
MAX_BACKLOG_MIN = 30.0

# 文件名整理用到的规则（2026-11-01 用户反馈「转发的原文件名太乱」）。
#
# 乱在两处：
#   1. 落盘名带 12 位 sha 前缀（`6e16038a834e-查看完整月报明细（Excel）`），
#      微信里显示的就是它 —— 一串哈希没人看得懂；
#   2. 手机端上报的名字常带格式后缀（`...（Excel）`），既不是真扩展名，
#      又占位置，微信里点开还是一堆括号。
_BAD_NAME_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')
_FORMAT_TAG_RE = re.compile(
    r"[（(]\s*(excel|xlsx?|xlsm|word|docx?|pdf|csv|zip|rar|7z|txt|pptx?|图片)"
    r"\s*[）)]\s*$", re.I)
# 格式词 → 真扩展名（只在原文件没有扩展名、或名字里的格式词与扩展名冲突时用）
_EXT_BY_TAG = {"excel": ".xlsx", "xls": ".xls", "xlsx": ".xlsx", "xlsm": ".xlsm",
               "word": ".docx", "doc": ".doc", "docx": ".docx", "pdf": ".pdf",
               "csv": ".csv", "zip": ".zip", "rar": ".rar", "7z": ".7z",
               "txt": ".txt", "ppt": ".ppt", "pptx": ".pptx", "图片": ".png"}
# 手机端只会给这种「通用按钮名」，单靠它区分不出是哪个文件 —— 补日期时间，
# 免得一天抓两份都叫「下载附件.xlsx」互相覆盖、也分不清谁是谁。
_GENERIC_NAMES = {"附件", "下载附件", "下载", "明细", "明细附件", "表格", "文件",
                  "查看附件", "点击下载", "下载文件"}
_NAME_MAX = 80                # 微信文件名显示上限之外的自我约束（含扩展名）
# ⚠ 别调太小：手机端抓下来的英文名形如
#   「2026-11-01_311392_intercept_risk_center_not_arrived_1793491732445.xlsx」
#   （74 字符）。60 的阈值会把它截成「…_179.xlsx」—— 比原样更难认。

# ── 用「卡片上的名字」当文件名（2026-11-01 用户要求）──────────────────
# 手机回传的文件名是钉钉服务端生成的英文串：
#   2026-11-01_311392_intercept_risk_center_not_arrived_1793513190555.xlsx
# 而群里那条消息明明写着「明细附件：拦截风险明细.xlsx」—— 用户要的是后者。
# 上游网关把「这次抓的是哪一条附件行」记在 attach_active 里传下来，这里负责
# 把它洗成一个能当文件名的标题。
_EXT_TAIL_RE = re.compile(
    r"\.(xlsx?|xlsm|csv|pdf|docx?|pptx?|zip|rar|7z|txt|png|jpe?g)$", re.I)
# 按钮文案的动作前缀：「下载3天滞留运单明细」→「3天滞留运单明细」
_LEAD_ACTION_RE = re.compile(
    r"^(?:请)?(?:立即|直接|马上)?(?:下载|查看完整|查看|点击查看|点击|打开|导出|"
    r"获取|保存|领取|查收)+")


def _card_title(prefer: str) -> str:
    """消息里那条附件行 → 可当文件名的标题（拿不到就返回空串）。

    「明细附件：拦截风险明细.xlsx」→「拦截风险明细」
    「下载附件」→``""``（纯按钮文案，拿来当文件名比英文串更难认）
    """
    s = (prefer or "").strip().strip(" :：·—|｜")
    if not s:
        return ""
    # 「明细附件：xxx」这类标签，冒号后才是真名字
    if ":" in s or "：" in s:
        tail = re.split(r"[:：]", s)[-1].strip()
        if tail:
            s = tail
    s = _LEAD_ACTION_RE.sub("", s).strip(" :：·—|｜")
    # 「查看完整周报明细（Excel）」→ 格式后缀不是文件名的一部分
    s = _FORMAT_TAG_RE.sub("", s).strip(" :：·—|｜")
    s = _EXT_TAIL_RE.sub("", s).strip(" :：·—|｜")
    if len(s) < 2:
        return ""
    if s.lower() in {g.lower() for g in _GENERIC_NAMES}:
        return ""
    return s


def pretty_file_name(name: str, path: str, prefer: str = "") -> str:
    """把「落盘名 + 上报名」整理成微信里好看的显示名。

    `path` 是磁盘上的真实文件（可能带 sha 前缀），`name` 是手机端上报的
    原始名，`prefer` 是**消息里那条附件行的名字**（有就优先用它，见
    :func:`_card_title`）。返回值只用于**显示与发送**，磁盘文件名不动 ——
    去重靠 sha，落盘名带前缀是为了「同名不同内容」不互相覆盖。
    """
    from pathlib import Path as _P

    disk = _P(path or "")
    ext = disk.suffix.lower()

    # 1) 卡片上写着的名字最像人话，优先
    card = _card_title(prefer)
    if card:
        m = _EXT_TAIL_RE.search(card)
        card_ext = ("." + m.group(1).lower()) if m else ""
        # 扩展名以**真实文件**为准：卡片写 .xlsx、实际下来的是 .csv 时别撒谎
        use_ext = ext or card_ext or ".xlsx"
        stem = (_EXT_TAIL_RE.sub("", card) if m else card).strip(" ._")
        stem = _BAD_NAME_CHARS.sub("_", stem).strip(" ._")
        room = max(8, _NAME_MAX - len(use_ext))
        if len(stem) > room:
            stem = stem[:room].rstrip(" ._-")
        if stem:
            return f"{stem}{use_ext}"

    raw = (name or "").strip() or disk.name

    # 手机端上报的名字可能本身就是带前缀的落盘名，先剥前缀
    raw = re.sub(r"^[0-9a-f]{12}-", "", raw, flags=re.I)

    stem = raw
    tag_ext = ""
    m = _FORMAT_TAG_RE.search(stem)
    if m:
        tag_ext = _EXT_BY_TAG.get(m.group(1).lower(), "")
        stem = _FORMAT_TAG_RE.sub("", stem).strip()
    # 「查看完整月报明细.xlsx（Excel）」这种中缀也没意义，统一处理
    stem = re.sub(r"\.(xlsx?|xlsm|docx?|pdf|csv|zip|rar|7z|txt|pptx?)", "",
                  stem, flags=re.I).strip()

    stem = _BAD_NAME_CHARS.sub("_", stem).strip(" ._")
    if not stem or stem.lower() in {g.lower() for g in _GENERIC_NAMES}:
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        stem = f"{stem or '附件'}-{stamp}"

    use_ext = ext or tag_ext or ".xlsx"
    if not use_ext.startswith("."):
        use_ext = "." + use_ext
    # 留出扩展名与 1 个字符余地，长名字截断但不切碎扩展名
    room = max(8, _NAME_MAX - len(use_ext))
    if len(stem) > room:
        stem = stem[:room].rstrip(" ._-")
    return f"{stem}{use_ext}"


def stage_media_file(path: str, display_name: str) -> str:
    """把附件复制成一份**文件名干净**的副本，返回副本路径。

    为什么要复制而不是改名：原文件是「sha 前缀 + 原名」的落盘格式，
    去重、断点续传、人工翻找都靠它；直接把名字改掉，下次同一个文件传上来
    会被当成新文件（路径变了）再发一遍。副本放在 `data/outbox/`，同名覆盖 ——
    outbox 只是发送用的暂存区，不需要留历史。
    """
    import shutil
    from pathlib import Path as _P

    src = _P(path)
    outbox = src.parent.parent / "outbox"
    try:
        outbox.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("建 outbox 目录失败（改用原名发送）：%s", exc)
        return str(src)
    dst = outbox / display_name
    try:
        if dst.exists():
            dst.unlink()
        shutil.copy2(src, dst)
        return str(dst)
    except OSError as exc:
        log.warning("复制附件到 outbox 失败（改用原名发送）：%s", exc)
        return str(src)



class BridgeService:
    def __init__(self, cfg: Config, target: Target | None = None):
        self.cfg = cfg
        self.storage = Storage(cfg.resolve(cfg.storage.db_path))
        self.router = Router(cfg.routes)
        self.target = target or build_target(cfg)
        self.counter: Counter = Counter()
        self.last_target_check: tuple[bool, str] = (True, "尚未检查")
        self._last_send = 0.0
        self._send_lock = threading.RLock()     # 补投线程与投递线程共用，须可重入
        self._send_count = 0                    # 已发送条数，用于插入随机长停顿

        # ---- 投递失败补投（2026-10-27：遮挡这种瞬态失败不该永久漏消息）----
        self.retry_delay = RETRY_DELAY_S
        self._retry_stop = threading.Event()
        self._retry_queue: queue.Queue = queue.Queue()
        self._retry_thread = threading.Thread(
            target=self._retry_worker, daemon=True, name="ddwx-retry")
        self._retry_thread.start()

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

    def handle(self, msg: Message, force: bool = False) -> bool:
        """处理一条消息。返回 True 表示确认为新消息（无论是否真的转发成功）。

        ``force=True`` 跳过去重检查，给「手动转发最近 N 条」这类**人工触发**用：
        用户既然明确点了发送，意思就是「这几条再发一次」，不该被去重挡成
        「什么都没发生」。入库仍是 INSERT OR IGNORE，投递记录照记，
        所以补发过的事实会留在库里，不会被下一次自动采集重复统计。

        附件的判重比文本严一档：必须**有成功的投递记录**才算重复（见
        ``Storage.is_duplicate`` 的说明）。文本消息入库即算处理完（可能命中
        规则被过滤，也可能发了），附件的存在意义就是「发出去」——入库却没
        发成功的那条，重传时必须能再发一次。
        """
        if not force and self.storage.is_duplicate(
                msg.uid, need_delivered=(msg.msg_type == "file")):
            self.counter["duplicate"] += 1
            log.debug("重复消息，跳过：%s", msg.uid)
            return False

        self.storage.save_message(msg)
        self.counter["received"] += 1
        # 有新消息 = 采集端活着：解除停滞状态
        self._last_activity = time.time()
        self._last_msg_desc = f"{msg.sender_name}：{(msg.content or '')[:40]}"
        self._idle_alerted = False

        # 程序自己发的运维通知（启动/停止/报告/告警）长在业务群里，采集端只看
        # 文本不看发送者 —— 不拦住就会把「转发桥已启动」「手机端失联」原样转给
        # 微信业务群。仍然入库留痕，只是不投递；force（手动补发）同样拦，因为
        # 人工补发的意图是「补那几条业务消息」，不包括运维文案。
        if (msg.content or "").lstrip().startswith(SYSTEM_PREFIX):
            self.counter["filtered"] += 1
            log.info("自身通知不转发（%s）：%s", msg.sender_name,
                     (msg.content or "").replace("\n", " ")[:40])
            return True

        # 只入库不投递（`no_forward`）。目前唯一的来源是「建基线时补全出来的
        # 长消息」：正文补齐了、值得留档，但它在基线里 = 多半几十分钟前就发过，
        # 自动发出去就是往业务群刷历史。留档即可，要补发用 forward_recent.py。
        if msg.no_forward:
            self.counter["filtered"] += 1
            log.info("只入库不转发（基线补全的历史消息，%d 字）：%s",
                     len(msg.content or ""),
                     (msg.content or "").replace("\n", " ")[:40])
            return True

        # 积压超龄不转发：手机卡住一段时间后恢复，屏上能看见的历史会被一起
        # 补上来，一口气全发出去等于往业务群刷旧闻（用户 2026-11-01 要求）。
        # force（人工补发）不受限 —— 那时候"发旧消息"正是用户要的。
        # 阈值可在「设置中心 → 时间范围」里改（wechat.max_backlog_min），
        # 0 = 关闭这条闸门。
        limit = self.backlog_limit()
        age_min = self._backlog_minutes(msg)
        if not force and limit > 0 and age_min > limit:
            self.counter["filtered"] += 1
            if msg.msg_type == "file":
                # 附件走这条路时最容易被当成「漏了」：手机端明明抓到了、
                # 文件也落盘了，只是抓取本身耗掉的时间把源消息推过了 30 分钟
                # 线（2026-11-04 20:43 实锤：19:32 的卡片、重试到 20:43 才
                # 拿到，71 分钟 > 30 → 只入库不转发，群里什么都没有）。
                # 这不是 bug 而是闸门在按规则办事，但**必须吵**：用户看到的是
                # 「附件没发出去」，日志里却只有一条 info，根本对不上。
                log.warning("附件已抓到但源消息超龄，只入库不转发"
                            "（源消息 %s，已 %.0f 分钟，上限 %.0f）：%s"
                            " —— 要补发用 tools/forward_recent.py",
                            msg.time_text or "时间未知", age_min, limit,
                            (msg.content or "").replace("\n", " ")[:40])
            else:
                log.info("积压超龄不转发（源消息 %s，已 %.0f 分钟，上限 %.0f）：%s",
                         msg.time_text or "时间未知", age_min, limit,
                         (msg.content or "").replace("\n", " ")[:40])
            return True

        matched = self.router.match(msg)
        if not matched.targets:
            self.counter["filtered"] += 1
            log.info("未命中转发规则，仅入库：%s | %s", msg.company, msg.content[:30])
            return True

        # 附件临时改投「文件传输助手」（用户 2026-11-01 20:26：今天补发的
        # 发文件助手，不是以后）。只换目标、不改规则：能不能发仍由路由决定，
        # 正文照旧进业务群。两个配置留空 = 行为完全不变。
        # ⚠ 带截止时间：过了 attach_target_until 自动失效，免得哪天忘了关，
        #   附件从此静默全进文件传输助手。
        targets = list(matched.targets)
        attach_tgt = (getattr(self.cfg.wechat, "attach_target", "") or "").strip()
        if attach_tgt and msg.msg_type == "file" and attach_tgt not in targets \
                and self._attach_redirect_on():
            log.info("附件改投「%s」（原目标 %s）：%s", attach_tgt,
                     ",".join(targets) or "-", (msg.content or "")[:30])
            targets = [attach_tgt]

        text = render(msg, matched.template or DEFAULT_TEMPLATE)
        for tgt in targets:
            ok = self._deliver_to_target(msg, tgt, text)
            if not ok:
                # 瞬态失败（窗口被挡、幽灵窗、一次网络抖动）占绝大多数，
                # 失败一次就放弃等于永久漏消息 —— uid 已入库，后续帧会被
                # 去重跳过，再没有自然重来的机会。延后 30 秒补投一次。
                self._retry_queue.put((msg, tgt, text))
        return True

    def _deliver_to_target(self, msg: Message, tgt: str, text: str,
                           note: str = "") -> bool:
        """把一条消息投给一个目标：文字 + 可选表格图。返回文字是否发出。

        全程持 ``_send_lock``：补投线程与正常投递并发时，两个流程的点击/
        OCR 绝不能交错（投递端不是线程安全的）。
        """
        prefix = f"[{note}] " if note else ""
        with self._send_lock:
            self._throttle()
            result = self.target.send_text(tgt, text)
            self.storage.log_delivery(
                msg.uid, tgt, "ok" if result.ok else "fail",
                f"{prefix}{result.detail}" if prefix else result.detail
            )
            if result.ok:
                self.counter["sent"] += 1
                self._consec_fail = 0
                log.info("%s已投递 → %s%s", prefix, tgt, tail("sent"))
            else:
                self.counter["failed"] += 1
                self._consec_fail += 1
                log.error("%s投递失败 → %s | %s", prefix, tgt, result.detail)
                self._alert_delivery_fail(tgt, result.detail)

            # ---- 附件：发**原文件** ----
            # 用户 2026-10-31 定：附件（周报/月报/运单明细这类 xlsx）一律发原
            # 文件，不要再渲染成长图。理由有两条，都是用户当场提的：
            #   ① 宽表切块时会丢最左边的网点名称，长图还长得没法在微信里看；
            #   ② 明细表要能筛选、能另存，图片一条都做不到。
            # 文字失败时**不再发文件**：会话都打不开，文件更发不出去。
            if result.ok and msg.msg_type == "file":
                media = (msg.media_path or "").strip()
                from pathlib import Path as _P

                if not media or not _P(media).is_file():
                    log.warning("附件原文件已不在，只发了提示文字：%s",
                                media or "（未记录路径）")
                    return result.ok
                self._throttle()
                try:
                    r2 = self.target.send_file(tgt, str(media))
                except AttributeError:
                    log.warning("投递端不支持发文件，附件未送达：%s", tgt)
                except Exception:                   # noqa: BLE001
                    log.exception("附件投递异常（文字已发）")
                else:
                    self.storage.log_delivery(
                        msg.uid, tgt, "ok" if r2.ok else "fail",
                        f"附件: {r2.detail}")
                    if r2.ok:
                        log.info("%s附件已投递 → %s（%s）%s", prefix, tgt,
                                 _P(media).name, tail("sent"))
                    else:
                        log.warning("%s附件投递失败 → %s | %s", prefix, tgt,
                                    r2.detail)
                return result.ok

            # ---- 表格转图片（文字照发，表格补一张图）----
            # 用户 2026-10-18 选定：钉钉卡片表格在微信里没有原生类型，转成
            # PNG 补发。文字失败时**不补图**（会话都打不开，图更发不出）。
            # 图片走 send_image —— 目标不支持（旧实现/其它平台）就跳过，
            # 不算失败：文字已经发到了，表格图片是锦上添花。
            if result.ok and self.cfg.wechat.table_image:
                # 走到这里的只有**文本消息**（附件在原文件分支就 return 了），
                # 表格图一律从正文现渲染。
                png = (msg.media_path or "").strip()
                if not png:
                    png = render_message(text, out_dir=self._table_png_dir(),
                                         key=msg.uid, title=msg.sender_name)
                if png:
                    # media_path 是「渲染时留的路径」，文件可能已被清理 ——
                    # 没了就只发文字，别让一张图把已成功的文字投递报成异常。
                    from pathlib import Path as _P

                    if not _P(png).is_file():
                        log.debug("图片文件已不在，跳过补图：%s", png)
                        return True
                    self._throttle()
                    try:
                        r2 = self.target.send_image(tgt, str(png))
                    except AttributeError:
                        log.debug("投递端不支持发图片，跳过表格图：%s", tgt)
                    except Exception:               # noqa: BLE001
                        log.exception("表格图片投递异常（不影响文字）")
                    else:
                        self.storage.log_delivery(
                            msg.uid, tgt, "ok" if r2.ok else "fail",
                            f"表格图: {r2.detail}")
                        if r2.ok:
                            log.info("表格图片已投递 → %s（%s）%s", tgt, _P(png).name,
                                 tail("table_png"))
                        else:
                            log.warning("表格图片投递失败 → %s | %s",
                                        tgt, r2.detail)
            return result.ok

    # ---------- 投递失败补投 ----------

    def _retry_worker(self) -> None:
        """补投线程：失败的投递 30 秒后原样再发一次，只补一次。

        为什么只一次：遮挡/幽灵窗这类瞬态故障，隔半分钟重试几乎必过；
        真故障（会话没了、规则变了）重试也是同样结果，无限重试只会
        往业务群反复重发同样的东西 —— 那比漏一条更糟（2026-10-20 铁律）。
        """
        while True:
            item = self._retry_queue.get()
            if item is None:
                return
            msg, tgt, text = item
            if self._retry_stop.wait(self.retry_delay):
                continue                    # 服务已停：丢弃，不再投
            log.info("投递失败 %.0f 秒后补投 → %s（%s）",
                     self.retry_delay, tgt, msg.sender_name)
            try:
                if self._deliver_to_target(msg, tgt, text, note=RETRY_NOTE):
                    log.info("补投成功 → %s", tgt)
            except Exception:               # noqa: BLE001
                log.exception("补投异常（放弃）：%s", msg.uid)

    # ---------- 附件转发 ----------

    @staticmethod
    def _backlog_minutes(msg: Message) -> float:
        """源消息距今多少分钟。

        ``msg_time`` 缺失、或比本机时间还晚（手机时钟快了）一律返回 0，
        也就是**不拦** —— 这条规则的用途是挡"陈年积压"，不是做时间校验，
        宁可用错放的阈值漏一条，也不能因为时钟问题把当下的消息全吃掉。
        """
        if not msg.msg_time:
            return 0.0
        try:
            delta = (datetime.now() - msg.msg_time).total_seconds() / 60.0
        except (TypeError, ValueError):
            return 0.0
        return delta if delta > 0 else 0.0

    def backlog_limit(self) -> float:
        """「积压超龄不转发」的阈值（分钟）；0 或负数 = 关闭这条闸门。

        取自 ``wechat.max_backlog_min``（设置中心 → 时间范围可改）。配置缺失、
        写错、写成 ``None`` 一律回落到 :data:`MAX_BACKLOG_MIN` —— 闸门是保护
        业务群不被历史刷屏的，不能因为一个配置项没写完就静默失效。
        """
        raw = getattr(self.cfg.wechat, "max_backlog_min", MAX_BACKLOG_MIN)
        try:
            val = float(raw)
        except (TypeError, ValueError):
            log.warning("max_backlog_min=%r 不是数字，按默认 %.0f 分钟处理",
                        raw, MAX_BACKLOG_MIN)
            return MAX_BACKLOG_MIN
        return max(0.0, val)

    def _attach_redirect_on(self) -> bool:
        """附件改投文件助手现在生效吗（看 ``attach_target_until``）。

        ``until`` 留空 = 没有截止时间（临时大扫除时可以这么用，但要清楚
        它会一直生效，直到有人把配置清掉）。
        时间串解析不了也**判失效**：配置写错了宁可按老路发业务群，
        不能让附件因为一个错字全部改道。
        """
        until = (getattr(self.cfg.wechat, "attach_target_until", "") or "").strip()
        if not until:
            return True
        try:
            deadline = datetime.fromisoformat(until)
        except (TypeError, ValueError):
            log.warning("attach_target_until 解析不了（%s），按失效处理", until)
            return False
        if datetime.now() <= deadline:
            return True
        log.info("附件改投已过期（截止 %s），改回业务群", until)
        return False

    def handle_attachment(self, path: str, name: str, chat: str = "",
                          wanted: str = "", msg_time=None) -> bool:
        """收到手机传回的附件文件 → 投递**原文件**（用户 2026-10-31 定）。

        链路的最后一环：网关 ``/file`` 已做过 sha1 去重（同一份文件只会进到
        这里一次），这里的防重复靠 uid（raw_id = ``attach-<sha1>``）入库判重
        —— 它跨重启有效，比内存集合可靠。

        ``wanted`` 是**消息里那条附件行的名字**（网关的 ``attach_active``，
        如「明细附件：拦截风险明细.xlsx」）。文件名优先用它 —— 手机回传的
        英文串（``2026-11-01_311392_intercept_risk_center_not_arrived_…``）
        在微信里没人看得懂（2026-11-01 用户要求）。

        为什么不再渲染表格图：宽表切块会丢左边的网点名称，转成图之后既不能
        筛选也不能另存，明细表（周报 108 行 × 22 列）更是长到没法看。原文件
        发过去，微信里能直接打开/另存，信息一点不丢。

        解析 xlsx **只为了在提示文字里写清行数**，解析失败也照发原文件 ——
        pdf/docx/图片类附件本来就解析不了，因为解析不了就整份丢掉太亏。
        """
        import hashlib

        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as exc:
            log.warning("附件 %s 读不回来（跳过转发）：%s", path, exc)
            return False
        if not data:
            log.warning("附件 %s 是空文件（跳过转发）", name)
            return False
        sha = hashlib.sha1(data).hexdigest()

        # 行数只是提示语的修饰，取不到就不写 —— 绝不能因为解析失败丢附件。
        summary = ""
        try:
            rows = read_rows(path)
            if len(rows) >= 2:
                summary = f"（{len(rows) - 1} 行）"
            else:
                log.info("附件 %s 只有表头没有数据，仍按原文件转发", name)
        except Exception as exc:                    # noqa: BLE001
            log.info("附件 %s 不是可解析的表格（照发原文件）：%s", name, exc)

        # 微信里显示的是**磁盘文件名**，不整理就等于把 sha 前缀和「（Excel）」
        # 一起发过去。这里改成干净副本，原文件保持前缀不动（去重靠它）。
        display = pretty_file_name(name, path, prefer=wanted)
        media = stage_media_file(path, display)

        msg = Message(source="dingtalk", chat_name=chat or "",
                      sender_name="附件",
                      content=f"【附件】{display}{summary}",
                      msg_type="file",
                      # ⚠ 必须转 str：media_path 会被 sqlite 原样绑定，
                      # Path 对象在这里就是 ProgrammingError。
                      media_path=str(media),
                      # 源消息时间：网关按附件行所在消息的时间条换算过来。
                      # 有了它，附件才跟正文一样受「积压超龄只入库不转发」
                      # 约束（2026-11-01 20:4x：正文拦得住、附件这条路是漏的
                      # —— 附件 msg_time 天生为空，年龄永远算 0）。
                      # None = 判断不出（读不出时刻 / 人工补抓），照常转发。
                      msg_time=msg_time,
                      raw_id=f"attach-{sha}")
        return self.handle(msg)

    def _table_png_dir(self) -> str:
        """表格图片的落盘目录：跟数据库同一个目录下的 table-png/。

        放在数据目录而不是临时目录：发送失败时用户还能手动把图翻出来发。
        """
        import tempfile
        from pathlib import Path

        base = Path(self.cfg.resolve(self.cfg.storage.db_path)).parent
        try:
            base.mkdir(parents=True, exist_ok=True)
            return str(base / "table-png")
        except OSError:
            return tempfile.gettempdir()

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
        """相邻两条发送之间保持随机间隔，降低微信风控风险。

        固定间隔（例如永远 3.0 秒一条）本身就很容易被识别为机器人节奏，
        所以这里做两层随机：
        1. **基础间隔**在 [interval*0.8, interval*1.6] 之间抖动 ——
           配置 5.0 时实际落在 4.0~8.0 秒；
        2. **长停顿**：每发 interval_long_every 条插一次
           interval_long_range 里的随机等待（默认 8~20 秒），
           模拟真人「发几条、停一会儿」的行为。

        需要精确复现时把 interval_jitter_long 关掉并把两个边界设成相等即可。
        """
        base = max(0.0, float(self.cfg.wechat.interval))
        with self._send_lock:
            now = time.time()
            self._send_count += 1

            if base <= 0:
                self._last_send = now
                return

            wait = random.uniform(base * 0.8, base * 1.6)

            every = int(getattr(self.cfg.wechat, "interval_long_every", 0) or 0)
            if getattr(self.cfg.wechat, "interval_jitter_long", False) and every > 0 \
                    and self._send_count % every == 0:
                rng = getattr(self.cfg.wechat, "interval_long_range", None) or [8.0, 20.0]
                try:
                    lo, hi = float(rng[0]), float(rng[1])
                except (TypeError, IndexError, ValueError):
                    lo, hi = 8.0, 20.0
                if hi < lo:
                    lo, hi = hi, lo
                wait += random.uniform(lo, hi)
                log.info("随机长停顿：本次等待 %.1f 秒（每 %d 条一次）", wait, every)

            delta = now - self._last_send
            if delta < wait:
                time.sleep(wait - delta)
            self._last_send = time.time()

    # ---------- 通知：生命周期 ----------

    def announce(self, text: str) -> bool:
        """发一条通知（不带冷却去重），用于开始/停止这类一次性事件。"""
        if not self.notifier:
            return False
        ok, _ = self.notifier.send_text(text, system=True)
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
        ok, _ = self.notifier.send_text(self.report_text(period), system=True)
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
        ok, _ = self.notifier.send_text(self.db_report_text(period), system=True)
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
            # 只对「窗口不可用」这类可自愈的问题做修复尝试：
            # 微信被最小化 / 收进托盘时，启动瞬间就该把它唤回来，
            # 而不是丢一句报错让用户自己找原因。
            ensure = getattr(self.target, "ensure_visible", None)
            if callable(ensure):
                fixed, why = ensure()
                if fixed:
                    log.info("微信窗口已自动恢复：%s", why)
                    ok, detail = self.target.health_check()
            # 仍然是坏的，再试一次「切到第一个目标群」：微信只有打开一个真实
            # 会话才会创建输入框，停在公众号/服务号会话上会被误判成
            # 「UIA 暴露被关掉了」（2026-10-17 实测「海康威视客户服务」）。
            # 反正真正发送前也要切过去，这里先切一次不影响任何东西。
            if not ok:
                opener = getattr(self.target, "open_chat", None)
                first = self.first_target()
                if callable(opener) and first:
                    log.info("自检未通过，尝试切到「%s」后复查", first)
                    try:
                        if opener(first):
                            ok, detail = self.target.health_check()
                    except Exception as exc:
                        log.debug("切换「%s」失败：%s", first, exc)
        self.last_target_check = (ok, detail)
        if not ok:
            problems.append(f"微信投递端不可用：{detail}")
        return problems

    def first_target(self) -> str:
        """配置里的第一个转发目标（自检复查、日志提示用）。"""
        for t in self.cfg.wechat.targets:
            if t:
                return t
        for r in self.cfg.routes:
            for t in r.targets:
                if t:
                    return t
        return ""

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
        # 补投线程：置停 + 唤醒队列，别让 close 卡在 30 秒延迟上
        self._retry_stop.set()
        self._retry_queue.put(None)
        if self._retry_thread.is_alive():
            self._retry_thread.join(timeout=1.0)
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
