"""拉取安装包的离线组件（换构建机时先跑这个）。

用法（用任意 Python 3.8+ 跑，需要能访问网络）::

    python installer/fetch_vendor.py

下载到 installer/_vendor/：
  * python-3.12.10-embed-amd64.zip   嵌入版运行时（华为云镜像）
  * wheels/*.whl                     requirements.txt 全量依赖 + pip
                                     （阿里云 PyPI 镜像，cp312/win_amd64）

注意：wheel 的平台/版本跟随**本机解释器**（用 3.12 跑就下 cp312 的），
构建解释器与 PY_RUNTIME_VER 保持同为 3.12 最省事。
"""
from __future__ import annotations

import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENDOR = HERE / "_vendor"
WHEELS = VENDOR / "wheels"
ROOT = HERE.parent

PY_EMBED_VER = "3.12.10"
EMBED_URLS = [
    f"https://mirrors.huaweicloud.com/python/{PY_EMBED_VER}/"
    f"python-{PY_EMBED_VER}-embed-amd64.zip",
    f"https://www.python.org/ftp/python/{PY_EMBED_VER}/"
    f"python-{PY_EMBED_VER}-embed-amd64.zip",
]
PIP_INDEX = "https://mirrors.aliyun.com/pypi/simple/"


def fetch(url: str, dest: Path) -> bool:
    print(f"下载 {url}")
    try:
        urllib.request.urlretrieve(url, dest)
        return True
    except Exception as exc:
        print(f"  失败：{exc}")
        return False


def main() -> int:
    VENDOR.mkdir(parents=True, exist_ok=True)
    WHEELS.mkdir(parents=True, exist_ok=True)

    embed = VENDOR / f"python-{PY_EMBED_VER}-embed-amd64.zip"
    if embed.is_file() and embed.stat().st_size > 5_000_000:
        print(f"运行时已存在：{embed}")
    else:
        for url in EMBED_URLS:
            if fetch(url, embed):
                break
        else:
            print("嵌入版 Python 下载失败")
            return 1

    print("下载依赖 wheel（几分钟，onnxruntime/opencv 体积较大）…")
    # pycryptodome / windows-capture 不写在 requirements.txt 里（装失败会拖垮
    # 主环境，见 setup_gui 的 _env_pycryptodome / _env_wgc），但安装器要用
    # 离线 wheel 单独装，所以这里必须显式一起拉。
    r = subprocess.run([
        sys.executable, "-m", "pip", "download",
        "-r", str(ROOT / "requirements.txt"), "pip",
        "pycryptodome", "windows-capture",
        "-d", str(WHEELS), "--only-binary=:all:", "-i", PIP_INDEX,
    ])
    if r.returncode != 0:
        print("wheel 下载失败")
        return 1

    n = len(list(WHEELS.glob("*.whl")))
    print(f"\n完成：{embed.name} + {n} 个 wheel → {VENDOR}")
    print("现在可以运行 python installer/build.py 打包了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
