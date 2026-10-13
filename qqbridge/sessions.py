"""会话路由：**QQ 的一头 ↔ 前端聊天记录的一头**。

前端的会话命名是固定的（`group` / `single-<角色id>`，见本体 §15）：
- QQ 群聊  → 前端**群聊**（`group`）
- QQ 私聊  → 前端**对应的私聊**（`single-<这个账号的小糖人id>`）

所以「同步进前端」不是另开一套，而是把同一轮对话往这两个 session 里各写一份 ——
大老大在小窝里往下翻，就能看见 QQ 里聊过什么。

**一个账号 = 一个小糖人**：一个桥接进程管一个 QQ 号（也就一个小糖人）。
想让多个小糖人都在 QQ 上，就把这个项目跑多份、每份一个账号（各自的 config.json）。
群聊里被 @ 时由本账号那个人格回；几个小糖人同在群里就各回各的 —— 不会互相抢话。

去重：网关重连时可能把最近的事件重放一遍（各家实现不一），所以按 message_id 记一个
有上限的小 LRU，见过的直接跳过 —— 不然重连一次就会重复回一遍话。
"""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from . import config as config_mod
from .brain import BrainClient, BrainError
from .onebot import Event


@dataclass
class Route:
    """一条消息该往哪去：谁回、回哪个 session、记忆归谁。

    `sync` 和 `reply` 是**两件事**：
    - `sync`   —— 写进前端聊天记录（白名单里的会话都同步，不然「在 QQ 聊的在窝里看不见」）
    - `reply`  —— 小糖人开不开口（群里默认要 @；静音模式只同步不回）
    """
    sync: bool
    reply: bool
    frontend_session: str          # 写进前端的哪个会话
    session_kind: str              # group | single（记忆提炼要用）
    character_id: str              # 本账号的人格（= 回话的这位）
    reason: str = ""               # 为什么回 / 为什么不回（日志里能看懂）


@dataclass
class PendingTurn:
    """攒着准备提炼记忆的一轮对话。"""
    session_kind: str
    character_ids: list[str]
    lines: list[str] = field(default_factory=list)

    def add(self, who: str, text: str) -> None:
        text = (text or "").strip()
        if text:
            self.lines.append(f"{who}：{text}")

    def flush_text(self) -> str:
        return "\n".join(self.lines)


class SeenMessages:
    """有上限的「见过的消息 id」集合（重连重放 / 自己发的回声都挡在外头）。"""

    def __init__(self, size: int = 400):
        self.size = size
        self._seen: OrderedDict[str, None] = OrderedDict()

    def check(self, message_id: str) -> bool:
        """第一次见 → True；已经见过 → False。"""
        if not message_id:
            return True
        if message_id in self._seen:
            self._seen.move_to_end(message_id)
            return False
        self._seen[message_id] = None
        while len(self._seen) > self.size:
            self._seen.popitem(last=False)
        return True

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(list(self._seen), ensure_ascii=False), encoding="utf-8")

    def load(self, path: Path) -> None:
        if not path.is_file():
            return
        try:
            for item in json.loads(path.read_text(encoding="utf-8")):
                self._seen[str(item)] = None
        except (json.JSONDecodeError, OSError, TypeError):
            pass


