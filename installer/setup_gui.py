"""喵喵钉钉-微信消息转发桥 —— Windows 安装向导。

用法
    喵喵钉钉-微信消息转发桥-Setup.exe            # 安装向导
    喵喵钉钉-微信消息转发桥-Setup.exe /uninstall  # 卸载（控制面板/开始菜单调用）

设计要点
    * 纯标准库 tkinter，PyInstaller --onefile 打成单文件 exe；
    * 程序文件打进内嵌的 payload.zip（构建脚本 installer/build.py 生成），
      安装时解压到目标目录；
    * 安装目录可能是只读的（C:\\Program Files），因此配置/数据库/日志
      统一落到 %APPDATA%\\喵喵钉钉-微信消息转发桥（应用内已适配）；
    * 目标目录不可写且当前进程无权限时，用 ShellExecute "runas" 提权重启自己；
    * 卸载 = 同一个 exe 加 /uninstall 参数，注册表登记在
      HKLM\\...\\Uninstall\\MiaoMiaoDingWeChatBridge，控制面板可见可卸载。
"""
from __future__ import annotations

import ctypes
import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import winreg
import zipfile
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

# ---------------------------------------------------------------- 品牌信息

APP_NAME = "喵喵钉钉-微信消息转发桥"
# ⚠ 这一行只是**兜底值**：installer/build.py 打包时会把整行替换成
#   bridge/__init__.py 里的 __version__。手工单独跑 setup_gui.py 调试时
#   才会用到这里的值 —— 改了记得跟 bridge/__init__.py 对齐。
#   格式必须是「0.xx」，与主程序底部的版本号一致。
VERSION = "0.52"
PUBLISHER = "杭州喵喵至家网络有限公司"
WEBSITE = "www.hbdxm.com"
COPYRIGHT = f"杭州喵喵至家网络有限公司　{WEBSITE}"
DESC = "把钉钉群消息自动转发到微信群（截屏 OCR 采集 + 微信控件投递）"
UNINSTALL_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\MiaoMiaoDingWeChatBridge"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "MiaoMiaoDingWeChatBridge"

PYPI_MIRRORS = [
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple/",
    "https://mirrors.cloud.tencent.com/pypi/simple",
]
PIP_COMMON = ["--progress-bar", "off", "--timeout", "15", "--retries", "2"]
PY_RUNTIME_VER = "3.12.10"
PY_URLS = [
    f"https://mirrors.huaweicloud.com/python/{PY_RUNTIME_VER}/"
    f"python-{PY_RUNTIME_VER}-amd64.exe",
    f"https://www.python.org/ftp/python/{PY_RUNTIME_VER}/"
    f"python-{PY_RUNTIME_VER}-amd64.exe",
]

NO_WINDOW = 0x08000000
# 兼容多种写法：Windows 的 /X、常规的 --x，以及裸词（Git Bash 会把 /x
# 误转换成路径，所以裸词形式在终端里反而最可靠）
_UNINSTALL_FLAGS = {"/uninstall", "--uninstall", "-uninstall", "-u", "uninstall"}
_SILENT_FLAGS = {"/s", "/silent", "--silent", "-s", "-q", "silent"}
IS_UNINSTALL = any(a.lower() in _UNINSTALL_FLAGS for a in sys.argv[1:])
IS_SILENT = any(a.lower() in _SILENT_FLAGS for a in sys.argv[1:])
# 静默安装是否也自动联网补齐运行环境（默认是，符合"缺什么自动装"的约定）
SILENT_AUTOENV = "--no-env" not in [a.lower() for a in sys.argv[1:]]


def _argv_value(flag: str) -> str | None:
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a.lower() == flag and i + 1 < len(args):
            return args[i + 1]
        if a.lower().startswith(flag + "="):
            return a.split("=", 1)[1]
    return None

# ---------------------------------------------------------------- 路径


def _payload_zip() -> Path | None:
    """内嵌 payload.zip 的位置（PyInstaller 解包目录 / 开发态 _build）。"""
    if getattr(sys, "frozen", False):
        p = Path(getattr(sys, "_MEIPASS", ".")) / "payload.zip"
    else:
        p = Path(__file__).resolve().parent / "_build" / "payload.zip"
    return p if p.is_file() else None


def _self_exe() -> Path:
    return Path(sys.executable).resolve() if getattr(sys, "frozen", False) \
        else Path(__file__).resolve()


def default_install_dir() -> Path:
    pf = os.getenv("ProgramFiles") or r"C:\Program Files"
    return Path(pf) / APP_NAME


def _same_volume_tmp(target: Path) -> Path:
    """挑一个与安装目录**同卷**的临时目录。

    Windows 允许重命名正在运行的 exe，但**不允许删除**它。``shutil.move``
    跨卷时会退化成「复制 + 删原文件」，删除这一步必然失败，整个 move 抛异常
    （实测 ``%TEMP%`` 在 C:、程序装在 D: 时，卸载程序挪不走自己，最后整目录
    只剩下一个 ``uninstall.exe``）。同卷则是纯 rename，一定成功。
    """
    tmp = Path(os.getenv("TEMP") or os.getenv("TMP") or ".")
    try:
        if os.path.splitdrive(str(tmp))[0].lower() == \
                os.path.splitdrive(str(target))[0].lower():
            return tmp
    except Exception:
        pass
    return target.parent                     # 不同卷：退到父目录（同卷）


def _dbg(msg: str) -> None:
    """把环境配置过程的关键步骤追加到 ``%TEMP%\\mm-setup-debug.log``。

    「一键自动配置」跑在后台线程里，窗口上那条日志框要靠 tkinter 的
    跨线程调度（``after``）才能显示 —— 调度本身一旦出问题，用户看到的
    就是「界面不动了」，且没有任何输出可排查。所以所有关键步骤都同步
    落一份文件日志：用户反馈卡住时，直接看这个文件就能定位卡在哪一步。
    """
    try:
        with open(Path(os.getenv("TEMP", ".")) / "mm-setup-debug.log",
                  "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] [{os.getpid()}] {msg}\n")
    except Exception:
        pass


def _spawn_deferred_cleanup(paths: list[Path]) -> bool:
    """让一个独立进程等本进程退出后，把残留的目录/文件删干净。

    用 ``-EncodedCommand``（UTF-16LE + base64）传脚本，不走 .bat/.ps1 文件 ——
    默认安装路径里有中文，写文件会撞上 cmd 的 OEM 代码页，脚本语法被劈断。
    """
    items = [p for p in paths if p]
    if not items:
        return False
    # PowerShell 的**单引号**字符串是字面量（反斜杠不转义），双引号里
    # json.dumps 会把 C:\x 写成 C:\\x，路径就废了。单引号内的 ' 用 '' 表示。
    quote = lambda s: "'" + str(s).replace("'", "''") + "'"
    script = "Start-Sleep -Seconds 2\n" + "\n".join(
        f"Remove-Item -LiteralPath {quote(p)} -Recurse -Force "
        f"-ErrorAction SilentlyContinue" for p in items)
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-WindowStyle", "Hidden", "-EncodedCommand", enc],
            # 用 CREATE_NO_WINDOW（无窗口）而**不是** DETACHED_PROCESS：
            # 后者会让 powershell 起来就退出、脚本一句都不执行（实测 exit 0、
            # 什么也没干成，而且完全静默）。父进程退出后子进程照常跑完，
            # 这条已单独验证过。
            creationflags=NO_WINDOW, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def user_data_dir() -> Path:
    base = os.getenv("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / APP_NAME


INSTALL_DIR = default_install_dir()

# ---------------------------------------------------------------- 权限


def _can_write(d: Path) -> bool:
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".__probe__"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def elevate_and_exit(args: list[str]) -> None:
    """用 runas 重新启动自己（触发 UAC），然后退出当前进程。"""
    exe = _self_exe()
    params = " ".join(f'"{a}"' for a in args)
    try:
        ctypes.windll.shell32.ShellExecuteW(None, "runas", str(exe), params, None, 1)
    except Exception:
        pass
    sys.exit(0)


def _ps(script: str, env: dict | None = None, timeout: int = 90) -> tuple[int, str]:
    """跑一段 PowerShell（UTF-8 BOM 落盘，绕开 cmd 代码页乱码）。"""
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False,
                                     encoding="utf-8-sig") as f:
        f.write(script)
        ps1 = f.name
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1],
            env=env or os.environ.copy(), capture_output=True, text=True,
            timeout=timeout, creationflags=NO_WINDOW, encoding="utf-8",
            errors="replace")
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    finally:
        try:
            os.unlink(ps1)
        except Exception:
            pass


PS_SHORTCUTS = r"""$ErrorActionPreference = 'Stop'
$ws = New-Object -ComObject WScript.Shell
function Make($dir, $name, $target, $lnkArgs) {
  if (-not $dir -or -not (Test-Path $dir)) { return }
  $lnk = $ws.CreateShortcut((Join-Path $dir ($name + '.lnk')))
  $lnk.TargetPath = $target
  if ($lnkArgs) { $lnk.Arguments = $lnkArgs }
  $lnk.WorkingDirectory = $env:MM_WD
  $lnk.IconLocation = $env:MM_ICON
  $lnk.Description = $env:MM_DESC
  $lnk.Save()
}
Make ([Environment]::GetFolderPath('Desktop')) $env:MM_NAME $env:MM_EXE $null
$folder = Join-Path ([Environment]::GetFolderPath('Programs')) $env:MM_FOLDER
if (-not (Test-Path $folder)) { New-Item -ItemType Directory -Path $folder | Out-Null }
Make $folder $env:MM_NAME $env:MM_EXE $null
Make $folder '卸载' $env:MM_UNINST '/uninstall'
Write-Output 'SHORTCUTS-OK'
"""

PS_REMOVE = r"""$ErrorActionPreference = 'SilentlyContinue'
$p = Join-Path ([Environment]::GetFolderPath('Desktop')) ($env:MM_NAME + '.lnk')
if (Test-Path $p) { Remove-Item $p -Force }
$folder = Join-Path ([Environment]::GetFolderPath('Programs')) $env:MM_FOLDER
if (Test-Path $folder) { Remove-Item $folder -Recurse -Force }
Write-Output 'REMOVED'
"""

# ================================================================ UI


