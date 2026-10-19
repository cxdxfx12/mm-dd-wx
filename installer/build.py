"""构建「喵喵钉钉-微信消息转发桥」安装包。

用法（在 ddwx-bridge 目录下，用带 tkinter 的 Python 跑）::

    python installer/build.py

产出::

    installer/dist/喵喵钉钉-微信消息转发桥-Setup.exe

三步
    1. 用 PyInstaller 把 ``app/gui.py`` 打成应用主程序 exe（含 tkinter）；
    2. 把项目文件 + 主程序 exe 组装成 ``payload.zip``（安装时解压的内容）；
    3. 把 ``installer/setup_gui.py`` + payload.zip 打成单文件 Setup 安装包。

需要构建解释器带 tkinter（官方 Windows 安装包默认自带），并已安装
``pyinstaller``（``pip install pyinstaller``）。
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # ddwx-bridge/
HERE = Path(__file__).resolve().parent
BUILD = HERE / "_build"
DIST = HERE / "dist"
APP_NAME = "喵喵钉钉-微信消息转发桥"
PYINSTALLER = [sys.executable, "-m", "PyInstaller"]

EXCLUDE_DIR_NAMES = {"__pycache__", ".git", ".idea", ".vscode", "node_modules"}
EXCLUDE_FILE_SUFFIX = {".pyc", ".pyo", ".log", ".tmp", ".spec"}
EXCLUDE_FILE_NAMES = {"_env.json"}       # 开发机的解释器缓存，不随安装包分发
# config.yaml 也在排除之列（它是个文件，靠名字匹配即可生效）：
# 构建机的配置里有真实群名和钉钉机器人 access_token，打进安装包等于把凭证
# 随二进制一起分发；而且升级时整包解压会覆盖用户自己改过的配置。
# 首次安装由 setup_gui.py 从 config.example.yaml 生成。
# .git 必须排除：早先的排除只在「文件/目录名」层面生效，rglob 仍会钻进
# .git 把对象库整个搬走，既撑大安装包又可能因只读属性报 PermissionError。
EXCLUDE_TOP = {"data", "installer", "dist", "build", ".workbuddy", ".git",
               "config.yaml", ".gitignore"}

WELCOME = r"""
=============================================================
  构建「{name}」安装包
