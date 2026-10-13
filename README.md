# 小糖人 QQ 桥 · qq-bridge 💬

**独立项目**（自己的 git 仓库）。让小糖人住进 QQ：**私聊**和**群聊**都能聊，
图 / 表情包 / 视频 / 语音都收得进也发得出，**聊过的还会同步进小窝**的群聊和对应私聊。

> 它自己不装模型、不存密钥、不落盘媒体。人格、记忆、模型、附件全在**大脑**（小糖人之家）那边 ——
> 这个项目只是把 QQ 和大脑接起来的那根线。

---

## 它到底做什么

```
QQ 网关（OneBot v11：SnowLuma / NapCat / Lagrange）
   │  ① 收到消息（白名单之外的**一律不理**）
   ▼
本项目（qq-bridge）
   ├─ ② 图片 / 表情包 / 视频 → 下载字节 → 交给大脑落盘（前端才显示得出来）
   │      视频还会请大脑抽 ≤4 帧 → 走视觉路，小糖人真的「看」得到
   ├─ ③ 语音 → 转文字（见下面「语音」一节的坦白）
   ├─ ④ 带上最近聊过的上下文 → 请大脑里这位小糖人说话（人格 / 记忆 / 密钥都在大脑）
   ▼
QQ  ◀── ⑤ 回话：文字（长文自动切条）/ 语音条 / 表情包 / 出件箱里的图片视频
小窝前端 ◀── ⑥ 同一轮对话各写一份：群聊 → 群聊会话，私聊 → 对应私聊
```

### 一个账号 = 一个小糖人

一个 QQ 号演一位小糖人。想让几个小糖人都在 QQ 上，就**把这个项目跑几份**，
每份一个 `config.json`、一个 QQ 号。群聊里被 @ 时各回各的，不会互相抢话。

---

## 和参考项目（Derpyu520/qq-bridge）的不同

参考项目值得抄的地方我抄了（OneBot v11、SILK 交给网关、发送白名单、语音额度）；
**这几处故意不抄**，因为和「大脑 / 外设」这个架构打架：

| 事情 | 参考项目 | 本项目 | 为什么 |
|---|---|---|---|
| 图片视频存哪 | 自己在桥里落盘 + 自己起静态服务 | 交给大脑的附件管道（`/api/qq-bridge/attachments`） | 前端聊天记录只认那一套（缩略图 / 画廊 / 删消息级联）。桥自己另存一份，删消息时文件就漏了 |
| 视频抽帧 | 自己在桥里 ffmpeg | 交给大脑的 `/api/media/video/analyze` | 大脑已经写好且带 15 秒 / 50MB / 8 帧的安全线，重复实现只会两处不一致 |
| 语音合成 | 自己接 TTS + SILK 编码 | 交给大脑的 `/api/voice/tts`（edge 晓晓 / piper 华燕 等） | 音色是用户在**大脑**里定的；SILK 仍是网关做（我们只给绝对路径） |
| 人设 | 桥里带一份角色卡 | 只传 `character_id` | 人设只有「角色介绍」一个来源，复制就会漂移 |
| 密钥 | 桥里配 | 一行都不存 | 统一走大脑的 `/api/nest/features/qq_bridge/api-config` |

**和 B站那个项目同一套规矩**：只调 `/api/nest/*`，不 import 本体任何代码。

---

## 怎么跑

### 0. 先配大脑

小窝前端 → 「🧠 小窝大脑 API」→ 填全局默认（或给 QQ 桥单配一份）。
QQ 桥在注册表里声明了 `needs_vision: true`，所以**记得也配视觉路**（看图要用）。

### 1. 装 QQ 网关

本项目**不内置网关**，也不碰 QQ 协议。任选一个 OneBot v11 实现：

- **SnowLuma**（参考项目用的那个）
- **NapCat**、**Lagrange** 等同类都行

网关里配「反向 WS 地址」指到本桥（默认）：

```
ws://127.0.0.1:3001/onebot/v11/ws
```

（少数网关只支持正向 WS：把 `config.json` 的 `onebot.mode` 改成 `client`，
`ws_url` 填网关的地址。）

### 2. 建配置

```bash
python -m qqbridge example          # 写出 config.example.json
cp config.example.json config.json  # 再改 config.json（它已被 .gitignore 挡住）
```

**至少要填两处，不然它谁都不理**：

