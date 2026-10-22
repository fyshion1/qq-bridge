"""Live account settings must change connections, not just routing labels."""
import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from test_bridge import cfg, FakeBrain
from qqbridge import config as config_mod
from qqbridge.bridge import Bridge, AccountRunner
from qqbridge.conversation import ConversationFlow


class AccountLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_basic_policy_caps_connections_and_pauses_pending_selection(self):
        updated = copy.deepcopy(self.cfg)
        updated['accounts'].append({'self_id': '22222', 'ws_url': 'ws://127.0.0.1:3101/'})
        updated['bridge'] = {'basic_mode': True}
        await self.bridge.sync_accounts(updated)
        self.assertEqual(len(self.bridge.accounts), 1)
        updated['bridge']['selection_required'] = True
        await self.bridge.sync_accounts(updated)
        self.assertEqual(self.bridge.accounts, [])

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(config_mod, 'STATE_DIR', Path(self.tmp.name))
        p.start(); self.addCleanup(p.stop)
        self.cfg = cfg(persona={'character_id': 'group'}, accounts=[
            {'self_id': '11111', 'persona': 'group', 'ws_url': 'ws://127.0.0.1:3001/'}])
        p = patch.object(config_mod, 'load', return_value=self.cfg)
        p.start(); self.addCleanup(p.stop)
        for name, replacement in [('run_forever', AsyncMock(side_effect=self.wait)), ('stop', Mock())]:
            p = patch('qqbridge.bridge.OneBotClient.' + name, new=replacement)
            p.start(); self.addCleanup(p.stop)
        for cls, name, replacement in [(ConversationFlow, 'start', AsyncMock()), (ConversationFlow, 'close', AsyncMock()),
                                       (AccountRunner, 'tick', Mock()), (AccountRunner, 'startup_log', AsyncMock())]:
            p = patch.object(cls, name, new=replacement)
            p.start(); self.addCleanup(p.stop)
        self.bridge = Bridge(brain=FakeBrain(), logger=lambda _: None)
        self.bridge.build()
        await self.bridge._start_account(self.bridge.accounts[0])

    async def wait(self):
        await asyncio.Event().wait()

    async def asyncTearDown(self):
        for runner in self.bridge.accounts:
            await self.bridge._stop_account(runner)

    async def test_new_account_connects_without_interrupting_original(self):
        old = self.bridge.accounts[0]
        old_task = self.bridge._client_tasks[old]
        updated = copy.deepcopy(self.cfg)
        updated['accounts'].append({'self_id': '22222', 'persona': '', 'ws_url': 'ws://127.0.0.1:3101/'})
        await self.bridge.sync_accounts(updated)
        self.assertEqual(len(self.bridge.accounts), 2)
        self.assertIs(self.bridge.accounts[0], old)
        self.assertIs(self.bridge._client_tasks[old], old_task)
        self.assertFalse(old_task.done())
        new = self.bridge.accounts[1]
        self.assertEqual(new.client.ws_url, 'ws://127.0.0.1:3101/')
        self.assertEqual(new.router.persona(), 'dalaoda')  # Blank means first role, never inherited group.
        self.assertIn(new, self.bridge._client_tasks)

    async def test_connection_change_and_account_removal_stop_obsolete_clients(self):
        old = self.bridge.accounts[0]
        old_task = self.bridge._client_tasks[old]
        updated = copy.deepcopy(self.cfg)
        updated['accounts'][0]['ws_url'] = 'ws://127.0.0.1:3201/'
        await self.bridge.sync_accounts(updated)
        self.assertTrue(old_task.done())
        self.assertNotIn(old, self.bridge._client_tasks)
        self.assertEqual(self.bridge.accounts[0].client.ws_url, 'ws://127.0.0.1:3201/')
        current = self.bridge.accounts[0]
        changed = copy.deepcopy(updated)
        changed['accounts'].append({'self_id': '22222'})
        await self.bridge.sync_accounts(changed)
        removed = self.bridge.accounts[1]
        removed_task = self.bridge._client_tasks[removed]
        await self.bridge.sync_accounts(updated)
        self.assertEqual(self.bridge.accounts, [current])
        self.assertTrue(removed_task.done())
        self.assertNotIn(removed, self.bridge._client_tasks)
