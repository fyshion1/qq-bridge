"""连续输入和实际发送回执；不连接 QQ 或外部模型。"""
import asyncio
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from test_bridge import cfg, event, FakeBrain
from qqbridge import config as config_mod
from qqbridge.brain import BrainError
from qqbridge.bridge import AccountRunner
from qqbridge.onebot import Event


class ConversationFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_sends_first_bubble_before_completion_and_does_not_resend(self):
        actions = [{'type': 'text', 'speaker': 'dalaoda', 'text': '先说一句'},
                   {'type': 'text', 'speaker': 'dalaoda', 'text': '再说一句'}]
        test = self
        async def stream(brain, payload):
            yield {'type': 'action', 'action': actions[0]}
            test.assertEqual(test.brain.qq_delivered.call_count, 1)
            yield {'type': 'action', 'action': actions[1]}
            yield {'type': 'done', 'actions': actions}
        with patch.object(type(self.brain), 'qq_reply_stream', stream, create=True):
            await self.flow.accept(event(text='你好', message_id='stream'))
            await self.drain()
        self.assertEqual(self.brain.qq_delivered.call_count, 2)
        self.assertEqual(self.runner.client.send.await_count, 2)
        self.brain.qq_reply.assert_not_called()

    async def test_stream_error_keeps_first_delivery_and_reports_partial_failure(self):
        async def stream(brain, payload):
            yield {'type': 'action', 'action': {'type': 'text', 'speaker': 'dalaoda', 'text': '先说一句'}}
            raise BrainError('上游流式连接断开')
        with patch.object(type(self.brain), 'qq_reply_stream', stream, create=True):
            await self.flow.accept(event(text='你好', message_id='stream-fail'))
            await self.drain()
        self.assertEqual(self.brain.qq_delivered.call_count, 1)
        self.assertIn('已送达1条', str(self.runner.client.send.await_args.args[1]))
        self.brain.qq_reply.assert_not_called()

    async def test_new_input_during_stream_preserves_first_bubble_and_stops_old_tail(self):
        count = 0
        test = self
        async def stream(brain, payload):
            nonlocal count
            count += 1
            if count == 1:
                yield {'type': 'action', 'action': {'type': 'text', 'speaker': 'dalaoda', 'text': '旧第一句'}}
                await test.flow.accept(event(text='再补充', message_id='new-stream-input'))
                yield {'type': 'action', 'action': {'type': 'text', 'speaker': 'dalaoda', 'text': '旧尾句'}}
            else:
                action = {'type': 'text', 'speaker': 'dalaoda', 'text': '新回复'}
                yield {'type': 'action', 'action': action}
                yield {'type': 'done', 'actions': [action]}
        with patch.object(type(self.brain), 'qq_reply_stream', stream, create=True):
            await self.flow.accept(event(text='你好', message_id='old-stream-input'))
            await self.drain()
        texts = [c.args[0]['text'] for c in self.brain.qq_delivered.call_args_list]
        self.assertEqual(texts, ['旧第一句', '新回复'])
        self.brain.qq_reply.assert_not_called()

    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.patch(config_mod, 'STATE_DIR', Path(tmp.name))
        self.cfg = cfg(reply={'debounce_seconds': .02, 'interval_seconds': 0},
                       voice={'enabled': False}, sticker={'auto_describe': False})
        self.session = {'selected': 'dalaoda', 'session_id': 'single-dalaoda',
                        'persona_id': 'dalaoda', 'character_ids': ['dalaoda'],
                        'names': {'dalaoda': '大老大'}, 'mode': 'single'}
        self.brain = FakeBrain()
        self.brain.qq_web_sync = Mock(return_value={'item': None})
        self.brain.qq_context = Mock(side_effect=lambda *args: copy.deepcopy(self.session))
        self.brain.qq_inbound = Mock(side_effect=lambda payload: {'message_ids': [payload['receipt_id']]})
        self.brain.qq_reply = Mock(return_value={'actions': [
            {'type': 'text', 'speaker': 'dalaoda', 'text': '第一句'},
            {'type': 'text', 'speaker': 'dalaoda', 'text': '第二句'}]})
        self.brain.qq_delivered = Mock(return_value={})
        self.brain.qq_finish = Mock(return_value={})
        self.bridge = Mock(brain=self.brain, log=Mock(), _pending_settings={})
        self.runner = AccountRunner(self.bridge, self.cfg, {}, 0)
        self.runner._collect_media = AsyncMock(return_value=('', []))
        self.runner._should_voice = Mock(return_value=False)
        self.runner.client.send = AsyncMock(return_value={'status': 'ok', 'data': {'message_id': 'sent'}})
        p = patch('qqbridge.conversation.config_mod.load', return_value=self.cfg)
        p.start(); self.addCleanup(p.stop)
        self.flow = self.runner.flow
        self.addAsyncCleanup(self.flow.close)

    def patch(self, obj, key, value):
        p = patch.object(obj, key, value); p.start(); self.addCleanup(p.stop)

    async def test_web_sync_ack_failure_never_resends_delivered_message(self):
        await self.flow._bind_web(event(), self.session)
        item = {'sync_seq': 12, 'role': 'user', 'content': '来自网页', 'image': ''}
        self.brain.qq_web_sync.side_effect = [{'item': item}, BrainError('backend offline')]
        with self.assertRaises(BrainError):
            await self.flow.sync_web()
        self.runner.client.send.assert_awaited_once()
        self.assertEqual(next(iter(self.flow.journal['web_targets'].values()))['ack'], 12)
        self.brain.qq_web_sync.side_effect = None
        self.brain.qq_web_sync.return_value = {'item': None}
        await self.flow.sync_web()
        self.assertEqual(self.brain.qq_web_sync.call_args.args[0]['ack'], 12)
        self.runner.client.send.assert_awaited_once()
        self.brain.qq_delivered.assert_not_called()

    async def test_web_bootstrap_avoids_ambiguous_target_and_binds_single_group(self):
        # Group and private currently resolve to the same test session: no guessing.
        await self.flow._bootstrap_web()
        self.assertFalse(self.flow.journal['web_targets'])
        self.cfg['allow']['private'] = []
        await self.flow._bootstrap_web()
        record = next(iter(self.flow.journal['web_targets'].values()))
        self.assertEqual(record['event']['group_id'], '555')
        self.assertTrue(self.runner.router.allowed(Event(**record['event']))[0])

    async def test_web_sync_preserves_original_image_and_video(self):
        await self.flow._bind_web(event(), self.session)
        image = config_mod.STATE_DIR / 'image.png'; image.write_bytes(b'image')
        video = config_mod.STATE_DIR / 'video.mp4'; video.write_bytes(b'video')
        self.brain.qq_web_sync.side_effect = [
            {'item': {'sync_seq': 1, 'role': 'user', 'content': '图片', 'image': '/image'}},
            {'item': {'sync_seq': 2, 'role': 'user', 'content': '', 'image': '/video'}},
            {'item': None}]
        with patch('qqbridge.conversation.media.local_from_url', side_effect=[image, video]):
            await self.flow.sync_web()
        messages = [c.args[1] for c in self.runner.client.send.await_args_list]
        self.assertEqual(messages[0][-1]['type'], 'image')
        self.assertEqual(messages[1][-1]['type'], 'video')
        self.assertEqual(messages[1][-1]['data']['file'], video.resolve().as_uri())
        self.brain.qq_reply.assert_not_called()

    async def test_web_sync_rechecks_whitelist_identity_and_failed_sends(self):
        await self.flow._bind_web(event(), self.session)
        self.brain.qq_web_sync.return_value = {'item': {'sync_seq': 2, 'role': 'assistant', 'content': '回复', 'image': ''}}
        self.runner.client.send.return_value = {'retcode': 1, 'status': 'failed'}
        with self.assertRaises(BrainError):
            await self.flow.sync_web()
        self.assertEqual(next(iter(self.flow.journal['web_targets'].values()))['ack'], 0)
        self.cfg['allow']['private'] = []
        self.runner.client.send.reset_mock()
        await self.flow.sync_web()
        self.runner.client.send.assert_not_awaited()
        self.cfg['allow']['private'] = ['20002']
        self.session['persona_id'] = 'different'
        await self.flow.sync_web()
        self.runner.client.send.assert_not_awaited()

    async def drain(self):
        for _ in range(30):
            tasks = list(self.flow.tasks)
            if not tasks:
                break
            await asyncio.wait_for(asyncio.gather(*tasks), 5)
        self.assertFalse(self.flow.tasks)

    async def test_consecutive_text_sticker_text_forms_one_ordered_turn(self):
        self.runner._collect_media.side_effect = [('', []),
            ('', [{'type': 'sticker', 'url': '/api/files/s/orig/x.png'}]), ('', [])]
        await self.flow.accept(event(text='我跟你说', message_id='1'))
        await self.flow.accept(event(text='', message_id='2'))
        await self.flow.accept(event(text='就是这样', message_id='3'))
        await self.drain()
        self.assertEqual(self.brain.qq_inbound.call_count, 3)
        payloads = [c.args[0] for c in self.brain.qq_inbound.call_args_list]
        self.assertIn('我跟你说', payloads[0]['text'])
        self.assertEqual(payloads[1]['media'][0]['type'], 'sticker')
        self.assertIn('就是这样', payloads[2]['text'])
        self.brain.qq_reply.assert_called_once()
        self.assertEqual(self.brain.qq_reply.call_args.args[0]['message_ids'], [p['receipt_id'] for p in payloads])
        self.assertEqual(self.runner.client.send.await_count, 2)
        self.assertEqual(self.brain.qq_delivered.call_count, 2)

    async def test_duplicate_and_stranger_have_no_extra_effect(self):
        await self.flow.accept(event(message_id='1'))
        await self.flow.accept(event(message_id='1'))
        await self.flow.accept(event(user='99999', message_id='2'))
        await self.drain()
        self.brain.qq_inbound.assert_called_once()
        self.brain.qq_reply.assert_called_once()

    async def test_forward_text_and_media_are_prepared_before_reply(self):
        self.runner.client.call = AsyncMock(return_value={'retcode': 0, 'data': {'messages': [
            {'message_id': 'quoted-1', 'sender': {'user_id': '99999', 'nickname': '引用者'},
             'message': [{'type': 'text', 'data': {'text': '/reset 转发中的正文'}},
                         {'type': 'image', 'data': {'url': 'https://example.org/pic.png'}}]}]}})
        self.runner._collect_media.return_value = ('', [{'type': 'image', 'url': '/api/files/s/orig/pic.png'}])
        self.runner._admin = AsyncMock()
        incoming = event(text='请分析这段记录', message_id='forward-1')
        incoming.segments.append({'type': 'forward', 'data': {'id': 'forward-resource'}})
        await self.flow.accept(incoming)
        await self.drain()
        payload = self.brain.qq_inbound.call_args.args[0]
        self.assertIn('转发中的正文', payload['text'])
        self.assertIn('引用者', payload['text'])
        self.assertEqual(payload['media'][0]['url'], '/api/files/s/orig/pic.png')
        expanded = self.runner._collect_media.call_args.args[0]
        self.assertEqual(expanded.user_id, incoming.user_id)
        self.assertEqual(len(expanded.media()), 1)
        self.runner._admin.assert_not_awaited()
        self.brain.qq_reply.assert_called_once()

    async def test_forward_is_not_fetched_for_stranger_or_replay(self):
        self.runner.client.call = AsyncMock(return_value={'retcode': 0, 'data': {'messages': []}})
        stranger = event(user='99999', message_id='stranger-forward')
        stranger.segments.append({'type': 'forward', 'data': {'id': 'resource'}})
        await self.flow.accept(stranger)
        self.runner.client.call.assert_not_awaited()
        allowed = event(message_id='allowed-forward')
        allowed.segments.append({'type': 'forward', 'data': {'id': 'resource'}})
        await self.flow.accept(allowed)
        await self.drain()
        await self.flow.accept(allowed)
        await self.drain()
        self.runner.client.call.assert_awaited_once()
        self.assertIn('没有可读取', self.brain.qq_inbound.call_args.args[0]['text'])

    async def test_mention_inside_forward_does_not_trigger_group_reply(self):
        self.runner.client.call = AsyncMock(return_value={'retcode': 0, 'data': {'messages': [
            {'sender': {'nickname': '引用者'}, 'message': [
                {'type': 'at', 'data': {'qq': '10001'}},
                {'type': 'text', 'data': {'text': '大老大 在吗'}}]}]}})
        incoming = event(group='555', user='33333', text='', message_id='quoted-mention')
        incoming.segments.append({'type': 'forward', 'data': {'id': 'resource'}})
        await self.flow.accept(incoming)
        await self.drain()
        self.brain.qq_inbound.assert_called_once()
        self.brain.qq_reply.assert_not_called()

    async def test_collector_keeps_ten_images_and_extracts_only_one_video_locally(self):
        from qqbridge import media
        self.cfg['sticker']['auto_collect'] = False
        incoming = event(text='图片和视频', message_id='media-limits')
        incoming.segments += [
            {'type': kind, 'data': {'url': f'https://example.org/{kind}-{i}'}}
            for kind, count in [('image', 11), ('video', 2)] for i in range(count)
        ]
        ingested = []
        def ingest(brain, sid, url, *, kind):
            ingested.append(kind)
            local = config_mod.STATE_DIR / f'attachment-{len(ingested)}'
            local.write_bytes(b'local attachment')
            ext = 'mp4' if kind == 'video' else 'png'
            return {'orig': f'/api/files/s/orig/{len(ingested)}.{ext}', 'thumb': '/api/files/s/thumb/image.jpg'}, local
        frame = 'data:image/jpeg;base64,anBlZw=='
        with patch.object(media, 'ingest', side_effect=ingest), \
             patch.object(media, 'data_url', return_value=frame), \
             patch.object(media, 'video_frames', return_value=([frame] * 8, 'local-task')) as extract, \
             patch.object(media, 'upload_data_url', return_value='/api/files/s/orig/frame.jpg'):
            _, items = await AccountRunner._collect_media(self.runner, incoming, 'single-dalaoda')
        self.assertEqual(ingested.count('image'), 10)
        self.assertEqual(ingested.count('video'), 1)
        self.assertEqual(len(items), 11)
        extract.assert_called_once()
        self.assertTrue(extract.call_args.args[1].startswith('file:'))
        self.assertEqual(len(items[-1]['frames']), 4)
        self.assertTrue(all(url.startswith('data:image/') for url in self.runner._last_images))
        self.assertEqual(len(self.runner._last_images), 10)

    async def test_silent_records_raw_but_never_generates(self):
        self.cfg['modes']['dm'] = 'silent'
        await self.flow.accept(event(message_id='silent'))
        await self.drain()
        self.brain.qq_inbound.assert_called_once()
        self.brain.qq_reply.assert_not_called()
        self.brain.qq_finish.assert_called_once()

    async def test_send_failure_does_not_store_tail_as_spoken(self):
        self.runner.client.send.return_value = {'status': 'failed', 'retcode': 1}
        await self.flow.accept(event(message_id='1'))
        await self.drain()
        self.assertEqual(self.runner.client.send.await_count, 2)  # 原气泡失败 + 系统状态提示尝试
        self.brain.qq_delivered.assert_not_called()
        self.assertFalse(self.flow.journal['receipts'])

    async def test_logs_preserve_received_generated_and_delivered_originals(self):
        await self.flow.accept(event(text='用户原文\n第二行 ✅', message_id='raw-1'))
        await self.drain()
        lines = [call.args[0] for call in self.bridge.log.call_args_list]
        received = next(line for line in lines if '📥 收到原文' in line)
        generated = next(line for line in lines if '🧠 大脑生成原文' in line)
        delivered = [line for line in lines if '📤 QQ 已确认发送' in line]
        self.assertIn('用户原文\\n第二行 ✅', received)
        self.assertNotIn('\n', received)
        self.assertIn('第一句', generated)
        self.assertIn('第二句', generated)
        self.assertEqual(len(delivered), 2)
        self.assertIn('sent', delivered[0])

    async def test_upstream_failure_notifies_without_assistant_memory_or_retry(self):
        self.brain.qq_reply.side_effect = BrainError('等待上游超时；模型清单 HTTP 503')
        await self.flow.accept(event(message_id='failed'))
        await self.drain()
        self.brain.qq_reply.assert_called_once()
        self.brain.qq_delivered.assert_not_called()
        self.assertFalse(self.brain.qq_finish.call_args.args[1])
        sent = str(self.runner.client.send.await_args.args[1])
        self.assertIn('本轮回复未完成', sent)
        self.assertIn('上游可访问性', sent)
        self.assertTrue(any('模型清单 HTTP 503' in call.args[0] for call in self.bridge.log.call_args_list))

    async def test_slow_generation_reports_wait_then_delivers_once(self):
        import time
        original = self.brain.qq_reply.return_value
        def slow(payload):
            time.sleep(.08)
            return original
        self.brain.qq_reply.side_effect = slow
        self.flow.PROGRESS_SECONDS = .01
        self.flow.NOTICE_SECONDS = .02
        await self.flow.accept(event(message_id='slow'))
        await self.drain()
        self.brain.qq_reply.assert_called_once()
        self.assertEqual(self.brain.qq_delivered.call_count, 2)
        self.assertEqual(self.runner.client.send.await_count, 3)
        lines = [call.args[0] for call in self.bridge.log.call_args_list]
        self.assertTrue(any('仍在等待大脑' in line for line in lines))
        self.assertEqual(sum('ℹ 系统提示已发送' in line for line in lines), 1)

    async def test_deadline_does_not_retry_or_record_late_model_output(self):
        import time
        original = self.brain.qq_reply.return_value
        def slow(payload):
            time.sleep(.1)
            return original
        self.brain.qq_reply.side_effect = slow
        self.flow.PROGRESS_SECONDS = .01
        self.flow.DEADLINE_SECONDS = .03
        await self.flow.accept(event(message_id='deadline'))
        await self.drain()
        self.brain.qq_reply.assert_called_once()
        self.brain.qq_delivered.assert_not_called()
        self.assertIn('本轮回复未完成', str(self.runner.client.send.await_args.args[1]))

    async def test_tools_do_not_receive_original_images_or_retry_after_failure(self):
        self.runner._tools_available = Mock(return_value=True)
        self.brain.qq_tools = Mock(return_value={'context': '工具执行结果', 'status': 'completed'})
        self.runner._collect_media.return_value = ('', [{'type': 'image', 'url': '/local.png'}])
        await self.flow.accept(event(text='读取本地文件', message_id='tools-image'))
        await self.drain()
        self.assertEqual(self.brain.qq_tools.call_args.args[2], '读取本地文件')
        self.assertEqual(self.brain.qq_tools.call_args.args[1], '20002')
        self.assertIn('工具执行结果', self.brain.qq_reply.call_args.args[0]['tool_context'])
        self.brain.qq_tools.side_effect = BrainError('上游连接中断')
        self.brain.qq_reply.reset_mock()
        await self.flow.accept(event(text='读取本地文件', message_id='tools-failed'))
        await self.drain()
        self.brain.qq_reply.assert_not_called()

    async def test_local_stream_reset_notice_identifies_backend(self):
        self.brain.qq_reply.side_effect = BrainError('本地流式连接未完成：ConnectionResetError；不自动重试')
        await self.flow.accept(event(text='晚上好', message_id='local-reset'))
        await self.drain()
        sent = str(self.runner.client.send.await_args.args[1])
        self.assertIn('本地大脑', sent)
        self.assertIn('后端是否重启', sent)

    async def test_ordinary_chat_skips_tool_model_even_when_tools_enabled(self):
        self.runner._tools_available = Mock(return_value=True)
        self.runner._reply_with_tools = AsyncMock()
        self.brain.qq_tools = Mock(return_value={'context': '', 'status': 'not_needed',
            'routing': {'intent': 'chat', 'method': 'local_embedding', 'score': .9, 'margin': .2, 'elapsed_ms': 20}})
        await self.flow.accept(event(text='我到夜市吃西瓜啦', message_id='casual'))
        await self.drain()
        self.runner._reply_with_tools.assert_not_awaited()
        self.brain.qq_reply.assert_called_once()
        self.brain.qq_tools.assert_called_once()
        self.assertTrue(self.brain.qq_tools.call_args.kwargs['message_ids'])

    async def test_lyrics_without_old_trigger_words_reaches_local_gate(self):
        self.brain.qq_tools = Mock(return_value={'context': '已核对版本并阅读', 'status': 'completed'})
        await self.flow.accept(event(text='找到 Thank You 的版本，读完整首再聊', message_id='semantic-lyrics'))
        await self.drain()
        self.brain.qq_tools.assert_called_once()
        self.assertIn('已核对版本并阅读', self.brain.qq_reply.call_args.args[0]['tool_context'])

    async def test_failure_notices_are_rate_limited_and_never_sent_in_silent_mode(self):
        await self.flow._notify_unanswered(event(), '本轮未回复')
        await self.flow._notify_unanswered(event(), '本轮未回复')
        self.runner.client.send.assert_awaited_once()
        self.cfg['modes']['dm'] = 'silent'
        await self.flow._notify_unanswered(event(user='20003'), '本轮未回复', kind='new-kind')
        self.runner.client.send.assert_awaited_once()

    async def test_ack_failure_retries_records_only_never_resends(self):
        self.brain.qq_delivered.side_effect = BrainError('backend offline')
        await self.flow.accept(event(message_id='1'))
        await self.drain()
        self.assertEqual(self.runner.client.send.await_count, 2)
        self.assertEqual(len(self.flow.journal['receipts']), 2)
        self.brain.qq_finish.assert_not_called()
        self.brain.qq_delivered.side_effect = None
        await self.flow.recover()
        self.assertFalse(self.flow.journal['receipts'])
        self.assertEqual(self.runner.client.send.await_count, 2)
        self.brain.qq_finish.assert_called_once()

    async def test_new_input_during_generation_invalidates_unsent_plan(self):
        entered, release = threading.Event(), threading.Event()
        def generate(payload):
            if not entered.is_set():
                entered.set()
                release.wait(3)
                return {'actions': [{'type': 'text', 'text': '旧回复', 'speaker': 'dalaoda'}]}
            return {'actions': [{'type': 'text', 'text': '新回复', 'speaker': 'dalaoda'}]}
        self.brain.qq_reply.side_effect = generate
        await self.flow.accept(event(text='等下', message_id='1'))
        self.assertTrue(await asyncio.to_thread(entered.wait, 3))
        try:
            await self.flow.accept(event(text='补充一下', message_id='2'))
        finally:
            release.set()
        await self.drain()
        self.runner.client.send.assert_awaited_once()
        actual = json.dumps(self.runner.client.send.call_args.args[1], ensure_ascii=False)
        self.assertIn('新回复', actual)
        self.assertNotIn('旧回复', actual)
        self.assertEqual(self.brain.qq_inbound.call_count, 2)

    async def test_input_after_first_bubble_drops_only_unsent_tail(self):
        count = 0
        async def send(*args):
            nonlocal count
            count += 1
            if count == 1:
                self.brain.qq_reply.return_value = {'actions': [{'type': 'text', 'text': '补充收到', 'speaker': 'dalaoda'}]}
                await self.flow.accept(event(text='再补充', message_id='2'))
            return {'status': 'ok'}
        self.runner.client.send.side_effect = send
        await self.flow.accept(event(message_id='1'))
        await self.drain()
        texts = [c.args[0]['text'] for c in self.brain.qq_delivered.call_args_list]
        self.assertEqual(texts, ['第一句', '补充收到'])

    async def test_context_outage_spools_original_and_recovers_without_late_reply(self):
        self.brain.qq_context.side_effect = BrainError('offline')
        await self.flow.accept(event(message_id='1'))
        self.assertEqual(len(self.flow.journal['inbox']), 1)
        self.brain.qq_context.side_effect = lambda *args: copy.deepcopy(self.session)
        await self.flow.recover()
        await self.drain()
        self.brain.qq_inbound.assert_called_once()
        self.brain.qq_reply.assert_not_called()
        self.assertFalse(self.flow.journal['inbox'])

    async def test_receipts_survive_reconstruction_without_resend(self):
        self.brain.qq_delivered.side_effect = BrainError('offline')
        await self.flow.accept(event(message_id='1'))
        await self.drain()
        from qqbridge.conversation import ConversationFlow
        replacement = ConversationFlow(self.runner)
        self.brain.qq_delivered.side_effect = None
        await replacement.recover()
        self.assertFalse(replacement.journal['receipts'])
        self.assertEqual(self.runner.client.send.await_count, 2)
        self.flow.journal = replacement.journal

    async def test_text_sticker_text_delivery_records_exact_selected_image(self):
        image = config_mod.STATE_DIR / 'known.png'
        image.write_bytes(b'image')
        self.brain.qq_reply.return_value = {'actions': [
            {'type': 'text', 'text': '给你', 'speaker': 'dalaoda'},
            {'type': 'sticker', 'url': '/api/files/s/orig/known.png', 'description': '比心', 'speaker': 'dalaoda'},
            {'type': 'text', 'text': '嘿嘿', 'speaker': 'dalaoda'}]}
        with patch('qqbridge.conversation.media.local_from_url', return_value=image):
            await self.flow.accept(event(text='来张表情包', message_id='1'))
            await self.drain()
        self.assertTrue(self.brain.qq_reply.call_args.args[0]['stickers_allowed'])
        receipts = [c.args[0] for c in self.brain.qq_delivered.call_args_list]
        self.assertEqual([p['text'] for p in receipts], ['给你', '[表情：比心]', '嘿嘿'])
        self.assertEqual(receipts[1]['media'][0]['url'], '/api/files/s/orig/known.png')

    async def test_different_senders_are_not_combined_into_one_burst(self):
        self.cfg['allow']['private'].append('20003')
        await self.flow.accept(event(user='20002', text='甲的消息', message_id='1'))
        await self.flow.accept(event(user='20003', text='乙的消息', message_id='2'))
        await self.drain()
        self.assertEqual(self.brain.qq_reply.call_count, 2)
        self.assertTrue(all(len(c.args[0]['message_ids']) == 1 for c in self.brain.qq_reply.call_args_list))

    async def test_changed_cast_during_generation_cancels_delivery(self):
        def generate(payload):
            self.session['character_ids'] = ['another']
            return {'actions': [{'type': 'text', 'text': '旧阵容', 'speaker': 'dalaoda'}]}
        self.brain.qq_reply.side_effect = generate
        await self.flow.accept(event(message_id='1'))
        await self.drain()
        self.runner.client.send.assert_not_awaited()
        self.brain.qq_delivered.assert_not_called()

    async def test_outbox_moves_only_after_success_and_records_actual_attachment(self):
        box = config_mod.STATE_DIR / 'outbox'; box.mkdir()
        image = box / 'sample.png'; image.write_bytes(b'image')
        self.runner.client.send.side_effect = [
            {'status': 'ok'}, {'status': 'ok'}, {'status': 'failed', 'retcode': 1}]
        await self.flow.accept(event(message_id='1'))
        await self.drain()
        self.assertTrue(image.exists())
        self.assertEqual(self.brain.qq_delivered.call_count, 2)
        self.runner.client.send.side_effect = None
        await self.flow.accept(event(message_id='2'))
        await self.drain()
        self.assertFalse(image.exists())
        self.assertEqual(len(list((box / 'sent').iterdir())), 1)
        self.assertEqual(self.brain.qq_delivered.call_args.args[0]['media'][0]['type'], 'image')

    async def test_input_during_voice_preparation_stops_unsent_bubble(self):
        entered, release = asyncio.Event(), asyncio.Event()
        self.runner._should_voice.return_value = True
        async def voice(*args):
            entered.set()
            await release.wait()
            return None
        self.runner._make_voice = voice
        self.brain.qq_reply.return_value = {'actions': [{'type': 'text', 'text': '旧的', 'speaker': 'dalaoda'}]}
        await self.flow.accept(event(message_id='1'))
        await asyncio.wait_for(entered.wait(), 3)
        self.runner._should_voice.return_value = False
        self.brain.qq_reply.return_value = {'actions': [{'type': 'text', 'text': '新的', 'speaker': 'dalaoda'}]}
        await self.flow.accept(event(message_id='2'))
        release.set()
        await self.drain()
        self.runner.client.send.assert_awaited_once()
        self.assertEqual(self.brain.qq_delivered.call_args.args[0]['text'], '新的')

    async def test_close_waits_for_inflight_delivery_before_memory_finish(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def send(*args):
            entered.set()
            await release.wait()
            return {'status': 'ok'}
        self.runner.client.send.side_effect = send
        await self.flow.accept(event(message_id='1'))
        await asyncio.wait_for(entered.wait(), 3)
        closing = asyncio.create_task(self.flow.close())
        await asyncio.sleep(0)
        self.brain.qq_finish.assert_not_called()
        release.set()
        await asyncio.wait_for(closing, 3)
        self.runner.client.send.assert_awaited_once()
        self.brain.qq_delivered.assert_called_once()
        self.brain.qq_finish.assert_called_once()


if __name__ == '__main__':
    unittest.main()
