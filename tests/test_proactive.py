"""主动联系隔离测试；不访问QQ、B站和付费模型。"""
import time
import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

import test_conversation_flow as flows
from test_bridge import event
from qqbridge.proactive import Proactive
from qqbridge.brain import BrainError


class ProactiveTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = flows.ConversationFlowTests.asyncSetUp
    patch = flows.ConversationFlowTests.patch
    drain = flows.ConversationFlowTests.drain

    async def setup_proactive(self):
        self.cfg['owner']['qq'] = '20002'
        self.cfg['proactive'] = {'enabled': True, 'bilibili': True, 'start_hour': 0, 'end_hour': 23}
        self.bridge.cfg = self.cfg
        self.bridge.accounts = [self.runner]
        self.runner.client.self_id = '10001'
        self.runner.client.connected.set()
        self.proactive = Proactive(self.bridge)
        self.bridge.proactive = self.proactive
        self.brain.exploration_claim = Mock(return_value={})
        self.brain.exploration_ack = Mock(return_value={'ok': True})
        self.brain.qq_proactive = Mock(return_value={'intent': 'say', 'actions': [
            {'type': 'text', 'text': '想找你说句话', 'speaker': 'dalaoda'}]})
        self.brain.qq_exploration = Mock(return_value={'stopped': False, 'uncertain': False})
        self.brain.qq_exploration.side_effect = lambda: {
            'publishable_runs': [self.brain.qq_proactive.call_args.args[0]['run_id']] if self.brain.qq_proactive.call_args else [],
            **self.brain.qq_exploration.return_value}
        p = patch('qqbridge.proactive.now_local', return_value=datetime(2026, 10, 19, 14, tzinfo=timezone(timedelta(hours=8))))
        p.start(); self.addCleanup(p.stop)

    async def test_first_tick_schedules_then_sends_only_to_owner_once(self):
        await self.setup_proactive()
        await self.proactive.tick()
        self.brain.qq_proactive.assert_not_called()
        self.assertGreater(self.proactive.state['next_at'], time.time())
        self.proactive.state['next_at'] = time.time() - 1
        await self.proactive.tick()
        self.runner.client.send.assert_awaited_once()
        self.assertEqual(self.runner.client.send.call_args.args[0].user_id, '20002')
        self.assertTrue(self.proactive.state['awaiting'])
        self.proactive.state['next_at'] = time.time() - 1
        await self.proactive.tick()
        self.runner.client.send.assert_awaited_once()
        self.brain.qq_delivered.assert_called_once()
        self.brain.qq_inbound.assert_not_called()
        self.assertFalse(self.brain.qq_finish.call_args.args[1])

    async def test_global_notification_precedes_random_qq_opportunity_without_model(self):
        await self.setup_proactive()
        self.proactive.state = {'day': '2026-10-19', 'opportunities': 3, 'next_at': time.time()+99999,
                                'count': 999, 'awaiting': True, 'paused_day': '2026-10-19', 'last_input': time.time()}
        plan = {'run_id': 'independent-run', 'intent': 'say', 'channel': 'private', 'actions': [
            {'type': 'text', 'speaker': 'dalaoda', 'text': '好想做完呀，去后台给我点预算嘛'}]}
        self.session['sandbox'] = True
        self.session['session_id'] = 'sandbox-dalaoda'
        self.brain.exploration_claim.return_value = {'run_id': 'independent-run', 'plan': plan}
        self.brain.qq_exploration.side_effect = None
        self.brain.qq_exploration.return_value = {'publishable_runs': ['independent-run'], 'qq_notify': True, 'qq_account_index': 0}
        self.cfg['proactive']['enabled'] = False  # entertainment off, notification still allowed
        self.cfg['proactive'].update(start_hour=10, end_hour=23, daily_limit=0)
        self.cfg['modes']['dm'] = 'silent'
        self.runner.flow.modes['dm'] = 'silent'
        with patch('qqbridge.proactive.now_local', return_value=datetime(2026, 10, 19, 3, tzinfo=timezone(timedelta(hours=8)))):
            await self.proactive.tick()
        self.brain.qq_proactive.assert_not_called()
        self.runner.client.send.assert_awaited_once()
        self.assertIn('好想做完呀', str(self.runner.client.send.call_args))
        self.brain.exploration_ack.assert_called_once_with('independent-run', True)
        self.assertEqual(self.proactive.state['count'], 999)

    async def test_owner_interjection_does_not_cancel_sandbox_message_bubbles(self):
        await self.setup_proactive()
        plan = {'run_id': 'independent-run', 'intent': 'say', 'channel': 'private', 'actions': [
            {'type': 'text', 'speaker': 'dalaoda', 'text': text} for text in ('想跟你分享进度', '还有个小问题想听听你的看法')]}
        self.session['sandbox'] = True
        self.session['session_id'] = 'sandbox-dalaoda'
        self.brain.exploration_claim.return_value = {'run_id': 'independent-run', 'plan': plan}
        self.brain.qq_exploration.side_effect = None
        self.brain.qq_exploration.return_value = {'publishable_runs': ['independent-run'], 'qq_notify': True, 'qq_account_index': 0}
        async def send(*args):
            self.proactive.state['last_input'] = time.time()
            return {'status': 'ok', 'data': {'message_id': 'sent'}}
        self.runner.client.send.side_effect = send
        with patch('qqbridge.proactive.asyncio.sleep', new=AsyncMock()):
            await self.proactive.tick()
        self.assertEqual(self.runner.client.send.await_count, 2)
        self.brain.exploration_ack.assert_called_once_with('independent-run', True)
        self.assertFalse(self.proactive.state.get('awaiting', False))

    async def test_qq_quiet_gate_does_not_claim_or_spend_global_notification(self):
        await self.setup_proactive()
        self.proactive.state.update(day='2026-10-19', count=3)
        await self.proactive.tick()
        self.brain.exploration_claim.assert_not_called()
        self.brain.qq_proactive.assert_not_called()

    async def test_quiet_hours_daily_cap_pause_recent_input_and_disconnect(self):
        await self.setup_proactive()
        now = datetime(2026, 10, 19, 14, tzinfo=timezone(timedelta(hours=8)))
        self.assertTrue(self.proactive.eligible(self.runner, self.cfg['proactive'], now))
        for changes in ({'count': 3}, {'paused_day': '2026-10-19'}, {'last_input': time.time()}, {'awaiting': True}):
            self.proactive.state = {'day': '2026-10-19', **changes}
            self.assertFalse(self.proactive.eligible(self.runner, self.cfg['proactive'], now))
        self.proactive.state = {'day': '2026-10-19'}
        self.runner.client.connected.clear()
        self.assertFalse(self.proactive.eligible(self.runner, self.cfg['proactive'], now))
        self.runner.client.connected.set()
        self.assertFalse(self.proactive.eligible(self.runner, self.cfg['proactive'], now.replace(hour=23)))

    async def test_owner_reply_invalidates_pending_and_pause_persists(self):
        await self.setup_proactive()
        self.proactive.state['awaiting'] = True
        key = ('p:20002', 'owner', 'session', ())
        self.flow.versions[key] = 1
        self.proactive.received(event(text='今天别打扰我'))
        self.assertEqual(self.flow.versions[key], 2)
        self.assertFalse(self.proactive.state['awaiting'])
        self.assertEqual(Proactive(self.bridge).state['paused_day'], '2026-10-19')
        self.proactive.received(event(text='可以来找我了'))
        self.assertNotIn('paused_day', self.proactive.state)

    async def test_pause_command_reaches_controller_before_unknown_admin_reply(self):
        await self.setup_proactive()
        await self.flow.accept(event(text='/proactive pause', message_id='pause-1'))
        await self.drain()
        self.assertEqual(self.proactive.state['paused_day'], '2026-10-19')
        self.assertTrue(all('不认识' not in str(c) for c in self.runner.client.send.call_args_list))

    async def test_owner_input_during_generation_discards_plan(self):
        await self.setup_proactive()
        def plan(payload):
            self.proactive.received(event(text='正在说话'))
            return {'intent': 'say', 'actions': [{'type': 'text', 'text': '旧想法', 'speaker': 'dalaoda'}]}
        self.brain.qq_proactive.side_effect = plan
        await self.proactive.contact(self.runner, self.cfg['proactive'])
        self.runner.client.send.assert_not_awaited()

    async def test_browse_at_most_once_and_only_link_goes_to_qq(self):
        await self.setup_proactive()
        self.proactive.browse = AsyncMock(return_value=[{'bvid': 'BV1234567890', 'title': '工具'}])
        self.brain.qq_proactive.side_effect = [{'intent': 'browse'}, {'intent': 'say', 'video_id': 'BV1234567890',
            'actions': [{'type': 'text', 'text': '看这个\nhttps://www.bilibili.com/video/BV1234567890', 'speaker': 'dalaoda'}]}]
        await self.proactive.contact(self.runner, self.cfg['proactive'])
        self.proactive.browse.assert_awaited_once()
        segments = self.runner.client.send.call_args.args[1]
        self.assertTrue(all(s['type'] == 'text' for s in segments))
        self.assertIn('BV1234567890', self.proactive.state['videos'])
        self.assertEqual(self.brain.qq_proactive.call_count, 2)

    async def test_changed_identity_blocks_and_disabled_clears_binding(self):
        await self.setup_proactive()
        self.proactive.state['binding'] = 'previous-identity'
        await self.proactive.contact(self.runner, self.cfg['proactive'])
        self.brain.qq_proactive.assert_not_called()
        self.cfg['proactive']['enabled'] = False
        await self.proactive.tick()
        self.assertNotIn('binding', self.proactive.state)

    async def test_stale_restart_schedule_does_not_catch_up(self):
        await self.setup_proactive()
        self.proactive.state['next_at'] = time.time() - 3600
        await self.proactive.tick()
        self.brain.qq_proactive.assert_not_called()
        self.assertGreater(self.proactive.state['next_at'], time.time())

    async def test_send_failure_consumes_opportunity_without_false_delivery(self):
        await self.setup_proactive()
        self.runner.client.send.return_value = {'status': 'failed', 'retcode': 1}
        with self.assertRaises(BrainError):
            await self.proactive.contact(self.runner, self.cfg['proactive'])
        self.assertEqual(self.proactive.state['count'], 1)
        self.assertTrue(self.proactive.state['awaiting'])
        self.brain.qq_delivered.assert_not_called()

    async def test_browse_uses_readonly_runtime_and_no_model_argv(self):
        await self.setup_proactive()
        self.proactive._browse = AsyncMock(return_value=[{'bvid': 'BV1234567890', 'title': '工具'}])
        rows = await self.proactive.browse(self.runner)
        self.assertEqual(rows[0]['bvid'], 'BV1234567890')
        self.proactive._browse.assert_awaited_once()
        self.cfg['proactive']['bilibili'] = False
        self.assertEqual(await self.proactive.browse(self.runner), [])
        self.proactive._browse.assert_awaited_once()

    async def test_scout_subprocess_is_fixed_and_killed_on_timeout(self):
        await self.setup_proactive()
        self.brain.scout_directive = Mock(return_value={'look_for': '工具', 'avoid': '带货'})
        process = Mock(returncode=None)
        process.communicate = AsyncMock(side_effect=TimeoutError)
        process.wait = AsyncMock()
        with patch('qqbridge.proactive.asyncio.create_subprocess_exec', AsyncMock(return_value=process)) as spawn:
            with self.assertRaises(TimeoutError):
                await self.proactive._browse(self.runner)
        self.assertIn('-I', spawn.call_args.args)
        self.assertTrue(any('scout.qq_candidates' in arg for arg in spawn.call_args.args))
        self.assertNotIn('shell', spawn.call_args.kwargs)
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()

    async def test_document_and_consult_continuations_keep_one_run_and_do_not_send_drafts(self):
        await self.setup_proactive()
        self.brain.qq_proactive.side_effect = [
            {'intent': 'continue', 'next_step': 2}, {'intent': 'continue', 'next_step': 3},
            {'intent': 'say', 'next_step': 4, 'actions': [{'type': 'text', 'speaker': 'dalaoda', 'text': '今天想偷个懒。'}]}]
        await self.proactive.contact(self.runner, self.cfg['proactive'])
        calls = self.brain.qq_proactive.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertEqual([c.args[0]['step'] for c in calls], [0, 2, 3])
        self.assertEqual(len({c.args[0]['run_id'] for c in calls}), 1)
        self.runner.client.send.assert_awaited_once()
        self.assertNotIn('今天想偷个懒', str(self.brain.qq_inbound.call_args_list))

    async def test_global_budget_gate_blocks_even_a_valid_generated_share(self):
        await self.setup_proactive()
        self.brain.qq_exploration.return_value = {'uncertain': True}
        await self.proactive.contact(self.runner, self.cfg['proactive'])
        self.runner.client.send.assert_not_awaited()

    async def test_revoked_project_blocks_already_generated_share(self):
        await self.setup_proactive()
        self.brain.qq_exploration.return_value = {'publishable_runs': []}
        await self.proactive.contact(self.runner, self.cfg['proactive'])
        self.runner.client.send.assert_not_awaited()

    async def test_three_skipped_opportunities_also_stop_for_day(self):
        await self.setup_proactive()
        now = datetime(2026, 10, 19, 14, tzinfo=timezone(timedelta(hours=8)))
        self.proactive.eligible(self.runner, self.cfg['proactive'], now)
        self.brain.qq_proactive.return_value = {'intent': 'skip'}
        for _ in range(3):
            await self.proactive.contact(self.runner, self.cfg['proactive'])
        self.assertFalse(self.proactive.eligible(self.runner, self.cfg['proactive'], now))
        self.assertEqual(self.proactive.state['opportunities'], 3)
        self.runner.client.send.assert_not_awaited()

    async def test_group_sharing_requires_membership_and_uses_native_owner_at(self):
        await self.setup_proactive()
        self.cfg['proactive']['group_id'] = '555'
        self.cfg['reply']['mention_sender_in_group'] = False
        self.runner.client.call = AsyncMock(return_value={'retcode': 0, 'data': {'user_id': 20002}})
        self.session['follow_active'] = True
        def context(selected, group, persona):
            result = copy.deepcopy(self.session)
            result.update(remote_group=group, follow_active=not bool(persona))
            if group:
                result.update(session_id='group', mode='group')
            return result
        self.brain.qq_context.side_effect = context
        self.brain.qq_proactive.return_value.update(channel='group')
        await self.proactive.contact(self.runner, self.cfg['proactive'])
        self.runner.client.send.assert_awaited_once()
        target, segments = self.runner.client.send.call_args.args
        self.assertEqual(target.group_id, '555')
        self.assertEqual(segments[0], {'type': 'at', 'data': {'qq': '20002'}})
        self.assertEqual(self.brain.qq_delivered.call_args.args[0]['session']['session_id'], 'group')

    async def test_missing_membership_does_not_fallback_to_other_channel(self):
        await self.setup_proactive()
        self.cfg['proactive']['group_id'] = '555'
        self.runner.client.call = AsyncMock(return_value={'retcode': 1})
        self.brain.qq_proactive.return_value.update(channel='group')
        await self.proactive.contact(self.runner, self.cfg['proactive'])
        self.runner.client.send.assert_not_awaited()
        self.assertNotIn('group', self.brain.qq_proactive.call_args.args[0]['channels'])

    async def test_space_has_explicit_visibility_and_uses_activity_not_fake_chat(self):
        await self.setup_proactive()
        self.cfg['proactive'].update(qzone=True, qzone_right='16')
        self.runner.client.call = AsyncMock(return_value={'retcode': 0, 'data': {'tid': 'real-tid'}})
        self.brain.activity_event = Mock(return_value={'seq': 1})
        self.brain.qq_proactive.return_value.update(channel='qzone')
        await self.proactive.contact(self.runner, self.cfg['proactive'])
        action, params = self.runner.client.call.call_args.args
        self.assertEqual(action, 'send_qzone_msg')
        self.assertEqual(params['ugc_right'], 16)
        self.assertEqual(params['target_uins'], [20002])
        self.assertNotIn('images', params)
        self.runner.client.send.assert_not_awaited()
        self.brain.qq_delivered.assert_not_called()
        self.assertEqual(self.brain.activity_event.call_args.args[0]['role'], 'activity')
        self.assertFalse(self.proactive.state['activity_outbox'])

    async def test_space_failure_never_retries_or_widens_audience(self):
        await self.setup_proactive()
        self.cfg['proactive'].update(qzone=True, qzone_right=64)
        self.runner.client.call = AsyncMock(return_value={'retcode': -1, 'status': 'failed'})
        self.brain.qq_proactive.return_value.update(channel='qzone')
        await self.proactive.contact(self.runner, self.cfg['proactive'])
        await self.proactive.tick()
        self.runner.client.call.assert_awaited_once()
        self.assertEqual(self.proactive.state['qzone_count'], 1)
        self.assertTrue(self.proactive.state['awaiting'])
        self.assertEqual(next(iter(self.proactive.state['publications'].values()))['status'], 'unknown')

    async def test_public_space_permission_is_rejected(self):
        await self.setup_proactive()
        self.cfg['proactive'].update(qzone=True, qzone_right=1)
        self.runner.client.call = AsyncMock()
        self.brain.qq_proactive.return_value.update(channel='qzone')
        await self.proactive.contact(self.runner, self.cfg['proactive'])
        self.runner.client.call.assert_not_awaited()

    async def test_space_ack_without_id_is_not_saved_as_success(self):
        await self.setup_proactive()
        self.cfg['proactive'].update(qzone=True, qzone_right=64)
        self.runner.client.call = AsyncMock(return_value={'retcode': 0, 'data': {}})
        self.brain.activity_event = Mock()
        self.brain.qq_proactive.return_value.update(channel='qzone')
        await self.proactive.contact(self.runner, self.cfg['proactive'])
        self.assertEqual(next(iter(self.proactive.state['publications'].values()))['status'], 'unknown')
        self.brain.activity_event.assert_not_called()

    async def test_owner_group_message_stops_pending_activity(self):
        await self.setup_proactive()
        self.proactive.state['awaiting'] = True
        self.proactive.received(event(group='555', text='今天别打扰我'))
        self.assertEqual(self.proactive.state['paused_day'], '2026-10-19')
        self.assertFalse(self.proactive.state['awaiting'])


if __name__ == '__main__':
    unittest.main()
