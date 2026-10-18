# -*- coding: utf-8 -*-
"""查询 Windows 的 ``UiaClientsAreListening()`` 返回值。

微信（Qt）在启动时调用它，只在返回 True 时才建立完整无障碍树。
用它可以判断「当前系统有没有被认定为存在 UIA 客户端」。

用法::

    python tools/check_uia_listening.py           # 查一次
    python tools/check_uia_listening.py --watch   # 每 2 秒查一次
"""
from __future__ import annotations

import ctypes
import sys
import time


def listening() -> bool:
    """UIAutomationCore!UiaClientsAreListening，返回 BOOL。"""
    uiac = ctypes.WinDLL("UIAutomationCore.dll")
    fn = uiac.UiaClientsAreListening
    fn.argtypes = []
    fn.restype = ctypes.c_bool
    return bool(fn())


def main() -> int:
    if "--watch" in sys.argv:
        try:
            while True:
                print(f"UiaClientsAreListening = {listening()}")
                time.sleep(2)
        except KeyboardInterrupt:
            return 0
    val = listening()
    print(f"UiaClientsAreListening = {val}")
    print("  True  = 系统认为有 UIA 客户端在监听（微信启动时应会暴露控件树）")
    print("  False = 没有客户端在听（微信启动时不会建无障碍树 → 自绘空壳）")
    return 0 if val else 1


if __name__ == "__main__":
    raise SystemExit(main())
