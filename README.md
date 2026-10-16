# ddwx-bridge｜钉钉 → 微信 消息转发桥

把钉钉里**指定公司/指定群**的消息，按规则自动转发到**指定的微信群**。

---

## ⚠ 当前结论（2026-10-16 实测）

针对「把钉钉群【数字质控】浙江杭州萧山区大江东公司的**所有消息**转发到微信群
申通五星业务员培训群」这一目标：

| 侧 | 状态 | 说明 |
|---|---|---|
| **微信投递** | ✅ **已真机验证通过** | `wechat4` 驱动，已实际发消息到「文件传输助手」并回读校验一致 |
| **钉钉取数（OCR）** | ✅ **当前方案** | 截屏 + OCR，能拿到群里全部消息；已在本机钉钉实测解析 |
| **钉钉取数（机器人）** | ❌ 不满足需求 | 只能收到 **@机器人** 的消息，收不到群内其他人之间的对话（平台限制） |
| **钉钉取数（控件树）** | ❌ 不可用 | 实测 `im_chat::ChatBubbleWidget` 子节点为空，证据见 `tools/dingtalk_tree.txt` |
| **会话存档 / `Chat.Message.Read`** | 备选正规路径 | 需企业管理员开通；`Chat.Message.Read` 官方标注**灰度中** |

### 钉钉侧三条路，逐一实测/核实

| 路径 | 结论 | 依据 |
|---|---|---|
| **群机器人（Stream）** | ❌ 只能收到 **@机器人** 的消息 | 官方开发者百科明确：「在群聊中，只有 AT 机器人的消息可以被机器人接收到」 |
| **UI 自动化读控件树** | ❌ 消息区无控件树 | 实测：`im_chat::ChatBubbleWidget` 子节点为空，证据见 `tools/dingtalk_tree.txt` |
| **截屏 OCR** | ⚠ 可做但有上限 | 唯一无需审批的兜底，需窗口常驻前台，有识别误差 |
| **会话存档 / `Chat.Message.Read`** | ✅ 官方正规路径 | 需企业管理员开通，可能要钉钉专业版；`Chat.Message.Read` 官方标注**灰度中** |

**关键认知**：机器人收不到群内其他人之间的对话，这是钉钉为保护隐私刻意做的平台限制，
不是配置问题。要拿到「该群所有消息」，只能走企业级授权（会话存档）。

### 微信侧：wxauto 已不适用于微信 4.x

- wxauto 于 **2025-10-28 归档停止维护**（本仓库即为 3.9 归档版）
- 微信 4.0 起改用 Qt Quick/QML 渲染，窗口类名由 `WeChatMainWndForPC` → `mmui::MainWindow`
- 本项目新增 `bridge/targets/wechat4.py`，按 `mmui::*` 类名直接实现，**已在本机微信 4.1.15.13 验证控件树可读**

## 架构

```
钉钉侧(Source)          转发服务(Bridge)                 微信侧(Target)
─────────────          ──────────────────             ────────────────────
Stream 长连接    →     去重落库 → 规则路由        →    wechat  驱动(微信3.9/wxauto)
或 UI 自动化            → 格式化 → 限流 → 投递          wechat4 驱动(微信4.x/mmui::)
（或截屏OCR兜底）                                         → 指定好友/群
```

两端都通过抽象接口解耦（`bridge/sources/base.py`、`bridge/targets/base.py`），
换采集方式或换投递目标都不影响中间逻辑。投递端由 `wechat.driver` 选择，
`auto` 会自动探测本机微信代际。

## 桌面版（标准 Windows 软件）✅ 推荐

不用记任何命令，装好快捷方式后双击就能用：

| 文件 | 作用 |
|---|---|
| **桌面 / 开始菜单「钉钉微信转发桥」** | 双击打开主界面（无黑框） |
| `启动消息转发桥.bat` | 同上（项目目录里的启动器） |
| `安装桌面快捷方式.bat` | 重新生成桌面/开始菜单快捷方式 |

