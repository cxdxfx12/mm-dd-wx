"""安卓采集端网关（mode: android）。

手机上的小 App 用无障碍服务读钉钉群消息，把结果 POST 到这里；本模块只做
「收 → 转成 Message → 交给下游」，不掺任何业务判断（去重、路由、格式化、
限流、投递、告警全在下游，一行都不用改）。

为什么这么切
    `bridge/sources/base.py` 的 Source 抽象本来就为「换一种取数方式」而设。
    网关接进来以后，`BridgeService.handle()` 之后的一切照旧复用 —— 这也是
    这套代码唯一需要改动的采集侧接入点。

安全（必读）
    网关要监听局域网（手机得连得上），所以 **token 是强制的**：token 为空时
    `check()` 直接不通过、`run()` 拒绝启动，不做「没配就放行」。原因是能往
    这里 POST 就等于能往你的微信群发消息 —— 裸奔等于把转发权限开放给整个
    网段。token 比较用 `hmac.compare_digest`，避免计时侧信道。

幂等
    手机端算好指纹放进 `fp` 字段，`raw_id = fp:seq`，uid 稳定可复现；重传
    （网络抖动、手机没收到响应）会得到同一个 uid，被下游 `is_duplicate` 挡掉。
    手机端没带 `fp` 时本模块用 `bridge.fingerprint` 补算 —— 与 OCR 路径
    **同一份实现**，避免两边算法漂移导致同一条消息被采两次。

时间过滤与基线
    走 ``/messages`` 时这两个判断由手机端完成（它自己知道哪些是新消息），本模块
    不再判 —— 再判一次反而会把「手机离线一夜、恢复后补报」的消息当历史丢掉。

    走 ``/screen`` 时情况相反：手机端只送「这一屏长什么样」，判新老只能在电脑侧做。
    这时由 ``dingtalk.android_parse`` 开关决定是否启用
    :class:`bridge.sources.android_parse.FrameDiffer` —— 它第一帧只建基线（一条
    不发），之后只转发基线（锚点）之后的部分。默认关闭：没看过真机画面的解析
    结果之前不要开，否则可能把认错的文本当真消息发进微信群。

两个端点
    ``POST /messages``
        手机端自己解析好、直接报**消息**。走这条路时手机端承担全部业务判断。

    ``POST /screen``  /  ``GET /screen``
        手机端只把当前界面的**无障碍节点树 XML** 原样送上来（格式与
        ``adb shell uiautomator dump`` 一致）。``dingtalk.android_parse = true``
        时本模块负责解析、判新老，再交给下游；= false 时只存不解析。
        ``GET /screen`` 取最新一份（``?raw=1`` 拿正文，不带就用 JSON 摘要），
        是排障和分析节点结构的入口。

    两条路可以同时存在，互不影响。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import socket
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from ..config import Config
from ..fingerprint import (NEAR_MIN_LEN, content_key, fp_of, is_same_message,
                           norm_sender)
from ..logfun import tail
from ..models import MSG_TYPES, Message
from .android_parse import (FrameDiffer, ParseError, Unit, clipped_units,
                            needs_scroll, parse_frame, unit_age_seconds,
                            units_to_messages)
from .base import MessageHandler, Source, infer_company
from .pending import PendingBuffer

log = logging.getLogger("bridge.source.android")

MAX_BODY = 2 * 1024 * 1024          # 单次请求体上限
# 节点树的体积比消息大得多（一屏几百个节点，中文正文，实测 30~300 KB），
# 所以给 /screen 单独放宽到 4 MB。上限仍然要有：监听局域网意味着任何人都能来灌。
MAX_SCREEN_BODY = 4 * 1024 * 1024
MAX_BATCH = 200                     # 单次请求最多几条消息
DEFAULT_PORT = 8765
# 每收到这么多帧画面打一条 INFO。帧率可能到 0.5/s，全打会把日志淹掉；
# 全不打又会让「手机到底还在不在上报」变成一个查不出来的问题。
SCREEN_LOG_EVERY = 30
# 连续这么多帧都认不出「聊天页」→ 升到 WARNING（会触发钉钉告警）。
# 认不出意味着一条都不转发，属于「程序在跑但静默失效」，必须让人知道。
UNKNOWN_PAGE_ALERT_AFTER = 3
# 连续认不出聊天页达到告警阈值时，自动下发一次「切回目标群」（watch_now）。
# 光告警解决不了「拉回来」：2026-10-31 17:50 附件抓完后手机停在电脑侧认不出
# 的页面，告警响了 5 分钟没人管，最后靠人手动切回。冷却防的是「钉钉真改版、
# 永远认不出」时每 3 帧就抢一次用户前台 —— 10 分钟一次是能接受的骚扰上限。
UNKNOWN_PAGE_AUTO_WATCH_COOLDOWN_S = 600.0
# ── 「电脑侧主动要一帧」（2026-10-27 真机踩到）────────────────────────
# 手机端「画面 md5 没变就不给电脑发」（省流量，设计如此）。于是出现一个死锁：
# **电脑重启 / 刚装好时手上没有基线，而手机画面恰好没变 —— 手机觉得没新东西，
# 一个包都不发，电脑就永远等在那里**。现场表现是「群里明明来消息了就是不转发，
# 我手动翻一下手机才补上」（用户原话）。
# 修法：电脑侧在每个心跳回包里告诉手机「我现在需要一帧」，手机看到新的请求
# 编号就清掉自己的 md5 指纹、立刻重发一次当前画面。
# 编号用「电脑的墙上时间秒」，不用自增计数：电脑重启后计数归零会小于手机记住的
# 旧值，手机就会当成「已经处理过的老请求」而忽略 —— 时间戳天生单调，重启也不会退。
WANT_FRAME_MIN_GAP = 60             # 两次请求的最小间隔（秒），别把心跳变成刷帧机
WANT_FRAME_IDLE_S = 1800            # 半小时一帧都没有：要一帧自证「我还能看见」

# 「同一条卡片换了渲染形态」的近似去重窗口（秒）。
# 实测两种形态的间隔：几分钟内来回切（上翻重绘、卡片重新渲染）。取 10 分钟
# 足以覆盖，又短到不会误伤「人工故意在十分钟后再发一条一样的话」。
# 机器人日报每天格式相同、数字不同，content_key 会保留全部数字，不会撞。
NEAR_DUP_WINDOW = 600.0
# 「附件抓取」指令的冷却（秒）。卡片里的「附件：运单明细」会随卡片重渲染在
# 几十帧里反复出现，不下冷却就是让手机反复跳 UC 下载同一个文件。10 分钟：
# 覆盖一张卡片的可见期；真正的「同一份文件不重发」由收到文件后的 sha1 去重
# 负责（content 变了 → 新文件 → 新 sha1 → 照收）。
ATTACH_COOLDOWN = 600.0
# 附件抓取失败后的退避（秒）：超时说明手机端这一趟没成功（页面被点走、
# 弹窗没人点、下载确认没接），卡片还在屏上就会下一帧立刻重派 —— 只会连成
# 一串 20 秒空等。3 分钟内不自动重派；手动补抓不受限。
ATTACH_FAIL_BACKOFF = 180.0
# 「最近见过的附件名」记忆窗口（秒）。用途只有一个：文件传回来时给它起个
# 人看得懂的名字（2026-11-01 用户要求「上面附件叫什么就用什么」）。自动抓取
# 时 `attach_active` 就是那一条；用户**手动点**链接时没有 active 单，只能靠
# 这份记忆按扩展名对上。窗口取 10 分钟：一条卡片从出现到被人点开通常在这个
# 范围内，再长的历史名字套到新文件上就纯属猜了。
ATTACH_NAME_WINDOW = 600.0
# 卡片正文里的附件行。两种实测形态：
#   「附件：运单明细」（当日签卡片，不带扩展名 —— 点它照样会下载）
#   「明细附件：拦截风险明细.xlsx」（拦截卡片，带扩展名）
# 触发字眼 2026-10-27 起可配置（dingtalk.attach_keys，行首前缀、纯文本），
# 这里只放默认值；_attach_re() 按配置现拼正则。
DEFAULT_ATTACH_KEYS = ["附件：", "附件:", "明细：", "明细:"]

# 「手机那边附件没抓成」的措辞特征（见 AttachmentFetcher.lastStatus 的各个分支）。
# 只有命中这些词才升级成 WARNING —— 成功的结论（「已上传 xxx.xlsx」）不值得
# 再打一行，正常链路的「收到手机附件」日志已经说清楚了。
_ATTACH_BAD_WORDS = ("找不到", "失败", "放弃", "没见到", "拒绝", "异常", "读不到", "休眠")


def _clean_attach_keys(raw) -> list:
    """清洗用户配置的触发字眼：去空、去重；全空回落默认。

    接受两种形态：YAML 列表，或逗号分隔的字符串（后者是为了让
    tools/ensure_config_keys.py 能把新键补进老配置 —— 它只补标量）。
    """
    if isinstance(raw, str):
        raw = re.split(r"[,，]", raw)
    out = []
    for k in (raw or []):
        k = str(k).strip()
        if k and k not in out:
            out.append(k)
    return out or list(DEFAULT_ATTACH_KEYS)


def _attach_re(keys: list) -> "re.Pattern":
    """触发字眼 → 行首匹配正则。字眼是纯文本，必须 re.escape。

    字眼后面允许跟冒号：配了「明细附件」这种不带冒号的键时，正文是
    「明细附件：拦截风险明细.xlsx」—— 只吃空白的话捕获组会把冒号一起吞进
    文件名（2026-11-01 14:3x 实测「：拦截风险明细.xlsx」），手机端拿这个
    名字去屏上找必然找不到。
    """
    return re.compile(
        r"^(?:明细)?(?:%s)[\s：:]*(\S[^\n]{0,40})$"
        % "|".join(re.escape(k) for k in keys),
        re.M)
# 锚点对不上时「请手机翻一屏试着找回」的节流参数。
#   · PROBE_COOLDOWN：发完翻页指令后，至少等这么久才算「屏幕安定」。手机翻上去
#     再翻回来约 700ms（Scroller 里 SWIPE_MS + SETTLE_MS），期间上报的都是中间
#     态帧。
#   · PROBE_MAX：一轮里最多请手机翻几次。
#   · PROBE_SETTLE_MAX：从第一次「等」算起，最多等这么久（秒）还没找回锚点就
#     真认输。手机没手势权限、页面卡死时全靠它兜底，否则会永远等下去。
#
# ⚠ 2026-10-28 修（真机「手动上翻多屏才找回」）：
#   旧值 MAX=6 / SETTLE=20。履约-客户声音晚报这种 1152 字 + 表格 + 附件的
#   超长 H5 卡片约 5 屏高，把上一帧锚点整条顶出 3.7 屏开外 —— 6 轮 × 0.62 屏
#   = 3.7 屏的探查深度刚好不够，PROBE_MAX 认输重建基线，新消息差点随基线
#   吞掉，最后靠人手动上翻多屏才找回。试探帧不 commit、不投递（锚点找回前
#   一切都在「等」），所以加深探查只有时延代价、没有灌历史风险：14 轮
#   × 0.62 ≈ 8.7 屏。SETTLE 同步放宽到 45 秒（14 轮 × ~2 秒 ≈ 30 秒+，
#   20 秒会在半路就认输）。换群/翻历史场景多花 ~25 秒才重建，可接受。
# ⚠ 2026-10-19 修（真机漏转「拦截单退单打印提醒」）：
#   旧值是 COOLDOWN=3.0 / MAX=2，且**冷却期内命中直接判认输**。14:11:55 发出
#   翻页指令，14:11:57 手机翻完把新帧送回来 —— 只隔 2 秒，落在冷却期里 →
#   `_should_probe` 返回 False → 直接 commit 重建基线。那张长卡片（把上一帧
#   锚点整条挤出屏幕的那条）就这样被登记成「已知历史」，**既没转发也没入库**，
#   连 forward_recent.py 都补不回来。
#   根因不是数值太小，是**状态不够**：冷却期内来的新帧意味着「手机正在翻」，
#   应该继续等，而不是认输。现在拆成 probe / wait / give_up 三态，
#   冷却期只表示 wait，不再表示 give_up。
PROBE_COOLDOWN = 1.2
PROBE_MAX = 14
PROBE_SETTLE_MAX = 45.0
# 「翻不动」判据：连续这么多轮试探拿到的**内容**一模一样，就认定翻不动。
#
# ⚠ 2026-10-29 真机（用户原话「老是锚点对不上」「不跳来回跳怎么找锚点呢」）：
#   手机端的 scroll 指令语义是「上翻一屏 → 报帧 → **立刻翻回底部**」（长消息
#   补全需要回位，否则基线错位）。锚点试探借用同一条指令的后果是：**每一轮
#   都从底部出发、只能看到底部上方那同一屏**，位置根本不累积 —— 14 轮全在
#   原地打转。日志实证：连续 40 帧字节数 180558/180567（同一屏），而 H5 卡片
#   每重绘一次整批指纹都变、md5 也跟着变，于是「屏幕在动」判据永远成立，
#   试探永不收敛（20:41–20:43 一口气 175 次）。
#   修法：① 试探改走独立的 ``probe`` 指令（手机端**只上翻不回位**，位置累积），
#   认输时再下发 ``to_bottom`` 一次性开回最新；② 判「翻没翻动」不看 md5，
#   看**内容签名**（发送者+正文）—— H5 重绘骗不过它。
PROBE_STUCK_MAX = 2
# 试探状态的保质期（秒）。锚点丢失后如果长时间一帧都没来（灭屏、用户拿走
# 手机、离开会话），上一轮的试探状态不该带到现在用：真机 2026-10-26 14:16
# 试探额度用光挂在被动「等」上，之后 42 分钟没帧；14:58 爆发的第一帧一来
# 就撞上「额度已用光」直接 give_up，整波消息差点被基线重建吞掉。
# 取值要点：一轮试探的极限时长 ≈ PROBE_MAX×(冷却+翻页往返) + SETTLE，
# 2026-10-28 起 ≈ 14×2+45 = 73 秒，取 90 盖过它。一轮里没动静超过这个
# 时长就认定这轮已经死了。过期后不再翻页 —— 锚点早已
# 远在好几屏之外，翻了也翻不回，直接 give_up + 打捞（见 _salvage_rebuild）。
ALIGN_STALE = 90.0
# 打捞（_salvage_rebuild）一次最多捞几条。限制爆炸半径：判据再稳，也只
# 承担「静默期后苏醒的第一屏」，不可能有一条以上的真新消息挤在底部。
SALVAGE_MAX_UNITS = 8
# 基线打捞（_sweep_recent，2026-10-27）的时间上限：屏上消息头顶时间条读出
# 的时刻距现在 ≤ 30 分钟才捞。用户原话「手机和当前时间相差在 30 分钟的消息
# 要转发」。上限同时挡住两类风险：翻历史时满屏旧消息（时间条超龄）、
# 「星期一/10-16」这类读不出时刻的段（直接不捞）。条数上限同打捞，限制
# 爆炸半径 —— 程序重启后的第一屏正常也就十几条。
SWEEP_MAX_AGE_S = 30 * 60.0
SWEEP_MAX_UNITS = 15
# 附件抓取下单后多久没收到文件就告警并释放冷却（秒）。正常链路 8~12 秒，
# 硬上限约 40 秒（20s 等落盘 + 12 步返回）；120 秒还没文件，基本是手机端
# 没找到附件行 / UC 下载失败，不告警就只有「发现附件」一条日志，最难查。
ATTACH_FETCH_TIMEOUT = 120.0
# 长消息「攒齐再发」最多等多久（秒）。等不到补全帧就发手头那半截 ——
# 手机没手势权限、用户在操作别的界面、群消息太快把前半截顶掉，都可能让
# 补全永远不来。宁可发一条不完整的，也不能永远不发。
#
# 2026-10-18 从 25 秒放到 45 秒：补全从「翻一次」变成「最多翻 6 次」，每次
# 翻完要等界面安定 + 上报 + 排空缓冲，四五个来回就要十几秒。25 秒会在第
# 3 屏还没翻到的时候就把半截放行 —— 而放行之后这条就再也补不回来了。
LONG_WAIT_MAX = 45.0
# 同一条长消息最多补几轮。每轮 = 手机往上翻一屏（约 0.6 屏高）。
#
# 用户 2026-10-18 原话：「1.6 屏不够，要 3 屏才够，有的消息很长的」。
# 4 到 5 屏的正文轮数在 12 左右，但**不能让手机无限翻** —— 翻得越多，
# 翻出几个月前历史消息的概率越大，那些会被当成长消息的一部分转进业务群。
# 所以取 6：能稳稳覆盖 3~3.5 屏的正文（每轮 0.6 屏，两头还有重叠余量），
# 再长的消息按「翻到第 6 轮为止」交付，宁可少一节，不灌历史。
FILL_MAX_ROUNDS = 6
# 看门狗多久检查一次「手机还在不在」（秒）。比心跳间隔（手机端 60s）小得多，
# 所以失联的发现延迟最多是「心跳间隔 + 这个 tick」。
OFFLINE_TICK = 10.0
# 失联期间重复提醒的间隔（秒）。告警本身有冷却，但日志也需要一个节奏 ——
# 每 10 秒刷一条 WARNING 会把日志文件灌满，真问题反而被盖掉。
OFFLINE_REMIND_EVERY = 1800.0

# 刚启动时给心跳留的宽限期（秒）。手机端心跳 60 秒一次，而第一帧画面几乎
# 总是比第一个心跳先到 —— 不设宽限期的话，每次重启主程序都会立刻判出一条
# 「多半是旧版 APK」，把人往「装错包」的方向带。取 2 倍心跳间隔，稳妥。
HEARTBEAT_GRACE = 120.0

# 手机自动巡检的可用间隔（分钟）。用户 2026-10-20 定：不设「掉了立刻拉」，
# 只给几个固定档，避免「实时抢占前台」。
#
# 档位不多是刻意的：这个数字直接决定**最长盲区**（手机被切走后最多这么多
# 分钟的消息是在外面发的、不会被补回来），可选项多了用户反而不会算。
# 0 = 关闭，且是默认值 —— 巡检会抢前台，必须由用户明确打开。
WATCH_CHOICES = (0, 5, 10, 15, 30)


def _unquote_header(raw) -> str:
    """解手机端塞进 HTTP 头里的中文（URL 编码），失败就原样返回。

    HTTP 头只允许 ISO-8859-1，中文群名没法直接放进去。手机端用
    URLEncoder 编码成纯 ASCII，这里解回来。

    为什么要 try：这个值纯粹是给人看的诊断信息。解析失败就当没有，
    绝不能让一次「读不懂的告警文案」把心跳打成 500 —— 那会把「巡检失败」
    升级成「手机失联」，从一个假故障变成两个。
    """
    if not raw:
        return ""
    try:
        return unquote(raw.strip())
    except Exception:
        return raw.strip()


def _watch_minutes(value, choices: tuple[int, ...] = WATCH_CHOICES) -> int:
    """把配置里的巡检间隔夹到允许的档位上。

    取「不超过给定值的最大档」而不是四舍五入：用户填 7 想要的大概是「别太
    频繁」，给 10 比给 5 更容易让盲区超出他的预期。填 1 这种明显想开得很勤的，
    给最小非零档（5）—— 比它更勤就该用别的机制了，不是这个功能的职责。
    """
    try:
        v = int(value)
    except (TypeError, ValueError):
        return 0
    if v <= 0:
        return 0
    picked = 0
    for c in choices:
        if 0 < c <= v:
            picked = c
    if not picked:
        # v 比最小档还小（比如填 1、2）：按最小档处理，并让调用方在日志里说清楚
        picked = min(c for c in choices if c > 0)
    return picked


TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%m-%d %H:%M", "%H:%M")


def _is_system_note(content: str) -> bool:
    """这条消息是不是本程序自己发到群里的运维通知。

    与 ``notify.SYSTEM_PREFIX`` 保持同一个字符串 —— 那边负责「不投递」，
    这边负责「不占画面」。两边判据必须一致，否则会出现「画面里剔掉了、
    投递却放行了」这种自相矛盾（或者反过来：画面里留着、白白挤掉真消息）。
    """
    return (content or "").lstrip().startswith("【系统】")


def _join_fragments(head: str, tail: str, *, u_first: int = 0) -> str | None:
    """把同一条长消息的两截拼起来。拼不上返回 ``None``。

    手机只往上翻 62% 高度，所以两块**一定重叠**（这是设计使然，不是意外）：
    下半截的开头那几十个字会在上半截的结尾再出现一次。所以直接 ``head+tail``
    会得到一段重复的话，必须按重叠部分接上。

    三种情形（按优先级）：
      1. 完全包含：一截是另一截的前缀/子串 —— 直接取长的。手机翻得够远时
         上半截可能已经包含了整条消息。
      2. 前缀重叠：``tail`` 的开头出现在 ``head`` 的结尾。从最长的重叠往下试，
         取第一个命中的（最长重叠最可靠 —— 短重叠容易偶然撞上）。
      3. 没有重叠：多半不是同一条消息（用户中间又发了别的），返回 ``None``，
         让调用方别乱拼。

    ``u_first`` = 这一帧里 ``tail`` 那条消息的顶边 y。补全帧里**上面那段才是
    前半截**，所以先按坐标把它们摆正再拼：``head`` 是「已攒住的那截」，
    ``tail`` 是「这一帧里同一个人发的另一截」，两者的上下关系由 ``u_first``
    与攒住时的位置对比决定 —— 但调用方没传位置时（``u_first == 0``）就按
    「head 在上」的默认顺序，与旧行为一致。
    """
    a, b = (head or "").strip(), (tail or "").strip()
    if not a or not b:
        return None
    if a == b:
        return a
    if a in b:                      # head 是 tail 的一段 → tail 更全
        return b
    if b in a:                      # tail 是 head 的一段 → head 更全
        return a

    # 前缀重叠：从长到短试。上限取较短那截的长度（重叠不可能更长）。
    limit = min(len(a), len(b))
    for n in range(limit, 7, -1):   # 至少重叠 8 个字才算数，防偶然撞上
        if a.endswith(b[:n]):
            return a + b[n:]
    # 反向也试一次：补全帧里翻到了更靠上的那段时，拼接方向是反的
    # （tail 在上、head 在下）。不试的话这种情形一律判「拼不上」，
    # 长消息就永远补不齐，只能等 25 秒超时发半截。
    for n in range(limit, 7, -1):
        if b.endswith(a[:n]):
            return b + a[n:]
    return None


# 判断「新截出来的一段接在全文的哪一头」时，最少要重叠几个字才算数。
# 和 `_join_fragments` 里的 8 对齐：比这更短的重叠容易偶然撞上（中文里
# 「的」「了」这种高频字两端凑巧相同是常事），撞上就会把顺序接反，
# 结果是一段顺序错乱的正文 —— 比缺一段更难发现。
_ALIGN_MIN = 8


def _align_fragment(known: str, fresh: str) -> tuple[str, str] | None:
    """把新截出来的一段正文对齐到已拼好的全文上。拼不上返回 ``None``。

    返回 ``(新全文, 方向)``，方向是 ``"up"``（这一段接在上面）或
    ``"down"``（接在下面）。

    ── 为什么不直接用 `_join_fragments`
    `_join_fragments(head, tail)` 回答的是「怎么拼成一段」，但它**假定了谁在
    上谁在下**（先试 head 结尾 = tail 开头，再试反向）。补全是多轮的：第 2 轮
    新露出的那截可能在全文上面（继续往上翻），也可能在下面（手机翻回来时列表
    底部又露出了新的），方向必须由调用方从**内容**判断出来，不能猜。
    猜错的后果是正文顺序错乱，而且拼出来照样「像一段完整的话」。

    判据按可靠性排序：
      ① 完全相同 / 互为包含 —— 取长的那个（没长进也算对齐，方向沿用）；
      ② 已知全文的**结尾**出现在新段**开头** → 新段在下面；
      ③ 已知全文的**开头**出现在新段**结尾** → 新段在上面；
      ④ 新段**整个落在**已知全文中间 → 没长进，原样返回（不算失败）。

    ①②③④ 都不成立就是拼不上（多半不是同一条消息，或者中间漏了一整屏）。
    此时**绝不硬拼** —— 拼出来的是一段顺序错乱的正文，比缺一段更难发现。

    ── ④ 为什么必须有
    多轮补全里，某几轮手机只多露一点点，新段可能整段都落在已经攒好的全文
    里面（完全被覆盖）。判成「拼不上」的话，这一轮被记成「没有进展」，连续
    两轮就会被当成「翻不动了」提前收尾 —— 而实际上再翻一轮还能接着补。
    """
    a, b = (known or "").strip(), (fresh or "").strip()
    if not a:
        return (b, "down") if b else None
    if not b:
        return a, "down"
    if a == b:
        return a, "down"
    if a in b:
        return b, "down"            # 新段把已知全文整个包含了，更全
    if b in a:
        return a, "down"            # 新段落在已知全文里，没长进（见 ④）

    limit = min(len(a), len(b))
    for n in range(limit, _ALIGN_MIN - 1, -1):
        if a.endswith(b[:n]):
            return a + b[n:], "down"
    for n in range(limit, _ALIGN_MIN - 1, -1):
        if b.endswith(a[:n]):
            return b + a[n:], "up"
    return None


# ------------------------------------------------------------------ 手机端下载

def _apk_version(p: "Path") -> tuple:
    """从文件名里取版本号，取不到就排最后。

    ``ddwx-collector-0.2.0.apk`` → ``(0, 2, 0)``。
    文件名不合格式（或纯数字段不是数字）时返回空元组，排序时垫底 ——
    宁可让用户拿到「格式规范的那个」，也不要随便挑一个。
    """
    rows: list[int] = []
    for part in p.stem.split("-")[-1].split("."):
        if not part.isdigit():
            return ()
        rows.append(int(part))
    return tuple(rows)


def _find_apk() -> "Path | None":
    """找最新编出来的 APK。

    **按版本号挑，不按 mtime。** 早先只取 mtime 最新的，真机上踩过：
    安装版把 APK 解压到 ``app/``，同一次解压出来的 0.1.0 与 0.2.0
    mtime 精确到秒**完全一样**，``max()`` 遇到相等时保留先遇到的，
    于是页面一直发 0.1.0 —— 那个版本缺手势权限、没有 /ping 心跳，
    用户装上后长消息补全失效，而页面上写着「下载并安装 0.1.0」，
    看起来完全正常，极难排查。

    mtime 只在版本号相同时（或都解析不出）才作为次级判据，
    这样「重新编过同一个版本号」仍能拿到新的那个。
    """
    dirs = _APK_DIRS or _apk_search_dirs()
    cands: list[Path] = []
    for d in dirs:
        try:
            if d.is_dir():
                cands.extend(p for p in d.glob("*.apk") if p.is_file())
        except OSError:
            continue
    if not cands:
        return None
    return max(cands, key=lambda p: (_apk_version(p), p.stat().st_mtime))


_APK_DIRS: "list[Path]" = []


def set_apk_dirs(dirs) -> None:
    """由采集端在启动时告诉网关「去哪儿找 APK」。

    不在这里直接算路径，是因为「程序目录在哪」是采集端才知道的事
    （打包成 exe 之后与源码目录完全不同）。网关只负责吐文件。
    """
    global _APK_DIRS
    _APK_DIRS = [Path(str(d)) for d in dirs]


def _apk_search_dirs() -> list[Path]:
    """可能放着 APK 的目录，按「越可能是最新的」排前面。

    覆盖的形态：
      · 源码跑（``python run.py`` / ``python -m ...``）—— 仓库里的 ``android/dist``；
      · 安装版 —— 程序目录下的 ``app/``（安装时把 APK 放这里），以及程序目录本身。

    ⚠ **不能用 ``sys.executable`` 推程序目录。** 2026-10-18 真机踩过：安装版的
    ``喵喵钉钉-微信消息转发桥.exe`` 只是个小启动器，真正的网关跑在它拉起的
    **worker 子进程**里（用 ``_env.json`` 里那个 Python 解释器），于是
    ``sys.executable`` 指向 ``.../envs/default/Scripts/python.exe``，
    ``exe_dir`` 算成那个 Scripts 目录 —— 安装目录**根本没进候选**，
    ``app/ddwx-collector-0.3.0.apk`` 从来没被扫到过，页面一直发老版本。

    ⚠ 也不能只用 ``__file__``：PyInstaller 打包后它指向临时解包目录，
    用户往里放 APK 是白放。—— 但本程序**没有**用 PyInstaller 完整打包
    （启动器形态，源码就在程序目录里），模块路径就是最终落盘路径，可行。
    结论：以「模块文件 → 上溯两级 = 仓库根/程序目录」为主，``sys.executable``
    仅作补充候选（真打了 PyInstaller 单文件包时还能兜一层）。
    """
    out: list[Path] = []
    here = Path(__file__).resolve()
    # .../程序目录/bridge/sources/android_gateway.py → 上溯到程序目录（= 源码里的仓库根）
    root = here.parents[2] if len(here.parents) > 2 else here.parent
    # root/app 排最前：安装版把「当前该发的包」放在这里，语义最明确
    out.append(root / "app")
    out.append(root / "android" / "dist")
    exe_dir = Path(sys.executable).resolve().parent
    out.append(exe_dir / "app")
    out.append(exe_dir)
    # 去重且保序
    seen, uniq = set(), []
    for d in out:
        key = str(d).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(d)
    return uniq


def _app_page(apk: "Path | None", ctx) -> str:
    """手机浏览器打开的那个下载页。

    刻意把「IP / 端口 / token」三样东西**直接印在页面上**：手机装完 App 要手填
    这三项，而 token 是 24 位随机串，用肉眼从电脑抄到手机是最容易错的一步。
    页面就在这里，照着抄（或长按复制）即可。
    """
    ips = lan_ips()
    host = ips[0] if ips else "（没探测到，请在电脑上 ipconfig 查看）"
    token = ctx.token or ""
    if apk is not None:
        size = apk.stat().st_size
        when = time.strftime("%m-%d %H:%M", time.localtime(apk.stat().st_mtime))
        download = f"""
        <a class="btn" href="/app.apk?t={int(time.time())}">下载并安装 {apk.name}</a>
        <p class="meta">{size // 1024} KB ｜ 编译于 {when}</p>
        <p class="tip">下载完成后，在通知栏或「文件管理 → 下载」里点开这个文件安装。<br>
        若提示「禁止安装未知应用」，按提示允许「浏览器」安装即可。</p>"""
    else:
        download = """
        <div class="warn">电脑上还没找到 APK。<br>
        请在 <code>ddwx-bridge/android</code> 下执行 <code>python build.py</code>
        编译一次，然后刷新本页。</div>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>喵喵采集端 · 安装</title>
<style>
  :root {{ color-scheme: light; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; padding:20px 16px 40px; background:#f5f6f8; color:#1f2328;
         font:16px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif; }}
  .card {{ max-width:520px; margin:0 auto; background:#fff; border-radius:14px;
           padding:22px 20px; box-shadow:0 1px 3px rgba(0,0,0,.08); }}
  h1 {{ font-size:19px; margin:0 0 4px; }}
  .sub {{ color:#6b7280; font-size:13px; margin:0 0 18px; }}
  .btn {{ display:block; text-align:center; background:#07c160; color:#fff;
          text-decoration:none; padding:15px; border-radius:10px;
          font-weight:600; font-size:17px; margin:6px 0 10px; }}
  .btn:active {{ background:#06ad56; }}
  .meta {{ text-align:center; color:#9ca3af; font-size:12px; margin:0 0 14px; }}
  .tip {{ color:#6b7280; font-size:13px; background:#f9fafb; padding:12px;
          border-radius:8px; margin:0; }}
  .warn {{ background:#fff7ed; color:#9a3412; padding:14px; border-radius:8px;
           font-size:14px; }}
  h2 {{ font-size:15px; margin:24px 0 10px; padding-top:18px;
        border-top:1px solid #eef0f3; }}
  .row {{ display:flex; align-items:center; padding:11px 0;
          border-bottom:1px solid #f3f4f6; }}
  .row:last-child {{ border-bottom:none; }}
  .k {{ color:#6b7280; font-size:13px; width:88px; flex:none; }}
  .v {{ font-family:ui-monospace,Menlo,Consolas,monospace; font-size:16px;
        font-weight:600; word-break:break-all; user-select:all; }}
  ol {{ padding-left:20px; margin:8px 0 0; color:#374151; font-size:14px; }}
  ol li {{ margin:7px 0; }}
  code {{ background:#f3f4f6; padding:1px 5px; border-radius:4px; font-size:13px; }}
</style></head>
<body><div class="card">
  <h1>喵喵采集端</h1>
  <p class="sub">把钉钉群消息自动转到微信的那台手机的采集程序</p>
  {download}

  <h2>装完后在 App 里填这三项</h2>
  <div class="row"><span class="k">IP 地址</span><span class="v">{host}</span></div>
  <div class="row"><span class="k">端口</span><span class="v">{ctx.port}</span></div>
  <div class="row"><span class="k">token</span><span class="v">{token}</span></div>
  {'<p class="meta" style="text-align:left;margin-top:10px">其它地址：' + "、".join(ips[1:]) + '</p>' if len(ips) > 1 else ''}

  <h2>装好之后</h2>
  <ol>
    <li>打开 App，填上面三项，点「测试连接」，看到绿色「连通」即可。</li>
    <li>点「打开系统无障碍设置」，打开「喵喵采集端·读钉钉」。</li>
    <li><b>把无障碍开关关掉再重新打开一次</b> —— 新版加了手势权限，
        不重开会导致长消息补全失败。</li>
    <li>回 App，滚到底部把「保活」两个按钮都点一遍。</li>
  </ol>
</div></body></html>"""


def _online_now(ctx: "_Ctx") -> bool:
    """此刻手机算不算在线 —— 按「最后一次心跳距今多久」算，不是「曾经连上过」。

    这里踩过一次：早先写成 ``bool(ctx.last_seen)``，于是手机锁屏八分钟、一次
    心跳都没有的时候，``/ping`` 还在回 ``online: true``，把排查的人（连我自己）
    引到「网络没问题，是不是电脑侧卡了」的错误方向上。字段名既然叫 online，
    就必须回答「现在」，而不是「历史上」。
    """
    last = ctx.last_seen
    if last <= 0.0:
        return False
    span = ctx.offline_after
    if span <= 0.0:          # 关掉失联检测时，只能退回「收到过就算在线」
        return True
    return (time.monotonic() - last) <= span


# --------------------------------------------------------------- 失联看门狗

def _fmt_span(seconds: float) -> str:
    s = int(max(0.0, seconds))
    if s < 60:
        return f"{s} 秒"
    if s < 3600:
        return f"{s // 60} 分钟"
    return f"{s // 3600} 小时 {s % 3600 // 60} 分钟"


def _watchdog_step(ctx: "_Ctx", started: float, now: float,
                   offline_after: float) -> tuple[int, str, tuple] | None:
    """判一次「手机还在不在」，顺带把状态迁移写回 ``ctx``。

    返回 ``(日志级别, 模板, 参数)``，不该说话时返回 ``None``。

    抽成纯函数（只依赖传入的 ``now``、不自己取时钟）是为了**可测**：
    走线程 + sleep 的测试既慢又会在负载高时随机红，而这里的每一条分支
    （从没收到过 / 掉线 / 恢复 / 旧版没有心跳）都是必须逐条锁住的现场结论。
    """
    msg: tuple[int, str, tuple] | None = None
    with ctx.seen_lock:
            last, heard = ctx.last_seen, ctx.heard_heartbeat
            state, device = ctx.state, ctx.last_device
            wall, last_alert = ctx.last_seen_wall, ctx.last_alert_at
            gap = (now - last) if last > 0.0 else (now - started)
            new_state = state

            # ---- 情况一：启动之后一次都没收到过 ----
            if last <= 0.0:
                if state != "missing" and gap >= offline_after:
                    new_state = "missing"
                    ctx.last_alert_at = now
                    msg = (logging.WARNING,
                           "安卓网关已就绪 %s，但一次都没有收到手机的上报。"
                           "手机端请自查：App 在运行吗？无障碍服务还开着吗？"
                           "填的 IP/端口/token 与电脑端一致吗（token 不对会一直是"
                           " 401）？手机和电脑在同一 Wi-Fi 吗？",
                           (_fmt_span(gap),))
                elif state == "missing" and now - last_alert >= OFFLINE_REMIND_EVERY:
                    ctx.last_alert_at = now
                    msg = (logging.WARNING,
                           "手机端仍然没有任何上报（已经 %s）。",
                           (_fmt_span(gap),))

            # ---- 情况二：有上报但从没心跳（旧版 APK，没有心跳就没法判失联）----
            elif not heard:
                # 宽限期：手机端心跳 60 秒一次，而**第一帧画面往往比第一个心跳先到**
                # （重启主程序、或手机刚上报完一帧后的头一分钟就是这样）。这时候
                # 立刻下「旧版 APK」的结论是错的，真机重启时每次都这么报一次，
                # 排查的人会以为装错了包，白折腾。
                if state != "online" and now - started >= HEARTBEAT_GRACE:
                    new_state = "online"
                    msg = (logging.INFO,
                           "已收到手机（%s）的上报，但它没有发心跳 —— 多半是旧版 APK。"
                           "旧版只在画面变化时上报，无法区分「App 被系统杀了」和"
                           "「群里没人说话」，所以失联检测对旧版不启用（否则必然"
                           "误报）。要准确检测请把手机端升级到 0.2.0。",
                           (device or "未知机型",))

            # ---- 情况三：正常在线 ----
            elif gap <= offline_after:
                if state == "offline":
                    away = now - (ctx.offline_last_seen or last)
                    ctx.offline_last_seen = 0.0
                    msg = (logging.WARNING,
                           "手机端恢复上报（断开了约 %s）。", (_fmt_span(away),))
                elif state == "missing":
                    msg = (logging.WARNING,
                           "手机端终于连上了（网关启动后空等了 %s）。",
                           (_fmt_span(now - started),))
                new_state = "online"

            # ---- 情况四：失联 ----
            elif state != "offline" or now - last_alert >= OFFLINE_REMIND_EVERY:
                new_state = "offline"
                ctx.last_alert_at = now
                ctx.offline_last_seen = last
                msg = (logging.WARNING,
                       "手机端失联：已经 %s 没有心跳（最后一次 %s，机型 %s）。"
                       "常见原因：系统把 App 清理了 / 锁屏断网 / 手机换了 Wi-Fi。"
                       "处理：打开手机上的采集端 App，看「最后动作」那一行是不是"
                       "「上报失败」；若 App 已被关掉，重新打开并确认无障碍服务"
                       "仍然开启。",
                       (_fmt_span(gap), wall or "未知", device or "未知机型"))

            ctx.state = new_state
    return msg


# --------------------------------------------------------------- 先攒后发

# 排空缓冲的节拍。要明显小于 ``pending.QUIET``（默认 3 秒），否则消息会
# 比「静默 3 秒」晚到一大截 —— 0.5 秒的抖动可以接受，2 秒就明显滞后了。
FLUSH_TICK = 0.5


def _flush_loop(ctx: "_Ctx", stop: threading.Event) -> None:
    """定期把缓冲里静默够久的消息投出去。

    单独一个线程而不是在 HTTP 请求里顺手做：``on_message`` 会**同步**等微信
    把消息发出去（OCR 通道要点会话、贴文本、等气泡出现，可能几秒）。放在
    HTTP 线程里会让手机的下一次上报被堵住 —— 而「攒」这件事本身就依赖手机
    持续上报，堵住上报等于攒不到内容。

    出错只记日志、不退出循环：一次投递失败（微信没开、会话没找到）不该让
    后面所有消息都卡住。失败的那几条已经从缓冲里拿出来了，按现状丢弃 ——
    它们仍在**手机屏上**，下次采集还会再来一遍。
    """
    while not stop.wait(FLUSH_TICK):
        try:
            buf = ctx.pending
            if buf is None:
                continue
            ready = buf.drain()
            if not ready:
                continue
            msgs = [m for m in (_rebuild_message(e) for e in ready)
                    if m is not None]
            if ctx.on_message is None or not msgs:
                continue
            _deliver_ready(ctx, msgs)
        except Exception:
            log.exception("排空消息缓冲失败（不影响后续）")


def _rebuild_message(entry) -> "Message | None":
    """把一个缓冲条目还原成**最终要发的** Message。

    ⚠ 不能直接用 ``entry.extra["message"]`` —— 那是**第一次**收到这条消息时
    构造的 Message，正文是当时那个半成品。缓冲会把后面更全的版本合并进
    ``entry.content``，但 Message 是不会自己跟着变的对象。2026-10-18 实测：
    卡片分三段长出来，缓冲里存的已是最全的 29 字，群里收到的却是第一段的
    6 字（``test_growing_h5_card_only_the_fullest_version_is_sent`` 抓到的）。

    所以这里拿**第一版的 Message 当模板**（uid / raw_id / msg_time / company
    都保持首次见到时的值 —— 去重要靠它），只把 ``content`` 和
    ``sender_name`` 换成缓冲里最全的那版。

    ``raw_id`` 保持不动的理由：同一条消息分几批渲染出来，如果每批都换一个
    uid，下游 `is_duplicate` 就挡不住，手机上重报一帧就多发一条。
    """
    base = entry.extra.get("message")
    if base is None:
        return None
    try:
        return replace(base, content=entry.content or base.content,
                       sender_name=entry.sender or base.sender_name,
                       chat_name=entry.chat_name or base.chat_name)
    except Exception:                       # noqa: BLE001  —— 兜底，别丢消息
        log.exception("重建缓冲消息失败，按首次见到的版本投递：%s",
                      (base.content or "")[:40])
        return base


def known_hit(sender: str, content: str, known) -> bool:
    """这条单元在已投递记录里吗？

    短消息 `is_same_message` 一律答 False，补一条精确相等兜底。判据刻意
    收得极紧：要么正文逐字相等，要么「字面片段集合」完全相等（即同一张
    卡片换了渲染形态，见 `fingerprint.is_same_message`）—— 宁可多发，
    也不能把两条真正不同的消息判成同一条吃掉。

    打捞（`_salvage_rebuild`）与正常投递（`_deliver_ready`）共用这一份，
    **别各写一份**（指纹/判据漂移的坑本仓库踩过，见 fingerprint.py 模块注释）。
    """
    ns = norm_sender(sender)
    return any((s == ns or not s or not ns)
               and (c == content or is_same_message(c, content))
               for s, c in known)


def _deliver_ready(ctx: "_Ctx", msgs: list) -> int:
    """同 ``_GatewayHandler._deliver`` 的投递部分，供 flusher 线程复用。

    刻意不把 `_deliver` 提成模块级函数：它依赖 `self.ctx.recent_keys` /
    `self.ctx.count` 这套状态，挪出去要改一圈调用点。这里只复刻**投递 +
    近似去重**这一段，逻辑必须与那边保持一致（判据都来自
    `fingerprint.is_same_message`，不会各写一份）。
    """
    now = time.monotonic()
    emitted = 0
    for msg in msgs:
        # ⚠ `no_forward`（基线里补全出来的历史消息）**不在这里拦**。
        #   本模块不持数据库，拦在这里 = 既不入库也不投递 = 消息彻底消失，
        #   而这条正文的价值恰恰是「留档，事后能用 forward_recent.py 查」。
        #   正确落点是 `service.handle()`：它先 save_message 再判 no_forward
        #   决定发不发。网关这一层的责任只是**别把标记弄丢**（`_rebuild_message`
        #   用 replace 重建，字段会原样带过去）。
        sender = norm_sender(msg.sender_name)
        with ctx.lock:
            for k, (_, _, at) in list(ctx.recent_keys.items()):
                if now - at > NEAR_DUP_WINDOW:
                    del ctx.recent_keys[k]
            dup_age = next(
                (now - at for s, old, at in ctx.recent_keys.values()
                 if s == sender and is_same_message(old, msg.content)),
                None)
        if dup_age is not None:
            log.info("同一卡片换了渲染形态（%.0f 秒前已发过同一条），跳过：%s",
                     dup_age, msg.content[:40].replace("\n", " "))
            with ctx.lock:
                ctx.count["rejected"] += 1
            continue
        # ⚠ NEAR_DUP_WINDOW 只是短窗。真机 2026-10-26 23:50→00:02：同一条
        #   「当日签」卡片在 12 分钟后被翻屏重渲染（「当前情况」块与 top10
        #   表格行顺序互换 → 指纹变），窗口已过，被当成新消息又投了一遍。
        #   `delivered_texts` 没有时限（deque 512 条），判据与打捞共用
        #   `known_hit`。⚠ 只对长消息生效（≥ NEAR_MIN_LEN）：delivered_texts
        #   无时限，若让「收到」这类短消息的精确相等也进来，正常的重复发言
        #   会被吃掉 —— 短消息宁可多发，这是 fingerprint.py 定死的方针。
        if len(content_key(msg.content)) >= NEAR_MIN_LEN:
            with ctx.lock:
                known = list(ctx.delivered_texts)
            if known_hit(sender, msg.content, known):
                log.info("投递记录里已有同一条（超出去重窗口的长窗兜底），跳过：%s",
                         msg.content[:40].replace("\n", " "))
                with ctx.lock:
                    ctx.count["rejected"] += 1
                continue
        try:
            ctx.on_message(msg)
            emitted += 1
            with ctx.lock:
                ctx.recent_keys[msg.uid] = (sender, msg.content,
                                            time.monotonic())
                # 打捞（_salvage_rebuild）的「已投递」对照记录。比 recent_keys
                # 的近似去重窗口长得多 —— 用户翻回早前的历史时，那些消息早
                # 出了 NEAR_DUP_WINDOW，只能靠这份滚动记录挡住不重发。
                ctx.delivered_texts.append((sender, msg.content))
        except Exception:
            log.exception("下游处理消息失败：%s", msg.uid)
    with ctx.lock:
        ctx.count["accepted"] += emitted
    if emitted:
        head = next((m.content[:40].replace("\n", " ") for m in msgs), "")
        log.info("安卓画面新增 %d 条消息：%s%s", emitted, head, tail("new_msgs"))
    return emitted


def _watchdog(ctx: "_Ctx", offline_after: float,
              stop: threading.Event) -> None:
    """盯着「手机还在不在」，状态**只在变化时**说话。

    为什么必须有这东西：手机端 App 被系统清掉、锁屏断网之后，电脑侧什么异常
    都收不到 —— 没有连接、没有请求、没有报错。表现出来的样子是「程序安安静静
    在跑」，而实际上一条消息都不会再转发。这种静默失效只能靠主动探测发现。

    判据用**心跳**（手机端每 60 秒 POST /ping），不用「多久没收到画面」：
    群里半天没人说话时画面本来就不变、手机也不上报，按画面判会天天误报，
    而误报几次之后人就再也不看告警了 —— 那比不告警更糟。

    决策在锁内、日志在锁外。看着绕，但必须这样：告警处理器是**同步发 HTTP**
    到钉钉的（要几秒），持锁写日志会把手机下一次上报一起卡住，
    现场表现成「一告警就断线」，反而更像手机出问题。
    """
    started = time.monotonic()
    while not stop.wait(OFFLINE_TICK):
        msg = _watchdog_step(ctx, started, time.monotonic(), offline_after)
        if msg is not None:
            log.log(msg[0], msg[1], *msg[2])


# --------------------------------------------------------------- 报文解析

def _parse_time(value: Any, now: datetime | None = None) -> datetime:
    """宽容解析手机端给的时间；认不出来就退回本机时间。

    时间只用于展示与下游格式，不参与去重（去重靠指纹），所以这里刻意取
    「宁可用本机时间，也不拒收」：手机端时区、格式的差异不值得把消息丢掉。
    支持 ``"2026-10-16 20:31:00"``、``"20:31"``、秒级/毫秒级时间戳。
    """
    now = now or datetime.now()
    if value is None or value == "":
        return now
    if isinstance(value, bool):
        return now
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e11:                       # 毫秒时间戳
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts)
        except (OverflowError, OSError, ValueError):
            return now

    text = str(value).strip().replace("T", " ")
    if text.isdigit():
        return _parse_time(int(text), now)
    text = text.split(".")[0].strip()
    for fmt in TIME_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt == "%m-%d %H:%M":
            return dt.replace(year=now.year)
        if fmt == "%H:%M":
            return now.replace(hour=dt.hour, minute=dt.minute,
                               second=0, microsecond=0)
        return dt
    return now


def build_messages(payload: dict, *, chat_name: str, companies: list[str],
                   now: datetime | None = None) -> tuple[list[Message], list[str]]:
    """把一次上报的报文变成 Message 列表，返回 ``(消息, 跳过原因)``。

    跳过不算错误：手机端可能读到纯图片/纯文件消息（没有文本），那是正常的，
    不该让它一直重传，所以也回 200。

    ``payload`` 既可以是 ``{"messages": [...]}``，也可以是单条消息对象本身
    （便于用 curl 手工验证）。
    """
    raw = payload.get("messages")
    if raw is None and ("content" in payload or "sender" in payload):
        raw = [payload]                     # 单条简写
    if not isinstance(raw, list):
        raise ValueError("messages 必须是数组")

    # 会话名优先级：报文里带的最准（手机知道自己在哪个群）→ 配置里第一个当兜底。
    # 配置里的常是片段（如「数字质控」），用来做路由的子串匹配，不该盖掉真名。
    default_chat = str(payload.get("chat_name") or "").strip() or chat_name
    out: list[Message] = []
    skipped: list[str] = []

    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            skipped.append(f"第 {idx + 1} 条不是对象")
            continue

        sender = str(item.get("sender") or "").strip()
        content = str(item.get("content") or "").strip()
        if not content:
            skipped.append(f"第 {idx + 1} 条没有文本内容")
            continue

        msg_chat = str(item.get("chat_name") or "").strip() or default_chat

        msg_type = str(item.get("msg_type") or "text").strip().lower()
        if msg_type not in MSG_TYPES:
            msg_type = "unknown"

        try:
            seq = max(1, int(item.get("seq") or 1))
        except (TypeError, ValueError):
            seq = 1

        fp = str(item.get("fp") or "").strip() or fp_of(sender, content)
        # 展示用昵称：自己发的统一成「(我)」，与 OCR 路径口径一致
        sender_name = "(我)" if item.get("is_self") else sender

        out.append(Message(
            source="android",
            chat_id=str(item.get("chat_id") or "").strip(),
            chat_name=msg_chat,
            sender_id="",
            sender_name=sender_name,
            company=infer_company(companies, msg_chat, sender_name, content),
            content=content,
            msg_type=msg_type,
            msg_time=_parse_time(item.get("time"), now),
            raw_id=f"{fp}:{seq}",
        ))
    return out, skipped


# --------------------------------------------------------------- 网络工具

def lan_ips() -> list[str]:
    """本机在局域网里的 IPv4 地址，用于提示手机该连哪个地址。"""
    ips: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))      # 不发包，只让系统选出出口网卡
            ips.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips


# --------------------------------------------------------------- HTTP 服务

def _want_frame(ctx: "_Ctx") -> int:
    """电脑侧现在要不要手机重发一帧？返回请求编号（0 = 不要）。

    为什么会需要：手机端「画面 md5 没变就不发」是省流量的正确设计，但它和
    「电脑侧没有基线」撞在一起就是死锁 —— 电脑重启后手上什么都没有，手机却
    觉得「跟上次一样，没什么可报的」，于是一个包都不发；用户看到的现象就是
    「群里有消息不转发，我手动翻一下手机才突然补上来」（2026-10-27 真机）。

    两种情形要：
    ① **没有基线**（刚启动 / 换群重建前）：这时候屏上有什么我都看不见，
       只能求它发一帧；
    ② **半小时一帧都没有**：群里可能真没人说话，但也可能采集端已经瞎了
       （灭屏挂起、无障碍被系统关掉、App 被杀）。要一帧能立刻区分这两件事
       —— 有业务帧 = 手机在岗只是安静；只有诊断帧 = 手机在但看不见钉钉。

    编号用**墙上时间秒**而不是自增计数：电脑重启后计数从 0 开始会小于手机
    记住的旧值，手机就当成「处理过的老请求」忽略掉 —— 时间戳单调，重启不倒退。
    """
    now = time.time()
    differ = getattr(ctx, "differ", None)
    if differ is None:
        # 只收不转（android_parse = false）：没有基线这回事，屏上有什么都不解析，
        # 求帧纯属白烧流量，别要。
        return 0
    need = not getattr(differ, "baseline_built", False)
    if not need:
        last_ts = float((ctx.screen or {}).get("ts") or 0.0)
        need = (now - last_ts) > WANT_FRAME_IDLE_S
    if not need:
        return 0
    if now - ctx.frame_req_at < WANT_FRAME_MIN_GAP:
        # 刚求过：把**同一个**编号再回一次（手机记过就不会重复处理），
        # 免得心跳 60 秒一轮把手机变成刷帧机。
        return ctx.frame_req
    # 严格递增：同一秒内连求两次时 int(now) 会相同，手机就会当成老请求忽略。
    # （编号是秒级时间戳，手机端用 int 收 —— 2038 年前不会溢出。）
    ctx.frame_req = max(int(now), ctx.frame_req + 1)
    ctx.frame_req_at = now
    # 日志节流：基线没建起来时这里每 60 秒会走一次，不节流会把日志刷成一片。
    # 10 分钟报一次刚好 —— 一条都不报会让「求了但手机不理我」完全无声。
    if now - ctx.frame_log_at >= 600:
        ctx.frame_log_at = now
        log.info("手机这阵子没给过画面（%s），请它重发一帧当前屏幕%s",
                 ("电脑侧还没建基线" if not getattr(differ, "baseline_built", False)
                  else "超过 %.0f 分钟零帧" % (WANT_FRAME_IDLE_S / 60)),
                 tail("frame_req"))
    return ctx.frame_req


def _screen_meta(screen: dict | None) -> dict:
    """画面缓冲的对外摘要。

    **刻意不含 XML 正文**：正文可能几百 KB，塞进 JSON 首页或日志里只会让
    「看一眼状态」这件事变成「拉一份大文件」。要正文走 ``GET /screen?raw=1``。
    """
    if not screen:
        return {"received": False, "hint": "还没收到任何画面"}
    ts = screen.get("ts")
    age = None if not ts else max(0.0, round(time.time() - float(ts), 1))
    return {
        "received": True,
        "at": screen.get("at", ""),
        "age_sec": age,
        "bytes": screen.get("bytes", 0),
        "md5": screen.get("md5", ""),
        "texts": screen.get("texts", 0),
        "device": screen.get("device", ""),
        "reason": screen.get("reason", ""),
        "count": screen.get("count", 0),
    }


# 键后「只剩文件格式标记」的行 —— 它是**卡片按钮文案**（「查看完整月报明细
# （Excel）」），不是「附件：<文件名>」。真机 2026-10-27 19:52 抓到过「（Excel）」
# 这种名字：日志看不出是哪个附件，手机端也只能靠 fallback 点「随便哪一行」。
# 这种行直接用**整行原文**当名字（它就是屏上的可点文本，手机端 contains 必中）。
_FORMAT_ONLY_RE = re.compile(
    r"^[（(【\[]?\s*(?:excel|xlsx?|xlsm|word|docx?|pdf|csv|zip|rar|7z|txt|pptx?"
    r"|图片|文件)\s*[）)】\]]?$", re.I)

# 名字**中间夹着**格式标记的行尾（「查看完整履约失败明细（Excel）」）。
#
# 2026-11-01 18:06 用户在群里看到晚报卡片、正文转过来了、附件却没来。原因不是
# 没认出这行，而是**认出来的名字短了一截**：
#
#     行原文：查看完整履约失败明细（Excel）
#     键「查看完整」命中 → 捕获组只剩：履约失败明细（Excel）
#
# 名字少了「查看完整」四个字，手机端 `contains(name)` 照样能中，但电脑侧日志/
# 去重/补发都以这个残缺名做键，且同屏还有「查看完整月报明细（Excel）」时手机端
# 的 fallback 分支很容易点错行。更根本的是：**屏上可点的就是整行**，名字取整行
# 才与真机一致 —— 与 `_FORMAT_ONLY_RE` 同一个处理方式。
_FORMAT_IN_NAME_RE = re.compile(
    r"[（(【\[]\s*(?:excel|xlsx?|xlsm|word|docx?|pdf|csv|zip|rar|7z|txt|pptx?"
    r"|图片|文件)\s*[）)】\]]", re.I)

# 「行内含」判据里允许出现在字眼两侧的分隔符（单个字符即算边界）。
# 刻意**不含**汉字/字母/数字：正文里「请下载附件查看」这种叙述句左邻「请」
# 右邻「查」，都不在类里 → 不会被误认成按钮文案。
_INLINE_SEP_CLS = r"\s｜|、，,。；;：:（）()\[\]【】“”\"'·—"

# ── 整行就是文件名（2026-11-01 17:22 实锤）────────────────────────────
#
# 拦截跟单日报的卡片底部直接写着：
#
#     附件
#     未及时转出明细.xlsx
#
# 这行既不以上述任何前缀开头（不是「附件：xxx」），也没有「下载/查看」动词
# （万能字眼要求名词+动作），于是**从来没下过单** —— 用户在群里看到正文转过
# 来了、附件永远不来。判据收得很窄，只认「整行就是一个文件名」：允许中文/
# 字母/数字/括号/连字符，长度 ≤60，且以文档扩展名收尾；正文里提到文件名的
# 长句子（带空格、逗号、句号）不会被误判。
_DOC_EXT_RE = re.compile(
    r"\.(xlsx|xlsm|xls|csv|docx|doc|pdf|zip|rar|7z|txt|pptx|ppt)$", re.I)
_FILE_LINE_RE = re.compile(
    r"^[\w\u4e00-\u9fa5（）()\[\]【】\-—_.·＋+&＆]{1,60}"
    r"\.(?:xlsx|xlsm|xls|csv|docx|doc|pdf|zip|rar|7z|txt|pptx|ppt)$", re.I)

# ── 万能字眼（2026-11-01 用户：「加个万能的字眼，附件、excel、明细都下载」）──
#
# 只认「短行 + 名词 + 动作」三件套，不是「正文里出现『明细』二字就抓」：
#   · 短行（≤16 字）：屏幕上可点的按钮/链接就是这么短；超长的多半是说明句；
#   · 名词：附件/明细/表格/excel… 说明「有个文件」；
#   · 动作：下载/查看/获取… 说明「这行是让你去拿文件的」。
# 反例（都不抓，正是想要的）：
#   「明细如下」           只名词无动作
#   「点击查看详情」       只动作无名词
#   「所有超过2天积压数据我已经整理成Excel附件，请立即催签！」  超长且带句读
# 名字取**整行原文**：手机端就是按这段文字在屏上 find 节点并点它的 bounds。
AUTO_ATTACH_NOUNS = ("附件", "明细", "表格", "清单", "报表", "名单", "汇总表",
                     "excel", "xlsx", "xls", "csv")
AUTO_ATTACH_ACTIONS = ("下载", "查看", "获取", "导出", "保存", "点击", "打开",
                       "领取", "查收")
AUTO_LINE_MAX = 12
# 句读符号 = 说明句而非按钮：按钮文案不会以「。」「！」收尾
_AUTO_STOP_CHARS = "。！？!?"
# 祈使/说明句的常见开头或连接词。真实按钮文案不会写「请…」，
# 但正文里到处是「请立即催签」「如需下载请点击」—— 2026-11-01 自己踩到：
# 「请下载附件查看今天的明细」一度被判成按钮（12 字、名词动作都齐）。
_AUTO_SENTENCE_WORDS = ("请", "如需", "如果", "若", "可以", "需要", "以下", "如下",
                        "详见", "注意", "提醒")


# ── 带「明细」字眼的行（2026-11-01 用户：「只要带明细字眼的都点开转发原文件」）──
#
# 背景：钉钉这些卡片里，「明细」两个字出现在三种完全不同的行上：
#
#     附件：运单明细                         ← 真附件行（老规则已覆盖）
#     查看完整履约失败明细（Excel）           ← 卡片底部按钮（老规则靠「查看完整」）
#     履约失败明细（10票）                    ← **表格标题**，点了什么都不会发生
#     今日巡检发现异常，麻烦你结合下方明细判断一下…   ← 叙述句
#
# 用户要的是"带明细的都点一遍，能下就下"。所以这几行**全部进候选**，但按
# "像不像按钮"排序：真附件行在前、表格标题在后。手机端拿到的是一个候选链，
# 逐个点、**拿到文件就停** —— 标题点空了只是白等几秒，不会把垃圾当成附件。
# 叙述句排除掉：它既不可点、又长，排进去纯属浪费时间。
_MINGXI_WORD = "明细"
# 候选行的长度上限。按钮文案都短；超长的是说明句（真机最长的是 33 字的
# 「今日巡检发现异常，麻烦你结合下方明细判断一下…」）。
_MINGXI_LINE_MAX = 30
# 句读：带逗号/句号的多半是说明句或「（33票，仅展示前30条）」这种统计头。
_MINGXI_STOP_CHARS = "。！？!?，,、；;"
# 祈使/说明句的常见词（与 _AUTO_SENTENCE_WORDS 同一思路，另加「麻烦/结合」）。
_MINGXI_STOP_WORDS = ("请", "麻烦", "如下", "详见", "结合", "需要", "如需", "如果",
                      "若", "注意", "提醒", "以下")
# 动作词：带这些词的「明细」行更像按钮，排前面。
_MINGXI_ACTIONS = ("下载", "查看", "导出", "获取", "打开", "点击", "领取", "查看完整")


def _mingxi_lines(content: str) -> list:
    """正文里**像按钮**的「明细」行（按出现顺序、去重）。"""
    out: list = []
    for ln in (content or "").splitlines():
        s = ln.strip()
        if not s or _MINGXI_WORD not in s or len(s) > _MINGXI_LINE_MAX:
            continue
        if any(c in s for c in _MINGXI_STOP_CHARS):
            continue
        if any(w in s for w in _MINGXI_STOP_WORDS):
            continue
        if s not in out:
            out.append(s)
    return out


def _attach_cands(content: str, keys: list, auto: bool = True,
                  limit: int = 6) -> list:
    """这次抓取要手机**依次尝试**的行（下标 0 最像附件）。

    设计意图：附件行到底长什么样，钉钉每种卡片都不一样，靠规则枚举永远
    会漏一种。与其在电脑侧猜死，不如把「像附件的行」排个序交给手机端：
    点第一行 → 等一小会儿 → 没文件就点下一行 → 拿到文件就收工。这样
    「带明细的表格标题」「查看完整…（Excel）」混在一张卡里也能命中。
    """
    names = _attach_names(content, keys, auto)
    cands: list = []

    def push(n: str) -> None:
        n = (n or "").strip()
        if n and n not in cands:
            cands.append(n)

    # ① 老规则认出来的（真附件行）—— 最可信，排最前。
    for n in names:
        push(n)
    # ② 「明细」行：带动作词的先（「查看完整…」），纯标题的后。
    #    已经是「附件：xxx」形态的别再整行收一遍 —— 「明细：普通件滞留催提
    #    AI代打明细」这种行，名字已经是「普通件滞留催提AI代打明细」，整行
    #    再加进去只会在手机端白点一次同一行。
    def redundant(line: str) -> bool:
        return any(n in line for n in cands)

    ming = _mingxi_lines(content)
    for n in [x for x in ming if any(a in x for a in _MINGXI_ACTIONS)]:
        if not redundant(n):
            push(n)
    for n in ming:
        if not redundant(n):
            push(n)
    return cands[:limit]


def _fetch_keys_for(ctx, name: str) -> str:
    """下发给手机的「找行字眼」：配置里的通用前缀 + **这次命中行自己的名字**。

    手机端找屏上的附件行用的是 ``startsWith(字眼)``，而「未及时转出明细.xlsx」
    这种纯文件名不以任何前缀开头 —— 只发配置字眼的话，电脑这边认出了名字、
    手机那边永远点不到那一行（2026-11-01 17:22 拦截跟单日报实锤：正文转发
    成功、附件永远不来）。把命中名去掉扩展名一起下发，那一行的开头就正好
    对得上，无需为此再发一版 APK。

    ⚠ 2026-11-01 18:0x 补：**没有文档扩展名的名字也要下发**。
    「查看完整履约失败明细（Excel）」这种卡片按钮名字里没有 ``.xlsx``，
    旧实现走到 ``if m:`` 就直接跳过了 —— 万一哪天配置里少了「查看完整」
    这个字眼，手机端就又只能靠 fallback 瞎点一行。名字本身就是屏上文本，
    ``startsWith(名字)`` 必然命中自己，所以无脑补进去最稳。
    """
    keys = list(getattr(ctx, "attach_keys", []) or [])
    stem = (name or "").strip().strip(" :：·—|｜")
    if not stem:
        return ",".join(keys)
    m = _DOC_EXT_RE.search(stem)
    head = (stem[:m.start()] if m else stem).strip(" :：·—|｜")
    # len>=3：太短的（「（Excel）」这种纯格式标记）补进去毫无意义还可能误配。
    if len(head) >= 3 and head not in keys:
        keys.append(head)
    return ",".join(keys)


def _attach_names(content: str, keys: list, auto: bool = True) -> list:
    """把消息正文里**所有**附件行都抠出来（按出现顺序、去重）。

    为什么要「所有」：服务诊断专员的滞留卡片一张里就有 4~5 条，形如

        明细：下载3天滞留运单明细
        明细：下载5天滞留运单明细
        明细：下载7天滞留运单明细
        明细：下载14天滞留运单明细

    旧实现只取第一条（``_attach_name``），于是每张卡只能抓到 1 个文件，
    其余几条永远不会被点 —— 表现出「附件只来了一部分」。
    """
    out: list = []
    lines = [ln.strip() for ln in (content or "").splitlines()]

    # 0) 整行就是文件名（「未及时转出明细.xlsx」）—— 见 _FILE_LINE_RE 注释。
    #    放最前面：这种行不需要猜，它本身就是附件按钮上写着的字。
    for ln in lines:
        if ln and _FILE_LINE_RE.match(ln) and ln not in out:
            out.append(ln)
    if out:
        return out

    for m in _attach_re(_clean_attach_keys(keys)).finditer(content or ""):
        name = m.group(1).strip()
        # 见 _FORMAT_ONLY_RE / _FORMAT_IN_NAME_RE 注释：键吃掉了按钮文案的前缀时，
        # 名字要退回**整行原文**，否则「查看完整履约失败明细（Excel）」会变成
        # 「履约失败明细（Excel）」—— 屏上根本没有以它开头的一行。
        if _FORMAT_ONLY_RE.match(name) or _FORMAT_IN_NAME_RE.search(name):
            name = m.group(0).strip()
        if name and name not in out:
            out.append(name)
    if out:
        return out
    # 卡片按钮有时不带格式后缀，整行就是短语本身（「查看完整月报明细」）。
    # 只认 ≥4 字的键按整行全等命中 —— 「附件：」这种标签不算按钮。
    for k in _clean_attach_keys(keys):
        if len(k) >= 4 and k in lines and k not in out:
            out.append(k)
    if out:
        return out
    # 3) 兜底：按钮文案被 OCR/卡片拼接**夹在长行中间**。
    #
    # 2026-11-01 12:08 实锤的一条积压异常提醒：
    #
    #     @所有人积压异常提醒: 下载附件 ｜ 承包区编码: 311242 ｜ 承包区名称: 浙江萧山菜鸟服务点 ｜ …
    #
    # 行首匹配（`_attach_re`）要求行以字眼开头 —— 这行以「@所有人积压异常提醒:」
    # 开头；整行全等更不中。于是「下载附件」这个按钮**从来没下过单**，用户在群里
    # 看到的是正文转发成功、附件永远不来。
    #
    # 判据刻意收得很窄，避免把叙述句误当按钮：
    #   · 键必须 ≥4 字（与整行规则同一门槛，「附件：」这种标签不参与）；
    #   · 键的**左右都必须是分隔符**（行首/行尾/空白/｜/标点）。这样
    #     「请下载附件查看」不命中（左「请」右「查」都是汉字），
    #     「下载附件 ｜」命中（右边是分隔符）。
    #   · 命中的名字就是**键本身**：屏幕上可点的就是这几个字，手机端
    #     按 contains 找得到。
    for k in _clean_attach_keys(keys):
        if len(k) < 4 or k in out:
            continue
        pat = re.compile(r"(?:^|[%s])(%s)(?=$|[%s])"
                         % (_INLINE_SEP_CLS, re.escape(k), _INLINE_SEP_CLS),
                         re.M)
        if pat.search(content or ""):
            out.append(k)
    if out or not auto:
        return out
    # 4) 万能字眼：短行里同时出现「名词 + 动作」就当附件按钮（规则见常量注释）。
    #    这一步在最后：前面任何一条命中了就不再往下猜，避免把真名字换成一整行。
    for ln in lines:
        if not (2 <= len(ln) <= AUTO_LINE_MAX):
            continue
        if any(c in ln for c in _AUTO_STOP_CHARS):
            continue
        if any(w in ln for w in _AUTO_SENTENCE_WORDS):
            continue
        low = ln.lower()
        if not any(n in low for n in AUTO_ATTACH_NOUNS):
            continue
        if not any(a in ln for a in AUTO_ATTACH_ACTIONS):
            continue
        name = ln.strip(" :：·—|｜")
        if name and name not in out:
            out.append(name)
    if out or not auto:
        return out
    # 5) 用户 2026-11-01：「只要带明细字眼的都点开转发原文件」。
    #    走到这里说明前面所有规则都没命中 —— 而正文里明明有「明细」。
    #    典型是「📦 猫超件明细（12 票）」这种把按钮写成标题形态的卡片：
    #    它既不是「附件：xxx」，也没有下载/查看这类动作词，老规则全放过了，
    #    结果整张卡的附件从来没下过单。既然用户要求"带明细就点"，就把它
    #    当最后一档候选收进来；点空了由手机端的候选链兜着（见 _attach_cands）。
    for ln in _mingxi_lines(content):
        if ln not in out:
            out.append(ln)
    return out


def _attach_name(content: str, keys: list, auto: bool = True) -> str:
    """从消息正文里抠**第一个**附件名；没有附件行返回空串。

    两种命中形态（真机都见过）：
      · 「附件：运单明细」 → 名字取键后的真名字「运单明细」；
      · 「查看完整月报明细（Excel）」 → 键后只剩格式标记，名字取**整行**。
    """
    names = _attach_names(content, keys, auto)
    return names[0] if names else ""


def _remember_attach_name(ctx: "_Ctx", name: str, now: float | None = None) -> None:
    """把卡片上看到的附件行名字记进 `attach_recent`（同名只刷新时间）。"""
    if not name:
        return
    recent = getattr(ctx, "attach_recent", None)
    if recent is None:                 # 老 ctx（测试里手搓的）没有这份记忆
        return
    now = time.monotonic() if now is None else now
    for i, (n, _ts) in enumerate(recent):
        if n == name:
            recent[i] = (name, now)
            return
    recent.append((name, now))


def _display_name_for_file(ctx: "_Ctx", phone_name: str) -> str:
    """文件传回来时该叫什么名字（空 = 没把握，让下游按手机原名整理）。

    优先「正在抓的那一条」（``attach_active``，自动抓取时一定有）；用户**手动
    点**链接时没有 active 单，就按扩展名在最近见过的附件行里找 —— 但**只有
    唯一候选**才采用：名字和内容对不上比英文名更误导人（2026-11-01 判断）。
    """
    active = (getattr(ctx, "attach_active", "") or "").strip()
    if active:
        return active
    ext = os.path.splitext(phone_name or "")[1].lower()
    now = time.monotonic()
    cands = {n for n, ts in getattr(ctx, "attach_recent", [])
             if now - ts <= ATTACH_NAME_WINDOW
             and os.path.splitext(n)[1].lower() in ("", ext)}
    return cands.pop() if len(cands) == 1 else ""


def _cb_wants_wanted(cb) -> bool:
    """下游回调吃不吃第 4 个参数（wanted 名字）。老接线/老测试只有 3 个。"""
    try:
        import inspect

        params = [p for p in inspect.signature(cb).parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        return len(params) >= 4
    except (TypeError, ValueError):
        return False


def _cb_wants_src(cb) -> bool:
    """下游回调吃不吃第 5 个参数（源消息时间 msg_time）。

    跟 ``_cb_wants_wanted`` 同一套兼容思路：``handle_attachment`` 加了
    ``msg_time`` 之后老接线（3/4 参）还得能跑，新接线才拿到时间。
    """
    try:
        import inspect

        params = [p for p in inspect.signature(cb).parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        return len(params) >= 5
    except (TypeError, ValueError):
        return False


def _take_attach_src_time(ctx: "_Ctx", *names) -> "datetime | None":
    """附件对应的**源消息**时间；判断不出返回 None。

    为什么需要：附件回到电脑侧时是一条凭 sha1 拼出来的 Message，``msg_time``
    天生为空 —— 而「积压超龄只入库不转发」按 msg_time 算年龄。不带上源时间
    的话年龄永远是 0，两小时前那张卡片的明细照样会刷进群。

    键可能对不上（候选链里手机点的是另一行），所以按 ``names`` 依次找，
    找到就把所有候选键一起清掉 —— 不清会出**隔天误拦**：同一个附件名明天
    还会出现，残留的旧时间条会让新附件被算成「一天前」。

    三个源都取不到时间、或时间条读不出绝对时刻（「星期一」「昨天」裸的）
    一律返回 None = 不拦。这套规则是挡陈年积压，不是做时间校验，宁可漏一条。
    """
    ts = ""
    hit = ""
    for key in names:
        if key and key in ctx.attach_src_ts:
            ts = ctx.attach_src_ts.get(key) or ""
            hit = key
            break
    for key in names:                     # 顺手清干净，避免隔天误判
        if key:
            ctx.attach_src_ts.pop(key, None)
    # 人工补抓（/attach-now）是人主动要的补发，不受积压闸门限制。
    manual = getattr(ctx, "attach_manual", None)
    if manual:
        for key in names:
            if key and key in manual:
                manual.discard(key)
                log.info("附件「%s」是人工补抓，跳过积压年龄判定", key)
                return None
    if not ts:
        return None
    try:
        from .android_parse import unit_age_seconds
    except ImportError:                   # 极端情况（拆分导入期）
        return None
    age = unit_age_seconds(ts)
    if age is None:
        return None
    return datetime.now() - timedelta(seconds=age)


def _attach_timeout_check(ctx: "_Ctx") -> None:
    """附件下单后迟迟没收到文件：告警一次并释放冷却。

    背景（真机 2026-10-27 17:57）：「明细附件：客户声音待履约汇总….xlsx」
    下发抓取指令后手机端一直没回传，PC 日志只有「发现附件」一行，之后
    什么都没有 —— 冷却还占着，就算卡片再渲染也不会重新下单。这里把
    超时的单撤掉（卡片再次出现 = 立刻重新下单）并打出 warning，让
    「抓了但没抓到」在日志里看得见。正常链路 8~12 秒，硬上限约 40 秒，
    120 秒（ATTACH_FETCH_TIMEOUT）还没文件就认定失败。
    """
    now = time.monotonic()
    for name, at in list(ctx.attach_orders.items()):
        if now - at < ATTACH_FETCH_TIMEOUT:
            continue
        # ⚠ 必须 pop 而不是 del：本函数每帧都被调用（见 _post_screen 末尾），
        #   而帧请求由 ThreadingHTTPServer 并发处理 —— 两个线程拿到同一份
        #   list() 快照时，后到的那个 del 会抛 KeyError，把整帧处理打成 500
        #   （手机据此重发，反而让并发更凶）。已被别人删掉就安静跳过。
        if ctx.attach_orders.pop(name, None) is None:
            continue
        ctx.attach_fail_at[name] = now        # 退避期内不许自动重派
        if getattr(ctx, "attach_active", "") == name:
            # 这条彻底失败了也得放行，否则队列里后面的明细永远轮不到。
            ctx.attach_active = ""
        if ctx.attach_file_at and ctx.attach_file_at > at:
            continue            # 期间收到过文件：链路正常，冷却自然结束
        log.warning("附件抓取超时：下单「%s」已 %.0f 秒没收到文件"
                    "（手机端最近结论：%s）—— 已释放冷却，"
                    "卡片再次出现会重新下单",
                    name, now - at,
                    ctx.last_attach or "手机从未回报过任何结论")

    # ⚠ 2026-11-01 实锤的另一种卡法：``attach_active`` 与 ``attach_orders``
    # **脱钩** —— active 里的名字在 orders 里没有键（``/attach-now`` 清冷却
    # 后塞队列、或上一轮已被删），上面的循环遍历的是 orders，根本轮不到它，
    # 于是 active 永远挂着：队列进不来、重试报「正在抓」。这里对 active
    # 单独兜底：只要它超过超时（或 orders 里没有它的下单时刻），一律放行。
    active = getattr(ctx, "attach_active", "")
    if active:
        at = ctx.attach_orders.get(active)
        if at is None or now - at >= ATTACH_FETCH_TIMEOUT:
            ctx.attach_orders.pop(active, None)
            ctx.attach_fail_at[active] = now      # 同上：退避，别立刻重派
            ctx.attach_active = ""
            log.warning("附件抓取超时（active 兜底）：「%s」没有在 %d 秒内"
                        "拿到文件 —— 已释放，可重新下单", active,
                        int(ATTACH_FETCH_TIMEOUT))


def _attach_pending(ctx: "_Ctx") -> dict:
    """还没拿到文件的附件订单：名字 → 已等秒数（排查用）。

    2026-10-31 那天日志里只有「发现附件」一行，手机端一个字都没回，分不清
    是「没收到指令」「收到了没点」「点了没下载」还是「下载了没传回来」。
    把这个字段挂到 /ping 上，订单挂了多久一眼可见。
    """
    now = time.monotonic()
    return {n: round(now - at, 1) for n, at in ctx.attach_orders.items()}


def _order_attach(ctx: "_Ctx", pending: list) -> str:
    """扫描本帧新单元里的附件行，返回**这一次**要下发的附件名（没有则空串）。

    一帧可能扫出好几个名字（一张滞留卡片就有 4~5 条「明细：…」），手机
    同一时刻只能抓一个，所以先进队列 ``ctx.attach_queue``，一次只放一个
    出去；上一个抓到文件（``/file`` 到达）或超时释放之后，下一帧再取下一个。

    冷却记在 ``ctx.attach_orders``（名字 → monotonic）：卡片重渲染会让
    同一条附件行在几十帧里反复出现，冷却期内不重复入队。

    总开关 ``ctx.attach_enabled`` 也在这里判 —— 关掉时连冷却都不记，
    重新打开后卡片再出现能立刻抓，不必等 10 分钟冷却过去。
    """
    if not getattr(ctx, "attach_enabled", True):
        return ""
    _attach_timeout_check(ctx)
    now = time.monotonic()

    # 1) 先把本帧看到的附件行全部收进队列（已在抓/冷却中的跳过）
    auto = bool(getattr(ctx, "attach_auto", True))
    for u in pending:
        content = getattr(u, "content", "")
        names = _attach_names(content, ctx.attach_keys, auto)
        for name in names:
            _remember_attach_name(ctx, name, now)
            if name == ctx.attach_active or name in ctx.attach_queue:
                continue
            if now - ctx.attach_orders.get(name, 0.0) < ATTACH_COOLDOWN:
                continue
            # 上一趟刚超时：退避期别自动重派，否则卡片在屏上就是无限循环
            if now - getattr(ctx, "attach_fail_at", {}).get(name, 0.0) < ATTACH_FAIL_BACKOFF:
                continue
            ctx.attach_queue.append(name)
            # 记下这条消息的时间条：附件回到电脑侧要靠它算「多少分钟前」，
            # 否则附件永远算 0 分钟、拦不住积压（见 attach_src_ts 的说明）。
            ctx.attach_src_ts.setdefault(name, getattr(u, "ts", "") or "")
            # 候选链（含「明细」行）随队列一起记着，下发时带给手机。
            cands = _attach_cands(content, ctx.attach_keys, auto)
            if len(getattr(ctx, "attach_cands", {}) or {}) > 32:
                ctx.attach_cands.clear()          # 只在异常长跑时才涨，保险起见
            ctx.attach_cands[name] = cands or [name]

    # 2) 上一条还没结束就不发新的：手机端同一时刻只跑一个抓取任务，
    #    并发下发会被它当场丢掉，那条订单就白白进了冷却（=永远抓不到）。
    if ctx.attach_active:
        return ""
    if not ctx.attach_queue:
        return ""
    name = ctx.attach_queue.pop(0)
    ctx.attach_orders[name] = now
    ctx.attach_active = name
    return name


@dataclass
class _Ctx:
    token: str
    chat_name: str
    # 网关实际监听的端口，`/app` 下载页要把这个数字印给用户看。
    # 不能写死 8765：用户在设置里改过端口时，页面照着旧值显示，
    # 手机上填了就是连不上，而且他没法从 App 里看出错在哪。
    port: int = DEFAULT_PORT
    # 最近投递过的消息 → (归一化发送者, 正文, 投递时刻)。用来拦「同一条卡片
    # 换了渲染形态又被当成新消息」的重复转发：横排表格和一格一行的指纹不同、
    # uid 不同，is_duplicate 挡不住，只能按**内容片段重合度**在时间窗内再挡一次。
    recent_keys: dict = field(default_factory=dict)
    # ── 锚点丢失时「翻一屏试探」的状态 ──────────────────────────────
    # 锚点丢失目前是静默重建基线 + 这一屏不发。但真机上它有两个常见原因：
    #   · 卡片重绘后锚点那条换了形态（已由 _locate 的近似匹配覆盖）；
    #   · 新消息把锚点整条挤出了屏幕（近似匹配也救不了 —— 屏上根本没有它）。
    # 后者只要手机往上翻一帧就能重新看到锚点，把中间那段补回来。所以丢失时
    # 不直接放弃，先让手机翻一屏再报一次；align_md5 记「为哪一帧翻的」，
    # 同一帧只试探一次，align_at 记上次试探时刻（冷却期内不再发指令 ——
    # 否则会变成翻上去、翻回来、又翻上去的来回抖）。
    align_md5: str = ""
    align_tried: int = 0
    align_at: float = 0.0
    # 第一次「等手机翻页安定」的时刻；0 = 当前不在等待中。
    # 有了它，一边翻一边来的中间态帧只会消耗「等待时间」而不会消耗试探次数。
    align_wait_since: float = 0.0
    # 上一轮试探帧的**内容签名**（发送者+正文）。判「手机到底翻没翻动」只能
    # 看它：md5 里混着坐标与 H5 重绘痕迹，同一屏也能每帧都不一样（2026-10-29
    # 真机同一屏 40 帧、md5 每帧都变 → 试探永不收敛）。
    align_sig: str = ""
    # 连续多少轮拿到的内容一模一样（= 翻不动）。
    align_stuck: int = 0
    # 本帧要下发的「累积式上翻」轮次（0 = 不下发）。
    # 与 scroll 刻意分开：scroll 是长消息补全（上翻一屏**再翻回底部**，位置
    # 不累积）；probe 是锚点试探（上翻一屏**停在原地**，下一轮接着往上，位置
    # 累积）。混用一条指令就是 2026-10-29 那个「来回跳、永远找不回」的根源。
    want_probe: int = 0
    # 要下发「回到底部（最新）」。试探认输后先把视口开回最新处再重建基线，
    # 否则基线建在历史中间，下面到达的新消息一条都看不见。
    want_bottom: bool = False
    # 已经请求过回底，等手机滚到底送来的那一帧：直接重建基线，不再试探。
    await_bottom: bool = False
    # 请求回底的时刻（monotonic）。手机没手势权限时回不来，靠它兜底就地重建。
    await_bottom_at: float = 0.0
    # 本帧的上报原因（X-Ddwx-Reason）。认「回底那一帧」（probe-bottom）只能靠它：
    # 回底后的画面和历史中间那一屏在节点树里长得一样，没法从内容上分辨。
    frame_reason: str = ""
    # 最近一帧「锚点健康」的画面单元（普通帧 / 基线帧 commit 后更新；
    # 补全帧不动它 —— 那是滚到半空的一帧，不代表会话底部）。
    # 基线重建前的打捞（_salvage_rebuild）拿它当参照系：当前帧与它有
    # 重叠 = 还是同一个会话，重叠区之下才是真新消息。
    last_good_units: list = field(default_factory=list)
    # 已投递内容的滚动记录（(norm_sender, 正文)）。打捞用它挡「已经发过的
    # 又捞一遍」；比 recent_keys 的 NEAR_DUP_WINDOW 长得多 —— 用户翻回早前
    # 的历史时，那些消息早出了近似去重窗口，只能靠这份记录挡。
    delivered_texts: deque = field(default_factory=lambda: deque(maxlen=512))
    # 持久去重回调（storage.is_duplicate，uid → bool）。基线打捞（_sweep_recent）
    # 靠它判断「这条入库过没」—— 内存里的 delivered_texts 重启就空了，而打捞
    # 恰恰要覆盖「程序重启后的第一屏」。uid = "android:{fp}:{seq}"，与正常
    # 投递路径同一套指纹，重启后依然稳定可复现。
    dup_check = None
    # ── 附件抓取（「附件：运单明细」→ 手机点击下载 → 传回电脑）──────
    # 电脑侧每帧扫描新消息里的附件行，冷却期内只下一次单（ATTACH_COOLDOWN）；
    # 同一份文件不重发靠 attach_seen（sha1），不是靠这里的名字冷却 ——
    # 名字冷却挡「这一分钟内别再点」，sha1 挡「这份文件别再发」。
    attach_orders: dict = field(default_factory=dict)     # 附件名 -> monotonic
    attach_seen: dict = field(default_factory=dict)       # sha1 -> True
    # 最近在卡片里见过的附件行名字 (name, monotonic)，新的在后（最多 16 条）。
    # 只服务于「文件传回来时叫什么」—— 见 `_display_name_for_file`。
    attach_recent: deque = field(default_factory=lambda: deque(maxlen=16))
    # 最近一次收到附件文件的时刻（monotonic）。附件超时告警（_attach_timeout_
    # check）用它区分「已收到，冷却正常结束」和「超时没收到，要告警放行」。
    attach_file_at: float = 0.0
    # 正在抓的那个附件名（空 = 当前没有在跑）。手机同一时刻只抓一个，队列
    # 里的下一个要等它结束才放行 —— 见 `_order_attach`。
    attach_active: str = ""
    # 排队等着抓的附件名（一张滞留卡就有 4~5 条「明细：…」）。
    attach_queue: list = field(default_factory=list)
    # 附件名 → 「这次要手机依次尝试的行」（见 `_attach_cands`）。
    # 用户 2026-11-01：「只要带明细字眼的都点开转发原文件」—— 一张卡里既有
    # 「查看完整…（Excel）」按钮、又有「履约失败明细（10票）」这种标题时，
    # 电脑侧排不出绝对正确的顺序，干脆把候选链下发给手机挨个试。
    attach_cands: dict = field(default_factory=dict)
    # 附件行所在**消息的时间条原文**（附件名 -> "今天 08:13"）。
    #
    # 存在的理由：附件回到电脑侧时是一条凭 sha1 拼出来的 Message，`msg_time`
    # 天生为空 —— 而「积压超龄只入库不转发」正是按 msg_time 算年龄的。不带上
    # 源时间的话年龄永远是 0，两小时前那张卡片的明细照样会刷进微信群
    # （2026-11-01 20:4x 查出来：正文拦得住，附件这条路漏）。
    #
    # 用 setdefault 保第一次：卡片重渲染时这条消息会在几十帧里反复出现，
    # 第一次看到的那个时间条才最接近消息真实时刻。
    attach_src_ts: dict = field(default_factory=dict)
    # 人工 /attach-now 点过的名字。这些是**人主动要的补发**，不受 30 分钟
    # 积压闸门限制（跟 forward_recent.py 的 force 一个道理：那时候"发旧的"
    # 正是用户要的）。用完即弃，避免这套豁免漏到同名自动单上。
    attach_manual: set = field(default_factory=set)
    # 失败退避：附件名 -> 最近一次超时的 monotonic。超时释放冷却后卡片还在
    # 屏上，下一帧会立刻重新下单 —— 2026-11-01 14:14~14:18 就这么连着重派了
    # 三次，每次 20 秒、来回跳 UC，日志刷满还干扰了用户。退避期内不许自动
    # 重派（手动 /attach-now 不受限）。
    attach_fail_at: dict = field(default_factory=dict)
    # 触发字眼（dingtalk.attach_keys）：下单时随 fetch_attach 一起下发给手机，
    # 手机端按同样的字眼找屏上的附件行 —— 两边必须一致，改配置两端同时生效。
    attach_keys: list = field(
        default_factory=lambda: list(DEFAULT_ATTACH_KEYS))
    # 总开关（dingtalk.attach_enabled）：关掉就完全不下发抓取指令。
    attach_enabled: bool = True
    # 万能字眼（dingtalk.attach_auto）：短行里「名词+动作」都算附件按钮。
    attach_auto: bool = True
    # 附件落盘目录与下游回调（service.handle_attachment）。空 = 不收文件。
    files_dir: str = ""
    on_file = None
    # give_up 是否因「试探状态过期」（ALIGN_STALE）触发。只有过期触发的
    # 重建才允许打捞 —— 活跃浏览（翻历史 / 换群）时的重建绝不能捞，否则
    # 用户翻哪屏就把哪屏灌进微信群。
    align_stale_gave_up: bool = False
    # 回底彻底没等到帧（手机没手势权限 / 卡死）时的「就地建基线」放行牌。
    # 没有它就是死循环：_probe_decision 认输前 _reset_probe() 会把
    # await_bottom 清掉，紧接着 _guard_fake_bottom 又看到屏上 99+ 徽标
    # → 再请回底 → 基线永远建不上，_sweep_recent 也永远轮不到跑。
    # 2026-10-31 09:31 真机：57 秒一轮，连着两轮都被自己打回。
    # 一次性用掉：下一次锚点丢失照常重新试探 / 回底。
    bottom_gave_up: bool = False
    # ── 「先攒后发」缓冲 ───────────────────────────────────────────
    # 钉钉机器人的 H5 卡片是分批渲染的，这一帧看到的往往只是半成品。
    # 早期用「扣住等重报帧指纹一致」，但 H5 卡片每重绘一次整批指纹都变，
    # 于是永远等不到第二眼（2026-10-18 全天：扣住 14 次、成功 1 次）。
    # 现在改成按 (发送者, 时间窗) 归并、保留最长版本、静默后投递。
    # 详见 bridge/sources/pending.py 的模块注释。
    pending: object | None = None
    # ── 长消息「攒齐再发」的状态（见 _hold_long / _ingest）─────────────
    # 一条超过一屏的消息，第一次出现时树里只有屏幕上那截。旧做法是「先发半截，
    # 再请手机翻一屏补全」—— 补全回来的前半截因为指纹不同（片段 ≠ 全文），
    # 又作为**一条新消息**发出去，群里收到的是一条残句 + 一条完整消息。
    # 用户的原话是「内容特别长的消息转发还是有问题」。
    #
    # 改成「攒」：第一次见长消息不发，只请手机翻一屏；等补全帧回来，
    # 把「上半截 + 下半截」拼成一条再投。long_tail 存第一次见到的那半截。
    #
    # 2026-10-18：从「一个 dict」改成「一个**整体状态**」——
    #   · rounds  已经补了几轮（每次翻一屏算一轮，上限 FILL_MAX_ROUNDS）；
    #   · head    目前拼出来的全文（每轮归并后更新，不丢中间段）；
    #   · tail    最初见到的那半截（屏幕上最下面那段），每轮都要重新和它
    #             对位置 —— 因为全文里还没盖到它也说不定；
    #   · top_seen 上一轮帧里有没有「顶边被切掉」的单元。还在切 = 上面还有
    #             内容 → 继续翻。这一条是**唯一**的停手判据。
    #
    # 为什么必须多轮：用户原话「1.6 屏不够，要 3 屏才够」。一次只翻 62% 屏高，
    # 3 屏的正文要翻四五轮才露全，而旧结构（一个只有两个槽的 dict）只有
    # 「上半截 + 下半截」两个槽，第三屏中间那段根本没有地方放。
    long_key: str = ""
    long_at: float = 0.0
    long_rounds: int = 0
    long_head: str = ""                     # 全文（按每轮目击顺序对出来的）
    long_order: str = "down"                # "down" = 后写的在下；"up" = 在上
    long_tail: str = ""                     # 最初见到的那半截
    long_sender: str = ""
    long_kind: str = ""
    long_top_seen: bool = False
    long_exhausted: bool = False            # 已经翻到顶 / 翻不动了
    # 每轮补全里「按发送者归并后的最佳版本」。不放进 long_head 直接拼 ——
    # 手机每轮只多露 0.6 屏，同一段正文会在相邻两轮里各出现一次，必须先
    # 按内容重叠把它们对齐，再决定新露出来的是「上面的一段」还是「下面的一段」。
    long_frags: list = field(default_factory=list)
    long_blocked: set = field(default_factory=set)
    # 这条长消息是**建基线那一帧**发现的。
    #
    # 建基线时的语义是「屏上已有的都算旧内容，不转发」。但屏上那条如果被切掉
    # 后半截，用户看到的就是半截 —— 补全是必须的。矛盾在于：补全完的那份到底
    # 该不该发？**不能发** —— 它在基线里，多半几分钟前就转发过了（真机
    # 2026-10-18：16:39 已发过完整版 829 字，20:10 建基线时又被翻出来）。
    # 盲发就是重复刷群。
    #
    # 所以这个标志的意思是：**照常补全，但收尾时只入库、不投递**。补来的
    # 完整正文会落库，事后用 forward_recent.py 能查、能人工补发；
    # 而「把历史消息重复甩进业务群」这种事故一次都不该有。
    long_baseline: bool = False
    companies: list[str] = field(default_factory=list)
    on_message: MessageHandler | None = None
    count: dict = field(default_factory=lambda: {"requests": 0, "accepted": 0,
                                                 "rejected": 0})
    # 最新一帧节点树。只留最新一份：这一层的语义就是「手机现在长什么样」，
    # 历史帧由解析器自己按需比对，网关不做序列缓存。
    screen: dict | None = None
    # 「电脑想要一帧」的请求编号（墙上时间秒，0 = 还没要过）与上次请求时刻。
    # 手机端拿它跟自己的记录比，比它大就说明是**新**请求 —— 于是清掉画面指纹、
    # 重发一次当前画面（见 WANT_FRAME_MIN_GAP 那段注释）。
    frame_req: int = 0
    frame_req_at: float = 0.0
    frame_log_at: float = 0.0
    # 帧解析（dingtalk.android_parse 才开启）。differ 有状态 —— 它记着上一帧的
    # 锚点，用来判「哪几条是新的」，所以必须跟 ctx 一起走、由同一把锁保护。
    parse: bool = False
    differ: FrameDiffer | None = None
    # 失联判定阈值（秒），0 = 不检测。只用于 /ping 的回包与看门狗。
    offline_after: float = 300.0
    # 手机自动巡检：盯哪个群、几十分钟查一次（0 = 关）。
    # 每一帧的响应都带上，手机端拿它重排自己的巡检定时器。
    # 为什么由电脑端下发：目标群就是 dingtalk.chat_names 的第一个，
    # 手机再单独填一遍必然出现「两边不一致」，而不一致的后果是每轮都点错群。
    watch_group: str = ""
    watch_min: int = 0
    # 让路截止时刻（time.monotonic，0 = 没让路）。给这台机器上**另一个**
    # 操作同一部手机的项目腾前台用：对方点「确定/上传」的那几秒，如果我们
    # 恰好巡检，会把钉钉拉到前台，对方的点击就落空了（2026-10-20 实证）。
    # 用单调时钟而不是墙上时间：用户改系统时间/夏令时不会让它提前或永不结束。
    watch_pause_until: float = 0.0
    # 「立刻切回目标群」的一次性指令（电脑端按钮点的）。用递增序号而不是布尔：
    # 用户可能连点两下，第二下也必须生效；发完前心跳就已经取走时序号不会丢。
    # 手机在 /ping 的响应里读 watch_now=1 就当场跑一轮巡检。
    watch_now_seq: int = 0
    watch_now_served: int = 0
    # 上一次「不是聊天页，本帧跳过」的说明。用户点开 @ 面板会连着报好几帧，
    # 内容一样就没必要重复写日志（日志是滚动文件，刷满了会盖掉真问题）。
    last_skip_note: str = ""
    # 连续多少帧认不出聊天页。只数「认不出来」，明确的 @ 面板/群信息不计。
    skip_streak: int = 0
    # 上一次「认不出聊天页 → 自动下发切回指令」的时刻（time.monotonic，
    # 0 = 从未）。冷却见 UNKNOWN_PAGE_AUTO_WATCH_COOLDOWN_S。
    last_auto_watch_at: float = 0.0
    # ---- 在线状态（失联告警用）----
    # 最后一次收到手机上报的时刻（time.monotonic）。0 = 至今一次都没收到。
    last_seen: float = 0.0
    last_seen_wall: str = ""              # 同一时刻的人话版本，写进告警文案
    last_device: str = ""                 # 手机型号，告警时好认出是哪台
    # 手机上报的 App 版本（X-Ddwx-Ver，APK 0.4.1 起带）。空 = 旧版没这个头。
    # 存在的理由很具体：同一个机型的 device 名永远不变，分不出 0.3.0 和 0.4.0，
    # 而「到底装上没装上带新功能的那版」是每次排查的第一个问题。
    last_ver: str = ""
    # 手机端自动巡检的最近一条结果与累计轮数（X-Ddwx-Watch / X-Ddwx-Watch-Runs）。
    # 存在的理由和 last_ver 一样：这几个值原来只活在手机内存里，电脑侧完全
    # 看不到 —— 「巡检到底跑没跑、结果是什么」只能靠人拿手机看设置页。
    # watch_runs 尤其关键：它不涨 = 定时器没在转（APK 没装对/服务没连上）。
    last_watch: str = ""
    last_watch_runs: int = 0
    # 「心跳里带没带巡检状态」的最后一个证据。
    #
    # 这不是可有可无的细节，而是 2026-10-20 卡了一整轮的那个问题：`/ping` 里
    # 根本没有 watch 字段时，有两种完全不同的原因 ——
    #   ① 手机端**没发**这两个头（老包/发头抛了异常）；
    #   ② 手机端发了，但**巡检一次都没跑**（那也应该显示 "(还没巡检过)" 才对）。
    # 光看「字段缺失」分不开这两者，只能靠人拿手机看设置页。记下最后一次心跳
    # 到底带没带，就能把方向一次分开：带了 = 问题在巡检调度；没带 = 问题在上报。
    #
    # 取值："never"（一次都没收到心跳）/ "yes" / "no"
    watch_header: str = "never"
    # 最近一次巡检**失败**时的现场快照（X-Ddwx-Probe）：前台包名、根节点
    # 能不能读到、标题读成什么、顶栏的 viewId。存在的理由很实际 ——
    # 失败原因常常是同一句话（「读不到钉钉标题栏」），而它背后至少有三种
    # 完全不同的情况：钉钉被拉到桌面前、停在某个没标题的页、根本不在钉钉里。
    # 华为屏蔽第三方 logcat，没有这一行，人不在手机边上就只能靠猜。
    last_probe: str = ""
    # 手机端附件抓取的最近一句结论（X-Ddwx-Attach，APK 0.5.17 起带）。
    #
    # 为什么必须上报：2026-10-27 19:52 电脑侧「发现附件」下单成功，之后手机端
    # 一个字都没有 —— 抓失败的原因（屏上找不到附件行 / 点了没下载 / UC 卡住）
    # 全躺在手机 logcat 里，而华为屏蔽第三方 logcat。用户只看到「没转发」，
    # 电脑侧只看到「发现附件」，中间那一段完全靠猜。现在失败现场随心跳回传。
    last_attach: str = ""
    # 是否收到过**心跳**。判定失联必须有它：手机端只在画面变化时上报，
    # 群里半天没人说话时它本来就不发东西 —— 按「多久没收到上报」判会天天误报。
    heard_heartbeat: bool = False
    # waiting（启动后还没收到）/ online / offline / missing（一直没收到）
    state: str = "waiting"
    last_alert_at: float = 0.0            # 上次告警时刻，用来控制重复提醒的节奏
    # 掉线那一刻「最后一次心跳」的时间。恢复时要用它算「断了多久」——
    # 不能用恢复后的 gap，那时 last_seen 刚被心跳刷新，算出来永远是 0 秒，
    # 日志会变成「断开了约 0 秒」，正好把最想看的信息抹掉。
    offline_last_seen: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)
    # 在线状态用**单独的锁**。绝不能复用上面那把：`_post_screen` 是拿着 lock
    # 再去调 touch 的，同一把不可重入的 Lock 会当场死锁 —— 表现是「收到画面后
    # 网关整个卡住，手机上报超时」，而日志里什么都没有。
    seen_lock: threading.Lock = field(default_factory=threading.Lock)

    def touch(self, device: str = "", heartbeat: bool = False,
              ver: str = "", watch: str = None, watch_runs: int = -1,
              probe: str = None, attach: str = None) -> None:
        """记一次「手机还活着」。心跳与业务上报都算。

        这里**只更新时间戳、不改状态**：状态机和它的日志全交给看门狗一处做，
        否则「恢复」的日志会出现在请求线程里、与看门狗的判定互相打架，
        现场就成了「一会儿说恢复、一会儿说失联」。
        """
        with self.seen_lock:
            self.last_seen = time.monotonic()
            self.last_seen_wall = time.strftime("%H:%M:%S")
            if device:
                self.last_device = device
            ver = (ver or "").strip()
            if ver:
                self.last_ver = ver
            if heartbeat:
                self.heard_heartbeat = True
            # 巡检状态用 None 作「本次没带」的哨兵：空串是**有意义**的
            # （手机刚启动、还没巡检过），不能拿它当「没带」把旧值擦掉。
            if watch is not None:
                self.last_watch = watch
            if watch_runs >= 0:
                self.last_watch_runs = watch_runs
            if probe is not None:
                self.last_probe = probe
            if attach is not None:
                self.last_attach = attach
            # 只在心跳里记（业务上报不带这两个头，记它会把证据冲成 "no"）
            if heartbeat:
                self.watch_header = "yes" if watch is not None else "no"


class _Handler(BaseHTTPRequestHandler):
    """只实现两个端点。

    刻意用 HTTP/1.0（不开 keep-alive）：消息频率很低，每请求一个连接的开销
    可以忽略，换来的是「不会因为漏写 Content-Length 而挂住」。
    """

    server_version = "ddwx-android-gateway/1.0"
    ctx: _Ctx = None                     # 由 _make_handler 注入

    # --- 基础设施 ---

    def log_message(self, fmt: str, *args) -> None:
        log.debug("网关 %s %s", self.address_string(), fmt % args)

    def _reply(self, code: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass                          # 手机提前断开，不值得记一条错误

    def _drain(self, length: int, cap: int = 8 * 1024 * 1024) -> None:
        """拒绝之前先把请求体读掉（最多读完 ``cap`` 字节）。

        不读完就回 413，客户端还在往里写，socket 会被 RST —— 客户端看到的不是
        「请求体过大」而是一句「连接被重置」，人就不知道到底哪儿错了。
        """
        remaining = min(max(0, length), cap)
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    return
                remaining -= len(chunk)
        except (OSError, ConnectionError) as exc:
            # 读请求体时对方已经走了（WinError 10054 就是这条路径进来的）。
            # 这里**只该记一条 debug**：用户的日志窗口里出现一句
            # 「ConnectionResetError: [WinError 10054] 远程主机强迫关闭了一个
            # 现有的连接」是没有任何可操作信息的，只会让人以为程序坏了。
            # 真正断了会由失联告警（按心跳判）报出来。
            log.debug("读请求体时对方断开（%s 字节未读）：%s", remaining, exc)
            self.close_connection = True

    def _path(self) -> str:
        p = urlparse(self.path).path
        return p.rstrip("/") or "/"

    def _apply_watch_pause(self, seconds: int) -> str:
        """让路：一段时间内不自动巡检，返回一句给人看的话。

        为什么需要它（2026-10-20 线上事故）：这台机器上还有另一个项目
        （运单揽件助手）在用 adb 操作同一台手机的申行者。我们的自动巡检
        每轮会「打开最近任务 → 拉起钉钉 → 搜索并点开目标群」，全程占好几秒
        前台。它恰好在对方点「确定」的那一刻跑起来，对方的点击就落到了
        最近任务界面上 —— 表现是「注入了 176 条，列表却是 0」，那批单全废。

        两个程序互不认识，谁也没错，错在没人协调。这个接口就是那段协调：
        对方动手前调一下，我们这 `seconds` 秒内不碰前台；对方跑完（或超时）
        自动恢复，不需要它记得恢复 —— 对方进程崩了也不会把巡检永久关死。

        实现上只改 ``ctx.watch_pause_until``（单调时钟），不动 ``watch_min``：
        ``watch_min`` 是用户在设置里填的长期配置，为了临时让路去改它，
        一旦恢复失败就会把用户的设置弄丢。
        """
        ctx = self.ctx
        seconds = max(0, min(int(seconds), 3600))
        until = time.monotonic() + seconds if seconds else 0.0
        with ctx.lock:
            ctx.watch_pause_until = until
        if not seconds:
            log.info("解除让路：自动巡检恢复正常")
            return "已解除让路，自动巡检恢复正常"
        log.info("收到让路请求：%d 秒内不自动巡检（给另一个操作手机的程序腾前台）",
                 seconds)
        return f"已让路 {seconds} 秒，期间不切群、不抢前台"

    def _watch_paused(self) -> float:
        """还要让路多少秒，0 = 没在让路。"""
        until = self.ctx.watch_pause_until
        if not until:
            return 0.0
        left = until - time.monotonic()
        return left if left > 0 else 0.0

    def _auto_watch_unknown_page(self) -> None:
        """连续认不出聊天页时，自动请手机跑一轮巡检把钉钉切回目标群。

        指令走现成的 watch_now 通道（心跳响应带下去，手机当场跑 SelfHeal，
        走搜索框回群那条成熟老路）。冷却防的是「钉钉真改版、永远认不出」时
        每 3 帧抢一次用户前台。只在恰好达到告警阈值的那个瞬间调用（由调用方
        ``skip_streak == UNKNOWN_PAGE_ALERT_AFTER`` 保证），天然不重复。

        ⚠ 调用点在 ``_ingest`` 里，而 ``_ingest`` 是在 ``/screen`` 的
        ``ctx.lock`` **里面**跑的（帧要按到达顺序喂给 FrameDiffer）。所以这里
        绝不能调 ``_queue_watch_now``（它自己也拿这把非重入锁 —— 会原地死锁，
        2026-10-31 回归测试第一个抓到），而是在已持锁的前提下直接递增序号。
        """
        ctx = self.ctx
        now = time.monotonic()
        if now - ctx.last_auto_watch_at < UNKNOWN_PAGE_AUTO_WATCH_COOLDOWN_S:
            return
        if not ctx.watch_group:
            return                    # 手机不知道切到哪个群，发了也白发
        ctx.last_auto_watch_at = now
        ctx.watch_now_seq += 1
        log.warning("连续 %d 帧认不出聊天页，已自动下发「切回目标群」指令"
                    "（第 %d 次，冷却 %.0f 秒）",
                    UNKNOWN_PAGE_ALERT_AFTER, ctx.watch_now_seq,
                    UNKNOWN_PAGE_AUTO_WATCH_COOLDOWN_S)

    def _queue_watch_now(self) -> str:
        """挂一条「立刻切回目标群」的指令，返回一句给人看的话。

        ⚠ 序号要**在锁里**加：抓取帧和心跳是两个不同线程，两边都在读
        ``watch_now_seq`` / 写 ``watch_now_served``。不加锁的话会出现
        「指令被读走了但 served 没更新 → 手机每帧都被要求切群」，
        表现就是用户回个消息就被程序抢走前台。
        """
        ctx = self.ctx
        with ctx.lock:
            if not ctx.watch_group:
                return ("没配目标群（dingtalk.chat_names），手机不知道该切到哪个群"
                        " —— 去「设置」里填上群名")
            ctx.watch_now_seq += 1
            seq = ctx.watch_now_seq
        log.info("收到「切到钉钉群」指令（第 %d 次）：等手机下次心跳带下去", seq)
        return (f"已排队：手机会在 1～60 秒内切到「{ctx.watch_group}」"
                f"（手机自己会戳醒心跳，通常几秒内就能切）")

    def _queue_attach_now(self, name: str) -> tuple[str, bool]:
        """手工把一个附件名塞进抓取队列，返回 `(给人看的话, 是否受理)`。

        与 `_order_attach` 共用同一份状态，所以要按它的规矩来：
          · 清掉这个名字的冷却，否则 10 分钟内的重试会被 `ATTACH_COOLDOWN`
            直接吞掉（表现成「点了没反应」）；
          · 不越过 `attach_active` —— 手机端并发下发会被当场丢弃，那条订单
            白白进冷却，等于永远抓不到；
          · 关掉附件总开关（``attach_enabled``）时直接拒绝并说明，别让指令
            排进队列后永远不消化。
        """
        ctx = self.ctx
        name = (name or "").strip()
        if not name:
            return ("没给附件名：用 ?name=下载附件 指定屏幕上那个按钮/附件行的文字",
                    False)
        if not getattr(ctx, "attach_enabled", True):
            return ("附件抓取总开关是关的（设置里「抓取附件」未勾选），"
                    "队列不会消化 —— 先打开再试", False)
        with ctx.lock:
            if name == ctx.attach_active:
                return (f"「{name}」正在抓（已下单 {ctx.attach_orders.get(name, 0):.0f}"
                        f"），别重复下"), False
            if name in ctx.attach_queue:
                return (f"「{name}」已在队列第 {ctx.attach_queue.index(name) + 1} 位",
                        False)
            ctx.attach_orders.pop(name, None)
            ctx.attach_queue.append(name)
            # 人工要的单不受 30 分钟积压闸门限制（见 _Ctx.attach_manual）。
            ctx.attach_manual.add(name)
            pos = len(ctx.attach_queue) + (1 if ctx.attach_active else 0)
            # 受单后**立刻要一帧**：手机静止时自己不刷帧（实测 age 能挂到
            # 7 分钟以上），而派单只发生在「收到帧」的那一刻 —— 不主动
            # 要帧，这单就在队列里干等到天荒地老（2026-11-01 实锤：三分钟
            # 全程无帧、无派单、无点击）。
            now = time.monotonic()
            ctx.frame_req = max(int(now), ctx.frame_req + 1)
            ctx.frame_req_at = now
        log.info("手工补抓附件「%s」（队列第 %d 位）：等手机下次心跳/抓帧取走",
                 name, pos)
        return (f"已受理：手机会在 1～60 秒内去点「{name}」并回传文件"
                f"（队列第 {pos} 位）"), True

    # --- 断连：不该在日志里变成红色 traceback ---

    def handle_one_request(self) -> None:
        """收口「手机把连接掐了」这一类异常。

        手机上滑掉 App、切飞行模式、换 WiFi、切后台 —— 都会在电脑侧抛出
        ``ConnectionResetError: [WinError 10054] 远程主机强迫关闭了一个现有的连接``。
        这是**移动端的日常**，不是故障：消息该重传的会重传、真断了有失联告警兜着。
        但 ``BaseHTTPRequestHandler`` 不接这个异常，它会一路冒到
        ``ThreadingHTTPServer.handle_error`` 打成 ``Exception occurred during
        processing of request`` 全屏 traceback，用户在日志窗口看到就以为程序崩了。
        """
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError,
                BrokenPipeError) as exc:
            log.debug("手机断开连接（%s）：%s", self.address_string(), exc)
            self.close_connection = True

    def handle(self) -> None:
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError,
                BrokenPipeError) as exc:
            log.debug("连接中断（%s）：%s", self.address_string(), exc)
            self.close_connection = True

    def _auth_ok(self) -> bool:
        # 两个头名都认：X-Bridge-Token 是本文档规定的口径，X-Auth-Token 是手机端
        # 那个自建 App 用的。它们指同一个 token，不构成额外的攻击面，
        # 但少了这个别名就会出现「App 里 token 填得一字不差却一直 401」这种
        # 极难自查的问题。
        got = (self.headers.get("X-Bridge-Token")
               or self.headers.get("X-Auth-Token") or "")
        if not got:
            # 也认 ?token=xxx —— 方便用浏览器或一行 curl 做连通性自测
            got = (parse_qs(urlparse(self.path).query).get("token") or [""])[0]
        if not got:
            return False
        # 编成 bytes 再比：hmac.compare_digest 对 str 要求纯 ASCII，
        # 用户把 token 填成中文时会直接抛 TypeError（变成 500），
        # 而这里应该是一个干净的 401。
        return hmac.compare_digest(got.encode("utf-8"),
                                   (self.ctx.token or "").encode("utf-8"))

    def _reject_auth(self) -> None:
        # 先把请求体读掉再回 401：不回完就 close，客户端还在写 body，
        # 内核直接给 RST —— 客户端看到的是 ConnectionAbortedError，
        # 而不是这句「token 不对」（手机 App 上就是「连接被重置」，
        # 排障时最容易被带偏；2026-10-31 全量测试偶发挂在同一条上）。
        self._drain_body()
        self._reply(401, {"ok": False, "error": "token 不对",
                          "hint": "请求头带上 X-Bridge-Token，值与本机 "
                                  "config.yaml 的 dingtalk.gateway_token 一致"})

    def _drain_body(self) -> None:
        """把 Content-Length 指的请求体读掉（最多 64KB），读不到就算了。

        只为让「拒绝」是个干净的响应，不做任何解析。
        """
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return
        if n <= 0:
            return
        try:
            self.rfile.read(min(n, 65536))
        except Exception:                              # noqa: BLE001
            pass

    # --- 端点 ---

    def do_GET(self) -> None:                                 # noqa: N802
        path = self._path()
        if path not in ("/", "/ping", "/screen", "/app", "/app.apk"):
            self._reply(404, {"ok": False, "error": "未知路径"})
            return
        # 下载页与 APK 本身**不要求 token**：手机第一次装的时候还没有 token
        # （token 要靠装完 App 才能填），要求鉴权就永远装不上。
        # 这两个端点只吐两个固定文件，不暴露任何消息内容，没有鉴权风险。
        if path in ("/app", "/app.apk"):
            self._serve_app(path)
            return
        if not self._auth_ok():
            self._reject_auth()
            return
        if path == "/screen":
            self._get_screen()
            return
        if path == "/ping":
            # 只有带 hb=1 才算「手机还在」：浏览器/curl 里随手打开一次只是自测，
            # 若也刷新在线状态，人工排查反而会把真正的失联掩盖掉。
            q = parse_qs(urlparse(self.path).query)
            if (q.get("hb") or [""])[0] in ("1", "true", "yes"):
                self.ctx.touch(device=(q.get("device") or [""])[0],
                               heartbeat=True)
        self._reply(200, {"ok": True, "source": "android",
                          "chat_name": self.ctx.chat_name,
                          "online": _online_now(self.ctx),
                          "state": self.ctx.state,
                          "last_seen": self.ctx.last_seen_wall,
                          "device": self.ctx.last_device,
                          "app_ver": self.ctx.last_ver or "(旧版未上报)",
                          "heartbeat": self.ctx.heard_heartbeat,
                          # 巡检状态。runs 不涨 = 定时器根本没在转，
                          # 这是「装了新包却没生效」最直接的证据。
                          "watch": self.ctx.last_watch or "(还没巡检过)",
                          "watch_runs": self.ctx.last_watch_runs,
                          "probe": self.ctx.last_probe,
                          # 手机端附件抓取的最近一句结论（空 = 还没抓过 / 旧 APK）。
                          "attach": self.ctx.last_attach or "(还没抓过附件)",
                          # 还没拿到文件的附件订单：名字 → 已等秒数。
                          # 有它在，「下单了但手机没反应」和「根本没下单」一眼分开。
                          "attach_pending": _attach_pending(self.ctx),
                          # 正在抓的那个 + 排队待抓的（多明细卡片会排队，
                          # 队列长时间不减 = 手机卡在某一条上了）。
                          "attach_active": self.ctx.attach_active,
                          "attach_queue": list(self.ctx.attach_queue),
                          # 心跳里到底带没带巡检状态。看这一项就能分清
                          # 「手机没报」和「报了但巡检没跑」。
                          "watch_header": self.ctx.watch_header,
                          "offline_after": self.ctx.offline_after,
                          # 只读展示（不推进编号）：人工排查时能看出电脑最近
                          # 有没有求过帧、手机有没有理它。
                          "want_frame": self.ctx.frame_req,
                          "screen": _screen_meta(self.ctx.screen),
                          "stats": dict(self.ctx.count)})

    # --- 手机端 APK 下载 ---

    def _serve_app(self, path: str) -> None:
        """``/app`` 给下载页，``/app.apk`` 给 APK 本体。

        为什么放在网关里而不是起一个文件服务器：手机只要能在浏览器里打开
        ``http://<电脑IP>:8765/``（本来就要填这个地址），就应该能顺手把 App 装好，
        不必再传数据线、也不必修一个 FTP。
        """
        apk = _find_apk()
        if path == "/app.apk":
            if apk is None:
                self._reply(404, {
                    "ok": False, "error": "电脑上还没有 APK",
                    "hint": "在 ddwx-bridge/android 下编译一次：python build.py"})
                return
            data = apk.read_bytes()
            try:
                self.send_response(200)
                # 必须是 apk 专属 MIME：手机浏览器认这个才肯下载安装，
                # 给成 octet-stream 有的 ROM 会存成无扩展名的文件。
                self.send_header("Content-Type",
                                 "application/vnd.android.package-archive")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{apk.name}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        self._html(200, _app_page(apk, self.ctx))

    def _html(self, code: int, html: str) -> None:
        data = html.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _get_screen(self) -> None:
        """取最新一帧节点树。默认给 JSON 摘要，``?raw=1`` 给 XML 正文。"""
        screen = self.ctx.screen
        if not screen:
            self._reply(404, {
                "ok": False, "error": "还没有收到任何画面",
                "hint": "手机端网络通了、无障碍服务开了、钉钉停在群聊页之后，"
                        "这里才会有内容"})
            return
        raw = (parse_qs(urlparse(self.path).query).get("raw") or [""])[0]
        if raw in ("1", "true", "yes"):
            data = (screen.get("xml") or "").encode("utf-8")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        meta = _screen_meta(screen)
        meta["ok"] = True
        self._reply(200, meta)

    def do_POST(self) -> None:                                # noqa: N802
        path = self._path()
        if path not in ("/", "/messages", "/screen", "/ping", "/watch-now",
                        "/watch-pause", "/attach-now", "/file"):
            self._reply(404, {"ok": False, "error": "未知路径"})
            return
        if not self._auth_ok():
            self._reject_auth()
            return

        if path == "/watch-pause":
            # 让路接口：另一个操作手机的程序（运单揽件助手）在点「确定/上传」
            # 之前调它，我们这 N 秒内不巡检、不抢前台。传 seconds=0 立刻解除。
            # 不传 seconds 默认 120 秒 —— 够对方跑完一批，又短到即使它崩了
            # 我们也最多瞎 2 分钟，下一轮巡检自己就回来了。
            try:
                wp_len = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                wp_len = 0
            raw = b""
            if wp_len > 0:
                try:
                    raw = self.rfile.read(min(wp_len, 4096))
                except (OSError, ConnectionError) as exc:
                    log.debug("读让路请求体时对方断开：%s", exc)
                    raw = b""
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            try:
                seconds = int(payload.get("seconds", 120))
            except (TypeError, ValueError):
                self._reply(400, {"ok": False, "error": "seconds 必须是整数"})
                return
            msg = self._apply_watch_pause(seconds)
            self._reply(200, {"ok": True, "message": msg,
                              "seconds": max(0, min(seconds, 3600)),
                              "paused_left": round(self._watch_paused(), 1)})
            return

        if path == "/watch-now":
            # 电脑端「切到钉钉群」按钮走的接口。挂一条一次性指令，手机在下一次
            # 心跳（60 秒内）或抓取帧（1 秒内戳醒心跳）取走。
            #
            # 为什么不直接连手机：手机没有常驻服务端，一直是它主动连电脑。
            # 电脑这边唯一能做的就是「留话」，等它来取。
            try:
                wn_len = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                wn_len = 0
            self._drain(wn_len)
            msg = self._queue_watch_now()
            self._reply(200, {"ok": True, "message": msg,
                              "chat_name": self.ctx.watch_group,
                              "pending": (self.ctx.watch_now_seq
                                          > self.ctx.watch_now_served)})
            return

        if path == "/attach-now":
            # 手工补抓一个附件（运维接口）。存在的理由：附件字眼是**事后**
            # 才被发现有遗漏的（2026-11-01 12:08「下载附件」夹在长行中间），
            # 那条消息早已入库、被 differ 当成已知，修好解析也不会重放。
            # 有了它就不用「删库重放」或「再改代码重启」——直接点一下手机去抓。
            #
            # 只做「塞进队列」：手机同一时刻只能跑一个抓取任务，真正的下发
            # 仍由 `_order_attach` 在下一帧统一调度（并发下发会被手机丢弃）。
            try:
                an_len = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                an_len = 0
            self._drain(an_len)
            q = parse_qs(urlparse(self.path).query)
            name = (q.get("name") or [""])[0].strip()
            # 可选：手工指定候选链（"A||B||C"）。不指定时手机只点这一行 ——
            # 手工补抓的场景里，人已经看准了屏上那一行是哪条。
            cands = [x.strip() for x in (q.get("cands") or [""])[0].split("||") if x.strip()]
            if cands:
                self.ctx.attach_cands[name] = cands
            msg, ok = self._queue_attach_now(name)
            ctx = self.ctx
            self._reply(200 if ok else 400, {
                "ok": ok, "message": msg, "name": name,
                "attach_enabled": bool(getattr(ctx, "attach_enabled", True)),
                "attach_active": ctx.attach_active,
                "attach_queue": list(ctx.attach_queue),
            })
            return

        if path == "/ping":
            # 手机端的**心跳**。它存在的唯一理由，是让电脑侧能区分
            # 「手机好好的，只是群里没人说话」和「手机端已经死了」——
            # 前者的表现是「没有新消息」，后者也是，光看消息永远分不出来。
            # 手机端会带一个 {} 作请求体；curl 手测可能什么都不带。
            # 不读掉又直接回包的话，客户端还在写 → RST → 它只看到「连接被重置」。
            try:
                hb_len = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                hb_len = 0
            self._drain(hb_len)
            device = (self.headers.get("X-Ddwx-Device") or "").strip()
            # ⚠ 必须带上 ver：心跳是手机端**唯一规律发送**的请求（60 秒一次），
            # /screen 只在画面变化时才来。漏了这里 = 手机明明装了新版，
            # /ping 却永远答「旧版未上报」（2026-10-20 就这么白查了一轮）。
            ver = (self.headers.get("X-Ddwx-Ver") or "").strip()
            # 巡检状态。手机端按 URL 编码塞进来（HTTP 头不能直接放中文），
            # 这里解回来。解不开就当没有 —— 诊断信息不该让心跳失败。
            # ⚠ 先取原始头再解码：`_unquote_header("")` 也返回 ""，
            # 而「头不存在」和「头存在但为空」是**两件事** —— 前者是旧版手机端
            # 不会发，后者是发了但还没巡检过。混为一谈就会出现「/ping 说带了，
            # 其实手机根本没这个功能」这种把排查带偏的结论。
            raw_watch = self.headers.get("X-Ddwx-Watch")
            watch = _unquote_header(raw_watch) if raw_watch is not None else None
            try:
                runs = int((self.headers.get("X-Ddwx-Watch-Runs") or "0").strip() or 0)
            except ValueError:
                runs = 0
            raw_probe = self.headers.get("X-Ddwx-Probe")
            probe = _unquote_header(raw_probe) if raw_probe is not None else None
            # 附件抓取结论（APK 0.5.17 起）。和巡检状态同一套做法：编码进头、
            # 解不开就当没有 —— 诊断信息绝不能让心跳本身失败。
            raw_attach = self.headers.get("X-Ddwx-Attach")
            attach = _unquote_header(raw_attach) if raw_attach is not None else None
            prev_attach = self.ctx.last_attach
            self.ctx.touch(device=device, heartbeat=True, ver=ver,
                           watch=watch, watch_runs=runs, probe=probe,
                           attach=attach)
            # 失败了当场说一句（只在新结论时打，否则每 60 秒刷一遍同样的字）。
            # 成功不用打：正常链路后面本来就有「收到手机附件」那行。
            #
            # ⚠ 2026-11-01 14:09 误报：worker 重启把 last_attach 清空了，手机
            # 端 lastStatus 还是上一轮（12:31 手动实验）的「屏上找不到附件行
            # （下载附件）」，重启后第一次心跳就被当成**新**故障告了警，用户
            # 以为又出问题了。判据补一条：当前没有在抓的单、也没有排队的单，
            # 那这个结论一定是陈旧的（手机不会替一个不存在的订单干活）。
            if attach and attach != prev_attach and any(
                    k in attach for k in _ATTACH_BAD_WORDS):
                if self.ctx.attach_active or self.ctx.attach_queue:
                    log.warning("手机那边附件没抓成：%s", attach)
                else:
                    log.info("手机端附件结论（陈旧，当前没有在抓的单）：%s", attach)
            hb = {"ok": True, "source": "android",
                  "chat_name": self.ctx.chat_name,
                  "last_seen": self.ctx.last_seen_wall,
                  "offline_after": self.ctx.offline_after,
                  # 巡检结论回给手机，是为了让**电脑端界面**（点完「切到钉钉群」
                  # 的按钮）能当场把结果说出来。界面只连本机网关、拿不到
                  # 进程内的状态，不放在回包里它就只能干等或让用户自己去看。
                  "watch": self.ctx.last_watch,
                  "watch_runs": self.ctx.last_watch_runs,
                  "probe": self.ctx.last_probe,
                  # 「请你重发一帧当前画面」。手机拿它跟自己的记录比，比它大
                  # 就清指纹重发。**必须随每次心跳都带**（不是只带一次）：
                  # 手机可能正好在重启 App，或者在忙别的抓取 —— 只带一次就丢。
                  "want_frame": _want_frame(self.ctx)}
            # 让路中：心跳里也给个标记，方便手机端/日志对上时间。
            paused_left = self._watch_paused()
            if paused_left:
                hb["watch_paused_left"] = round(paused_left, 1)
            # 「立刻切到钉钉群」指令随心跳下发（电脑端按钮点的）。
            # 只发一次：手机取走后立刻清掉，否则它每 60 秒被切一次群 ——
            # 用户回个消息就会被程序抢走前台。
            if self.ctx.watch_now_seq > self.ctx.watch_now_served:
                self.ctx.watch_now_served = self.ctx.watch_now_seq
                hb["watch_now"] = 1
            # 附件单也搭心跳的回程车下发。
            #
            # ⚠ 2026-11-01 16:4x 的死锁，必须这么修：派单原本只在 ``/screen``
            # 的响应里做，手机端也只读帧响应（CollectorService.maybeFetchAttachment）。
            # 而手机一旦停在「附件下载后的预览页」，那一页界面不变就不报帧 ——
            # 于是「不报帧 → 取不到单 → 没人触发 AttachmentFetcher.restoreToChat
            # 退出预览页 → 继续不报帧」，原地锁死。现象就是：屏幕上明明白白
            # 停着那个页面，程序一动不动，**用户只能手动点左上角「返回」**。
            # 心跳是手机唯一规律发出的请求（60 秒一次），把单挂在这条路上，
            # 通道就永远断不了：哪怕一帧都收不到，60 秒内也能把手机拉回来。
            attach_hb = _order_attach(self.ctx, [])
            if attach_hb:
                hb["fetch_attach"] = attach_hb
                hb["fetch_keys"] = _fetch_keys_for(self.ctx, attach_hb)
                cands_hb = (getattr(self.ctx, "attach_cands", {}) or {}).get(attach_hb) or [attach_hb]
                hb["fetch_cands"] = "||".join(cands_hb)
                log.info("附件「%s」随心跳下发（画面帧通道可能已经停摆；候选 %d 个）",
                         attach_hb, len(cands_hb))
            self._reply(200, hb)
            return

        limit = MAX_SCREEN_BODY if path in ("/screen", "/file") else MAX_BODY
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._reply(400, {"ok": False, "error": "请求体为空"})
            return
        if length > limit:
            self._drain(length)
            self._reply(413, {"ok": False,
                              "error": f"请求体过大（{length} > {limit} 字节）"})
            return

        raw = self.rfile.read(length)

        if path == "/screen":
            self._post_screen(raw, length)
            return

        if path == "/file":
            self._post_file(raw, length)
            return

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._reply(400, {"ok": False, "error": f"报文不是合法 JSON：{exc}"})
            return
        if not isinstance(payload, dict):
            self._reply(400, {"ok": False, "error": "报文顶层必须是对象"})
            return

        items = payload.get("messages")
        if isinstance(items, list) and len(items) > MAX_BATCH:
            self._reply(413, {"ok": False,
                              "error": f"单次最多 {MAX_BATCH} 条，收到 {len(items)} 条"})
            return

        try:
            msgs, skipped = build_messages(
                payload, chat_name=self.ctx.chat_name,
                companies=self.ctx.companies)
        except (ValueError, TypeError) as exc:
            self._reply(400, {"ok": False, "error": str(exc)})
            return

        # 能在 /messages 里报消息，本身就说明手机还活着（老版 APK 没有心跳，
        # 靠这一条也能让「启动后一次都没收到」的告警不再误报）。
        self.ctx.touch(device=(self.headers.get("X-Ddwx-Device") or "").strip(),
                       ver=(self.headers.get("X-Ddwx-Ver") or "").strip())

        for msg in msgs:
            try:
                self.ctx.on_message(msg)
            except Exception:                                  # 单条出错不拖垮整批
                log.exception("下游处理消息失败：%s", msg.uid)

        with self.ctx.lock:
            self.ctx.count["requests"] += 1
            self.ctx.count["accepted"] += len(msgs)
            self.ctx.count["rejected"] += len(skipped)

        if msgs:
            head = msgs[0].content[:40].replace("\n", " ")
            log.info("安卓端上报 %d 条（跳过 %d 条）：%s", len(msgs), len(skipped), head)

        self._reply(200, {"ok": True, "accepted": len(msgs),
                          "skipped": skipped, "chat_name": self.ctx.chat_name})

    # --- 附件上传（仅 dingtalk.android_parse = true）---

    def _post_file(self, raw: bytes, length: int) -> None:
        """收手机传回的附件文件。

        请求体是 JSON：``{"name": 显示名, "sha1": 文件摘要,
        "data": base64, "chat": 群名}``。手机侧在下发抓取指令后点击附件、
        等钉钉把文件下到公开 Download 目录、读出来传上来 —— 这条链路的
        电脑侧终点就是这里：落盘 + 去重 + 交给 service 渲染投递。

        去重两层：
          · sha1 相同的文件**不再回调下游**（卡片重渲染会触发重复下载上传），
            但仍回 ok=true —— 手机不需要重传，别让它把 409 当故障。
          · 文件落盘名带 sha1 前缀：同名不同内容天然分文件。
        """
        try:
            meta = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._reply(400, {"ok": False, "error": f"报文不是合法 JSON：{exc}"})
            return
        if not isinstance(meta, dict):
            self._reply(400, {"ok": False, "error": "报文顶层必须是对象"})
            return
        name = str(meta.get("name") or "").strip()
        chat = str(meta.get("chat") or "").strip()
        if not chat:
            # 手机端拿不到会话名时这里就是空串。空 chat 会让附件的 Message
            # 匹配不上任何转发规则 → 只入库、不进微信（2026-10-31 17:50
            # 「查看完整月报明细（Excel）」就是这么被吞的，日志只留一行
            # 「未命中转发规则」）。网关自己知道在盯哪个群，用它对上。
            chat = self.ctx.watch_group or ""
        sha = str(meta.get("sha1") or "").strip().lower()
        data_b64 = str(meta.get("data") or "")
        if not name or not sha or not data_b64:
            self._reply(400, {"ok": False,
                              "error": "缺 name / sha1 / data 字段"})
            return
        if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
            self._reply(400, {"ok": False, "error": "sha1 不是 40 位十六进制"})
            return
        try:
            data = base64.b64decode(data_b64, validate=True)
        except Exception as exc:                       # noqa: BLE001
            self._reply(400, {"ok": False,
                              "error": f"data 不是合法 base64：{exc}"})
            return
        if hashlib.sha1(data).hexdigest() != sha:
            self._reply(400, {"ok": False,
                              "error": "sha1 校验不符（传输损坏？请重传）"})
            return
        if not self.ctx.files_dir:
            self._reply(500, {"ok": False,
                              "error": "网关没配附件目录（files_dir 为空）"})
            return

        # 文件名只留白名单字符：手机传什么就信什么是路径穿越的老坑。
        safe = re.sub(r"[^\w.\-()（）]", "_",
                      name.replace("\\", "/").split("/")[-1]).strip("._")
        if not safe:
            safe = "attachment"
        os.makedirs(self.ctx.files_dir, exist_ok=True)
        path = os.path.join(self.ctx.files_dir, f"{sha[:12]}-{safe}")
        duplicate = os.path.exists(path)
        if not duplicate:
            tmp = path + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)          # 先写临时名再改名：中断不留半个文件

        # 「要不要交给下游」只看**本进程内**有没有见过这个 sha。
        # 磁盘上已有同名文件 ≠ 已经发成功了：2026-11-01 14:2x 实测，文件落盘
        # 之后投递线程卡死，手机重传同一份文件时被这里按「文件已存在」挡成
        # duplicate，消息就再也发不出去。真正的「发过没有」由 service 按
        # deliveries 表判定（Storage.is_duplicate(need_delivered=True)）。
        # attach_seen 只留作统计/诊断（进程内见过哪些 sha），不再参与决策。
        self.ctx.attach_seen[sha] = True
        # 附件超时告警的「已收到」标记：哪怕这份文件不是当次下单的那份
        # （名字对不上），也说明链路活着，不该在冷却上再叠一次告警。
        self.ctx.attach_file_at = time.monotonic()
        # 文件在微信里叫什么：优先「正在抓的那条附件行」的名字（用户看得懂的
        # 中文名），取完再清 active —— 顺序反了就只能拿到手机那串英文名。
        wanted = _display_name_for_file(self.ctx, name)
        # 候选链（用户 2026-11-01「带明细都点」）带来的新情况：手机可能点的
        # **不是** attach_active 那一行，而是候选链里的下一行。这时手机报上来
        # 的 name 才是「真正下到这个文件的那一行」，用它当显示名才对得上内容
        # —— 否则「履约失败明细（10票）」这个表格标题会被印到别的文件头上。
        chain = (getattr(self.ctx, "attach_cands", {}) or {}).get(
            self.ctx.attach_active) or []
        if name and name != wanted and name in chain:
            wanted = name
        # 这一个抓到了 → 放行队列里的下一个（多明细卡片靠它往下走）。
        # 取源消息时间要在清 active **之前**：这条单对应的消息时间条记在
        # attach_src_ts 里，键可能是 active 名、手机报的行名、或显示名。
        src_time = _take_attach_src_time(self.ctx, self.ctx.attach_active,
                                         name, wanted)
        self.ctx.attach_active = ""
        cb = self.ctx.on_file
        # ⚠ attach_seen **不能**拿来挡回调（2026-11-01 14:46~14:53 实锤）：
        #   它在回调前就标记 sha，而第一次回调完全可能「没发成功」（目标群
        #   名对不上 → 未命中规则，仅入库）。之后手机每重传一次都被
        #   fresh=False 拦成「重复跳过」—— 这份文件就永远发不出去了。
        #   「发成功过没有」由下游按 deliveries 表精确判定
        #   （Storage.is_duplicate(need_delivered=True)），这里只负责转交。
        if cb is not None:
            try:
                if _cb_wants_src(cb):
                    cb(path, name, chat, wanted, src_time)
                elif _cb_wants_wanted(cb):
                    cb(path, name, chat, wanted)
                else:
                    cb(path, name, chat)
            except Exception:                          # noqa: BLE001
                log.exception("附件下游处理失败：%s", name)

        log.info("收到手机附件「%s」（%d 字节%s）→ %s", name, len(data),
                 "，磁盘已有同名文件（是否重发由投递记录判定）" if duplicate else "",
                 path)
        self._reply(200, {"ok": True, "duplicate": duplicate})

    def _post_screen(self, raw: bytes, length: int) -> None:
        """收一帧节点树。只做「存下来 + 记元信息」，不解析 —— 解析是采集端的事。"""
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            self._reply(400, {"ok": False, "error": f"画面不是 UTF-8：{exc}"})
            return
        # 入口形状检查。这一条的价值在于把「IP 填成了别的服务」「手机端版本不对」
        # 这类问题在门口就挡下来 —— 否则后面解析器要拿一堆垃圾去猜结构，
        # 报出来的错会离真正的原因非常远。
        head = text[:600]
        if "<hierarchy" not in head:
            self._reply(400, {
                "ok": False, "error": "请求体不像 uiautomator 节点树 XML",
                "hint": "应当以 <?xml 开头且含 <hierarchy>；"
                        "确认手机端 App 是配套版本"})
            return

        try:
            texts = int(self.headers.get("X-Ddwx-Texts") or 0)
        except ValueError:
            texts = 0

        with self.ctx.lock:
            prev = self.ctx.screen or {}
            count = int(prev.get("count", 0)) + 1
            screen = {
                "xml": text,
                "bytes": length,
                # 手机端给了摘要就用它（省一次全量哈希），没给就自己算
                "md5": (self.headers.get("X-Ddwx-Md5") or "").strip()
                       or hashlib.md5(raw).hexdigest(),
                "texts": texts,
                "device": (self.headers.get("X-Ddwx-Device") or "").strip(),
                "ver": (self.headers.get("X-Ddwx-Ver") or "").strip(),
                "reason": (self.headers.get("X-Ddwx-Reason") or "").strip(),
                "ts": time.time(),
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "count": count,
            }
            self.ctx.screen = screen
            # 解析 + 判新老都在这把锁里：帧必须**按到达顺序**喂给 FrameDiffer，
            # 否则两帧并发时锚点会错位，表现为偶尔漏发或重复发。这两步是纯
            # 内存计算（几毫秒），占锁时间可控。
            pending: list = []
            note = ""
            scroll = False
            if self.ctx.parse and self.ctx.on_message is not None:
                # 手机端自报的帧原因（tick / scroll-fill / probe-fill /
                # probe-bottom …）。「回到底部」那一帧只能靠它认出来 ——
                # 回底后的画面和历史中间那一屏在节点树里长得一模一样。
                self.ctx.frame_reason = screen["reason"]
                # 手机端上翻补全后重报的那一帧会带 reason=scroll-fill。
                # 它必须走另一套判定 —— 见 _ingest 里的说明。
                pending, note, scroll = self._ingest(
                    text, filling=(screen["reason"] == "scroll-fill"),
                    frame_md5=screen["md5"])

        # 记「手机还活着」。必须在锁外调：touch 走的是另一把锁，
        # 但把它放在这里能让 hold 锁的时间只包含解析。
        self.ctx.touch(device=screen["device"], ver=screen.get("ver", ""))

        if count == 1:
            log.info("收到第一帧手机画面：%d 字节，%d 条文本（来自 %s）%s",
                     length, texts, screen["device"] or "未知名", tail("frames"))
        elif count % SCREEN_LOG_EVERY == 0:
            log.info("手机画面已累计 %d 帧（最新 %d 字节 / %d 条文本）%s",
                     count, length, texts, tail("frames"))
        else:
            log.debug("画面 #%d：%d 字节，%d 条文本，md5=%s",
                      count, length, texts, screen["md5"][:8])

        # 投递在锁外：下游 on_message 会同步等微信把消息发出去（可能几秒），
        # 占着锁会把 GET /screen 和手机的下一次上报一起卡住。
        emitted = self._emit(pending)

        # 附件订单超时必须**每帧**查。原来只在「下一张卡片出现时」顺手查一次，
        # 抓失败就永远静默 —— 2026-10-31 15:18 下单「运单明细」后手机端零动作，
        # 日志里连一条「超时」都没有，用户只看到「附件没发出去」。每帧查 = 必留痕。
        _attach_timeout_check(self.ctx)

        body = {"ok": True, "bytes": length, "count": count,
                "md5": screen["md5"]}
        # 自动巡检指令：每一帧都带，手机端见 applyWatch（幂等，值没变不动调度器）。
        # 放在 parse 判断**外面** —— 它和「解析不解析消息」无关，只收不转的模式
        # 下手机同样会跑偏，同样需要它自己走回来。
        #
        # 让路期间（另一个操作手机的程序正在点确定/上传）**下发 0**：手机端
        # 收到 0 就停掉巡检定时器，不去抢前台。这比在手机端加判断更稳 ——
        # 手机那边的巡检是独立调度器，PC 侧管不到，只能通过「不下发」来闭麦。
        # 让路一结束，下一帧又变回正常的 watch_min，手机自己就重新排上。
        paused = self._watch_paused()
        if paused:
            body["watch_group"] = self.ctx.watch_group or ""
            body["watch_min"] = 0
            body["watch_paused_left"] = round(paused, 1)
        elif self.ctx.watch_min > 0 and self.ctx.watch_group:
            body["watch_group"] = self.ctx.watch_group
            body["watch_min"] = self.ctx.watch_min
        else:
            # 明确下发 0：手机端据此停掉巡检。不能「不发」—— 不发的话手机
            # 会一直沿用本地记住的旧值，用户在电脑上关掉了却停不下来。
            body["watch_group"] = self.ctx.watch_group or ""
            body["watch_min"] = 0
        # 「电脑端点过按钮、指令还没被取走」。只是一个**提醒手机去戳心跳**的标记，
        # 真正的指令在 /ping 响应里（watch_now=1）。分两处是刻意的：
        # 指令必须在心跳里一次性取走（不然每帧都切群，用户回消息会被抢前台），
        # 而抓取帧只负责「快」—— 它几百毫秒一次，能把 60 秒的等待压到 1 秒内。
        if self.ctx.watch_now_seq > self.ctx.watch_now_served:
            body["watch_now_pending"] = 1
        if self.ctx.parse:
            # 手机端日志里直接能看到「这一帧有没有产生转发」，省得两头对时间。
            # ⚠ 这里的数字是**收进缓冲**的条数，不一定是「已投递」的条数 ——
            # 消息要静默几秒才发（见 pending.py）。所以另给一个 buffered 说明
            # 还在攒着几条，免得拿日志断言「转发成功」时被误导。
            body["messages"] = emitted
            if self.ctx.pending is not None:
                body["buffered"] = len(self.ctx.pending)
            if note:
                body["note"] = note
            # 这是给手机端的**指令**：屏上有消息被切掉了，请往上翻一屏再报。
            # 手机端见 android/.../CollectorService.maybeFillTruncated。
            if scroll:
                body["scroll"] = True
            # 锚点试探的另一条指令：**往上翻一屏、停在原地**（位置累积），
            # 下一轮接着往上。与 scroll 分开是刻意的 —— scroll 会翻回底部，
            # 位置不累积，拿来找锚点等于原地打转（2026-10-29 真机 175 次空翻）。
            if self.ctx.want_probe:
                body["probe"] = self.ctx.want_probe
                self.ctx.want_probe = 0
            # 试探认输 / 状态过期：先请手机把视口开回最新处，再拿那一帧重建
            # 基线。不回底就重建 = 基线建在历史中间，下面的新消息永远看不见。
            if self.ctx.want_bottom:
                body["to_bottom"] = 1
                self.ctx.want_bottom = False
            # 附件抓取指令：新消息里出现「附件：xxx」→ 请手机点它、等下载、
            # 把文件传回 /file。冷却期内同名不再下（卡片重渲染会反复出现）。
            # 放在 parse 块里：只解析不转的模式没有消息概念，也没有附件可言。
            # ⚠ 不能只在 `if pending:` 里调：一张卡片有多条明细时要排队逐个
            #   抓，队列的推进得靠**后面没有新消息的那些帧**（上一个抓到文件
            #   后，下一帧才放行下一个）。
            attach = _order_attach(self.ctx, pending)
            if attach:
                body["fetch_attach"] = attach
                # 字眼一起下发：手机端按它找屏上的附件行。
                body["fetch_keys"] = _fetch_keys_for(self.ctx, attach)
                # 候选链（用户 2026-11-01：带「明细」的行都要点一遍）。
                # 用 "||" 分隔：行里可能带逗号（「仅展示前30条」）。
                cands = (getattr(self.ctx, "attach_cands", {}) or {}).get(attach) or [attach]
                body["fetch_cands"] = "||".join(cands)
                log.info("发现附件「%s」，已下发抓取指令%s（候选 %d 个：%s；队列还剩 %d 个）",
                         attach, tail("attach_order"), len(cands), " → ".join(cands),
                         len(self.ctx.attach_queue))
        self._reply(200, body)

    # --- 画面解析（仅 dingtalk.android_parse = true）---

    def _ingest(self, text: str, *, filling: bool = False,
                frame_md5: str = "") -> tuple[list, str, bool]:
        """一帧节点树 → 本帧**新增**的消息单元。

        返回 ``(单元列表, 说明, 是否要补全)``。这三个值都不代表「已经发出去
        了」—— 单元交给 ``_emit`` 归并进 ``ctx.pending``，静默几秒后才由
        flusher 线程取最全的版本投递（见 ``pending.py``）。

        解析失败**不能**回 4xx/5xx：手机端收到错误会重传同一帧，而这一帧的
        形状问题是重传解决不了的，只会把日志刷满。所以记一条 WARNING、
        照常回 200，让采集继续。

        第三个返回值是给手机端的指令：True = 这一屏有消息被屏幕边界切掉，
        请往上翻一屏再报一次（见 ``android/.../Scroller.java``）。
        """
        try:
            frame = parse_frame(text)
        except ParseError as exc:
            log.warning("手机画面解析失败（这一帧跳过）：%s", exc)
            return [], "解析失败", False

        if not frame.is_chat:
            # 用户点开了 @ 面板、群信息、搜索页 —— 屏上是一排排人名和身份标签，
            # 结构和消息列表几乎一样。必须整帧丢掉，**连基线都不能动**：
            # 拿这些人名当基线，回到聊天页时会把整屏旧消息判成新消息。
            note = f"不是聊天页（{frame.page_reason}），本帧跳过"
            if frame.page == "other":
                # 明确的非聊天页（认出了「选择要@的人」这类文案）：这是用户的
                # 正常操作，日志只能到 INFO —— WARNING 会被钉钉告警处理器抓走，
                # 用户每点一次 @ 就收到一条手机告警。
                self.ctx.skip_streak = 0
                if note != self.ctx.last_skip_note:
                    log.info("安卓画面解析：%s", note)
                    self.ctx.last_skip_note = note
            else:
                # 认不出来的页面才是真问题：多半是钉钉改版把输入区文案改了，
                # 那样会「程序在跑、一条不转」。连续几帧都认不出就升级成
                # WARNING —— 让它进钉钉告警，别静默失效。
                self.ctx.skip_streak += 1
                if self.ctx.skip_streak == UNKNOWN_PAGE_ALERT_AFTER:
                    log.warning(
                        "连续 %d 帧认不出聊天页（%s）—— 钉钉可能改版了，"
                        "现在一条消息都不会转发。用 tools/android_capture.py "
                        "抓一帧看 page_reason", self.ctx.skip_streak, frame.page_reason)
                    self.ctx.last_skip_note = note
                    # 告警只是「让人知道」，手机还停在别的页面上没人拉。
                    # 2026-10-31 17:50：附件抓完后手机停在认不出的页面，
                    # 告警响了 5 分钟，最后靠用户手动切回 —— 不能再靠人。
                    self._auto_watch_unknown_page()
            return [], note, False

        self.ctx.skip_streak = 0
        self.ctx.last_skip_note = ""

        # ── 剔除本程序自己发到群里的运维通知 ──────────────────────────
        # `【系统】…` 那些「转发桥已启动 / 已停止 / 采集波动」是**自己发到
        # 钉钉群里的**，手机画面里当然也有它们。service 层早就拦住了不投递，
        # 但**拦住投递不等于没代价**：它们在手机上照样占列表高度。
        #
        # 2026-10-18 实测事故：可视区 1753 px，两条系统通知（227 + 578）吃掉
        # 805 px，真正的业务消息「拦截跟单日报」「按需派送履约风险提醒」被
        # 直接顶出屏幕 —— 采集端根本没机会看见它们，日志还全绿。
        # 程序每重启一次就往群里插两条通知，这类丢失会累积。
        #
        # 所以在**解析之后、进 diff 之前**就把它们剔掉：既不占锚点、不占
        # 新消息名单，也不影响「锚点上方是否来了新卡片」的判断。剔除是纯
        # 净化，不改任何状态，同一帧重报结果一致。
        dropped = self._drop_system_notes(frame)
        if dropped:
            log.info("安卓画面解析：屏上有 %d 条本程序自己的运维通知，"
                     "不参与比对（否则它们会把真业务消息顶出屏幕）", dropped)

        differ = self.ctx.differ
        if differ is None:
            differ = self.ctx.differ = FrameDiffer()

        # ── 上翻补全帧 ──────────────────────────────────────────────
        # 这一帧是手机按我们的要求往上翻一屏后报回来的，它与常规帧**方向相反**：
        # 常规帧的新消息在锚点**下方**（列表底部），补全帧的新内容在锚点**上方**
        # （被切掉的那半截长消息）。FrameDiffer 只会取 anchor 之后的部分，
        # 所以补全帧直接喂给它等于白翻 —— 实测「新增 0 条」。
        #
        # 这里绕开锚点，改用一条更保守的规则：**只转发当前屏里从未见过的那些
        # 单元**（靠已入库的指纹判断）。滚动时旧消息会重新出现，靠「没见过」
        # 就能把真正的补全内容挑出来；而补全内容天然只会是那条长消息的另外几段，
        # 不会把历史灌进群。
        if filling:
            # 补全帧只**挪锚点**、不做常规判定：它的新内容在锚点上方，喂给
            # FrameDiffer 等于白翻（实测「新增 0 条」）。
            #
            # 这一帧承载两件事，顺序不能换：
            #   ① 把「刚才那条长消息」又露出来的那段接进全文（_merge_long）；
            #   ② 捡起屏上其它从没见过的新内容（_ingest_fill）—— 锚点丢失时
            #      被挤掉的那条消息就靠这一步捡回来。
            #
            # 长消息**攒齐之前不许把它当新消息发**：多轮补全期间每一帧都会
            # 看到它，发出去就是「第 1 段 → 第 12 段 → 完整版」连着好几条刷屏
            # （旧实现里的「残句 + 完整消息」就是这么来的）。
            c = self.ctx
            progressed = False
            if c.long_head:
                progressed = self._merge_long(frame) is not None
            pending, note, _ = self._ingest_fill(frame)
            differ.note_scroll(frame.units)

            out = list(pending)
            extra_note = note
            want_more = False
            if c.long_head:
                c.long_rounds += 1
                if self._long_settled(frame):
                    done = self._long_done("已经翻到顶（顶边不再被切）")
                    if done is not None:
                        out.append(done)
                    extra_note = "长消息补全"
                elif c.long_rounds >= FILL_MAX_ROUNDS:
                    done = self._long_done(
                        f"翻满 {FILL_MAX_ROUNDS} 轮仍没到顶，按现状投递")
                    if done is not None:
                        out.append(done)
                    extra_note = "长消息补全（已到轮数上限）"
                elif self._long_timed_out():
                    done = self._long_done("等待超时")
                    if done is not None:
                        out.append(done)
                    extra_note = "长消息补全（已超时）"
                elif not progressed and c.long_rounds >= 2:
                    # 连着两轮一个字都没多 —— 再翻也是白翻（常见原因：手机
                    # 已经到列表顶、或者这条消息被后面的新消息顶走了）。别让
                    # 手机空翻，把已补到的发出去。
                    done = self._long_done("连续两轮没有新内容，停止翻页")
                    if done is not None:
                        out.append(done)
                    extra_note = "长消息补全（翻不动了）"
                else:
                    # 上面还有内容：请手机**接着往上翻**。这是多轮补全的核心
                    # ——手机端每收到一次 scroll 就翻一屏并重报一帧，直到我们
                    # 不再下发为止。
                    want_more = True
                    extra_note = f"长消息补全中（第 {c.long_rounds} 轮）"
                    log.info("安卓画面解析：长消息还没露全（已 %d 字，第 %d/%d 轮），"
                             "请手机继续上翻", len(c.long_head), c.long_rounds,
                             FILL_MAX_ROUNDS)
            return out, extra_note, want_more

        # ── H5 卡片：不再在这里扣住 ─────────────────────────────────
        # 钉钉机器人消息是 H5 卡片，节点坐标常年是坏的（frame.garbled），而且
        # **内容分批长出来**：第一帧可能只有标题，表格单元要再过几百毫秒才
        # 渲染完。抓到半成品就转发，群里收到的就是「有标题没数据」的残废消息
        # （2026-10-17 拦截跟单日报真机事故）。
        #
        # 早期在这里做「扣住待稳」：新内容第一次见先不发，让手机 800ms 后
        # 重报，等「同样的内容见到第二次」再发。**这个办法在真机上基本没
        # 工作过** —— 2026-10-18 全天日志：扣住第 1 眼 14 次、进到第 2 眼
        # 0 次、终投递 1 条。原因是它的前提不成立：H5 卡片每重绘一次，整批
        # 节点的指纹都会变，于是 `hold_key` 每帧都对不上，`seen` 永远等于 1。
        #
        # 现在改成「攒」：这一帧解析出什么就**原样收进 `ctx.pending`**，由
        # `PendingBuffer` 按 (发送者, 时间窗) 归并、只保留最全的版本、静默
        # 几秒后投递。好处是**不需要屏幕上连续两帧一致** —— 卡片长了几截就
        # 攒几截，重绘换指纹、锚点丢失、被系统通知挤出屏幕都不再有影响。
        #
        # 所以这里不再扣住，也不再请手机重报（少一轮往返，消息反而更早到）。
        garbled = frame.garbled
        # 一律先 peek（只判定、不动锚点）。原生帧过去直接 feed，锚点立刻被
        # 覆盖成本帧 —— 那样「锚点丢失后翻一屏试探」就永远对不上了（旧锚点
        # 已经被冲掉）。改成 peek 之后再决定：真发新消息 / 重建基线 / 试探，
        # 只有"确认不要试探"的分支才 commit。
        differ.set_view(frame.view)         # 判「锚点上方那条是不是新卡片」要用
        diff = differ.peek(frame.units)

        if garbled and (diff.baseline or diff.anchor_lost):
            if not diff.baseline:
                # H5 帧的锚点丢失同样先试探一次（理由同下面原生帧那处）。
                # **必须在 commit 之前**：commit 会把锚点覆盖成当前这屏，
                # 覆盖之后就再也对不上了，试探也就白翻了。
                #
                # ⚠ 2026-10-31（用户第二次说「有时还是上下快速跳动找锚点」）：
                #   当天日志 H5 锚点对不上 51 次、原生帧只有 3 次 —— 跳动几乎
                #   全部来自这条分支。H5 卡片的节点指纹每重绘一次就整批变，
                #   「锚点找不到」多半不是视口翻了页，而是同一条卡片换了形态；
                #   往上翻既找不回那条锚点（下一帧照样对不上，一路翻到 14 次），
                #   用户看到的就是屏幕上下乱跳。
                #   跳过翻页的条件（两条都要满足，宁严不宽）：
                #     · 屏上没有任何「不在最新处」特征（frame.hints 空）；
                #     · 屏上仍有**已知消息**做证据 —— 保证重建后的打捞层
                #       （_salvage_rebuild / _sweep_recent）能把这一屏的真新
                #       消息捡回来，而不是把它们登记成历史。
                if (not getattr(frame, "hints", ())
                        and not self.ctx.await_bottom
                        and self._has_known_unit(frame)):
                    log.info("安卓画面解析：H5 锚点对不上，但视口就在最新处"
                             "（屏上有已知消息做证据）—— 不翻页，就地重建基线")
                    # 走打捞：这一屏的真新消息不能随基线吞掉。
                    self.ctx.align_stale_gave_up = True
                else:
                    verdict = self._probe_decision(frame_md5,
                                                   self._frame_sig(frame.units))
                    if verdict == "wait":
                        # ⚠ 绝不能落到下面的 commit：commit 会把锚点覆盖成当前
                        #   这屏，之后翻回去也对不上了。
                        return [], "锚点丢失，等手机翻页安定", False
                    if verdict == "probe":
                        self.ctx.align_md5 = frame_md5
                        log.warning("安卓画面解析：H5 卡片的锚点对不上 —— "
                                    "已请手机往上翻一屏（累积第 %d 次，不回到原位）",
                                    self.ctx.align_tried)
                        # 第三位刻意是 False：翻页指令走 body["probe"]，
                        # **不是** body["scroll"]（scroll 是「翻上去再翻回来」，
                        # 位置不累积，用来找锚点等于原地打转）。
                        return [], "锚点丢失，往上翻一屏找", False
            if self._guard_fake_bottom(frame):
                return [], "视口不在最新处（假底部），已请手机回底再建基线", False
            differ.commit(frame.units)
            self.ctx.last_good_units = list(frame.units)
            if diff.baseline:
                log.info("安卓画面解析：已建立基线（当前屏 %d 条），"
                         "此后只转发基线之后的新消息%s", len(frame.units),
                         tail("baseline"))
                # 30 分钟内的真新消息不随基线吞掉（2026-10-27）
                s = self._sweep_deliver(frame, differ)
                if s:
                    return s
                return [], "已建立基线", False
            log.warning("安卓画面解析：锚点丢失，基线已重建到当前画面")
            # 一轮结束，试探状态必须一起清（否则下一帧丢失会立刻命中「过期」
            # 分支、白跳过一次翻页找回）。align_stale_gave_up 不在复位范围内。
            self._reset_probe()
            salvaged = (self._salvage_rebuild(frame)
                        if self.ctx.align_stale_gave_up else [])
            # 打捞窗口只在重建这一次：不复位的话，之后任何重建都会带上打捞，
            # 「翻历史/换群」触发的重建就会把历史灌进微信群。
            self.ctx.align_stale_gave_up = False
            # 30 分钟内的真新消息不随基线吞掉（_sweep_recent 自带证据与
            # 时间护栏，翻历史/换群时捞不出东西）。
            swept = [u for u in self._sweep_recent(frame)
                     if u.fp not in {x.fp for x in salvaged}]
            combined = list(salvaged) + swept
            if combined:
                out, _note, want_more = self._finish(combined, frame, differ)
                return out, ("锚点丢失（打捞 %d 条）" % len(combined)), want_more
            return [], "锚点丢失", False

        if diff.baseline:
            if self._guard_fake_bottom(frame):
                return [], "视口不在最新处（假底部），已请手机回底再建基线", False
            differ.commit(frame.units)          # peek 之后必须自己推进锚点
            self.ctx.last_good_units = list(frame.units)
            log.info("安卓画面解析：已建立基线（当前屏 %d 条），"
                     "此后只转发基线之后的新消息%s", len(frame.units),
                     tail("baseline"))
            # ── 基线帧上被切的长消息：补全，但**不发** ──────────────
            #
            # 旧实现这里只 log.info 一句就完了，理由是「建基线时滚动会把刚
            # 记下的基线弄脏」。那条顾虑是错的：`differ.commit()` 已经把锚点
            # 推到本帧，手机往上翻屏走的是 scroll-fill 通道，`_ingest_fill`
            # 根本不动基线（它只 peek），脏不了。
            #
            # 而不补的代价是实打实的：程序重启／新装那一刻，屏上正好卡着一条
            # 长消息 → 它进了基线 → 补全逻辑（要求 `new` 非空）一辈子不会再
            # 碰它 → 后半截永久丢失，且日志里只有一行 info，没人会注意到。
            # 真机 2026-10-18 20:10：这条 1181 字的卡片结尾停在
            # 「查看完整月报明细（Exce」，就是这么来的。
            #
            # 但要补全**不等于**要投递：它在基线里，多半几分钟前就发过了
            # （同一条 16:39 已转发过完整版）。所以打上 `long_baseline`，
            # 一路补全、收尾时只入库不投递。
            #
            # ⚠ 2026-10-27：基线里除了「多半发过的长消息」，还可能有**真的
            #   没见过的新消息**（程序重启 / 手机离线后恢复的第一屏）。先把
            #   30 分钟内的真新消息捞出来投掉（_sweep_deliver），长消息
            #   照旧只补全不入群 —— 两件事互不妨碍。
            swept_out, swept_note, swept_want = [], None, False
            s = self._sweep_deliver(frame, differ)
            if s:
                swept_out, swept_note, swept_want = s
            why = needs_scroll(frame)
            sus = []
            if why:
                sus, _ = self._long_suspects(frame.units, frame)
            # ── 钉钉把越界 bounds 夹回视口内 → 长卡片「看起来」完整 ──
            # clipped_units 认不出这种形状（y0 恰好等于视口顶）。真机
            # 2026-11-01 15:43：程序重启建基线，屏上正卡着一张没转发过的
            # 长卡片，基线把它吞了 → 用户只能手动翻手机（原话）。
            # 判据刻意保守，见 _tall_bottom_suspect。
            tall = None
            if not sus:
                tall = self._tall_bottom_suspect(frame)
                if tall is not None:
                    why = (f"最底一条 {len(tall.content)} 字、从视口顶铺到底"
                           "—— 疑似比屏幕高的卡片")
            if why and (sus or tall is not None):
                target = sus[-1] if sus else tall
                seen = self._delivered_before(target)
                log.info("安卓画面解析：基线帧里有一条疑似没显示全的长消息"
                         "（%s）—— 请手机补全一次（%s）", why,
                         "投递过，补全只记录" if seen else "从没投过，补全后照常转发")
                self._remember_long(target, why,
                                    top_cut=min(target.y0, target.y1)
                                    < frame.view[1] - 12)
                # 「只记录不转发」只适用于**发过的**历史；重启/掉线那一刻
                # 屏上的新卡片从来没投过，补全后必须照常进群（2026-11-01）。
                self.ctx.long_baseline = seen
                return swept_out, (swept_note or
                                   ("基线里的长消息，只补全不转发" if seen
                                    else "基线长消息未投递过，补全后转发")), True
            if why:
                log.info("安卓画面解析：当前屏有消息没显示全（%s）", why)
            if swept_out:
                return swept_out, swept_note, swept_want
            return [], "已建立基线", False
        if diff.anchor_lost:
            # 锚点丢失 ≠ 一定是翻历史。真机上最常见的两个原因恰恰是**漏发**：
            #   · 新消息太长，把上一帧的锚点整条挤出了屏幕；
            #   · 卡片重绘后锚点换了形态（近似匹配能兜住的都已兜住，剩下的
            #     是真的不在屏上了）。
            # 这两种情况下，手机只要**往上翻一屏**，锚点就会重新露出来，夹在
            # 中间那条被漏掉的消息也跟着出现。所以先不认输，让手机翻一屏再
            # 报一次（走的还是 scroll-fill 通道，手机端一行代码都不用改）。
            #
            # 防抖：同一帧（按 md5）只试探一次。翻完还对不上，说明确实是在
            # 翻历史或者换了群，那就重建基线、不再纠缠 —— 否则手机会在
            # 「翻上去、翻回来」之间无限来回。
            md5 = frame_md5
            verdict = self._probe_decision(md5, self._frame_sig(frame.units))
            if verdict == "wait":
                # 手机正在翻（或屏幕还没安定）：什么都不动，等下一帧。
                # 不 commit 是关键 —— 锚点留着，翻回去才可能对上。
                return [], "锚点丢失，等手机翻页安定", False
            if verdict == "probe":
                # 试探时不 commit —— 锚点必须留着，否则翻回来也对不上
                self.ctx.align_md5 = md5
                log.warning("安卓画面解析：上一帧的锚点在本帧里找不到 —— "
                            "已请手机往上翻一屏（累积第 %d 次，不回到原位）",
                            self.ctx.align_tried)
                # 同 H5 分支：走 body["probe"]，不走 body["scroll"]。
                return [], "锚点丢失，往上翻一屏找", False
            differ.commit(frame.units)          # 认输了，基线重建到当前画面
            self.ctx.last_good_units = list(frame.units)
            log.warning("安卓画面解析：上一帧的锚点在本帧里找不到（翻历史／换群／"
                        "一屏全换）—— 本帧不转发，基线已重建到当前画面")
            # ⚠ 重建基线 = 把屏上一切登记为「已知历史」，**这一屏里真正的新
            #   消息会连同基线一起被吞掉，且不入库**（forward_recent.py 也补
            #   不回来）。所以至少把屏上最新几条打进日志，事后有人问「XX 为
            #   什么没转」时有据可查 —— 之前这里是完全静默的。
            for u in (frame.units[-3:] if frame.units else []):
                log.warning("  基线重建前屏上最新一条（可能被吞）：%s：%s",
                            getattr(u, "sender", "") or "（未知）",
                            (getattr(u, "content", "") or "")
                            .replace("\n", " ")[:60])
            # ⚠ **重置基线之前，先把攒着的长消息投出去。**
            #   真机 2026-10-18 19:54（用户原话「翻了也不行」）：
            #   为补齐那条 1231 字的卡片，手机按指示往上翻了一屏；翻完锚点
            #   找不回，试探额度用完 → 走这里 commit 重建。结果**刚补全的
            #   全文被一起丢掉**，用户只看到「翻页之前那半截」。
            #   补全的成果不能因为「锚点对不上」这个跟它无关的理由作废。
            done = self._long_done("锚点丢失，基线重建前先投递已攒到的内容")
            # ⚠ 打捞（2026-10-26）：光把屏上最新几条打进日志救不了消息。
            #   但**只有静默期后（试探状态过期）的重建才允许捞** —— 活跃
            #   翻历史触发的重建捞了就是把历史灌进群。
            salvaged = (self._salvage_rebuild(frame)
                        if self.ctx.align_stale_gave_up else [])
            self.ctx.align_stale_gave_up = False   # 同上：打捞窗口只此一次
            # 30 分钟内的真新消息不随基线吞掉（_sweep_recent 自带证据与
            # 时间护栏，翻历史/换群时捞不出东西）。
            swept = [u for u in self._sweep_recent(frame)
                     if u.fp not in {x.fp for x in salvaged}]
            combined = list(salvaged) + swept
            if combined:
                out, _note, want_more = self._finish(combined, frame, differ)
                out = ([done] if done is not None else []) + list(out)
                return out, ("锚点丢失（打捞 %d 条）" % len(combined)), want_more
            if done is not None:
                return [done], "锚点丢失（已投递攒到的长消息）", False
            return [], "锚点丢失", False

        new = list(diff.units)

        # 这一帧正常判出了新内容 = 锚点是好的。清零试探计数，让下次真遇到
        # 「长消息挤掉锚点」时还有翻一屏的机会（不清零的话，一天里偶尔两次
        # 误试探就会把额度耗光，之后再丢消息就不再救了）。
        self._reset_probe()                 # 锚点回来了，这一轮结束

        if diff.scrambled:
            # 锚点上方夹进了新内容（新卡片从屏幕顶挤进来，见 android_parse
            # 的 _above_anchor_is_new）。这时「锚点」已经不可靠：不管把锚点
            # 推到帧尾还是推到旧锚点，下一帧都会重新判出同一条，同一条发
            # 好几遍（实测 4 帧抛 4 次）。直接重建基线到当前这一屏。
            #
            # ⚠ 旧实现在这里还要「扣住等渲染稳定」，现在不需要了：内容是
            #   攒进 `ctx.pending` 的，重复收同一批只会被归并成同一个 key，
            #   取最全的一版 —— 多收几遍不会多发。
            log.info("安卓画面解析：锚点上方来了新内容（卡片从屏幕顶挤进来），"
                     "锚点重建到当前这一屏")

        # 原生帧、H5 卡片都走同一条路：推进锚点，把新内容交给「攒齐再发」
        # 那一层。锚点必须推进 —— 不推进的话下一帧会重新判出同一批，
        # 缓冲虽然能归并掉，但会白白续命（把 quiet 一直往后推）。
        differ.commit(frame.units)
        self.ctx.last_good_units = list(frame.units)

        # 一屏装不下的长消息：树里只有屏幕上那截，滚出去的部分根本不存在。
        return self._finish(new, frame, differ)

    def _finish(self, new: list, frame, differ) -> tuple[list, str, bool]:
        """常规帧的收尾：处理「长消息只露了半截」这一种情况。

        ⚠ 这里**不再先发半截**。旧做法是「照常转发 + 请手机翻一屏补全」，
        但补全回来的前半截指纹与后半截不同（片段 ≠ 全文），
        ``is_same_message`` 也判不相等（它要求片段集合完全相等），于是前半截
        作为**一条新消息**又发了一遍。群里看到的是「残句 → 完整消息」，
        用户的原话是「内容特别长的消息转发还有问题」。

        改成「攒」：先把这条扣住不发，只请手机翻页；补全帧回来时由
        ``_merge_long`` 一轮一轮接进全文。等不到（超时 ``LONG_WAIT_MAX``）
        或翻满 ``FILL_MAX_ROUNDS`` 轮才按现状放行 —— 宁可发一条不完整的，
        也不能永远不发。
        """
        why = needs_scroll(frame)
        if not (why and new):
            # ⚠ 这里**不能**顺手把攒着的长消息清掉。
            #   旧实现清掉的理由是「用户可能已经划走了」。但多轮补全要翻
            #   五六次，中间必然夹着「这一帧没有截断、也没有新消息」的帧
            #   （手机翻页的中间态、系统通知插队、列表还没停稳）。
            #   在这种帧上清掉 = 前面几轮白翻，而且不会再有人请手机继续翻，
            #   长消息就永远停在「攒了 2 屏」的状态直到 45 秒超时。
            #   真正该清的地方只有两处：攒齐/翻满/超时（`_long_done`），
            #   或者补全帧里确认已经换了话题（见 `_ingest_fill`）。
            return new, "", False

        sus, rest = self._long_suspects(new, frame)
        if not sus:
            log.warning("安卓画面解析：%s —— 这一屏新转的消息可能不完整，"
                        "已请手机往上翻页补全", why)
            return new, "消息可能不完整", True

        # 只攒**最下面那条**（最新的那条长消息）：上面那些已经被后面的消息
        # 挤过去了，翻页也补不到它们的前半截。
        target = sus[-1]
        c = self.ctx
        # ⚠ 顺序不能反：**先判超时，再记住这条**。
        #   反过来写的话，同一条长消息每上报一帧都会被 `_remember_long` 把
        #   `long_at` 重置成「现在」，elapsed 永远是 0，超时这条路一辈子走
        #   不到 —— 手机会在「翻上去、翻回来」之间无限来回，消息永远不发。
        #   这正是 `test_long_message_sent_once_when_fill_never_comes` 抓到的
        #   那个 bug（`LONG_WAIT_MAX` patch 成 0 仍然返回「待补全」）。
        if c.long_head and c.long_key == target.fp and self._long_timed_out():
            log.warning("安卓画面解析：长消息等了 %.0f 秒还没补齐，按现状投递"
                        "（可能只有后半截，共 %d 字）", LONG_WAIT_MAX,
                        len(c.long_head))
            done = self._long_done("等待超时")
            out = ([done] if done is not None else []) + list(rest)
            return out, "消息可能不完整", True

        if not c.long_head or c.long_key != target.fp:
            top_cut = min(target.y0, target.y1) < frame.view[1] - 12
            self._remember_long(target, why, top_cut=top_cut)

        # ⚠ 扣住这条长消息时，**锚点必须退回它之前**。
        #   调用方在这之前已经 commit 过（把锚点推到了本帧结尾 = 长消息本身）。
        #   锚点落在长消息上会带来两个后果：
        #     · 下次同一条长消息再上报时 diff 判「没有新消息」，攒齐逻辑再也
        #       看不到它 → 永远补不上，也就永远不发；
        #     · 超时兜底那条路也走不到（new 一直是空的）。
        #   退回「长消息之前最后一条」之后，长消息每次上报都仍然是"新消息"，
        #   攒齐/超时两条路都能正常走到。
        before = [u for u in frame.units if u.fp != target.fp
                  and max(u.y0, u.y1) <= min(target.y0, target.y1)]
        if before:
            differ.commit(before)
        # 其余正常消息照发，不因为一条长消息把整批都压住。
        if rest:
            return rest, "长消息待补全", True
        return [], "长消息待补全", True

    @staticmethod
    def _known_hit(sender: str, content: str, known) -> bool:
        """打捞的「已投递」判据 —— 委托模块级 `known_hit`，别各写一份。"""
        return known_hit(sender, content, known)

    def _has_known_unit(self, frame) -> bool:
        """屏上还有没有「已知消息」（上一正常帧见过，或已投递过）。

        H5 锚点对不上时就地重建基线的**安全前提**：屏上至少要有一条已知
        消息，证明这是同一个会话、且重建后的打捞层有证据可依（只捞证据
        之下 = 底部进来的新消息）。一条都没有 = 换了群 / 整屏全新，宁可
        老老实实翻页找锚点，也不能把整屏当基线登记成历史。
        """
        known = self.ctx.delivered_texts
        good = {u.fp for u in (self.ctx.last_good_units or [])}
        for u in (frame.units or []):
            if u.fp in good or self._known_hit(u.sender, u.content, known):
                return True
        return False

    def _guard_fake_bottom(self, frame) -> bool:
        """建基线前的「假底部」护栏：视口不在最新处就先请手机回底。

        返回 True = 本帧已被拦下（不要 commit 基线）。

        2026-10-29 大江东群漏消息的根因：机器人 H5 容器里所有滚动手段
        失效，基线被建在「假底部」—— 视口停在历史中间就把屏上一切登记
        成已知历史，压在底下的真新消息之后只会出现在锚点**上方**，
        永远不会被转发。屏上有 99+ 未读徽标、「查看更多消息」「回到最新」
        这类节点特征（frame.hints，见 android_parse.ui_hints）时，先请
        手机回到底部（want_bottom → to_bottom），等回底帧再建基线。

        已在等回底（await_bottom）或这一帧就是回底帧（probe-bottom）时
        不再拦 —— 否则 H5 容器特征清不掉时会死循环；此时按现状建基线
        （有限让步），打捞层（_sweep_recent）会把屏上 30 分钟内的真新
        消息捞出来。
        回底彻底等不到时（手机没手势权限，_probe_decision 超时认输）靠
        ``ctx.bottom_gave_up`` 一次性放行：认输前 _reset_probe 已经把
        await_bottom 清掉，不放行就是「认输 → 被拦 → 再回底」的 57 秒
        一轮死循环，基线永远建不上、一条消息都转不出去（2026-10-31）。
        """
        if getattr(self.ctx, "bottom_gave_up", False):
            # 回底已经彻底等不到：再拦就是死循环（基线建不上 = 一条不转）。
            # 放行就地建基线，屏上内容交给打捞层 —— hints 命中时
            # _sweep_recent 整屏都是候选。
            self.ctx.bottom_gave_up = False
            self.ctx.await_bottom = False
            log.warning("安卓画面解析：回底帧始终没来（多半没有手势权限）—— "
                        "就地在当前屏建基线，屏上内容按打捞候选处理")
            return False
        if not getattr(frame, "hints", ()):
            return False
        if self.ctx.await_bottom or self.ctx.frame_reason == "probe-bottom":
            # 回底帧到了（或已在等）：标志一次性用掉，按现状建基线。
            self.ctx.await_bottom = False
            return False
        log.warning("安卓画面解析：屏上有「不在最新处」特征（%s）—— "
                    "不建基线，先请手机回到底部",
                    "、".join(frame.hints))
        self._ask_bottom()
        return True

    def _sweep_recent(self, frame) -> list:
        """基线（首帧 / 重建）时把屏上「30 分钟内且从没见过」的消息捞出来。

        背景（2026-10-27 用户原话「还是老是漏掉消息」）：首帧建基线和锚点
        丢失重建都会把屏上一切登记为「已知历史」—— 程序重启、手机离线、
        长卡片挤掉锚点，那一刻屏上真正的新消息就被吞掉，且**不入库**，
        forward_recent.py 都补不回来。打捞判据层层收窄：

          · 同会话证据：屏上至少有一条「入库过 / 投过」的单元，且只捞
            **证据之下**的单元（新消息只会从底部进来）。一条证据都没有时
            还有第二类证据：屏上的「99+ 未读徽标 / 查看更多消息 / 回到最新」
            UI 特征（frame.hints，2026-10-30 假底部事故后加）—— 它们同样
            证明「视口不在最新处」，此时整屏都是打捞候选；两类证据都没有
            = 换了群 / 全新安装，宁可不捞 —— 把别的群的消息打进目标群
            是事故；
          · 时间判据：单元头顶时间条必须读得出绝对时刻（「今天 17:57」）
            且距现在 ≤ SWEEP_MAX_AGE_S（30 分钟）。裸「今天」「星期一」
            「10-16」读不出时刻，一律不捞；
          · 没入库过（dup_check = storage.is_duplicate，重启后仍然有效）
            且没投过（known_hit，本进程内的近似去重记录）；
          · 被屏幕上/下沿夹掉的单元不捞 —— 内容是错的，交给长消息补全
            通道；
          · 条数上限 SWEEP_MAX_UNITS，限制爆炸半径。
        """
        units = frame.units or []
        if not units:
            return []
        check = self.ctx.dup_check
        known = list(self.ctx.delivered_texts)

        def _is_known(u) -> bool:
            if check is not None:
                try:
                    # 正常投递路径 raw_id = f"{fp}:{seq}"，批内第一次出现
                    # 就是 :1 —— 用它当「这条内容见过没」的判据。
                    if check(f"android:{u.fp}:1"):
                        return True
                except Exception:                  # noqa: BLE001
                    log.exception("dup_check 失败，按「见过」处理（宁可不捞）")
                    return True
            return known_hit(u.sender, u.content, known)

        good_fps = {u.fp for u in (self.ctx.last_good_units or [])}
        evidence = -1
        for i, u in enumerate(units):
            if u.fp in good_fps or _is_known(u):
                evidence = i
        hints = tuple(getattr(frame, "hints", ()) or ())
        if evidence < 0:
            if not hints:
                return []                 # 两类证据都没有，宁可不捞
            # UI 特征兜底（2026-10-30 假底部事故）：屏上没有任何已入库消息，
            # 但有「99+ 未读徽标 / 查看更多消息 / 回到最新」类节点 —— 视口
            # 不在最新处，整屏都是基线没盖住的候选。时间/去重/夹断护栏照旧。
            log.warning("安卓画面解析：屏上没有已入库消息，但发现「不在最新处」"
                        "特征（%s）—— 整屏按打捞候选处理",
                        "、".join(hints))
            cand = list(units)
        else:
            cand = list(units[evidence + 1:])
        bad = {id(u) for u in clipped_units(frame)}   # 被夹的交给补全通道
        now = datetime.now()
        kept = []
        for u in cand:
            if id(u) in bad:
                continue
            age = unit_age_seconds(u.ts, now)
            if age is None or age > SWEEP_MAX_AGE_S:
                continue
            if _is_known(u):
                continue
            kept.append(u)
        kept = kept[-SWEEP_MAX_UNITS:]
        if kept:
            log.warning("安卓画面解析：基线打捞 %d 条 30 分钟内没见过的消息"
                        "（交攒齐层去重）", len(kept))
        return kept

    def _sweep_deliver(self, frame, differ) -> tuple | None:
        """把打捞结果交攒齐层投出去。没有可捞的返回 None（调用方走原路）。"""
        swept = self._sweep_recent(frame)
        if not swept:
            return None
        out, _note, want = self._finish(swept, frame, differ)
        return out, ("基线打捞 %d 条（30 分钟内）" % len(swept)), want

    def _salvage_rebuild(self, frame) -> list:
        """静默期后重建基线前的打捞。

        **只在试探状态过期（ALIGN_STALE）触发的 give_up 上调用** —— 调用点
        见 `align_stale_gave_up`。活跃浏览（翻历史 / 换群）触发的重建绝不
        打捞，否则用户翻哪屏就把哪屏灌进微信群。

        场景（真机 2026-10-26 14:16→14:58）：锚点丢失的试探状态挂了 42 分钟
        （灭屏 / 用户拿走手机），期间爆发的消息在苏醒后的第一帧上直接
        give_up，重建基线把它们连同「已知历史」一起吞掉，用户只能手动翻页
        走补全通道捞回。打捞的依据全在「静默期」这个前提上：

          · 同一会话的证据：屏上至少有 1 条「见过」的单元 —— 要么
            与上一正常帧指纹相同（被顶上去的旧消息），要么与已投递记录
            同文（H5 卡片重渲染会换指纹，只能按正文近似认）。一条都没有
            = 换了群 / 深翻远史，宁可不捞 —— **认不出会话绝不能捞**，
            把别的群的消息打进目标群是事故；
          · 只捞**最靠下的证据之下**的单元：新消息只会从底部进来；往上
            翻出的旧内容在证据之上，不碰；
          · 已投递过的跳过（正文近似相同即算同一条，含换渲染形态）——
            依据 `ctx.delivered_texts`，比 NEAR_DUP_WINDOW 长得多。
        """
        units = frame.units or []
        good = self.ctx.last_good_units or []
        if not units or not good:
            return []
        good_fps = {u.fp for u in good}
        known = self.ctx.delivered_texts
        evidence = -1                     # 最靠下的「见过」单元下标
        for i, u in enumerate(units):
            if u.fp in good_fps:
                evidence = i
                continue
            if self._known_hit(u.sender, u.content, known):
                evidence = i
        if evidence < 0:
            return []                     # 没有同会话证据，宁可不捞
        below = units[evidence + 1:]
        if not below:
            return []
        kept = []
        for u in below:
            if not self._known_hit(u.sender, u.content, known):
                kept.append(u)
        kept = kept[-SALVAGE_MAX_UNITS:]
        if kept:
            log.warning("安卓画面解析：静默期后重建基线，打捞证据之下 "
                        "%d 条疑似新消息（交攒齐层去重）", len(kept))
        return kept

    @staticmethod
    def _frame_sig(units) -> str:
        """一帧的**内容签名**：发送者 + 正文的指纹。

        为什么不直接用帧 md5：md5 里混着节点坐标、H5 卡片的重绘痕迹、时间戳
        渲染差异 —— 同一屏内容也能每帧都不一样。2026-10-29 真机就是这么栽的：
        手机在 H5 卡片中间反复上翻，40 帧的字节数都是 180558/180567（同一屏），
        但 md5 每帧都变，于是「屏幕在动 → 继续翻」判据永远成立，一口气探了
        175 次，锚点一次也没找回。内容签名只由「谁说了什么」决定，重绘骗不过。
        """
        h = hashlib.md5()
        for u in units or []:
            h.update((getattr(u, "sender", "") or "").encode("utf-8", "replace"))
            h.update(b"\x1f")
            h.update((getattr(u, "content", "") or "").encode("utf-8", "replace"))
            h.update(b"\x1e")
        return h.hexdigest()

    def _probe_decision(self, frame_md5: str, sig: str = "") -> str:
        """锚点对不上时怎么处置：``probe`` / ``wait`` / ``give_up``。

        - ``probe``   请手机**往上翻一屏并停在原地**（累积，位置不还原）；
        - ``wait``    **什么都不做**：既不 commit、也不发指令，等下一帧；
        - ``give_up`` 认输，调用方 commit 重建基线（并已请求手机回到底部）。

        ⚠ 「等」必须是独立状态。旧实现只有「翻」和「认输」两态，冷却期内
          来的新帧（手机刚翻完送回的第一帧，恰好落在冷却期里）被直接判成
          认输 —— 长卡片挤掉锚点的那种漏转就是这么来的。屏幕变了 ≠ 手机
          没在翻，恰恰相反，它说明手机正在翻。

        ⚠ 判「翻没翻动」看**内容签名**而不是 md5：H5 卡片每重绘一次整批指纹
          都变，md5 跟着变，于是「屏幕在动」永远成立、试探永远不收敛
          （2026-10-29 真机：同一屏 40 帧、连续 175 次试探）。内容签名由
          发送者+正文算出，重绘骗不过它。

        判据顺序：
          · 已请求回底（``await_bottom``）→ give_up（这一帧就是从最新处来的）；
          · 试探状态过期（ALIGN_STALE）→ give_up；
          · 冷却期内 → wait（翻页还没安定，中间态帧没有判断价值）；
          · 内容跟上轮一模一样且累计到 PROBE_STUCK_MAX → give_up（翻不动）；
          · md5 跟上一次试探帧一样 → wait（屏幕一动没动，再发指令也是同一屏）；
          · 一轮翻到 ``PROBE_MAX`` → give_up；
          · 等待累计超过 ``PROBE_SETTLE_MAX`` → give_up（手机没响应/卡死兜底）；
          · 其余 → probe。
        """
        now = time.monotonic()
        if self.ctx.await_bottom:
            # 已经请手机「回到底部」了。**只有回底那一帧**才重建基线：
            # 视口还停在历史中间时就地重建 = 把历史当成新起点，下面到达的
            # 新消息永远看不见（只能等 15 分钟一轮的巡检来拉）。
            came_from_bottom = self.ctx.frame_reason == "probe-bottom"
            waited = now - self.ctx.await_bottom_at
            if came_from_bottom:
                log.info("安卓画面解析：手机已回到底部，基线重建到最新这一屏")
                self._reset_probe()
                return "give_up"
            if waited >= PROBE_SETTLE_MAX:
                log.warning("安卓画面解析：请手机回到底部后 %.0f 秒没等到回底帧"
                            "（多半没有手势权限）—— 就地重建基线", waited)
                self._reset_probe()
                # 放行牌：_reset_probe() 刚把 await_bottom 清掉，不标记的话
                # 调用方的 _guard_fake_bottom 会再拦一次，认输等于白认。
                self.ctx.bottom_gave_up = True
                return "give_up"
            return "wait"       # 还在往回滚的路上，先别动基线
        # ⚠ 试探状态会过期（见 ALIGN_STALE 注释）。过期不再翻页：锚点早已
        #   远在好几屏之外，翻了也翻不回；直接请手机回底，拿最新那一帧重建。
        if self.ctx.align_at and (now - self.ctx.align_at) > ALIGN_STALE:
            log.warning("安卓画面解析：锚点丢失的试探状态已挂了 %.0f 秒"
                        "（期间没有新帧）—— 不再翻页，回到底部重建基线并打捞",
                        now - self.ctx.align_at)
            self.ctx.align_stale_gave_up = True
            self._ask_bottom()
            return "wait"
        settled = (now - self.ctx.align_at) >= PROBE_COOLDOWN
        moved = bool(frame_md5) and frame_md5 != self.ctx.align_md5
        same_content = bool(sig) and sig == self.ctx.align_sig
        self.ctx.align_stuck = (self.ctx.align_stuck + 1) if same_content else 0

        if not settled:
            return self._wait_or_give_up(now, "翻页还没安定")
        if same_content and self.ctx.align_stuck > PROBE_STUCK_MAX:
            log.warning("安卓画面解析：连翻 %d 轮，屏上内容一个字没变 —— "
                        "翻不动了（已到列表顶／H5 卡片吃掉了滑动），"
                        "回到底部重建基线", self.ctx.align_stuck)
            self._ask_bottom()
            return "wait"
        if not moved:
            return self._wait_or_give_up(now, "屏幕没有再变化")

        if self.ctx.align_tried >= PROBE_MAX:
            log.warning("安卓画面解析：一轮翻了 %d 次都没找回锚点 —— "
                        "多半是在翻历史或换了群，不再试探，回到底部重建基线",
                        self.ctx.align_tried)
            self._ask_bottom()
            return "wait"

        self.ctx.align_tried += 1
        self.ctx.align_at = now
        self.ctx.align_sig = sig
        self.ctx.want_probe = self.ctx.align_tried
        self.ctx.align_wait_since = 0.0     # 新一轮翻页，等待重新计时
        return "probe"

    def _ask_bottom(self) -> None:
        """试探认输：请手机回到底部（最新），下一帧再重建基线。

        为什么必须回底：视口如果停在某张 H5 卡片中间，就地重建基线等于把
        「历史中间那一屏」当成新起点 —— 下面到达的新消息永远看不见，只能等
        15 分钟一轮的巡检来拉。先开回最新再建基线，才是「恢复到能继续采」。
        """
        self._reset_probe()                 # 先清（它会把 await_bottom 置回 False）
        self.ctx.want_bottom = True
        self.ctx.await_bottom = True
        self.ctx.await_bottom_at = time.monotonic()

    def _wait_or_give_up(self, now: float, why: str) -> str:
        """进入/继续「等」，等超时就认输。"""
        if not self.ctx.align_wait_since:
            self.ctx.align_wait_since = now
            log.info("安卓画面解析：%s —— 先等手机翻完再判（最多 %.0f 秒）",
                     why, PROBE_SETTLE_MAX)
        waited = now - self.ctx.align_wait_since
        if waited >= PROBE_SETTLE_MAX:
            log.warning("安卓画面解析：等了 %.0f 秒锚点仍未找回（%s）—— "
                        "不再等，回到底部重建基线", waited, why)
            self._ask_bottom()
            return "wait"
        return "wait"

    def _reset_probe(self) -> None:
        """一轮结束：丢失时刻、等待起点与翻页额度一起归零。

        ⚠ ``align_at``/``align_md5`` 必须一起清：give_up 后基线已重建到当前
        画面，锚点是新的了，老状态残留的话，下一帧锚点再对不上时会**立刻**
        再命中「过期」分支 —— 实测 2026-10-27 16:40 过期重建后，16:54 的帧
        打出「已挂 6184 秒」（起点其实是 15:11），还白白跳过了一次翻页找回。
        """
        self.ctx.align_wait_since = 0.0
        self.ctx.align_tried = 0
        self.ctx.align_at = 0.0
        self.ctx.align_md5 = ""
        self.ctx.align_sig = ""
        self.ctx.align_stuck = 0
        # 「等回底」是一次性的：重建基线之后视口就在最新处，标志必须清掉，
        # 否则之后每帧锚点丢失都会被判成「已回底」直接重建、再也不试探。
        self.ctx.await_bottom = False

    def _ingest_fill(self, frame) -> tuple[list, str, bool]:
        """上翻补全帧：把「锚点上方、此前没见过」的内容挑出来。

        为什么不能用 ``FrameDiffer``：它只会取锚点**之后**（下方）的部分，
        而补全帧要的恰恰是锚点**之前**（上方）—— 直接喂进去实测「新增 0 条」，
        等于白翻一屏。

        安全性靠两层：
          1. **必须有锚点**。锚点（上一帧的最后若干条）在本帧里找得到，说明
             这一帧确实只是「同一段对话往上滚动了一下」，不是换了群/翻了历史；
          2. **只取锚点上方的**。锚点下方那些本来就会被常规帧处理，重复捡会重发。

        仍然可能有误判：手机往上翻得多、翻出了从没转过的历史消息，它们会被
        当成补全内容发出去。所以手机端只翻一屏（62% 高度），且同一屏只补一次 ——
        这个量级下最多露出「长消息的另外几段」，不会翻出历史。

        补全帧**不动基线**：基线要留给「手机翻回原位」之后的那一帧。
        """
        if not frame.units:
            return [], "补全帧空", False
        differ = self.ctx.differ
        if differ is None or not differ.baseline_built:
            return [], "补全帧但没有基线，跳过", False

        at = differ.locate_anchor(frame.units)
        if at < 0:
            # 锚点找不到 = 这一屏跟上一帧对不上（多半是翻过头了）。
            # 不转发、也不动基线，等手机翻回来。
            return [], "补全帧对不上锚点，跳过", False

        # ⚠ 这里原来有一句「锚点找到了就 align_tried = 0」。它只清计数、不清
        #   align_at/align_md5，结果是：补全帧一来就把额度归零，主帧下一轮又
        #   从 0 开始 → 日志永远是「第 1 次」，PROBE_MAX 形同虚设。清试探状态
        #   只走 _reset_probe()（锚点确实恢复、或认输重建时），不在补全路径里
        #   做半套。

        # 两边都要看，因为「上翻一屏」在两个方向上都可能是漏消息的现场：
        #
        #   · 锚点**上方**：被切掉的长消息前半截（长消息补全，最常见）；
        #   · 锚点**下方**：这一帧新露出来的内容。锚点丢失以后常规帧一条都
        #     没发（全走了锚点丢失分支），所以锚点下面那些很可能也是漏的。
        #     实测原型：锚点在屏幕最上面，一条很高的新消息把它下面撑满，
        #     中间那条新消息被挤出可见区 —— 上翻一屏后它正好夹在锚点和
        #     长消息之间，只看「上方」是空的。
        #
        # 但下方只捡到**第一个被切掉的消息为止**：再往下的都是完整显示的
        # 新消息，它们本来就会在手机翻回列表底部时由常规帧正常投递（按顺序、
        # 按时延），在这里抢先发一遍会把顺序打乱，也白占了近似去重的窗口。
        known = set(differ._anchor)
        seen_text = list(differ._anchor_text)
        cut = {u.fp for u in clipped_units(frame)}
        picked = list(frame.units[:at])                  # 上方：整段都要
        for u in frame.units[at + 1:]:                   # 下方：捡到被切掉的为止
            picked.append(u)
            if u.fp in cut:
                break
        kept = []
        for u in picked:
            if u.fp in known:
                continue
            ns = norm_sender(u.sender)
            if any(norm_sender(s) == ns and is_same_message(c, u.content)
                   for s, c in seen_text):
                continue          # 换了渲染形态的同一条，指纹不同内容相同
            kept.append(u)
        if not kept:
            return [], "补全帧没有新内容", False
        log.info("安卓画面解析：上翻补全到 %d 条此前没看到的内容%s",
                 len(kept), tail("fill"))
        return kept, "补全帧", False

    # --- 长消息攒齐 ---

    def _drop_system_notes(self, frame) -> int:
        """把本程序自己发到群里的运维通知从画面里剔掉，返回剔了几条。

        判据只认 ``【系统】`` 前缀（与 ``notify.SYSTEM_PREFIX`` 同一个字符串）。
        这批文案是我们自己发的，service 层已经拦着不投递，让它们参与比对
        有害无益：

          · 它们在手机上**占列表高度** —— 可视区只有 1753 px，两条通知就能
            吃掉 805 px，真业务消息被顶出屏幕（2026-10-18 实测丢消息）；
          · 它们会让多出来的「新消息」把锚点往前推，其它消息因此被判旧。

        ⚠ 只改 ``frame.units``，**不动 frame 的其它字段**（view / garbled /
        page 都得留着）—— ``clipped_units``、``needs_scroll`` 都依赖它们。
        """
        keep = [u for u in frame.units if not _is_system_note(u.content)]
        dropped = len(frame.units) - len(keep)
        if dropped:
            frame.units = keep
        return dropped

    def _delivered_before(self, unit) -> bool:
        """这条消息投递过没有（按正常路径的 uid 查持久去重表）。

        没有判重依据（dup_check 未接）时按「投过」处理 —— 保守：宁可少发，
        也不能把一条历史灌进微信群。
        """
        check = self.ctx.dup_check
        if check is None:
            return True
        try:
            return bool(check(f"android:{unit.fp}:1"))
        except Exception:                              # noqa: BLE001
            log.exception("dup_check 失败，按「投过」处理（宁可不发）")
            return True

    def _tall_bottom_suspect(self, frame):
        """最底那条「从视口顶铺到底、又没投过」的消息 —— 疑似比屏幕高的卡片。

        ── 为什么 clipped_units 认不出它 ────────────────────────────
        钉钉把越界的 bounds **夹回视口内**，于是这张卡片「看起来」端端正正
        待在屏里（y0 == 视口顶，①②③ 全不命中）。真机 2026-11-01 15:43：
        程序重启建基线时屏上正卡着一张没转发过的长卡片，基线把它吞掉，
        用户只能手动翻手机才补回来。

        判据刻意保守 —— 这个判定错了就会白翻屏，甚至把上一条消息拼进正文：
          · 只看**最底下**那条（新消息只从底部进来）；
          · 顶边贴视口上沿（±24px），且高度 ≥ 60% 视口高；
          · 正文 ≥ 120 字（短内容一屏必然装得下，不可能是「被切的长文」）；
          · **从未投递过**（`_delivered_before`）—— 发过的历史不值得为它翻屏。
        """
        units = frame.units or []
        if not units:
            return None
        u = units[-1]
        top, bottom = frame.view[1], frame.view[3]
        vh = max(1, bottom - top)
        y0, y1 = min(u.y0, u.y1), max(u.y0, u.y1)
        if y0 > top + 24:                # 顶边没贴边 = 上沿可见，没被切
            return None
        if (y1 - y0) < vh * 0.6 and y1 < bottom - 24:
            return None                  # 高度与贴底都不占，不像长卡片
        if len(u.content or "") < 120:
            return None
        if self._delivered_before(u):
            return None
        return u

    def _long_suspects(self, units: list, frame) -> tuple[list, list]:
        """把本帧新消息分成「疑似被切掉的长消息」和「其余」。

        为什么不能先发半截再补：补全帧回来的前半截与已发的后半截**指纹
        不同**（片段 ≠ 全文），``is_same_message`` 也判不相等（要求片段集合
        完全相等），于是它被当成一条全新消息发出去。微信群收到的顺序是
        「残句（后半截）→ 完整消息」，用户看到的就是长消息转发错乱。

        判据只用两条，都来自真机存档：
          · 它就是 ``clipped_units`` 认出来的那条（贴边或被撑满）；
          · 或者正文里有明确的「被截断」迹象 —— 高度已经很大但文本很短，
            说明树里只拿到了部分内容。
        """
        cut = {u.fp for u in clipped_units(frame)}
        sus, rest = [], []
        for u in units:
            (sus if u.fp in cut else rest).append(u)
        return sus, rest

    def _remember_long(self, unit, reason: str,
                       *, top_cut: bool = False) -> None:
        """开始攒一条长消息：记下最初见到的半截，等手机翻页补全。"""
        self.ctx.long_key = unit.fp
        self.ctx.long_sender = unit.sender
        self.ctx.long_kind = unit.kind
        self.ctx.long_tail = unit.content or ""
        self.ctx.long_head = self.ctx.long_tail
        self.ctx.long_frags = [self.ctx.long_tail] if self.ctx.long_tail else []
        self.ctx.long_order = "down"
        self.ctx.long_rounds = 0
        self.ctx.long_top_seen = top_cut
        self.ctx.long_exhausted = False
        self.ctx.long_at = time.monotonic()
        log.info("安卓画面解析：这条消息没显示全（%s，现有 %d 字）—— 先不发，"
                 "请手机往上翻页取前半截（最多 %d 轮）",
                 reason, len(unit.content), FILL_MAX_ROUNDS)

    def _merge_long(self, frame) -> object | None:
        """把这一帧里「刚才那条长消息」的可见部分归并进攒着的全文。

        返回 ``None`` = 这一帧没让全文变长（可能压根没看到它，也可能只是
        同一段又露了一遍）。调用方靠返回值判「这一轮有没有收获」，**不靠它判
        「补完了没有」** —— 由 ``_long_settled`` 按「顶边还切不切」定。

        ── 多轮累积（2026-10-18）
        旧版本只拼两截（攒住的那半截 + 帧里最长的候选），所以一条 3 屏的正文
        只能得到「第 1 屏 + 第 3 屏」，中间那段没有地方放 —— 而屏幕上已经
        翻过去了，事后再也拿不回来，日志还全绿。用户原话「1.6 屏不够，
        要 3 屏才够，有的消息很长的」。

        现在改成：每轮把「本帧里同一发送者的**所有候选逐个**拿去对齐」，取能
        拼出最长全文的那个，再判它接在全文的哪一头。重叠的部分自然被吃掉，
        中间段一轮一轮补齐。

        ── 两个不能省的细节
        ① **不能只取本帧里最长的那段**。补全帧里同一条长消息通常有两块：上面
           那块是新翻上来的（内容更多，但因为顶部被切而**短**），下面那块是
           原本就在屏上的半截（文字更多）。按长度取最长的会稳定选中下面那块
           —— 它当然能对齐（它就是已攒的那段），于是「本轮没有进展」，手机
           白翻一屏，第 2 屏永远补不上。真机原型日志：
           `已攒 220 字 / 新的 40 字 ... 对不上，忽略`。
        ② 攒住的 ``long_tail`` 不能丢：手机翻回底部后屏幕上又是最初那半截，
           它会被 ``_align_fragment`` 认成「已知全文的一部分」，长度不变、
           全文不乱。这样来回翻也不会把已经补齐的正文弄丢。
        """
        c = self.ctx
        if not c.long_head:
            return None
        ns = norm_sender(c.long_sender)
        # ⚠ **不能只取本帧里最长的那一段**。补全帧里同一条长消息通常有两块：
        #   上面那块是新翻上来的（内容更多，但因为顶部被切而**短**），
        #   下面那块是原本就在屏上的半截（内容少，但节点高度大、文字反而多）。
        #   按长度取「最长的」会稳定选中下面那块 —— 它当然能对齐（它就是
        #   已攒的那段），于是「本轮没有进展」，手机白翻一屏，第 2 屏永远
        #   补不上。真机日志：`已攒 220 字 / 新的 40 字 ... 对不上，忽略`。
        #   所以必须**逐个候选都试一次对齐**，取能拼出最长全文的那个。
        best = None
        for u in frame.units:
            if norm_sender(u.sender) != ns:
                continue
            text = (u.content or "").strip()
            if not text:
                continue
            got = _align_fragment(c.long_head, text)
            if got is None:
                continue
            if best is None or len(got[0]) > len(best[1][0]):
                best = (u, got)
        if best is None:
            # 整个帧里跟已攒的那段都拼不上：多半是中间漏了一整屏（同一屏里
            # 翻得太多），或者这个人恰好又发了另一条长消息。**不硬拼** ——
            # 硬拼出来的是一段顺序错乱的正文，比缺一段更难被发现。
            log.info("安卓画面解析：补全帧里同发送者的正文跟已攒的（%d 字）"
                     "对不上，本轮忽略", len(c.long_head))
            return None
        u, (merged, direction) = best
        before = len(c.long_head)
        c.long_head = merged
        if len(c.long_head) > before:
            if direction != c.long_order and c.long_rounds:
                # 方向翻转 = 之前一直在往上翻，这次新内容出现在下面（手机
                # 往下滚了 / 用户自己动了屏幕）。记一笔，排障时能看出来。
                log.info("安卓画面解析：长消息补全方向变成「%s」"
                         "（之前是「%s」）", direction, c.long_order)
            c.long_order = direction
            log.info("安卓画面解析：长消息第 %d 轮补全 —— %d → %d 字（接在%s）",
                     c.long_rounds + 1, before, len(c.long_head),
                     "上面" if direction == "up" else "下面")
        return Unit(sender=c.long_sender or u.sender, content=c.long_head,
                    kind=c.long_kind or "text", y0=u.y0, y1=u.y1)

    def _long_settled(self, frame) -> bool:
        """这一轮补完之后，长消息已经齐了吗？

        ── 判据只能看「顶边还切不切」
        唯一可靠的信号是：**这条消息在屏幕顶端还有没有内容被挡住**。还在挡
        （``y0 < top``）= 上面还有，继续翻；贴顶了 = 已经是第一屏，齐了。

        几条容易踩反的分支：
          · 手机上翻到顶（列表滚到头）时，钉钉把 bounds 裁到可视区内，
            ``y0`` 正好等于 ``top`` —— 这时也判「齐了」。这是对的：再翻也
            没有了。翻到顶的副作用是第一屏那些旧消息会露出来，但
            ``_ingest_fill`` 只在**锚点上方**捡内容、且带指纹与文本去重兜着，
            不会把历史灌进群（见「翻过头一条都不许发」那条用例）。
          · 帧里根本没看到这条长消息 → 不判齐，走超时放行。多半是它被后面
            新来的消息顶出了屏幕，这时说「齐了」是瞎猜。
        """
        c = self.ctx
        ns = norm_sender(c.long_sender)
        unit = self._find_in_frame(frame, ns)
        if unit is None:
            return False
        _, top, _, _ = frame.view
        y0 = min(unit.y0, unit.y1)
        still_cut = y0 < top - 12        # 12px 吃掉气泡圆角与滚动惯性
        c.long_top_seen = still_cut
        return not still_cut

    @staticmethod
    def _find_in_frame(frame, ns: str):
        """本帧里这个发送者**最高的那个**单元（数组不保证按 y 排序）。

        ⚠ 必须自己比 y，不能取第一个命中的：补全帧里同一条长消息的两段
        都在，第一个命中的可能是下面那半截（贴底、看起来像「不切」），
        于是会误判成「齐了」，中间那段永远补不上。
        """
        best = None
        for u in frame.units:
            if norm_sender(u.sender) != ns:
                continue
            if best is None or min(u.y0, u.y1) < min(best.y0, best.y1):
                best = u
        return best

    def _long_timed_out(self) -> bool:
        """等了太久还没等到补全 —— 放行，发手头这半截。

        宁可发一条不完整的，也不能永远不发（手机可能没手势权限、用户可能在
        操作别的界面、群消息太快把前半截顶掉了）。

        `>=` 而不是 `>`：等待上限为 0 时必须立刻放行。写成 `>` 时
        「`monotonic()` 差刚好等于 0」这一瞬间会被判成没超时，全量跑测试时
        偶发命中（单跑不复现，因为两次调用之间总隔几微秒）。
        """
        return (time.monotonic() - self.ctx.long_at) >= LONG_WAIT_MAX

    def _long_done(self, why: str) -> object | None:
        """结束攒齐，返回「按现状能发的那个版本」。

        任何一条收尾路径（齐了 / 轮数用完 / 超时 / 翻不动了）都走这里，
        保证 **已经补到多少就发多少**，不会因为「没齐」就把辛苦补来的
        中间段丢掉。
        """
        c = self.ctx
        text = c.long_head or c.long_tail
        sender = c.long_sender
        kind = c.long_kind or "text"
        # ⚠ **先取值再清状态。** `_clear_long()` 会把 `long_baseline` 复位，
        #   顺序写反的话这里永远读到 False —— 2026-10-18 实测就是：
        #   基线补全出的 231 字正文被当成普通新消息投进业务群。
        baseline_only = c.long_baseline
        self._clear_long()
        if not text:
            return None
        if baseline_only:
            log.info("安卓画面解析：基线里的长消息补全完毕（%s）—— 共 %d 字，"
                     "只入库不转发（它在基线里，多半已经发过）", why, len(text))
            unit = Unit(sender=sender, content=text, kind=kind, y0=0, y1=0)
            unit.baseline_only = True       # 只入库：service.handle 拦投递
            return unit
        log.info("安卓画面解析：长消息收尾（%s）—— 共 %d 字，投递 1 条",
                 why, len(text))
        return Unit(sender=sender, content=text, kind=kind, y0=0, y1=0)

    def _clear_long(self) -> None:
        c = self.ctx
        c.long_key = ""
        c.long_at = 0.0
        c.long_rounds = 0
        c.long_head = ""
        c.long_order = "down"
        c.long_tail = ""
        c.long_sender = ""
        c.long_kind = ""
        c.long_top_seen = False
        c.long_exhausted = False
        c.long_frags = []
        c.long_baseline = False

    def _emit(self, units: list) -> int:
        """把消息单元**归并进缓冲**，返回收下的条数。

        ── 为什么不再直接投递 ─────────────────────────────────────────
        钉钉机器人消息是 H5 卡片，内容分批渲染（先标题、后表格）。拿「这一帧
        看到的样子」直接发，群里收到的就是残废消息。

        早期对策是「扣住等渲染稳定」（新内容第一次见先不发，等重报帧里指纹
        一致了再发）。它在真机上**基本没工作过**：2026-10-18 全天日志里
        「扣住第 1 眼」14 次、「进到第 2 眼」0 次、终投递 1 条。原因是它的
        前提不成立 —— H5 卡片每重绘一次，整批节点的**指纹都会变**，于是
        「还是同一条吗」永远答「不是」，`seen` 永远等于 1。

        现在改成不问「两帧一样吗」（H5 上无解），改问「这条消息我见过的最全
        版本是什么」：每帧的解析结果都按 (发送者, 时间窗) 归并进
        `PendingBuffer`，**保留更长的那个版本**，静默 `pending.QUIET` 秒后
        由 `_flush_pending` 投递最全的那版。

        于是卡片分批渲染、重绘换指纹、锚点丢失、被系统通知挤出屏幕 —— 这些
        真机故障全都不再有影响，因为它们都只影响「哪一帧看得到什么」，而这
        一层只关心「见过的所有版本里哪个最全」。
        """
        if not units or self.ctx.on_message is None:
            return 0
        buf = self.ctx.pending
        if buf is None:
            return 0
        msgs = units_to_messages(units, chat_name=self.ctx.chat_name or "",
                                 companies=self.ctx.companies)
        took = 0
        for msg in msgs:
            action = buf.offer(
                msg.sender_name, msg.content,
                chat_name=msg.chat_name or self.ctx.chat_name or "",
                join=_join_fragments,
                # 整条 Message 原样带上：缓冲只负责「哪一版最全」，
                # uid / msg_time / company 这些下游要用的字段别丢。
                message=msg)
            took += 1
            if action == "grow":
                log.info("安卓画面解析：%s 的卡片又长了一截（现 %d 字），"
                         "继续攒着不发%s", msg.sender_name, len(msg.content),
                         tail("pending"))
            elif action == "new":
                log.info("安卓画面解析：收到 %s 的一条消息（%d 字），"
                         "先攒着，静默 %.0f 秒后发最全的版本%s",
                         msg.sender_name, len(msg.content), buf.quiet,
                         tail("pending"))
        return took


def _make_handler(ctx: _Ctx):
    return type("_BoundHandler", (_Handler,), {"ctx": ctx})


class _GatewayServer(ThreadingHTTPServer):
    """HTTP 服务本体。

    刻意覆写 ``allow_reuse_address``：Python 自带的 HTTPServer 把它设成 True，
    而 Windows 上 SO_REUSEADDR 的语义是「允许另一个进程抢同一个端口」。
    那会让「端口已被占用」退化成一个静默的双绑定 —— 两个进程各接一半连接，
    表现为「手机有时连得上有时连不上」，极难排查。所以 Windows 上关掉；
    POSIX 上保留，避免重启时被 TIME_WAIT 卡住。
    """

    allow_reuse_address = (os.name != "nt")
    daemon_threads = True


# --------------------------------------------------------------- Source

class AndroidGatewaySource(Source):
    """等手机把消息 POST 进来的采集端。"""

    name = "android"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        dt = cfg.dingtalk
        self.host = str(getattr(dt, "gateway_host", "") or "").strip() or "0.0.0.0"
        try:
            self.port = int(getattr(dt, "gateway_port", DEFAULT_PORT)
                            or DEFAULT_PORT)
        except (TypeError, ValueError):
            self.port = DEFAULT_PORT
        self.token = str(getattr(dt, "gateway_token", "") or "").strip()
        self.parse = bool(getattr(dt, "android_parse", False))
        try:
            self.offline_after = float(getattr(dt, "android_offline_after", 300.0))
        except (TypeError, ValueError):
            self.offline_after = 300.0
        if self.offline_after < 0:
            self.offline_after = 0.0          # 负数按「关闭」处理，别让它变成「秒级误报」
        self.chat_name = (dt.chat_names or [""])[0] if dt.chat_names else ""
        self.companies = list(dt.companies or [])
        # 附件转发的触发字眼（行首前缀，纯文本）。随 fetch_attach 一起下发给
        # 手机端 —— 手机按同样的字眼找屏上的附件行，两边永远一致。
        self.attach_keys = _clean_attach_keys(getattr(dt, "attach_keys", None))
        # 总开关：关掉就不下发 fetch_attach（手机端不用知道这个键）。
        self.attach_enabled = bool(getattr(dt, "attach_enabled", True))
        # 万能字眼：短行里「名词（附件/明细/Excel…）+ 动作（下载/查看…）」
        # 就当作附件按钮。默认开 —— 用户的诉求是「别漏」。
        self.attach_auto = bool(getattr(dt, "attach_auto", True))
        # 附件落盘目录：跟数据库同目录下的 attachments/（service 侧能找到）。
        try:
            base = Path(cfg.resolve(cfg.storage.db_path)).parent
            self.files_dir = str(base / "attachments")
        except Exception:                              # noqa: BLE001
            self.files_dir = ""                        # 算不出就收不了文件，别炸
        # 自动巡检：盯的就是路由里的第一个群（chat_names[0]），不另开一个配置项 ——
        # 两处群名一旦不一致，手机每轮巡检都会点错群，而且从日志上很难看出来。
        self.watch_group = self.chat_name
        self.watch_min = _watch_minutes(getattr(dt, "android_watch_min", 0))

        self.count = {"requests": 0, "accepted": 0, "rejected": 0}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._watchdog_stop = threading.Event()
        self.online = False                    # 只用于退出时留个快照

    # --- 生命周期 ---

    def check(self) -> tuple[bool, str]:
        if not self.token:
            return False, (
                "安卓网关没配 token（dingtalk.gateway_token）—— 网关要监听局域网，"
                "手机才连得上；没有 token 等于任何人都能往你的微信群发消息。"
                "请在 config.yaml 的 dingtalk 段填一个随机字符串，"
                "并让手机端填同一个值")
        if not (0 < self.port < 65536):
            return False, f"网关端口不合法：{self.port}"
        if not self.chat_name and not self.companies:
            # 不是硬错误：路由里若用 「*」 或按公司匹配仍可用，只提示一下
            log.warning("未配置 dingtalk.chat_names，安卓端必须在报文里带 chat_name，"
                        "否则路由规则里的群名匹配会落空")
        ips = lan_ips()
        where = "、".join(f"http://{ip}:{self.port}" for ip in ips) or \
            f"http://<本机IP>:{self.port}"
        return True, f"监听 {self.host}:{self.port}｜手机端填：{where}"

    def run(self, on_message: MessageHandler, on_file=None,
            dup_check=None) -> None:
        """启动网关。``dup_check``（uid → bool）接 ``storage.is_duplicate``：
        基线打捞（_sweep_recent）靠它判断「这条入库过没」—— 内存里的
        delivered_texts 重启就空了，而打捞恰恰要覆盖重启后的第一屏。"""
        ok, why = self.check()
        if not ok:
            raise RuntimeError(why)

        ctx = _Ctx(token=self.token, chat_name=self.chat_name,
                   port=self.port,
                   companies=self.companies, on_message=on_message,
                   parse=self.parse, offline_after=self.offline_after,
                   watch_group=self.watch_group, watch_min=self.watch_min,
                   attach_keys=self.attach_keys,
                   attach_enabled=self.attach_enabled,
                   attach_auto=self.attach_auto,
                   differ=FrameDiffer() if self.parse else None)
        ctx.dup_check = dup_check
        # 附件抓取链路：目录 + 下游回调。任一缺失都只是「收不了附件」，
        # 不影响消息转发主流程。
        ctx.files_dir = self.files_dir or ""
        ctx.on_file = on_file
        # 「先攒后发」缓冲 + 把它定期排空的线程。
        # 关掉 parse 时不建（那种模式只收不转，没有消息要投）。
        if self.parse and on_message is not None:
            ctx.pending = PendingBuffer()
        self._ctx = ctx               # 给 flush_pending_now / 排障工具用

        # 手机端 APK 下载（/app、/app.apk）。目录在这里算而不是写死在模块里：
        # 打包成 exe 后模块文件在临时解包目录下，与用户看到的程序目录无关。
        set_apk_dirs(_apk_search_dirs())
        self._stop.clear()
        try:
            server = _GatewayServer((self.host, self.port), _make_handler(ctx))
        except OSError as exc:
            raise RuntimeError(
                f"网关端口 {self.host}:{self.port} 起不来：{exc}"
                f"（是不是已被别的程序占用？）") from exc

        self._server = server
        self._ready.set()

        ips = lan_ips()
        log.info("安卓网关已就绪：监听 %s:%d", self.host, self.port)
        for ip in ips:
            log.info("  手机端请填：http://%s:%d   （token 见 config.yaml）",
                     ip, self.port)
        if not ips:
            log.warning("  没探测到局域网 IP，请用 ipconfig 查本机地址")
        log.info("  连通性自测：手机浏览器打开 http://<本机IP>:%d/ping?token=<token>",
                 self.port)
        log.info("  收消息 POST /messages ｜ 收画面 POST /screen ｜ "
                 "本机查最新画面 GET /screen?token=<token>&raw=1")
        # 手机装 App 只需这一条地址：手机浏览器打开它就能下载，页面上还印着
        # 该填的 IP / 端口 / token，省掉「用肉眼从电脑抄 24 位 token」这一步。
        apk = _find_apk()
        if apk is not None:
            log.info("  手机装 App：浏览器打开 http://%s:%d/app  （当前包 %s，%d KB）",
                     ips[0] if ips else "<本机IP>", self.port,
                     apk.name, apk.stat().st_size // 1024)
        else:
            log.info("  手机装 App：/app 页面已就绪，但电脑上还没编出 APK —— "
                     "在 android 目录跑一次 python build.py")
        if self.parse:
            log.info("  画面解析已开启（dingtalk.android_parse = true）："
                     "第一帧只建基线不转发，之后只转发基线之后的新消息")
        else:
            log.warning("  注意：本模式**只负责收**。手机报上来的是消息还是画面，"
                        "决定了下游有没有东西可转发 —— 只收到画面时程序不会自动解析，"
                        "别把「网关在收」当成「业务在转」。"
                        "要开启解析：config.yaml 的 dingtalk.android_parse 设为 true"
                        "（先用 tools/android_capture.py 看过一帧真实画面的解析结果）")

        # ---- 「先攒后发」排空线程 ----
        if ctx.pending is not None:
            self._flusher_stop = threading.Event()
            threading.Thread(target=_flush_loop,
                             args=(ctx, self._flusher_stop),
                             name="android-flush", daemon=True).start()
            log.info("  消息缓冲已开启：同一张卡片分批渲染的内容会先攒起来"
                     "（静默 %.0f 秒），只发最全的那一版", ctx.pending.quiet)

        # ---- 自动巡检指令（下发给手机，手机自己跑定时）----
        if self.watch_min > 0 and self.watch_group:
            log.info("  手机自动巡检已开启：每 %d 分钟检查一次「%s」，不在这个群里就"
                     "自动点回去（期间手机会被抢前台，消息最多有这么久的盲区）",
                     self.watch_min, self.watch_group)
        elif not self.watch_group:
            # 群名空 = 巡检开了也没用（手机端见空群名会直接不动作）。
            # 单独说一句，避免用户把 watch_min 设成 10 却发现手机不动，
            # 然后花时间怀疑是 APK 版本或权限问题。
            log.warning("  手机自动巡检不生效：没配 dingtalk.chat_names，"
                        "手机不知道该回到哪个群 —— 填上目标群名即可")
        else:
            # ⚠ 不要写死「= 0」：原文案会误导两类人 ——
            #   ① 配置里压根没这个键（老配置/第一次用），日志却说「你设成了 0」，
            #      用户去翻配置找不到这一行，以为程序读错了配置；
            #   ② 手填了非档位值（如 7），被夹到 5 或按关闭处理，日志显示的
            #      也不是用户填的那个数。
            log.info("  手机自动巡检未开启（dingtalk.android_watch_min 未设置或为 0）："
                     "手机被切走或钉钉崩溃后不会自己恢复，只能靠人发现"
                     "（失联告警是唯一的提示）")

        # ---- 失联看门狗 ----
        self._watchdog_stop = threading.Event()
        if self.offline_after > 0:

            self._watchdog_stop.clear()
            threading.Thread(target=_watchdog,
                             args=(ctx, self.offline_after, self._watchdog_stop),
                             name="android-watchdog", daemon=True).start()
            log.info("  失联检测已开启：手机超过 %s 没有心跳就告警"
                     "（要准确检测，手机端需 0.2.0 及以上；旧版只会报「一次都没收到」）",
                     _fmt_span(self.offline_after))
        else:
            log.info("  失联检测已关闭（dingtalk.android_offline_after = 0）："
                     "手机端挂了不会有任何提示，只能靠群里没消息自己察觉")

        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            self._watchdog_stop.set()
            fs = getattr(self, "_flusher_stop", None)
            if fs is not None:
                fs.set()
            # 停止运行时把缓冲里还没到点的消息**照现状发掉**：它们已经被手机
            # 采集到了，丢掉就是真丢消息。等不到「静默」也无所谓 —— 宁可发
            # 一版可能不完整的，也不能因为程序退出而吞掉。
            if ctx.pending is not None:
                rest = ctx.pending.flush_all()
                if rest:
                    log.info("停止运行：把缓冲里还没发的 %d 条消息照现状发出",
                             len(rest))
                    try:
                        _deliver_ready(ctx, [e.extra.get("message")
                                             for e in rest
                                             if e.extra.get("message") is not None])
                    except Exception:
                        log.exception("停止运行：发缓冲消息失败")
            server.server_close()
            self._server = None
            self.count = dict(ctx.count)
            self.online = _online_now(ctx)   # 语义同 /ping：退出那一刻是否还在线

    def stop(self) -> None:
        self._stop.set()
        wd = getattr(self, "_watchdog_stop", None)
        if wd is not None:
            wd.set()
        srv = self._server
        if srv is not None:
            # shutdown() 必须与 serve_forever 不在同一个线程里调用，
            # 这里统一丢给临时线程，避免 stop() 被谁调用都安全。
            threading.Thread(target=srv.shutdown, daemon=True).start()

    def wait_ready(self, timeout: float = 5.0) -> bool:
        """等监听套接字真正就绪（自检工具与测试用）。"""
        return self._ready.wait(timeout)

    def flush_pending_now(self) -> int:
        """立刻把「先攒后发」缓冲里的消息全部投出去，返回条数。

        给测试和排障用：正常情况下由 flusher 线程按 ``FLUSH_TICK`` 节拍、
        等消息静默够久了才发；这里不等，直接清空。**不要在生产路径里调它**
        —— 它会把还在长的半成品也发出去。
        """
        ctx = getattr(self, "_ctx", None)
        if ctx is None or ctx.pending is None:
            return 0
        ready = ctx.pending.flush_all()
        if not ready or ctx.on_message is None:
            return 0
        return _deliver_ready(ctx,
                              [m for m in (_rebuild_message(e) for e in ready)
                               if m is not None])

    def describe(self) -> str:
        return (f"安卓网关 | 监听 {self.host}:{self.port} | "
                f"会话 {self.chat_name or '(报文自带)'} | "
                f"已收 {self.count['accepted']} 条")

    def switch_to_group_now(self) -> str:
        """让手机立刻把钉钉切回目标群，返回一句给人看的结果。

        为什么不在这里直接发 HTTP 给手机：手机没有常驻的服务端，只有它主动
        连电脑的份。所以走「挂指令」这条路 —— 下一跳心跳（60 秒内）就把
        ``watch_now=1`` 带下去，手机收到当场跑一轮 SelfHeal。

        ⚠ 只有**单独设了间隔**才需要这个。人点按钮时盯着屏幕等结果，
        60 秒的心跳周期太长，所以这里会顺手把心跳周期之外的一条捷径也用上：
        调用方（GUI）可以紧接着看 ``/ping`` 的 ``watch`` 字段是否刷新。
        """
        ctx = getattr(self, "_ctx", None)
        if ctx is None:
            return "网关还没启动，指令发不出去"
        if not ctx.watch_group:
            return ("没配目标群（dingtalk.chat_names），手机不知道该切到哪个群 "
                    "—— 去「设置」里填上群名")
        ctx.watch_now_seq += 1
        return (f"已排队：手机会在下一次心跳（60 秒内）切到「{ctx.watch_group}」。"
                f"想立刻看到效果，可以等手机上那一轮跑完，"
                f"或在手机上点「立即查找要转发的群」")
