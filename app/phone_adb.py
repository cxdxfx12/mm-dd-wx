"""adb 直连：把钉钉拉到前台、切到目标群。

为什么必须有这条路（2026-11-02 13:13 真机现场）
    用户点「切到钉钉群」失败，手机回报：

        失败：拉不起钉钉（系统拒绝了直接启动；最近任务里也点不开钉钉）
        失败现场：pkg=cn.sto.sxz.core.app · title=揽件扫描

    也就是说，手机前台被**另一个程序**占着时，我们采集端（无障碍服务）
    去 ``startActivity`` 会被华为 ROM 的「后台弹出界面」策略直接拒掉。
    这不是权限没开，是系统层面的硬限制 —— 那个开关也不一定管用。

    而 ``adb shell`` 是 shell 身份，**不受后台启动限制**。真机实测：

        input keyevent HOME        → 焦点变成华为桌面
        monkey -p com.alibaba... 1  → 0.57 秒，焦点回到
                                      rimet/ChatMsgActivity（还是原来的聊天页）

    所以「切回钉钉」这件事必须由电脑端用 adb 干，不能指望手机端自己拉。

为什么不用 adb 打字搜群
    ``adb shell input text`` 只吃 ASCII，群名是中文
    （「【数字质控】浙江杭州萧山区大江东公司」）。要打中文得额外装
    ADBKeyboard 之类，为一个按钮不值当。

    所以这里的进群策略是**只在看得见的控件里点**：
        ① 先把钉钉拉到前台（monkey，0.6 秒）；
        ② dump 一次，标题（``tv_title``）就是群名 —— 对上了就算成功；
        ③ 没对上才在**当前界面**里找同名文本点它（例如停在会话列表）；
        ④ 仍然没有就往上滑一屏再看，最多三轮。
    找不到就老实报「没找到」，绝不猜坐标瞎点 —— 用户的手机正在被别的
    程序用着，乱点一下的代价比多等一会儿大得多。

界面读取的实现参照同机另一个项目（``sto-scan-assistant`` 的
``tools/phone.py``）：``dumpsys window`` 判焦点、``monkey`` 兜底拉起、
``uiautomator dump`` 拿树、按文字找 bounds 再 ``input tap``。
那套东西在同一台机器上验证过是稳的，这里按钉钉的控件名做了收敛。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

DINGTALK_PKG = "com.alibaba.android.rimet"
# 聊天页标题栏的控件 id（真机 2026-11-02 抓的，text 就是群名）
TITLE_RID = "com.alibaba.android.rimet:id/tv_title"
LAUNCHER = "android.intent.category.LAUNCHER"

# 超时：dump 在钉钉上实测 3 秒左右（界面大、节点多），给足余量
SHORT_TIMEOUT = 15.0
DUMP_TIMEOUT = 30.0

_KEYEVENT_HOME = "KEYCODE_HOME"
_KEYCODE_WAKEUP = 224


class AdbError(RuntimeError):
    """adb 不可用 / 设备不在线 / 命令失败。"""


@dataclass(frozen=True)
class Node:
    """界面上的一个控件。bounds 是**真机像素**，可直接喂给 input tap。"""

    text: str
    desc: str
    rid: str
    cls: str
    clickable: bool
    bounds: tuple[int, int, int, int]

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return ((x1 + x2) // 2, (y1 + y2) // 2)


_NODE_RE = re.compile(r"<node\b[^>]*>")


def _attr(tag: str, key: str) -> str:
    m = re.search(key + r'="([^"]*)"', tag)
    return m.group(1) if m else ""


def parse_nodes(xml: str) -> list[Node]:
    """把 uiautomator 的 XML 拍平成控件列表。

    只保留能算出席位的节点：没有 bounds 的点了也没意义，直接丢。
    """
    out: list[Node] = []
    for m in _NODE_RE.finditer(xml or ""):
        tag = m.group(0)
        nums = re.findall(r"-?\d+", _attr(tag, "bounds"))
        if len(nums) < 4:
            continue
        x1, y1, x2, y2 = (int(v) for v in nums[:4])
        out.append(
            Node(
                text=_attr(tag, "text"),
                desc=_attr(tag, "content-desc"),
                rid=_attr(tag, "resource-id"),
                cls=_attr(tag, "class"),
                clickable=_attr(tag, "clickable") == "true",
                bounds=(x1, y1, x2, y2),
            )
        )
    return out


def find_title(nodes: list[Node]) -> str:
    """当前聊天页的标题（群名）。不在聊天页时返回空串。"""
    for n in nodes:
        if n.rid == TITLE_RID and n.text.strip():
            return n.text.strip()
    return ""


def find_text(nodes: list[Node], text: str, *, exact: bool = False) -> Node | None:
    """按文字找控件。``exact`` 优先完全相等，否则用包含（钉钉会在名字后加后缀）。"""
    target = (text or "").strip()
    if not target:
        return None
    if exact:
        for n in nodes:
            if n.text.strip() == target or n.desc.strip() == target:
                return n
    for n in nodes:
        t, d = n.text.strip(), n.desc.strip()
        if target in t or target in d:
            return n
    return None


def same_group(title: str, group: str) -> bool:
    """标题算不算「就是目标群」。

    钉钉的标题有时是「群名」，有时后面跟「(内部群)」「8位成员」之类，
    两个方向都包含一下就够了；空值一律不算。
    """
    a, b = (title or "").strip(), (group or "").strip()
    if not a or not b:
        return False
    if a == b or a.startswith(b) or b in a:
        return True
    # 反向包含（标题比配置的群名短，例如配了带后缀的全名）。
    # 只在标题够长时才认，免得一个字的标题碰巧命中。
    return len(a) >= 4 and a in b


def find_adb() -> str | None:
    """按「程序目录 → PATH」找 adb.exe。

    安装版的 adb 在 ``<安装目录>\\tools\\platform-tools``；源码态在仓库根。
    路径解析的细节与 ``app/phone_panel.py`` 保持一致，免得两处各找各的。
    """
    roots: list[Path] = []
    here = Path(__file__).resolve().parent
    roots += [here.parent, here]
    exe_dir = Path(sys.executable).resolve().parent
    roots += [exe_dir, exe_dir.parent]
    for root in roots:
        for rel in ("tools/platform-tools/adb.exe",
                    "platform-tools/adb.exe",
                    "android/platform-tools/adb.exe"):
            p = root / rel
            if p.is_file():
                return str(p)
    return shutil.which("adb")


class Adb:
    """一台设备的 adb 句柄。所有调用都同步、带超时，失败抛 AdbError。"""

    def __init__(self, adb_path: str | None = None, serial: str | None = None) -> None:
        self.adb = adb_path or find_adb()
        if not self.adb:
            raise AdbError("没找到 adb.exe（tools\\platform-tools 下）")
        self.serial = serial
        # MSYS_NO_PATHCONV：/sdcard 在 Git Bash 里会被改写成 Windows 路径，
        # 同一个坑在另一个项目里踩过。
        self._env = dict(os.environ, MSYS_NO_PATHCONV="1")

    # ------------------------------------------------------------ 底层
    def run(self, args: list[str], timeout: float = SHORT_TIMEOUT) -> str:
        cmd = [self.adb]
        if self.serial:
            cmd += ["-s", self.serial]
        cmd += args
        kw = {}
        if sys.platform == "win32":
            # adb.exe 是控制台程序，从 pythonw 里调会每回闪一个黑框
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                               env=self._env, **kw)
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"adb 超时（{timeout:.0f}s）：{' '.join(args[:3])}") from exc
        except OSError as exc:
            raise AdbError(f"adb 起不来：{exc}") from exc
        out = (p.stdout or b"").decode("utf-8", "ignore")
        err = (p.stderr or b"").decode("utf-8", "ignore")
        return out + err

    def shell(self, cmd: str, timeout: float = SHORT_TIMEOUT) -> str:
        return self.run(["shell", cmd], timeout=timeout).strip()

    # ------------------------------------------------------------ 设备
    def pick_serial(self) -> str:
        """挑一台在线设备（优先真机）。挑不到抛 AdbError —— 调用方要能说清原因。"""
        out = self.run(["devices"], timeout=SHORT_TIMEOUT)
        rows = []
        for line in out.splitlines()[1:]:
            line = line.strip()
            if not line or line.startswith("*"):
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                rows.append(parts[0])
        if not rows:
            raise AdbError("没有在线设备：USB 线没插稳，或手机上没点「允许 USB 调试」")
        real = [r for r in rows if not r.startswith("emulator-")]
        self.serial = (real or rows)[0]
        return self.serial

    # ------------------------------------------------------------ 屏幕
    def wake(self) -> str:
        """点亮屏幕并尝试解除锁屏。

        有密码的锁屏解不开（只能靠人），但至少不会在灭屏状态下白点一气 ——
        那种情况下 dump 出来的是锁屏界面，报错会指向完全错误的方向。
        """
        try:
            state = self.shell("dumpsys power", timeout=20)
        except AdbError:
            return "unknown"
        m = re.search(r"mWakefulness=(\w+)", state)
        level = m.group(1) if m else "unknown"
        if level != "Awake":
            self.shell(f"input keyevent {_KEYCODE_WAKEUP}")
            time.sleep(0.8)
        self.shell("wm dismiss-keyguard")
        time.sleep(0.3)
        return level

    def focused_pkg(self) -> str:
        """当前焦点窗口属于哪个包。读不到返回空串。"""
        out = self.shell("dumpsys window", timeout=20)
        m = re.search(r"mCurrentFocus=\S+ \S+ ([A-Za-z0-9_.]+)/", out)
        return m.group(1) if m else ""

    # ------------------------------------------------------------ 操作
    def tap(self, x: int, y: int) -> None:
        self.shell(f"input tap {int(x)} {int(y)}")

    def key_home(self) -> None:
        self.shell(f"input keyevent {_KEYEVENT_HOME}")

    def swipe_up(self) -> None:
        """往上滑一屏（内容上移 = 看后面的条目）。屏幕尺寸按真机现值算。"""
        w, h = self.screen_size()
        x = w // 2
        self.shell(f"input swipe {x} {int(h * 0.72)} {x} {int(h * 0.32)} 300")

    def screen_size(self) -> tuple[int, int]:
        """真实可用分辨率。

        ⚠ 折叠屏会同时报 Physical 与 Override（真机：2480x2200 / 1176x2480），
        以 Override 为准 —— dump 的 bounds 和 input tap 用的是这套坐标。
        """
        out = self.shell("wm size", timeout=10)
        m = re.search(r"Override size:\s*(\d+)x(\d+)", out)
        if not m:
            m = re.search(r"Physical size:\s*(\d+)x(\d+)", out)
        if not m:
            return (1080, 1920)
        return (int(m.group(1)), int(m.group(2)))

    def dump(self, timeout: float = DUMP_TIMEOUT) -> str:
        """拿当前界面的控件树 XML。"""
        self.shell("uiautomator dump /sdcard/ddwx_ui.xml >/dev/null 2>&1",
                   timeout=timeout)
        return self.run(["exec-out", "cat /sdcard/ddwx_ui.xml"], timeout=timeout)

    def dump_nodes(self) -> list[Node]:
        return parse_nodes(self.dump())

    def launch_dingtalk(self) -> bool:
        """把钉钉拉到前台。返回是否成功（以焦点包名为准，不信 monkey 的退出码）。

        用 monkey 而不是 ``am start``：钉钉的入口 Activity 不保证导出，
        ``am start`` 可能被 SecurityException 拒；monkey 走 LAUNCHER 意图，
        真机实测 0.6 秒见效，而且会恢复到上次那个页面（通常就是目标群）。
        """
        self.shell(
            f"monkey -p {DINGTALK_PKG} -c {LAUNCHER} 1",
            timeout=30,
        )
        for _ in range(8):
            if self.focused_pkg() == DINGTALK_PKG:
                return True
            time.sleep(0.6)
        return False

    def ensure_dingtalk(self, *, retries: int = 2) -> bool:
        """确保钉钉在前台。已经在就不动（避免每点一次都重启一遍界面）。"""
        if self.focused_pkg() == DINGTALK_PKG:
            return True
        for _ in range(max(1, retries)):
            if self.launch_dingtalk():
                return True
            time.sleep(0.8)
        return False


def switch_to_group(group: str, *, adb: Adb | None = None,
                    rounds: int = 3) -> tuple[bool, str]:
    """把手机上的钉钉切到 ``group`` 这个群。

    返回 ``(是否成功, 给人看的一句话)``。**不抛异常**：界面上的按钮需要一个
    能直接显示的结果，把 AdbError 变成人话比让它冒到顶层更有用。

    失败时的说明要能直接指向下一步动作（没插线 / 锁屏 / 钉钉没装 / 界面里
    就是没这个群），不要只说「失败」。
    """
    name = (group or "").strip()
    if not name:
        return False, "没配目标群，不知道该切到哪个群"

    try:
        a = adb or Adb()
        a.pick_serial()
        a.wake()
        was_front = a.focused_pkg() == DINGTALK_PKG
        if not a.ensure_dingtalk():
            cur = a.focused_pkg() or "（读不到）"
            return False, (f"adb 拉不起钉钉（当前前台是 {cur}）—— "
                           f"手机可能在锁屏，或钉钉被系统杀掉了")
    except AdbError as exc:
        return False, f"adb 不通：{exc}"

    state = "already" if was_front else "launched"
    # 刚拉起的界面要给它一点时间渲染标题栏，否则 dump 出来还是上一屏
    time.sleep(0.6 if was_front else 1.0)

    last_title = ""
    for _ in range(max(1, rounds)):
        try:
            nodes = a.dump_nodes()
        except AdbError as exc:
            return False, f"读界面失败：{exc}"
        title = find_title(nodes)
        if title:
            last_title = title
            if same_group(title, name):
                how = ("钉钉本来就在目标群" if state == "already"
                       else "adb 已把钉钉切回目标群")
                return True, f"{how}：{title}"
        # 不在目标群：只在**看得见**的控件里找同名对话，找不到绝不猜坐标
        hit = find_text(nodes, name, exact=True) or find_text(nodes, name)
        if hit is not None and hit.rid != TITLE_RID:
            try:
                a.tap(*hit.center)
            except AdbError as exc:
                return False, f"点击失败：{exc}"
            time.sleep(1.2)
            continue
        try:
            a.swipe_up()
        except AdbError:
            break
        time.sleep(0.8)

    if last_title:
        return False, f"钉钉在前台，但当前标题是「{last_title}」，没找到「{name}」"
    return False, f"钉钉在前台，但界面里没找到「{name}」这个群"
