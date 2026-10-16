"""消息指纹：跨采集端共用的「这是不是同一条消息」判据。

为什么单独拆一个模块
    OCR 路径（`sources/dingtalk_ocr.py`）和安卓网关（`sources/android_gateway.py`）
    都要算指纹，而 `dingtalk_ocr` 依赖 Windows UI Automation、导入很重且只能在
    Windows 上跑。指纹算法一旦两边各写一份，早晚会漂移，而漂移的后果很具体：

        同一个发送者有两种写法 → 算出两个指纹 → 同一条消息被采两次 → 重复转发。

    这个坑本仓库已经踩过一次（OCR 把机器人昵称有时读成「签收跟单专员」、
    有时读成「签收跟单专员 机器人」，两条记录正文 md5 完全相同）。所以实现
    只留一份，放在这里。
"""
from __future__ import annotations

import hashlib
import re

SENDER_SPACE_RE = re.compile(r"\s+")
ROBOT_SUFFIX = "机器人"


def norm_sender(sender: str) -> str:
    """发送者归一化，**只用于指纹与相似度判定**，不改变入库/转发时的展示。

    两处实测过的读法抖动，都会把同一条消息算成两条：
      · 机器人昵称的后缀时有时无 —— 同一帧消息分别读成
        「签收跟单专员」和「签收跟单专员 机器人」，正文 md5 完全相同；
      · 昵称被 OCR 拆成两个文本框，拼起来时中间多一个空格。

    这里统一去掉空白与结尾的「机器人」，让它们对上。刻意**不**去掉
    更靠前的字，避免把「签收跟单专员」和「拦截跟单专员」这类不同机器人
    混成同一个。
    """
    s = SENDER_SPACE_RE.sub("", sender or "")
    return s[:-len(ROBOT_SUFFIX)] if s.endswith(ROBOT_SUFFIX) else s


def fp_of(sender: str, content: str) -> str:
    """发送者 + 内容 → 消息指纹。"""
    return hashlib.md5(
        f"{norm_sender(sender)}|{content}".encode("utf-8")
    ).hexdigest()


def unit_fp(unit: dict) -> str:
    """消息单元指纹。``unit`` 需含 ``sender`` 与 ``content``。

    发送者先过一遍 :func:`norm_sender` —— 否则机器人昵称多读/少读一个
    「机器人」后缀就会算出另一个指纹，同一条消息被当成两条重复采、重复转发。
    """
    return fp_of(unit["sender"], unit["content"])
