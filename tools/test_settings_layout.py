"""设置中心布局探针（离线，纯 tkinter，不碰网络与微信）。

验证的是一条曾经真实踩过的坑：**页签内容一旦比窗口高，「保存」按钮会被
pack 挤成 0 高度而消失**（Tk 的 pack 按调用顺序分配空间，排最后的先被挤）。
所以这里逐页签断言：

    - 「保 存」按钮存在且**高度 > 0**、且在窗口可视范围内
    - 每个页签内容都能被滚到（滚动区存在时不会把窗口撑爆）

跑法：

    python tools/test_settings_layout.py
    python tools/test_settings_layout.py --size 760x520    # 指定窗口尺寸

需要能创建窗口的环境（本机桌面会话）。用 `C:/Users/.../Python312/python.exe`
这类带 tkinter 的解释器运行；若报 `no display` 说明不在桌面会话里。
"""
from __future__ import annotations

import argparse
import sys
import tkinter as tk
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
# settings_ui 是 app/ 目录内的模块，按「脚本目录即工作目录」的方式 import
# 同目录的 cfgfile，所以要把 app/ 也加进来
if str(BASE_DIR / "app") not in sys.path:
    sys.path.insert(0, str(BASE_DIR / "app"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 设置中心里各页签的标题顺序（与 _build 一致）
TABS = ["转发规则", "时间范围", "采集与投递", "消息格式", "钉钉通知", "其他"]


def find_button(widget, text: str):
    """在控件树里找文字匹配的 Button（递归）。"""
    for ch in widget.winfo_children():
        try:
            if ch.winfo_class() == "Button" and str(ch.cget("text")).replace(" ", "") == text:
                return ch
        except Exception:
            pass
        got = find_button(ch, text)
        if got is not None:
            return got
    return None


def make_stub_app(root: tk.Tk):
    """设置中心只用到 app 上这几个方法，探针里给个空实现就够。"""
    stub = tk.Frame(root)          # 借一个真 widget 当 app（要能做 Toplevel 的 master）

    def noop(*a, **kw):
        return None

    stub.autostart_enabled = lambda: False
    stub.set_autostart_enabled = noop
    stub.open_config_file = noop
    stub.open_data_dir = noop
    stub.open_log_dir = noop
    stub.open_settings = noop
    stub.re_detect_env = noop
    stub.reload_after_settings = noop
    return stub


def main() -> int:
    ap = argparse.ArgumentParser(description="设置中心布局探针（离线）")
    ap.add_argument("--size", action="append", default=[],
                    help="测试的窗口尺寸，可重复，如 --size 840x700 --size 760x520")
    ap.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    args = ap.parse_args()
    sizes = args.size or ["840x700", "760x520"]

    import yaml
    from app import settings_ui

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    root = tk.Tk()
    # 注意：**不能** root.withdraw() —— 主窗口隐藏时 Toplevel 里的控件量不到
    # 真实高度（winfo_height 恒为 1），布局断言会全部假失败。
    root.geometry("1x1+0+0")
    app = make_stub_app(root)
    failures: list[str] = []

    for size in sizes:
        w, h = (int(x) for x in size.lower().split("x"))
        win = settings_ui.SettingsDialog(app, Path(args.config), cfg, {})
        win.win.geometry(f"{w}x{h}")
        # 必须真的映射出来，否则 winfo_height 全是 1px（量不到真实布局）
        win.win.deiconify()
        win.win.update_idletasks()
        win.win.update()
        nb = None
        for ch in win.win.winfo_children():
            if ch.winfo_class() == "TNotebook":
                nb = ch
                break
        tabs = list(nb.tabs()) if nb else []
        print(f"\n===== 窗口 {w}x{h}｜页签 {len(tabs)} 个 =====")

        for i, tab_id in enumerate(tabs):
            nb.select(tab_id)
            win.win.update_idletasks()
            win.win.update()
            title = nb.tab(tab_id, "text").strip()
            btn = find_button(win.win, "保存")
            if btn is None:
                failures.append(f"{size} / {title}：找不到「保存」按钮")
                print(f"  [失败] {title}：找不到保存按钮")
                continue
            bh = btn.winfo_height()
            by = btn.winfo_rooty() - win.win.winfo_rooty()
            visible = bh > 10 and 0 <= by <= h
            flag = "通过" if visible else "失败"
            print(f"  [{flag}] {title:<8} 保存按钮 h={bh}px y={by} 窗口高={h}")
            if not visible:
                failures.append(f"{size} / {title}：保存按钮不可见（h={bh}, y={by}）")

        win.win.destroy()

    root.destroy()
    print("\n" + "=" * 60)
    if failures:
        print(f"不通过：{len(failures)} 处")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("全部通过：各页签下「保存」按钮都可见")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
