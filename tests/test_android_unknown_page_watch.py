"""「连续认不出聊天页 → 自动下发切回目标群」的回归测试。

2026-10-31 真机事故：附件抓完后手机停在电脑侧认不出的页面，PC 只会
WARNING「连续 3 帧认不出聊天页」—— 告警响了 5 分钟没人管，最后靠用户
手动切回。现在告警的同一瞬间自动走 watch_now 通道（10 分钟冷却）。
"""

import json
import urllib.error
import urllib.request
from urllib.parse import quote

from tests.test_android_gateway import FRAME_1, TOKEN, GatewayCase


class TestAutoWatchOnUnknownPage(GatewayCase):
    """parse=True 的真网关，连发「认不出」的帧，盯 watch_now 指令。"""

    parse = True

    def post_screen(self, body, token: str | None = TOKEN, **headers):
        if isinstance(body, str):
            body = body.encode("utf-8")
        req = urllib.request.Request(self.url("/screen"), data=body, method="POST")
        if token is not None:
            req.add_header("X-Bridge-Token", token)
        req.add_header("Content-Type", "text/plain; charset=utf-8")
        for k, v in headers.items():
            req.add_header(k.replace("_", "-"), str(v))
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def post_unknown(self, n: int):
        """第 n 帧认不出的画面（每次换文本，避免 md5 撞车被当成重传）。"""
        return self.post_screen(FRAME_1.replace("96票", "%d票" % (96 + n)))

    def test_auto_watch_after_three_unknown_frames(self):
        for i in range(2):
            code, body = self.post_unknown(i)
            self.assertEqual(code, 200)
            self.assertNotIn("watch_now_pending", body)
        code, body = self.post_unknown(2)          # 第 3 帧 → 达到告警阈值
        self.assertEqual(code, 200)
        self.assertEqual(body.get("watch_now_pending"), 1)
        self.assertEqual(self.source._ctx.watch_now_seq, 1)

    def test_no_refire_within_streak_or_cooldown(self):
        for i in range(6):                         # streak 1..6，只在 ==3 触发
            self.post_unknown(i)
        self.assertEqual(self.source._ctx.watch_now_seq, 1)
        # 模拟「回到聊天页又跑偏」：streak 清零但还在冷却期内 → 不下发
        self.source._ctx.skip_streak = 0
        for i in range(6, 9):
            self.post_unknown(i)
        self.assertEqual(self.source._ctx.watch_now_seq, 1)
        # 冷却过了（把计时往回拨 10 分钟 + 1 秒）→ 再次下发
        self.source._ctx.last_auto_watch_at -= 601.0
        self.source._ctx.skip_streak = 0
        for i in range(9, 12):
            self.post_unknown(i)
        self.assertEqual(self.source._ctx.watch_now_seq, 2)

    def test_unknown_frames_accumulate_streak(self):
        """unknown 帧逐帧累计 streak（触发的火种），别的帧型不走这里。"""
        self.post_unknown(0)
        self.assertEqual(self.source._ctx.skip_streak, 1)
        self.post_unknown(1)
        self.assertEqual(self.source._ctx.skip_streak, 2)
