"""OneBot v11 接入层：**只做协议，不做业务**。

QQ 那边由网关负责（**NapCat** 为首选，SnowLuma / Lagrange 等同为 OneBot v11 系）——
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

from aiohttp import ClientError, WSMsgType, web

Handler = Callable[["Event"], Awaitable[None]]
NoticeHandler = Callable[["Notice"], Awaitable[None]]


class OneBotError(RuntimeError):
    pass


# 连网关时**该重连**的异常。aiohttp 的 ClientError 不在 OSError 那支上
# （WSServerHandshakeError 的 mro 是 ClientResponseError → ClientError → Exception），
# 所以握手被拒（401）以前会直接穿出 run_forever，把整个桥打崩 —— 网关挂错号时必现。
_CONNECT_ERRORS = (OSError, OneBotError, ClientError)


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


def seg_at(user_id: str) -> dict:
    """@ 某人（群里回话时用）。"""
    return {"type": "at", "data": {"qq": str(user_id)}}


def text_of_record(seg: dict) -> str:
    """语音段里**网关顺手给的转写**（就是手机上那个「转文字」）。

    各家网关字段名不一：NapCat/Lagrange 系常见 `text`，也有塞在 `summary` 里的。
    有就用 —— 这就是大老大说的「ASR 用 QQ 的，qq 有转写的」。
    """
    data = (seg or {}).get("data") or {}
    for key in ("text", "transcription", "summary", "trans"):
        value = str(data.get(key) or "").strip()
        # 有些网关没有转写时会把文件名 / "[语音]" 塞进 summary，那些不算转写。
        if value and not value.startswith("[") and value not in {"语音", "voice"}:
            return value
    return ""


# ---- 通知事件（戳一戳 / 群成员变动 / 撤回）----
#
# 参考项目接了 poke，我们**默认也接**（大老大批注「都想要」），但加了防刷屏：
# 同一个人在 30 秒内戳它只理一次 —— 不然有人连点几下就是一片回复。


@dataclass
class Notice:
    """一条通知事件（规整过的）。"""

    kind: str                      # poke | group_increase | group_decrease | recall | other
    sub_type: str = ""
    group_id: str = ""
    user_id: str = ""              # 触发者（谁戳的 / 谁进群）
    target_id: str = ""            # 被戳的 / 被操作的
    operator_id: str = ""
    message_id: str = ""
    self_id: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def is_group(self) -> bool:
        return bool(self.group_id)

    @property
    def session_key(self) -> str:
        return f"{'g' if self.is_group else 'p'}:{self.group_id or self.user_id}"

    def describe(self) -> str:
        """给人看的一句话（同步进小窝时用，别让它开口）。"""
        who = self.user_id or "有人"
        if self.kind == "poke":
            return f"{who} 戳了戳我"
        if self.kind == "group_increase":
            return f"{who} 进群了"
        if self.kind == "group_decrease":
            return f"{who} 退群了（{self.sub_type or 'leave'}）"
        if self.kind == "recall":
            return f"{who} 撤回了一条消息"
        return f"一条 {self.kind} 通知"


def parse_notice(payload: dict, self_id: str = "") -> Notice | None:
    """OneBot 通知事件 → Notice；不是我们关心的一类就返回 None。

    只认：戳一戳（notify/poke）、群成员增减、消息撤回。其余 notice 一律不管 ——
    少一类事件就少一处会莫名其妙自己动起来的地方。
    """
    if (payload or {}).get("post_type") != "notice":
        return None
    notice_type = str(payload.get("notice_type") or "")
    sub_type = str(payload.get("sub_type") or "")
    if notice_type == "notify":
        if sub_type != "poke":
            return None
        return Notice(kind="poke", sub_type=sub_type,
                      group_id=str(payload.get("group_id") or ""),
                      user_id=str(payload.get("user_id") or ""),
                      target_id=str(payload.get("target_id") or ""),
                      self_id=str(payload.get("self_id") or self_id or ""),
                      raw=payload)
    if notice_type in ("group_increase", "group_decrease"):
        return Notice(kind=notice_type, sub_type=sub_type,
                      group_id=str(payload.get("group_id") or ""),
                      user_id=str(payload.get("user_id") or ""),
                      operator_id=str(payload.get("operator_id") or ""),
                      self_id=str(payload.get("self_id") or self_id or ""),
                      raw=payload)
    if notice_type == "group_recall":
        return Notice(kind="recall", sub_type=sub_type,
                      group_id=str(payload.get("group_id") or ""),
                      user_id=str(payload.get("user_id") or ""),
                      operator_id=str(payload.get("operator_id") or ""),
                      message_id=str(payload.get("message_id") or ""),
                      self_id=str(payload.get("self_id") or self_id or ""),
                      raw=payload)
    return None


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

    def __init__(self, cfg: dict, on_event: Handler, logger=print, on_notice: NoticeHandler | None = None):
        ob = (cfg or {}).get("onebot") or {}
        self.mode = str(ob.get("mode") or "server")
        self.ws_url = str(ob.get("ws_url") or "")
        self.host = str(ob.get("listen_host") or "127.0.0.1")
        self.port = int(ob.get("listen_port") or 3001)
        self.path = str(ob.get("listen_path") or "/onebot/v11/ws")
        self.access_token = str(ob.get("access_token") or "")
        self.self_id = str(ob.get("self_id") or "")
        self.reconnect_seconds = int(ob.get("reconnect_seconds") or 5)
        # 问网关要语音转写的等待上限。**必须大于网关自己等 QQ 推结果的时间**
        # （SnowLuma 的 waitPttTransText 是 20s），否则我们永远先放弃。
        self.ptt_timeout = float(ob.get("ptt_timeout") or 30)
        self.on_event = on_event
        self.on_notice = on_notice
        self.log = logger
        self._ws = None
        self._pending: dict[str, asyncio.Future] = {}
        self._session: Any = None
        self._stop = asyncio.Event()
        self.connected = asyncio.Event()
        self._queue: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None

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

    async def forward_messages(self, forward_id: str = "", *, message_id: str = "", timeout: float = 20) -> list[dict] | None:
        """SnowLuma get_forward_msg returns data.messages (OneBot message events).

        Prefer the forward segment resource id; message_id refers to its outer QQ
        message, not to the resource id. None explicitly means unreadable.
        """
        params = {"id": forward_id} if forward_id else {"message_id": message_id}
        if not (forward_id or message_id):
            return None
        result = await self.call("get_forward_msg", params, timeout=timeout)
        data = result.get("data")
        if result.get("retcode") != 0 or not isinstance(data, dict):
            return None
        messages = data.get("messages")
        return messages if isinstance(messages, list) else None

    async def ptt_text(self, message_id: str) -> str:
        """问网关要这条语音的**转写文本**（SnowLuma 的 `fetch_ptt_text`）。

        它不给就返回空串 —— 调用方按 `[语音]` 记，不装懂。

        ⚠ 超时必须**大于**网关自己等 QQ 推结果的时间（SnowLuma 是 20s），
        不然我们永远比它先放弃，本来能拿到的转写也拿不到。
        别名只在网关「不认识这个动作」时才接着试 —— 认识但没转写，试几个都一样。
        """
        if not message_id:
            return ""
        for action in ("fetch_ptt_text", "get_ptt_text", "get_record_text"):
            result = await self.call(action, {"message_id": message_id}, timeout=self.ptt_timeout)
            if result.get("retcode") == 0:
                text = str((result.get("data") or {}).get("text") or "").strip()
                if text:
                    return text
            elif result.get("retcode") != 1404:      # 不是「没这个动作」就别再试别名了
                return ""
        return ""

    # ---- 事件队列 ----
    #
    # ⚠ 读循环里**绝不能直接 await 业务处理**：处理里会调动作（发消息、问转写），
    # 而动作的 echo 只有读循环能收 —— 在读循环里 await 处理 = 自己等自己，每条都超时。

    def _ensure_worker(self) -> None:
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=256)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work())

    async def _work(self) -> None:
        """串行消费事件：一条处理完再处理下一条（顺序和以前一样，只是不堵读循环）。"""
        assert self._queue is not None
        while not self._stop.is_set():
            handler, arg = await self._queue.get()
            try:
                await handler(arg)
            except Exception as exc:  # noqa: BLE001 - 单条出错不能带崩整条连接
                self.log(f"⚠ 处理这条事件出错了：{exc.__class__.__name__}: {exc}")
            finally:
                self._queue.task_done()

    def _enqueue(self, handler: Handler | NoticeHandler, arg: Any) -> None:
        self._ensure_worker()
        try:
            self._queue.put_nowait((handler, arg))  # type: ignore[union-attr]
        except asyncio.QueueFull:
            self.log("⚠ 事件堆太多处理不过来，这条丢了")

    # ---- 连接 ----

    async def run_forever(self) -> None:
        """连上 → 收事件 → 断了重连。Ctrl+C / stop() 才退出。"""
        if self.mode == "server":
            await self._serve()
        else:
            while not self._stop.is_set():
                try:
                    await self._dial_once()
                except _CONNECT_ERRORS as exc:
                    self.log(f"⚠ 和 QQ 网关的连接断了：{self._connect_hint(exc)}；"
                             f"{self.reconnect_seconds}s 后重连")
                self.connected.clear()
                if self._stop.is_set():
                    break
                await asyncio.sleep(self.reconnect_seconds)

    def _connect_hint(self, exc: Exception) -> str:
        """把「连不上」说得能照着修 —— 尤其是 401。

        401 不是网络问题，是**网关认的不是我们要连的那个号**：SnowLuma 把每条 WS 服务
        按 QQ 号分开存（`config/onebot_<号>.json`），每个号一个自己的 accessToken。
        网关挂在 A 号上、桥配的是 B 号的 token 时，端口通、握手 401 —— 看着像 token 填错，
        其实是**挂错号**了（网关只挂电脑上当前登录的那个 QQ）。
        """
        import aiohttp

        if isinstance(exc, aiohttp.WSServerHandshakeError):
            if exc.status == 401:
                return (f"网关拒绝了（401，access_token 对不上）。多数情况不是 token 填错了，"
                        f"而是**网关挂的不是这个号** —— SnowLuma 每个 QQ 号一份配置"
                        f"（config/onebot_<号>.json）、各自的 accessToken 都不一样，"
                        f"而它只会挂电脑上当前登录的那个 QQ。"
                        f"先确认 QQ 里登录的是机器人号（配置里 self_id={self.self_id or '未填'}），"
                        f"再重启网关")
            return f"网关拒绝了握手（HTTP {exc.status}）"
        return str(exc)

    def stop(self) -> None:
        self._stop.set()
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()

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
        if payload.get("post_type") == "notice":
            # 通知事件（戳一戳 / 群成员变动 / 撤回）走另一条回调 —— 它们不该被当成
            # 「有人跟我说话」，处理方式完全不同（默认只记一笔，不一定开口）。
            notice = parse_notice(payload, self.self_id)
            if notice is None or self.on_notice is None:
                return
            self._enqueue(self.on_notice, notice)
            return
        event = parse_event(payload, self.self_id)
        if event is None:
            return
        if not self.self_id and event.self_id:
            self.self_id = event.self_id
        self._enqueue(self.on_event, event)