class Router:
    """把「收谁的话、谁来回、写到哪个会话」这套判断收在一处。"""

    # 启动参数指定的身份（小窝前端「这个号演谁」）—— 优先于配置文件。
    override = ""

    def __init__(self, cfg: dict, brain: BrainClient, logger=print):
        self.cfg = cfg
        self.brain = brain
        self.log = logger
        self.seen = SeenMessages()
        self.seen.load(config_mod.state_file("seen_messages.json"))
        self._persona = ""
        self._roster: list[dict] = []
        self.pending: dict[str, PendingTurn] = {}

    # ---- 人格 / 名册 ----

    def refresh_roster(self) -> list[dict]:
        """从大脑拉角色名册（= 角色介绍里的人）。拉不到就退回配置里的那个。"""
        self._roster = self.brain.characters()
        return self._roster

    def persona(self) -> str:
        """本账号演谁。优先级：启动时指定的 → config.persona.character_id → 名册第一个（并提醒一次）。

        **不猜**：没配就明确说「你没配，我先用谁」—— 悄悄换人最难受。
        """
        if self._persona:
            return self._persona
        name = str(self.override or ((self.cfg.get("persona") or {}).get("character_id")) or "").strip()
        if not name:
            roster = self._roster or self.refresh_roster()
            if roster:
                name = str(roster[0].get("id") or "")
                self.log(f"⚠ config.json 没写 persona.character_id，先按名册第一个人 `{name}` 演。"
                         f"要换人就把这一项填上。")
        self._persona = name
        return name

    def persona_name(self) -> str:
        pid = self.persona()
        for item in self._roster:
            if item.get("id") == pid:
                return str(item.get("name") or pid)
        return pid or "?"

    # ---- 放行判断 ----

    def allowed(self, event: Event) -> tuple[bool, str]:
        """**白名单**判断：这个群 / 这个人允不允许被我看见。

        注意这里**不管 @ 不 @** —— 「能不能看见」和「要不要开口」是两件事
        （见 route()）：白名单里的群聊会同步进前端，但只有 @ 我才回。
        """
        allow = self.cfg.get("allow") or {}
        owner = str(((self.cfg.get("owner") or {}).get("qq")) or "")
        is_owner = bool(owner) and event.user_id == owner
        if event.user_id == event.self_id:
            return False, "自己发的"
        if event.is_group:
            if event.group_id not in [str(g) for g in (allow.get("groups") or [])]:
                return False, f"群 {event.group_id} 不在白名单里"
            return True, ""
        if event.user_id in [str(p) for p in (allow.get("private") or [])]:
            return True, ""
        if is_owner and (self.cfg.get("owner") or {}).get("whitelist_bypass", True):
            return True, ""
        return False, f"私聊 {event.user_id} 不在白名单里"

    def route(self, event: Event) -> Route:
        """一条事件 → 同步到哪、要不要开口、谁回。"""
        character_id = self.persona()
        silent = Route(False, False, "", "group" if event.is_group else "single", character_id)
        ok, why = self.allowed(event)
        if not ok:
            silent.reason = why
            return silent
        # 白名单里的会话**一律同步进前端**（大老大要能在小窝里看见 QQ 里聊了什么），
        # 哪怕这条不 @ 我、哪怕这个会话设了静音 —— 同步是记录，不是打扰。
        session = "group" if event.is_group else f"single-{character_id}"
        kind = "group" if event.is_group else "single"
        if not self.seen.check(event.message_id):
            return Route(True, False, session, kind, character_id, "这条消息已经同步过了（重放）")
        mode = ((self.cfg.get("modes") or {}).get("group" if event.is_group else "dm") or "chat")
        if mode == "silent":
            return Route(True, False, session, kind, character_id, "这个会话设成静音了：只同步，不开口")
        if event.is_group and (self.cfg.get("allow") or {}).get("require_mention_in_group", True) \
                and not event.mentions_me():
            # 群里别的话：同步下来（大老大看得到群聊上下文），但不插嘴。
            owner = str(((self.cfg.get("owner") or {}).get("qq")) or "")
            bypass = (self.cfg.get("owner") or {}).get("whitelist_bypass", True)
            if not (bypass and owner and event.user_id == owner):
                return Route(True, False, session, kind, character_id, "群里没 @ 我：只同步，不插嘴")
        return Route(True, True, session, kind, character_id, "")

    # ---- 记忆轮次 ----

    def remember(self, session_key: str, session_kind: str, character_id: str, who: str, text: str) -> None:
        turn = self.pending.setdefault(session_key, PendingTurn(session_kind, [character_id]))
        turn.add(who, text)
        if character_id not in turn.character_ids:
            turn.character_ids.append(character_id)

    def flush_memory(self) -> None:
        """把攒够的轮次交给大脑提炼记忆（默认进**普通记忆**，见 brain.extract_memories）。"""
        mem = self.cfg.get("memory") or {}
        if not mem.get("enabled", True):
            self.pending.clear()
            return
        window = int(mem.get("window") or 24)
        self.brain.isolate_memory = bool(mem.get("isolate", False))
        for key, turn in list(self.pending.items()):
            if len(turn.lines) < window:
                continue
            text = turn.flush_text()
            self.pending.pop(key, None)
            try:
                self.brain.extract_memories(text, character_ids=turn.character_ids,
                                            session_kind=turn.session_kind)
                self.log(f"🧠 记住了一轮（{len(turn.lines)} 条）")
            except BrainError as exc:
                self.log(f"⚠ 记忆提炼没成功：{exc}")

    def save(self) -> None:
        self.seen.save(config_mod.state_file("seen_messages.json"))