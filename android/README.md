# 手机端采集器（Android）

把**钉钉安卓客户端当前界面的无障碍节点树**读出来，POST 给电脑上的转发桥。

它替换的是 `dingtalk.mode: ocr` 那条路：OCR 是「截图 + 认字」，这一条是「直接读文本」，
拿到的是**精确原文**，不需要 OCR、也不需要为 OCR 打的那一整套补丁
（标题护栏、锚点防护、贴底检测、相似度匹配）。

---

## 为什么是自建 App

原本的第一选择是「电脑端定时 `adb shell uiautomator dump`」——手机端一行代码都不用写。
但在这台华为 Mate Xs 2（HarmonyOS）上，**USB 调试怎么都打不开**：

| 排查项 | 结论 |
|---|---|
| 拔插是否生效 | ✓ 生效（设备接入时间每次都在变） |
| 数据线 / 接口 / 供电 | ✓ 正常（MTP 一直通） |
| Windows 驱动 | ✓ 正常（无任何未知设备） |
| USB 集线器 | ✓ 排除（设备直挂主板根集线器） |
| 华为电脑管家进程 | ✓ 排除（已退出，且停掉了后台服务） |
| 手机开关状态 | ✗ **USB 调试显示已开，但设备始终只有 MTP + 大容量存储两个接口，没有第三个 ADB 接口** |

主机侧能查的全部排除之后，剩下的只能是手机固件层没把 ADB 挂进 USB 配置。
这条路走不通，就只剩「让手机自己把树送出来」。

第二个选择是现成的无障碍自动化 App（AutoX.js / AutoJs6 / MacroDroid）。**主动放弃**，原因是
Auto.js 一系全部沿袭原作者的「MPL-2.0 + 非商业使用」条款，而本产品是商用交付物，
挂一个非商用运行时做采集端风险不值得冒。自建则零许可证问题。

---

## 设计取向：手机端故意做「笨」

这个 App **不解析消息、不判新老、不去重、不管时间窗**。它只负责一件事：

> 钉钉界面一变，就把当前这一屏的节点树原样送出去。

所有业务判断都留在电脑侧的 Python 里。这样换来三件事：

1. **改规则不用重装手机** —— 解析规则、锚点逻辑、时间窗调整全在电脑上改，手机上不用动；
2. **不转发历史的机制只有一份** —— 与现役 OCR 路径共用同一套下游（去重、路由、格式化、
   投递、回读校验、告警、统计），不会两条路各写一份然后慢慢跑偏；
3. **手机端的出错面只剩两件事** —— 「读不到」和「网络不通」，排障路径短。

代价：手机需要一直亮屏停在群聊页（跟 OCR 路径的要求完全一样）。

---

## 编译

### 准备编译环境（只做一次）

刻意**不用 Gradle**：Gradle 要联网拉 AGP 和一堆依赖，而本机网络分区很怪
（`dl.google.com` 通、`github.com` 网页域名不通）。手工走 `aapt2 + javac + d8 +
zipalign + apksigner` 这条链，装好之后**完全离线可编译**，改一行 Java 重打一个包只要几秒。

需要两样东西（默认装在 `D:/android-build`，可用环境变量 `DDWX_ANDROID_HOME` 改）：

```
D:/android-build/
├── jdk-17.0.20.1+1/                 Microsoft OpenJDK 17
└── sdk/
    ├── build-tools/33.0.2/          aapt2 · d8 · zipalign · apksigner
    └── platforms/android-33/        android.jar
```

来源（下载速度可以，几十秒）：

```bash
# JDK
curl -L -o jdk17.zip "https://aka.ms/download-jdk/microsoft-jdk-17-windows-x64.zip"
# Android 命令行工具
curl -L -o cmdline-tools.zip \
  "https://dl.google.com/android/repository/commandlinetools-win-11076708_latest.zip"

# 然后用 sdkmanager 装平台与构建工具
sdkmanager --sdk_root=<home>/sdk "platforms;android-33" "build-tools;33.0.2"
```

### 打包

```bash
cd D:/wxauto-main/ddwx-bridge
python android/build.py            # 产物在 android/dist/ddwx-collector-0.1.0.apk
python android/build.py --clean    # 先清干净
```

产物约 **28 KB**。`android/debug.keystore` 是自签名证书，**必须留在仓库里**：
换一份证书签出来的包，Android 会以「签名不一致」拒绝覆盖安装。

### 两个踩过的坑（改 build 脚本时别踩回去）

- **`aapt2 link` 的 `-R` 不是「资源」的意思**，它是**覆盖层（overlay）**语义。用它传编译好的
  资源会报 `resource string/xxx does not override an existing resource`——报错信息完全指不到
  真正的原因。**要把 res.zip 当位置参数传**。
