"""把**小窝前端改的设置**拉回来（§9.6.5）。

大老大原话：「这块希望在前端可以自己配置，现在说比较复杂」——
以前改白名单 / 音色 / 表情包开关得开终端编辑 `config.json`（中文还乱码），现在：

    小窝前端「💬 QQ 桥设置」──PUT──▶ 本体（feature_settings 表）
                                          │
                                    本进程每轮 pull() 一次
                                          ▼
                            覆盖 config.json 里的同名项 → 立刻生效

三件事说清楚：

1. **前端优先**。本地 `config.json` 仍然有用（没起大脑时兜底、想写死时方便），
   但前端写的值覆盖它 —— 你在网页上点的那一下就是最后生效的那个。
2. **只认登记过的 key**（`config.REMOTE_KEYS`）。前端塞别的 key 进来一律忽略 ——
   免得「顺手改点别的」这种事发生。
3. **拉不到不算错**。大脑没起、功能没声明 → 保持本地配置，桥照跑（单向依赖，不反向拖累）。
"""

from __future__ import annotations

from . import config as config_mod
from .brain import BrainClient, BrainError


class SettingsSync:
    """每轮拉一次前端设置（30 秒的 ticker 里调 `pull()`）。"""

    def __init__(self, brain: BrainClient, logger=print):
        self.brain = brain
        self.log = logger
        self._last: dict | None = None
        self._stamp = ""

    def pull(self, *, quiet: bool = False) -> dict:
        """拉一次并铺到 `config.REMOTE`。返回这次生效的那份（拉不到就返回上次的）。"""
        try:
            values = self.brain.settings()
        except (BrainError, ValueError) as exc:
            if not quiet:
                self.log(f"ℹ 没能从大脑读到前端设置（用本地 config.json 照跑）：{exc}")
            return config_mod.REMOTE
        # 只留登记过的 key（前端将来加了新字段、桥这边还没更新时不会误用）。
        clean = {k: v for k, v in (values or {}).items() if k in config_mod.REMOTE_KEYS or k == 'accounts'}
        if clean != self._last:
            changed = sorted(set(clean) ^ set(self._last or {}))
            self._last = clean
            config_mod.set_remote(clean)
            if changed and self._stamp:
                self.log(f"⚙ 前端设置变了（{'、'.join(changed[:6])}）—— 已经生效，不用重启")
        self._stamp = "pulled"
        return config_mod.REMOTE

    def summary(self) -> str:
        """给自检 / 启动日志用的一句话。"""
        remote = config_mod.REMOTE or {}
        if not remote:
            return "（还没从大脑读到，用本地 config.json）"
        keys = ("allow.groups", "allow.private", "voice.voice", "sticker.reply_chance", "tools.enabled")
        bits = [f"{k.split('.')[-1]}={remote.get(k)!r}" for k in keys if k in remote]
        return "、".join(bits) or f"{len(remote)} 项"
