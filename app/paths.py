"""解释器解析与基础路径（只用标准库，任何 Python 都能导入）。

为什么需要两个解释器
    本程序分两个进程跑：

      GUI 进程   —— 需要 ``tkinter``（Python 标准库自带），不需要任何第三方包，
                    这样界面能秒开，也不会被 OCR 那些大依赖拖慢。
      工作进程   —— 需要 ``pyyaml / pillow / numpy / rapidocr（或旧包
                    rapidocr-onnxruntime）`` 才能真正采集钉钉消息、投递微信。

    这台机器上的现实是：
      * 系统 Python 3.12.4（``%LOCALAPPDATA%\\Programs\\Python\\Python312``）
        带 tkinter，但没有装任何重依赖；
      * 托管 venv（``~/.workbuddy/binaries/python/envs/default``，Python 3.13.14）
        装了全部重依赖，但没有 tkinter。

    所以两个解释器必须分开解析。解析结果缓存在 ``app/_env.json``，
    下次启动直接复用；界面上有「重新检测环境」可以强制重扫。

覆盖方式
    环境变量 ``DDWX_GUI_PYTHON`` / ``DDWX_WORKER_PYTHON`` 指定的解释器优先。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

APP_NAME = "喵喵钉钉-微信消息转发桥"


def _detect_roots() -> tuple[Path, Path]:
    """定位 APP_DIR 与 BASE_DIR。

    打包成 exe 后 ``__file__`` 指向 PyInstaller 的解压临时目录（_MEIPASS），
    不能再用它推 BASE_DIR；此时必须以 **exe 自身所在目录** 为准——安装后
    ``<安装目录>\\喵喵钉钉-微信消息转发桥.exe`` 旁边就是 ``app/``、
    ``bridge/``、``config.yaml``。
    """
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        return exe_dir / "app", exe_dir
    app = Path(__file__).resolve().parent
    return app, app.parent


APP_DIR, BASE_DIR = _detect_roots()


def _is_writable(d: Path) -> bool:
    try:
        probe = d / ".__write_probe__"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def user_data_dir() -> Path:
    """用户可写数据目录（``%APPDATA%\\喵喵钉钉-微信消息转发桥``）。"""
    base = os.getenv("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    d = Path(base) / APP_NAME
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


# 安装到 C:\Program Files 时程序目录对普通用户是只读的，
# 此时 config / 数据库 / 日志必须落到 %APPDATA%；开发态（目录可写）维持原样。
BASE_DIR_WRITABLE = _is_writable(BASE_DIR)
DATA_ROOT = BASE_DIR if BASE_DIR_WRITABLE else user_data_dir()

CACHE_FILE = (APP_DIR / "_env.json") if BASE_DIR_WRITABLE else (DATA_ROOT / "_env.json")

NO_WINDOW = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW

GUI_MODULES = ("tkinter",)
# OCR 包二选一：新版 rapidocr（3.x / PP-OCRv6）或旧包 rapidocr_onnxruntime
# （1.x / PP-OCRv3，回退）。用「组」表达任一语义，见 missing_modules。
OCR_MODULE_GROUPS = (("rapidocr", "rapidocr_onnxruntime"),)
WORKER_MODULES = ("yaml", "PIL", "numpy", "onnxruntime") + OCR_MODULE_GROUPS
WORKER_ESSENTIAL = ("yaml", "PIL", "numpy")        # 缺这几个就一定跑不起来


def _run(exe: str, args: list[str], timeout: float = 20.0):
    try:
        return subprocess.run(
            [exe, *args], capture_output=True, text=True, timeout=timeout,
            creationflags=NO_WINDOW, encoding="utf-8", errors="replace",
        )
    except Exception:
        return None


def missing_modules(exe: str, modules: tuple[str, ...]) -> tuple[bool, list[str]]:
    """在指定解释器里查这些模块是否存在（用 find_spec，不真正导入，快）。

    ``modules`` 的元素可以是模块名字符串，也可以是**字符串元组** ——
    后者表示「组内任一存在即算有」（用于 OCR 新旧包二选一）。
    缺失项的报告中组内名字用 ``|`` 连接。
    """
    groups: list[list[str]] = [
        [m] if isinstance(m, str) else list(m) for m in modules
    ]
    code = (
        "import importlib.util as u\n"
        "groups = %r\n"
        "miss = []\n"
        "for g in groups:\n"
        "    found = False\n"
        "    for m in g:\n"
        "        try:\n"
        "            if u.find_spec(m) is not None:\n"
        "                found = True\n"
        "                break\n"
        "        except Exception:\n"
        "            pass\n"
        "    if not found:\n"
        "        miss.append('|'.join(g))\n"
        "print(','.join(miss))\n" % (groups,)
    )
    r = _run(exe, ["-c", code])
    if r is None or r.returncode != 0:
        return False, ["|".join(g) for g in groups]
    miss = [m for m in (r.stdout or "").strip().split(",") if m.strip()]
    return (not miss), miss


_PY_NAME_RE = re.compile(r"^python(w|_d)?(\d+(\.\d+)*)?t?\.exe$", re.IGNORECASE)


def _looks_like_python(p: str | Path) -> bool:
    """这个可执行文件是不是一个 Python 解释器？

    ⚠ 这条判断是**安全阀**：打包成 exe 之后 ``sys.executable`` 指向的是
    本程序自己。如果把它当成解释器去执行 ``-c "..."``，冻结版 exe 会忽略
    命令行参数、直接再开一个主窗口——每探测一次就多一个窗口，新实例又去
    探测，于是无限自我复制（表现为「不停地弹窗口」）。所以任何候选解释器
    都必须先过这道命名检查。

    只认 ``python.exe / pythonw.exe / python3.exe / python3.12.exe`` 这类
    标准命名，别的（包括名字里带 python 的第三方程序）一律不认。
    """
    return bool(_PY_NAME_RE.match(Path(str(p)).name))


def _candidate_pythons() -> list[str]:
    """按可能性从高到低列出本机可能存在的 python.exe。"""
    out: list[str] = []

    def add(p: str | Path) -> None:
        try:
            p = str(Path(p))
        except Exception:
            return
        if p and p not in out and Path(p).is_file() and _looks_like_python(p):
            out.append(p)

    # 1) 当前解释器（可能就已经够用）
    #    冻结成 exe 时必须跳过——那指向的是本程序，不是解释器
    if not getattr(sys, "frozen", False):
        add(sys.executable)

    # 2) 环境变量指定的
    for env in ("DDWX_GUI_PYTHON", "DDWX_WORKER_PYTHON"):
        v = os.getenv(env)
        if v:
            add(v)

    # 3) 项目自带 venv
    add(BASE_DIR / ".venv" / "Scripts" / "python.exe")

    # 3.5) 安装程序自动配置出来的运行时（用户目录优先，其次安装目录）
    for root in (DATA_ROOT, BASE_DIR):
        for sub in ("venv", "runtime", "python"):
            add(root / sub / "Scripts" / "python.exe")
            add(root / sub / "python.exe")

    # 4) 托管 venv（本机重依赖都在这里）
    add(Path.home() / ".workbuddy" / "binaries" / "python" / "envs" / "default"
        / "Scripts" / "python.exe")

    # 5) 常见安装位置：用户级 / 系统级 / 商店版占位
    roots = [
        Path(os.getenv("LOCALAPPDATA", "")) / "Programs" / "Python",
        Path(os.getenv("LOCALAPPDATA", "")) / "Python",
        Path(os.getenv("ProgramFiles", "C:/Program Files")) / "Python",
        Path("C:/Python312"), Path("C:/Python311"), Path("C:/Python310"),
    ]
    for root in roots:
        if not root.is_dir():
            continue
        try:
            subdirs = sorted([d for d in root.iterdir() if d.is_dir()], reverse=True)
        except Exception:
            continue
        for d in subdirs:
            add(d / "python.exe")
        add(root / "python.exe")

    # 6) PATH 上的
    for name in ("python.exe", "python3.exe"):
        w = shutil.which(name)
        if w:
            add(w)
    return out


def _load_cache() -> dict:
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cache(data: dict) -> None:
    try:
        CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception:
        pass


def resolve(force: bool = False) -> dict:
    """返回 ``{"gui": ..., "worker": ..., "gui_missing": [...], "worker_missing": [...]}``。

    ``gui``   —— 带 tkinter 的解释器（能跑界面）；
    ``worker`` —— 能跑采集/投递的解释器。
    """
    cache = {} if force else _load_cache()

    def ok(exe: str | None, modules: tuple[str, ...]) -> bool:
        return bool(exe) and Path(exe).is_file() and missing_modules(exe, modules)[0]

    gui = cache.get("gui")
    if not ok(gui, GUI_MODULES):
        gui = None
        for exe in _candidate_pythons():
            good, _miss = missing_modules(exe, GUI_MODULES)
            if good:
                gui = exe
                break

    worker = cache.get("worker")
    if not ok(worker, WORKER_ESSENTIAL):
        worker = None
        # 优先找「重依赖齐全」的；退而求其次只要有 essentials
        best_partial: tuple[str, list[str]] | None = None
        for exe in _candidate_pythons():
            good, _miss = missing_modules(exe, WORKER_MODULES)
            if good:
                worker = exe
                break
            good_ess, miss_ess = missing_modules(exe, WORKER_ESSENTIAL)
            if good_ess and best_partial is None:
                best_partial = (exe, miss_ess)
        if worker is None and best_partial:
            worker = best_partial[0]

    # 冻结成 exe 时 sys.executable 是本程序自己，不能拿来当解释器兜底；
    # 这时宁可留空，让界面明确提示"缺少运行环境"，也不要启动自己。
    self_exe_fallback = "" if getattr(sys, "frozen", False) else sys.executable

    if worker is None:
        worker = gui or self_exe_fallback

    info = {
        "gui": gui or self_exe_fallback,
        "worker": worker,
        "gui_missing": missing_modules(gui or self_exe_fallback, GUI_MODULES)[1],
        "worker_missing": missing_modules(worker, WORKER_MODULES)[1],
        "gui_ok": bool(gui),
        "worker_ok": bool(worker),
    }
    _save_cache({"gui": info["gui"], "worker": info["worker"]})
    return info


def windowed(exe: str) -> str:
    """把 python.exe 换成同目录的 pythonw.exe（不弹黑框）。"""
    p = Path(exe)
    pw = p.with_name("pythonw.exe")
    return str(pw) if pw.is_file() else exe


def config_path() -> Path:
    """配置文件位置。

    程序目录可写（开发 / 便携）→ 直接用程序目录里的 config.yaml，行为与以前一致；
    程序目录只读（装在 C:\\Program Files）→ 首次从程序目录**播种**一份到
    %APPDATA%，之后读写都走 %APPDATA%，普通用户权限下也能保存设置。
    """
    if BASE_DIR_WRITABLE:
        return BASE_DIR / "config.yaml"
    target = DATA_ROOT / "config.yaml"
    if not target.is_file():
        src = BASE_DIR / "config.yaml"
        if src.is_file():
            try:
                shutil.copy2(src, target)
            except Exception:
                return src
    return target


def data_dir(*parts: str) -> Path:
    """数据目录（数据库 / 备份 / 日志），与 config 同一个可写根下。"""
    d = DATA_ROOT / "data"
    for p in parts:
        d = d / p
    return d


def venv_hint() -> str:
    return (
        "未找到同时满足依赖的 Python 解释器。\n"
        "界面需要一个带 tkinter 的 Python（官方安装包默认自带）；\n"
        f"采集/投递需要 pyyaml、pillow、numpy、rapidocr（或旧包"
        f" rapidocr-onnxruntime）。\n"
        f"当前工作解释器：{resolve().get('worker')}"
    )


if __name__ == "__main__":       # 命令行自检：python app/paths.py
    for k, v in resolve(force=True).items():
        print(f"{k}: {v}")