- **`build-tools/33.0.2/d8.bat` 在 JDK 17 上跑不起来**：它的第 109 行用了 `-Djava.ext.dirs`，
  这个选项在 JDK 9 就被删了，直接 `Could not create the Java Virtual Machine`。绕开办法是
  自己调主类：`java -cp <build-tools>/lib/d8.jar com.android.tools.r8.D8 ...`
  （`apksigner.bat` 用的是 `-jar`，没这个问题。）

另外 `resources.arsc` 必须保持**不压缩**（`aapt2 link -0 arsc`）：Android 11 起
（targetSdk 30+）系统会直接拒绝安装 arsc 被 deflate 的包，而 `zipalign` 只能对齐、不能解压。

---

## 安装到手机

1. 用 USB **数据线**连电脑（华为电脑管家会抢占，先在管家界面断开手机），
   手机下拉通知栏把 USB 连接方式选成「**传输文件**」。
2. 电脑上打开资源管理器 → 手机 → 内部存储，把
   `android/dist/ddwx-collector-0.1.0.apk` 拖进任意文件夹（例如 `Download`）。
   —— 这条路只需要 MTP，**不需要 USB 调试**。
3. 手机上用「文件管理」找到这个 APK 点开安装。首次会提示「来自外部来源」，
   按提示允许即可（具体入口各 ROM 不同，通常是「设置 → 安全 → 安装未知应用」）。
4. 打开「**喵喵采集端**」，点「打开系统无障碍设置」，
   在列表里找到「**喵喵采集端·读钉钉**」并打开。

> 装完可以在应用信息页看权限：只有「网络」和「读取手机存储（Android 9 及以下）」，
> 没有相机、通讯录、位置。它只读无障碍节点树，**不点击、不输入、不改动钉钉里的任何内容**。

---

## 配置

在 App 首页填三项，与电脑上 `config.yaml` 的 `dingtalk` 段一致：

| App 里的项 | 对应 config.yaml | 说明 |
|---|---|---|
| 电脑 IP | —— | 电脑的**局域网**地址（`ipconfig` 查，主程序启动日志里也会打印） |
| 端口 | `gateway_port` | 默认 8765 |
| 令牌 token | `gateway_token` | 必须一字不差；这个值同时用于手机端鉴权 |

填完点「**保存设置**」。然后：

1. 点「**测试与电脑的连通性**」——应当看到「✔ 通了」。
   如果报「连不上」，先看 Windows 防火墙有没有放行 8765 端口，
   再确认手机连的 WiFi 与电脑在同一网段。
2. 打开钉钉，进入目标群聊页，**保持亮屏**，再切回 App 看状态区：
   - 「读到文本 N 条」—— N 应当明显大于 0
   - 「已上报」—— 说明电脑收到了

### 建议同做的两件事

- 系统设置 → 电池 → 把本应用设为「**不允许后台省电**」（各 ROM 名称不同，
  华为是「应用启动管理 → 手动管理 → 允许后台活动」）。
- 钉钉的群聊页不要退出，也不要锁屏。锁屏后无障碍树会变成锁屏界面，读到的东西毫无意义。

---

## 电脑侧

### 联调阶段：不用改 `mode`

只要想确认「手机报了没有、报上来长什么样」，用这个**只收不转**的工具就够 ——
它只起接收网关这一层，**不开微信、不连数据库、不改 config.yaml**，也不管你
当前是 `ocr` 还是 `android` 模式：

```bash
python tools/android_capture.py          # 起网关，等手机报第一帧 → 存下来 + 出结构判定
python tools/android_capture.py --now    # 已经收到过帧了？直接看最新那一帧
python tools/android_capture.py --keep   # 收到后继续盯着，观察上报是否持续
python tools/android_capture.py --bind 127.0.0.1   # 只在本机自测，不触发防火墙提示
```

它会占住 `gateway_port`（默认 8765），所以**别跟主程序同时跑** ——
两边抢同一个端口，后起的那个会直接报「端口起不来」。

收到帧之后它会连出两段报告，**两段都要看**：

| 段落 | 回答什么问题 |
|---|---|
| 结构判定 | 这棵树长什么样（有没有「一个子节点 = 一条消息」的容器） |
| **解析预览** | 用**正式解析器**跑这一帧，会切成哪几条消息（发送者/正文/类型） |

解析预览才是决定能不能开后台解析的依据。只有它满足下面三条，才去开：

1. 条数与屏幕上肉眼看到的一致；
2. 发送者与正文分开了（发送者不是空的、正文里没有混进昵称）；
3. 消息列表里没有出现 `09:12` / `昨天` 这类时间条。

任何一条不满足，**先别开**，把这一帧 XML（`data/screen-*.xml`）发出来调判据 ——
解析器对结构的假设在 `bridge/sources/android_parse.py` 顶部有说明。

### 开启后台解析（真正开始转发）

确认解析预览没问题后：

```yaml
dingtalk:
  mode: "android"
  gateway_token: "…"
  android_parse: true      # ← 只有这一行会让 /screen 产生转发
```

