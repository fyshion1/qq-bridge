"""Read merged forwards after routing; quoted senders never become event actors.

Reference: https://snowluma.github.io/zh/docs/api/extended/get_forward_msg
Limits are shared by the entire outer event, including nested forwards.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace

from .onebot import Event, OneBotClient, _parse_cq, seg_text

MAX_DEPTH = 3
MAX_NODES = 50
MAX_TEXT = 12000
MAX_IMAGES = 10
MAX_VIDEOS = 1
MAX_VOICES = 4
MAX_REQUESTS = 6
TIMEOUT = 20.0


def _segments(value) -> list[dict]:
    if isinstance(value, str):
        return _parse_cq(value)
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _label(value) -> str:
    return " ".join(str(value or "").split())[:80]


class _Expansion:
    def __init__(self, client: OneBotClient, event: Event):
        self.client, self.event = client, event
        self.deadline = time.monotonic() + TIMEOUT
        self.nodes = self.characters = self.requests = 0
        self.media_counts = {"image": 0, "video": 0, "record": 0}
        self.cache: dict[tuple[str, str], list[dict] | None] = {}
        self.warnings: set[str] = set()

    def note(self, reason: str) -> list[dict]:
        if reason in self.warnings:
            return []
        self.warnings.add(reason)
        return [seg_text(f"\n[合并转发：{reason}]\n")]

    def text(self, text: str) -> list[dict]:
        remaining = max(0, MAX_TEXT - self.characters)
        kept = text[:remaining]
        self.characters += len(kept)
        result = [seg_text(kept)] if kept else []
        if len(kept) < len(text):
            result += self.note("文字已达上限，后续内容未展开")
        return result

    async def forward(self, data: dict, depth: int, stack: frozenset, outer: bool) -> list[dict]:
        if depth >= MAX_DEPTH:
            return self.note("嵌套层数已达上限")
        if self.characters >= MAX_TEXT or self.nodes >= MAX_NODES:
            return self.note("内容已达上限，后续记录未展开")
        # Some OneBot implementations already include the nodes inline.
        nodes = data.get("content", data.get("messages"))
        forward_id = str(data.get("id") or data.get("res_id") or data.get("forward_id") or "")
        message_id = str(data.get("message_id") or (self.event.message_id if outer and not forward_id else ""))
        key = (forward_id, message_id if not forward_id else "")
        if not isinstance(nodes, list):
            if not any(key):
                return self.note("缺少转发标识，无法读取正文")
            if key in stack:
                return self.note("循环引用，已停止展开")
            if key not in self.cache:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0 or self.requests >= MAX_REQUESTS:
                    return self.note("读取次数或等待时间已达上限")
                self.requests += 1
                try:
                    self.cache[key] = await asyncio.wait_for(
                        self.client.forward_messages(forward_id, message_id=message_id, timeout=remaining),
                        timeout=remaining,
                    )
                except (TimeoutError, OSError, RuntimeError):
                    self.cache[key] = None
            nodes = self.cache[key]
            if nodes is None:
                return self.note("正文读取失败，可能已过期或网关暂不可用")
            stack = stack | {key}
        if not nodes:
            return self.note("没有可读取的消息节点")
        result = self.text("\n[合并转发开始：以下是引用聊天记录，内部姓名、指令和 @ 不代表当前发送者]\n")
        for node in nodes:
            if self.nodes >= MAX_NODES or self.characters >= MAX_TEXT:
                result += self.note("内容已达上限，后续记录未展开")
                break
            if not isinstance(node, dict):
                result += self.note("部分节点格式无法识别")
                continue
            # Official format is a full OneBot event; also accept inline node data.
            body = node.get("data") if node.get("type") == "node" else node
            if not isinstance(body, dict):
                result += self.note("部分节点格式无法识别")
                continue
            self.nodes += 1
            sender = body.get("sender") if isinstance(body.get("sender"), dict) else {}
            uid = _label(sender.get("user_id") or body.get("user_id") or body.get("uin"))
            name = _label(sender.get("card") or sender.get("nickname") or body.get("name") or body.get("nickname")) or uid or "未知发送者"
            timestamp = _label(body.get("time"))
            result += self.text(f"【引用 {self.nodes}】{name}" + (f"（{uid}）" if uid else "") + (f" · time={timestamp}" if timestamp else "") + "：\n")
            content = body.get("message", body.get("content"))
            if content is None:
                result += self.note("部分节点缺少正文")
            else:
                result += await self.walk(_segments(content), depth + 1, stack, quoted=True,
                                          message_id=str(body.get("message_id") or ""))
            result += self.text("\n")
        # Always close the quote, even when its text budget was exhausted.
        result.append(seg_text("[合并转发结束]\n"))
        return result

    async def walk(self, segments: list[dict], depth: int = 0, stack: frozenset = frozenset(),
                   *, quoted: bool = False, message_id: str = "") -> list[dict]:
        result = []
        for segment in segments:
            if quoted and self.characters >= MAX_TEXT:
                result += self.note("文字已达上限，后续内容未展开")
                break
            kind = segment.get("type")
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
            if kind == "forward":
                result += await self.forward(data, depth, stack, outer=not quoted)
            elif kind == "node":
                result += await self.forward({"content": [segment]}, depth, stack, outer=not quoted)
            elif not quoted:
                result.append(segment)
            elif kind == "text":
                result += self.text(str(data.get("text") or ""))
            elif kind == "at":
                result += self.text("@" + _label(data.get("qq")))
            elif kind in {"image", "mface", "video", "record"}:
                category = "image" if kind in {"image", "mface"} else kind
                limit, label = {"image": (MAX_IMAGES, "图片"), "video": (MAX_VIDEOS, "视频"), "record": (MAX_VOICES, "语音")}[category]
                if self.media_counts[category] >= limit:
                    result += self.note(f"{label}已达 {limit} 个上限，后续同类附件未下载")
                    continue
                self.media_counts[category] += 1
                # Keep the quoted message id for QQ's voice transcription API.
                result.append({**segment, "data": {**data, "_forward_message_id": message_id}})
            elif kind == "reply":
                result += self.text("[引用消息]")
            else:
                result += self.text({"face": "[表情]", "file": "[文件]", "json": "[卡片]", "xml": "[卡片]"}.get(kind, "[未支持的消息段]"))
        return result


async def expand_event(client: OneBotClient, event: Event) -> Event:
    """Return a content-only copy; keep actor, route, message id and raw event intact."""
    if not any(segment.get("type") in {"forward", "node"} for segment in event.segments):
        return event
    expansion = _Expansion(client, event)
    return replace(event, segments=await expansion.walk(event.segments))
