"""Offline forward resolution and bounded expansion; never sends QQ messages."""
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_bridge import event
from qqbridge import forwards, onebot


def forward(resource="root"):
    return {"type": "forward", "data": {"id": resource}}


def node(text="原始正文", segments=None):
    return {"message_id": "inner-message", "time": 1234567890,
            "sender": {"user_id": "99999", "nickname": "原发送者", "card": "群名片"},
            "message": segments if segments is not None else [onebot.seg_text(text)]}


class ForwardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = Mock(spec=onebot.OneBotClient)
        self.client.forward_messages = AsyncMock(return_value=[node()])
        self.event = event(text="请看这段", message_id="outer-message")
        self.event.segments += [forward(), onebot.seg_text("这是我的补充")]

    async def test_official_nodes_preserve_order_and_outer_identity(self):
        self.client.forward_messages.return_value = [node("第一句"), node("第二句")]
        expanded = await forwards.expand_event(self.client, self.event)
        text = expanded.plain_text()
        for value in ("请看这段", "群名片", "99999", "1234567890", "第一句", "第二句", "这是我的补充"):
            self.assertIn(value, text)
        self.assertLess(text.index("第一句"), text.index("第二句"))
        self.assertEqual(expanded.user_id, self.event.user_id)
        self.assertEqual(expanded.message_id, "outer-message")
        self.assertEqual(expanded.raw, self.event.raw)
        self.assertEqual(self.event.segments[1], forward())

    async def test_nested_forward_and_cq_content(self):
        self.client.forward_messages.side_effect = lambda rid, **kw: (
            [node(segments=[onebot.seg_text("外层"), forward("inner")])] if rid == "root" else
            [{"sender": {"nickname": "内层"}, "message": "文字[CQ:image,file=image.jpg]"}])
        expanded = await forwards.expand_event(self.client, self.event)
        self.assertIn("内层", expanded.plain_text())
        self.assertEqual(expanded.media()[0]["data"]["file"], "image.jpg")
        self.assertEqual(self.client.forward_messages.await_count, 2)

    async def test_quoted_mentions_commands_and_voice_keep_their_origin(self):
        self.client.forward_messages.return_value = [node(segments=[
            {"type": "at", "data": {"qq": self.event.self_id}}, onebot.seg_text("/reset /silent"),
            {"type": "record", "data": {"file": "voice"}}])]
        expanded = await forwards.expand_event(self.client, self.event)
        self.assertFalse(expanded.mentions_me())
        self.assertFalse(expanded.plain_text().startswith("/reset"))
        self.assertEqual(expanded.user_id, self.event.user_id)
        self.assertIn("/silent", expanded.plain_text())
        self.assertEqual(expanded.media()[0]["data"]["_forward_message_id"], "inner-message")

    async def test_inline_nodes_need_no_request(self):
        self.event.segments = [{"type": "forward", "data": {"content": [
            {"type": "node", "data": {"name": "内嵌", "uin": "123", "content": "不用联网"}}]}}]
        expanded = await forwards.expand_event(self.client, self.event)
        self.assertIn("不用联网", expanded.plain_text())
        self.client.forward_messages.assert_not_awaited()

    async def test_cycle_stops_and_siblings_share_cache(self):
        self.client.forward_messages.return_value = [node(segments=[forward()])]
        self.event.segments = [forward(), forward()]
        expanded = await forwards.expand_event(self.client, self.event)
        self.assertIn("循环引用", expanded.plain_text())
        self.assertEqual(self.client.forward_messages.await_count, 1)

    async def test_depth_nodes_text_media_and_request_budgets(self):
        self.client.forward_messages.side_effect = lambda rid, **kw: [node(segments=[forward(rid + "x")])]
        expanded = await forwards.expand_event(self.client, self.event)
        self.assertIn("嵌套层数", expanded.plain_text())
        self.assertEqual(self.client.forward_messages.await_count, forwards.MAX_DEPTH)
        self.client.forward_messages.reset_mock(side_effect=True)
        self.client.forward_messages.return_value = [node(str(i)) for i in range(100)]
        expanded = await forwards.expand_event(self.client, self.event)
        self.assertEqual(expanded.plain_text().count("【引用 "), forwards.MAX_NODES)
        self.assertIn("内容已达上限", expanded.plain_text())
        self.client.forward_messages.return_value = [node("x" * 50000)]
        expanded = await forwards.expand_event(self.client, self.event)
        self.assertLess(len(expanded.plain_text()), forwards.MAX_TEXT + 500)
        self.assertIn("这是我的补充", expanded.plain_text())
        self.assertLess(expanded.plain_text().index("[合并转发结束]"), expanded.plain_text().index("这是我的补充"))
        self.client.forward_messages.return_value = [node(segments=[
            {"type": "image", "data": {"url": "https://example.org/test.png"}} for _ in range(20)])]
        expanded = await forwards.expand_event(self.client, self.event)
        self.assertEqual(len(expanded.media()), forwards.MAX_IMAGES)
        self.assertIn("图片已达", expanded.plain_text())
        self.client.forward_messages.reset_mock()
        self.event.segments = [forward(str(i)) for i in range(20)]
        expanded = await forwards.expand_event(self.client, self.event)
        self.assertEqual(self.client.forward_messages.await_count, forwards.MAX_REQUESTS)
        self.assertIn("读取次数", expanded.plain_text())

    async def test_image_video_and_voice_limits_are_independent_across_nested_records(self):
        def media(kind, count):
            return [{"type": kind, "data": {"url": f"https://example.org/{kind}-{i}"}} for i in range(count)]
        self.client.forward_messages.side_effect = lambda rid, **kw: (
            [node(segments=media("image", 6) + media("video", 1) + [forward("nested")])]
            if rid == "root" else [node(segments=media("mface", 8) + media("video", 2) + media("record", 7))])
        expanded = await forwards.expand_event(self.client, self.event)
        kept = expanded.media()
        self.assertEqual(sum(s["type"] in {"image", "mface"} for s in kept), 10)
        self.assertEqual(sum(s["type"] == "video" for s in kept), 1)
        self.assertEqual(sum(s["type"] == "record" for s in kept), 4)
        self.assertIn("视频已达 1 个上限", expanded.plain_text())

    async def test_failure_empty_malformed_and_timeout_are_visible(self):
        for result in (None, [], [{"message": None}, None]):
            self.client.forward_messages.return_value = result
            expanded = await forwards.expand_event(self.client, self.event)
            self.assertIn("合并转发", expanded.plain_text())
            self.assertIn("这是我的补充", expanded.plain_text())
        async def slow(*args, **kwargs):
            await asyncio.sleep(10)
        self.client.forward_messages.side_effect = slow
        with patch.object(forwards, "TIMEOUT", .01):
            expanded = await asyncio.wait_for(forwards.expand_event(self.client, self.event), .5)
        self.assertIn("读取失败", expanded.plain_text())

    async def test_no_forward_is_a_noop(self):
        ordinary = event()
        self.assertIs(await forwards.expand_event(self.client, ordinary), ordinary)
        self.client.forward_messages.assert_not_awaited()

    async def test_protocol_uses_correct_resource_and_outer_id_parameters(self):
        client = onebot.OneBotClient({}, None, lambda _: None)
        client.call = AsyncMock(return_value={"retcode": 0, "data": {"messages": [node()]}})
        self.assertTrue(await client.forward_messages("resource-id"))
        client.call.assert_awaited_with("get_forward_msg", {"id": "resource-id"}, timeout=20)
        await client.forward_messages(message_id="outer-id")
        client.call.assert_awaited_with("get_forward_msg", {"message_id": "outer-id"}, timeout=20)
        for result in ({"retcode": 0, "data": {"messages": "malformed"}}, {"retcode": 1404}):
            client.call.return_value = result
            self.assertIsNone(await client.forward_messages("resource-id"))