```jsonc
{
  "allow": {
    "groups":  [123456789],   // 要接的群号（空 = 群里谁都不理）
    "private": [987654321]    // 要接的私聊 QQ 号（空 = 私聊谁都不理）
  },
  "persona": { "character_id": "dalaoda" },   // 这个号演谁（= 角色介绍里的 id）
  "owner":   { "qq": 987654321 }              // 管理员（不受白名单限制；留空 = 没有管理员）
}
```

### 3. 跑起来

**推荐在小窝前端点**：糖人小窝 → 💬 QQ 桥 → 「自检（不连 QQ）」先看一眼 →
「接上 QQ（常驻）」→ 日志就在旁边滚动（后端按 utf-8 读，中文不乱码）。

命令行也行（在**本目录下**）：

```bash
python -m qqbridge check      # 自检：大脑通不通、白名单空不空、模型配没配（不连 QQ）
python -m qqbridge whoami     # 这个号演谁 + 家里有哪些人
python -m qqbridge run        # 常驻
```

Windows 终端中文乱码就设 `PYTHONIOENCODING=utf-8`（在小窝前端点的话后端已经帮你设了）。

### 4. 不想装 QQ 也能先试

`tools/fake_onebot.py` 是个**假网关**：它连上桥、发一条消息、把桥的回复打出来。

```bash
python tools/fake_onebot.py                       # 私聊问它一句
python tools/fake_onebot.py --group 123456789     # 群里 @ 它
python tools/fake_onebot.py --image 图.png        # 带张图（走视觉路）
python tools/fake_onebot.py --listen              # 常驻：终端里打字当说话
```

---

## 语音这回事（要说清楚）

拆成两个方向，因为**能力不对等**：

**出（小糖人 → 你）**：✅ 完整可用。走大脑的 TTS，`voice.auto` 三档：

| 值 | 行为 |
|---|---|
| `off` | 永远打字 |
| `mirror`（**默认**） | 你先发语音，我也回语音（不主动开口吓人） |
| `always` | 每次都回语音（超过 `max_chars` 自动改打字） |

`max_per_minute` 给语音单独限流，免得连着刷一堆语音条。

**入（你 → 小糖人）**：⚠ **本地没有语音识别**。本体的语音模块只有 TTS（说），
没有 ASR（听）—— B站那个项目不涉及，DST 那边也一样。

参考项目是**靠 QQ 自己的语音转文字**（`fetch_ptt_text`），自己发的语音还常常拿不到
（retcode=100）。所以这里的路径是：

1. 网关要是顺手给了转写（少数会带 `text` 字段）→ 直接用；
2. 否则看 `config.json` 的 `asr.engine`：
   - `none`（**默认**）→ 按 `[语音]` 记下，**不硬猜**；
   - `openai` → 走 OpenAI 兼容的 `/audio/transcriptions`（whisper 这类）。
     `base_url` / `api_key` 留空就用大脑给本功能配的那份中转（vsllm 这类通常也带 whisper）。

想让它真的「听懂」，就把 `asr.engine` 改成 `openai` 并填好识别模型。
**用哪个引擎等你拍板**（本地 whisper 要拉模型文件；走中转按量付费）。见 `QUESTIONS.md`。

---

## 图片 / 视频 / 表情包

**收进来**（对方发的）：

- 图片、表情包 → `/api/qq-bridge/attachments` → 附件管道（**按内容签名去重**，群里反复发同一个表情只存一份）
- 视频 → 存原文件 + 请大脑抽 ≤4 帧（默认）走视觉路 → 小糖人真的看得到画面
- 抽帧或转码失败是**正常路径**（QQ 里转发的视频经常超 15 秒 / 50MB）：
  退化成「[视频]」占位，不报错、不影响回话

**发出去**（小糖人发的）：

- **表情包**：`data/stickers/` 就是它的表情库。默认 `sticker.reply_chance = 0`
  （**不主动发表情包**，免得像刷屏机器人）；想让它偶尔来一张，调成 `0.1` 之类。
  `sticker.auto_collect` 开着的话，它会把群里收到的表情**学下来**存进这个目录。
  想直接给它一批，把图片丢进 `data/stickers/` 就行。
- **图片 / 视频 / 任何文件**：`data/outbox/` 是**出件箱** —— 往里面丢文件，
  下一轮回话时带出去，发完移到 `data/outbox/sent/`，不会重复发。
  （这就是「小糖人也能发图片视频」的口子；以后别的功能想让它在 QQ 里发东西，也走这里。）

---

## 同步进小窝（这两件事是分开的）

