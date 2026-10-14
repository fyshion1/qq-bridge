"""纯离线测试：白名单 / 同步与开口 / 重放去重 / 事件解析（不连真 QQ、不调大脑）。

    python -m unittest discover -s tests -v

重点测三件最容易出事、又最难靠肉眼发现的：
1. **白名单**：不该理的群 / 人绝不能理（装错了就是被拉进陌生群乱说话）。
2. **同步 vs 开口**：白名单里的群聊要同步进前端，但没 @ 我不许插嘴。
3. **重放**：网关重连重放旧消息时，不能重复回一遍。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qqbridge import config as config_mod  # noqa: E402
from qqbridge import onebot  # noqa: E402
from qqbridge.brain import BrainClient  # noqa: E402
from qqbridge.onebot import Event, parse_event, split_text  # noqa: E402
from qqbridge.sessions import Router, SeenMessages  # noqa: E402

SELF = "10001"


def event(*, group: str = "", user: str = "20002", text: str = "在吗", mention: bool = False,
          message_id: str = "m1") -> Event:
    segments: list[dict] = []
    if mention:
        segments.append({"type": "at", "data": {"qq": SELF}})
    if text:
        segments.append({"type": "text", "data": {"text": text}})
    return Event(kind="group" if group else "private", user_id=user, group_id=group,
                 message_id=message_id, nickname="路人", card="", self_id=SELF, segments=segments)


class FakeBrain:
    """冒充大脑：只记下被调用了什么。"""

    def __init__(self, roster=None):
        self.calls: list[tuple] = []
        self._roster = roster or [{"id": "dalaoda", "name": "大老大", "emoji": "🐯"}]

    def characters(self):
        return self._roster

    def api_config(self):
        return {"model": "fake", "base_url": "http://127.0.0.1:8000/v1", "api_key": "sk-x"}

    def chat(self, character_id, messages, **kw):
        self.calls.append(("chat", character_id, kw.get("images")))
        return {"message": {"content": "回你一句"}}

    def append_message(self, session_id, role, content="", **kw):
        self.calls.append(("append", session_id, role, content))
        return {}

    def extract_memories(self, text, **kw):
        self.calls.append(("memory", kw.get("character_ids")))
        return {}

    def upload_attachment(self, session_id, path, source="upload"):
        self.calls.append(("upload", session_id, source))
        return {"orig": f"/api/files/{session_id}/orig/x.png",
                "thumb": f"/api/files/{session_id}/thumb/x.jpg"}

    def tts_bytes(self, text, *, engine="edge", voice=""):
        self.calls.append(("tts", text, engine))
        return b"fake-audio", "mp3"


def cfg(**over) -> dict:
    base = json.loads(json.dumps(config_mod.DEFAULT))
    base["allow"]["groups"] = ["555"]
    base["allow"]["private"] = ["20002"]
    base["persona"]["character_id"] = "dalaoda"
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key].update(value)
        else:
            base[key] = value
    return base


def router(**over) -> Router:
    return Router(cfg(**over), FakeBrain())  # type: ignore[arg-type]


class AllowTests(unittest.TestCase):
    def test_stranger_private_is_ignored(self):
        ok, why = router().allowed(event(user="99999"))
        self.assertFalse(ok)
        self.assertIn("白名单", why)

    def test_unknown_group_is_ignored(self):
        ok, _ = router().allowed(event(group="777", mention=True))
        self.assertFalse(ok)

    def test_owner_private_bypasses_whitelist(self):
        ok, _ = router(owner={"qq": "88888", "whitelist_bypass": True}).allowed(event(user="88888"))
        self.assertTrue(ok)

    def test_self_message_ignored(self):
        ok, why = router().allowed(event(group="555", user=SELF, mention=True))
        self.assertFalse(ok)
        self.assertIn("自己", why)

    def test_group_without_mention_syncs_but_does_not_reply(self):
        route = router().route(event(group="555", mention=False, message_id="g1"))
        self.assertTrue(route.sync)
        self.assertFalse(route.reply)
        self.assertEqual(route.frontend_session, "group")

    def test_group_with_mention_replies(self):
        route = router().route(event(group="555", mention=True, message_id="g2"))
        self.assertTrue(route.sync and route.reply)
        self.assertEqual(route.session_kind, "group")

    def test_mention_requirement_can_be_turned_off(self):
        r = router()
        r.cfg["allow"]["require_mention_in_group"] = False
        self.assertTrue(r.route(event(group="555", mention=False, message_id="g3")).reply)

    def test_silent_mode_syncs_without_reply(self):
        route = router(modes={"dm": "silent", "group": "chat"}).route(event(user="20002", message_id="p1"))
        self.assertTrue(route.sync)
        self.assertFalse(route.reply)
        self.assertIn("静音", route.reason)

    def test_private_routes_to_single_session(self):
        route = router().route(event(user="20002", message_id="p2"))
        self.assertEqual(route.frontend_session, "single-dalaoda")
        self.assertEqual(route.session_kind, "single")

    def test_persona_falls_back_to_first_in_roster(self):
        self.assertEqual(router(persona={"character_id": ""}).persona(), "dalaoda")


class DedupTests(unittest.TestCase):
    def test_same_message_id_not_replied_twice(self):
        r = router()
        first = r.route(event(group="555", mention=True, message_id="dup"))
        second = r.route(event(group="555", mention=True, message_id="dup"))
        self.assertTrue(first.reply)
        self.assertFalse(second.reply)
        self.assertIn("重放", second.reason)

    def test_seen_set_is_bounded(self):
        seen = SeenMessages(size=3)
        for i in range(10):
            seen.check(f"m{i}")
        self.assertFalse(seen.check("m9"))
        self.assertTrue(seen.check("m0"))


class EventTests(unittest.TestCase):
    def test_parse_group_event(self):
        e = parse_event({"post_type": "message", "message_type": "group", "group_id": 555,
                         "user_id": 20002, "self_id": 10001, "message_id": 7,
                         "sender": {"nickname": "小明", "card": "班长"},
                         "message": [{"type": "text", "data": {"text": "hi"}}]})
        self.assertIsNotNone(e)
        assert e is not None
        self.assertEqual(e.session_key, "g:555")
        self.assertEqual(e.display_name, "班长")
        self.assertTrue(e.is_group)

    def test_non_message_event_ignored(self):
        self.assertIsNone(parse_event({"post_type": "notice", "notice_type": "group_increase"}))

    def test_mentions_me_detects_at_segment(self):
        self.assertTrue(event(group="555", mention=True).mentions_me())

    def test_plain_text_marks_media(self):
        e = Event(kind="group", user_id="1", group_id="5", self_id=SELF, segments=[
            {"type": "at", "data": {"qq": SELF}},
            {"type": "text", "data": {"text": "看这个 "}},
            {"type": "image", "data": {}},
            {"type": "record", "data": {}},
        ])
        self.assertEqual(e.plain_text(), "@我看这个 [图片][语音]")
        self.assertEqual(len(e.media()), 2)

    def test_cq_string_message_is_parsed(self):
        # 网关一定会在事件里带 self_id（自己的 QQ 号）—— 没有它就判断不出 @ 的是不是我。
        e = parse_event({"post_type": "message", "message_type": "private", "user_id": 9,
                         "self_id": 10001,
                         "message": "hi[CQ:image,file=a.jpg][CQ:at,qq=10001]"})
        self.assertIsNotNone(e)
        assert e is not None
        self.assertEqual([s["type"] for s in e.segments], ["text", "image", "at"])
        self.assertTrue(e.mentions_me())

    def test_split_text_breaks_long_reply(self):
        chunks = split_text("啊" * 900, limit=400)
        self.assertGreaterEqual(len(chunks), 3)
        self.assertTrue(all(len(c) <= 400 for c in chunks))


class MemoryTests(unittest.TestCase):
    def test_turn_flushes_after_window(self):
        r = router(memory={"enabled": True, "window": 2, "isolate": False})
        r.remember("g:555", "group", "dalaoda", "小明", "第一句")
        r.remember("g:555", "group", "dalaoda", "大老大", "第二句")
        r.flush_memory()
        self.assertIn(("memory", ["dalaoda"]), r.brain.calls)  # type: ignore[attr-defined]

    def test_memory_not_isolated_by_default(self):
        self.assertFalse(BrainClient.isolate_memory)

    def test_pending_turn_keeps_only_non_empty_lines(self):
        r = router(memory={"enabled": True, "window": 99})
        r.remember("g:555", "group", "dalaoda", "小明", "   ")
        self.assertEqual(r.pending["g:555"].lines, [])


class SegmentTests(unittest.TestCase):
    def test_file_segments_use_absolute_posix_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "a.gif"
            target.write_bytes(b"GIF89a")
            seg = onebot.seg_image_file(str(target))
            self.assertEqual(seg["type"], "image")
            self.assertNotIn("\\", seg["data"]["file"])

    def test_record_segment_is_record_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "v.mp3"
            target.write_bytes(b"x")
            self.assertEqual(onebot.seg_record_file(str(target))["type"], "record")


class VoiceTranscriptionTests(unittest.TestCase):
    """语音识别走 **QQ 自己的转写**（大老大定的：'ASR 用 QQ 的，qq 有转写的'）。"""

    def test_gateway_text_is_used(self):
        seg = {"type": "record", "data": {"file": "a.silk", "text": "今晚吃什么"}}
        self.assertEqual(onebot.text_of_record(seg), "今晚吃什么")

    def test_placeholder_is_not_a_transcription(self):
        # 有些网关没转写时会把文件名 / "[语音]" 塞进 summary —— 那些不算听懂。
        for value in ("[语音]", "语音", "voice", "a.silk"):
            self.assertEqual(onebot.text_of_record({"data": {"summary": value}}), value
                             if value not in ("[语音]", "语音", "voice") else "")

    def test_missing_transcription_returns_empty(self):
        self.assertEqual(onebot.text_of_record({"data": {"file": "a.silk"}}), "")


class NoticeTests(unittest.TestCase):
    """通知事件：戳一戳会回一句，其余默认不处理（大老大批注「都想要」+ 防刷屏）。"""

    def test_poke_targeting_me_is_parsed(self):
        payload = {"post_type": "notice", "notice_type": "notify", "sub_type": "poke",
                   "user_id": "20002", "target_id": SELF, "group_id": "555"}
        notice = onebot.parse_notice(payload, SELF)
        self.assertIsNotNone(notice)
        self.assertEqual(notice.kind, "poke")
        self.assertTrue(notice.is_group)
        self.assertEqual(notice.session_key, "g:555")

    def test_group_member_change_is_parsed(self):
        payload = {"post_type": "notice", "notice_type": "group_decrease", "sub_type": "leave",
                   "user_id": "20002", "group_id": "555"}
        self.assertEqual(onebot.parse_notice(payload, SELF).kind, "group_decrease")

    def test_other_notices_ignored(self):
        payload = {"post_type": "notice", "notice_type": "friend_add", "user_id": "20002"}
        self.assertIsNone(onebot.parse_notice(payload, SELF))
        self.assertIsNone(onebot.parse_notice({"post_type": "message"}, SELF))


class AdminCommandTests(unittest.TestCase):
    """管理命令**桥直接执行，不过模型**（让模型决定「要不要照做」等于把开关交给概率）。"""

    def _router(self, **over):
        base = {"owner": {"qq": "20002", "admin_commands": True}}
        base.update(over)
        return router(**base)

    def test_non_owner_gets_nothing(self):
        self.assertIsNone(self._router().admin_command(event(text="/reset", user="99999")))

    def test_reset_returns_action(self):
        got = self._router().admin_command(event(text="/reset"))
        self.assertEqual(got["action"], "reset")

    def test_say_carries_text(self):
        got = self._router().admin_command(event(text="/say 大家早"))
        self.assertEqual((got["action"], got["arg"]), ("say", "大家早"))

    def test_unknown_command_is_reported(self):
        self.assertIn("不认识", self._router().admin_command(event(text="/nope"))["reply"])

    def test_admin_commands_can_be_turned_off(self):
        self.assertIsNone(self._router(owner={"qq": "20002", "admin_commands": False})
                          .admin_command(event(text="/reset")))


class MultiAccountTests(unittest.TestCase):
    """一份配置带多个 QQ 号（大老大：「我 QQ 号一堆，可以每个都配一个」）。"""

    def test_accounts_fall_back_to_single(self):
        rows = config_mod.accounts(cfg())
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["groups"])      # None = 用顶层白名单

    def test_each_account_gets_its_own_port_and_persona(self):
        rows = config_mod.accounts(cfg(accounts=[
            {"name": "大号", "self_id": "111", "persona": "dalaoda", "listen_port": 3101},
            {"name": "小号", "self_id": "222", "persona": "xiaoshiyin"},
        ], onebot={"listen_port": 3100}))
        self.assertEqual([r["listen_port"] for r in rows], [3101, 3101])   # 第二个按 3100+1
        self.assertEqual([r["persona"] for r in rows], ["dalaoda", "xiaoshiyin"])

    def test_disabled_account_is_skipped(self):
        rows = config_mod.accounts(cfg(accounts=[
            {"name": "停用的", "self_id": "111", "enabled": False},
            {"name": "在用的", "self_id": "222"},
        ]))
        self.assertEqual([r["self_id"] for r in rows], ["222"])

    def test_account_whitelist_overrides_top_level(self):
        r = Router(cfg(), FakeBrain(), account={"groups": ["777"], "private": []})
        self.assertEqual(r._groups(), ["777"])
        self.assertEqual(r._private(), [])
        self.assertTrue(r.allowed(event(group="777", mention=True))[0])
        self.assertFalse(r.allowed(event(group="555", mention=True))[0])


class SettingsMergeTests(unittest.TestCase):
    """前端设置压过 config.json（§9.6.5）—— 你在网页上点的那一下才是最后生效的。"""

    def tearDown(self):
        config_mod.set_remote({})

    def test_remote_overrides_local(self):
        config_mod.set_remote({"allow.groups": ["888"], "voice.voice": "huayan"})
        loaded = config_mod.apply_remote(cfg())
        self.assertEqual(loaded["allow"]["groups"], ["888"])
        self.assertEqual(loaded["voice"]["voice"], "huayan")

    def test_unregistered_key_is_ignored(self):
        config_mod.set_remote({"allow.everything": True, "evil.key": "x"})
        loaded = config_mod.apply_remote(cfg())
        self.assertNotIn("everything", loaded["allow"])
        self.assertNotIn("evil", loaded)

    def test_local_file_still_used_when_nothing_pulled(self):
        config_mod.set_remote({})
        self.assertEqual(config_mod.apply_remote(cfg())["allow"]["groups"], ["555"])


class LocalToolTests(unittest.TestCase):
    """本地小工具：给能力 + 画硬边界（大老大：「可以碰一下，只要别搞坏东西」）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "笔记.txt").write_text("今天要买牛奶", encoding="utf-8")
        self.tools = __import__("qqbridge.tools", fromlist=["x"])
        self.cfg = cfg(tools={"enabled": True, "root": str(self.root), "allow": ["dir", "read", "find"]})

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_inside_sandbox(self):
        ok, out = self.tools.run(self.cfg, "local_read", {"path": "笔记.txt"})
        self.assertTrue(ok, out)
        self.assertIn("牛奶", out)

    def test_escape_is_denied(self):
        ok, out = self.tools.run(self.cfg, "local_read", {"path": "../../secret.txt"})
        self.assertFalse(ok)
        self.assertIn("越界", out)

    def test_absolute_path_outside_is_denied(self):
        ok, out = self.tools.run(self.cfg, "local_read", {"path": "C:/Windows/win.ini"})
        self.assertFalse(ok)

    def test_shell_metacharacters_are_denied(self):
        for path in ("x | cat /etc/passwd", "a > b.txt", "a; rm -rf /"):
            self.assertFalse(self.tools.run(self.cfg, "local_read", {"path": path})[0], path)

    def test_unlisted_action_is_denied(self):
        ok, out = self.tools.run(self.cfg, "local_rm", {"path": "."})
        self.assertFalse(ok)
        self.assertIn("没有", out)

    def test_disabled_by_default(self):
        self.assertFalse(self.tools.enabled(cfg()))
        self.assertEqual(self.tools.allowed_names(cfg()), [])

    def test_config_cannot_add_new_actions(self):
        # 配置只能从登记表里**挑**名字，塞一个 rm 进去是没用的。
        names = self.tools.allowed_names(cfg(tools={"enabled": True, "allow": ["rm", "dir"]}))
        self.assertEqual(names, ["dir"])

    def test_deleting_names_are_hardcoded_forbidden(self):
        self.assertIn("rm", self.tools.FORBIDDEN)
        self.assertIn("del", self.tools.FORBIDDEN)
        self.assertIn("format", self.tools.FORBIDDEN)


