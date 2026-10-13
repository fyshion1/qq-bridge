"""OneBot v11 接入层：**只做协议，不做业务**。

QQ 那边由网关负责（参考项目用的是 SnowLuma / Lagrange 系，NapCat 同理）——
网关把 QQ 的收发翻译成 OneBot v11 的**事件**和**动作**，我们两头都不碰协议细节：

    网关 ──事件──▶ 我们（reverse WS 服务端，网关主动连过来）
    网关 ◀─动作── 我们（send_private_msg / send_group_msg / get_record …）

两种连法都支持，因为各家网关的叫法不一样：
- `mode="server"`（默认，**反向 WS**）：我们监听一个端口，网关配「反向 WS 地址」指过来。
  好处：不用管网关自己的 HTTP 端口，也不需要在网关里配我们的地址之外的任何东西。
- `mode="client"`（正向 WS）：我们主动连网关的 `ws://…`。

**媒体不自己下**：图 / 视频 / 语音的 URL 由网关给（`image.url`、`video.url`、`record.url`），
我们只负责下载字节 → 交给大脑落盘 → 拿回前端能显示的引用（见 media.py）。
这样「谁在存文件」始终只有一个答案：**大脑的附件管道**。

动作一律走 `_call(action, params)`，返回 `retcode`；失败不抛异常，只记账 —— 一条动作失败
不该把整条消息流打断。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from aiohttp import WSMsgType, web

Handler = Callable[["Event"], Awaitable[None]]


class OneBotError(RuntimeError):
    pass


# ---- 事件 ----
# 只把我们要用的字段规整出来，其余原样留在 raw 里（排查问题时看 raw 就够）。

@dataclass
class Event:
    """一条规整后的消息事件。"""

    kind: str                      # private | group
    user_id: str
    group_id: str = ""
    message_id: str = ""
    nickname: str = ""
    card: str = ""                 # 群名片（群里显示的是这个）
    role: str = ""                 # owner / admin / member
    self_id: str = ""
    segments: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def is_group(self) -> bool:
        return self.kind == "group"

    @property
    def session_key(self) -> str:
        """QQ 侧的会话标识：群 = `g:<群号>`，私聊 = `p:<QQ号>`。"""
        return f"{'g' if self.is_group else 'p'}:{self.group_id or self.user_id}"

    @property
    def display_name(self) -> str:
        return self.card or self.nickname or self.user_id

    def plain_text(self) -> str:
        """把消息段拼成纯文本：文字照抄，@/表情/图片给一个占位符（模型也需要「看到了什么」）。"""
        out: list[str] = []
        for seg in self.segments:
            t = seg.get("type")
            data = seg.get("data") or {}
            if t == "text":
                out.append(str(data.get("text", "")))
            elif t == "at":
                out.append("@某人" if str(data.get("qq")) != self.self_id else "@我")
            elif t == "image":
                out.append("[图片]")
            elif t == "face":
                out.append("[表情]")
            elif t == "mface":
                out.append("[表情包]")
            elif t == "record":
                out.append("[语音]")
            elif t == "video":
                out.append("[视频]")
            elif t == "reply":
                out.append("")   # 引用块本身不写进正文
            elif t == "forward":
                out.append("[合并转发]")
        return "".join(out).strip()

    def mentions_me(self) -> bool:
        for seg in self.segments:
            if seg.get("type") == "at" and str((seg.get("data") or {}).get("qq")) == self.self_id:
                return True
        # 有些网关会把 @ 只写进 raw_message（CQ 码），兜一层。
        return bool(self.self_id) and f"[CQ:at,qq={self.self_id}]" in str(self.raw.get("raw_message", ""))

    def media(self) -> list[dict]:
        """这条消息里的媒体段（图片 / 表情包 / 语音 / 视频），按出现顺序。"""
        keep = {"image", "mface", "record", "video", "face"}
        return [s for s in self.segments if s.get("type") in keep]


def parse_event(payload: dict, self_id: str = "") -> Event | None:
    """OneBot 事件 JSON → Event；不是消息事件就返回 None。

    只在 `post_type == "message"` 时理人。通知 / 请求（加好友、群邀请）以后要接
    再加分支 —— 现在明确不处理，免得莫名其妙自己动起来。
    """
    if (payload or {}).get("post_type") != "message":
        return None
    kind = str(payload.get("message_type") or "")
    if kind not in ("private", "group"):
        return None
    sender = payload.get("sender") or {}
    message = payload.get("message")
    if isinstance(message, str):          # 少数网关只给 CQ 码字符串
        segments = _parse_cq(message)
    elif isinstance(message, list):
        segments = [s for s in message if isinstance(s, dict)]
    else:
        segments = []
    return Event(
        kind=kind,
        user_id=str(payload.get("user_id") or ""),
        group_id=str(payload.get("group_id") or ""),
        message_id=str(payload.get("message_id") or ""),
        nickname=str(sender.get("nickname") or ""),
        card=str(sender.get("card") or ""),
        role=str(sender.get("role") or ""),
        self_id=str(payload.get("self_id") or self_id or ""),
        segments=segments,
        raw=payload,
    )


def _parse_cq(text: str) -> list[dict]:
    """CQ 码字符串 → 消息段（只认我们关心的那几种，够用了）。"""
    import re

    segs: list[dict] = []
    pos = 0
    for match in re.finditer(r"\[CQ:([a-z_]+)((?:,[^\]]*)?)\]", text):
        if match.start() > pos:
            segs.append({"type": "text", "data": {"text": text[pos:match.start()]}})
        name = match.group(1)
        data: dict[str, str] = {}
        for pair in match.group(2).lstrip(",").split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                data[k.strip()] = v.strip()
        segs.append({"type": name, "data": data})
        pos = match.end()
    if pos < len(text):
        segs.append({"type": "text", "data": {"text": text[pos:]}})
    return segs


# ---- 消息段构造（发出去的那一头）----

def seg_text(text: str) -> dict:
    return {"type": "text", "data": {"text": text}}


def seg_image_file(path: str) -> dict:
    """本地图片按 `file://` 发（网关自己读文件，我们不必先上传到哪里）。

    藏着的坑：路径必须是**绝对路径**且用正斜杠，Windows 的反斜杠会被网关当成转义。
    """
    from pathlib import Path

    return {"type": "image", "data": {"file": Path(path).resolve().as_posix()}}


def seg_record_file(path: str) -> dict:
    """语音条。`record` 的编码由**网关**负责（Lagrange/NapCat 会把 wav/mp3/amr 转 SILK），
    所以这里只管给一个绝对路径 —— 我们不做 SILK 编码，也不该做。"""
    from pathlib import Path

    return {"type": "record", "data": {"file": Path(path).resolve().as_posix()}}


def split_text(text: str, limit: int = 400) -> list[str]:
    """超长回复切成几条发（QQ 单条正文有上限，且长消息看起来像刷屏）。

    优先在句号 / 换行 / 逗号处断开（读起来像人分几条说），找不到就近硬切。
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        # 只在前半段里找断点：断点太靠前会让上一条短得可笑，不如硬切。
        cut = max(rest.rfind("。", 0, limit), rest.rfind("\n", 0, limit), rest.rfind("，", 0, limit))
        if cut < limit // 2:
            cut = limit - 1        # +1 补上这个字符，正好 limit 个
        chunks.append(rest[:cut + 1].strip())
        rest = rest[cut + 1:]
    if rest.strip():
        chunks.append(rest.strip())
    return [c for c in chunks if c]


