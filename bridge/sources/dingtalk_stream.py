"""钉钉 Stream 长连接采集端（推荐路线，mode: stream）。

基于钉钉官方 SDK dingtalk-stream：
    机器人 Stream 模式下，钉钉通过 WebSocket 长连接推送事件，
    无需公网 IP、无需 HTTPS 证书。

能力边界（务必了解）：
    机器人只能收到「发给它自己」的消息 —— 单聊发给机器人，或群里 @机器人。
    群聊仅接收文本，不支持接收文件与语音。
"""
from __future__ import annotations

import logging
import urllib.error
import urllib.request
from datetime import datetime

from ..models import Message
from .base import Source, infer_company

log = logging.getLogger("bridge.source.stream")

TOKEN_URL = "https://oapi.dingtalk.com/gettoken?appid={cid}&appsecret={secret}"
TOKEN_TIMEOUT = 8

# 钉钉 msgtype -> 内部类型
MSG_TYPE_MAP = {
    "text": "text",
    "picture": "image",
    "richText": "text",
}


class DingTalkStreamSource(Source):
    name = "dingtalk-stream"

    def __init__(self, cfg):
        self.cfg = cfg
        self.dt = cfg.dingtalk
        self._on_message = None

    # ---------- 转换 ----------

    def _to_message(self, data) -> Message | None:
        """把 SDK 的 ChatbotMessage 转成本项目的 Message。"""
        text = ""
        if data.text is not None and data.text.content:
            text = data.text.content.strip()
        elif data.message_type == "richText":
            text = "\n".join(data.get_text_list() or []).strip()

        msg_type = MSG_TYPE_MAP.get(data.message_type or "text", "unknown")
        if not text:
            # 图片/文件等无文本内容的消息，用占位描述，避免整条丢弃
            placeholder = {"image": "[图片]", "unknown": "[非文本消息]"}.get(msg_type)
            if not placeholder:
                log.debug("跳过无文本内容的消息：type=%s", data.message_type)
                return None
            text, msg_type = placeholder, "image" if msg_type == "image" else "unknown"

        msg_time = datetime.now()
        if data.create_at:
            try:
                msg_time = datetime.fromtimestamp(int(data.create_at) / 1000)
            except (ValueError, OSError, TypeError):
                pass

        chat_name = data.conversation_title or (
            "单聊" if data.conversation_type == "1" else "未知会话"
        )
        sender = data.sender_nick or ""

        return Message(
            source="dingtalk",
            chat_id=data.conversation_id or "",
            chat_name=chat_name,
            sender_id=data.sender_staff_id or data.sender_id or "",
            sender_name=sender,
            company=infer_company(self.dt.companies, chat_name, sender),
            content=text,
            msg_type=msg_type,
            msg_time=msg_time,
            raw_id=data.message_id or "",
        )

    def _dispatch(self, msg: Message) -> None:
        """消息到业务层的唯一出口，异常在此吞掉，避免影响长连接。"""
        if msg.company == "" and self.dt.companies:
            log.warning(
                "未能识别归属公司，将按路由规则处理 | 会话=%s 发送者=%s（配置的公司名：%s）",
                msg.chat_name, msg.sender_name, self.dt.companies,
            )
        try:
            self._on_message(msg)
        except Exception:
            log.exception("处理消息失败：%s", msg.uid)

    # ---------- Source 接口 ----------

    def check(self) -> tuple[bool, str]:
        if not self.dt.client_id or not self.dt.client_secret:
            return False, "缺少凭证：请配置 dingtalk.client_id / client_secret"
        try:
            import dingtalk_stream  # noqa: F401
        except ImportError:
            return False, "未安装 dingtalk-stream，执行：pip install dingtalk-stream"

        url = TOKEN_URL.format(cid=self.dt.client_id, secret=self.dt.client_secret)
        try:
            with urllib.request.urlopen(url, timeout=TOKEN_TIMEOUT) as resp:
                import json
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            return False, f"凭证校验失败（网络不可达：{exc.reason}）"
        except Exception as exc:
            return False, f"凭证校验异常：{type(exc).__name__}: {exc}"

        if payload.get("errcode") != 0:
            return False, f"凭证无效：errcode={payload.get('errcode')} {payload.get('errmsg')}"
        return True, "凭证有效，长连接就绪"

    def run(self, on_message) -> None:
        from dingtalk_stream import (
            AckMessage, AsyncChatbotHandler, ChatbotMessage,
            Credential, DingTalkStreamClient,
        )

        self._on_message = on_message
        source = self

        class _Handler(AsyncChatbotHandler):
            """同步 process 会被 SDK 提交到线程池执行，不会阻塞 WebSocket 事件循环。"""

            def __init__(self):
                super().__init__(max_workers=4)

            def process(self, callback):
                try:
                    data = ChatbotMessage.from_dict(callback.data)
                    log.info(
                        "收到钉钉消息 | 会话=%s 发送者=%s 类型=%s",
                        data.conversation_title or data.conversation_id,
                        data.sender_nick, data.message_type,
                    )
                    msg = source._to_message(data)
                    if msg is not None:
                        source._dispatch(msg)
                except Exception:
                    log.exception("解析钉钉回调失败")
                return AckMessage.STATUS_OK, "OK"

        client = DingTalkStreamClient(Credential(self.dt.client_id, self.dt.client_secret))
        client.register_callback_handler(ChatbotMessage.TOPIC, _Handler())
        log.info("Stream 长连接启动（SDK 内置断线重连），等待钉钉推送消息…")
        client.start_forever()      # 阻塞运行，内部自动重连

    def stop(self) -> None:
        # start_forever 为阻塞式，由进程信号终止（Ctrl+C）
        log.info("收到停止信号，长连接将在当前进程退出时关闭")
