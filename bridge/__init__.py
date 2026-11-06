"""钉钉 → 微信 消息转发桥。

版本号规则（用户 2026-10-19 定）：

    **每发布一次更新或修复，末位 +0.01。**  0.31 → 0.32 → 0.33 …

    这是**整个桌面程序**的版本，显示在软件底部状态栏和「关于」里。
    手机端 APK 有自己的版本号（android/app/build.gradle 的 versionName），
    两者独立演进，不要互相参照。

这个文件是**唯一**的版本来源：
  · ``app/gui.py``           底部状态栏 / 关于对话框直接读它；
  · ``installer/build.py``   打包时把它写进安装向导（注册表的 DisplayVersion、
                             安装完成提示、控制面板里的版本都跟着变）。

所以改版本只需要动这一行，别再在别处硬编码。
"""

__version__ = "0.62"

# 任何 import bridge.* 的进程（worker / GUI / 菜单里的 tools 脚本 / 单测）
# 都必须在碰窗口 API 之前声明 Per-Monitor V2。以前只有 run.py 和 GUI 的
# __main__ 做这件事，菜单子进程一跑就错位。放在包初始化里是唯一能保证
# 「所有入口都过一遍」的位置；win_dpi.enable() 幂等，重复调用无害。
from .win_dpi import enable as _enable_dpi  # noqa: E402

_enable_dpi()
