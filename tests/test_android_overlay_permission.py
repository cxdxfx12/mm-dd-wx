"""「后台弹出界面」权限必须真的能开（2026-10-20 真机）。

## 现场

用户点「立即巡检」，手机毫无反应，电脑侧收到：

    15:36:47 · 手动 · 失败：拉不起钉钉（系统拒绝了启动请求）

`startActivity` 抛异常了。提示让他「去手机设置里开『后台弹出界面』」——
**但他手机设置里根本没有这个开关**。

## 根因

`AndroidManifest.xml` 里没声明 `SYSTEM_ALERT_WINDOW`。
Android 的规则是：**不声明这个权限，系统设置里不会出现该项**。
于是形成闭环死锁：

1. 巡检要抢前台 → 需要这个权限
2. 程序提示用户去开 → 但开关不存在
3. 用户开不了 → 巡检永远失败

## 修法

1. 清单声明 `SYSTEM_ALERT_WINDOW`（这是开关出现的前提）；
2. 界面加一个按钮，直接 `ACTION_MANAGE_OVERLAY_PERMISSION` 跳到本应用的开关页，
   不让用户自己在多层菜单里翻；
3. 已经开过了要说清楚，别让用户反复点。
"""
from __future__ import annotations

import unittest
from pathlib import Path

ANDROID = Path(__file__).resolve().parents[1] / "android"


class TestOverlayPermission(unittest.TestCase):
    def setUp(self):
        self.manifest = (ANDROID / "AndroidManifest.xml").read_text(encoding="utf-8")
        self.activity = (ANDROID / "src" / "com" / "hbdxm" / "ddwx"
                         / "MainActivity.java").read_text(encoding="utf-8")
        self.layout = (ANDROID / "res" / "layout" / "activity_main.xml").read_text(encoding="utf-8")
        self.strings = (ANDROID / "res" / "values" / "strings.xml").read_text(encoding="utf-8")

    def test_manifest_declares_system_alert_window(self):
        self.assertIn("android.permission.SYSTEM_ALERT_WINDOW", self.manifest,
                      "不声明它，系统设置里不会出现「后台弹出界面」开关")

    def test_button_exists(self):
        self.assertIn('android:id="@+id/btn_overlay"', self.layout)
        self.assertIn('name="btn_overlay"', self.strings)

    def test_button_opens_overlay_settings(self):
        body = self.activity.split("R.id.btn_overlay", 1)[1]
        body = body.split("R.id.btn_snapshot", 1)[0]
        self.assertIn("openOverlaySettings()", body)
        # 已经开过就别再跳，省得用户以为没生效
        self.assertIn("canDrawOverlays()", body)

    def test_jump_intent_targets_this_package(self):
        body = self.activity.split("private void openOverlaySettings", 1)[1]
        body = body.split("private void openBatteryWhitelist", 1)[0]
        self.assertIn("ACTION_MANAGE_OVERLAY_PERMISSION", body)
        self.assertIn('"package:" + getPackageName()', body,
                      "不带包名会跳到应用列表，用户还得自己再找一遍")

    def test_sdk_guard(self):
        body = self.activity.split("private void openOverlaySettings", 1)[1]
        body = body.split("private void openBatteryWhitelist", 1)[0]
        self.assertIn("Build.VERSION_CODES.M", body)


if __name__ == "__main__":
    unittest.main()