```bash
# 命令行方式
pythonw app\gui.py              # 打开主界面
python app\setup.py             # 安装快捷方式（含图标）
python app\setup.py --remove    # 卸载快捷方式与开机自启
python app\setup.py --quiet     # 静默安装（部署脚本用）
```

### 主界面能做什么

- **状态灯**：采集端 / 投递端 / 运行状态一目了然（启动时自动自检）
- **统计**：本次运行（识别/投递/失败/跳过跨天/去重）+ 累计入库与投递
- **运行日志**：按级别着色，自动滚动，右键可复制/清空
- **工具栏**：`▶ 开始运行` `■ 停止` `自检` `钉钉预览` `转发一条…` `设 置` `消息记录` `导出 CSV` `数据目录`
- **菜单栏**：运行 / 设置 / 工具 / 帮助（`F5` 开始、`F6` 停止、`Ctrl+,` 打开设置）
- **试运行提示条**：配置为 dry-run 时顶部会有黄色提醒，防止误发

### 设置中心（点工具栏「设 置」或菜单「设置」）

| 页签 | 能设置什么 |
|---|---|
| **转发规则** | 哪个钉钉群 → 哪个（哪些）微信群；一条规则一行，可自由增删；钉钉群名填片段即可（子串匹配），微信群名必须与客户端完全一致，多个用 `;` 分隔 |
| **时间范围** | 不限时间 / 只转发当天消息（默认）/ 只在指定时段转发（`08:00-18:00`，支持跨零点 `22:00-06:00`） |
| **采集与投递** | 试运行开关、钉钉轮询间隔、微信发送间隔、锚点防护、标题护栏与阈值 |
| **消息格式** | 转发文案模板（`{company} {sender} {chat} {time} {content} {type}`），带实时预览 |
| **钉钉通知** | 自定义机器人 webhook、加签 / 关键词、告警场景与阈值、定时运行报告，可**当场发测试消息** |
| **其他** | 开机自动启动、解释器信息、重新检测环境、打开数据/日志目录 |

保存时自动把原配置备份到 `data/backup/`；改动**按行写入** `config.yaml`，
注释说明全部保留（`routes` 段由界面生成，段落头有说明）。

### 钉钉机器人通知（告警 + 运行报告）

**先说能力边界，免得误解**：自定义机器人的 webhook 是**单向**的 —— 只能往钉钉群里
发消息，**收不到**群里其他人发的消息。所以它**不参与**「钉钉 → 微信」的取数，
取数仍然是 OCR 截屏方案。它解决的是另一个问题：这套程序最怕**静默失效**
（钉钉窗口被切走、被最小化之后，转发停下来但界面毫无提示，没人盯着就发现不了）。

| 触发场景 | 说明 |
|---|---|
| 采集中断 | 找不到钉钉窗口 / 窗口被最小化 / 定位不到消息区 / 截图失败 |
| 转发已暂停 | 标题护栏拦下 —— 钉钉当前不是目标群 |
| 投递失败 | 微信连续失败达到阈值（默认 3 次）才提醒，偶发一次不打扰 |
| 采集停滞 | 连续 N 分钟（默认 120）没有新消息，只在 `08:00-21:00` 判定以免夜里误报 |
| 运行报告 | 每小时 / 每天定时把收到、发送、失败、累计入库发到群里 |
| 启停通知 | 开始、停止运行时各发一条 |

实现上刻意**零侵入采集代码**：`bridge/notify.py` 往 `bridge` 这个 logger 上挂一个
`AlertHandler`，把采集端本来就会打的 WARNING 日志映射成告警文案。好处是采集、
投递、路由任何一处将来新增的 ERROR 都自动覆盖，且通知模块出问题不会影响主流程。

