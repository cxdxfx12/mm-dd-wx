"""设置中心「保存」往返验收（离线，纯 tkinter，不碰网络与微信）。

设置中心的保存路径已经踩过两个真实的坑，这里一次性锁死：

1. **保存按钮被挤没**（见 test_settings_layout.py）
2. **老配置缺新键**：``set_scalar`` 只会改已存在的键，老版本生成的
   config.yaml 里没有 ``dingtalk.time_window`` 这类新键时，保存会报
   「以下配置项没能写入」且什么都没存上（2026-10-16 线上实录）

这里对每种配置形态都真实点一次「保存」，然后读回 config.yaml 断言：

    - 保存没有报「没能写入」
    - 该有的键都在，且值正确
    - notify.webhooks 的机器人清单与界面上一致
    - 再存一次内容不再变化（往返稳定）
    - routes 仍是最后一个顶层段（set_routes 的硬前提）

跑法：

    python tools/test_settings_save.py
"""
from __future__ import annotations

import re
import sys
import tempfile
import tkinter as tk
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
for _p in (str(BASE_DIR), str(BASE_DIR / "app")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import yaml                                                   # noqa: E402
from app import settings_ui                                   # noqa: E402
from bridge.config import load_config                         # noqa: E402
from bridge.notify import collect_robots                      # noqa: E402

URL_A = "https://oapi.dingtalk.com/robot/send?access_token=aaaa1111aaaa"
URL_B = "https://oapi.dingtalk.com/robot/send?access_token=bbbb2222bbbb"
SEC_B = "SECbbbbbbbbbbbbbbbbbbbb"

EXAMPLE = (BASE_DIR / "config.example.yaml").read_text(encoding="utf-8")

# 老版本生成的配置：dingtalk 缺 time_window / anchor_guard，
# notify 缺 webhooks / report_hourly 等新键 —— 就是线上踩到的那种
OLD = re.sub(r"  webhooks:\n    - .*\n    # - .*\n", "", EXAMPLE)
OLD = OLD.replace('  webhook: ""', '  webhook: "https://oapi.dingtalk.com/robot/send?access_token=oldrobot"')
OLD = re.sub(r"  time_window: .*\n", "", OLD)
OLD = re.sub(r"  anchor_guard: .*\n", "", OLD)

ROBOTS = [(URL_A, "", ""), (URL_B, SEC_B, "转发桥")]


def stub_app(root: tk.Tk):
    stub = tk.Frame(root)                 # 借一个真 widget 当 Toplevel 的 master
    noop = lambda *a, **kw: None          # noqa: E731
    stub.autostart_enabled = lambda: False
    for name in ("set_autostart_enabled", "open_config_file", "open_data_dir",
                 "open_log_dir", "open_settings", "re_detect_env",
                 "reload_after_settings"):
        setattr(stub, name, noop)
    return stub


def notify_section(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    i = next(i for i, l in enumerate(lines) if l.startswith("notify:"))
    j = next((j for j, l in enumerate(lines[i + 1:], i + 1)
              if l and not l.startswith((" ", "\t", "#"))), len(lines))
    return "\n".join(lines[i:j])


def run_case(root: tk.Tk, title: str, config_text: str, problems: list[str],
             mode: str = "", token: str = ""):
    tmp = Path(tempfile.mkdtemp(prefix="ddwx_save_"))
    path = tmp / "config.yaml"
    path.write_text(config_text, encoding="utf-8")
    cfg = yaml.safe_load(config_text) or {}

    win = settings_ui.SettingsDialog(stub_app(root), path, cfg, {})
    win.win.deiconify()
    win.win.update()

    # 界面上重建机器人行，并启用通知
    for item in list(win._robot_rows):
        win._del_robot_row(item["row"])
    for u, s, k in ROBOTS:
        win._add_robot_row(u, s, k)
    win.st["n_enabled"].set(True)
    # 数据来源：模拟用户在界面上切到手机模式并填 token
    if mode:
        win.st["src"].set(mode)
        win._on_source_radio()          # 真实点击单选时由 ttk 调用，这里手动触发
        if token:
            win.st["gw_token"].set(token)
    win.win.update_idletasks()
    win.win.update()

    win.save()
    first = path.read_text(encoding="utf-8")
    win.win.destroy()

    # ---- 再存一次：内容必须稳定 ----
    cfg2 = yaml.safe_load(first) or {}
    win2 = settings_ui.SettingsDialog(stub_app(root), path, cfg2, {})
    win2.win.deiconify()
    win2.win.update()
    win2.save()
    second = path.read_text(encoding="utf-8")
    win2.win.destroy()

    print(f"\n===== {title} =====")
    back = load_config(path)
    specs = [(r.url, r.secret, r.keyword) for r in collect_robots(back.notify)]
    print(f"  机器人      ：{len(specs)} 个")
    for u, s, k in specs:
        print(f"    · {u[-12:]}  加签={'有' if s else '无'}  关键词={k or '-'}")
    print(f"  time_window ：{back.dingtalk.time_window!r}")
    print(f"  anchor_guard：{back.dingtalk.anchor_guard!r}")
    print(f"  数据来源    ：{back.dingtalk.mode!r}"
          f"  网关 {back.dingtalk.gateway_host}:{back.dingtalk.gateway_port}"
          f"  token={'有' if back.dingtalk.gateway_token else '无'}"
          f"  解析={back.dingtalk.android_parse}")

    if problems:
        problems.append(f"{title}：保存时报了配置项写入失败")
    if specs != ROBOTS:
        problems.append(f"{title}：机器人清单读回不一致 {specs}")
    if back.dingtalk.time_window in (None, ""):
        problems.append(f"{title}：dingtalk.time_window 没写进去")
    if back.dingtalk.anchor_guard is None:
        problems.append(f"{title}：dingtalk.anchor_guard 没写进去")
    if mode and back.dingtalk.mode != mode:
        problems.append(f"{title}：数据来源没写进去（读回 {back.dingtalk.mode!r}）")
    if token and back.dingtalk.gateway_token != token:
        problems.append(f"{title}：网关 token 没写进去")
    if mode == "android" and not back.dingtalk.android_parse:
        # 切到手机模式却没开解析 = 收得到但一条都不转，是最难自己发现的那种坏
        problems.append(f"{title}：切到手机模式后没自动开启画面解析")
    if first != second:
        problems.append(f"{title}：保存两次内容有差异（往返不稳定）")
    tail = [l for l in second.rstrip().splitlines() if l.strip()][-1]
    if not tail.startswith("    "):
        problems.append(f"{title}：routes 不是最后一个顶层段（末行={tail!r}）")
    if "没能写入" in notify_section(path):
        problems.append(f"{title}：notify 段写坏了")


def main() -> int:
    root = tk.Tk()
    root.geometry("1x1+0+0")
    problems: list[str] = []
    run_case(root, "新装（样板生成，已有 webhooks 键）", EXAMPLE, problems)
    run_case(root, "老配置升级（缺 time_window / webhooks 等新键）", OLD, problems)
    # 数据来源切到手机：老配置里根本没有 gateway_* 这几个键，
    # 走的正是「改或插」，也是线上升级时最容易存不下来的那种形态。
    run_case(root, "老配置 + 切到手机钉钉", OLD, problems,
             mode="android", token="Tok3n-测试-abc")
    root.destroy()

    print("\n" + "=" * 60)
    if problems:
        print(f"不通过：{len(problems)} 处")
        for p in problems:
            print("  -", p)
        return 1
    print("全部通过：三种配置形态下保存往返均无损")
    return 0


if __name__ == "__main__":
    sys.exit(main())
