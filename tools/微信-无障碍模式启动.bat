@echo off
chcp 936 >nul
title 微信（强制开启无障碍模式）

rem ============================================================
rem  用途：以 QT_ACCESSIBILITY=1 启动微信，强制 Qt 建立无障碍控件树。
rem  背景：2026-10-18 微信 4.1.15.13 的主窗口退化成纯 Qt 空窗
rem        （类名 Qt51514QWindowIcon，没有 mmui:: 控件），
rem        转发桥读不到控件。本脚本绕过微信自己的无障碍自动检测。
rem
rem  【使用前提】先完全退出微信（托盘右键 -> 退出，
rem     并在任务管理器确认 Weixin.exe 全部消失），否则微信会
rem     只把已有窗口唤到前台，环境变量不生效。
rem ============================================================

echo.
echo   请先确认微信已经完全退出（托盘右键退出 / 任务管理器无 Weixin.exe）
echo.
pause

set QT_ACCESSIBILITY=1
set QT_LOGGING_RULES=qt.accessibility.*=true

echo.
echo   正在以无障碍模式启动微信...
echo   环境变量: QT_ACCESSIBILITY=%QT_ACCESSIBILITY%
echo.

start "" "C:\Program Files\Tencent\Weixin\Weixin.exe" --scene=startmenu

echo   已发出启动命令。请手动扫码登录，登录后回到对话里说一声，
echo   我会立即检测控件树是否恢复。
echo.
pause