同一类告警有**冷却时间**（默认 300 秒）—— 窗口被切走时每轮 OCR 都会报错，
不去重会瞬间把群刷爆。发送失败会释放冷却，下一轮还能再试。

```bash
python run.py --notify-test      # 发一条测试消息，验证 webhook / 关键词 / 加签
python run.py --notify-report    # 把累计统计发到钉钉群
```

配置在 `config.yaml` 的 `notify` 段（**必须写在 `routes:` 之前** —— 界面重建
`routes` 段时要求它是最后一个顶层段）。webhook 带 `access_token`，等同于凭证，
建议用环境变量下发：

```bash
set DINGTALK_ROBOT_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=xxx
set DINGTALK_ROBOT_SECRET=SECxxxxxxxx
```

机器人的安全设置三选一，三种都支持：**自定义关键词**（填 `notify.keyword`，
内容缺词时自动补一行）、**加签**（填 `notify.secret`）、**IP 白名单**（钉钉侧配置）。
发送失败的返回码会给出人话解释，例如 `errcode=310000` 就是安全设置不匹配。

### 实现要点（双解释器）

界面（`app/gui.py`）只需要标准库 + tkinter，秒开；采集/投递的重依赖
（rapidocr、onnxruntime、pillow…）都在工作进程 `app/bridge_worker.py` 里跑，
GUI 通过管道实时读它的日志（`@@LOG@@` JSON 行），停止通过**哨兵文件**
`data/run.stop` 握手（Windows 无法向无控制台子进程投递 Ctrl+Break）。
解释器自动探测逻辑见 `app/paths.py`，结果缓存在 `app/_env.json`。

## 目录结构

```
ddwx-bridge/
├── start.bat             Windows 一键启动菜单（纯 ASCII，中文菜单由 Python 输出）
├── bridge/
│   ├── config.py         配置加载（YAML → dataclass，敏感字段支持环境变量）
│   ├── models.py         Message 模型，uid 作为幂等去重键
│   ├── storage.py        SQLite 落库 / 去重 / 投递日志 / 导出
│   ├── router.py         规则路由（公司、会话、关键词、消息类型）
│   ├── formatter.py      模板渲染 + 批量摘要
│   ├── service.py        编排：收 → 去重 → 路由 → 格式化 → 限流 → 投递
│   ├── notify.py         钉钉机器人通知：故障告警 + 运行报告（单向，只发不收）
│   ├── sources/          采集端
│   │   ├── dingtalk_ocr.py      OCR 截屏（当前使用）
│   │   ├── dingtalk_stream.py   Stream 长连接（需凭证）
│   │   ├── dingtalk_ui.py       钉钉客户端 UI（实测不可用，保留作参考）
│   │   └── factory.py           按配置装配采集端
│   └── targets/          投递端
│       ├── wechat.py           微信 3.9.x（wxauto 原版逻辑）
│       ├── wechat4.py          微信 4.x（mmui:: 控件，自研）
│       └── __init__.py         driver 探测与工厂
├── tools/
│   ├── forward_one.py           端到端转发一条（默认只预览）
│   ├── test_dingtalk_ocr.py     钉钉 OCR 识别效果预览（只读）
│   ├── test_scroll_guard.py     实测滚动不会导致历史消息被误转发
│   ├── test_wechat4.py          微信投递端验证（默认只读）
│   ├── probe_dingtalk_ui.py     钉钉控件树探针（判断 UI 路线是否可行）
│   ├── probe_dingtalk_capture.py  截图方式探针（PrintWindow / 位图 / 屏幕）
│   ├── probe_wechat_ui.py       微信 4.x 控件树探针
│   ├── dump_dingtalk_tree.py    导出钉钉控件树
│   ├── dingtalk_tree.txt        实测证据：钉钉消息区为空
│   └── wechat_tree.txt          实测证据：微信 4.x 控件树
├── tests/                148 项离线单元测试（核心链路 / OCR 解析 / 锚点 / 跨天 /
│                         一对多 / 解释器探测 / 钉钉通知）
├── run.py                入口
├── probe_dingtalk.py     钉钉侧能力探针
├── config.example.yaml   配置样板
└── requirements.txt
```

