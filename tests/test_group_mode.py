"""群聊模式复用本体会话，避免假角色与重复记忆写入。"""
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_bridge import cfg, event, FakeBrain
from qqbridge.bridge import AccountRunner
from qqbridge.sessions import Router


class GroupModeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = cfg(persona={"character_id": "group"})
        self.brain = FakeBrain([{"id": "a", "name": "甲角色"}, {"id": "b", "name": "乙角色"}])
        self.brain.group_session = Mock(return_value={"character_ids": ["a"],
            "session_id": "dalaoda@group", "persona_id": "dalaoda"})
        self.brain.group_turn = Mock(return_value={"assistant": {"content": "**甲角色**：我在"}})
        bridge = Mock(brain=self.brain, log=Mock())
        self.runner = AccountRunner(bridge, self.cfg, {}, 0)
        self.runner._collect_media = AsyncMock(return_value=("", []))
        self.runner._send = AsyncMock()
        self.runner._sync_inbound = AsyncMock()
        self.runner.router.remember = Mock()
        self.load = patch('qqbridge.bridge.config_mod.load', return_value=self.cfg)
        self.load.start()
        self.addCleanup(self.load.stop)

    async def test_private_message_uses_group_session_without_double_write(self):
        route = self.runner.router.route(event(message_id='route-only'))
        self.assertEqual((route.frontend_session, route.session_kind), ('group', 'group'))
        await self.runner.handle(event(message_id='group-private'))
        args = self.brain.group_turn.call_args
        self.assertEqual(args.args[0]['session_id'], 'dalaoda@group')
        self.runner._sync_inbound.assert_not_awaited()
        self.runner.router.remember.assert_not_called()
        self.runner._send.assert_awaited_once()

    async def test_only_present_names_trigger_reply(self):
        await self.runner.handle(event(group='555', text='乙角色在吗', message_id='absent'))
        self.brain.group_turn.assert_not_called()
        await self.runner.handle(event(group='555', text='甲角色在吗', message_id='present'))
        self.brain.group_turn.assert_called_once()

    async def test_silent_mode_stays_silent(self):
        self.cfg['modes']['group'] = 'silent'
        await self.runner.handle(event(group='555', mention=True, message_id='silent-group'))
        self.brain.group_turn.assert_not_called()
        self.runner._sync_inbound.assert_awaited_once()
        self.runner.router.remember.assert_not_called()

    async def test_switch_back_to_character_clears_cached_group(self):
        self.assertEqual(self.runner.router.persona(), 'group')
        changed = cfg(persona={'character_id': 'a'})
        self.runner.apply(changed, {})
        self.assertEqual(self.runner.router.persona(), 'a')
        route = self.runner.router.route(event(message_id='single-again'))
        self.assertEqual(route.frontend_session, 'single-a')
