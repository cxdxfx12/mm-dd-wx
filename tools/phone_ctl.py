#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""phone_ctl.py —— 用 adb 从 PC 操控安卓手机（截图 / 控件树定位 / 点击 / 滑动 / 按键 / 等文字出现）

为什么有它
----------
这个项目以前的排障手段只有「等手机自己上报画面」和「让 App 去点」，
一旦界面卡住（比如钉钉附件的 H5 预览页是空白），人就没法插手。
本脚本把 adb 的四个基本能力封装成一条条命令，问题现场能直接看、直接点。

前置条件
--------
1. tools/platform-tools/adb.exe 存在（本仓库已就位，版本 37.0.1）；
   也可以用环境变量 ADB 指定别的 adb。
2. 手机连着 USB 并且已授权调试（首次插线手机上会弹「允许 USB 调试」）。
   拔线使用时：手机开「开发者选项 → 无线调试」，先 `adb pair IP:端口 配对码`，
   再 `adb connect IP:端口`（无线调试页面会显示连接端口）。

常用命令
--------
    python tools/phone_ctl.py devices                 # 看设备在不在
    python tools/phone_ctl.py shot                    # 截一张图，返回本地路径
    python tools/phone_ctl.py ui --grep 运单           # 列出屏上带「运单」的控件和坐标
    python tools/phone_ctl.py find 运单明细            # 只查不点
    python tools/phone_ctl.py tap-text 运单明细        # 按文字点（推荐，不用猜坐标）
    python tools/phone_ctl.py tap-text 运单明细 --x-ratio 0.2   # 文字在整行左端时
    python tools/phone_ctl.py tap 358 1117            # 按绝对坐标点
    python tools/phone_ctl.py swipe 588 1900 588 700 400        # 手指上滑（看后面的消息）
    python tools/phone_ctl.py key back                # 返回键；还支持 home/wake/unlock/recent
    python tools/phone_ctl.py wait-text 到最新 --timeout 20     # 等某段文字出现
    python tools/phone_ctl.py scene                   # 截图 + 屏上文字摘要，一眼看清现状
    python tools/phone_ctl.py pull /sdcard/Download/x.xlsx

约定
----
* 截图默认缩到 640 宽存到 tmpd/phone/，同时保留原图（--no-scale 可关掉缩放）。
* uiautomator dump 拿的是**控件树**：有文字的节点一定有准确 bounds，
  比按截图目测坐标可靠得多，所以「按文字点」应作为首选。
* 所有会改变手机状态的动作（tap/swipe/key/input）都支持 --dry-run 先看要做什么。
* ⚠ 与采集 App 共存（2026-10-31 实测）：
  1. dump/截图会短暂占用无障碍通道，桥正在转发高峰时别高频跑；巡检自己会等。
  2. 操控完**务必把钉钉停在目标群聊天页**；停在别的页面（搜索结果、
     预览页）桥会判「不在聊天页」而停止转发（日志报 page_reason）。
  3. 从**搜索**进群会打开「带返回箭头的二级会话页」，正常进群路径是
     钉钉主界面 → 会话列表 → 点群。误入了就按两次返回再从列表进。
  4. 手机在钉钉里打开过的附件 H5 预览页可能一直是空白（预览服务加载
     不出来），文件不会落盘 —— 附件请走 PC 钉钉下载。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import unescape

# ---------------------------------------------------------------- 基础环境

HERE = Path(__file__).resolve().parent          # .../ddwx-bridge/tools
PROJECT = HERE.parent                           # .../ddwx-bridge
SHOT_DIR = PROJECT / "tmpd" / "phone"

SERIAL: str | None = None
DRY_RUN = False


def adb_path() -> str:
    env = os.environ.get("ADB")
    if env and Path(env).exists():
        return env
    for name in ("adb.exe", "adb"):
        cand = HERE / "platform-tools" / name
        if cand.exists():
            return str(cand)
    found = shutil.which("adb")
    if found:
        return found
    sys.exit("[phone_ctl] 找不到 adb。请把 platform-tools 放进 tools/ 下，"
             "或设置环境变量 ADB 指向 adb.exe")


ADB = adb_path()


def run(args: list[str], *, binary: bool = False, timeout: int = 60,
        check: bool = True, mutating: bool = True) -> bytes | str:
    """执行一条 adb 命令。返回 bytes（binary=True）或去掉首尾空白的 str。

    mutating=False 表示只读（截图 / dump / cat / pull），--dry-run 时照样执行，
    这样「预览要点的位置」才有意义；只有真正改手机状态的动作才被拦下。
    """
    cmd = [ADB] + (["-s", SERIAL] if SERIAL else []) + args
    if DRY_RUN and mutating:
        print("[dry-run]", " ".join(cmd))
        return b"" if binary else ""
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        sys.exit(f"[phone_ctl] 超时（{timeout}s）：{' '.join(args)}")
    if check and p.returncode != 0:
        err = (p.stderr or b"").decode("utf-8", "replace").strip()
        sys.exit(f"[phone_ctl] adb 失败（{p.returncode}）：{' '.join(args)}\n{err}")
    if binary:
        return p.stdout
    return p.stdout.decode("utf-8", "replace").strip()