## 验证微信投递端（默认只读，零副作用）

```bash
python tools/test_wechat4.py                                  # 探活 + 列会话
python tools/test_wechat4.py --locate "申通五星业务员培训群"      # 只定位、不点击
python tools/test_wechat4.py --open "申通五星业务员培训群"        # 切换会话并回读（不发消息）
python tools/test_wechat4.py --send "文件传输助手" "测试" --yes   # 真发，必须显式 --yes
```

`--locate` 会打印目标单元格矩形、表格可视区与**计划点击点**，
并校验点击点确实落在表格内。会话列表是虚拟滚动的，滚出视口的项
`BoundingRectangle` 会跑到表格外，若不校验就会点到别的位置 —— 这类点击
在 `wechat4.py` 里已被强制拦截。

## 端到端转发一条（推荐的上线前验证）

`tools/forward_one.py` 把「钉钉抓取 → 渲染 → 微信投递」串起来跑一次，
用于正式运行前确认整条链路真的通。**默认只预览，不发送。**

```bash
python tools/forward_one.py                        # 预览最新一条（只读）
python tools/forward_one.py --index 2              # 预览从最新往上数第 2 条
python tools/forward_one.py --yes                  # 真实转发最新一条
python tools/forward_one.py --to "某某群" --yes      # 换目标会话
python tools/forward_one.py --plain --yes          # 去掉【链路测试】提示行
python tools/forward_one.py --text "任意文案" --yes   # 跳过钉钉，单测微信投递链路
```

预览会打印群标题、标题护栏结果、可见消息清单，以及**将要发出去的完整文案**；
确认无误后再加 `--yes`。该工具不写数据库，因此不会影响正式运行的幂等去重记录。

**实测结论（2026-10-16）**：真实转发成功，微信侧回读校验一致 ——
发送内容 `【链路测试】… 【数字质控·大江东】晟约跟单专员 机器人 12:54 / @所有人【客户声音履约风险提醒】…`
已出现在「申通五星业务员培训群」的聊天记录中。

## 快速开始

**方式一：一键启动（推荐）**

双击 `start.bat`，在菜单里选择：

```
[1] 模拟测试      样例消息跑通全链路（不需要微信/钉钉）
[2] 交互测试      手动输入消息，实时看过滤与转发效果
[3] 钉钉OCR预览   截一帧看钉钉群消息识别效果（只读）
[4] 转发一条      抓一条钉钉消息，预览后确认发送到微信群
[5] 启动自检      检查配置、微信连接与钉钉窗口
[6] 正式运行      采集钉钉消息并投递微信（Ctrl+C 停止）
[7] 能力探针      检测钉钉凭证、SDK 与本机客户端
[8] 查看统计      累计入库与投递情况
[9] 导出消息      把已入库消息导出为 CSV
[0] 退出
```

> `start.bat` 本身**只含 ASCII 字符**，中文菜单由 Python 打印。
> 这是刻意的：cmd.exe 用系统 OEM 代码页（简体中文为 GBK/936）读取 .bat，
> 如果文件里有 UTF-8 中文字节，语法会被劈断，报出
> `'[1]' 不是内部或外部命令` 这类错误。以后改这个脚本请保持纯 ASCII。

`[1] 模拟测试` 是**完全无副作用**的：它强制 dry-run（即使你把 `dry_run` 改成了 false
也不会真的发消息），并使用一次性临时数据库，因此可以反复运行、结果稳定，也不会污染真实数据。

**方式二：命令行**