| | 说明 |
|---|---|
| **同步** | 白名单里的会话**一律**写进前端聊天记录：群聊 → `group`，私聊 → `single-<这个号演的人>`。哪怕这条没 @ 我、哪怕这个会话设了静音 —— **同步是记录，不是打扰**。 |
| **开口** | 群里默认**要 @ 才回**（`allow.require_mention_in_group`）；私聊直接回；`modes.dm/group = "silent"` 时只同步不开口。 |

发出去的话也同步一份（`sync.to_frontend`），所以小窝里看到的是完整一来一回。

**记忆**：QQ 里聊的**默认汇入普通记忆**（`memory.isolate = false`）——
大老大的批注是「QQ 里聊的应该能同步进群聊 / 私聊记忆」，日常闲聊里也记得。
攒够 `memory.window` 条（默认 24）提一次给大脑。要隔离就把 `isolate` 改 true。

---

## 配置一览（`config.json`）

| 段 | 关键项 | 默认 | 说明 |
|---|---|---|---|
| `onebot` | `mode` | `server` | `server` = 反向 WS（推荐）；`client` = 正向 |
| | `listen_port` / `listen_path` | `3001` / `/onebot/v11/ws` | 反向 WS 监听 |
| | `access_token` | 空 | 网关配了就得填一样的 |
| | `self_id` | 空 | 留空就连上后问网关要 |
| `allow` | `groups` / `private` | **空** | **空 = 谁都不理**（防「刚装好就被拉进陌生群」） |
| | `require_mention_in_group` | `true` | 群里要 @ 才回 |
| `persona` | `character_id` | 空 | 这个号演谁；留空 = 名册第一个（会提醒一次） |
| `owner` | `qq` | 空 | 管理员；留空 = 管理命令全禁用 |
| | `whitelist_bypass` | `true` | 管理员私聊不受白名单限制 |
| `sync` | `to_frontend` | `true` | 同步进前端聊天记录 |
| | `persona_id` | 空 | 用户身份隔离（§22）；留空 = 大老大 |
| `reply` | `chunk_chars` | `400` | 长回复切条长度 |
| | `mention_sender_in_group` | `true` | 群里回话时 @ 一下说话的人 |
| `media` | `video_frames` | `4` | QQ 来的视频抽几帧送视觉（≤8） |
| `voice` | `enabled` / `auto` | `true` / `mirror` | 能不能发语音 / 什么时候发 |
| | `engine` / `voice` | `edge` / 晓晓 | 音色由大脑的语音设置决定 |
| `asr` | `engine` | **`none`** | 语音识别引擎；`none` = 只当「[语音]」 |
| `sticker` | `auto_collect` / `reply_chance` | `true` / `0` | 学不学表情 / 主动发的概率 |
| `memory` | `isolate` / `window` | `false` / `24` | 记忆隔离 / 攒多少条提一次 |
| `modes` | `dm` / `group` | `chat` | `silent` = 只同步不开口 |

改完**不用重启**：桥在每条消息前重新读一次配置（文件很小，不当那套热重载的奴隶）。

---

## 安全边界（有意为之，别改松）

- **白名单为空 = 谁都不理**。不做「默认全接」。
- 群里默认要 @。不 @ 只同步，不插嘴。
- **只允许本机回环 HTTP 连大脑**（大脑没有鉴权，绝不能指向公网）。
- 密钥只在内存里过一遍，不落盘、不打日志。
- 下载的 URL 只认 `http` / `https` / `file`，且有体积上限。
- 临时文件（下载的图、合成的语音）用完就删；语音临时文件一天后清。
- **不给 QQ 面向的会话任何本地工具**（参考项目也是这条规矩）：QQ 里的话进不了文件系统。

---

## 测试

```bash
python -m unittest discover -s tests -v
```

23 个纯离线用例（不连真 QQ、不连大脑），重点压三件最容易出事又最难肉眼发现的事：
**白名单**、**同步 vs 开口**、**重放去重**。

---

## 目录

```
qqbridge/
├── __main__.py   命令行入口（run / check / whoami / example）
├── config.py     配置：默认值 + 深合并 + 状态文件路径
├── onebot.py     OneBot v11：事件解析、动作调用、正/反向 WS、重连
├── sessions.py   会话路由：白名单、同步 vs 开口、重放去重、记忆轮次
├── media.py      媒体：下载 / 落盘 / 抽帧 / 语音进出
├── brain.py      大脑客户端（标准库 only）
└── bridge.py     主循环：一条消息进来 → 回话 → 两边留痕
tools/fake_onebot.py   假网关（不装 QQ 也能试）
tests/test_bridge.py   离线测试
```