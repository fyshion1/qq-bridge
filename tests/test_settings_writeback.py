"""设置写回回归测试：离线验证配置保留与失败重试。"""

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qqbridge.brain import BrainClient, BrainError
from qqbridge.bridge import Bridge
from qqbridge.settings import SettingsSync
from qqbridge import config as config_mod


class SettingsWritebackTests(unittest.TestCase):
    def test_pull_preserves_account_table_and_rejects_unknown_settings(self):
        rows = [{'self_id': '11111', 'persona': 'group'}, {'self_id': '22222', 'persona': 'xiaoshiyin'}]
        brain = Mock()
        brain.settings.return_value = {'accounts': rows, 'allow.private': ['33333'], 'unknown.admin': True}
        with patch.object(config_mod, 'REMOTE', {}):
            sync = SettingsSync(brain, lambda _: None)
            remote = sync.pull()
            self.assertEqual(remote['accounts'], rows)
            self.assertNotIn('unknown.admin', remote)
            effective = config_mod.apply_remote(config_mod.DEFAULT)
            self.assertEqual([r['self_id'] for r in config_mod.accounts(effective)], ['11111', '22222'])

    def test_brain_prefers_explicit_overrides_over_form_defaults(self):
        brain = BrainClient()
        brain._request = Mock(return_value={
            'values': {'onebot.mode': 'client', 'allow.private': []},
            'overrides': {'onebot.mode': 'server'},
        })
        self.assertEqual(brain.settings(), {'onebot.mode': 'server'})

    def test_only_reply_waits_longer_than_backend_deadline(self):
        brain = BrainClient(timeout=120)
        brain._request = Mock(return_value={})
        brain.qq_reply({'message_ids': ['m1']})
        self.assertEqual(brain._request.call_args.kwargs['timeout'], 390)
        brain.capabilities()
        self.assertEqual(brain._request.call_args.kwargs, {})
        self.assertEqual(brain.timeout, 120)

    def test_autonomous_agent_wait_does_not_raise_resource_limits(self):
        brain = BrainClient(timeout=120)
        brain._request = Mock(return_value={})
        brain.qq_proactive({'run_id': 'test'})
        self.assertEqual(brain._request.call_args.kwargs['timeout'], 150)
        self.assertEqual(brain.timeout, 120)

    def test_save_preserves_other_settings(self):
        brain = BrainClient()
        existing = {"allow.groups": ["555"], "voice.enabled": True,
                    "sticker.library": ["old"]}
        saved = {"values": {**existing, "sticker.library": ["old", "new"]}}
        brain._request = Mock(side_effect=[{"values": existing}, saved])
        self.assertEqual(brain.save_settings({"sticker.library": ["old", "new"]}), saved)
        path = "/api/nest/features/qq_bridge/settings"
        self.assertEqual(brain._request.call_args_list[0].args, (path,))
        brain._request.assert_called_with(path, saved, method="PUT")
        self.assertEqual(existing["sticker.library"], ["old"])

    def test_read_failure_never_writes(self):
        for response in (BrainError("offline"), {}, {"values": []}):
            with self.subTest(response=response):
                brain = BrainClient()
                brain._request = Mock(side_effect=[response])
                with self.assertRaises(BrainError):
                    brain.save_settings({"sticker.library": ["new"]})
                self.assertEqual(brain._request.call_count, 1)


class BridgeWritebackTests(unittest.IsolatedAsyncioTestCase):
    def make_bridge(self, brain):
        # 不加载真实配置，也不连接 QQ；只走实际写回路径。
        bridge = Bridge.__new__(Bridge)
        bridge.brain = brain
        bridge.log = Mock()
        bridge._pending_settings = {}
        return bridge

    async def test_real_client_writeback_updates_local_settings(self):
        brain = BrainClient()
        values = {"allow.groups": ["555"], "sticker.library": ["new"]}
        brain._request = Mock(side_effect=[{"values": {"allow.groups": ["555"]}},
                                           {"values": values}])
        bridge = self.make_bridge(brain)
        bridge.queue_setting("sticker.library", ["new"])
        with patch("qqbridge.bridge.config_mod.set_remote") as set_remote:
            await bridge._flush_settings()
        self.assertEqual(bridge._pending_settings, {})
        self.assertEqual(set_remote.call_args.args[0]["sticker.library"], ["new"])

    async def test_failed_write_retries_without_stopping_bridge(self):
        brain = Mock()
        brain.save_settings.side_effect = [BrainError("offline"), {"values": {}}]
        bridge = self.make_bridge(brain)
        bridge.queue_setting("sticker.library", ["new"])
        await bridge._flush_settings()
        self.assertEqual(bridge._pending_settings, {"sticker.library": ["new"]})
        with patch("qqbridge.bridge.config_mod.set_remote"):
            await bridge._flush_settings()
        self.assertEqual(brain.save_settings.call_count, 2)
        self.assertEqual(bridge._pending_settings, {})

    async def test_newer_queued_value_survives_failed_write(self):
        brain = Mock()
        bridge = self.make_bridge(brain)

        def fail(values):
            bridge.queue_setting("sticker.library", ["newer"])
            raise BrainError("offline")

        brain.save_settings.side_effect = fail
        bridge.queue_setting("sticker.library", ["old"])
        await bridge._flush_settings()
        self.assertEqual(bridge._pending_settings, {"sticker.library": ["newer"]})
