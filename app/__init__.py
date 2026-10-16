"""桌面版「钉钉 → 微信 消息转发桥」。

模块划分
    paths.py          解析运行所需的两个 Python 解释器
    proc.py           子进程封装（启动 / 停止 / 日志流）
    cfgfile.py        保留注释地读写 config.yaml
    icon.py           生成程序图标 .ico
    bridge_worker.py  工作进程（用带重依赖的解释器运行 run.py）
    gui.py            主窗口
"""