```bash
pip install -r requirements.txt          # 1. 安装依赖
cp config.example.yaml config.yaml       # 2. 生成配置并编辑
python run.py --simulate                 # 3. 跑通链路（无需微信）
python run.py --interactive              # 4. 手动喂消息看效果
python run.py --check                    # 5. 自检全部就绪后
python run.py --live                     # 6. 正式运行
```

### 常用命令

| 命令 | 作用 |
|---|---|
| `python run.py --simulate` | 用 4 条样例消息跑通全链路（含过滤、去重场景） |
| `python run.py --interactive` | 交互测试：手动输入消息，实时观察过滤/格式化/投递 |
| `python run.py --check` | 启动自检：采集端凭证、转发目标、微信连接 |
| `python run.py --live` | 正式运行：采集真实消息并投递 |
| `python run.py --stats` | 查看累计统计 |
| `python tools/forward_one.py --yes` | 端到端转发一条（上线前验证，不加 `--yes` 只预览） |
| `python tools/test_dingtalk_ocr.py` | 只读预览：钉钉当前帧会转发哪些内容 |
| `python tools/test_scroll_guard.py` | 实测滚动聊天区不会导致历史消息被误转发 |
| `python run.py --export out.csv` | 导出消息为 CSV（UTF-8-BOM，Excel 可直接打开） |
| `python run.py --notify-test` | 发一条钉钉测试消息，验证 webhook / 关键词 / 加签 |
| `python run.py --notify-report` | 把累计统计发到钉钉群 |
| `python probe_dingtalk.py` | 探测钉钉侧该走哪条路线 |
| `python tools/dump_dingtalk_tree.py` | 导出钉钉控件树（UI 模式调试用） |
| `python -m unittest discover -s tests -v` | 运行单元测试 |

> 交互测试的输入格式：`发送者|消息内容`，不写发送者则记为「测试用户」；输入 `:q` 退出。
> 测试数据会写入 `data/bridge.db`，正式上线前可直接删除该文件（会自动重建）。

## 配置说明

### 路由规则

`routes` 从上到下依次匹配，**命中多条时目标合并去重**，模板取第一条命中的：

```yaml
routes:
  - name: "示例公司全部消息"
    companies: ["示例科技有限公司"]   # 空列表或 ["*"] 表示不限
    chat_names: []                    # 限定来源会话
    keywords: []                      # 内容关键词，命中任一即可
    msg_types: ["text"]               # text / image / file / voice / link
    targets: ["测试群"]               # 微信中的群名或好友备注，需完整匹配
    template: "【{company}】{sender} {time}\n{content}"
```

模板占位符：`{company}` `{sender}` `{chat}` `{time}` `{content}` `{type}`

### 安全相关

- **`wechat.dry_run: true`** —— 只打印不真发。首次联调务必保持开启。
- **`wechat.interval`** —— 相邻两条消息的最小发送间隔（秒），默认 3.0。调小会显著增加微信风控风险。
- **凭证不要写进配置文件**，用环境变量：
  ```bash
  export DINGTALK_CLIENT_ID=your_client_id
  export DINGTALK_CLIENT_SECRET=your_client_secret
  ```

## 钉钉侧采集模式

### 模式 A：`ocr`（当前使用）—— 截屏 + OCR

原理：UIAutomation 定位钉钉消息滚动区（`im_chat::ChatBubbleWidget`，正好是纯消息区），
用 `PrintWindow(PW_RENDERFULLCONTENT)` 截屏——**不抢焦点，窗口被遮挡也能截**——
再送 RapidOCR（PP-OCR 离线模型）识别，按像素颜色把文本块分成正文/发送者/时间戳，
按行距聚成消息，帧间多重集合差分发现新消息。

**安全护栏**：每帧先 OCR 窗口标题条，与 `chat_names` 模糊匹配（阈值 `title_ratio`），
不匹配就整帧丢弃——钉钉里开着别的会话时绝不会转发错内容。