class Wizard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.silent = IS_SILENT
        self.ok = False
        if self.silent:
            self.withdraw()                                  # 静默：不出界面
            d = _argv_value("--dir") or _argv_value("/d")
            if d:
                global INSTALL_DIR
                INSTALL_DIR = Path(d)
        self.title(f"{APP_NAME} 安装向导")
        self.geometry("680x520")
        self.minsize(640, 480)
        self.configure(bg="#eef1f7")
        self.resizable(False, False)
        self._center()

        self.install_dir = INSTALL_DIR
        self.desk_shortcut = tk.BooleanVar(value=True)
        self.autostart = tk.BooleanVar(value=False)
        self.wipe_data = tk.BooleanVar(value=False)
        self.launch_after = tk.BooleanVar(value=True)

        self.already_installed = self._detect_previous()
        self._build_header()
        self.body = tk.Frame(self, bg="#eef1f7")
        self.body.pack(fill="both", expand=True)
        self.pages = {}
        for name, builder in (("welcome", self._page_welcome),
                              ("install", self._page_install),
                              ("env", self._page_env),
                              ("finish", self._page_finish),
                              ("uninst", self._page_uninstall)):
            f = tk.Frame(self.body, bg="#eef1f7")
            f.pack(fill="both", expand=True)
            builder(f)
            self.pages[name] = f

        if self.silent:
            self.install_dir = INSTALL_DIR
            self.path_var = tk.StringVar(value=str(self.install_dir))
            if any(a.lower() == "--purge" for a in sys.argv[1:]):
                self.wipe_data.set(True)
            if IS_UNINSTALL:
                self._run_silent_uninstall()
                return
            self._run_silent()
            return

        if IS_UNINSTALL:
            self._show("uninst")
        else:
            self._show("welcome")

    # ---------------- 静默安装 ----------------

    def _run_silent(self):
        """/S 静默安装：无界面，完成后以退出码 0/1 结束。"""
        if not _can_write(self.install_dir):
            print(f"[静默安装] 无权限写入：{self.install_dir}", file=sys.stderr)
            self.destroy()
            sys.exit(3)
        try:
            self._install_worker()
            if (self.env_missing and SILENT_AUTOENV):
                self._env_worker()
            print("[静默安装] 完成：" + str(self.install_dir))
            self.ok = True
        except Exception as exc:
            traceback.print_exc()
            print(f"[静默安装] 失败：{exc}", file=sys.stderr)
        finally:
            code = 0 if self.ok else 1
            self.destroy()
            sys.exit(code)

    # ---------------- 框架 ----------------

    def _center(self):
        self.update_idletasks()
        w, h = 680, 520
        x = (self.winfo_screenwidth() - w) // 2
        y = (self.winfo_screenheight() - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _build_header(self):
        head = tk.Frame(self, bg="#1e2a4a", height=104)
        head.pack(fill="x")
        head.pack_propagate(False)
        inner = tk.Frame(head, bg="#1e2a4a")
        inner.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(inner, text=APP_NAME, bg="#1e2a4a", fg="white",
                 font=("Microsoft YaHei UI", 15, "bold")).pack()
        tk.Label(inner, text=f"版本 {VERSION}　·　{DESC}",
                 bg="#1e2a4a", fg="#aab6d4",
                 font=("Microsoft YaHei UI", 9)).pack(pady=(3, 0))
        tk.Label(inner, text=f"版权所有 © {COPYRIGHT}",
                 bg="#1e2a4a", fg="#7d8bab",
                 font=("Microsoft YaHei UI", 8)).pack(pady=(2, 0))

    def _show(self, name: str):
        if self.silent:
            self.current_page = name
            return
        for f in self.pages.values():
            f.pack_forget()
        self.pages[name].pack(fill="both", expand=True)

    @staticmethod
    def _card(parent, title: str) -> tk.Frame:
        box = tk.Frame(parent, bg="white", highlightbackground="#e2e7f0",
                       highlightthickness=1)
        box.pack(fill="x", padx=28, pady=(16, 0))
        tk.Label(box, text=title, bg="white", fg="#69728c",
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=16,
                                                      pady=(12, 2))
        return box

    # ---------------- 欢迎页 ----------------

    def _page_welcome(self, f: tk.Frame):
        if self.already_installed:
            tip = tk.Frame(f, bg="#fdf6e3", highlightbackground="#e7d9a0",
                           highlightthickness=1)
            tip.pack(fill="x", padx=28, pady=(14, 0))
            tk.Label(tip, text="检测到本机已安装此程序，继续将覆盖安装"
                               "（配置与消息记录会保留）。",
                     bg="#fdf6e3", fg="#8a6d1a",
                     font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=14,
                                                          pady=8)

        box = self._card(f, "安装位置")
        row = tk.Frame(box, bg="white")
        row.pack(fill="x", padx=16, pady=(2, 6))
        self.path_var = tk.StringVar(value=str(self.install_dir))
        e = tk.Entry(row, textvariable=self.path_var,
                     font=("Microsoft YaHei UI", 10), relief="flat",
                     highlightbackground="#e2e7f0", highlightthickness=1)
        e.pack(side="left", fill="x", expand=True, ipady=5)
        ttk.Button(row, text="浏览…", width=8,
                   command=self._browse).pack(side="left", padx=(8, 0))
        tk.Label(box, text="将安装程序、OCR 引擎与微信投递组件；配置和数据"
                           "保存在用户目录，不影响系统。",
                 bg="white", fg="#9aa3b8", font=("Microsoft YaHei UI", 8)
                 ).pack(anchor="w", padx=16, pady=(0, 12))

        box2 = self._card(f, "附加选项")
        for text, var in (("在桌面创建快捷方式", self.desk_shortcut),
                          ("开机自动启动转发服务", self.autostart)):
            ttk.Checkbutton(box2, text=text, variable=var).pack(
                anchor="w", padx=16, pady=4)

        bar = tk.Frame(f, bg="#eef1f7")
        bar.pack(side="bottom", fill="x", pady=14)
        ttk.Button(bar, text="开始安装", style="Accent.TButton",
                   command=self._start_install).pack(side="right", padx=28)
        ttk.Button(bar, text="退出", width=8,
                   command=self.destroy).pack(side="right", padx=(0, 8))

    def _browse(self):
        d = filedialog.askdirectory(parent=self, title="选择安装位置",
                                    initialdir=str(self.install_dir))
        if d:
            self.path_var.set(d.replace("/", "\\"))

    def _detect_previous(self) -> bool:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, UNINSTALL_KEY) as k:
                winreg.QueryValueEx(k, "InstallLocation")
            return True
        except Exception:
            return False

    # ---------------- 安装页 ----------------

    def _page_install(self, f: tk.Frame):
        box = self._card(f, "正在安装")
        self.prg = ttk.Progressbar(box, maximum=100)
        self.prg.pack(fill="x", padx=16, pady=(4, 8))
        self.step_lbl = tk.Label(box, text="准备中…", bg="white", fg="#1c2333",
                                 font=("Microsoft YaHei UI", 10), anchor="w")
        self.step_lbl.pack(fill="x", padx=16, pady=(0, 10))

        logbox = self._card(f, "详细过程")
        self.log_txt = tk.Text(logbox, height=13, font=("Microsoft YaHei UI", 9),
                               bg="#fbfcfe", relief="flat", state="disabled",
                               highlightbackground="#e2e7f0", highlightthickness=1)
        self.log_txt.pack(fill="both", expand=True, padx=16, pady=(2, 12))

    def _log(self, msg: str, tag: str = "n"):
        color = {"n": "#1c2333", "ok": "#0f9d58", "err": "#e5484d",
                 "dim": "#8b93a8"}[tag]
        self.log_txt.configure(state="normal")
        self.log_txt.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n",
                            ("t",))
        self.log_txt.tag_config("t", foreground=color)
        self.log_txt.see("end")
        self.log_txt.configure(state="disabled")

    def _set_step(self, text: str, pct: float | None = None):
        self.step_lbl.configure(text=text)
        if pct is not None:
            self.prg.configure(value=pct)

    def _start_install(self):
        self.install_dir = Path(self.path_var.get().strip().strip('"'))
        if not self.install_dir or self.install_dir.drive == "":
            messagebox.showwarning("提示", "请先选择有效的安装位置。", parent=self)
            return
        if not _can_write(self.install_dir):
            if messagebox.askyesno(
                    "需要管理员权限",
                    f"没有权限写入：\n{self.install_dir}\n\n"
                    "是否以管理员身份重新运行安装程序？", parent=self):
                elevate_and_exit([])
            return
        self._show("install")
        threading.Thread(target=self._install_worker, daemon=True).start()

    # ---- 安装各步骤（后台线程里跑）----

    def _install_worker(self):
        self.env_missing = False
        try:
            self._step_extract()
            self._step_config()
            self._step_shortcuts()
            self._step_register()
            self._step_env()
        except Exception as exc:
            traceback.print_exc()
            self._ui(lambda: (self._log(f"安装失败：{exc}", "err"),
                              self._set_step("安装失败", 100)))
            return
        if self.env_missing and self.silent and SILENT_AUTOENV:
            self._env_worker()

    def _ui(self, fn, *a):
        if self.silent:                      # 静默：没有主循环，直接执行
            try:
                fn(*a)
            except Exception:
                pass
            return
        self.after(0, lambda: fn(*a))

    def _step_extract(self):
        payload = _payload_zip()
        if payload is None:
            raise RuntimeError("安装包内部缺少程序文件（payload.zip）")
        target = self.install_dir
        self._ui(self._set_step, "正在解压程序文件…", 5)
        _dbg(f"extract start: {payload} -> {target}")
        self._release_file_locks(target)
        with zipfile.ZipFile(payload) as zf:
            names = zf.namelist()
            total = max(len(names), 1)
            for i, name in enumerate(names):
                self._extract_one(zf, name, target)
                if i % 25 == 0 or i == total - 1:
                    self._ui(self._set_step,
                             f"正在解压程序文件… {i + 1}/{total}",
                             5 + 60 * (i + 1) / total)
                    # 心跳落盘：界面若被杀软拦停/冻结，日志能告诉我们
                    # 解压到底走到哪、卡没卡（2026-11-04 用户装机卡在
                    # 151/256，实际文件已全部落盘，界面只是没再刷新）。
                    _dbg(f"extract {i + 1}/{total}")
        _dbg("extract done")
        self._ui(self._log, f"程序文件已释放到 {target}（{total} 个文件）", "ok")
        self._drop_stale_apks(target, names)

        # 复制一份自己作为卸载程序
        try:
            me = _self_exe()
            if getattr(sys, "frozen", False):
                shutil.copy2(me, target / "uninstall.exe")
        except Exception as exc:
            self._ui(self._log, f"卸载程序复制失败（不影响使用）：{exc}", "dim")
        self._ui(self._set_step, "程序文件已就绪", 65)

    def _extract_one(self, zf: zipfile.ZipFile, name: str, target: Path) -> None:
        """解压单个条目，遇到文件被占用时自动释放占用再重试。

        2026-11-04 真机踩到：``tools/platform-tools/AdbWinApi.dll`` 报
        ``[Errno 13] Permission denied`` —— 上一版留下的 **adb server 常驻进程**
        （adb.exe 监听 5037）一直握着自己的 DLL，安装器覆盖不了。用户看到的
        就是「安装失败」，而且重试也没用，除非手动去任务管理器结束 adb.exe。
        所以这里对 PermissionError 做「释放 → 重试」的闭环，而不是一次失败就崩。
        """
        if name.endswith("/"):
            return
        last: Exception | None = None
        for attempt in range(4):
            try:
                zf.extract(name, target)
                return
            except PermissionError as exc:                 # 被占用 / 只读
                last = exc
                _dbg(f"extract busy ({attempt + 1}/4): {name} -> {exc}")
                if attempt == 0:
                    self._kill_by_image("adb.exe")
                elif attempt == 1:
                    self._release_file_locks(target)
                time.sleep(0.4 * (attempt + 1))
            except Exception as exc:                       # noqa: BLE001
                last = exc
                _dbg(f"extract fail: {name} -> {exc}")
                time.sleep(0.2)
        raise RuntimeError(
            f"无法写入文件（可能被其他程序占用）：\n{target / name}\n\n"
            f"具体错误：{last}\n\n"
            "请先退出正在运行的「喵喵钉钉-微信消息转发桥」，"
            "并在任务管理器里结束 adb.exe 后重试安装。")

    def _kill_by_image(self, image: str) -> None:
        """按进程名强杀。装升级包时必须的：旧进程握着 exe/dll，覆盖必失败。"""
        try:
            r = subprocess.run(["taskkill", "/F", "/IM", image],
                               capture_output=True, timeout=10,
                               creationflags=0x08000000 if os.name == "nt" else 0,
                               stdin=subprocess.DEVNULL)
            _dbg(f"taskkill {image} rc={r.returncode}")
        except Exception as exc:                           # noqa: BLE001
            _dbg(f"taskkill {image} failed: {exc}")

    def _release_file_locks(self, target: Path) -> None:
        """安装/升级前的清场：停掉会锁住目标目录文件的进程。

        两类：
        1. **主程序自己** —— 它锁着 ``喵喵钉钉-微信消息转发桥.exe``，不结束就
           覆盖不了（升级时提示「正在使用中」或直接 PermissionError）。
        2. **adb server** —— 手机画面面板一取帧就拉起常驻的 adb.exe，它握着
           ``tools/platform-tools/AdbWinApi.dll`` 等文件。这是 2026-11-04
           用户在 AdbWinApi.dll 上撞到 Permission denied 的真凶。

        静默安装时本来就该先停程序（用户点安装就是要升级）；GUI 安装也一样，
        否则必然失败。停完不自动重启，交回用户自己开。
        """
        if os.name != "nt":
            return
        adb = target / "tools" / "platform-tools" / "adb.exe"
        if adb.exists():
            try:
                r = subprocess.run([str(adb), "kill-server"], capture_output=True,
                                   timeout=8, creationflags=0x08000000,
                                   stdin=subprocess.DEVNULL)
                _dbg(f"adb kill-server rc={r.returncode}")
            except Exception as exc:                       # noqa: BLE001
                _dbg(f"adb kill-server failed: {exc}")
        self._kill_by_image("adb.exe")
        self._kill_by_image(APP_NAME + ".exe")
        time.sleep(0.5)

    def _drop_stale_apks(self, target: Path, names: list[str]) -> None:
        """删掉上一版留下、这一版不再需要的 APK。

        升级是「解压覆盖」，**不会**删除旧版本才有、新版本没有的文件。
        早先在真机上踩过：新装的机器里同时躺着
        ``android/dist/ddwx-collector-0.2.0.apk``（老版装的）和
        ``app/ddwx-collector-0.3.0.apk``（新版刚放的），网关的
        ``_apk_search_dirs()`` 把这两个目录都当候选，任何一点版本比较的
        岔子都会让 /app 页面发回老包 —— 用户看到「升级了还是老版本」。

        APK 是整包替换的产物，没有「用户自己改过」的可能，删旧的没有风险；
        其它文件一律不碰（用户可能在 ``data/`` 里放了东西）。
        """
        fresh = {n.replace("\\", "/") for n in names if n.lower().endswith(".apk")}
        keep = {Path(n).name for n in fresh}
        for sub in ("android/dist", "app"):
            d = target / sub
            if not d.is_dir():
                continue
            for p in d.glob("*.apk"):
                rel = f"{sub}/{p.name}"
                if rel in fresh or p.name in keep:
                    continue
                try:
                    p.unlink()
                    self._ui(self._log, f"已清理旧版安装包 {rel}", "dim")
                except OSError as exc:
                    self._ui(self._log, f"（保留）旧包删不掉 {rel}：{exc}", "dim")

    def _step_config(self):
        self._ui(self._set_step, "正在写入程序配置…", 68)
        app_dir = self.install_dir / "app"
        sys.path.insert(0, str(app_dir))
        import cfgfile                                   # noqa: E402
        cfg = self.install_dir / "config.yaml"
        sample = self.install_dir / "config.example.yaml"

        # 安装包**刻意不含 config.yaml**，原因有两个：
        #   1) 构建机的 config.yaml 里带着真实的群名和机器人 token，
        #      打进安装包等于把凭证随二进制一起分发出去；
        #   2) 升级时整包解压会覆盖掉用户自己改过的配置。
        # 所以首次安装从样板生成，升级时原配置原样保留。
        if not cfg.is_file() and sample.is_file():
            shutil.copy2(sample, cfg)
            self._ui(self._log, "已按配置样板生成 config.yaml", "ok")

        # 从老版本升级上来的 config.yaml 没有 notify 段，缺了它「设置中心 →
        # 钉钉通知」就存不进去。段文本直接从样板里取，新增配置项时不会漏。
        if cfgfile.ensure_section(cfg, "notify", cfgfile.section_text(sample, "notify")):
            self._ui(self._log, "钉钉通知配置段已就绪", "ok")

        # wxauto 库随程序一起安装，写绝对路径，配置搬到 %APPDATA% 后也不失效
        cfgfile.set_scalar(cfg, "wechat", "wxauto_path",
                           str(self.install_dir / "wxauto-main"))
        self._ui(self._log, "config.yaml 已就位（wxauto_path 指向随装的库）", "ok")

    def _step_shortcuts(self):
        if not self.desk_shortcut.get():
            self._ui(self._set_step, "已跳过快捷方式", 75)
            return
        self._ui(self._set_step, "正在创建快捷方式…", 75)
        env = os.environ.copy()
        env.update({
            "MM_NAME": APP_NAME,
            "MM_FOLDER": APP_NAME,
            "MM_EXE": str(self.install_dir / f"{APP_NAME}.exe"),
            "MM_UNINST": str(self.install_dir / "uninstall.exe"),
            "MM_ICON": str(self.install_dir / "app" / "app.ico"),
            "MM_WD": str(self.install_dir),
            "MM_DESC": DESC,
        })
        code, out = _ps(PS_SHORTCUTS, env)
        if "SHORTCUTS-OK" not in out:
            self._ui(self._log, f"快捷方式创建失败：{out.strip()[:300]}", "err")
        else:
            self._ui(self._log, "已创建桌面与开始菜单快捷方式", "ok")
        self._ui(self._set_step, "快捷方式已就绪", 80)

    def _step_register(self):
        self._ui(self._set_step, "正在登记卸载信息…", 85)
        uninst = self.install_dir / "uninstall.exe"
        size_kb = 0
        try:
            for p in self.install_dir.rglob("*"):
                try:
                    if p.is_file():
                        size_kb += p.stat().st_size // 1024
                except Exception:
                    pass
        except Exception:
            pass
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, UNINSTALL_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            def setv(name, value, typ=winreg.REG_SZ):
                winreg.SetValueEx(k, name, 0, typ, value)
            setv("DisplayName", APP_NAME)
            setv("DisplayVersion", VERSION)
            setv("Publisher", PUBLISHER)
            setv("InstallLocation", str(self.install_dir))
            setv("DisplayIcon", str(self.install_dir / "app" / "app.ico"))
            setv("UninstallString", f'"{uninst}" /uninstall')
            setv("NoModify", 1, winreg.REG_DWORD)
            setv("NoRepair", 1, winreg.REG_DWORD)
            setv("EstimatedSize", max(size_kb, 1), winreg.REG_DWORD)
        if self.autostart.get():
            try:
                with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                        winreg.KEY_SET_VALUE) as k:
                    winreg.SetValueEx(
                        k, RUN_VALUE, 0, winreg.REG_SZ,
                        f'"{self.install_dir / APP_NAME + ".exe"}"')
            except Exception:
                pass
        self._ui(self._log, "已登记到「设置 → 应用」（可在控制面板卸载）", "ok")
        self._ui(self._set_step, "卸载信息已登记", 90)

    def _find_existing_worker(self) -> Path | None:
        """找已有的、依赖齐全的工作解释器（不走 paths.resolve ——
        Setup 是冻结 exe，resolve 用 sys.executable 推 BASE_DIR 会扫错地方，
        装到 Program Files 时必然误判「缺依赖」弹配置页，实测过的坑）。"""
        sys.path.insert(0, str(self.install_dir / "app"))
        import paths                                   # noqa: E402
        for root in (self.install_dir, user_data_dir()):
            for parts in (("runtime", "python.exe"),
                          ("venv", "Scripts", "python.exe")):
                exe = root.joinpath(*parts)
                if exe.is_file() and paths.missing_modules(
                        str(exe), paths.WORKER_MODULES)[0]:
                    return exe
        # 上一次配置写进缓存的解释器（%APPDATA% 侧的 _env.json）
        try:
            cache = user_data_dir() / "_env.json"
            w = json.loads(cache.read_text(encoding="utf-8")).get("worker")
            if w and Path(w).is_file() and paths.missing_modules(
                    w, paths.WORKER_MODULES)[0]:
                return Path(w)
        except Exception:
            pass
        return None

    def _step_env(self):
        self._ui(self._set_step, "正在检测运行环境…", 92)
        existing = self._find_existing_worker()

        def report_ok():
            self._log(f"工作解释器：{existing}", "ok")
            self._log("运行环境完整，可以开始使用。", "ok")
        if existing:
            self.env_missing = False
            self._ui(report_ok)
            self._ui(self._set_step, "环境检测通过", 100)
            self.after(600, lambda: self._show("finish"))
            return

        def report_missing():
            self._log("未找到可用的运行环境，需要配置", "dim")
        self.env_missing = True
        offline = self._vendor_bundle() is not None

        def goto_env():
            self._show("env")
            if offline:
                # 随包离线组件：配置又快又不联网，没必要让用户再点一次
                self._env_log("检测到随包离线组件，自动开始配置（全程离线）。", "ok")
                self._start_env_setup()
        self._ui(report_missing)
        self._ui(self._set_step, "需要配置运行环境", 95)
        self.after(600, goto_env)

    # ---------------- 静默卸载 ----------------

    def _run_silent_uninstall(self):
        """/S /uninstall：无界面、无确认，直接卸载。"""
        try:
            self._uninstall_worker()
            print("[静默卸载] 完成")
            self.ok = True
        except Exception as exc:
            traceback.print_exc()
            print(f"[静默卸载] 失败：{exc}", file=sys.stderr)
        finally:
            code = 0 if self.ok else 1
            self.destroy()
            sys.exit(code)

    # ---------------- 环境配置页 ----------------

    def _page_env(self, f: tk.Frame):
        offline = self._vendor_bundle() is not None
        box = self._card(f, "运行环境配置"
                         if offline else "运行环境需要联网配置")
        intro = ("安装包内已附带完整的 Python 运行时和全部依赖，\n"
                 "点击开始后自动完成配置，全程离线、无需联网。"
                 if offline else
                 "本机还没有能跑「钉钉采集 + 微信投递」的 Python 环境。\n"
                 "点击下面的按钮，安装程序会自动：\n"
                 "  1. 准备一个 Python 3 运行时（本机没有就自动下载，约 25MB）\n"
                 "  2. 建立独立虚拟环境并安装依赖（国内镜像，约 300-600MB）\n"
                 "全部装在用户目录，不污染系统 Python。")
        tk.Label(box, text=intro,
                 bg="white", fg="#1c2333", justify="left",
                 font=("Microsoft YaHei UI", 10)).pack(anchor="w", padx=16,
                                                       pady=(2, 12))

        logbox = self._card(f, "配置过程")
        self.env_log = tk.Text(logbox, height=12, font=("Microsoft YaHei UI", 9),
                               bg="#fbfcfe", relief="flat", state="disabled",
                               highlightbackground="#e2e7f0", highlightthickness=1)
        self.env_log.pack(fill="both", expand=True, padx=16, pady=(2, 12))
        self.env_prg = ttk.Progressbar(logbox, mode="indeterminate")
        self.env_prg.pack(fill="x", padx=16, pady=(0, 12))

        bar = tk.Frame(f, bg="#eef1f7")
        bar.pack(side="bottom", fill="x", pady=14)
        self.env_btn = ttk.Button(bar, text="一键自动配置（离线）" if offline
                                  else "一键自动配置（联网）",
                                  style="Accent.TButton",
                                  command=self._start_env_setup)
        self.env_btn.pack(side="right", padx=28)
        ttk.Button(bar, text="跳过，稍后再说", width=14,
                   command=lambda: self._show("finish")).pack(side="right",
                                                              padx=(0, 8))

    def _env_log(self, msg: str, tag: str = "n"):
        color = {"n": "#1c2333", "ok": "#0f9d58", "err": "#e5484d",
                 "dim": "#8b93a8"}[tag]
        _dbg(f"env[{tag}] {msg}")          # 先落文件，再走界面调度

        def go():
            self.env_log.configure(state="normal")
            self.env_log.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
            self.env_log.tag_add("t", "end-2l", "end-1l")
            self.env_log.tag_config("t", foreground=color)
            self.env_log.see("end")
            self.env_log.configure(state="disabled")
        self.after(0, go)

    def _start_env_setup(self):
        self.env_btn.configure(state="disabled")
        self.env_prg.start(12)
        threading.Thread(target=self._env_worker, daemon=True).start()

    def _run_logged(self, argv, cwd=None, timeout=3600,
                    env=None) -> tuple[int, str]:
        """跑外部命令，把关键输出滚动写进环境日志。

        逐字符读取（pip 的进度条用 \\r 刷新，按行读会憋到结束才出输出，
        界面几分钟没动静就像死机——实测过的坑）。
        """
        self._env_log("$ " + " ".join(str(a) for a in argv[:6]) +
                      (" …" if len(argv) > 6 else ""), "dim")
        _dbg("run: " + " ".join(str(a) for a in argv))
        proc = subprocess.Popen(
            [str(a) for a in argv], cwd=cwd, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", creationflags=NO_WINDOW, env=env)
        out_tail: list[str] = []
        state = {"last": time.time()}
        t0 = time.time()

        def heartbeat():
            # 长时间无输出时给用户一句活话，避免“不动了”的错觉
            while proc.poll() is None:
                if time.time() - state["last"] > 20:
                    self._env_log(
                        f"仍在执行…（已用时 {int(time.time() - t0)} 秒，"
                        "大依赖下载需要几分钟）", "dim")
                    state["last"] = time.time()
                time.sleep(5)
        threading.Thread(target=heartbeat, daemon=True).start()

        def emit(line: str):
            out_tail.append(line)
            low = line.lower()
            if any(k in low for k in ("error", "失败", "could not", "no matching")):
                self._env_log(line[:300], "err")
            elif any(k in low for k in ("successfully installed", "installed",
                                        "done", "created")):
                self._env_log(line[:300], "ok")
            elif low.startswith(("collecting ", "downloading ", "using cached")):
                self._env_log(line[:200], "dim")
            state["last"] = time.time()

        assert proc.stdout is not None
        buf = ""
        while True:
            ch = proc.stdout.read(1)
            if not ch:
                break
            if ch in "\r\n":
                if buf.strip():
                    emit(buf.strip())
                buf = ""
            else:
                buf += ch
        if buf.strip():
            emit(buf.strip())
        try:
            code = proc.wait(timeout=timeout)
        except Exception:
            proc.kill()
            _dbg(f"run TIMEOUT/KILL: {' '.join(str(a) for a in argv[:3])}")
            return -1, "timeout"
        _dbg(f"run done rc={code} ({time.time() - t0:.0f}s): "
             f"{' '.join(str(a) for a in argv[:3])}")
        return code, "\n".join(out_tail[-40:])

    def _pick_base_python(self) -> str | None:
        """找一个能用来建 venv 的现成 Python（≥3.9）。"""
        import ast
        sys.path.insert(0, str(self.install_dir / "app"))
        import paths                                   # noqa: E402
        cands = paths._candidate_pythons()
        _dbg(f"probe candidates: {cands}")
        for exe in cands:
            t0 = time.time()
            try:
                r = subprocess.run(
                    [exe, "-c", "import sys;print(sys.version_info[:2])"],
                    capture_output=True, text=True, timeout=20,
                    creationflags=NO_WINDOW, encoding="utf-8", errors="replace")
            except subprocess.TimeoutExpired:
                _dbg(f"probe TIMEOUT ({time.time() - t0:.1f}s): {exe}")
                continue
            except Exception as exc:
                _dbg(f"probe ERROR ({time.time() - t0:.1f}s): {exe} -> {exc!r}")
                continue
            _dbg(f"probe ({time.time() - t0:.1f}s) rc={r.returncode} "
                 f"out={r.stdout.strip()!r}: {exe}")
            try:
                ver = ast.literal_eval((r.stdout or "").strip() or "(0, 0)")
            except Exception:
                continue
            if isinstance(ver, tuple) and ver >= (3, 9):
                return exe
        return None

    def _download_python(self, dest: Path) -> Path | None:
        """下载官方 Python 安装器（华为云镜像优先）。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        for url in PY_URLS:
            try:
                self._env_log(f"下载 Python {PY_RUNTIME_VER}：{url}", "dim")

                def hook(blocks: int, bs: int, total: int):
                    if total > 0:
                        pct = min(100, blocks * bs * 100 // total)
                        self._env_log(f"下载中… {pct}%")

                urllib.request.urlretrieve(url, dest, reporthook=hook)
                return dest
            except Exception as exc:
                self._env_log(f"下载失败：{exc}", "err")
        return None

    def _vendor_bundle(self):
        """内嵌离线组件：(运行时 zip, wheels 目录)；没带就返回 None。"""
        if getattr(sys, "frozen", False):
            base = Path(getattr(sys, "_MEIPASS", "."))
        else:
            base = Path(__file__).resolve().parent / "_vendor"
        vdir = base / "vendor" if (base / "vendor").is_dir() else base
        zips = list(vdir.glob("python-*-embed-amd64.zip"))
        wheels = vdir / "wheels"
        if zips and wheels.is_dir():
            return zips[0], wheels
        return None

    @staticmethod
    def _dir_writable(d: Path) -> bool:
        try:
            probe = d / ".__write_probe__"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return True
        except Exception:
            return False

    def _env_offline(self, data_root: Path, runtime_zip: Path,
                     wheels_dir: Path) -> Path | None:
        """用随包的 Python 运行时 + wheel 离线配好环境（全程不联网）。"""
        rt = data_root / "runtime"
        py = rt / "python.exe"
        if not py.is_file():
            self._env_log("解压随包 Python 运行时 …", "dim")
            if rt.exists():
                shutil.rmtree(rt, ignore_errors=True)
            try:
                with zipfile.ZipFile(runtime_zip) as zf:
                    zf.extractall(rt)
            except Exception as exc:
                self._env_log(f"运行时解压失败：{exc}", "err")
                return None
            # 嵌入版默认只认 python312.zip；打开 site-packages 支持
            pth = rt / "python312._pth"
            if pth.is_file():
                pth.write_text("python312.zip\n.\nLib/site-packages\n"
                               "import site\n", encoding="utf-8")
        if not py.is_file():
            self._env_log("运行时解压后缺 python.exe。", "err")
            return None
        self._env_log(f"使用随包 Python 运行时：{py}", "ok")

        pip_whl = next(wheels_dir.glob("pip-*.whl"), None)
        if pip_whl is None:
            self._env_log("离线组件里缺 pip wheel。", "err")
            return None
        # 嵌入版不带 pip；且 ._pth 存在时是隔离模式，PYTHONPATH 会被忽略。
        # 把 pip wheel 路径临时写进 ._pth 引导 pip，装好后还原。
        pth = rt / "python312._pth"
        base_pth = "python312.zip\n.\nLib/site-packages\nimport site\n"
        if pth.is_file():
            pth.write_text(base_pth.replace("import site",
                                            f"{pip_whl}\nimport site"),
                           encoding="utf-8")
        self._env_log("初始化 pip（离线）…", "dim")
        code, _ = self._run_logged(
            [py, "-m", "pip", "install", "--no-index",
             "--no-warn-script-location",
             "--find-links", str(wheels_dir), "pip"])
        if pth.is_file():
            pth.write_text(base_pth, encoding="utf-8")
        if code != 0:
            self._env_log("pip 初始化失败。", "err")
            return None

        req = self.install_dir / "requirements.txt"
        self._env_log("安装依赖（全部随包，无需联网）…", "dim")
        code, _ = self._run_logged(
            [py, "-m", "pip", "install", "--no-index",
             "--no-warn-script-location",
             "--find-links", str(wheels_dir), "-r", str(req)])
        if code != 0:
            self._env_log("离线依赖安装失败。", "err")
            return None
        return py

    def _env_finish(self, vpy: Path):
        """收尾：OCR 预热（尽力）→ 自检 → 写解释器缓存 → 完成页。"""
        # 预热 OCR 模型：若 rapidocr 的模型不在 wheel 里，这次会把模型拉下来；
        # 离线组件场景下 wheel 已带模型，此步秒过。失败不阻塞安装。
        self._env_log("预热 OCR 模型…", "dim")
        warmup = (
            "import numpy as np\n"
            "try:\n"
            "    from rapidocr import RapidOCR\n"
            "    eng = RapidOCR()\n"
            "except ImportError:\n"
            "    from rapidocr_onnxruntime import RapidOCR\n"
            "    eng = RapidOCR(det_limit_side_len=1536, det_box_thresh=0.35,\n"
            "                   det_thresh=0.25, text_score=0.35,\n"
            "                   det_model_path=None)\n"
            "eng(np.full((64, 320, 3), 255, dtype='uint8'))\n"
            "print('ok')\n"
        )
        self._run_logged([vpy, "-c", warmup])

        sys.path.insert(0, str(self.install_dir / "app"))
        import paths                                 # noqa: E402
        # 注意：不能在这里调 paths.resolve()——Setup 本身是冻结 exe，
        # paths 会拿 sys.executable（下载目录里的 Setup）推 BASE_DIR，
        # 扫出的候选解释器里没有刚配好的运行时，必然误报全缺。
        # 直接对工作解释器做缺失自检：
        good, miss = paths.missing_modules(str(vpy), paths.WORKER_MODULES)
        if not good:
            self._env_log(f"配置完成但仍有缺失：{'、'.join(miss)}", "err")
            self._env_btn_retry()
            return
        # 把结果写进「正式应用」启动时会读的缓存位置。双写两处：
        # <安装目录>\app\_env.json（安装目录可写时应用从这读）和
        # %APPDATA%\<应用>\_env.json（应用判定安装目录只读时从这读）——
        # Setup 与应用对「目录可写」的判定可能因提权与否不一致，双写兜底。
        payload = json.dumps({"gui": "", "worker": str(vpy)},
                             ensure_ascii=False, indent=2)
        for cf in (self.install_dir / "app" / "_env.json",
                   user_data_dir() / "_env.json"):
            try:
                cf.parent.mkdir(parents=True, exist_ok=True)
                cf.write_text(payload, encoding="utf-8")
            except Exception:
                pass
        self._env_log(f"环境配置完成！工作解释器：{vpy}", "ok")
        self._ui(self._set_step, "环境已就绪", 100)
        self.after(800, lambda: self._show("finish"))

    def _env_worker(self):
        _dbg("=== env-worker start ===")
        try:
            # 运行时/venv 的落点与正式应用 paths.py 的规则保持一致：
            # 安装目录可写就放安装目录，否则放 %APPDATA%。
            data_root = (self.install_dir
                         if self._dir_writable(self.install_dir)
                         else user_data_dir())

            vendored = self._vendor_bundle()
            if vendored:
                self._env_log("检测到随包离线组件，全程无需联网。", "ok")
                vpy = self._env_offline(data_root, *vendored)
                if vpy is None:
                    self._env_btn_retry()
                    return
                self._env_finish(vpy)
                return

            base = self._pick_base_python()
            _dbg(f"base python = {base}")
            if base:
                self._env_log(f"使用本机 Python：{base}", "ok")
            else:
                self._env_log("本机没有可用的 Python，开始自动下载…", "dim")
                installer_exe = self._download_python(
                    Path(os.getenv("TEMP", ".")) /
                    f"python-{PY_RUNTIME_VER}-amd64.exe")
                if not installer_exe:
                    self._env_log("无法下载 Python（网络受限），"
                                  "可稍后手动安装 Python 3.10+ 后重试。", "err")
                    self._env_btn_retry()
                    return
                target = data_root / "runtime"
                self._env_log(f"静默安装 Python 到 {target} …", "dim")
                code, _ = self._run_logged([
                    installer_exe, "/quiet", "InstallAllUsers=0", "PrependPath=0",
                    "Include_launcher=0", "Include_test=0",
                    f"TargetDir={target}"], timeout=900)
                if code != 0 or not (target / "python.exe").is_file():
                    self._env_log("Python 静默安装失败。", "err")
                    self._env_btn_retry()
                    return
                base = str(target / "python.exe")
                self._env_log(f"Python 已装好：{base}", "ok")

            venv = data_root / "venv"
            if not (venv / "Scripts" / "python.exe").is_file():
                self._env_log(f"创建虚拟环境 {venv} …", "dim")
                code, _ = self._run_logged([base, "-m", "venv", str(venv)])
                if code != 0:
                    self._env_log("虚拟环境创建失败。", "err")
                    self._env_btn_retry()
                    return
            vpy = venv / "Scripts" / "python.exe"

            self._env_log("升级 pip（国内镜像）…", "dim")
            self._run_logged([vpy, "-m", "pip", "install", "--upgrade", "pip",
                              "-i", PYPI_MIRRORS[0], "--quiet", *PIP_COMMON])

            req = self.install_dir / "requirements.txt"
            self._env_log("安装依赖：pyyaml / pillow / numpy / rapidocr / "
                          "onnxruntime …（体积较大，需要几分钟）", "dim")
            code = 1
            for i, mirror in enumerate(PYPI_MIRRORS):
                code, out = self._run_logged(
                    [vpy, "-m", "pip", "install", "-r", str(req),
                     "-i", mirror, *PIP_COMMON])
                if code == 0:
                    break
                if i < len(PYPI_MIRRORS) - 1:
                    self._env_log("换备用镜像重试…", "dim")
            if code != 0:
                # 最后一次兜底：某些启动环境带着失效的代理变量，
                # 会让 pip 秒失败（versions: none）。去掉代理直连再试。
                self._env_log("镜像均失败，尝试不走代理直连…", "dim")
                clean_env = {k: v for k, v in os.environ.items()
                             if not k.lower().endswith("_proxy")}
                code, _ = self._run_logged(
                    [vpy, "-m", "pip", "install", "-r", str(req),
                     "-i", PYPI_MIRRORS[0], *PIP_COMMON], env=clean_env)
            if code != 0:
                self._env_log("依赖安装失败，请检查网络后重试。", "err")
                self._env_btn_retry()
                return

            self._env_finish(vpy)
        except Exception as exc:
            _dbg("env-worker CRASH:\n" + traceback.format_exc())
            traceback.print_exc()
            self._env_log(f"配置过程出错：{exc}", "err")
            self._env_btn_retry()

    def _env_btn_retry(self):
        def go():
            self.env_prg.stop()
            self.env_btn.configure(state="normal", text="重试")
        self.after(0, go)

    # ---------------- 完成页 ----------------

    def _page_finish(self, f: tk.Frame):
        box = self._card(f, "安装完成")
        tk.Label(box, text=f"{APP_NAME} {VERSION} 已安装到：",
                 bg="white", fg="#69728c",
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=16)
        tk.Label(box, text=str(self.install_dir), bg="white", fg="#1c2333",
                 font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w",
                                                               padx=16,
                                                               pady=(2, 4))
        tk.Label(box, text="桌面和开始菜单已创建快捷方式；配置与数据保存在\n"
                           f"{user_data_dir()}",
                 bg="white", fg="#69728c", justify="left",
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=16,
                                                      pady=(0, 4))
        tk.Label(box, text=f"版权所有 © {COPYRIGHT}", bg="white", fg="#9aa3b8",
                 font=("Microsoft YaHei UI", 8)).pack(anchor="w", padx=16,
                                                       pady=(0, 8))
        ttk.Checkbutton(box, text="立即启动程序",
                        variable=self.launch_after).pack(anchor="w", padx=16,
                                                         pady=(0, 12))
        bar = tk.Frame(f, bg="#eef1f7")
        bar.pack(side="bottom", fill="x", pady=14)
        ttk.Button(bar, text="完成", style="Accent.TButton",
                   command=self._finish).pack(side="right", padx=28)

    def _finish(self):
        if self.launch_after.get():
            exe = self.install_dir / f"{APP_NAME}.exe"
            try:
                subprocess.Popen([str(exe)], cwd=str(self.install_dir))
            except Exception as exc:
                messagebox.showerror("启动失败", str(exc), parent=self)
        self.destroy()

    # ---------------- 卸载 ----------------

    def _page_uninstall(self, f: tk.Frame):
        box = self._card(f, f"卸载 {APP_NAME}")
        self.uninst_info = tk.Label(
            box, text="正在读取安装信息…", bg="white", fg="#1c2333",
            justify="left", font=("Microsoft YaHei UI", 10))
        self.uninst_info.pack(anchor="w", padx=16, pady=(2, 8))
        ttk.Checkbutton(box, text="同时删除配置与消息记录（%APPDATA%）",
                        variable=self.wipe_data).pack(anchor="w", padx=16,
                                                      pady=(0, 12))
        self.uninst_log = tk.Text(box, height=8, font=("Microsoft YaHei UI", 9),
                                  bg="#fbfcfe", relief="flat", state="disabled",
                                  highlightbackground="#e2e7f0",
                                  highlightthickness=1)
        self.uninst_log.pack(fill="both", expand=True, padx=16, pady=(0, 12))

        bar = tk.Frame(f, bg="#eef1f7")
        bar.pack(side="bottom", fill="x", pady=14)
        self.uninst_btn = ttk.Button(bar, text="卸载", style="Accent.TButton",
                                     command=self._do_uninstall)
        self.uninst_btn.pack(side="right", padx=28)
        ttk.Button(bar, text="取消", width=8,
                   command=self.destroy).pack(side="right", padx=(0, 8))
        self.after(200, self._fill_uninst_info)

    def _fill_uninst_info(self):
        loc = self._detect_previous() and self._install_location()
        text = (f"安装位置：{loc or '（未知，将从注册表登记的位置卸载）'}\n"
                "快捷方式、开机自启与卸载登记将一并移除。")
        self.uninst_info.configure(text=text)
        if not loc:
            self.uninst_log.configure(state="normal")
            self.uninst_log.insert("end", "没有在注册表找到安装记录。\n")
            self.uninst_log.configure(state="disabled")

    def _install_location(self) -> str | None:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, UNINSTALL_KEY) as k:
                v, _t = winreg.QueryValueEx(k, "InstallLocation")
                return v
        except Exception:
            return None

    def _do_uninstall(self):
        if not messagebox.askyesno(
                "确认卸载", f"确定要卸载 {APP_NAME} 吗？", parent=self):
            return
        self.uninst_btn.configure(state="disabled")
        threading.Thread(target=self._uninstall_worker, daemon=True).start()

    def _ulog(self, msg: str):
        def go():
            self.uninst_log.configure(state="normal")
            self.uninst_log.insert("end", f"[{time.strftime('%H:%M:%S')}] "
                                          f"{msg}\n")
            self.uninst_log.see("end")
            self.uninst_log.configure(state="disabled")
        self.after(0, go)

    def _uninstall_worker(self):
        import time as _t
        self._ulog("正在移除快捷方式与开机自启…")
        env = os.environ.copy()
        env.update({"MM_NAME": APP_NAME, "MM_FOLDER": APP_NAME})
        _ps(PS_REMOVE, env)
        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                    winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, RUN_VALUE)
        except Exception:
            pass

        # ⚠ 必须**先**把安装位置取出来再删登记：一旦注册表项被删掉，
        #   _install_location() 就返回 None，只能退回默认目录 —— 装到自定义
        #   目录（--dir / 用户改过路径）时会变成「卸载看着成功、文件全留着」。
        loc = (_argv_value("--dir") or _argv_value("/d")
               or self._install_location() or str(default_install_dir()))

        self._ulog("正在移除卸载登记…")
        try:
            winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, UNINSTALL_KEY)
        except Exception:
            pass

        if Path(loc).is_dir():
            self._ulog(f"正在删除程序文件：{loc}")
            target = Path(loc)
            # 卸载程序自己就在安装目录里：先把自己挪走，再整目录删除。
            # 必须挪到**同一个卷**，跨卷 move 会退化成「复制 + 删原文件」，
            # 而运行中的 exe 删不掉 —— 见 _same_volume_tmp 的注释。
            moved_self: Path | None = None
            try:
                me = _self_exe()
                if me.resolve().is_relative_to(target.resolve()):
                    moved_self = _same_volume_tmp(target) / (
                        f"mm-uninst-{os.getpid()}{me.suffix}")
                    shutil.move(str(me), moved_self)
            except Exception:
                moved_self = None

            def onerr(func, path, _exc):
                try:
                    _t.sleep(0.2)
                    func(path)
                except Exception:
                    self._ulog(f"（保留）无法删除：{path}")
            shutil.rmtree(target, ignore_errors=False, onexc=onerr)
            if target.exists():
                # 兜底：还有删不掉的东西（最常见是运行中的 uninstall.exe，
                # 其次是被杀毒软件短暂占用），交给一个独立进程等我们退出再删。
                if _spawn_deferred_cleanup([target, moved_self]):
                    self._ulog("程序文件将在卸载程序退出后自动清除。")
                else:
                    self._ulog(f"（保留）请手动删除残留目录：{target}")
            else:
                self._ulog("程序文件已删除。")
                # 自己还在运行，挪出去的副本删不掉，同样交给独立进程收尾
                # （同卷时它在 %TEMP%，不同卷时在安装目录的父级，都别留下）
                if moved_self is not None:
                    _spawn_deferred_cleanup([moved_self])

        if self.wipe_data.get():
            d = user_data_dir()
            if d.is_dir():
                self._ulog(f"正在删除配置与数据：{d}")
                shutil.rmtree(d, ignore_errors=True)

        self._ulog("卸载完成。")
        self.after(0, lambda: messagebox.showinfo(
            "卸载完成", f"{APP_NAME} 已从这台电脑移除。", parent=self)
            or self.destroy())


def main() -> int:
    print(f"[安装程序] 模式：{'卸载' if IS_UNINSTALL else '安装'}"
          f"{'（静默）' if IS_SILENT else ''}", flush=True)
    if IS_UNINSTALL:
        # 卸载需要管理员权限删 Program Files
        if not _can_write(default_install_dir()):
            print("[安装程序] 无写入权限，请求管理员权限重启动…", flush=True)
            elevate_and_exit(sys.argv[1:])
    elif not _payload_zip():
        # 开发态直接跑脚本时给出提示
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "安装包不完整",
            "没有找到内嵌的程序文件（payload.zip）。\n"
            "请用 installer/build.py 重新构建安装包。")
        return 1
    app = Wizard()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
