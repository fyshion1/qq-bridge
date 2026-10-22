"""Real runtime + fake model, without network requests or real QQ messages."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from qqbridge import config as config_mod
from qqbridge import tools
from qqbridge.bridge import AccountRunner
from test_bridge import FakeBrain, cfg, event


class ToolRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        state = patch.object(config_mod, "STATE_DIR", self.root)
        state.start()
        self.addCleanup(state.stop)
        self.cfg = cfg(owner={"qq": "20002", "tools": True}, tools={
            "enabled": True, "require_owner": True, "root": str(self.root),
            "allow": ["search"], "max_turns": 3,
        })
        from xiaotangren_tools import web_search
        search = patch.object(web_search, 'search', return_value='buy milk')
        self.search = search.start(); self.addCleanup(search.stop)
        installed = patch.object(web_search, 'available', return_value=True)
        installed.start(); self.addCleanup(installed.stop)
        self.brain = FakeBrain()
        self.log = Mock()
        self.runner = AccountRunner(Mock(brain=self.brain, log=self.log), self.cfg, {}, 0)

    async def test_owner_is_checked_even_when_bypassing_bridge_guard(self):
        for actor in ("", "stranger"):
            context = tools.context_for(self.cfg, actor)
            self.assertEqual(tools.tool_schema(self.cfg, context), [])
            ok, output = await tools.arun(self.cfg, "web_search", {"query": "news"}, context)
            self.assertFalse(ok)
            self.assertIn("denied", output)
        ok, _ = await tools.arun(self.cfg, "web_search", {"query": "news"})
        self.assertFalse(ok)

    async def test_owner_disabled_at_executor(self):
        self.cfg["owner"]["tools"] = False
        ok, _ = await tools.arun(self.cfg, "web_search", {}, tools.context_for(self.cfg, "20002"))
        self.assertFalse(ok)

    async def test_legacy_names_and_strict_arguments(self):
        context = tools.context_for(self.cfg, "20002")
        ok, text = await tools.arun(self.cfg, "search", {"query": "news"}, context)
        self.assertTrue(ok)
        self.assertEqual(text, "buy milk")
        for args in ({}, {"query": 3}, {"query": "news", "shell": True}):
            ok, text = await tools.arun(self.cfg, "web_search", args, context)
            self.assertFalse(ok)
            self.assertIn("invalid_arguments", text)

    async def test_model_call_execution_result_and_final_reply(self):
        turns_seen = []

        def tool_turn(character, turns, **kwargs):
            turns_seen.append(json.loads(json.dumps(turns)))
            if len(turns_seen) == 1:
                self.assertEqual(kwargs["tools"][0]["function"]["name"], "web_search")
                return {"tool_calls": [{"id": "c1", "name": "web_search",
                                        "arguments": '{"query":"news"}'}]}
            self.assertEqual(turns[-1], {"role": "tool", "tool_call_id": "c1", "content": "buy milk"})
            self.assertEqual(turns[-2]["tool_calls"][0]["function"]["name"], "web_search")
            return {"content": "Remember to buy milk", "tool_calls": []}

        self.brain.tool_turn = tool_turn
        incoming = event(text="read note.txt")
        route = self.runner.router.route(incoming)
        reply = await self.runner._reply_with_tools(route, incoming,
                                                    [{"role": "user", "content": "read note.txt"}], [])
        self.assertEqual(reply, "Remember to buy milk")
        self.assertEqual(len(turns_seen), 2)
        logs = " ".join(str(call) for call in self.log.call_args_list)
        self.assertIn("tool_call", logs)
        self.assertNotIn("buy milk", logs)
        self.assertNotIn("note.txt", logs)

    async def test_rejected_call_returns_explicit_tool_error(self):
        calls = []

        def tool_turn(character, turns, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                return {"tool_calls": [{"id": "bad", "name": "web_search",
                                        "arguments": '{"query":3}'}]}
            self.assertIn("invalid_arguments", turns[-1]["content"])
            return {"content": "Access denied", "tool_calls": []}

        self.brain.tool_turn = tool_turn
        incoming = event()
        reply = await self.runner._reply_with_tools(self.runner.router.route(incoming), incoming,
                                                    [{"role": "user", "content": "read"}], [])
        self.assertEqual(reply, "Access denied")

    async def test_existing_turn_limit_stops_repeated_calls(self):
        self.brain.tool_turn = Mock(return_value={"tool_calls": [
            {"id": "again", "name": "web_search", "arguments": "{}"}]})
        incoming = event()
        await self.runner._reply_with_tools(self.runner.router.route(incoming), incoming,
                                           [{"role": "user", "content": "loop"}], [])
        self.assertEqual(self.brain.tool_turn.call_count, 3)


if __name__ == "__main__":
    unittest.main()
