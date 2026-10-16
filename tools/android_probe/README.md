# 安卓端无障碍探测（阶段 0）

这个目录不产出功能，只回答**一个问题**：

> 钉钉安卓客户端到底肯不肯把群消息的文本暴露给无障碍服务？

**为什么必须先问这个**：PC 端已经确认读不到 —— 钉钉 PC 用自研 DUI 框架自绘，
消息区控件树是空的（证据在 `../../tools/dingtalk_tree.txt`，`ChatBubbleWidget`
下没有任何子节点）。安卓端是原生 Android 视图体系（Activity / RecyclerView /
TextView），**大概率**能读到。但「大概率」不能当依据：这个前提不成立，后面
的读取、去重、上报全都不用做。所以它是一道闸门，先花半天探明。

过了闸门的好处很大：文本是**精确原文**，不再有 OCR 错字，也不需要标题护栏、
锚点防护、贴底检测这一整套为「截图认字」打的补丁。

---

## 三条探测路线，从省事到正式

| 路线 | 需要装什么 | 耗时 | 说明 |
|---|---|---|---|
| **甲 · adb dump** | platform-tools（约 5 MB） | 5 分钟 | **先用这个**。不需要装任何 App、不需要编译 |
| 乙 · AutoX.js 脚本 | AutoX.js（手机上装） | 10 分钟 | 更接近真实无障碍服务的视角，能看事件流 |
| 丙 · 本目录的 Android 工程 | Android Studio（约 1 GB） | 半小时 | 最可靠，而且这就是阶段 1 的基座 |

### 路线甲：adb + uiautomator（推荐先跑）

```bash
# 手机开「开发者选项 → USB 调试」，USB 连电脑
adb devices                            # 确认设备已授权
# 手机上打开钉钉，进入目标群聊页，让消息气泡显示出来
adb shell uiautomator dump /sdcard/dd.xml
adb pull /sdcard/dd.xml .
python tools/android_probe/analyze_uiautomator.py dd.xml
```

分析器会直接给判定：读到了多少文本、有没有「一个子节点 = 一条消息」的容器。

### 路线乙：AutoX.js

把 `autojs_probe.js` 拷到手机上跑，结果写到 `/sdcard/ddprobe_dump.xml`。
拉回来之后**用同一个分析器**出判定：

```bash
adb pull /sdcard/ddprobe_dump.xml .
python tools/android_probe/analyze_uiautomator.py ddprobe_dump.xml
```

⚠ 该脚本**未经真机验证**（开发机上没有安卓设备），用的是 AutoX.js 最通用的一套
API。跑不通就按报错调方法名，思路不变。
⚠ AutoX.js 自己需要无障碍权限，拿到后能读你屏幕上的一切 —— 请在专用测试机上跑。

### 路线丙：本目录的 Android 工程

纯 Android 框架 API，**零第三方依赖**。两种打开方式：

**方式一（最省事，推荐）**：在 Android Studio 里 `New Project → Empty Views Activity`，
包名填 `com.hbdxm.ddprobe`，然后把本目录这 5 个文件的内容粘贴进去（覆盖）：

```
app/src/main/AndroidManifest.xml
app/src/main/java/com/hbdxm/ddprobe/MainActivity.java
app/src/main/java/com/hbdxm/ddprobe/ProbeService.java
app/src/main/res/layout/activity_main.xml
app/src/main/res/xml/accessibility_service_config.xml
app/src/main/res/values/strings.xml
```

**方式二**：直接 `Open` 本目录。本目录**不带 gradle wrapper**（wrapper 含二进制
jar，不适合入库），所以第一次打开时 Android Studio 会提示配置 Gradle，按提示
用本地 Gradle 或让它生成 wrapper 即可；AGP 版本也按 Studio 的提示升级。

装上之后：

1. 点「打开系统『无障碍』设置」，在列表里找到「钉钉消息探测」并开启
2. 回到钉钉，进入目标群聊页 —— 消息一有变化就会自动抓取
3. 拉回电脑：

```bash
adb pull /sdcard/Android/data/com.hbdxm.ddprobe/files/ddprobe/latest.xml .
python tools/android_probe/analyze_uiautomator.py latest.xml
```

同目录下的 `dump_<时间戳>.txt` 是给人读的版本（文本清单 + 可滚动容器 +
缩进节点树），`latest.txt` 是它的固定路径副本。

---

## 怎么算「通过」

分析器会给结论。判据只有两条：

1. **读得到正文** —— 带文本的节点里有长文本（比如那串「客户声音履约风险提醒…374票」）
2. **分得清边界** —— 存在一个容器，它的每个子节点大致对应一条消息

两条都满足就是**可行**，可以直接进阶段 1。

## 一个重要的判定口径

`uiautomator` 看到的是无障碍树的一个**较保守**视图：

- 它说「读得到」→ 用真正的 `AccessibilityService` **一定**能读到；
- 它说「读不到」→ **不代表**一定读不到。`AccessibilityService` 可以加
  `FLAG_INCLUDE_NOT_IMPORTANT_VIEWS`（消息气泡常被标成「对无障碍不重要」）
  和 `FLAG_RETRIEVE_INTERACTIVE_WINDOWS`，视野比 uiautomator 大。

所以它是**单向可信的筛子**：过了就放心往下做，没过才需要上路线乙/丙复核。
另外 **别用 `uiautomator dump --compressed`** —— 那个会把没有文本的节点整个删掉。

## 已知的坑

- **dump 时钉钉必须在具体群聊页**。停在会话列表页会得到一个「没有文本」的假阴性。
- **时间分隔条也是消息列表的子节点**。`09:12`、`昨天`、`星期一`、`10-16` 这类
  会和真消息平级。阶段 1 必须先把它们摘掉，否则会把 `09:12` 当成一条消息转发。
  分析器已经把每个子节点标成 `消息` / `时间分隔` / `非文本`。
- **图片/文件消息只有 `content-desc`**，形如 `[图片]`，没有正文。和 PC 端 OCR 打平
  （都拿不到文件本体）。
- 部分国产 ROM 会在锁屏后冻结后台，探测时请让屏幕保持亮着。
- 钉钉的 `resource-id` 是混淆过的字符串，**版本升级可能变**。阶段 1 不要把
  id 写死，要留一个「按 id 找不到就按结构找」的降级路径。

## 探测完之后

- **通过** → 进阶段 1：读取 + 指纹去重（指纹直接复用 `bridge/fingerprint.py`，
  与 OCR 路径同一份实现）；然后按本仓库 `bridge/sources/android_gateway.py`
  的协议 POST 给电脑。
- **不通过** → 到此为止。现有 OCR 方案继续用，不必再投入后面的工程量。
