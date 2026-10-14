"""会话路由：**QQ 的一头 ↔ 前端聊天记录的一头**。

前端的会话命名是固定的（`group` / `single-<角色id>`，见本体 §15）：
- QQ 群聊  → 前端**群聊**（`group`）
- QQ 私聊  → 前端**对应的私聊**（`single-<这个账号的小糖人id>`）

所以「同步进前端」不是另开一套，而是把同一轮对话往这两个 session 里各写一份 ——
大老大在小窝里往下翻，就能看见 QQ 里聊过什么。

**一个账号 = 一个小糖人**：一个桥接进程可以带**多个 QQ 号**（`config.accounts()`，大老大：
「我 QQ 号一堆，可以每个都配一个」）。每个号有自己的人格、自己的白名单、自己的网关端口，
所以每个号一个 `Router` 实例、一个 `OneBotClient` 连接 —— 它们互不干扰。
群聊里被 @ 时由本账号那个人格回；几个小糖人同在群里就各回各的 —— 不会互相抢话。

前端「这个号演谁」的切换：小窝里改 `accounts[i].persona`（或单账号的 `persona.character_id`），
桥每轮 pull 一次设置 → 下一轮就换人，不用重启。

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

    def __init__(self, cfg: dict, brain: BrainClient, logger=print, account: dict | None = None):
        self.cfg = cfg
        self.brain = brain
        self.log = logger
        # 这个 Router 服务的那个 QQ 号（`config.accounts()` 里的一条）。None = 单账号。
        self.account = account or {}
        self.seen = SeenMessages()
        self.seen.load(config_mod.state_file(
            f"seen_messages{'-' + str(self.account.get('self_id')) if self.account.get('self_id') else ''}.json"))
        self._persona = ""
        self._roster: list[dict] = []
        self.pending: dict[str, PendingTurn] = {}
        self._intro: list[str] = []      # 启动时想让人看见的话（多账号时由 handle 打前缀）

    def note(self, text: str) -> None:
        """记一句要在日志里说的话（多账号时自动带 `[号名]` 前缀）。"""
        self._intro.append(text)
        self.log(text)

    def drain(self) -> list[str]:
        out, self._intro = self._intro, []
        return out

    def refresh_roster(self) -> list[dict]:
        """从大脑拉角色名册（= 角色介绍里的人）。拉不到就退回配置里的那个。"""
        self._roster = self.brain.characters()
        return self._roster

    def persona(self) -> str:
        """本账号演谁。优先级：启动时指定的 → 这个号的配置 → 名册第一个（并提醒一次）。

        **不猜**：没配就明确说「你没配，我先用谁」—— 悄悄换人最难受。
        """
        if self._persona:
            return self._persona
        name = str(self.override or self.account.get("persona")
                   or ((self.cfg.get("persona") or {}).get("character_id")) or "").strip()
        if not name:
            roster = self._roster or self.refresh_roster()
            if roster:
                name = str(roster[0].get("id") or "")
                self.note(f"⚠ 没写这个号演谁，先按名册第一个人 `{name}` 演。"
                          f"要换人去小窝前端「💬 QQ 桥设置 → 谁在 QQ 上」里改。")
        self._persona = name
        return name

    def reset_persona(self) -> None:
        """设置变了（前端换了人）→ 下次重新算一遍，并把提醒也重置掉。"""
        self._persona = ""

    def persona_name(self) -> str:
        pid = self.persona()
        for item in self._roster:
            if item.get("id") == pid:
                return str(item.get("name") or pid)
        return pid or "?"

    # ---- 放行判断 ----

    def _groups(self) -> list[str]:
        """这个号接哪些群：**号上写了就用号的**，没写才用顶层白名单。"""
        own = self.account.get("groups")
        if own is not None:
            return [str(g) for g in own]
        return [str(g) for g in ((self.cfg.get("allow") or {}).get("groups") or [])]

    def _private(self) -> list[str]:
        own = self.account.get("private")
        if own is not None:
            return [str(p) for p in own]
        return [str(p) for p in ((self.cfg.get("allow") or {}).get("private") or [])]

    def allowed(self, event: Event) -> tuple[bool, str]:
        """**白名单**判断：这个群 / 这个人允不允许被我看见。

        注意这里**不管 @ 不 @** —— 「能不能看见」和「要不要开口」是两件事
        （见 route()）：白名单里的群聊会同步进前端，但只有 @ 我才回。
        """
        owner = str(((self.cfg.get("owner") or {}).get("qq")) or "")
        is_owner = bool(owner) and event.user_id == owner
        if event.user_id == event.self_id:
            return False, "自己发的"
        if event.is_group:
            if event.group_id not in self._groups():
                return False, f"群 {event.group_id} 不在白名单里"
            return True, ""
        if event.user_id in self._private():
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
        # 已经 @ 我了 → 一定是叫我（不用再看后面的规则）。
        if event.is_group and not event.mentions_me():
            owner = str(((self.cfg.get("owner") or {}).get("qq")) or "")
            is_owner = bool(owner) and event.user_id == owner
            bypass = (self.cfg.get("owner") or {}).get("whitelist_bypass", True)
            # **「叫名字」也算叫我**：QQ 群里的 @ 经常被客户端吞成纯文本（尤其是转发 / 复制的话），
            # 只认 at 段会漏掉一大半。这里多认一条「消息里出现了本账号小糖人的名字」。
            if self.mentions_name(event):
                return Route(True, True, session, kind, character_id, "")
            if (self.cfg.get("allow") or {}).get("require_mention_in_group", True) and not (bypass and is_owner):
                # 群里别的话：同步下来（大老大看得到群聊上下文），但不插嘴。
                return Route(True, False, session, kind, character_id, "群里没 @ 我：只同步，不插嘴")
        return Route(True, True, session, kind, character_id, "")

    def mentions_name(self, event: Event) -> bool:
        """消息文本里有没有直接叫本账号那位小糖人的名字（兜住「@ 被吞成文本」的情况）。"""
        name = self.persona_name()
        if not name or name == "?" or len(name) < 2:
            return False
        return name in event.plain_text()

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
        self.seen.save(self._seen_path())

    def _seen_path(self) -> Path:
        """去重表按账号分开存（多账号共用一个文件会互相顶掉）。"""
        tag = f"-{self.account.get('self_id')}" if self.account.get("self_id") else ""
        return config_mod.state_file(f"seen_messages{tag}.json")

    # ---- 管理命令（**桥直接执行，不过模型**）----

    def admin_command(self, event: Event) -> dict | None:
        """大老大发的 `/xxx` 命令 → 桥自己执行。

        返回 `{"reply": 回什么, "action": 要让桥做什么, "arg": 参数}`；不是命令就 None。
        为什么不过模型：这些是**操作**（清上下文 / 静音 / 发言），让模型决定「要不要照做」
        等于把开关交给了概率。参考项目也是桥接硬执行，我们照抄。

        `action` 取值：`""`（只回话）/ `reset` / `silent` / `active` / `say`。
        """
        owner = str(((self.cfg.get("owner") or {}).get("qq")) or "")
        if not (self.cfg.get("owner") or {}).get("admin_commands", True):
            return None
        if not owner or event.user_id != owner:
            return None
        text = event.plain_text().strip()
        if not text.startswith("/"):
            return None
        name, _, rest = text[1:].partition(" ")
        name, rest = name.strip().lower(), rest.strip()
        if name in ("help", "?"):
            return {"action": "", "arg": "", "reply": (
                "能用的：/reset 忘掉这个会话刚才聊的 · /silent 这个会话只看不说 · "
                "/active 恢复说话 · /whoami 我是谁 · /say 内容（我直接照发，不过模型）")}
        if name == "whoami":
            return {"action": "", "arg": "",
                    "reply": f"我是「{self.persona_name()}」（{self.persona()}），这个号 {event.self_id or '(未知)'}。"}
        if name == "reset":
            return {"action": "reset", "arg": "", "reply": "好，这个会话刚才聊的我不记得了。"}
        if name == "silent":
            return {"action": "silent", "arg": "", "reply": "这个会话我只看不说了（/active 恢复）。"}
        if name == "active":
            return {"action": "active", "arg": "", "reply": "回来了，说话吧。"}
        if name == "say" and rest:
            return {"action": "say", "arg": rest, "reply": ""}
        return {"action": "", "arg": "", "reply": f"不认识 `/{name}`，发 /help 看看有哪些。"}