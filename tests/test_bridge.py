"""纯离线测试：白名单 / 同步与开口 / 重放去重 / 事件解析（不连真 QQ、不调大脑）。

    python -m unittest discover -s tests -v

重点测三件最容易出事、又最难靠肉眼发现的：
1. **白名单**：不该理的群 / 人绝不能理（装错了就是被拉进陌生群乱说话）。
2. **同步 vs 开口**：白名单里的群聊要同步进前端，但没 @ 我不许插嘴。
3. **重放**：网关重连重放旧消息时，不能重复回一遍。
"""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()