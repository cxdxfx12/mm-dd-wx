"""附件「临时改投」文件传输助手的回归锁。

来源：用户 2026-11-01 20:26 ——
    「我是说今天要补发的发到文件助手，不是以后的」

所以这不是一个永久开关，而是「目标 + 截止时间」：
今天补回来的历史附件落到文件传输助手（别刷业务群），日常流水照旧进业务群。

四条边界：
1. 生效期内 ``msg_type=file`` → 只发文件传输助手；
2. 正文（text）**不跟着跑** —— 用户要的是附件换个地方，不是整条链路改道；
3. 过了截止时间 → **自动失效**，附件回到业务群。这条最关键：忘了关的后果
   是第二天附件静默全进文件助手，业务群收不到东西，几乎不可能当场发现；
4. 两个配置留空 → 一个字节的行为都不变（老配置文件照旧能跑）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bridge.config import Config, RouteConfig, WeChatConfig          # noqa: E402
from bridge.models import Message                                     # noqa: E402
from bridge.service import BridgeService                              # noqa: E402
from bridge.targets.base import SendResult, Target                    # noqa: E402

BIZ = "申通五星业务员培训群"
HELPER = "文件传输助手"


class FakeTarget(Target):
    name = "fake"

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send_text(self, target: str, text: str) -> SendResult:
        self.sent.append((target, text))
        return SendResult(True, target, "fake")


def make(seq: int, msg_type: str = "text") -> Message:
    return Message(
        chat_id="c1", chat_name="钉钉群", sender_id="u1", sender_name="附件",
        company="示例科技", content=f"【附件】明细{seq}.xlsx（12 行）",
        msg_type=msg_type, msg_time=datetime.now(), raw_id=f"attach-{seq}",
    )


def iso(delta: timedelta) -> str:
    return (datetime.now() + delta).replace(microsecond=0).isoformat()


class Base(unittest.TestCase):
    attach_target = ""
    until = ""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Config(
            storage=type("S", (), {"db_path": str(Path(self.tmp.name) / "s.db")})(),
            wechat=WeChatConfig(dry_run=True, interval=0.0, targets=[BIZ],
                                attach_target=self.attach_target,
                                attach_target_until=self.until),
            routes=[RouteConfig(name="t", companies=["示例科技"], targets=[BIZ],
                                template="{content}")],
        )
        cfg._base_dir = Path(self.tmp.name)
        self.target = FakeTarget()
        self.svc = BridgeService(cfg, target=self.target)

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def sent_to(self) -> list[str]:
        return [t for t, _ in self.target.sent]


class TestWindowOpen(Base):
    """截止时间还没到：今天这批补发走文件助手。"""
    attach_target = HELPER
    until = iso(timedelta(hours=4))

    def test_file_goes_to_helper(self):
        self.assertTrue(self.svc.handle(make(1, "file")))
        self.assertEqual(self.sent_to(), [HELPER])

    def test_text_still_goes_to_business_group(self):
        self.assertTrue(self.svc.handle(make(2, "text")))
        self.assertEqual(self.sent_to(), [BIZ])


class TestWindowExpired(Base):
    """截止时间已过：自动回到业务群，不需要人记着改配置。"""
    attach_target = HELPER
    until = iso(timedelta(minutes=-1))

    def test_file_falls_back_to_business_group(self):
        self.assertTrue(self.svc.handle(make(3, "file")))
        self.assertEqual(self.sent_to(), [BIZ])


class TestNotConfigured(Base):
    """两个配置都留空：行为与改动前完全一致。"""
    attach_target = ""
    until = ""

    def test_file_goes_to_business_group(self):
        self.assertTrue(self.svc.handle(make(4, "file")))
        self.assertEqual(self.sent_to(), [BIZ])


class TestBadDeadline(Base):
    """时间写错宁可发业务群，也不能让附件全部静默改道。"""
    attach_target = HELPER
    until = "今天晚上"

    def test_bad_deadline_is_treated_as_off(self):
        self.assertTrue(self.svc.handle(make(5, "file")))
        self.assertEqual(self.sent_to(), [BIZ])


if __name__ == "__main__":
    unittest.main()
