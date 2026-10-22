"""Sandbox account intake never invokes ordinary chat or host tools."""
import copy
import unittest
from unittest.mock import Mock
import test_conversation_flow as fixture
from test_bridge import event
from qqbridge.sandbox import command


class SandboxFlowTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ConversationFlowTests.asyncSetUp
    patch = fixture.ConversationFlowTests.patch
    drain = fixture.ConversationFlowTests.drain

    def bind(self):
        self.cfg['owner']['qq'] = '20002'
        self.runner.index = 1
        self.session.update(sandbox=True, session_id='sandbox-dalaoda')
        self.brain.qq_account_context = Mock(side_effect=lambda selected, group, persona, account: copy.deepcopy(self.session))
        self.brain.qq_exploration = Mock(return_value={'qq_account_index': 1})
        self.brain.sandbox_signal = Mock(return_value={'accepted': True, 'speaker': 'dalaoda', 'message': '已记下，使用已有额度', 'action': 'start'})

    async def test_normal_owner_text_only_wakes_life_no_shared_model_or_media(self):
        self.bind()
        await self.flow.accept(event(text='今天想做些什么呀？'))
        await self.drain()
        self.brain.sandbox_signal.assert_called_once()
        self.assertEqual(self.brain.sandbox_signal.call_args.args[0]['account'], 1)
        self.brain.qq_reply.assert_not_called()
        self.runner._collect_media.assert_not_awaited()
        self.runner.client.send.assert_awaited_once()
        self.assertIn('流式处理', str(self.runner.client.send.call_args))
        self.assertFalse(self.flow.journal['web_targets'])

    async def test_sandbox_skips_ordinary_web_sync_even_when_groups_are_configured(self):
        self.bind()
        self.cfg['allow']['groups'] = ['555']
        await self.flow._bootstrap_web()
        await self.flow.sync_web()
        self.brain.qq_account_context.assert_not_called()
        self.brain.qq_web_sync.assert_not_called()
        self.runner.client.send.assert_not_awaited()

    async def test_explicit_command_ack_and_unfunded_message_are_deterministic(self):
        self.bind()
        self.brain.sandbox_signal.return_value.update(accepted=False, message='独立钱包不足，请到前端审批')
        await self.flow.accept(event(text='去玩玩沙盒，看看蓝色大肥鱼'))
        await self.drain()
        self.assertEqual(self.brain.sandbox_signal.call_args.args[0]['prompt'], '看看蓝色大肥鱼')
        self.brain.qq_reply.assert_not_called()
        self.assertIn('独立钱包不足', str(self.runner.client.send.call_args))
        self.assertFalse(self.brain.qq_finish.call_args.args[1])

    async def test_old_account_stays_normal(self):
        self.cfg['owner']['qq'] = '20002'
        self.brain.sandbox_signal = Mock()
        await self.flow.accept(event(text='去玩玩沙盒'))
        await self.drain()
        self.brain.sandbox_signal.assert_not_called()
        self.brain.qq_reply.assert_called_once()

    async def test_new_account_never_routes_to_host_admin_or_stranger(self):
        self.bind()
        self.runner._admin = Mock()
        await self.flow.accept(event(text='/unknown-admin-command'))
        await self.drain()
        self.runner._admin.assert_not_called()
        self.brain.qq_reply.assert_not_called()
        self.cfg['allow']['private'].append('30003')
        self.brain.qq_inbound.reset_mock()
        await self.flow.accept(event(text='我也来指挥你', user='30003', message_id='stranger'))
        await self.drain()
        self.brain.qq_inbound.assert_not_called()

    async def test_forward_and_other_person_cannot_be_control_commands(self):
        self.assertEqual(command(event(text='/sandbox pause'), '20002')['action'], 'pause')
        self.assertIsNone(command(event(text='/sandbox pause', user='30003'), '20002'))
        forwarded = event(text='/sandbox pause')
        forwarded.segments.append({'type': 'forward', 'data': {'id': 'forward'}})
        self.assertIsNone(command(forwarded, '20002'))
