import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from qqbridge.brain import BrainClient
from qqbridge.proactive import Proactive


class ExpertModeTests(unittest.TestCase):
    def test_normal_or_uninstalled_activity_does_not_poll_plugin_api(self):
        for status in ({'enabled': False, 'autonomy_installed': True},
                       {'enabled': True, 'autonomy_installed': False}):
            brain = BrainClient()
            brain._request = Mock(return_value=status)
            self.assertTrue(brain.qq_exploration()['disabled'])
            brain._request.assert_called_once_with('/api/expert')

    def test_expert_with_plugin_reads_activity_status(self):
        brain = BrainClient()
        brain._request = Mock(side_effect=[{'enabled': True, 'autonomy_installed': True}, {'qq_notify': True}])
        self.assertEqual(brain.qq_exploration(), {'qq_notify': True})
        self.assertEqual(brain._request.call_args.args, ('/api/qq-bridge/exploration',))

    def test_disabled_activity_neither_flushes_nor_generates(self):
        controller = Proactive.__new__(Proactive)
        controller.busy = False
        controller.bridge = SimpleNamespace(brain=SimpleNamespace(qq_exploration=Mock(return_value={'disabled': True})))
        controller.flush_activities = AsyncMock()
        asyncio.run(controller.tick())
        controller.flush_activities.assert_not_awaited()
        self.assertFalse(controller.busy)