**运行前提**：
1. 钉钉客户端打开着目标群（本例：`【数字质控】浙江杭州萧山区大江东公司`）
2. 钉钉窗口**不能最小化**（被遮挡可以，最小化截不到）
3. 本机已装 `rapidocr-onnxruntime`（见 requirements.txt）

**先预览再上线**（强烈建议）：

```bash
python tools/test_dingtalk_ocr.py            # 截一帧，看「将转发的消息」长什么样
python tools/test_dingtalk_ocr.py --raw      # 带颜色分类细节
python tools/test_dingtalk_ocr.py --watch 60 # 连续观察 60 秒（判定逻辑与正式运行一致）
python tools/test_scroll_guard.py            # 实测「滚动不会让历史消息被误发」
```

### 时间维度的三道防线（`time_filter` + `time_window` + `anchor_guard`）

采集端只在**程序启动之后**新到的消息上做转发（首帧建基线，不倒灌历史）。
但仅靠「帧间差分」有一个真实风险：**聊天区被滚动时，历史消息会进入视口，
上一帧里没有它们，就会被误当成新消息发出去。** 这属于多转发，比漏发更麻烦。
因此加了两道防线，默认都开启：

**防线 A｜`anchor_guard: true` —— 锚点定位**

新消息只会出现在**锚点下方**。锚点的选取对 OCR 抖动很宽容：从上一帧最下面
往上找**第一条仍留在本帧里**的消息作为锚。滚动带进视口的历史消息必然落在锚点
之上，因此不会被选中。若上一帧没有任何一条消息还留在本帧中，说明视口发生了
大幅位移，该帧**整体不转发**——宁可漏一帧，也不误发历史。
（界面「设置 → 时间范围」里的三个选项就是这层的开关。）

**防线 B｜`time_filter` + `time_window` —— 时间维度过滤**

| 取值 | 行为 |
|---|---|
| `any` | 不做时间限制 |
| `today`（默认） | 丢弃钉钉标注为「昨天 / 星期X / 月日」的跨天消息 |
| `window` | 在 `today` 的基础上，再要求消息落在 `time_window`（`HH:MM-HH:MM`，支持跨零点） |

说明：钉钉对「最新几条」不显示时间戳，这类消息按**当前时刻**判定——
所以 `window` 的实际效果就是「只在上班时段转发」。

实测证据（`tools/test_scroll_guard.py`，在真实钉钉窗口上滚动 5 格）：

```
[防线 A 生效] 判定为「视口大幅位移」→ 本帧整体不转发
[对照] 若关闭 anchor_guard，会有 2 条被误当成新消息发出：
    ✗ @所有人今日出仓进度提醒 …
    ✗ @所有人 履约-客户声音早报 …
```

**防线 B｜`time_filter: today` —— 跨天时间戳过滤**

依据钉钉的时间戳语义：**同一天的消息只显示 `hh:mm`，跨天才显示
「昨天 hh:mm」「星期X hh:mm」「MM-DD」**。因此凡是被日期前缀覆盖的消息，
一律判定为往日内容并丢弃；判不出来的一律放行（交给防线 A 兜）。

```yaml
dingtalk:
  time_filter: "today"   # today = 丢弃跨天旧消息；any = 不做时间限制
  anchor_guard: true     # 锚点定位，防止滚动导致历史消息被误转发
```

**已知限制**：OCR 长数字可能有误差（已做数字内空格修复，但不保证 100%）；
两条轮询之间滚出屏幕的消息拿不到；图片/文件只能转发文字描述；
发送者名字在「同一人连续发言」时钉钉不显示，会识别为空；
连续多条**内容完全相同**的消息正常可识别，但若恰好与锚点内容一致且发生 OCR
抖动，理论上可能重发一次（服务层按 `uid` 去重可兜住）。

### 模式 B：`stream`（备选，需审批）

官方长连接事件订阅，**无需公网 IP、无需 HTTPS 证书**，本地跑即可。

钉钉开放平台配置步骤：

