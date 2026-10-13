"""QQ 桥的主循环：**一条消息进来 → 小糖人回话 → 两边都留痕**。

一句话说清它干的活：

    QQ（OneBot 网关）
      │  ① 收到 @我 / 私聊我的消息（白名单之外一律不理）
      ▼
    桥（本项目）  ──② 图片/表情包/视频/语音 → 下载字节 → 交给大脑落盘、抽帧、识别
      │           ──③ 带上「刚才聊过什么」让大脑里的这位小糖人说话（人格/记忆/密钥都在大脑）
      ▼
    QQ  ◀──④ 回话（文字 / 语音条 / 表情包，长文自动切条）
    前端聊天记录 ◀──⑤ 同一轮对话也写进小窝：群聊 → 群聊会话，私聊 → 对应私聊

**边界**（和参考项目最重要的区别）：
- 本项目**不装 TTS、不落盘媒体、不自己抽帧** —— 全用大脑已经做好的那套（`/api/voice/tts`、
  附件管道、`/api/media/video/analyze`）。少一套实现就少一处会和前端不一致。
- 本项目**不做 ASR**（本地没有识别引擎）。voice 进来时先看有没有现成的转写（QQ 自己给的，
  少数网关会带），没有就按 `asr.engine` 走外部 whisper；engine=none 时明确当「[语音]」处理，
  不假装听懂。
- 群里默认**要 @ 才回**；语音默认**只在对方先发语音时才回语音**（`voice.auto=mirror`）。
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
from . import media, onebot
from .brain import BrainClient, BrainError
from .onebot import Event, OneBotClient
from .sessions import Router

# 一次最多送几张图给模型（token 安全线，和本体的 8 张抽帧同一档）。
MAX_IMAGES_PER_TURN = 4
# 群聊里聊过多少条之后，前面的话就不带了（省 token，也不至于忘得太快）。
HISTORY_TURNS = 16


class History:
    """每个 QQ 会话各记一段「刚才聊了什么」——大脑不存历史，得我们带过去。

    落 `data/conversations.json`：进程重启后小糖人不会突然失忆（重启前聊的还认得）。
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

    def save(self) -> None:
        try:
            self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass


class Bridge:
    # 小窝前端可以指定「这个号演谁」（一个账号 = 一个小糖人）—— 覆盖 config.json 里那一项，
    # 但**只覆盖这一次运行**，不写回配置文件（下次点「自检」看到的还是你配的那个）。
    persona_override = ""

    def __init__(self, brain: BrainClient | None = None, logger=print):
        self.cfg = config_mod.load()
        self.brain = brain or BrainClient()
        self.log = logger
        self.router = Router(self.cfg, self.brain, logger)
        self.router.override = self.persona_override
        self.history = History(config_mod.state_file("conversations.json"))
        self.client = OneBotClient(self.cfg, self.handle, logger)
        self._voice_times: deque[float] = deque()
        self._outbox_sent: set[str] = set()

    # ---- 起停 ----

    async def run(self) -> None:
        """常驻。Ctrl+C（SIGINT）优雅退出。"""
        self.cfg = config_mod.load()
        self.router.cfg = self.cfg
        self.check_startup()
        self.router.refresh_roster()
        who = self.router.persona_name()
        self.client.mode = self.client_mode()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.client.stop)
            except (NotImplementedError, RuntimeError):   # Windows 上部分场景不支持
                pass
        self.log(f"💬 QQ 桥起来了。这个号演的是「{who}」（{self.router.persona()}）")
        self.log("   想改白名单 / 语音 / 别演谁 → 改 features/qq-bridge/config.json，不用重启。")
        try:
            await asyncio.gather(self.client.run_forever(), self._ticker())
        except asyncio.CancelledError:
            raise
        finally:
            self.router.save()
            self.history.save()
            self.log("收工，QQ 桥停了。")

    def client_mode(self) -> str:
        return str((self.cfg.get("onebot") or {}).get("mode") or "server")

    async def _ticker(self) -> None:
        """每 30 秒做点慢活：存历史、攒够的记忆交出去、清过期语音文件。"""
        while True:
            await asyncio.sleep(30)
            self.history.save()
            self.router.save()
            self.router.flush_memory()
            self._prune_voice()

    def check_startup(self) -> None:
        """启动时把「没配好」的地方一次说清 —— 比连上半天不理人强。"""
        allow = self.cfg.get("allow") or {}
        ob = self.cfg.get("onebot") or {}
        if not (allow.get("groups") or allow.get("private")):
            self.log("⚠ allow.groups / allow.private 都是空的 —— **谁都不理**。")
            self.log("  要让它开口，把群号 / QQ 号填进 config.json（模板见 config.example.json）。")
        if not (self.cfg.get("persona") or {}).get("character_id"):
            pass   # persona() 会自己提醒一次
        if self.client_mode() == "server":
            self.log(f"   反向 WS 等在这里：ws://{ob.get('listen_host', '127.0.0.1')}:"
                     f"{ob.get('listen_port', 3001)}{ob.get('listen_path', '/onebot/v11/ws')}")
        try:
            provider = self.brain.api_config()
            self.log(f"🧠 大脑配好了：{provider.get('model')} @ {provider.get('base_url')}")
        except BrainError as exc:
            self.log(f"⚠ 大脑那头的模型配置还没好：{exc}")
        asr = self.cfg.get("asr") or {}
        if str(asr.get("engine") or "none") == "none":
            self.log("ℹ 语音只做「发出」，不做识别（asr.engine=none）—— 收到语音会当「[语音]」处理。"
                     "想让小糖人听懂，把 asr.engine 改成 openai 并填好识别模型（见 config.example.json）。")

    # ---- 一条消息 ----

    async def handle(self, event: Event) -> None:
        """收到消息。**先判断该不该动**，再决定同步 / 回话。"""
        self.cfg = config_mod.load()          # 改配置不用重启
        self.router.cfg = self.cfg
        route = self.router.route(event)
        if not route.sync and not route.reply:
            self.log(f"· 略过 {event.session_key}（{route.reason}）")
            return

        session_id = route.frontend_session
        text = event.plain_text()
        # 语音先转写：转不出字就只剩「[语音]」，也不装懂。
        voice_text, media_items = await self._collect_media(event, session_id)
        if voice_text:
            text = (text.replace("[语音]", f"[语音] {voice_text}") if text else f"[语音] {voice_text}")

        prefix = self._prefix(event)
        self.log(f"← {event.session_key} {event.display_name}：{(text or '（只有媒体）')[:40]}")
        self._sync_inbound(session_id, prefix, text, media_items)
        self.router.remember(event.session_key, route.session_kind, route.character_id,
                             self._speaker(event), text or "[发了点东西]")
        if not route.reply:
            return
        await self._reply(event, route, session_id, text, media_items)

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
            url = str(data.get("url") or data.get("file") or "")
            if not url:
                continue
            if kind == "record":
                # ① 网关要是顺手给了转写，直接用（参考项目就是这么干的）。
                given = str(data.get("text") or "").strip()
                if given:
                    transcript = given
                    continue
                # ② 没给 → 走外部 ASR（engine=none 时返回空，不硬猜）。
                try:
                    path = media.download(url, max_bytes=media.MAX_VOICE_BYTES, ext=".amr")
                except ValueError as exc:
                    self.log(f"⚠ 语音没下来：{exc}")
                    continue
                try:
                    transcript = media.transcribe(self.brain, path, self.cfg) or transcript
                    if not transcript:
                        self.log("· 收到一条语音，但没转出字（asr 没配？）—— 按 [语音] 记下")
                finally:
                    path.unlink(missing_ok=True)
                continue
            sticker = kind == "mface" or bool(media_cfg.get("treat_small_image_as_sticker", False))
            try:
                saved, local = media.ingest(
                    self.brain, session_id, url,
                    kind="video" if kind == "video" else ("sticker" if sticker else "image"))
            except (ValueError, BrainError) as exc:
                self.log(f"⚠ 一个{kind}没存下来：{exc}")
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
                self.log(f"⚠ 一个{kind}的封面没保住：{exc}")
            finally:
                if sticker and (self.cfg.get("sticker") or {}).get("auto_collect", True):
                    self._collect_sticker(local)      # 先学下来，再删临时文件
                local.unlink(missing_ok=True)
        self._last_images = images_for_model[:MAX_IMAGES_PER_TURN]
        return transcript, items

    def _collect_sticker(self, src: Path) -> None:
        """学下一个表情包（本地库，不外传）。内容相同就不重复存。

        这也是「小糖人自己攒表情包」的来源 —— 想直接给它一批，把文件丢进
        `data/stickers/` 就行，文件名叫什么都无所谓（回话时按 chance 随机挑一张）。
        """
        try:
            digest = hashlib.sha1(src.read_bytes()).hexdigest()[:12]
            suffix = src.suffix.lower()
            if suffix not in {".gif", ".png", ".jpg", ".jpeg", ".webp"}:
                suffix = ".gif"
            target = sticker_dir() / f"{digest}{suffix}"
            if not target.exists():
                shutil.copy2(src, target)
                self.log(f"🎴 收下一个表情包（{target.name}）")
        except OSError:
            pass

    # ---- 回话 ----

    async def _reply(self, event: Event, route, session_id: str, text: str, items: list[dict]) -> None:
        images = getattr(self, "_last_images", [])
        messages = self.history.get(event.session_key)
        who = self._speaker(event)
        body = text or ("（发了点我看不懂的东西）" if not items else "（发了张图 / 一段视频）")
        messages.append({"role": "user", "content": f"{who}：{body}" if event.is_group else body})
        try:
            result = self.brain.chat(route.character_id, messages[-HISTORY_TURNS:],
                                     images=images, character_ids=[route.character_id],
                                     conversation_id=f"qq-{event.session_key}")
        except BrainError as exc:
            self.log(f"⚠ 小糖人没能回话：{exc}")
            return
        reply = str(((result.get("message") or {}).get("content")) or "").strip()
        if not reply:
            self.log("⚠ 大脑回了空话，这条跳过（可能是上游抽风）")
            return
        self.history.add(event.session_key, "user", f"{who}：{body}" if event.is_group else body)
        self.history.add(event.session_key, "assistant", reply)

        await self._send(event, reply)
        self._sync_outbound(session_id, reply, items)
        self.router.remember(event.session_key, route.session_kind, route.character_id,
                             self.router.persona_name(), reply)
        self.router.flush_memory()

    async def _send(self, event: Event, reply: str) -> None:
        """把回复发回 QQ：文字切条 + （可选）语音条 + （可选）表情包 + 出件箱。"""
        reply_cfg = self.cfg.get("reply") or {}
        voice_cfg = self.cfg.get("voice") or {}
        segment_sets: list[list[dict]] = []
        use_voice = self._should_voice(event, reply)
        if use_voice:
            path = await self._make_voice(reply, voice_cfg)
            if path is not None:
                segments: list[dict] = []
                if event.is_group and reply_cfg.get("mention_sender_in_group", True):
                    segments.append({"type": "at", "data": {"qq": event.user_id}})
                segments.append(onebot.seg_record_file(str(path)))
                # 语音后面不再跟整段文字（像真人：说了就不重复打一遍），只留一句短的。
                segment_sets.append(segments)
        if not use_voice or not segment_sets:
            for chunk in onebot.split_text(reply, int(reply_cfg.get("chunk_chars") or 400)):
                segments = []
                if event.is_group and reply_cfg.get("mention_sender_in_group", True):
                    segments.append({"type": "at", "data": {"qq": event.user_id}})
                    segments.append(onebot.seg_text(" "))
                segments.append(onebot.seg_text(chunk))
                segment_sets.append(segments)
        sticker = self._maybe_sticker()
        if sticker is not None:
            segment_sets.append([onebot.seg_image_file(str(sticker))])
        segment_sets.extend(self._outbox_segments())
        for segments in segment_sets:
            result = await self.client.send(event, segments)
            if result.get("retcode") not in (0, None) and result.get("status") == "failed":
                self.log(f"⚠ 发回 QQ 失败：{result.get('message') or result.get('wording')}")
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
            self.log(f"⚠ 语音没合成出来，这条改打字：{exc}")
            return None
        self.log(f"🎤 回了一条语音（{path.name}）")
        return path

    def _maybe_sticker(self) -> Path | None:
        sticker_cfg = self.cfg.get("sticker") or {}
        chance = float(sticker_cfg.get("reply_chance") or 0)
        if not sticker_cfg.get("enabled", True) or chance <= 0:
            return None       # 默认 0：不主动发表情包（免得像刷屏机器人）
        if random.random() > chance:
            return None
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
            self.log(f"📤 出件箱发了一个文件：{path.name}")
        return out

    def _prune_voice(self) -> None:
        """清掉一天前发出去的语音临时文件（语音条发完就没用了）。"""
        cutoff = time.time() - 86400
        for path in config_mod.STATE_DIR.glob("tmp/voice-*"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass

    # ---- 同步进前端 ----

    def _prefix(self, event: Event) -> str:
        sync = self.cfg.get("sync") or {}
        if event.is_group and sync.get("prefix_remote", True):
            return f"{event.display_name}："
        return ""

    def _speaker(self, event: Event) -> str:
        return event.display_name if event.is_group else "大老大"

    def _sync_inbound(self, session_id: str, prefix: str, text: str, items: list[dict]) -> None:
        """QQ 那边来人说的话 → 写进前端会话（群聊 → group，私聊 → single-<角色>）。"""
        if not (self.cfg.get("sync") or {}).get("to_frontend", True):
            return
        cfg = self.cfg.get("sync") or {}
        persona = str(cfg.get("persona_id") or "")
        target = f"{persona}@{session_id}" if persona and persona != "dalaoda" else session_id
        first = items[0] if items else None
        picture = str((first or {}).get("thumb") or (first or {}).get("url") or "")
        try:
            self.brain.append_message(
                target, "user", f"{prefix}{text}".strip(),
                image=picture, image_source="qq" if picture else "",
                kind="text",
            )
        except BrainError as exc:
            self.log(f"⚠ 同步进前端没成功（QQ 那边照常聊）：{exc}")

    def _sync_outbound(self, session_id: str, reply: str, items: list[dict]) -> None:
        if not (self.cfg.get("sync") or {}).get("to_frontend", True):
            return
        cfg = self.cfg.get("sync") or {}
        persona = str(cfg.get("persona_id") or "")
        target = f"{persona}@{session_id}" if persona and persona != "dalaoda" else session_id
        try:
            self.brain.append_message(target, "assistant", reply, kind="text")
        except BrainError as exc:
            self.log(f"⚠ 同步进前端没成功：{exc}")


def sticker_dir() -> Path:
    path = config_mod.STATE_DIR / "stickers"
    path.mkdir(parents=True, exist_ok=True)
    return path