开关的语义要记清：

- `false`（默认）= 网关只收，一条都不转发；
- `true` = 每收一帧就解析一次。**第一帧只建基线**（一条都不发），
  之后只转发「上一帧最后一条消息之后」的部分；
- 往上翻历史、换群、一屏全换 → 锚点丢失，这一帧**不转发**并把基线重建到当前画面
  （宁可漏几条用 `tools/forward_recent.py` 补发，也不冒险把历史灌进群）。

### 正式上线

1. `config.yaml` 里 `dingtalk.mode` 改成 `"android"`，
   `gateway_token` 填一个随机串（**不能留空**，网关监听局域网，没 token 等于谁都能往
   你的微信群发消息）：

   ```bash
   python -c "import secrets;print(secrets.token_urlsafe(24))"
   ```

2. 解析确认无误后再加 `android_parse: true`（见上一节）。

3. 启动主程序。日志里会打印手机端该填的地址；启动横幅会明确写出解析开没开。

4. 验证手机上报是否到了（这次是问**已在运行的主程序**）：

   ```bash
   python tools/android_screen.py                 # 只看状态，不下载正文
   python tools/android_screen.py --watch 60      # 盯 60 秒，看上报是否持续
   python tools/android_screen.py --save --analyze  # 存一帧并出结构判定
   ```

> `android_capture.py` 与 `android_screen.py` 的分工：前者**自己起网关**（联调用，
> 不需要主程序），后者**连现成的网关**（线上用，主程序在跑时才通）。
> 两者都只是排障与取证入口，都不参与转发。

---

## 当前进度

| 环节 | 状态 |
|---|---|
| 闸门：钉钉安卓的无障碍树里有消息文本 | ✅ 手机「屏幕朗读」实测读出完整正文 |
| 手机端采集 App | ✅ 本篇，已编译出可直接安装的 APK |
| 电脑侧 `/screen` 接收端点 | ✅ `bridge/sources/android_gateway.py` |
| 排障工具 | ✅ `tools/android_screen.py`（连现成网关）、`tools/android_capture.py`（自起网关，联调用） |
| 电脑侧解析器（XML → 消息） | ✅ `bridge/sources/android_parse.py`（解析 + 基线/锚点差分） |
| **消息边界确认（真机）** | ✅ 2026-10-17 真机多帧：容器 `ListView`、组内多条消息、昵称与正文分开、时间条过滤、半截行丢弃 |
| 页面类型门卫 | ✅ 2026-10-17 `detect_page`：@面板/群信息这一屏同样是「带一堆文本的滚动列表」，不判就会把人名当群消息发出去（真机实测踩到） |
| 后台解析开关 | ✅ `dingtalk.android_parse`（本机已置 `true`）；样板文件仍默认 `false` |
| 设置中心 / 主界面的选择器 | ✅ 主界面左栏「数据来源」下拉 + 设置中心「采集与投递」卡片（含网关三项） |
| 保活与异常恢复 | ⏳ 待定（手机端被系统杀掉后不会自启） |

### 真机实测出来的四个坑（解析器已按它们改）

一帧真实画面（Mate Xs 2 / 钉钉 7.x，群聊页贴底）暴露的假设错误，全部有回归锁
（`tests/test_android_parse.py::TestRealDeviceLayout`）：

1. **列表的一个子节点不是一条消息**。`ListView#list_view` 的子节点是
   「时间分隔条（`layout_chatting_split`，`今天 08:13`）+ 若干条 `RelativeLayout`」
   的一组。按「一个子节点一条」切，整屏会被拼成一条、时间条混进正文。
2. **底部功能条也是 RecyclerView**（`#native_menu_bar`，员工档案/任务进度/使用帮助/
   Group Check in 四个带文本子节点）。只按「能切出几条」挑容器，它会赢过只有两组
   消息的列表 —— 所以先按面积筛掉窄条，再比条数。
3. **深层子节点的 `bounds` 会失真**：行级 bounds 准确，但行内会出现
   `[212,265][964,-929]` 这种「top 恒等于列表顶部、bottom 为负」的乱码。
   切分只依赖**结构**（叶子/容器/面积），不依赖这些坐标。
4. **两个隐形噪音**：气泡里挂着一个 `android.view.View`、content-desc 为 `复制`
   （长按气泡的动作条，常驻在树里）；每条消息右侧还有一个**零宽**的
   `chatting_item_hidden_time`（`08:13`，给读屏用的）。两者都会被当正文发出去。

> **注意**：`android_parse` 默认是 `false`，此时 `mode: android` 下网关**只负责收**。
> 手机报的是画面，不开解析就不会有任何转发 —— 这是刻意的安全默认值。
> 别把「网关在收」当成「业务在转」。
>
> 另外手机端**整树 md5 不变就不上报**：网关若是后起的，要等画面变一下
> （来新消息、轻轻滑一下）才会收到第一帧。
