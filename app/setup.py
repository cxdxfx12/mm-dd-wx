"""一键安装：桌面 + 开始菜单快捷方式（含图标），并做环境自检。

    python app/setup.py            # 安装快捷方式
    python app/setup.py --remove   # 移除快捷方式与开机自启

为什么用 PowerShell 建快捷方式
    Windows 没有官方命令行快捷方式工具；WScript.Shell COM 是事实标准。
    脚本本体写成 UTF-8 BOM 的临时 .ps1 文件再执行，可以彻底绕开
    cmd 代码页（GBK/UTF-8）造成的中文乱码问题。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
BASE_DIR = APP_DIR.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import icon      # noqa: E402
import paths     # noqa: E402

NAME = "钉钉微信转发桥"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "DingTalkWeChatBridge"

PS_TEMPLATE = r"""$ErrorActionPreference = 'Stop'
$ws = New-Object -ComObject WScript.Shell
function Make($dir) {
  if (-not $dir -or -not (Test-Path $dir)) { return }
  $lnk = $ws.CreateShortcut((Join-Path $dir ($env:DDWX_NAME + '.lnk')))
  $lnk.TargetPath = $env:DDWX_PY
  $lnk.Arguments  = '"' + $env:DDWX_SCRIPT + '"'
  $lnk.WorkingDirectory = $env:DDWX_WD
  $lnk.IconLocation = $env:DDWX_ICO
  $lnk.Description = $env:DDWX_DESC
  $lnk.Save()
}
Make ([Environment]::GetFolderPath('Desktop'))
Make ([Environment]::GetFolderPath('Programs'))
Write-Output 'SHORTCUTS-OK'
"""

PS_REMOVE = r"""$ErrorActionPreference = 'SilentlyContinue'
foreach ($dir in @([Environment]::GetFolderPath('Desktop'),
                  [Environment]::GetFolderPath('Programs'))) {
  $p = Join-Path $dir ($env:DDWX_NAME + '.lnk')
  if (Test-Path $p) { Remove-Item $p -Force }
}
Write-Output 'REMOVED'
"""

CREATE_NO_WINDOW = 0x08000000


def message(text: str, title: str = NAME, error: bool = False) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x10 if error else 0x40)
    except Exception:
        print(text)


def _run_ps(script: str, env: dict) -> tuple[int, str]:
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False,
                                     encoding="utf-8-sig") as f:
        f.write(script)
        ps1 = f.name
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", ps1],
            env=env, capture_output=True, text=True, timeout=60,
            creationflags=CREATE_NO_WINDOW, encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    finally:
        try:
            os.unlink(ps1)
        except Exception:
            pass


def install() -> int:
    # 1) 图标（没有就现生成，保证快捷方式一定有图标可用）
    try:
        if not (APP_DIR / "app.ico").exists():
            icon.build()
        if not (APP_DIR / "icon_64.png").exists():
            (APP_DIR / "icon_64.png").write_bytes(
                icon._png(bytes(icon.render_rgba(64)), 64, 64))
    except Exception:
        pass

    # 2) 解释器
    info = paths.resolve(force=True)
    py = paths.windowed(info["gui"]) if info.get("gui_ok") else paths.windowed(
        info["worker"])
    script = APP_DIR / "gui.py"
    env = os.environ.copy()
    env.update({
        "DDWX_PY": py,
        "DDWX_SCRIPT": str(script),
        "DDWX_WD": str(BASE_DIR),
        "DDWX_ICO": str(APP_DIR / "app.ico"),
        "DDWX_NAME": NAME,
        "DDWX_DESC": "钉钉群消息自动转发到微信群",
    })

    code, out = _run_ps(PS_TEMPLATE, env)
    if "SHORTCUTS-OK" not in out:
        message("创建快捷方式失败：\n" + out.strip()[:600], error=True)
        return 1

    # 3) 顺手把工作解释器写进缓存，首次打开界面可以更快
    try:
        paths._save_cache({"gui": info["gui"], "worker": info["worker"]})
    except Exception:
        pass

    go = ctypes.windll.user32.MessageBoxW(
        0, f"安装完成！\n\n已在桌面和开始菜单创建「{NAME}」快捷方式。\n\n"
           f"界面解释器：{py}\n\n要现在启动吗？", NAME, 0x44)  # 4=yesno 64=info
    if go == 6:
        subprocess.Popen([py, str(script)], cwd=str(BASE_DIR))
    return 0


def remove() -> int:
    env = os.environ.copy()
    env["DDWX_NAME"] = NAME
    code, out = _run_ps(PS_REMOVE, env)
    try:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass
    except Exception:
        pass
    message("已移除桌面/开始菜单快捷方式与开机自启。")
    return 0


def main() -> int:
    if "--remove" in sys.argv or "-r" in sys.argv:
        return remove()
    if "--quiet" in sys.argv or "-q" in sys.argv:
        # 静默安装：不弹窗、不启动（供自动化 / 首次部署调用）
        return _install_shortcuts_only()
    return install()


def _install_shortcuts_only() -> int:
    try:
        if not (APP_DIR / "app.ico").exists():
            icon.build()
    except Exception:
        pass
    info = paths.resolve(force=True)
    py = paths.windowed(info["gui"]) if info.get("gui_ok") else paths.windowed(
        info["worker"])
    env = os.environ.copy()
    env.update({
        "DDWX_PY": py,
        "DDWX_SCRIPT": str(APP_DIR / "gui.py"),
        "DDWX_WD": str(BASE_DIR),
        "DDWX_ICO": str(APP_DIR / "app.ico"),
        "DDWX_NAME": NAME,
        "DDWX_DESC": "钉钉群消息自动转发到微信群",
    })
    code, out = _run_ps(PS_TEMPLATE, env)
    ok = "SHORTCUTS-OK" in out
    print(("已创建快捷方式：" if ok else "创建失败：") +
          f"桌面 / 开始菜单「{NAME}」\n界面解释器：{py}")
    if not ok:
        print(out.strip()[:600])
    try:
        paths._save_cache({"gui": info["gui"], "worker": info["worker"]})
    except Exception:
        pass
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
