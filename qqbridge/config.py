"""配置：`config.json`（**不进 git**，模板是 `config.example.json`）。

设计原则和参考项目一样但更轻：
- **默认关**。没配 `allow.groups` / `allow.private` 就是「谁都不理」—— 宁可不动，不可乱动。
- **默认安静**。群里默认要 @ 才回；语音默认只在对方先发语音时回语音（避免开口吓人）。
- 改完不用重启：`bridge.py` 每轮前重新读一次（文件很小，省得搞热重载那套）。
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
        # 两种连法（SnowLuma / NapCat / Lagrange 都是 OneBot v11 这一套）：
        #   server = 反向 WS：我们监听端口，网关配「反向 WS 地址」指过来（**推荐**）
        #   client = 正向 WS：我们主动连 ws_url
        "mode": "server",
        "listen_host": "127.0.0.1",
        "listen_port": 3001,
        "listen_path": "/onebot/v11/ws",
        "ws_url": "ws://127.0.0.1:3001",   # mode=client 时才用
        "access_token": "",
        "self_id": "",          # 留空 = 连上后问网关要（get_login_info）
        "reconnect_seconds": 5,
    },
    "allow": {
        # 空 = 谁都不理。真要用必须显式填，防「刚装好就被拉进陌生群」。
        "groups": [],
        "private": [],
        "require_mention_in_group": True,   # 群里要 @ 才回（不想让她插嘴就靠这个）
        "ignore_bots": True,
    },
    "persona": {
        # 这个 QQ 号演谁。留空 = 名册第一个人（会提醒你一次）。
        # **一个账号 = 一个小糖人**：想让几个小糖人都在 QQ 上，就跑几份、各配各的。
        "character_id": "",
    },
    "owner": {
        "qq": "",                # 管理员 QQ。留空 = 没有管理员（管理命令全禁用）
        "admin_commands": True,  # /reset /silent /active 这类（**桥接硬执行**，不过模型）
        "whitelist_bypass": True,  # 管理员私聊不受 allow.private 限制
    },
    "sync": {
        "to_frontend": True,     # QQ 里聊的同步进前端聊天记录（群聊 → group，私聊 → single-<角色>）
        "persona_id": "",        # 留空 = 默认身份（大老大）；填了会加 `<身份>@` 前缀
        "prefix_remote": True,   # 对方昵称作前缀写进内容，免得前端看不出谁在说话
    },
    "reply": {
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
        "engine": "none",        # none / openai（OpenAI 兼容 /audio/transcriptions）
        "base_url": "",          # 留空 = 用大脑给这个功能配的 base_url
        "api_key": "",           # 留空 = 用大脑那份 key（不落盘到别处）
        "model": "whisper-1",
        "language": "zh",
        "max_seconds": 120,      # 超长语音不转写（省 token，也不做录音长期留存）
    },
    "sticker": {
        "enabled": True,
        "auto_collect": True,    # 收到别人的表情就学下来（本地库，不外传）
        "reply_chance": 0.0,     # 0 = 只在被明确要求时表情包回复（默认不主动刷屏）
    },
    "memory": {
        "enabled": True,
        "isolate": False,        # False = 汇入普通记忆（大老大批注：QQ 聊的日常也记得）
        "window": 24,            # 攒够多少条提一次
    },
    "modes": {
        "dm": "chat",            # chat / silent
        "group": "chat",
    },
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


def load(path: Path = CONFIG_PATH) -> dict:
    if not path.is_file():
        return json.loads(json.dumps(DEFAULT))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return json.loads(json.dumps(DEFAULT))
    return _merge(DEFAULT, raw if isinstance(raw, dict) else {})


def write_example(path: Path = EXAMPLE_PATH) -> Path:
    path.write_text(json.dumps(DEFAULT, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def state_file(name: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / name