"""小糖人之家「大脑」客户端（标准库 only，无第三方依赖）。

QQ 桥是**外设**：人格、记忆、模型、密钥全在大脑那边，这里一行 key 都不存。
只调通用外设接入层 `/api/nest/*`（契约见本体 `docs/nest-integration.md`）与聊天记录接口。

    GET  /api/nest/features/qq_bridge/api-config   拿合并后的模型配置（含视觉路）
    POST /api/nest/chat                            让指定小糖人说话（可带图 → 走视觉路）
    POST /api/nest/feed                            把「她跟你说的话」也推一份到小窝（可选）
    POST /api/messages                             把 QQ 里的一轮对话同步进前端会话记录
    GET  /api/characters                           角色名册（= 角色介绍里的人，人设唯一来源）

安全约定（抄 DST / B站 适配器的做法）：
- 只允许本机回环 HTTP —— 大脑没有鉴权，绝不能指向公网地址。
- api_key 只在内存里过一下，不落盘、不打日志。
"""

from __future__ import annotations

import base64
import ipaddress
import json
import mimetypes
import uuid
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

FEATURE_ID = "qq_bridge"


class BrainError(RuntimeError):
    """大脑给了明确错误（配置没填、上游 409…），重试也没用的那种。"""


class BrainClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8000", timeout: float = 120.0):
        parsed = urlparse(base_url)
        host = parsed.hostname or ""
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = host == "localhost"
        if parsed.scheme != "http" or not local or parsed.username or parsed.password:
            raise ValueError("大脑目前没有鉴权，只允许本机回环 HTTP 地址")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # ---- 底层 ----

    def _request(self, path: str, payload: dict | None = None, method: str | None = None,
                 raw: bytes | None = None, content_type: str = "application/json",
                 timeout: float | None = None):
        body = raw if raw is not None else (None if payload is None else json.dumps(payload).encode("utf-8"))
        request = Request(
            self.base_url + path, data=body, method=method,
            headers={"Content-Type": content_type},
        )
        try:
            with urlopen(request, timeout=self.timeout if timeout is None else timeout) as response:  # noqa: S310 - 已限制为本机 http
                return json.load(response)
        except OSError as exc:  # HTTPError 也是 OSError
            detail = ""
            if hasattr(exc, "read"):
                try:
                    detail = json.loads(exc.read().decode("utf-8")).get("detail", "")
                except Exception:  # noqa: BLE001
                    detail = ""
            raise BrainError(detail or f"大脑请求失败：{exc}") from exc

    # ---- 契约 ----

    def capabilities(self) -> dict:
        return self._request("/api/nest/capabilities")

    def qq_tools(self, session: dict, actor_id: str, text: str, *, message_ids: list[str] | None = None) -> dict:
        return self._request('/api/qq-bridge/tools', {'session': session,
                             'actor_id': actor_id, 'text': text, 'message_ids': message_ids or []}, timeout=150)

    def api_config(self) -> dict:
        """本功能实际生效的模型配置（全局默认 + 本功能覆盖）。

        api_key 只在内存里传给上游，**不要**写进日志或配置文件。
        """
        cfg = self._request(f"/api/nest/features/{FEATURE_ID}/api-config")
        for field in ("base_url", "api_key", "model"):
            if not cfg.get(field):
                raise BrainError(
                    f"大脑还没给 {FEATURE_ID} 配好上游（缺 {field}）。"
                    "去小窝 → 🧠 小窝大脑 API 里填全局默认，或给这个功能单配一份。"
                )
        return cfg

    def characters(self) -> list[dict]:
        """家里的角色名册（id / name / emoji）—— 就是前端「角色介绍」那些人。

        **人设只有这一个来源**：本项目只传 character_id，人格由大脑组装。
        拉不到就返回空表（调用方回退到本地兜底名单）。
        """
        try:
            return self._request("/api/characters") or []
        except BrainError:
            return []

    def settings(self) -> dict:
        """大老大在**小窝前端**改的那些设置（§9.6.5）—— 值存大脑，这里拉回来覆盖 config.json。

        拉不到（大脑没起 / 这个功能没声明 settings）就返回空表：桥照常用自己的 config.json。
        这是**故意的单向依赖**：没有大脑时桥照样能跑，只是少了一份前端配置。
        """
        try:
            got = self._request(f"/api/nest/features/{FEATURE_ID}/settings") or {}
        except BrainError:
            return {}
        # Newer brains distinguish saved/runtime overrides from schema defaults.
        # Fall back to values for compatibility with an older brain.
        values = got.get("overrides", got.get("values"))
        return values if isinstance(values, dict) else {}

    def save_settings(self, values: dict) -> dict:
        """合并后写回设置；读取失败时不写，避免清空其他前端配置。"""
        path = f"/api/nest/features/{FEATURE_ID}/settings"
        current = self._request(path)
        if not isinstance(current, dict) or not isinstance(current.get("values"), dict):
            raise BrainError("大脑返回的设置格式不正确，取消写回")
        return self._request(path, {"values": {**current["values"], **values}}, method="PUT")

    def group_session(self, persona_id: str = "") -> dict:
        return self._request("/api/qq-bridge/group-session?" + urlencode({"persona_id": persona_id}))

    def qq_context(self, selected: str, remote_group: bool = False, persona_id: str = "") -> dict:
        return self._request("/api/qq-bridge/context?" + urlencode({
            "selected": selected, "remote_group": str(remote_group).lower(), "persona_id": persona_id}))

    def qq_proactive(self, payload: dict) -> dict:
        # Planning (55s), bounded agent (60s), snapshot and cleanup can share
        # one response. This changes the HTTP wait, never the sandbox budget.
        return self._request('/api/qq-bridge/proactive', payload, timeout=max(self.timeout, 150.0))

    def qq_account_context(self, selected: str, remote_group: bool, persona_id: str, account: int) -> dict:
        return self._request('/api/qq-bridge/context?' + urlencode({
            'selected': selected, 'remote_group': str(remote_group).lower(), 'persona_id': persona_id, 'account': account}))

    def qq_exploration(self) -> dict:
        return self._request('/api/qq-bridge/exploration')

    def exploration_claim(self, payload: dict) -> dict:
        return self._request('/api/qq-bridge/exploration-outbox/claim', payload)

    def sandbox_signal(self, payload: dict) -> dict:
        return self._request('/api/qq-bridge/sandbox/signal', payload)

    def exploration_ack(self, run_id: str, sent: bool) -> dict:
        return self._request('/api/qq-bridge/exploration-outbox/ack', {'run_id': run_id, 'sent': sent})

    def qq_exploration_stop(self) -> dict:
        return self._request('/api/qq-bridge/exploration/stop', {})

    def activity_event(self, payload: dict) -> dict:
        return self._request('/api/memory/events', payload)

    def scout_directive(self) -> dict:
        return self._request('/api/nest/features/bilibili_scout/directive') or {}

    def qq_inbound(self, payload: dict) -> dict:
        return self._request("/api/qq-bridge/inbound", payload)

    def qq_web_sync(self, payload: dict) -> dict:
        return self._request('/api/qq-bridge/web-sync', payload)

    def qq_reply(self, payload: dict) -> dict:
        # 后端整轮硬期限360秒，额外留30秒回传错误；不改变普通本机API超时。
        return self._request("/api/qq-bridge/reply", payload, timeout=max(self.timeout, 390.0))

    async def qq_reply_stream(self, payload: dict):
        """Read local NDJSON one complete event at a time; never fall back/retry."""
        import asyncio
        request = Request(self.base_url + '/api/qq-bridge/reply-stream',
                          data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'})
        response = None
        try:
            response = await asyncio.to_thread(urlopen, request, timeout=45)
            while True:
                line = await asyncio.to_thread(response.readline)
                if not line:
                    break
                item = json.loads(line)
                if item.get('type') == 'error':
                    raise BrainError(str(item.get('message') or '流式回复未完成'))
                yield item
        except (OSError, ValueError) as exc:
            raise BrainError('本地流式连接未完成：' + type(exc).__name__ + '；不自动重试') from exc
        finally:
            if response is not None:
                try:
                    await asyncio.wait_for(asyncio.to_thread(response.close), 2)
                except TimeoutError:
                    pass  # A blocked reader finishes under its own 45-second socket timeout.

    def qq_delivered(self, payload: dict) -> dict:
        return self._request("/api/qq-bridge/delivered", payload)

    def qq_finish(self, session: dict, auto_memory: bool = True) -> dict:
        return self._request("/api/qq-bridge/finish", {"session": session, "auto_memory": auto_memory})

    def qq_lease(self, session: dict) -> dict:
        return self._request('/api/qq-bridge/lease', session)

    def qq_describe_stickers(self) -> dict:
        return self._request("/api/qq-bridge/stickers/describe", {})

    def group_turn(self, session: dict, text: str, request_id: str, *,
                   images: list[str] | None = None, attachment: str = "", auto_memory: bool = True) -> dict:
        """普通群聊的另一入口：同一历史、提示词、身份与每日记忆流程。"""
        return self._request("/api/chat/session/turn", {
            "feature_id": FEATURE_ID, "character_id": session["character_ids"][0],
            "mode": "group", "session_id": session["session_id"],
            "persona_id": session["persona_id"], "text": text,
            "request_id": request_id, "images": images or [], "attachment": attachment, "auto_memory": auto_memory,
        })

    def chat(
        self,
        character_id: str,
        messages: list[dict],
        *,
        character_ids: list[str] | None = None,
        images: list[str] | None = None,
        state: dict | None = None,
        conversation_id: str = "",
        max_tokens: int = 1024,
        memory_recall: bool = False,
    ) -> dict:
        """让一位（或多位）小糖人说话。messages 是 [{"role","content"}]。

        images 是 **data URL** 列表（≤8 张，如 `data:image/png;base64,…`）；
        传了就走该功能的视觉路（qq_bridge 在注册表里声明了 needs_vision=True）。
        裸 base64 会被 vsllm 这类中转打成 400 —— 必须带 `data:` 前缀。

        memory_recall=True 时大脑会像普通聊天一样召回长期记忆（家记忆 + 在场角色个人记忆）。
        QQ 桥默认开着：和群聊 / 私聊是同一个大脑，不该换个入口就失忆。
        召回的是日常聊天那一桶记忆；`memory.isolate` 只影响写入（提炼进哪个命名空间）。
        """
        payload = {
            "feature_id": FEATURE_ID,
            "character_id": character_id,
            "conversation_id": conversation_id,
            "messages": messages,
            "state": state or {},
            "stream": False,
            "max_tokens": max_tokens,
        }
        if character_ids:
            payload["character_ids"] = list(character_ids)
        if images:
            payload["images"] = list(images)
        if memory_recall:
            payload["memory_recall"] = True
        return self._request("/api/nest/chat", payload)

    # ---- 带工具的一轮（§9.6.6）----

    def tool_turn(
        self,
        character_id: str,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        images: list[str] | None = None,
        state: dict | None = None,
        conversation_id: str = "",
        max_tokens: int = 1024,
    ) -> dict:
        """问一次模型（带工具声明），拿回它的决定 —— **大脑不执行任何工具**。

        返回 `{content, tool_calls: [{id, name, arguments}], finish_reason}`。
        `arguments` 是模型给的**原始 JSON 字符串**，解析 + 白名单 + 沙箱都在本项目这边
        （`tools.py`）。也就是说：大脑只是个「会说工具话」的模型客户端，
        真正的动作权限从来没离开过这台机器上的这个进程。
        """
        payload = {
            "feature_id": FEATURE_ID,
            "character_id": character_id,
            "conversation_id": conversation_id,
            "messages": messages,
            "state": state or {},
            "tools": list(tools or []),
            "max_tokens": max_tokens,
        }
        if images:
            payload["images"] = list(images)
        return self._request("/api/nest/tool-turn", payload)

    # ---- 同步进前端的会话记录（§15 的 /api/messages）----

    def append_message(self, session_id: str, role: str, content: str = "", *,
                       image: str = "", image_source: str = "", kind: str = "text",
                       segments: list[dict] | None = None) -> dict:
        """往某条前端会话里追一条消息（QQ 里聊的也要在小窝里看得见）。

        `session_id` 用前端那套命名：群聊 = `group`，私聊 = `single-<角色id>`。
        """
        return self._request("/api/messages", {
            "session_id": session_id, "role": role, "content": content,
            "image": image, "image_source": image_source, "kind": kind,
            "segments": segments or [],
        })

    def upload_attachment(self, session_id: str, path: str | Path, source: str = "upload") -> dict:
        """本地媒体 → 落盘成前端能显示的引用（图片带缩略图，视频原样存）。

        走的是 QQ 桥自己那套 `/api/qq-bridge/attachments`（本体里新加的路由），
        它复用聊天的附件管道，但**按内容签名去重**：同一个表情包反复发只存一份。
        返回 `{"url": …, "thumb": …, "kind": "image"|"video"|"sticker", …}`。
        """
        blob = Path(path).read_bytes()
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        return self._request("/api/qq-bridge/attachments", {
            "session_id": session_id, "mime": mime, "source": source,
            "filename": Path(path).name,
            "data": base64.b64encode(blob).decode("ascii"),
        })

    def push_feed(self, *, title: str, kind: str = "note", url: str = "",
                  summary: str = "", cover: str = "", payload: dict | None = None) -> dict:
        """（可选）把 QQ 里的事也推一份到小窝动态流。默认流程不用 —— 见 README。"""
        return self._request("/api/nest/feed", {
            "feature_id": FEATURE_ID, "kind": kind, "title": title,
            "url": url, "summary": summary, "cover": cover, "payload": payload or {},
        })

    def feedback(self, status: str = "saved") -> list[dict]:
        """读回大老大的反馈（默认读「收藏」的）—— 本项目唯一的偏好信号来源之一。"""
        q = urlencode({"feature_id": FEATURE_ID, "status": status, "limit": 100})
        return self._request(f"/api/nest/feed?{q}").get("items", [])

    def extract_memories(self, text: str, *, character_ids: list[str],
                         session_kind: str = "group", preview: bool = False) -> dict:
        """提炼记忆。

        ⚠ 和别的功能不同：QQ 桥**默认不传 feature_id** —— 大老大批注「QQ 里聊的
        应该能同步进群聊 / 私聊记忆」，所以让这些记忆落成普通记忆（`game_id=''`），
        日常闲聊里也记得。要隔离时把 `isolate=True` 传进来（见 memory.py）。
        """
        payload = {"text": text, "session_kind": session_kind,
                   "character_ids": list(character_ids), "preview": preview}
        if self.isolate_memory:
            payload["feature_id"] = FEATURE_ID
        return self._request("/api/memories/extract", payload)

    # ---- 语音（出）----

    def tts_bytes(self, text: str, *, engine: str = "edge", voice: str = "") -> tuple[bytes, str]:
        """让小糖人「出声」：拿大脑的 TTS 合成一段音频。返回 (字节, 扩展名)。

        走的是本体已经做好的 `/api/voice/tts`（edge 晓晓 / piper 华燕·小雅·超文），
        本项目**不自己装 TTS** —— 音色偏好是用户在大脑那边定的，桥不该另有一套。
        出口格式：edge → mp3，piper → wav。SILK 编码交给 QQ 网关做。
        """
        body = json.dumps({"text": text, "engine": engine, "voice": voice}).encode("utf-8")
        request = Request(self.base_url + "/api/voice/tts", data=body, method="POST",
                          headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - 已限制为本机 http
                audio = response.read()
                mime = response.headers.get("Content-Type", "") or ""
        except OSError as exc:
            raise BrainError(f"语音合成失败：{exc}") from exc
        if not audio:
            raise BrainError("语音合成结果为空（引擎没装好？去大脑的语音设置里看一眼）")
        if "wav" in mime or engine == "piper":
            return audio, "wav"
        return audio, "mp3"

    # ---- 语音（入）：语音转文字 ----

    def transcribe_local(self, path: str | Path) -> str:
        result = self._request('/api/media/audio/transcribe-local', method='POST',
                               raw=Path(path).read_bytes(), content_type='application/octet-stream',
                               timeout=180)
        return str(result.get('text') or '').strip()

    def transcribe(self, path: str | Path, *, base_url: str = "", api_key: str = "",
                   model: str = "whisper-1", language: str = "zh") -> str:
        """把一段语音转成文字（OpenAI 兼容 `/audio/transcriptions`，multipart 手搓）。

        **本地没有 ASR**：本体的语音模块只有 TTS（说），没有识别（听）——
        DST 那边的语音也是同一个道理。参考项目靠的是 **QQ 自己的语音转文字**，
        而自己发的语音往往拿不到转写（retcode=100）。所以这里接的是外部 ASR：
        不填 base_url 就用大脑给本功能配的那个中转（vsllm 这类通常也带 whisper）。

        ⚠ 音频字节只在本函数里过一遍，不落盘、不写日志。
        """
        data = Path(path).read_bytes()
        if not data:
            raise BrainError("语音是空的")
        boundary = "----qqbridge" + uuid.uuid4().hex
        parts: list[bytes] = []
        for name, value in (("model", model), ("language", language), ("response_format", "json")):
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{Path(path).name}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n".encode())
        parts.append(data)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(parts)
        request = Request(
            (base_url or self.base_url).rstrip("/") + "/audio/transcriptions", data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                     **({"Authorization": f"Bearer {api_key}"} if api_key else {})},
        )
        try:
            with urlopen(request, timeout=max(self.timeout, 180.0)) as response:  # noqa: S310 - 地址由用户在本地配置
                parsed = json.loads(response.read().decode("utf-8") or "{}")
        except OSError as exc:
            raise BrainError(f"语音识别失败：{exc}") from exc
        if isinstance(parsed, dict):
            return str(parsed.get("text") or "").strip()
        return ""

    # 是否把 QQ 记忆隔离进 qq_bridge 命名空间（默认 False = 汇入普通记忆，见 README）。
    isolate_memory = False
