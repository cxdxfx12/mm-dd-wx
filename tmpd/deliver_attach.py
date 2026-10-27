# -*- coding: utf-8 -*-
"""一次性补投：17:57 那张「客户声音待履约汇总」附件。

文件是 adb 驱动手机手动下载后拉回来的（UC 那份文件名是乱码，内容一致），
这里走正式投递链路 handle_attachment：解析 xlsx → 渲染表格图 → 申通五星
业务员培训群。raw_id = attach-<sha1>，之后手机端即使再抓到同一份也不会重发。
"""
import os
import sys

ROOT = r"C:\Program Files\喵喵钉钉-微信消息转发桥"
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from bridge.config import load_config          # noqa: E402
from bridge.service import BridgeService       # noqa: E402

PATH = r"D:\wxauto-main\ddwx-bridge\tmpd\ddwx-attach.xlsx"
NAME = "客户声音待履约汇总_311392_20261027.xlsx"
CHAT = "【数字质控】浙江杭州萧山区大江东公司"

cfg = load_config(os.path.join(ROOT, "config.yaml"))
svc = BridgeService(cfg)
ok = svc.handle_attachment(PATH, NAME, chat=CHAT)
print("handle_attachment:", ok)
