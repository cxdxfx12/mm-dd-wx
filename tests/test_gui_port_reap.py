"""启动前收掉残留 worker（``app/gui.py::_reap_stale_worker``）。

事故（2026-11-02 15:29）
    用户点「启动转发服务」，界面只留一行「工作进程异常退出」，此后一条
    消息、一个附件都没转发。根因不在采集也不在投递：上一轮排查时手动起
    的 worker 没带 ``--parent-pid``，界面关掉它还在跑、一直占着 8765，
    新 worker 起网关时 ``WinError 10048`` 当场退出。

这里钉三件事：
    1. 只认 ``LISTENING`` —— ``TIME_WAIT``/``FIN_WAIT_2`` 是已断开的旧连接，
       当成占用会误杀正常进程；
    2. 只杀**确认是 bridge_worker.py** 的进程；
    3. 端口被别的程序占着时原样报错，不替用户杀别人的程序。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app import gui  # noqa: E402

# 真机 netstat 形状（中文 Windows，列头是「协议」；含 IPv6 与半开连接）
NETSTAT = """
活动连接

  协议  本地地址          外部地址        状态           PID
  TCP    0.0.0.0:8765           0.0.0.0:0              LISTENING       33548
  TCP    192.168.1.209:8765     192.168.1.218:57116    FIN_WAIT_2      33548
  TCP    192.168.1.209:8765     192.168.1.218:46092    TIME_WAIT       0
  TCP    [::]:8765              [::]:0                 LISTENING       40001
  TCP    0.0.0.0:8080           0.0.0.0:0              LISTENING       777
"""


def _fake_run(stdout: str):
    """按 args[0] 分流的 subprocess.run 替身（netstat / powershell / taskkill）。"""
    calls: list[list[str]] = []

    def run(argv, *a, **kw):
        calls.append(list(argv))
        out = stdout if argv and argv[0] == "netstat" else ""
        return mock.Mock(stdout=out, returncode=0)

    run.calls = calls                                    # type: ignore[attr-defined]
    return run


class TestPortOwnerScan(unittest.TestCase):
    def test_only_listening_counts(self):
        """只认 LISTENING：TIME_WAIT / FIN_WAIT_2 不算占用。"""
        with mock.patch.object(gui.subprocess, "run", _fake_run(NETSTAT)):
            pids = gui._port_owner_pids(8765)
        self.assertEqual(sorted(pids), [33548, 40001])

    def test_other_port_not_mixed_in(self):
        """8080 的监听者不能算到 8765 头上（端口号按整段比，不做子串）。"""
        with mock.patch.object(gui.subprocess, "run", _fake_run(NETSTAT)):
            self.assertNotIn(777, gui._port_owner_pids(8765))

    def test_free_port(self):
        with mock.patch.object(gui.subprocess, "run", _fake_run("")):
            self.assertEqual(gui._port_owner_pids(8765), [])

    def test_netstat_failure_is_not_fatal(self):
        """netstat 起不来（权限/被杀）时当成空闲，别把启动卡死。"""
        with mock.patch.object(gui.subprocess, "run",
                               side_effect=OSError("no netstat")):
            self.assertEqual(gui._port_owner_pids(8765), [])


class TestReap(unittest.TestCase):
    def test_free_path_does_nothing(self):
        run = _fake_run("")
        with mock.patch.object(gui.subprocess, "run", run):
            how, why = gui._reap_stale_worker(8765)
        self.assertEqual((how, why), ("free", ""))
        self.assertFalse([c for c in run.calls if c[:1] == ["taskkill"]])

    def test_kills_own_worker(self):
        """占用者是我们自己的 worker → 收掉，端口释放后返回 killed。"""
        run = _fake_run("")
        with mock.patch.object(gui, "_port_owner_pids",
                               side_effect=[[33548], [], []]), \
             mock.patch.object(gui, "_proc_cmdline",
                               return_value='python.exe "C:/x/app/bridge_worker.py" '
                                            "--live --config C:/x/config.yaml"), \
             mock.patch.object(gui.subprocess, "run", run):
            how, why = gui._reap_stale_worker(8765)
        self.assertEqual(how, "killed")
        self.assertEqual(why, "")
        kills = [c for c in run.calls if c[:1] == ["taskkill"]]
        self.assertEqual(len(kills), 1)
        self.assertIn("33548", kills[0])
        self.assertIn("/T", kills[0])                    # 连子进程一起收

    def test_never_kills_foreign_program(self):
        """端口被别的程序占着：报错，一个 taskkill 都不许发。"""
        run = _fake_run("")
        with mock.patch.object(gui, "_port_owner_pids", return_value=[999]), \
             mock.patch.object(gui, "_proc_cmdline",
                               return_value=r"C:\Other\pretender.exe --serve"), \
             mock.patch.object(gui.subprocess, "run", run):
            how, why = gui._reap_stale_worker(8765)
        self.assertEqual(how, "other")
        self.assertIn("999", why)
        self.assertIn("pretender.exe", why)
        self.assertFalse([c for c in run.calls if c[:1] == ["taskkill"]])

    def test_reports_when_kill_does_not_free_port(self):
        """杀不掉（权限不足）时要说清楚，别静默继续启动又撞一次 10048。"""
        run = _fake_run("")
        with mock.patch.object(gui, "_port_owner_pids", return_value=[33548]), \
             mock.patch.object(gui, "_proc_cmdline",
                               return_value="python.exe bridge_worker.py"), \
             mock.patch.object(gui.subprocess, "run", run), \
             mock.patch.object(gui.time, "sleep"):
            how, why = gui._reap_stale_worker(8765)
        self.assertEqual(how, "stuck")
        self.assertIn("释放不掉", why)


class TestWiring(unittest.TestCase):
    def test_start_live_reaps_before_spawning(self):
        """接点在 _start_live 里，且必须在 runner.start 之前 —— 顺序反了等于没装。"""
        src = (BASE_DIR / "app" / "gui.py").read_text(encoding="utf-8")
        self.assertIn("_reap_stale_worker()", src)
        at = src.index("_reap_stale_worker()")
        spawn = src.index("self.runner.start(", at)
        self.assertLess(at, spawn)

    def test_blocked_start_does_not_flip_state_to_running(self):
        """端口被外人占用时不能把状态设成 running：界面会假装在转发。"""
        src = (BASE_DIR / "app" / "gui.py").read_text(encoding="utf-8")
        at = src.index("_reap_stale_worker()")
        block = src[at:src.index("argv_live = self._worker_argv(", at)]
        self.assertIn('self.state = "idle"', block)
        self.assertIn("return", block)


if __name__ == "__main__":
    unittest.main()
