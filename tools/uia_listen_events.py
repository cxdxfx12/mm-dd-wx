# -*- coding: utf-8 -*-
"""真正订阅 UIA 事件，让 ``UiaClientsAreListening()`` 返回 True。

## 为什么需要它

2026-10-18 排查微信「自绘空壳」时，从 `Weixin.dll` 里挖到关键字符串：

    UiaClientsAreListening
    WM_GETOBJECT
    clicfg_accessible_check_narrator
    gAccessibilityMode
    accessibility_enable_scheme / accessibility_mode_show_scheme

即微信（Qt）在**启动时**调用 `UiaClientsAreListening()`，只在返回 True 时才
建立完整的无障碍树。而 `UiaClientsAreListening()` 判定的是「**有没有 UIA 客户端
订阅了事件**」—— 光反复查询（`GetChildren` / `FindFirst`）**不算**订阅。

上一版 `uia_keepalive.py` 只轮询不订阅，所以实验无效。本脚本用
`IUIAutomation::AddAutomationEventHandler` 真正注册一个全局事件处理器，
这才是让系统进入「有无障碍客户端在监听」状态的正确姿势。

用法::

    python tools/uia_listen_events.py         # 订阅并常驻，Ctrl+C 退出
    python tools/uia_listen_events.py 300     # 挂 300 秒

⚠ 纯监听，不修改任何系统设置，不碰微信进程。
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path


def main() -> int:
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 600
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root.parent / "wxauto-main"))

    import comtypes
    import comtypes.client
    from comtypes.gen.UIAutomationClient import (
        CUIAutomation, IUIAutomation, IUIAutomationEventHandler,
    )

    uia = comtypes.client.CreateObject(CUIAutomation._reg_clsid_, interface=IUIAutomation)
    root_el = uia.GetRootElement()

    # 真正的事件处理器：系统据此判定「有客户端在监听」
    class Handler(comtypes.COMObject):
        _com_interfaces_ = [IUIAutomationEventHandler]

        def HandleAutomationEvent(self, sender, eventId):
            return 0

    handler = Handler()

    # 订阅整棵桌面树的 FocusChanged（最轻量、覆盖最广）
    UIA_AutomationFocusChangedEventId = 20005
    subs = []
    for eid, name in ((UIA_AutomationFocusChangedEventId, "FocusChanged"),):
        try:
            uia.AddAutomationEventHandler(eid, root_el, 1, None, handler)
            subs.append((eid, name))
            print(f"  已订阅事件 {name}（id={eid}）")
        except Exception as exc:
            print(f"  订阅 {name} 失败: {exc}")

    # 再加一个结构变化事件，双保险
    UIA_StructureChangedEventId = 20002
    try:
        uia.AddStructureChangedEventHandler(root_el, 1, None, handler)
        subs.append((UIA_StructureChangedEventId, "StructureChanged"))
        print("  已订阅事件 StructureChanged（id=20002）")
    except Exception as exc:
        print(f"  订阅 StructureChanged 失败: {exc}")

    print(f"\n[{datetime.now():%H:%M:%S}] UIA 事件订阅已生效，挂 {seconds} 秒……")
    print("  现在 UiaClientsAreListening() 应返回 True")
    print("  → 请在这期间完全退出微信并重新打开，然后 Ctrl+C 或等它自动结束\n")

    end = time.time() + seconds
    n = 0
    while time.time() < end:
        n += 1
        # 顺便看看微信有没有变成 mmui::MainWindow
        try:
            el = uia.PaneControl(searchDepth=8, ClassName="mmui::MainWindow") \
                if hasattr(uia, "PaneControl") else None
        except Exception:
            el = None
        if n % 20 == 0:
            print(f"  [{datetime.now():%H:%M:%S}] 监听中…… 剩余 {end - time.time():.0f}s")
        time.sleep(1.5)

    for eid, name in subs:
        try:
            uia.RemoveAutomationEventHandler(eid, root_el, handler)
        except Exception:
            pass
    print(f"[{datetime.now():%H:%M:%S}] 已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