class OneBotClient:
    """一条 WS 连接 + 事件回调 + 动作调用。

    连接被掐断（网关重启、网络抖动）由 `run_forever` 负责重连 —— 桥不该因为网关
    重启一次就得让人手动点一遍。
    """

    def __init__(self, cfg: dict, on_event: Handler, logger=print):
        ob = (cfg or {}).get("onebot") or {}
        self.mode = str(ob.get("mode") or "server")
        self.ws_url = str(ob.get("ws_url") or "")
        self.host = str(ob.get("listen_host") or "127.0.0.1")
        self.port = int(ob.get("listen_port") or 3001)
        self.path = str(ob.get("listen_path") or "/onebot/v11/ws")
        self.access_token = str(ob.get("access_token") or "")
        self.self_id = str(ob.get("self_id") or "")
        self.reconnect_seconds = int(ob.get("reconnect_seconds") or 5)
        self.on_event = on_event
        self.log = logger
        self._ws = None
        self._pending: dict[str, asyncio.Future] = {}
        self._session: Any = None
        self._stop = asyncio.Event()
        self.connected = asyncio.Event()

    # ---- 动作 ----

    async def call(self, action: str, params: dict | None = None, timeout: float = 20.0) -> dict:
        """发一个动作给网关，等 echo 回来。失败返回 `{"retcode": -1, "message": …}`，不抛。"""
        if self._ws is None or self._ws.closed:
            return {"status": "failed", "retcode": -1, "message": "还没连上 QQ 网关"}
        echo = uuid.uuid4().hex[:12]
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        payload = {"action": action, "params": params or {}, "echo": echo}
        try:
            await self._ws.send_str(json.dumps(payload, ensure_ascii=False))
            result = await asyncio.wait_for(future, timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            return {"status": "failed", "retcode": -1, "message": f"动作 {action} 超时"}
        except (OSError, RuntimeError) as exc:
            return {"status": "failed", "retcode": -1, "message": f"动作 {action} 发送失败：{exc}"}
        finally:
            self._pending.pop(echo, None)
        return result if isinstance(result, dict) else {"status": "ok", "retcode": 0}

    async def login_info(self) -> dict:
        return (await self.call("get_login_info")).get("data") or {}

    async def send_private(self, user_id: str, segments: list[dict]) -> dict:
        return await self.call("send_private_msg", {"user_id": int(user_id) if str(user_id).isdigit() else user_id,
                                                    "message": segments})

    async def send_group(self, group_id: str, segments: list[dict]) -> dict:
        return await self.call("send_group_msg", {"group_id": int(group_id) if str(group_id).isdigit() else group_id,
                                                  "message": segments})

    async def send(self, event: "Event", segments: list[dict]) -> dict:
        if event.is_group:
            return await self.send_group(event.group_id, segments)
        return await self.send_private(event.user_id, segments)

    async def get_media_url(self, file: str) -> str:
        """把消息段里的 `file` 换成可下载的 URL。

        多数网关直接在 `image.url` 里给了下载地址；没给时问一次 `get_image` / `get_record`
        （`get_record` 对语音还顺带把 SILK 转成 wav/amr，正好用来喂 ASR）。
        """
        for action in ("get_image", "get_record", "get_file"):
            result = await self.call(action, {"file": file}, timeout=30.0)
            data = result.get("data") or {}
            url = data.get("url") or data.get("file")
            if result.get("retcode") == 0 and url:
                if isinstance(url, str) and url.startswith(("http://", "https://", "file://")):
                    return url
                return str(url)   # 有些网关回本地绝对路径，交给下载那头处理
        return ""

    # ---- 连接 ----

    async def run_forever(self) -> None:
        """连上 → 收事件 → 断了重连。Ctrl+C / stop() 才退出。"""
        if self.mode == "server":
            await self._serve()
        else:
            while not self._stop.is_set():
                try:
                    await self._dial_once()
                except (OSError, OneBotError) as exc:
                    self.log(f"⚠ 和 QQ 网关的连接断了：{exc}；{self.reconnect_seconds}s 后重连")
                self.connected.clear()
                if self._stop.is_set():
                    break
                await asyncio.sleep(self.reconnect_seconds)

    def stop(self) -> None:
        self._stop.set()

    async def _serve(self) -> None:
        """反向 WS：我们当服务端，网关连进来。"""
        app = web.Application()
        app.router.add_get(self.path, self._handle_reverse)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        self.log(f"🛰 等 QQ 网关连过来：ws://{self.host}:{self.port}{self.path}")
        try:
            await self._stop.wait()
        finally:
            await runner.cleanup()

    async def _handle_reverse(self, request: web.Request) -> web.WebSocketResponse:
        if self.access_token:
            token = request.query.get("access_token") or request.headers.get("Authorization", "").replace("Bearer ", "")
            if token != self.access_token:
                raise web.HTTPUnauthorized(text="access_token 不对")
        ws = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024, heartbeat=30)
        await ws.prepare(request)
        self._ws = ws
        await self._on_connected()
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._dispatch(msg.data)
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            if self._ws is ws:
                self._ws = None
            self.connected.clear()
            self.log("⚠ QQ 网关断开了，继续等它连回来")
        return ws

    async def _dial_once(self) -> None:
        """正向 WS：我们连网关（少数网关只支持这种）。"""
        import aiohttp

        headers = {"Authorization": f"Bearer {self.access_token}"} if self.access_token else {}
        async with aiohttp.ClientSession(headers=headers) as session:
            self._session = session
            async with session.ws_connect(self.ws_url, heartbeat=30,
                                          max_msg_size=16 * 1024 * 1024) as ws:
                self._ws = ws
                await self._on_connected()
                async for msg in ws:
                    if msg.type == WSMsgType.TEXT:
                        await self._dispatch(msg.data)
                    elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                        break

    async def _on_connected(self) -> None:
        """连上后先宣布「连上了」，问自己是谁这件事**丢到后台**去做。

        ⚠ 这里绝不能 await 动作结果：动作的 echo 要靠 `_dispatch` 回填，而 `_dispatch`
        只在读消息的循环里跑 —— 在读循环启动前 await，就是自己等自己（死等 20 秒超时）。
        """
        self.connected.set()
        asyncio.create_task(self._fetch_login_info())  # noqa: RUF006 - 短命任务，失败自己吞

    async def _fetch_login_info(self) -> None:
        info = await self.login_info()
        if info.get("user_id"):
            self.self_id = str(info["user_id"])
            self.log(f"✅ 和 QQ 网关连上了，我是 {info.get('nickname', '?')}（{self.self_id}）")
        else:
            self.log("✅ 和 QQ 网关连上了（网关没回登录信息，先按配置里那个 id 用）")
        if not self.self_id:
            self.log("⚠ 拿不到机器人自己的 QQ 号 —— 群里 @ 判断会失效，请填 config.json 的 onebot.self_id")
        self.connected.set()

    async def _dispatch(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        echo = payload.get("echo")
        if echo is not None and echo in self._pending:
            future = self._pending[echo]
            if not future.done():
                future.set_result(payload)
            return
        if payload.get("post_type") == "meta_event":
            return
        event = parse_event(payload, self.self_id)
        if event is None:
            return
        if not self.self_id and event.self_id:
            self.self_id = event.self_id
        try:
            await self.on_event(event)
        except Exception as exc:  # noqa: BLE001 - 单条消息出错不能带崩整条连接
            self.log(f"⚠ 处理这条消息出错了：{exc.__class__.__name__}: {exc}")