class _FakeWS:
    """站一个 WS：动作发出去后，在**读循环**里回 echo（真网关就是这么干的）。"""

    def __init__(self, client: onebot.OneBotClient, answer: dict | None = None):
        self.client = client
        self.closed = False
        self.answer = answer
        self.sent: list[dict] = []

    async def send_str(self, raw: str) -> None:
        payload = json.loads(raw)
        self.sent.append(payload)
        echo = payload.get("echo")
        data = dict(self.answer or {"retcode": 0, "data": {}})
        data["echo"] = echo
        # 关键：模拟网关的 echo 也要经 `_dispatch` 回来 —— 读循环被业务堵住就永远回不来。
        asyncio.get_running_loop().call_soon(lambda: asyncio.create_task(self.client._dispatch(json.dumps(data))))


class ReadLoopTests(unittest.IsolatedAsyncioTestCase):
    """读循环不能被业务处理堵住 —— 堵住的动作全超时（真实踩过的坑）。"""

    async def _client(self, answer: dict | None = None) -> tuple[onebot.OneBotClient, _FakeWS]:
        client = onebot.OneBotClient(cfg(), lambda e: asyncio.sleep(0), print)
        client.access_token = "t"
        ws = _FakeWS(client, answer)
        client._ws = ws
        return client, ws

    async def test_handler_runs_off_the_read_loop(self):
        """处理里调动作能拿到 echo —— 说明读循环还活着。"""
        client, _ = await self._client()
        got: list[str] = []

        async def on_event(event: onebot.Event) -> None:
            result = await client.call("send_group_msg", {"group_id": 1})
            got.append(str(result.get("retcode")))

        client.on_event = on_event
        await client._dispatch(json.dumps({"post_type": "message", "message_type": "private",
                                           "user_id": "1", "message_id": "m1", "raw_message": "hi",
                                           "message": [{"type": "text", "data": {"text": "hi"}}],
                                           "self_id": "9"}))
        await asyncio.sleep(0.05)
        self.assertEqual(got, ["0"])

    async def test_ptt_text_asks_the_gateway(self):
        client, ws = await self._client({"retcode": 0, "data": {"text": "今天吃了吗"}})
        self.assertEqual(await client.ptt_text("m1"), "今天吃了吗")
        self.assertEqual(ws.sent[0]["action"], "fetch_ptt_text")
        self.assertEqual(ws.sent[0]["params"]["message_id"], "m1")

    async def test_ptt_text_empty_without_message_id(self):
        client, ws = await self._client()
        self.assertEqual(await client.ptt_text(""), "")
        self.assertEqual(ws.sent, [])       # 没有 id 就不该打扰网关

    async def test_ptt_text_falls_back_to_old_action_name(self):
        """SnowLuma 三个别名（fetch_ptt_text / get_ptt_text / get_record_text）都要试。"""
        calls: list[str] = []

        async def call(action, params=None, timeout=20.0):
            calls.append(action)
            return {"retcode": 0 if action == "get_record_text" else 1404,
                    "data": {"text": "喂" if action == "get_record_text" else ""}}

        client, _ = await self._client()
        client.call = call
        self.assertEqual(await client.ptt_text("m9"), "喂")
        self.assertEqual(calls, ["fetch_ptt_text", "get_ptt_text", "get_record_text"])


if __name__ == "__main__":
    unittest.main()