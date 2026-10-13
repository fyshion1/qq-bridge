"""假 QQ 网关：**不装 QQ、不配网关也能先看桥通不通**。

它就是一个最小的 OneBot v11 实现，摆在桥的对面试：

    你（这个脚本）──ws──▶ QQ 桥（reverse WS 服务端）
                  ◀────── 它调 send_private_msg / send_group_msg，你打印出来

用法（先另开一个窗口跑 `python -m qqbridge run`）：

    python tools/fake_onebot.py                       # 私聊问它一句
    python tools/fake_onebot.py --group 555           # 在群里 @ 它
    python tools/fake_onebot.py --text "你今天干嘛了"
    python tools/fake_onebot.py --image 小图.png      # 带张图（走视觉路）
    python tools/fake_onebot.py --voice 语音.wav      # 发条语音（走 ASR，配了才有用）
    python tools/fake_onebot.py --listen              # 常驻：在终端里打字，它就当你说的话

注意：桥的白名单得先放行（私聊填你的 QQ 号、群聊填这个群号），否则它会「略过 · 白名单」——
这是**故意的**，桥不该理没放行的人。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import aiohttp

WSS = "ws://127.0.0.1:3001/onebot/v11/ws"
SELF = "10001"        # 假装这是桥那个机器人的 QQ 号
SENDER = "20002"      # 假装这是发消息的人（私聊白名单里要有它）


class FakeGateway:
    def __init__(self, url: str, access_token: str = "", logger=print):
        self.url = url
        self.token = access_token
        self.log = logger
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.session: aiohttp.ClientSession | None = None
        self.replied = asyncio.Event()

    async def connect(self) -> None:
        params = {"access_token": self.token} if self.token else {}
        self.session = aiohttp.ClientSession()
        self.ws = await self.session.ws_connect(self.url, params=params, heartbeat=30)
        self.log(f"✅ 连上桥了：{self.url}")

    async def close(self) -> None:
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()
        if self.session is not None:
            await self.session.close()

    async def send_event(self, payload: dict) -> None:
        assert self.ws is not None
        await self.ws.send_str(json.dumps(payload, ensure_ascii=False))

    def _segments(self, args) -> list[dict]:
        segs: list[dict] = []
        if args.group:
            segs.append({"type": "at", "data": {"qq": SELF}})
        if args.image:
            segs.append({"type": "image", "data": {"file": Path(args.image).resolve().as_posix()}})
        if args.voice:
            segs.append({"type": "record", "data": {"file": Path(args.voice).resolve().as_posix()}})
        segs.append({"type": "text", "data": {"text": args.text}})
        return segs

    def event(self, args, text: str | None = None) -> dict:
        segs = self._segments(args)
        if text is not None:
            segs = [{"type": "at", "data": {"qq": SELF}}] if args.group else []
            segs.append({"type": "text", "data": {"text": text}})
        base = {
            "post_type": "message",
            "message_type": "group" if args.group else "private",
            "message_id": str(asyncio.get_running_loop().time()).replace(".", ""),
            "user_id": int(SENDER),
            "self_id": int(SELF),
            "sender": {"nickname": "测试员", "card": "测试员"},
            "message": segs,
        }
        if args.group:
            base["group_id"] = int(args.group)
        return base

    async def listen(self, args) -> None:
        """收桥的动作：get_login_info 回一下；发消息的打出来。"""
        assert self.ws is not None
        async for msg in self.ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            payload = json.loads(msg.data)
            echo, action = payload.get("echo"), payload.get("action")
            params = payload.get("params") or {}
            if action == "get_login_info":
                await self.ws.send_str(json.dumps({
                    "status": "ok", "retcode": 0, "echo": echo,
                    "data": {"user_id": int(SELF), "nickname": "小糖人（假）"}}, ensure_ascii=False))
                self.log("→ get_login_info：已告诉它我是谁")
                continue
            if action in ("send_private_msg", "send_group_msg"):
                where = f"群 {params.get('group_id')}" if action.endswith("group_msg") else f"私聊 {params.get('user_id')}"
                self.log(f"\n💬 小糖人（{where}）：")
                for seg in params.get("message") or []:
                    kind, data = seg.get("type"), seg.get("data") or {}
                    if kind == "text":
                        self.log(f"   {data.get('text', '')}")
                    elif kind == "at":
                        self.log(f"   @{data.get('qq')}")
                    elif kind == "record":
                        self.log(f"   [语音条] {data.get('file', '')}")
                    else:
                        self.log(f"   [{kind}] {data.get('file', '')}")
                await self.ws.send_str(json.dumps({
                    "status": "ok", "retcode": 0, "echo": echo, "data": {"message_id": 1}}, ensure_ascii=False))
                self.replied.set()
                continue
            if echo is not None:
                # 其它动作（get_image / get_record…）一律回「失败」——假网关没有真文件。
                await self.ws.send_str(json.dumps({
                    "status": "failed", "retcode": 1404, "echo": echo, "wording": "假网关没有这个文件"},
                    ensure_ascii=False))

    async def run(self, args) -> None:
        await self.connect()
        task = asyncio.create_task(self.listen(args))
        try:
            await self.send_event(self.event(args))
            self.log(f"→ 发了一条{'群' if args.group else '私聊'}消息：{args.text}")
            if not args.listen:
                # 等它真的回一条，而不是傻等固定秒数（回完就走，干脆）。
                try:
                    await asyncio.wait_for(self.replied.wait(), timeout=args.wait)
                except (TimeoutError, asyncio.TimeoutError):
                    self.log(f"（等了 {args.wait:.0f} 秒没等到回话 —— 看桥那边的日志，多半是白名单 / 大脑没配好）")
                return
            self.log("\n现在你在终端里打字就是往它说话（空行 + Ctrl+C 退出）…")
            while True:
                line = await asyncio.to_thread(input, "你：")
                if not line.strip():
                    break
                self.replied.clear()
                await self.send_event(self.event(args, text=line.strip()))
                try:
                    await asyncio.wait_for(self.replied.wait(), timeout=60)
                except (TimeoutError, asyncio.TimeoutError):
                    pass
        finally:
            task.cancel()
            await self.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="假 QQ 网关（不装 QQ 也能试桥）")
    parser.add_argument("--url", default=WSS)
    parser.add_argument("--token", default="")
    parser.add_argument("--group", default="", help="群号（留空 = 私聊；群里要 @ 才回，这里自动带上 @）")
    parser.add_argument("--text", default="你在干嘛呀")
    parser.add_argument("--image", default="", help="带一张图（走视觉路）")
    parser.add_argument("--voice", default="", help="带一条语音（走 ASR，没配识别就只当[语音]）")
    parser.add_argument("--listen", action="store_true", help="常驻：在终端打字当说话")
    parser.add_argument("--wait", type=float, default=90.0, help="不常驻时等回复的秒数")
    args = parser.parse_args()

    gateway = FakeGateway(args.url, args.token)
    try:
        asyncio.run(gateway.run(args))
    except KeyboardInterrupt:
        pass
    except aiohttp.ClientConnectorError:
        print(f"✗ 连不上 {args.url} —— 桥起了吗？（另一个窗口跑 `python -m qqbridge run`）")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())