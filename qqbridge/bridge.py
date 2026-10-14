"""QQ 桥的主循环：**一条消息进来 → 小糖人回话 → 两边都留痕**。

一句话说清它干的活：

    QQ（OneBot 网关）
      │  ① 收到 @我 / 叫我名字 / 私聊我的消息（白名单之外一律不理）
      ▼
    桥（本项目）  ──② 图片/表情包/视频/语音 → 下载字节 → 交给大脑落盘、抽帧、转写
      │           ──③ 带上「刚才聊过什么」让大脑里的这位小糖人说话（人格/记忆/密钥都在大脑）
      │           ──④ 大老大让它动手时：走大脑的工具轮（§9.6.6）拿决定，**在这里执行**
      ▼
    QQ  ◀──⑤ 回话（文字 / 语音条 / 表情包，长文自动切条）
    前端聊天记录 ◀──⑥ 同一轮对话也写进小窝：群聊 → 群聊会话，私聊 → 对应私聊

**边界**（和参考项目最重要的区别）：
- 本项目**不装 TTS、不落盘媒体、不自己抽帧** —— 全用大脑已经做好的那套（`/api/voice/tts`、
  附件管道、`/api/media/video/analyze`）。少一套实现就少一处会和前端不一致。
- 语音**识别**用 **QQ 自己的转写**（大老大定的：网关一般带 `record.text`），
  网关没给时才按 `asr.engine=openai` 走外部 whisper；两边都没有就明确记 `[语音]`。
- 群里默认**要 @ 才回**（@ 被客户端吞成纯文本时，叫名字也算）；语音默认**对方先发语音才回语音**。
- **多个 QQ 号**（大老大：「我 QQ 号一堆」）：一份配置里 `accounts` 写几条就带几个号，
  每个号自己的人格 / 白名单 / 端口 / 连接，互不干扰。
- 设置**全部在小窝前端改**（§9.6.5）：每 30 秒从大脑 pull 一次，改完就生效，不用重启。
- 本地小工具**默认关**，开了也只在划定的目录里、只跑白名单动作，动作在本进程执行（见 tools.py）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import shutil
import signal
import time
from collections import deque
from pathlib import Path

from . import config as config_mod
from . import media, onebot, tools
from .brain import BrainClient, BrainError
from .onebot import Event, Notice, OneBotClient
from .sessions import Router
from .settings import SettingsSync

# 一次最多送几张图给模型（token 安全线，和本体的 8 张抽帧同一档）。
MAX_IMAGES_PER_TURN = 4
# 群聊里聊过多少条之后，前面的话就不带了（省 token，也不至于忘得太快）。
HISTORY_TURNS = 16


class History:
    """每个 QQ 会话各记一段「刚才聊了什么」——大脑不存历史，得我们带过去。

    落 `data/conversations-<号>.json`：进程重启后小糖人不会突然失忆（重启前聊的还认得）。
    """

    def __init__(self, path: Path, turns: int = HISTORY_TURNS):
        self.path = path
        self.turns = turns
        self._data: dict[str, list[dict]] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._data = {str(k): list(v)[-turns:] for k, v in loaded.items() if isinstance(v, list)}
            except (json.JSONDecodeError, OSError, TypeError):
                self._data = {}

    def get(self, key: str) -> list[dict]:
        return [dict(m) for m in self._data.get(key, [])]

    def add(self, key: str, role: str, content: str) -> None:
        if not (content or "").strip():
            return
        bucket = self._data.setdefault(key, [])
        bucket.append({"role": role, "content": content})
        del bucket[:-self.turns]

    def clear(self, key: str) -> None:
        self._data.pop(key, None)

    def save(self) -> None:
        try:
            self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass


class AccountRunner:
    """**一个 QQ 号**：一条网关连接 + 一套判断 + 一段历史。

    多账号就是多个这东西并排跑（`Bridge` 负责一起起停）。它们之间只共享
    「大脑客户端」和「前端设置」这两样只读的东西 —— 谁也不影响谁。
    """

    def __init__(self, bridge: "Bridge", cfg: dict, account: dict, index: int):
        self.bridge = bridge
        self.brain = bridge.brain
        self.log = bridge.log
        self.cfg = cfg
        self.account = dict(account)
        self.index = index
        self.router = Router(cfg, self.brain, self.log, self.account)
        self.persona_override = ""      # 启动参数 `--characters`（小窝前端「这个号演谁」）
        tag = self.account.get("self_id") or f"号{index + 1}"
        self.history = History(config_mod.state_file(f"conversations-{tag}.json"))
        self.client = OneBotClient(self._client_cfg(cfg), self.handle, self.log, on_notice=self.on_notice)
        self._voice_times: deque[float] = deque()
        self._last_images: list[str] = []
        self._poke_seen: dict[str, float] = {}

    # ---- 账号 → 这份连接用的配置 ----

    def _client_cfg(self, cfg: dict) -> dict:
        """把「这个号的」端口 / token / self_id / 网关地址铺到通用配置上。

        - 反向 WS（`mode=server`）：每个号必须占一个**不同端口**，网关那边也按端口配；
        - 正向 WS（`mode=client`）：每个号要连**自己的那个网关地址**——
          SnowLuma 是「一个 QQ 号一个 ws server 端口」，所以 `ws_url` 也得一号一份。
        """
        out = json.loads(json.dumps(cfg))
        ob = out.setdefault("onebot", {})
        if self.account.get("listen_port"):
            ob["listen_port"] = int(self.account["listen_port"])
        if self.account.get("ws_url"):
            ob["ws_url"] = str(self.account["ws_url"])
        if self.account.get("access_token") is not None:
            ob["access_token"] = self.account.get("access_token") or ""
        if self.account.get("self_id"):
            ob["self_id"] = str(self.account["self_id"])
        return out

    def apply(self, cfg: dict, account: dict) -> None:
        """前端设置变了 → 就地更新这个号的判断项（**不重连**）。

        能热改的：演谁、白名单、群/私聊名单、模式、语音、表情包、工具。
        改不了的：**端口 / QQ 号**（那是连接本身），改了会明确提示要重启。
        """
        old = dict(self.account)
        self.cfg = cfg
        self.account = dict(account)
        self.router.cfg = cfg
        self.router.account = self.account
        if str(self.account.get("persona") or "") != str(old.get("persona") or ""):
            self.router.reset_persona()
            self.log(f"[{self.label()}] 换人了 → 现在演「{self.router.persona_name()}」")
        if str(self.account.get("listen_port") or "") != str(old.get("listen_port") or "") \
                or str(self.account.get("self_id") or "") != str(old.get("self_id") or ""):
            self.log(f"[{self.label()}] ⚠ 端口 / QQ 号改了 —— 这个得重启才生效（停掉再点一次启动）")

    def label(self) -> str:
        return self.account.get("name") or self.account.get("self_id") or "默认"

    def refresh_roster(self) -> None:
        self.router.refresh_roster()

    # ---- 起停 ----

    async def startup_log(self) -> None:
        cfg = self.cfg
        who = self.router.persona_name()
        self.log(f"[{self.label()}] 💬 这个号演的是「{who}」（{self.router.persona()}）")
        ob = (cfg.get("onebot") or {})
        if (self.client.mode or "server") == "server":
            self.log(f"[{self.label()}]    反向 WS 等在这里：ws://{ob.get('listen_host', '127.0.0.1')}:"
                     f"{self.client.port}{self.client.path}")
        groups, private = self.router._groups(), self.router._private()
        if not groups and not private:
            self.log(f"[{self.label()}] ⚠ 白名单是空的 —— **这个号谁都不理**。"
                     f"去小窝前端「💬 QQ 桥设置 → 白名单」里填。")
        else:
            self.log(f"[{self.label()}]    收这些：群 {groups or '（空）'} · 私聊 {private or '（空）'}")
        if tools.enabled(cfg):
            self.log(f"[{self.label()}] 🧰 本地小工具开着：{tools.allowed_names(cfg)} @ {tools.sandbox_root(cfg)}"
                     + ("（只有大老大能用）" if (cfg.get("tools") or {}).get("require_owner", True) else "（谁都能用）"))
        asr = str((cfg.get("asr") or {}).get("engine") or "qq")
        if asr == "qq":
            self.log(f"[{self.label()}] ℹ 语音识别用 QQ 自己的转写（网关给了就用）")

    # ---- 一条消息 ----

    async def handle(self, event: Event) -> None:
        """收到消息。**先判断该不该动**，再决定同步 / 回话。"""
        # 设置每轮重读一次（前端改完不用重启）。
        self.cfg = config_mod.load()

        # 管理命令最优先：`/reset` `/silent` 这类是**桥直接执行**的，不过模型。
        handled = await self._admin(event)
        if handled:
            return

        self.router.cfg = self.cfg
        route = self.router.route(event)
        if not route.sync and not route.reply:
            self.log(f"[{self.label()}] · 略过 {event.session_key}（{route.reason}）")
            return

        session_id = route.frontend_session
        text = event.plain_text()
        # 语音先转写：转不出字就只剩「[语音]」，也不装懂。
        voice_text, media_items = await self._collect_media(event, session_id)
        if voice_text:
            text = (text.replace("[语音]", f"[语音] {voice_text}") if text else f"[语音] {voice_text}")

        prefix = self._prefix(event)
        self.log(f"[{self.label()}] ← {event.session_key} {event.display_name}："
                 f"{(text or '（只有媒体）')[:40]}")
        await self._sync_inbound(session_id, prefix, text, media_items)
        self.router.remember(event.session_key, route.session_kind, route.character_id,
                             self._speaker(event), text or "[发了点东西]")
        if not route.reply:
            return
        await self._reply(event, route, session_id, text, media_items)

    async def _admin(self, event: Event) -> bool:
        """大老大的 `/命令`。返回 True = 这条已经处理完了，不用再走模型。"""
        command = self.router.admin_command(event)
        if command is None:
            return False
        off = False
        if command["action"] == "reset":
            self.history.clear(event.session_key)
            self.router.pending.pop(event.session_key, None)
        elif command["action"] in ("silent", "active"):
            # 这个会话临时改模式（不写配置文件 —— 前端改的才是长久的）。
            mode = "silent" if command["action"] == "silent" else "chat"
            self.cfg.setdefault("modes", {})["group" if event.is_group else "dm"] = mode
        elif command["action"] == "say":
            await self._send(event, command["arg"], force_text=True)
        if command["reply"]:
            await self._send(event, command["reply"], force_text=True)
        self.log(f"[{self.label()}] 🛠 管理命令：{event.plain_text().strip()[:40]}")
        return True

    async def _collect_media(self, event: Event, session_id: str) -> tuple[str, list[dict]]:
        """这条消息里的媒体 → 落盘引用 + 送模型的图。返回 (语音转写, 附件列表)。

        单张图 / 表情包失败不中断整条消息 —— 大不了一张图看不见，话还是要回的。
        """
        media_cfg = self.cfg.get("media") or {}
        transcript = ""
        items: list[dict] = []
        images_for_model: list[str] = []
        for seg in event.media():
            if len(items) >= MAX_IMAGES_PER_TURN + 2:
                break
            kind = str(seg.get("type"))
            data = seg.get("data") or {}
            if kind == "face":       # QQ 自带小黄脸：没有文件，只有 id
                continue
            if kind == "record":
                transcript = await self._voice_in(seg, transcript)
                continue
            url = str(data.get("url") or data.get("file") or "")
            if not url:
                continue
            sticker = kind == "mface" or bool(media_cfg.get("treat_small_image_as_sticker", False))
            try:
                saved, local = media.ingest(
                    self.brain, session_id, url,
                    kind="video" if kind == "video" else ("sticker" if sticker else "image"))
            except (ValueError, BrainError) as exc:
                self.log(f"[{self.label()}] ⚠ 一个{kind}没存下来：{exc}")
                continue
            try:
                if kind == "video":
                    frames, _ = media.video_frames(self.brain, url)
                    images_for_model.extend(frames[: int(media_cfg.get("video_frames") or MAX_IMAGES_PER_TURN)])
                    item = {"type": "video", "url": saved.get("orig") or saved.get("thumb", ""),
                            "thumb": saved.get("thumb", "")}
                    if frames:
                        # 视频没有缩略图（save_video 的 thumb 指原文件）：把抽出来的第一帧
                        # 存成一张附件，当前端气泡的封面（库里只存引用，不存 base64）。
                        item["thumb"] = media.upload_data_url(self.brain, session_id, frames[0]) or item["thumb"]
                else:
                    images_for_model.append(media.data_url(local))
                    item = {"type": "sticker" if sticker else "image",
                            "url": saved.get("orig") or saved.get("thumb", ""),
                            "thumb": saved.get("thumb", "")}
                items.append(item)
            except (ValueError, BrainError) as exc:
                self.log(f"[{self.label()}] ⚠ 一个{kind}的封面没保住：{exc}")
            finally:
                if sticker:
                    self._collect_sticker(saved)
                local.unlink(missing_ok=True)
        self._last_images = images_for_model[:MAX_IMAGES_PER_TURN]
        return transcript, items

    async def _voice_in(self, seg: dict, current: str) -> str:
        """一条语音 → 文字。**优先用 QQ 自己的转写**（大老大定的），再退到外部 whisper。

        三级降级，每级都留痕：
        1. 网关已经给了 `record.text`（手机上「转文字」那个）→ 直接用，一分钱不花；
        2. `asr.engine=openai` → 下载语音 → 外部 whisper（音频过上游，要花钱）；
        3. 都没有 → 返回空串，调用方按 `[语音]` 记，**不假装听懂**。
        """
        given = onebot.text_of_record(seg)
        if given:
            self.log(f"[{self.label()}] 🎧 QQ 转写：{given[:40]}")
            return given
        if current:
            return current
        engine = str((self.cfg.get("asr") or {}).get("engine") or "qq")
        if engine != "openai":
            self.log(f"[{self.label()}] ℹ 收到语音，网关没给转写，也没开外部识别 —— 按 [语音] 记下")
            return ""
        url = str((seg.get("data") or {}).get("url") or (seg.get("data") or {}).get("file") or "")
        if not url:
            return ""
        try:
            path = media.download(url, max_bytes=media.MAX_VOICE_BYTES, ext=".amr")
        except ValueError as exc:
            self.log(f"[{self.label()}] ⚠ 语音没下来：{exc}")
            return ""
        try:
            return media.transcribe(self.brain, path, self.cfg) or ""
        finally:
            path.unlink(missing_ok=True)

    def _collect_sticker(self, saved: dict) -> None:
        """学下一个表情包 —— **索引存进大脑的设置里**，本体附件库就是唯一的库。

        为什么不自己存一份（参考项目那样）：前端只认附件管道那套引用，另有副本就会出现
        「删了消息表情还在 / 存储管理算不到」两本账。这里只往库里记一条 URL。
        """
        if not (self.cfg.get("sticker") or {}).get("auto_collect", True):
            return
        url = str(saved.get("orig") or saved.get("thumb") or "")
        if not url:
            return
        library = list((self.cfg.get("sticker") or {}).get("library") or [])
        if url in library:
            return
        library.append(url)
        del library[:-300]        # 最多记 300 个，够用了
        self.bridge.queue_setting("sticker.library", library)
        self.log(f"[{self.label()}] 🎴 收下一个表情包")

    # ---- 回话 ----

    async def _reply(self, event: Event, route, session_id: str, text: str, items: list[dict]) -> None:
        images = self._last_images
        messages = self.history.get(event.session_key)
        who = self._speaker(event)
        body = text or ("（发了点我看不懂的东西）" if not items else "（发了张图 / 一段视频）")
        messages.append({"role": "user", "content": f"{who}：{body}" if event.is_group else body})
        turns = messages[-HISTORY_TURNS:]
        if self._tools_available(event):
            reply = await self._reply_with_tools(route, event, turns, images)
        else:
            reply = await self._reply_plain(route, event, turns, images)
        if not reply:
            return
        self.history.add(event.session_key, "user", f"{who}：{body}" if event.is_group else body)
        self.history.add(event.session_key, "assistant", reply)

        await self._send(event, reply)
        self._sync_outbound(session_id, reply, items)
        self.router.remember(event.session_key, route.session_kind, route.character_id,
                             self.router.persona_name(), reply)
        self.router.flush_memory()

    async def _reply_plain(self, route, event: Event, turns: list[dict], images: list[str]) -> str:
        try:
            result = await asyncio.to_thread(
                self.brain.chat, route.character_id, turns,
                images=images, character_ids=[route.character_id],
                conversation_id=f"qq-{event.session_key}",
            )
        except BrainError as exc:
            self.log(f"[{self.label()}] ⚠ 小糖人没能回话：{exc}")
            return ""
        reply = str(((result.get("message") or {}).get("content")) or "").strip()
        if not reply:
            self.log(f"[{self.label()}] ⚠ 大脑回了空话，这条跳过（可能是上游抽风）")
        return reply

    def _tools_available(self, event: Event) -> bool:
        """这一轮能不能动本地工具：开关 + 有动作 + （默认）只有大老大。"""
        if not tools.enabled(self.cfg) or not tools.allowed_names(self.cfg):
            return False
        if not (self.cfg.get("tools") or {}).get("require_owner", True):
            return True
        owner = str(((self.cfg.get("owner") or {}).get("qq")) or "")
        admin = (self.cfg.get("owner") or {}).get("tools")
        if admin is False:
            return False
        return bool(owner) and event.user_id == owner

    async def _reply_with_tools(self, route, event: Event, turns: list[dict], images: list[str]) -> str:
        """带工具的一轮：**问大脑要决定，在本进程执行，最多来回几圈**。

        循环为什么要在这里转（而不是让大脑转）：执行权必须留在能看见沙箱的这个地方。
        大脑那侧只回答「模型想调 local_read，参数是 …」，一个动作都不碰。
        """
        schema = tools.tool_schema(self.cfg)
        state = {"note": tools.describe_for_model(self.cfg)}
        convo = [dict(m) for m in turns]
        max_turns = int((self.cfg.get("tools") or {}).get("max_turns") or 3)
        last_text = ""
        for _ in range(max(1, min(max_turns, 6))):
            try:
                result = await asyncio.to_thread(
                    self.brain.tool_turn, route.character_id, convo,
                    tools=schema, images=images, state=state,
                    conversation_id=f"qq-{event.session_key}",
                )
            except BrainError as exc:
                self.log(f"[{self.label()}] ⚠ 工具轮失败，改普通回话：{exc}")
                return await self._reply_plain(route, event, turns, images)
            images = []       # 图只在第一轮带一次，后面轮次已经「看过」了
            calls = result.get("tool_calls") or []
            last_text = str(result.get("content") or "").strip() or last_text
            if not calls:
                return last_text
            convo.append({"role": "assistant", "content": result.get("content") or "",
                          "tool_calls": [{"id": c.get("id"), "type": "function",
                                          "function": {"name": c.get("name"), "arguments": c.get("arguments")}}
                                         for c in calls]})
            for call in calls:
                ok, output = tools.run(self.cfg, str(call.get("name")), call.get("arguments") or "{}")
                self.log(f"[{self.label()}] 🧰 {call.get('name')} → {'✓' if ok else '✗'} {output[:60]!r}")
                convo.append({"role": "tool", "tool_call_id": str(call.get("id") or ""),
                              "content": output[:4000]})
        return last_text or "（我想试着看看文件，但绕了几圈没绕明白）"

    async def _send(self, event: Event, reply: str, *, force_text: bool = False) -> None:
        """把回复发回 QQ：文字切条 + （可选）语音条 + （可选）表情包 + 出件箱。"""
        reply_cfg = self.cfg.get("reply") or {}
        voice_cfg = self.cfg.get("voice") or {}
        segment_sets: list[list[dict]] = []
        use_voice = False if force_text else self._should_voice(event, reply)
        if use_voice:
            path = await self._make_voice(reply, voice_cfg)
            if path is not None:
                segments: list[dict] = []
                if event.is_group and reply_cfg.get("mention_sender_in_group", True):
                    segments.append(onebot.seg_at(event.user_id))
                segments.append(onebot.seg_record_file(str(path)))
                segment_sets.append(segments)
        if not use_voice or not segment_sets:
            for chunk in onebot.split_text(reply, int(reply_cfg.get("chunk_chars") or 400)):
                segments = []
                if event.is_group and reply_cfg.get("mention_sender_in_group", True):
                    segments.append(onebot.seg_at(event.user_id))
                    segments.append(onebot.seg_text(" "))
                segments.append(onebot.seg_text(chunk))
                segment_sets.append(segments)
        sticker = self._maybe_sticker(reply)
        if sticker is not None:
            segment_sets.append([onebot.seg_image_file(str(sticker))])
        segment_sets.extend(self._outbox_segments())
        for segments in segment_sets:
            result = await self.client.send(event, segments)
            if result.get("status") == "failed":
                self.log(f"[{self.label()}] ⚠ 发回 QQ 失败：{result.get('message') or result.get('wording')}")
            await asyncio.sleep(0.4)     # 连发几条时给网关一点喘息（也像人在打字）

    def _should_voice(self, event: Event, reply: str) -> bool:
        voice_cfg = self.cfg.get("voice") or {}
        if not voice_cfg.get("enabled", True):
            return False
        auto = str(voice_cfg.get("auto") or "mirror")
        if auto == "off":
            return False
        if auto == "mirror" and not any(s.get("type") == "record" for s in event.segments):
            return False
        if len(reply) > int(voice_cfg.get("max_chars") or 80):
            return False          # 太长了不像语音条，改成打字
        limit = int(voice_cfg.get("max_per_minute") or 3)
        now = time.time()
        while self._voice_times and now - self._voice_times[0] > 60:
            self._voice_times.popleft()
        if len(self._voice_times) >= limit:
            return False
        self._voice_times.append(now)
        return True

    async def _make_voice(self, text: str, voice_cfg: dict) -> Path | None:
        """调大脑的 TTS 合成一条语音 —— 音色由大脑那边定（本项目不装 TTS）。"""
        plain = text.strip()
        try:
            path = await asyncio.to_thread(
                media.speak, self.brain, plain,
                engine=str(voice_cfg.get("engine") or "edge"),
                voice=str(voice_cfg.get("voice") or ""),
            )
        except BrainError as exc:
            self.log(f"[{self.label()}] ⚠ 语音没合成出来，这条改打字：{exc}")
            return None
        self.log(f"[{self.label()}] 🎤 回了一条语音（{path.name}）")
        return path

    def _maybe_sticker(self, reply: str) -> Path | None:
        """要不要来一张表情包。**库在大脑的附件里**，本地只留索引和出件箱里的实物。

        三种来源，谁有用谁：库里的（学来的 / 前端传的）→ 本地 `data/stickers/` 的实物。
        被明确点名（「发表情包」）时一定来一张 —— 大老大喜欢这个，别让它装作没听见。
        """
        sticker_cfg = self.cfg.get("sticker") or {}
        if not sticker_cfg.get("enabled", True):
            return None
        asked = any(word in (reply or "") or word in "" for word in ("表情包", "表情"))
        chance = float(sticker_cfg.get("reply_chance") or 0)
        if not asked and (chance <= 0 or random.random() > chance):
            return None
        if asked:
            # 被点名了：从库里随机挑一张（先挑库，库空再退回本地实物）。
            library = [str(u) for u in (sticker_cfg.get("library") or []) if str(u).strip()]
            if library:
                path = media.local_from_url(random.choice(library))
                if path is not None:
                    return path
        files = [p for p in sticker_dir().glob("*") if p.is_file() and p.suffix.lower() != ".json"]
        return random.choice(files) if files else None

    def _outbox_segments(self) -> list[list[dict]]:
        """出件箱：`data/outbox/` 里放什么就发什么（图片 / 视频 / 任何 QQ 认的文件）。

        这是「小糖人也能发图片视频」的口子 —— 大老大（或别的功能）往那个目录丢文件，
        下一轮回话时带出去，发完移到 `data/outbox/sent/`，不会重复发。
        """
        box = config_mod.STATE_DIR / "outbox"
        box.mkdir(parents=True, exist_ok=True)
        sent_dir = box / "sent"
        out: list[list[dict]] = []
        for path in sorted(p for p in box.iterdir() if p.is_file()):
            kind = "video" if path.suffix.lower() in media.VIDEO_EXT else "image"
            out.append([{"type": kind, "data": {"file": path.resolve().as_posix()}}])
            sent_dir.mkdir(exist_ok=True)
            try:
                shutil.move(str(path), str(sent_dir / path.name))
            except OSError:
                pass
            self.log(f"[{self.label()}] 📤 出件箱发了一个文件：{path.name}")
        return out

    # ---- 通知事件（戳一戳 / 群成员变动 / 撤回）----

    async def on_notice(self, notice: Notice) -> None:
        """通知事件。**默认不回话**（除了戳一戳，那是明确在跟你玩）。"""
        self.cfg = config_mod.load()
        self.router.cfg = self.cfg
        events_cfg = self.cfg.get("events") or {}
        if notice.kind == "poke":
            if not events_cfg.get("poke", True):
                return
            # 只理戳「我」的（有人戳别人不关我事），同一个人 30 秒内只理一次（防连点刷屏）。
            me = self.client.self_id or str((self.cfg.get("onebot") or {}).get("self_id") or "")
            if notice.target_id and me and notice.target_id != me:
                return
            now = time.time()
            if now - self._poke_seen.get(notice.user_id, 0) < 30:
                return
            self._poke_seen[notice.user_id] = now
            if not self.router._groups() and notice.is_group:
                return
            if notice.is_group and notice.group_id not in self.router._groups():
                return
            fake = Event(kind="group" if notice.is_group else "private", user_id=notice.user_id,
                         group_id=notice.group_id, self_id=me, segments=[],
                         raw={"post_type": "message"})
            reply = random.choice(["呀，被你戳到了～", "在的在的，怎么啦？", "（被戳）诶？"])
            self.log(f"[{self.label()}] 👉 被戳了（{notice.user_id}）")
            await self._send(fake, reply, force_text=True)
            await self._sync_inbound("group" if notice.is_group else f"single-{self.router.persona()}",
                                     self._prefix(fake), "（被戳了一下）", [])
            return
        if not events_cfg.get("notice", False):
            return
        # 群成员变动 / 撤回：**只同步一条记录进小窝，不开口**（大老大看得到就行）。
        if notice.is_group and notice.group_id not in self.router._groups():
            return
        self.log(f"[{self.label()}] 📝 {notice.describe()}")
        await self._sync_inbound("group" if notice.is_group else f"single-{self.router.persona()}",
                                 "", notice.describe(), [])

    # ---- 同步进前端 ----

    def _prefix(self, event: Event) -> str:
        sync = self.cfg.get("sync") or {}
        if event.is_group and sync.get("prefix_remote", True):
            return f"{event.display_name}："
        return ""

    def _speaker(self, event: Event) -> str:
        return event.display_name if event.is_group else "大老大"

    def _persona_session(self, session_id: str) -> str:
        cfg = self.cfg.get("sync") or {}
        persona = str(cfg.get("persona_id") or "")
        return f"{persona}@{session_id}" if persona and persona != "dalaoda" else session_id

    async def _sync_inbound(self, session_id: str, prefix: str, text: str, items: list[dict]) -> None:
        """QQ 那边来人说的话 → 写进前端会话（群聊 → group，私聊 → single-<角色>）。"""
        if not (self.cfg.get("sync") or {}).get("to_frontend", True):
            return
        target = self._persona_session(session_id)
        first = items[0] if items else None
        picture = str((first or {}).get("thumb") or (first or {}).get("url") or "")
        kind = "image" if str((first or {}).get("type") or "") in ("image", "sticker") else "text"
        try:
            await asyncio.to_thread(
                self.brain.append_message, target, "user", f"{prefix}{text}".strip(),
                image=picture, image_source="qq" if picture else "", kind=kind)
        except BrainError as exc:
            self.log(f"[{self.label()}] ⚠ 同步进前端没成功（QQ 那边照常聊）：{exc}")

    def _sync_outbound(self, session_id: str, reply: str, items: list[dict]) -> None:
        if not (self.cfg.get("sync") or {}).get("to_frontend", True):
            return
        try:
            self.brain.append_message(self._persona_session(session_id), "assistant", reply, kind="text")
        except BrainError as exc:
            self.log(f"[{self.label()}] ⚠ 同步进前端没成功：{exc}")

    # ---- 定时活 ----

    def tick(self) -> None:
        self.history.save()
        self.router.save()
        self.router.flush_memory()
        self._prune_voice()

    def _prune_voice(self) -> None:
        """清掉一天前发出去的语音临时文件（语音条发完就没用了）。"""
        cutoff = time.time() - 86400
        for path in config_mod.STATE_DIR.glob("tmp/voice-*"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass


class Bridge:
    """整座桥：**一个到多个 QQ 号** + 前端设置同步 + 共同的心跳。"""

    # 小窝前端「这个号演谁」传进来的角色 id（只覆盖**第一个号**，别的号各演各的）。
    persona_override = ""

    def __init__(self, brain: BrainClient | None = None, logger=print):
        self.log = logger
        self.brain = brain or BrainClient()
        self.settings = SettingsSync(self.brain, logger)
        self.cfg = config_mod.load()
        self.accounts: list[AccountRunner] = []
        self._pending_settings: dict = {}      # 桥自己攒的改动（学到的表情包…），ticker 里写回

    # ---- 组装 ----

    def build(self) -> list[AccountRunner]:
        """按配置（含前端设置）造出每个号的运行器。"""
        self.cfg = config_mod.load()
        rows = config_mod.accounts(self.cfg)
        self.accounts = []
        for index, row in enumerate(rows):
            runner = AccountRunner(self, self.cfg, row, index)
            if index == 0 and self.persona_override:
                runner.persona_override = self.persona_override
            self.accounts.append(runner)
        return self.accounts

    def queue_setting(self, key: str, value) -> None:
        """桥自己想把某个设置写回大脑（例如学到的表情包进库）。下一轮 tick 写。"""
        self._pending_settings[key] = value

    async def _flush_settings(self) -> None:
        if not self._pending_settings:
            return
        payload = dict(self._pending_settings)
        self._pending_settings.clear()
        try:
            saved = await asyncio.to_thread(self.brain.save_settings, payload)
        except BrainError as exc:
            self.log(f"⚠ 没能把设置写回大脑：{exc}")
            return
        values = (saved or {}).get("values") or {}
        # 立刻让本地配置跟上（不然刚存的表情包这一轮还看不到）。
        config_mod.set_remote({**config_mod.REMOTE, **values})

    # ---- 起停 ----

    async def run(self) -> None:
        """常驻。Ctrl+C（SIGINT）优雅退出。"""
        self.settings.pull(quiet=True)     # 先拉一次前端设置（拉到就以前端为准）
        self.cfg = config_mod.load()
        self.check_startup()
        self.build()
        loop = asyncio.get_running_loop()
        stop_all = lambda: [r.client.stop() for r in self.accounts]  # noqa: E731
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_all)
            except (NotImplementedError, RuntimeError):   # Windows 上部分场景不支持
                pass
        self.log(f"💬 QQ 桥起来了：{len(self.accounts)} 个号。"
                 f"设置来自{'小窝前端' if config_mod.REMOTE else '本地 config.json'}。")
        for runner in self.accounts:
            runner.refresh_roster()
            await runner.startup_log()
        self.log("   想改白名单 / 演谁 / 语音 / 表情包 → 小窝前端「💬 QQ 桥设置」，改完不用重启。")
        try:
            await asyncio.gather(*(r.client.run_forever() for r in self.accounts), self._ticker())
        except asyncio.CancelledError:
            raise
        finally:
            for runner in self.accounts:
                runner.tick()
            await self._flush_settings()
            self.log("收工，QQ 桥停了。")

    def check_startup(self) -> None:
        """启动时把「没配好」的地方一次说清 —— 比连上半天不理人强。"""
        try:
            provider = self.brain.api_config()
            self.log(f"🧠 大脑配好了：{provider.get('model')} @ {provider.get('base_url')}")
        except BrainError as exc:
            self.log(f"⚠ 大脑那头的模型配置还没好：{exc}")
        asr = str((self.cfg.get("asr") or {}).get("engine") or "qq")
        if asr == "none":
            self.log("ℹ 语音只做「发出」，不做识别（asr.engine=none）—— 收到语音当「[语音]」处理。")
        elif asr == "openai":
            self.log("ℹ 语音识别：先用 QQ 自带的转写；没有时走外部 whisper（会花钱、音频过上游）。")

    async def _ticker(self) -> None:
        """每 30 秒做点慢活：拉前端设置、存历史、攒够的记忆交出去、清过期语音文件。"""
        while True:
            await asyncio.sleep(30)
            before = dict(config_mod.REMOTE)
            self.settings.pull(quiet=True)
            if dict(config_mod.REMOTE) != before:
                # 前端改了设置 → 每个号就地更新（演谁 / 白名单 / 语音…立刻生效）。
                self.cfg = config_mod.load()
                rows = config_mod.accounts(self.cfg)
                for index, runner in enumerate(self.accounts):
                    runner.apply(self.cfg, rows[index] if index < len(rows) else runner.account)
            for runner in self.accounts:
                runner.tick()
            await self._flush_settings()


def sticker_dir() -> Path:
    path = config_mod.STATE_DIR / "stickers"
    path.mkdir(parents=True, exist_ok=True)
    return path