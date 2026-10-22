"""配置：`config.json`（**不进 git**，模板是 `config.example.json`）。

设计原则和参考项目一样但更轻：
- **默认关**。没配 `allow.groups` / `allow.private` 就是「谁都不理」—— 宁可不动，不可乱动。
- **默认安静**。群里默认要 @ 才回；语音默认只在对方先发语音时回语音（避免开口吓人）。
- 改完不用重启：`bridge.py` 每轮前重新读一次（文件很小，省得搞热重载那套）。

**两处可改，前端说了算**（§9.6.5）：
- `config.json` —— 本地文件，适合「我就要这么写死」或没起大脑时兜底；
- 小窝前端「💬 QQ 桥设置」—— 存本体（`/api/nest/features/qq_bridge/settings`），
  **优先级更高**，每轮刷新，改完就生效。大老大原话：「这块希望在前端可以自己配置」。

合并顺序：`DEFAULT` → `config.json` → 前端设置（`REMOTE`，见 settings.py）。
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
EXAMPLE_PATH = ROOT / "config.example.json"
STATE_DIR = ROOT / "data"

DEFAULT: dict = {
    "brain": {
        # 本体的地址。**只允许本机回环**（本体没有鉴权）—— 换了端口就改这里，别指到别的机器。
        "base_url": "http://127.0.0.1:8000",
    },
    "onebot": {
        # 两种连法（SnowLuma 为首选，本机正在用它；NapCat / Lagrange 同为 OneBot v11 这一套）：
        #   client = 正向 WS：我们主动连 ws_url（**默认** —— 网关那边什么都不用配、不用重启）
        #   server = 反向 WS：我们监听端口，网关配「反向 WS 地址」指过来
        # ⚠ 3010 是**本桥**的端口，故意避开 3000/3001 —— 那是 SnowLuma（以及 NapCat）
        #   默认占的 WS/HTTP 口，撞上就是「网关连不上」。多号依次排开，别重。
        "mode": "client",
        "listen_host": "127.0.0.1",
        "listen_port": 3010,
        "listen_path": "/onebot/v11/ws",
        "ws_url": "ws://127.0.0.1:3001/",   # mode=client 时才用（正向：我们连网关）
        "access_token": "",
        "self_id": "",          # 留空 = 连上后问网关要（get_login_info）
        "reconnect_seconds": 5,
        # 问网关要语音转写的等待上限（秒）。**必须大于网关自己等 QQ 推结果的时间**
        # （SnowLuma 是 20s），否则我们总先放弃，本来拿得到的也丢了。
        "ptt_timeout": 30,
    },
    # **多个 QQ 号**（大老大：「我 QQ 号一堆，可以每个都配一个」）：
    # 一个账号 = 一个小糖人 = 一份 {self_id, persona, 端口, 白名单}。
    # 空列表 = 单账号模式，用下面 onebot.* / persona.* / allow.*。
    "accounts": [],
    "allow": {
        # 空 = 谁都不理。真要用必须显式填，防「刚装好就被拉进陌生群」。
        "groups": [],
        "private": [],
        "require_mention_in_group": True,   # 群里要 @ 才回（不想让她插嘴就靠这个）
        "ignore_bots": True,
    },
    "persona": {
        # 这个 QQ 号演谁。留空 = 名册第一个人（会提醒你一次）。
        # **一个账号 = 一个小糖人**：想几个小糖人都在 QQ 上，就配多条 `accounts`
        # （一份配置带多个号），或者把这个项目跑几份、各配各的 config.json。
        "character_id": "",
    },
    "owner": {
        "qq": "",                # 管理员 QQ。留空 = 没有管理员（管理命令全禁用）
        "admin_commands": True,  # /reset /silent /active 这类（**桥接硬执行**，不过模型）
        "whitelist_bypass": True,  # 管理员私聊不受 allow.private 限制
        "tools": None,           # None = 跟 tools.require_owner 走
    },
    "sync": {
        "to_qq": True,           # 网页新消息同步到该账号最近使用的同身份 QQ 会话
        "to_frontend": True,     # QQ 里聊的同步进前端聊天记录（群聊 → group，私聊 → single-<角色>）
        "persona_id": "",        # 留空 = 默认身份（大老大）；填了会加 `<身份>@` 前缀
        "prefix_remote": True,   # 对方昵称作前缀写进内容，免得前端看不出谁在说话
    },
    "reply": {
        "debounce_seconds": 2.0,      # 连续文字/表情在停顿后合为一轮
        "max_wait_seconds": 10.0,     # 不无限等待续发
        "max_bubbles": 4,
        "interval_seconds": 1.2,
        "chunk_chars": 400,           # 长回复按这个长度切条发（QQ 单条有上限）
        "mention_sender_in_group": True,  # 群里回话时 @ 一下说话的人
    },
    "media": {
        # 图 / 表情包 / 视频都交给**大脑的附件管道**落盘（前端才显示得出来），这里只管送进去。
        "video_frames": 4,            # QQ 来的视频抽几帧送视觉（≤8，token 安全线）
        "treat_small_image_as_sticker": False,  # 把小图当表情包收进图书馆（默认按普通图）
    },
    "voice": {
        "enabled": True,         # 小糖人能不能发语音（TTS → QQ 语音条）
        "engine": "edge",        # 走大脑的 /api/voice/tts
        "voice": "zh-CN-XiaoxiaoNeural",
        "auto": "mirror",        # off / mirror（对方发语音我也发语音）/ always
        "max_chars": 80,         # 太长了不像语音条，超了自动改发文字
        "max_per_minute": 3,     # 语音额度与文本分开（抄参考项目的做法）
    },
    "asr": {
        "enabled": True,
        # qq     = 用**QQ 自己的转写**（手机上那个「转文字」，网关一般挂在 record 段里）—— 默认；
        # openai = QQ 没给转写时再用外部 whisper 兜一层（要花钱、音频过上游）；
        # none   = 不识别，收到就记 [语音]。
        # 大老大批注：「ASR 用 QQ 的，按项目的来，qq 有转写的」。
        "engine": "qq",
        # QQ 没有返回转写时，默认调用本体的本地 Paraformer（需单独安装依赖和模型）。
        # none 可关闭；只有显式选 openai 才上传到已配置云端并可能计费。
        "fallback": "local",
        "base_url": "",          # 留空 = 用大脑给这个功能配的 base_url
        "api_key": "",           # 留空 = 用大脑那份 key（不落盘到别处）
        "model": "whisper-1",
        "language": "zh",
        "max_seconds": 120,      # 超长语音不转写（省 token，也不做录音长期留存）
    },
    "sticker": {
        "auto_describe": True,       # 原图只自动标注一次；不写长期记忆
        "enabled": True,
        "auto_collect": True,    # 收到别人的表情就学下来
        # 0 = 不主动发（只在你点名要的时候发）。大老大喜欢小糖人的表情包，可往上调。
        "reply_chance": 0.0,
        # 表情包**存在大脑的附件库里**（前端显示、删消息级联、存储管理都跟着走），
        # 这里只是一份索引：一串 `/api/files/<会话>/orig/<hash>.<ext>` 引用。
        "library": [],
        "library_session": "single-dalaoda",
    },
    "proactive": {
        "enabled": False,
        "account_index": 0,
        "persona_id": "",
        "min_minutes": 90,
        "max_minutes": 180,
        "daily_limit": 3,
        "start_hour": 10,
        "end_hour": 23,
        "bilibili": True,
        "run_tokens": 12000,
        "day_tokens": 24000,
        "group_id": "",
        "qzone": False,
        "qzone_right": 64,
    },
    "memory": {
        "enabled": True,
        # 以下三项仅保留给旧兼容方法；正常运行统一走本体每日记忆流程。
        "isolate": False,
        "window": 24,
        "idle_seconds": 300,
    },
    # 本地小工具（大老大：「不碰本地工具的话，我倒是希望他能碰一下，只要别搞坏东西」）。
    # 默认**关**。开了之后：只在 root 里活动 + 只跑登记表里的动作 + 危险操作永久拒绝。
    # ⚠ `allow` 里只能写登记表里有的名字：dir / read / find / info（本地只读），
    #    以及 search（联网搜索，走 omnireach）。配置只能从登记表里**挑**，
    #    加不进新动作（写了个不存在的名字就是没这一项）。
    "tools": {
        "enabled": False,
        "allow": ["search"],
        "require_owner": True,   # 只有大老大的消息能触发
        "max_turns": 3,          # 一轮里最多来回几次（防模型钻牛角尖）
    },
    # 事件（大老大：「都想要」）—— 戳一戳 / 群成员变动 / 撤回。
    "events": {
        "poke": True,            # 被戳一戳回一句（同一人 30 秒内只理一次，不刷屏）
        "notice": False,         # 群成员变动 / 撤回 → 同步一条记录进小窝（不开口）
    },
    "modes": {
        "dm": "chat",            # chat / silent
        "group": "chat",
    },
}

# 前端设置里的 key（`feature_settings` 表）→ 本地配置的路径。
# 登记表就是**白名单**：不在表里的 key 一律忽略，免得前端顺手改了别的项。
REMOTE_KEYS: dict[str, tuple[str, ...]] = {
    'bridge.basic_mode': ('bridge', 'basic_mode'),
    'bridge.selection_required': ('bridge', 'selection_required'),
    **{f'proactive.{key}': ('proactive', key) for key in (
        'enabled', 'account_index', 'persona_id', 'min_minutes', 'max_minutes',
        'daily_limit', 'start_hour', 'end_hour', 'bilibili', 'run_tokens', 'day_tokens',
        'group_id', 'qzone', 'qzone_right')},
    "reply.debounce_seconds": ("reply", "debounce_seconds"),
    "reply.max_wait_seconds": ("reply", "max_wait_seconds"),
    "reply.max_bubbles": ("reply", "max_bubbles"),
    "reply.interval_seconds": ("reply", "interval_seconds"),
    "sticker.auto_describe": ("sticker", "auto_describe"),
    "persona.character_id": ("persona", "character_id"),
    "onebot.self_id": ("onebot", "self_id"),
    "onebot.mode": ("onebot", "mode"),
    "onebot.listen_port": ("onebot", "listen_port"),
    "onebot.listen_path": ("onebot", "listen_path"),
    "onebot.ws_url": ("onebot", "ws_url"),
    "onebot.access_token": ("onebot", "access_token"),
    "allow.groups": ("allow", "groups"),
    "allow.private": ("allow", "private"),
    "allow.require_mention_in_group": ("allow", "require_mention_in_group"),
    "allow.ignore_bots": ("allow", "ignore_bots"),
    "owner.qq": ("owner", "qq"),
    "owner.admin_commands": ("owner", "admin_commands"),
    "owner.tools": ("owner", "tools"),
    "voice.enabled": ("voice", "enabled"),
    "voice.engine": ("voice", "engine"),
    "voice.voice": ("voice", "voice"),
    "voice.auto": ("voice", "auto"),
    "voice.max_chars": ("voice", "max_chars"),
    "voice.max_per_minute": ("voice", "max_per_minute"),
    "asr.engine": ("asr", "engine"),
    "asr.fallback": ("asr", "fallback"),
    "asr.base_url": ("asr", "base_url"),
    "asr.api_key": ("asr", "api_key"),
    "asr.model": ("asr", "model"),
    "sticker.enabled": ("sticker", "enabled"),
    "sticker.reply_chance": ("sticker", "reply_chance"),
    "sticker.auto_collect": ("sticker", "auto_collect"),
    "sticker.treat_small_image_as_sticker": ("media", "treat_small_image_as_sticker"),
    "sticker.library": ("sticker", "library"),
    "sticker.library_session": ("sticker", "library_session"),
    "tools.enabled": ("tools", "enabled"),
    "tools.allow": ("tools", "allow"),
    "tools.require_owner": ("tools", "require_owner"),
    "sync.to_frontend": ("sync", "to_frontend"),
    "sync.to_qq": ("sync", "to_qq"),
    "sync.persona_id": ("sync", "persona_id"),
    "memory.isolate": ("memory", "isolate"),
    "memory.enabled": ("memory", "enabled"),
    "memory.idle_seconds": ("memory", "idle_seconds"),
    "events.poke": ("events", "poke"),
    "events.notice": ("events", "notice"),
    "media.video_frames": ("media", "video_frames"),
    "modes.group": ("modes", "group"),
    "modes.dm": ("modes", "dm"),
}


def _merge(base: dict, over: dict) -> dict:
    """深合并：只覆盖写了的键，缺的继续用默认值（旧配置不会因为加字段就崩）。"""
    out = dict(base)
    for key, value in (over or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_raw(path: Path = CONFIG_PATH) -> dict:
    """只读本地那两份（DEFAULT + config.json），不含前端设置。"""
    if not path.is_file():
        return json.loads(json.dumps(DEFAULT))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return json.loads(json.dumps(DEFAULT))
    return _merge(DEFAULT, raw if isinstance(raw, dict) else {})


# 前端设置那一份（`settings.pull()` 拉到之后填进来）。改完不用重启 —— 每轮重新合并。
REMOTE: dict = {}


def apply_remote(cfg: dict, remote: dict | None = None) -> dict:
    """把前端设置铺到配置上。**只认 REMOTE_KEYS 里登记过的 key + `accounts`**，其余忽略。

    `accounts` 特殊：它不是「一个路径上一个值」，而是**整份账号表**（一个 QQ 号一行）。
    大老大要的就是「哪个号演谁在前端切换」——所以这里整表替换，不是逐字段合并。
    空表 = 没在前端配过多账号，保持本地 `config.json` 里那份（单账号或多账号都行）。
    """
    out = cfg
    src = REMOTE if remote is None else remote
    for key, value in src.items():
        if str(key) == "accounts":
            # 前端存过（非空）才覆盖；空表不动本地那份，免得「一打开前端就把号清空」。
            if isinstance(value, list) and value:
                out["accounts"] = value
            continue
        path = REMOTE_KEYS.get(str(key))
        if not path:
            continue
        node = out
        for step in path[:-1]:
            nxt = node.get(step)
            if not isinstance(nxt, dict):
                nxt = {}
                node[step] = nxt
            node = nxt
        node[path[-1]] = value
    return out


def set_remote(remote: dict) -> None:
    """进程里存一份前端设置（`settings.pull()` 拉到之后调）。"""
    global REMOTE
    REMOTE = dict(remote or {})


def load(path: Path = CONFIG_PATH) -> dict:
    """生效配置 = 默认 → config.json → 前端设置。改完下一轮就读到，不用重启。"""
    return apply_remote(load_raw(path))


def accounts(cfg: dict | None = None) -> list[dict]:
    """**这份配置要连几个 QQ 号**，每个演谁。

    三种情形：
    - `accounts` 里有启用的行 → 每行一个号（一份配置带多个号，各连各的网关端口）；
    - 没有 → 回落到「单账号」：一条，字段来自 `onebot.*` / `persona.*` / `allow.*`；
    - 某号没写 `groups` / `private` / `ws_url` → `None`，由调用方回落到顶层那份。
    """
    cfg = cfg if cfg is not None else load()
    if (cfg.get('bridge') or {}).get('selection_required'):
        return []
    rows = [row for row in (cfg.get("accounts") or []) if isinstance(row, dict) and row.get("enabled", True)]
    if (cfg.get('bridge') or {}).get('basic_mode'):
        rows = rows[:1]
    if not rows:
        return [{
            "name": "默认", "self_id": str((cfg.get("onebot") or {}).get("self_id") or ""),
            "persona": "", "listen_port": None, "access_token": None,
            "ws_url": None, "groups": None, "private": None,
        }]
    base_port = int((cfg.get("onebot") or {}).get("listen_port") or 3010)
    out: list[dict] = []
    for index, row in enumerate(rows):
        out.append({
            "name": str(row.get("name") or f"号{index + 1}"),
            "self_id": str(row.get("self_id") or ""),
            "persona": str(row.get("persona") or ""),
            "independent_persona": True,
            # 没写端口就依次排开（3010 / 3011 …）—— 反向 WS 每个号必须占一个端口。
            "listen_port": int(row["listen_port"]) if row.get("listen_port") else base_port + index,
            "access_token": row.get("access_token"),   # None = 用顶层那个
            # 正向 WS（桥主动连网关）时，**每个号连自己的那个 ws 服务端**——
            # SnowLuma 是「一个 QQ 号一个 ws server 端口」，所以这里也得一个号一个地址。
            "ws_url": str(row.get("ws_url") or "").strip() or None,
            "groups": row.get("groups"),               # None = 用顶层白名单
            "private": row.get("private"),
        })
    return out


def write_example(path: Path = EXAMPLE_PATH) -> Path:
    path.write_text(json.dumps(DEFAULT, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def state_file(name: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / name