1. 进入「应用开发 → 企业内部应用」，创建应用
2. 「应用能力 → 机器人」添加机器人，**消息接收模式必须选 Stream 模式**
3. 「开发配置 → 权限管理」申请：
   - `im:message.p2p_msg:readonly`（读取用户发给机器人的单聊消息）
   - `im:message.group_at_msg:readonly`（接收群聊中 @机器人的消息）
4. 「开发配置 → 事件订阅」选择 **Stream 模式推送**，添加事件 `im.message.receive_v1`
5. 「版本管理与发布」创建版本并发布（企业账号需管理员审核，**权限变更后必须重新发布才生效**）
6. 把 `config.yaml` 的 `dingtalk.mode` 设为 `stream`，填入凭证

**能力边界（重要）**：机器人只能收到**发给它自己**的消息——单聊发给机器人，或群里 @机器人 的消息。
钉钉群聊不支持接收文件与语音，只接收 @机器人 的文本。标准版 OpenAPI 配额约 1 万次/月。

### 模式 C：`ui`（已证伪，仅存档）

操作钉钉 PC 客户端。无需审批，但**稳定性明显低于模式 A**：钉钉 PC 端为 CEF/Electron 架构，
聊天区的内部 Web 内容未必暴露给 UIAutomation。

使用前必须先确认控件结构可见：

```bash
# 1. 确保钉钉客户端已启动并登录
# 2. 导出控件树
python tools/dump_dingtalk_tree.py > dingtalk_tree.txt
# 3. 打开 dingtalk_tree.txt，检查聊天消息区是否有 [TextControl] 节点
```

- **有 `[TextControl]` 节点** → 可用，按实际结构微调 `bridge/sources/dingtalk_ui.py`
  中的 `CLIENT_CLASS_CANDIDATES` 与 `_read_visible()` 的配对逻辑
- **消息区整片为空** → 该路线不可用，请改走 Stream 模式

UI 模式必须在 `config.yaml` 的 `dingtalk.chat_names` 里指定要监听的会话名，
轮询间隔由 `dingtalk.ui_poll_interval` 控制（默认 5 秒）。

## 已知限制

- **读不到他人应用推给自己个人的工作通知**：钉钉开放 API 的消息通知接口全部是「发送」方向
  （发送工作通知、查发送进度、查发送结果、撤回），不存在接收端读取接口。
- **读不到你无权访问的会话**：不在群里、没有机器人，任何技术手段都取不到，这是权限问题。
- **微信侧无法主动加好友、不能大规模群发**：只能操作已存在的会话，高频发送会触发风控。
- **依赖客户端版本**：wxauto 为微信 3.9.X 归档版本，微信客户端升级后控件定位可能失效。
- **需保持前台窗口**：投递时微信窗口不能被完全遮挡或最小化，建议专机专用。

## 数据维护

消息与投递记录都在 `data/bridge.db`（SQLite），导出用 `run.py --export`。

清空数据（例如清掉测试期间产生的记录）时，**建议先停掉正在运行的进程**，
然后备份再清：

```bash
# 备份
mkdir -p data/backup
cp data/bridge.db "data/backup/bridge-$(date +%Y%m%d-%H%M%S).db"

# 清空（会同时清掉投递记录）
python -c "import sqlite3;c=sqlite3.connect('data/bridge.db');c.execute('DELETE FROM deliveries');c.execute('DELETE FROM messages');c.commit();c.execute('VACUUM');c.commit();c.close()"
```

注意：`run.py --simulate` 用的是临时库，**不会**写入 `data/bridge.db`，不需要为它清理。

## 合规提示

- 本项目仅用于企业内部信息同步，请勿用于骚扰、营销群发等场景。
- 微信侧基于 UIAutomation 模拟操作，存在账号被限制的风险，请自行评估。
- 钉钉侧请通过官方开放平台正规申请权限，不要逆向客户端数据。
