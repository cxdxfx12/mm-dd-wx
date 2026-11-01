@echo off
chcp 65001 >nul
title 手机操控 - 钉钉采集手机（PAL-AL00）
cd /d "%~dp0scrcpy-win64-v5.0.1"

rem 统一用项目里的 adb（tools\platform-tools），避免与 scrcpy 自带版本抢 5037 端口
set "ADB=%~dp0platform-tools\adb.exe"

scrcpy.exe --window-title="手机操控 PAL-AL00" -m 900 --max-fps 30 -w --no-audio

echo.
echo 窗口已关闭。若报 device not found，请检查 USB 线 / 手机是否授权调试。
pause