=============================================================
"""


def run(argv: list[str], cwd: Path) -> None:
    print("$ " + " ".join(argv), flush=True)
    r = subprocess.run(argv, cwd=str(cwd))
    if r.returncode != 0:
        raise SystemExit(f"命令失败（exit={r.returncode}）：{' '.join(argv)}")


def pyi(*args: str) -> None:
    run([*PYINSTALLER, "--noconfirm", "--clean", *args], cwd=ROOT)


def build_app_exe() -> Path:
    """应用主程序：tkinter GUI（不需要任何第三方包）。"""
    print("\n>>> 第 1 步：打包应用主程序 exe …", flush=True)
    name = APP_NAME
    pyi("--onefile", "--windowed", "--icon", str(ROOT / "app" / "app.ico"),
        "--name", name, "--paths", "app", str(ROOT / "app" / "gui.py"))
    out = ROOT / "dist" / f"{name}.exe"
    if not out.is_file():
        raise SystemExit("主程序 exe 没有生成")
    print(f"    主程序：{out}（{out.stat().st_size / 1e6:.1f} MB）", flush=True)
    return out


def want(p: Path) -> bool:
    if p.name in EXCLUDE_DIR_NAMES or p.name in EXCLUDE_FILE_NAMES:
        return False
    if p.suffix.lower() in EXCLUDE_FILE_SUFFIX:
        return False
    return True


def copy_tree(src: Path, dst: Path, top_excludes: set[str] | None = None
              ) -> set[str]:
    """复制目录（过滤缓存/垃圾文件），返回复制文件的相对路径集合。

    用 ``os.walk`` **逐层剪枝**，而不是 ``rglob`` 全量遍历后再过滤。
    区别很关键：``rglob`` 会继续钻进被排除的目录，再把里面的文件逐个搬过去，
    于是「排除 .git」只挡住了那个目录条目本身，对象库照样被完整复制
    （踩过：安装包被 .git 撑大，还会因 git 对象只读而报 PermissionError）。
    """
    copied: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(src):
        d = Path(dirpath)
        rel_dir = d.relative_to(src)
        if top_excludes and rel_dir.parts and rel_dir.parts[0] in top_excludes:
            dirnames[:] = []
            continue
        # 就地剪枝：这些子目录既不进入也不复制
        dirnames[:] = [n for n in dirnames if n not in EXCLUDE_DIR_NAMES]
        for name in filenames:
            p = d / name
            if not want(p):
                continue
            rel = p.relative_to(src)
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
            copied.add(rel.as_posix())
    return copied


def _force_unlink(p: Path) -> None:
    """删掉文件；只读的（比如 git 对象）先去只读位再删。

    Windows 上 ``unlink`` 碰到只读文件会直接 PermissionError，
    而 git 对象默认就是只读的。
    """
    try:
        p.chmod(p.stat().st_mode | stat.S_IWRITE)
    except Exception:
        pass
    p.unlink()


def build_payload(app_exe: Path) -> Path:
    """组装安装载荷：程序文件 + 主程序 exe。

    注意：这里**不做整目录删除**（既不安全也没必要）。做法是
    「覆盖写入 + 精准清理上一轮遗留的多余文件」，因此可以反复构建。
    """
    print("\n>>> 第 2 步：组装程序文件 payload …", flush=True)
    payload = BUILD / "payload"
    payload.mkdir(parents=True, exist_ok=True)

    expected: set[str] = set()
    # 1) ddwx-bridge 的程序内容（不含 data/ 等运行期目录）
    for item in ROOT.iterdir():
        if item.name in EXCLUDE_TOP:
            continue
        dst = payload / item.name
        if item.is_dir():
            # android/dist 里是全套历史 APK（含旧版本）。安装版只需要第 4 步
            # 挑出来的那一个，其余都是白占体积，而且旧包一旦被点到就会装上
            # 缺手势权限的版本 —— 长消息补全当场失效。源码本身留着（才几十 KB），
            # 出问题时能对着看。
            ex = {"dist"} if item.name == "android" else None
            expected |= {f"{item.name}/{r}" for r in copy_tree(item, dst, ex)}
        elif want(item):
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dst)
            expected.add(item.name)

    # 2) wxauto 库（config 里 wxauto_path 指向安装目录下的这个副本）
    wx = ROOT.parent / "wxauto-main"
    if wx.is_dir():
        expected |= {"wxauto-main/" + r for r in copy_tree(wx, payload / "wxauto-main")}

    # 3) 主程序 exe
    shutil.copy2(app_exe, payload / app_exe.name)
    expected.add(app_exe.name)

    # 4) 手机端 APK —— 放进 app/ 目录
    #
    # 手机装 App 走的是网关的 /app 页面（浏览器打开就能下），而安装版里
    # 没有 android/ 源码目录，所以 APK 必须随安装包一起发下去。
    # 位置与 gateway._apk_search_dirs() 里的「exe 同级 app/」一致。
    apk_src = ROOT / "android" / "dist"
    apks = sorted(apk_src.glob("*.apk"), key=lambda p: p.stat().st_mtime) \
        if apk_src.is_dir() else []
    if apks:
        latest = apks[-1]
        (payload / "app").mkdir(parents=True, exist_ok=True)
        shutil.copy2(latest, payload / "app" / latest.name)
        expected.add(f"app/{latest.name}")
        print(f"    手机端 APK：{latest.name}（{latest.stat().st_size // 1024} KB）")
        # `android/` 段会被整目录带进 payload（第 147 行只排除了 dist/），但
        # **升级时不清理老文件** —— 老版本装机时留下的 `android/dist/0.2.0.apk`
        # 会一直躺在新装的机器里。而 `_apk_search_dirs()` 又恰好把
        # `<程序目录>/android/dist` 列为候选，只要版本比较出了任何岔子，
        # 页面就会发回老包（2026-10-18 真机事故）。
        # 这里顺手写一份同版本的进去：同一个版本号出现两份，`_find_apk()` 取
        # mtime 最新的，`copy2` 保留了源文件 mtime，两份等价，不会歧义。
        adist = payload / "android" / "dist"
        adist.mkdir(parents=True, exist_ok=True)
        shutil.copy2(latest, adist / latest.name)
        expected.add(f"android/dist/{latest.name}")
    else:
        print("    ⚠ 没有找到 android/dist/*.apk —— 手机将无法用 /app 页面下载。"
              "先在 android 目录跑一次 python build.py")
    total = len(expected)

    # 清掉上一轮遗留、本轮不再需要的文件（精准删除，不做整目录清除）
    stale = 0
    for p in payload.rglob("*"):
        if p.is_file() and p.relative_to(payload).as_posix() not in expected:
            try:
                _force_unlink(p)
                stale += 1
            except Exception as exc:
                print(f"    （跳过）{p.name}: {exc}")
    if stale:
        print(f"    已清理上一轮遗留的 {stale} 个多余文件")
    # 顺带清掉因此变空的目录，避免下一轮又扫到
    for d in sorted((x for x in payload.rglob("*") if x.is_dir()),
                    key=lambda x: len(x.parts), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass

    zip_path = BUILD / "payload.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for rel in sorted(expected):
            zf.write(payload / rel, rel)
    size = zip_path.stat().st_size / 1e6
    print(f"    载荷：{len(expected)} 个文件 → {zip_path}（{size:.1f} MB）", flush=True)
    return zip_path


def app_version() -> str:
    """从 ``bridge/__init__.py`` 读版本号 —— 全项目唯一来源。

    不能 import bridge 来读：本脚本跑在构建解释器里，不一定装了项目依赖，
    而 bridge/__init__.py 一旦将来 import 了别的东西就会把打包卡死。
    """
    text = (ROOT / "bridge" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise SystemExit("bridge/__init__.py 里找不到 __version__")
    return m.group(1)


def build_setup(payload_zip: Path) -> Path:
    print("\n>>> 第 3 步：打包安装向导 Setup …", flush=True)
    # 安装向导里的 VERSION 是硬编码的常量（它被单独打成 Setup.exe，运行时
    # 拿不到项目源码）。这里在打包前把那一行替换成同一个版本号 ——
    # 否则会出现「界面写着 0.31、控制面板写着 1.0.0」的错位，
    # 用户报版本时会报错，排查方向跟着跑偏。
    ver = app_version()
    src = (HERE / "setup_gui.py").read_text(encoding="utf-8")
    patched, n = re.subn(r'^VERSION = "[^"]*"', f'VERSION = "{ver}"',
                         src, count=1, flags=re.M)
    if n != 1:
        raise SystemExit("setup_gui.py 里找不到 VERSION = \"...\" 这一行")
    staged = BUILD / "setup_gui.py"
    staged.write_text(patched, encoding="utf-8")
    print(f"    安装向导版本：{ver}（来自 bridge/__init__.py）", flush=True)
    pyi("--onefile", "--windowed", "--icon", str(ROOT / "app" / "app.ico"),
        "--name", f"{APP_NAME}-Setup",
        "--add-data", f"{payload_zip};.", str(staged))
    out = ROOT / "dist" / f"{APP_NAME}-Setup.exe"
    if not out.is_file():
        raise SystemExit("Setup 没有生成")
    return out


def main() -> int:
    print(WELCOME.format(name=APP_NAME))
    print(f"    版本：{app_version()}")
    BUILD.mkdir(exist_ok=True)
    DIST.mkdir(exist_ok=True)

    app_exe = build_app_exe()
    payload = build_payload(app_exe)
    setup = build_setup(payload)

    # 收拢产物到 installer/dist/
    final = DIST / f"{APP_NAME}-Setup.exe"
    shutil.move(str(setup), final)

    print("\n=============================================================")
    print(f"  安装包已生成：{final}")
    print(f"  大小：{final.stat().st_size / 1e6:.1f} MB")
    print("=============================================================")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
