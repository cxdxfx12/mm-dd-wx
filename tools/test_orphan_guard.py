"""实测「界面消失后工作进程自动退出」的看门狗。

    1. 起一个"假装是界面"的进程（睡 60 秒）；
    2. 用它的 PID 启一个真实的工作进程（--live --parent-pid ...）；
    3. 杀掉"界面"；
    4. 断言工作进程在若干秒内自己退出（否则说明会出现偷偷转发的孤儿进程）。

    python tools/test_orphan_guard.py
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "app"))

from bridge_worker import _pid_alive          # noqa: E402

WORKER_PY = str(Path(sys.executable))
NO_WINDOW = 0x08000000


def main() -> int:
    stop_file = BASE_DIR / "data" / "run.stop"
    stop_file.parent.mkdir(parents=True, exist_ok=True)
    stop_file.unlink(missing_ok=True)

    fake_gui = subprocess.Popen(
        [WORKER_PY, "-c", "import time; time.sleep(60)"],
        creationflags=NO_WINDOW)
    print(f"[1] 假装界面进程 pid={fake_gui.pid}")

    worker = subprocess.Popen(
        [WORKER_PY, str(BASE_DIR / "app" / "bridge_worker.py"),
         "--live", "--config", str(BASE_DIR / "config.yaml"),
         "--stop-file", str(stop_file),
         "--parent-pid", str(fake_gui.pid)],
        cwd=str(BASE_DIR), creationflags=NO_WINDOW,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")
    print(f"[2] 工作进程 pid={worker.pid}（父进程 = 假装界面）")

    time.sleep(12)
    if worker.poll() is not None:
        print("    ⚠ 工作进程提前退出，本次测试无效")
        fake_gui.kill()
        return 1
    print("    ✓ 界面存活时，工作进程保持运行")

    print(f"[3] 杀掉假装界面 pid={fake_gui.pid}")
    fake_gui.kill()
    fake_gui.wait(timeout=10)

    deadline = time.time() + 15
    while time.time() < deadline:
        if worker.poll() is not None:
            break
        time.sleep(0.3)

    code = worker.poll()
    if code is None:
        worker.kill()
        print("    ✗ 失败：界面已退出，但工作进程仍在运行（会成为孤儿进程）")
        return 1

    out = (worker.stdout.read() if worker.stdout else "") or ""
    hit = "自动停止转发" in out
    print(f"    ✓ 工作进程已自行退出（exit={code}）")
    print(f"    {'✓' if hit else '·'} 退出原因日志："
          f"{'识别到界面退出' if hit else '（未匹配到关键字）'}")
    stop_file.unlink(missing_ok=True)
    return 0 if hit else 1


if __name__ == "__main__":
    raise SystemExit(main())
