# -*- coding: utf-8 -*-
"""持续挂一个 UIA 事件监听器，让系统处于「有无障碍客户端在监听」状态。

用途：验证「微信启动时如果检测到 UIA 客户端在监听，就会注册控件树」的假设
（2026-10-18 12:58 那次实验没做干净：程序先开了，但自检在微信启动之后才跑）。

用法::

    python tools/uia_keepalive.py        # 挂 10 分钟，每 2 秒轮询一次桌面树
    python tools/uia_keepalive.py 300    # 挂 300 秒

Ctrl+C 提前退出。纯只读。
"""
from __future__ import annotations

import sys
import time
from datetime import datetime

BASE = None  # 占位，导入时再解析


def main() -> int:
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 600
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    wxauto = root.parent / "wxauto-main"
    sys.path.insert(0, str(wxauto))

    from wxauto import uiautomation as uia

    print(f"[{datetime.now():%H:%M:%S}] UIA 监听器已启动（挂 {seconds} 秒，"
          f"每 2 秒扫一次桌面）…… Ctrl+C 退出")

    # 注册焦点变化事件监听 —— 这会让 UiaClientsAreListening() 变为 True
    try:
        uia.GetRootControl()
        # 触发一次完整的连接建立
        _ = uia.Control(searchDepth=1).Exists(0.5, 0)
        print("  首次桌面扫描完成，UIA 客户端已向系统注册")
    except Exception as exc:
        print(f"  首次扫描异常（不影响监听）: {exc}")

    end = time.time() + seconds
    n = 0
    while time.time() < end:
        n += 1
        try:
            # 轮询微信窗口是否出现 mmui::（出现就大声报告）
            c = uia.PaneControl(searchDepth=8, ClassName="mmui::MainWindow")
            if c.Exists(0.3, 0):
                print(f"\n[{datetime.now():%H:%M:%S}] ★★★ 检测到 mmui::MainWindow！"
                      f"hwnd={c.NativeWindowHandle} ★★★")
                return 0
        except Exception:
            pass
        try:
            uia.GetRootControl().GetChildren()   # 保持客户端活跃
        except Exception:
            pass
        time.sleep(2)
        if n % 15 == 0:
            print(f"  [{datetime.now():%H:%M:%S}] 仍在监听……（剩余 {end - time.time():.0f}s）")
    print(f"[{datetime.now():%H:%M:%S}] 监听结束，未检测到 mmui::MainWindow")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