def shell(cmd: str, **kw) -> str:
    return run(["shell", cmd], **kw)          # type: ignore[arg-type]


def shell_ro(cmd: str, **kw) -> str:
    """只读 shell（--dry-run 时也照常执行）。"""
    kw.setdefault("mutating", False)
    return run(["shell", cmd], **kw)          # type: ignore[arg-type]


# ---------------------------------------------------------------- 控件树

@dataclass
class Node:
    text: str
    desc: str
    cls: str
    clickable: bool
    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def label(self) -> str:
        return self.text or self.desc

    @property
    def center(self) -> tuple[int, int]:
        return ((self.x1 + self.x2) // 2, (self.y1 + self.y2) // 2)

    def point(self, x_ratio: float = 0.5, y_ratio: float = 0.5) -> tuple[int, int]:
        return (int(self.x1 + (self.x2 - self.x1) * x_ratio),
                int(self.y1 + (self.y2 - self.y1) * y_ratio))


_NODE_RE = re.compile(r"<node\b[^>]*>")
_ATTR_RE = re.compile(r'([\w:-]+)="([^"]*)"')
_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def parse_nodes(xml: str) -> list[Node]:
    out: list[Node] = []
    for tag in _NODE_RE.findall(xml):
        a = dict(_ATTR_RE.findall(tag))
        m = _BOUNDS_RE.search(a.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        if x2 <= x1 or y2 <= y1:            # 无面积的容器没有点击意义
            continue
        out.append(Node(
            text=unescape(a.get("text", "")),
            desc=unescape(a.get("content-desc", "")),
            cls=a.get("class", ""),
            clickable=a.get("clickable") == "true",
            x1=x1, y1=y1, x2=x2, y2=y2,
        ))
    return out


_UI_DUMP_PATH = "/sdcard/_ddwx_phone_ctl.xml"


def dump_nodes() -> list[Node]:
    shell_ro(f"uiautomator dump {_UI_DUMP_PATH} >/dev/null 2>&1", check=False, timeout=30)
    raw = run(["exec-out", "cat", _UI_DUMP_PATH], binary=True, timeout=30, mutating=False)
    shell_ro(f"rm -f {_UI_DUMP_PATH}", check=False)
    if not raw:
        sys.exit("[phone_ctl] 取不到控件树（手机可能锁屏 / 界面在播放视频 / 无障碍被关）")
    return parse_nodes(raw.decode("utf-8", "replace"))


# ---------------------------------------------------------------- 截图

def screencap_png() -> bytes:
    data = run(["exec-out", "screencap", "-p"], binary=True, timeout=60, mutating=False)
    if not data.startswith(b"\x89PNG"):
        # 个别机型 exec-out 会被 shell 污染，退回「存手机再 pull」
        tmp = "/sdcard/_ddwx_phone_ctl.png"
        shell_ro(f"screencap -p {tmp}")
        data = run(["exec-out", "cat", tmp], binary=True, timeout=60, mutating=False)
        shell_ro(f"rm -f {tmp}", check=False)
        if not data.startswith(b"\x89PNG"):
            sys.exit("[phone_ctl] 截图失败（不是 PNG 数据）")
    return data


def save_shot(path: Path | None = None, scale: int = 640) -> Path:
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = path or SHOT_DIR / time.strftime("shot-%Y%m%d-%H%M%S.png")
    path = path if path.is_absolute() else (PROJECT / path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(screencap_png())
    if scale:
        try:
            from PIL import Image
            im = Image.open(path).convert("RGB")
            w, h = im.size
            if w > scale:
                im.thumbnail((scale, int(h * scale / w)))
                small = path.with_name(path.stem + "_small.png")
                im.save(small)
                print(f"[shot] {path}  ({w}x{h})  缩略图 {small}")
                return small
        except Exception:                    # noqa: BLE001 —— 没装 PIL 就只给原图
            pass
    print(f"[shot] {path}")
    return path


# ---------------------------------------------------------------- 命令实现

def cmd_devices(_a) -> None:
    print(run(["devices", "-l"], mutating=False))


def cmd_shot(a) -> None:
    save_shot(Path(a.out) if a.out else None, 0 if a.no_scale else a.scale)


def cmd_ui(a) -> None:
    nodes = dump_nodes()
    kw = (a.grep or "").strip()
    shown = 0
    for n in nodes:
        lab = n.label
        if not lab:
            continue
        if kw and kw not in lab:
            continue
        flag = "可点" if n.clickable else "    "
        print(f"{flag}  {lab[:60]!r:<62} center={n.center}")
        shown += 1
    print(f"[ui] 共 {len(nodes)} 个有面积节点，命中 {shown} 个文字节点")


def cmd_find(a) -> None:
    nodes = dump_nodes()
    hits = [n for n in nodes if n.label and a.text in n.label]
    if not hits:
        sys.exit(f"[find] 屏上没有包含 {a.text!r} 的文字节点")
    for n in hits:
        print(f"{n.label[:60]!r}  bounds=({n.x1},{n.y1})-({n.x2},{n.y2})  "
              f"center={n.center}  clickable={n.clickable}")


def _do_tap(pt: tuple[int, int], note: str = "") -> None:
    x, y = pt
    print(f"[tap] ({x},{y}) {note}".rstrip())
    if DRY_RUN:
        print("[dry-run] adb shell input tap", x, y)
        return
    shell(f"input tap {x} {y}")


def cmd_tap(a) -> None:
    _do_tap((a.x, a.y))


def cmd_tap_text(a) -> None:
    nodes = dump_nodes()
    hits = [n for n in nodes if a.text in n.label]
    if not hits:
        sys.exit(f"[tap-text] 屏上没有 {a.text!r}，先跑 `ui --grep` 看看")
    # 多个命中时取最靠下（列表类界面通常最新/最相关在下方），可用 --index 指定
    hits.sort(key=lambda n: n.y1)
    n = hits[a.index if a.index < len(hits) else -1]
    _do_tap(n.point(a.x_ratio, 0.5), f"← {n.label[:40]!r}")
    if not a.no_wait:
        time.sleep(a.wait)


def cmd_swipe(a) -> None:
    print(f"[swipe] ({a.x1},{a.y1}) → ({a.x2},{a.y2}) {a.ms}ms")
    if DRY_RUN:
        return
    shell(f"input swipe {a.x1} {a.y1} {a.x2} {a.y2} {a.ms}")
    time.sleep(a.wait)


_KEYMAP = {
    "back": "KEYCODE_BACK",
    "home": "KEYCODE_HOME",
    "recent": "KEYCODE_APP_SWITCH",
    "menu": "KEYCODE_MENU",
    "wake": "KEYCODE_WAKEUP",
    "sleep": "KEYCODE_SLEEP",
    "enter": "KEYCODE_ENTER",
    "del": "KEYCODE_DEL",
    "unlock": "KEYCODE_MENU",       # 无密码锁屏：亮屏后按一下菜单即可划开
}


def cmd_key(a) -> None:
    code = _KEYMAP.get(a.name.lower())
    if not code:
        sys.exit(f"[key] 不认识 {a.name!r}，可用：{', '.join(_KEYMAP)}")
    if a.name.lower() == "unlock":
        shell("input keyevent KEYCODE_WAKEUP")
        time.sleep(0.4)
    print(f"[key] {a.name} → {code}")
    if DRY_RUN:
        return
    shell(f"input keyevent {code}")
    time.sleep(a.wait)


def cmd_input(a) -> None:
    if not a.text.isascii():
        sys.exit("[input] adb 的 input text 不支持中文。"
                 "中文请装 ADBKeyBoard 后用广播输入，或直接在手机上打字。")
    esc = a.text.replace(" ", "%s")
    print(f"[input] {a.text!r}")
    if DRY_RUN:
        return
    shell(f'input text "{esc}"')


def cmd_wait_text(a) -> None:
    t0 = time.monotonic()
    while True:
        nodes = dump_nodes()
        if any(a.text in n.label for n in nodes):
            print(f"[wait-text] 已出现 {a.text!r}（{time.monotonic() - t0:.1f}s）")
            return
        if time.monotonic() - t0 >= a.timeout:
            sys.exit(f"[wait-text] 等了 {a.timeout}s 仍没出现 {a.text!r}")
        time.sleep(a.interval)


def cmd_scene(a) -> None:
    img = save_shot(Path(a.out) if a.out else None, 0 if a.no_scale else a.scale)
    nodes = dump_nodes()
    texts = [n.label for n in nodes if n.label]
    print(f"[scene] 图：{img}")
    print(f"[scene] 屏上 {len(texts)} 段文字，前 40 段：")
    for t in texts[:40]:
        print("   ", t[:80])


def cmd_pull(a) -> None:
    dest = Path(a.dest) if a.dest else (SHOT_DIR / Path(a.remote).name)
    if not dest.is_absolute():
        dest = PROJECT / dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    remote = fix_msys_path(a.remote)
    run(["pull", remote, str(dest)], mutating=False)
    print(f"[pull] {remote} -> {dest}")


def fix_msys_path(remote: str) -> str:
    """还原被 Git Bash (MSYS) 转换坏的手机路径。

    在 Git Bash 里跑 `phone_ctl.py pull /sdcard/x`，/sdcard/x 会被改写成
    「C:/Program Files/.../PortableGit/sdcard/x」再交给 Python —— 本脚本用
    subprocess 调 adb 没事，但**收到的参数已经坏了**。这里把常见手机路径
    前缀找回来，几种一起兜住。
    """
    if os.name != "nt":
        return remote
    for marker in ("/sdcard/", "/data/local/tmp/", "/storage/emulated/0/"):
        i = remote.find(marker)
        if i > 0:
            return remote[i:]
    return remote


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    # --serial / --dry-run 两种位置都认：`--dry-run tap ...` 和 `tap ... --dry-run`
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--serial", help="多台设备时指定序列号（默认取唯一一台）")
    common.add_argument("--dry-run", action="store_true", help="只打印将执行的动作，不碰手机")

    p = argparse.ArgumentParser(
        prog="phone_ctl.py",
        description="用 adb 从 PC 操控手机：截图 / 控件树定位 / 点击 / 滑动 / 按键",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
        parents=[common],
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("devices", parents=[common], help="列出已连接设备").set_defaults(fn=cmd_devices)

    sp = sub.add_parser("shot", parents=[common], help="截一张图")
    sp.add_argument("-o", "--out", help="保存路径（默认 tmpd/phone/shot-时间.png）")
    sp.add_argument("--scale", type=int, default=640, help="缩略图宽度，默认 640")
    sp.add_argument("--no-scale", action="store_true", help="只存原图")
    sp.set_defaults(fn=cmd_shot)

    sp = sub.add_parser("ui", parents=[common], help="列出屏上文字节点")
    sp.add_argument("--grep", help="只看包含该字样的节点")
    sp.set_defaults(fn=cmd_ui)

    sp = sub.add_parser("find", parents=[common], help="查某段文字在哪（只读）")
    sp.add_argument("text")
    sp.set_defaults(fn=cmd_find)

    sp = sub.add_parser("tap-text", parents=[common], help="按文字点（推荐）")
    sp.add_argument("text")
    sp.add_argument("--x-ratio", type=float, default=0.5,
                    help="点在该节点横向的什么位置（0=最左，1=最右），默认居中")
    sp.add_argument("--index", type=int, default=-1, help="多个命中时选第几个（按从上到下）")
    sp.add_argument("--wait", type=float, default=1.5, help="点完等几秒，默认 1.5")
    sp.add_argument("--no-wait", action="store_true")
    sp.set_defaults(fn=cmd_tap_text)

    sp = sub.add_parser("tap", parents=[common], help="按绝对坐标点")
    sp.add_argument("x", type=int)
    sp.add_argument("y", type=int)
    sp.set_defaults(fn=cmd_tap)

    sp = sub.add_parser("swipe", parents=[common], help="滑动")
    sp.add_argument("x1", type=int)
    sp.add_argument("y1", type=int)
    sp.add_argument("x2", type=int)
    sp.add_argument("y2", type=int)
    sp.add_argument("--ms", type=int, default=400, help="滑动时长，默认 400ms")
    sp.add_argument("--wait", type=float, default=0.8)
    sp.set_defaults(fn=cmd_swipe)

    sp = sub.add_parser("key", parents=[common], help="按键：back/home/recent/menu/wake/sleep/enter/del/unlock")
    sp.add_argument("name")
    sp.add_argument("--wait", type=float, default=0.8)
    sp.set_defaults(fn=cmd_key)

    sp = sub.add_parser("input", parents=[common], help="输入 ASCII 文本（中文不支持）")
    sp.add_argument("text")
    sp.set_defaults(fn=cmd_input)

    sp = sub.add_parser("wait-text", parents=[common], help="等某段文字出现")
    sp.add_argument("text")
    sp.add_argument("--timeout", type=float, default=15)
    sp.add_argument("--interval", type=float, default=1.0)
    sp.set_defaults(fn=cmd_wait_text)

    sp = sub.add_parser("scene", parents=[common], help="截图 + 屏上文字摘要")
    sp.add_argument("-o", "--out")
    sp.add_argument("--scale", type=int, default=640)
    sp.add_argument("--no-scale", action="store_true")
    sp.set_defaults(fn=cmd_scene)

    sp = sub.add_parser("pull", parents=[common], help="把手机上的文件取到电脑")
    sp.add_argument("remote")
    sp.add_argument("dest", nargs="?")
    sp.set_defaults(fn=cmd_pull)

    return p


def main() -> None:
    global SERIAL, DRY_RUN
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[attr-defined]
        except Exception:                                            # noqa: BLE001
            pass
    args = build_parser().parse_args()
    SERIAL = args.serial
    DRY_RUN = args.dry_run
    args.fn(args)


if __name__ == "__main__":
    main()
