# -*- coding: utf-8 -*-
"""只读：列出微信主进程加载的**非系统、非微信自身**模块。

用来判断第三方 DLL（输入法插件、显卡工具、录屏、杀软）是否注入微信。
原理：PSAPI EnumProcessModules + GetModuleFileNameExW。
"""
import ctypes
import ctypes.wintypes as w

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

TH32CS_SNAPPROCESS = 0x2
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", w.DWORD), ("cntUsage", w.DWORD),
        ("th32ProcessID", w.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
        ("th32ParentProcessID", w.DWORD),
        ("pcPriClassBase", ctypes.c_long), ("dwFlags", w.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


def pids_of(name):
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    pe = PROCESSENTRY32()
    pe.dwSize = ctypes.sizeof(pe)
    out = []
    ok = kernel32.Process32First(snap, ctypes.byref(pe))
    while ok:
        if pe.szExeFile.decode("mbcs", "ignore").lower() == name.lower():
            out.append(pe.th32ProcessID)
        ok = kernel32.Process32Next(snap, ctypes.byref(pe))
    kernel32.CloseHandle(snap)
    return out


def modules_of(pid):
    """注意：句柄必须声明为 c_void_p，否则 64 位下的 HMODULE 值会被截断/溢出。"""
    kernel32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    psapi.EnumProcessModules.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), w.DWORD,
        ctypes.POINTER(w.DWORD),
    ]
    psapi.EnumProcessModules.restype = w.BOOL
    psapi.GetModuleFileNameExW.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, w.DWORD,
    ]
    psapi.GetModuleFileNameExW.restype = w.DWORD

    h = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return []
    try:
        need = w.DWORD()
        arr = (ctypes.c_void_p * 1024)()
        if not psapi.EnumProcessModules(h, arr, ctypes.sizeof(arr), ctypes.byref(need)):
            return []
        n = need.value // ctypes.sizeof(ctypes.c_void_p)
        buf = ctypes.create_unicode_buffer(1024)
        out = []
        for i in range(min(n, 1024)):
            if psapi.GetModuleFileNameExW(h, arr[i], buf, 1024):
                out.append(buf.value)
        return out
    finally:
        kernel32.CloseHandle(h)


def main():
    best = None
    for pid in pids_of("Weixin.exe"):
        mods = modules_of(pid)
        if best is None or len(mods) > len(best[1]):
            best = (pid, mods)
    if not best:
        print("没有找到 Weixin.exe 进程")
        return 1
    pid, mods = best
    print("主进程 PID=%d 模块数=%d" % (pid, len(mods)))
    third = [
        m for m in mods
        if not m.lower().startswith("c://windows//")
        and "tencent\\weixin" not in m.lower()
    ]
    print("非系统、非微信目录的模块 %d 个：" % len(third))
    for m in third:
        print("   ", m)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
