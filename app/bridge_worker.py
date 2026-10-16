"""工作进程：用「带重依赖的解释器」运行 bridge 的真正逻辑。

职责
    GUI 进程只带 tkinter，装不下 rapidocr / onnxruntime 这些重依赖；
    所有真正的采集与投递都在本进程里跑，GUI 通过管道读它的输出。

输出协议
    * 日志     —— ``@@LOG@@{"t":..., "level":..., "name":..., "msg":...}``
    * 其余输出 —— 原样透传（GUI 当普通文本显示）

用法（由 GUI 调用，也可手工运行）
    python app/bridge_worker.py --live --config config.yaml      # 正式运行
    python app/bridge_worker.py --check                          # 自检
    python app/bridge_worker.py --dump-config                    # 打印配置 JSON
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

LOG_PREFIX = "@@LOG@@"
_HEAD = "@@WORKER@@"


def emit(kind: str, **payload) -> None:
    """向 GUI 发一条机器可读的控制信息。"""
    payload["kind"] = kind
    print(_HEAD + json.dumps(payload, ensure_ascii=False), flush=True)


class _JsonLogHandler(logging.Handler):
    """把 logging 输出变成单行 JSON，GUI 按级别着色、按内容统计。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        data = {
            "t": datetime.now().strftime("%H:%M:%S"),
            "level": record.levelname,
            "name": record.name,
            "msg": msg,
        }
        try:
            sys.stdout.write(LOG_PREFIX + json.dumps(data, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except Exception:
            pass


def _pop_arg(argv: list[str], flag: str) -> str | None:
    """取出 ``--flag value`` 形式的本进程专用参数，并从 argv 里摘掉。

    run.py 的 argparse 不认识这些参数，必须在转交前剥离。
    """
    if flag not in argv:
        return None
    i = argv.index(flag)
    value = argv[i + 1] if i + 1 < len(argv) else None
    if value is not None:
        del argv[i:i + 2]
    else:
        del argv[i:i + 1]
    return value


def _pid_alive(pid: int) -> bool:
    """判断某个进程是否还活着（Windows）。"""
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def _watch_stop_file(path: str, parent_pid: int | None = None,
                     parent_name: str = "界面") -> None:
    """看门狗：停止哨兵文件出现、或**界面进程消失**时，在主线程抛 KeyboardInterrupt。

    Windows 下 GUI 无法向无控制台的子进程投递 CTRL_BREAK，所以用文件握手。
    抛在主线程才能被 run.py 的 ``except KeyboardInterrupt`` 接住并收尾。

    监视父进程（界面）是必需的：界面被强杀 / 直接关掉时来不及写哨兵文件，
    工作进程会变成没人管的"孤儿"——它仍在偷偷转发，用户点「停止」也停不掉。
    """
    import ctypes
    import threading
    import time as _time
    from pathlib import Path as _Path

    marker = _Path(path)
    main_id = threading.main_thread().ident
    while True:
        if marker.exists():
            break
        if parent_pid and not _pid_alive(parent_pid):
            print(f"[worker] {parent_name}已退出，自动停止转发", flush=True)
            break
        _time.sleep(0.25)
    if main_id is None:
        return
    ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_long(main_id), ctypes.py_object(KeyboardInterrupt))


def dump_config(argv: list[str]) -> int:
    """把配置转成 JSON 打印（GUI 没有 yaml，靠这一步拿配置值）。"""
    from bridge.config import load_config
    from dataclasses import asdict, is_dataclass

    path = argv[argv.index("--dump-config") + 1] if len(argv) > 1 else str(
        BASE_DIR / "config.yaml")
    try:
        cfg = load_config(path)
    except Exception as exc:
        emit("config-error", message=str(exc))
        return 2

    def plain(obj):
        if is_dataclass(obj):
            return {k: plain(v) for k, v in asdict(obj).items()}
        if isinstance(obj, list):
            return [plain(v) for v in obj]
        return obj

    emit("config", data=plain(cfg))
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if "--dump-config" in argv:
        return dump_config(argv)

    stop_file = _pop_arg(argv, "--stop-file")
    parent_raw = _pop_arg(argv, "--parent-pid")
    parent_pid = int(parent_raw) if (parent_raw or "").isdigit() else None
    if stop_file:
        try:
            Path(stop_file).unlink(missing_ok=True)
        except Exception:
            pass
        threading.Thread(target=_watch_stop_file,
                         args=(stop_file, parent_pid),
                         daemon=True, name="stop-watch").start()

    # 装上 JSON 日志处理器后，run.py 里的 logging.basicConfig 会自动让位
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handler = _JsonLogHandler()
    handler.setFormatter(logging.Formatter())
    root.addHandler(handler)

    emit("start", argv=argv)
    import run                                   # noqa: E402  项目入口
    sys.argv = [str(BASE_DIR / "run.py"), *argv]
    try:
        code = run.main()
    except SystemExit as exc:                    # argparse 用它退出
        code = int(exc.code or 0)
    except KeyboardInterrupt:
        print("已停止")
        code = 0
    except Exception:
        emit("crash", traceback=traceback.format_exc())
        code = 1
    emit("exit", code=code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
