"""低频自主活动。仅受控 B 站工具、文档草稿、计量顾问与固定目标发布。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import Field

from . import config as config_mod
from .brain import BrainError
from .onebot import Event, OneBotError


def now_local():
    return datetime.now(timezone(timedelta(hours=8)))


def publish_allowed(gate, run_id):
    return bool(run_id and not gate.get('stopped') and not gate.get('uncertain')
                and run_id in gate.get('publishable_runs', []))





class Proactive:
    def __init__(self, bridge):
        self.bridge = bridge
        self.path = config_mod.state_file('proactive.json')
        try:
            self.state = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(self.state, dict):
                raise ValueError('invalid state')
        except (OSError, ValueError):
            self.state = {}
        self.busy = False

    def save(self):
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.state, ensure_ascii=False), encoding='utf-8')
        temp.replace(self.path)

    def received(self, event):
        owner = str((self.bridge.cfg.get('owner') or {}).get('qq') or '')
        if event.user_id != owner:
            return
        self.state['last_input'] = time.time()
        self.state['awaiting'] = False
        for runner in self.bridge.accounts:
            for key in list(runner.flow.versions):
                if key[1] == owner or key[0] == event.session_key:
                    runner.flow.versions[key] += 1
        text = event.plain_text().strip()
        if text in ('今天别打扰我', '今天不要打扰我', '暂停主动消息', '/proactive pause'):
            self.state['paused_day'] = now_local().date().isoformat()
        elif text in ('可以来找我了', '恢复主动消息', '/proactive resume'):
            self.state.pop('paused_day', None)
        self.save()

    def eligible(self, runner, settings, now, *, scheduling=True, notification=False):
        owner = str((runner.cfg.get('owner') or {}).get('qq') or '')
        if (not notification and not settings.get('enabled')) or not owner or not owner.isdigit():
            return False
        if not notification and runner.index != int(settings.get('account_index', 0)):
            return False
        if not runner.client.connected.is_set() or not runner.client.self_id or runner.client.self_id == owner:
            return False
        expected_bot = str(runner.account.get('self_id') or (runner.cfg.get('onebot') or {}).get('self_id') or '')
        if expected_bot and expected_bot != runner.client.self_id:
            return False
        if not runner.router.allowed(Event(kind='private', user_id=owner, self_id=runner.client.self_id))[0]:
            return False
        if runner.flow.closed or runner.flow.bursts or runner.flow.sending or runner.flow.journal['receipts']:
            return False
        if not notification and ((runner.cfg.get('modes') or {}).get('dm') == 'silent' or runner.flow.modes.get('dm') == 'silent'):
            return False
        if any(lock.locked() for lock in runner.flow.locks.values()):
            return False
        if notification:
            return True  # Sandbox messages have no social clock, cap or reply gate.
        day = now.date().isoformat()
        if self.state.get('day') != day:
            self.state.update(day=day, count=0, videos_today=0, opportunities=0, awaiting=False)
        if self.state.get('paused_day') == day or self.state.get('awaiting'):
            return False
        if self.state.get('count', 0) >= max(0, min(int(settings.get('daily_limit', 3)), 10)):
            return False
        if scheduling and self.state.get('opportunities', 0) >= 3:
            return False
        start, end = int(settings.get('start_hour', 10)) % 24, int(settings.get('end_hour', 23)) % 24
        in_hours = start <= now.hour < end if start < end else (now.hour >= start or now.hour < end) if start > end else False
        if not in_hours or time.time() - self.state.get('last_input', 0) < 1800:
            return False
        return True

    async def tick(self):
        if self.busy:
            return
        self.busy = True
        try:
            gate = await asyncio.to_thread(self.bridge.brain.qq_exploration)
            if gate.get('disabled'):
                return
            await self.flush_activities()
            settings = self.bridge.cfg.get('proactive') or {}
            if gate.get('qq_notify'):
                runner = next((r for r in self.bridge.accounts if r.index == gate.get('qq_account_index') and self.eligible(r, settings, now_local(), scheduling=False, notification=True)), None)
                if runner and await self.notify(runner, settings):
                    return
            if not settings.get('enabled'):
                if self.state.pop('binding', None):
                    self.save()
                return
            now = now_local()
            runner = next((r for r in self.bridge.accounts if r.index != gate.get('qq_account_index') and self.eligible(r, settings, now, scheduling=False)), None)
            if runner is None:
                return
            if not self.eligible(runner, settings, now):
                return
            stamp = time.time()
            if not self.state.get('next_at'):
                self.schedule(settings)
                return
            if stamp < self.state['next_at']:
                return
            # 不补发停机期间的旧问候，重启后重新等随机时机。
            if stamp - self.state['next_at'] > 600:
                self.schedule(settings)
                return
            self.schedule(settings)  # 失败/跳过也消耗一次机会，避免每30秒调模型。
            await self.contact(runner, settings)
        except (BrainError, OneBotError, OSError, ValueError, TypeError, TimeoutError) as exc:
            self.bridge.log(f'⚠ 主动联系本次跳过：{type(exc).__name__}')
        finally:
            self.busy = False

    def schedule(self, settings):
        low = max(30, min(int(settings.get('min_minutes', 90)), 1440))
        high = max(low, min(int(settings.get('max_minutes', 180)), 1440))
        self.state['next_at'] = time.time() + random.uniform(low, high) * 60
        self.save()

    async def browse(self, runner, interest=''):
        from .tools import runtime
        if runtime() is None:
            return []
        from xiaotangren_tools import Arguments, Tool, ToolContext, ToolExecutor, ToolPolicy, ToolRegistry
        class BrowseArguments(Arguments):
            interest: str = Field(default='', max_length=120)
        registry = ToolRegistry()
        async def handler(arguments, context):
            return json.dumps(await self._browse(runner, arguments.interest), ensure_ascii=False)
        registry.register(Tool('bilibili_candidates', '只读公开视频候选，不登录不下载', BrowseArguments,
                               handler, timeout_seconds=25))
        owner = str((runner.cfg.get('owner') or {}).get('qq') or '')
        executor = ToolExecutor(registry, ToolPolicy(enabled=bool((runner.cfg.get('proactive') or {}).get('enabled')),
            allowed_names=frozenset({'bilibili_candidates'}), max_output_chars=8000,
            owner_tools_enabled=(runner.cfg.get('proactive') or {}).get('bilibili', True)))
        result = await executor.execute('bilibili_candidates', json.dumps({'interest': interest}), ToolContext(
            actor_id=owner, owner_id=owner, conversation_id='proactive-owner', call_id=uuid.uuid4().hex))
        return json.loads(result.content) if result.ok and not result.truncated else []

    async def _browse(self, runner, interest=''):
        directive = await asyncio.to_thread(runner.brain.scout_directive)
        folder = Path(__file__).resolve().parents[2] / 'bilibili-scout'
        # 固定解释器、模块、目录；模型无路径/argv/环境变量输入权。
        env = {key: value for key, value in os.environ.items()
               if key.upper() in {'SYSTEMROOT', 'WINDIR', 'PATH', 'TEMP', 'TMP'}}
        process = await asyncio.create_subprocess_exec(sys.executable, '-I', '-X', 'utf8', '-c',
            "import runpy,sys;sys.path.insert(0,sys.argv[1]);runpy.run_module('scout.qq_candidates',run_name='__main__')",
            str(folder), cwd=str(folder), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env,
            **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
        try:
            output, _ = await asyncio.wait_for(process.communicate(json.dumps({
                'look_for': (str(directive.get('look_for') or '')[:800] + '\n角色好奇：' + str(interest)[:120]),
                'avoid': str(directive.get('avoid') or '')[:1000]}).encode()), 20)
            if process.returncode or len(output) > 16000:
                return []
            videos = json.loads(output).get('videos', [])
            seen = set(self.state.get('videos', []))
            return [v for v in videos[:3] if isinstance(v, dict) and
                    re.fullmatch(r'BV[A-Za-z0-9]{10}', str(v.get('bvid') or '')) and v['bvid'] not in seen]
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def contact(self, runner, settings):
        settings = dict(settings)
        flow = runner.flow
        owner = str((runner.cfg.get('owner') or {}).get('qq') or '')
        session = await asyncio.to_thread(runner.brain.qq_context, runner.router.persona(), False,
            str(settings.get('persona_id') or (runner.cfg.get('sync') or {}).get('persona_id') or ''))
        identity = [runner.client.self_id, owner, session['persona_id']]
        # 首次固定身份。身份/QQ切换后要显式关闭再重新配置，不沿用旧主人目标。
        binding = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        if self.state.get('binding') not in (None, binding):
            runner.log('⚠ 主动联系身份已变化，请重新绑定后启用')
            return
        self.state['binding'] = binding
        self.state['opportunities'] = self.state.get('opportunities', 0) + 1
        self.save()
        can_browse = bool(settings.get('bilibili', True)) and not self.state.get('videos_today')
        generation = self.state.get('last_input', 0)
        channels = ['private']
        group_id = str(settings.get('group_id') or '').strip()
        if await self.group_allowed(runner, group_id, owner):
            channels.append('group')
        week = now_local().strftime('%G-%V')
        if self.state.get('week') != week:
            self.state.update(week=week, qzone_count=0)
        if settings.get('qzone') and self.state.get('qzone_count', 0) < 2:
            channels.append('qzone')
        payload = {'session': session, 'can_browse': can_browse, 'run_id': uuid.uuid4().hex,
                   'step': 0, 'channels': channels, 'social_only': True}
        deadline = time.monotonic() + 600
        plan = {}
        # 次数只是额外防卡死；实际预算由本体统一预留并累计 input+output。
        for _ in range(6):
            if generation != self.state.get('last_input', 0) or time.monotonic() >= deadline:
                return
            if not (runner.cfg.get('proactive') or {}).get('enabled'):
                return
            if dict(runner.cfg.get('proactive') or {}) != settings:
                return
            plan = await asyncio.wait_for(asyncio.to_thread(runner.brain.qq_proactive, dict(payload)), 155)
            if plan.get('run_id') and plan['run_id'] != payload['run_id']:
                return
            plan['run_id'] = payload['run_id']
            payload['step'] = int(plan.get('next_step', payload['step'] + 1))
            if plan.get('intent') == 'browse' and payload['can_browse']:
                if generation != self.state.get('last_input', 0):
                    return
                payload['videos'] = await self.browse(runner, str(plan.get('interest') or '')[:120])
                payload['can_browse'] = False
            elif plan.get('intent') != 'continue':
                break
            if payload['step'] >= 6:
                return
        if plan.get('intent') != 'say' or not plan.get('actions'):
            return
        await self.publish(runner, settings, session, plan, generation, channels)

    async def notify(self, runner, settings):
        # Pure delivery: no planning/model request and no resource approval.
        # A disconnected/muted/rate-limited bridge never blocks global play.
        session = await asyncio.to_thread(runner.flow.context, runner.router.persona(), False,
            str((runner.cfg.get('sync') or {}).get('persona_id') or ''))
        owner = str((runner.cfg.get('owner') or {}).get('qq') or '')
        binding = hashlib.sha256(json.dumps([runner.client.self_id, owner, session['persona_id']]).encode()).hexdigest()
        if self.state.get('sandbox_binding') not in (None, binding):
            return False
        self.state['sandbox_binding'] = binding
        self.save()
        generation = self.state.get('last_input', 0)
        item = await asyncio.to_thread(runner.brain.exploration_claim,
                                      {'session': session, 'owner': owner, 'account': runner.index})
        if not item or not item.get('plan'):
            return False
        sent = False
        try:
            sent = bool(await self.publish(runner, dict(settings), session, item['plan'], generation, ['private'], notification=True))
        finally:
            await asyncio.to_thread(runner.brain.exploration_ack, item['run_id'], sent)
        return True

    async def publish(self, runner, settings, session, plan, generation, channels, *, notification=False):
        flow = runner.flow
        owner = str((runner.cfg.get('owner') or {}).get('qq') or '')
        group_id = str(settings.get('group_id') or '').strip()
        if ((not notification and generation != self.state.get('last_input', 0)) or not self.bound(runner, session)
                or (not notification and dict(runner.cfg.get('proactive') or {}) != settings)
                or not self.eligible(runner, settings, now_local(), scheduling=False, notification=notification)):
            return
        channel = str(plan.get('channel') or 'private')
        if channel not in channels:
            return
        gate = await asyncio.to_thread(runner.brain.qq_exploration)
        if not publish_allowed(gate, plan['run_id']) or (notification and (not gate.get('qq_notify') or gate.get('qq_account_index') != runner.index)):
            return
        if channel == 'qzone':
            return await self.publish_space(runner, settings, session, plan, generation)
        event = Event(kind='private', user_id=owner, self_id=runner.client.self_id)
        if channel == 'group':
            if not await self.group_allowed(runner, group_id, owner):
                return
            target = await asyncio.to_thread(runner.brain.qq_context, runner.router.persona(), True, session['persona_id'])
            if target['persona_id'] != session['persona_id'] or any(a.get('speaker') not in target['character_ids'] for a in plan['actions']):
                return
            target['follow_active'] = session.get('follow_active', False)
            session = target
            event = Event(kind='group', group_id=group_id, user_id=owner, self_id=runner.client.self_id)
        key = (('sandbox:' if notification else '') + event.session_key, owner, session['session_id'], tuple(session['character_ids']))
        version = flow.versions.get(key, 0) + 1
        flow.versions[key] = version
        async with flow.locks.setdefault(event.session_key, asyncio.Lock()):
            flow.journal['finish'][session['session_id']] = {'session': session,
                # 原文已按当前活动契约归档；此处不另起未计量的记忆整理模型。
                'auto_memory': False}
            flow._save()
            delivered = False
            confirmed = 0
            for index, action in enumerate(plan['actions'][:3]):
                if action.get('type') not in ('text', 'sticker'):
                    break
                latest = await asyncio.to_thread(runner.flow.context if notification else runner.brain.qq_context, runner.router.persona(), event.is_group,
                    str(((runner.cfg.get('sync') or {}).get('persona_id') if notification else settings.get('persona_id') or (runner.cfg.get('sync') or {}).get('persona_id')) or ''))
                if latest != session or (not notification and (generation != self.state.get('last_input', 0) or not flow.current(key, version))):
                    break
                if str((runner.cfg.get('owner') or {}).get('qq') or '') != owner:
                    break
                if not notification and self.state.get('paused_day') == now_local().date().isoformat():
                    break
                if (not notification and not (runner.cfg.get('proactive') or {}).get('enabled')) or not runner.client.connected.is_set():
                    break
                gate = await asyncio.to_thread(runner.brain.qq_exploration)
                if not publish_allowed(gate, plan['run_id']) or (notification and (not gate.get('qq_notify') or gate.get('qq_account_index') != runner.index)):
                    break
                # 最多3次发送尝试；发送超时也占额度，绝不自动重放主动消息。
                if not delivered:
                    if not notification:
                        self.state['count'] = self.state.get('count', 0) + 1
                        self.state['awaiting'] = True
                    if plan.get('video_id'):
                        self.state['videos_today'] = 1
                        self.state['videos'] = [*self.state.get('videos', []), plan['video_id']][-1000:]
                    self.save()
                    delivered = True
                sending = asyncio.create_task(flow._deliver(event, session, action, f"pro-sandbox-{plan['run_id']}-{index}" if notification else 'pro-' + uuid.uuid4().hex,
                    None if notification else key, None if notification else version,
                    **({'notification_run': plan['run_id']} if notification else {})))
                flow.sending.add(sending)
                sending.add_done_callback(flow._sent_done)
                if await asyncio.shield(sending):
                    confirmed += 1
                if index < len(plan['actions']) - 1:
                    await asyncio.sleep(random.uniform(1.2, 2.5))
            if delivered:
                await flow._finish()
            return confirmed == len(plan['actions'])

    async def group_allowed(self, runner, group_id, owner):
        if not group_id.isdigit() or (runner.cfg.get('modes') or {}).get('group') == 'silent' or runner.flow.modes.get('group') == 'silent':
            return False
        event = Event(kind='group', group_id=group_id, user_id=owner, self_id=runner.client.self_id)
        if not runner.router.allowed(event)[0]:
            return False
        result = await runner.client.call('get_group_member_info', {'group_id': int(group_id), 'user_id': int(owner), 'no_cache': True})
        return result.get('retcode', -1) == 0 and str((result.get('data') or {}).get('user_id') or '') == owner

    def bound(self, runner, session):
        identity = [runner.client.self_id, str((runner.cfg.get('owner') or {}).get('qq') or ''), session['persona_id']]
        return self.state.get('sandbox_binding' if session.get('sandbox') else 'binding') == hashlib.sha256(json.dumps(identity).encode()).hexdigest()

    async def publish_space(self, runner, settings, session, plan, generation):
        current = runner.cfg.get('proactive') or {}
        owner = str((runner.cfg.get('owner') or {}).get('qq') or '')
        right = int(settings.get('qzone_right', 64))
        if not current.get('enabled') or not current.get('qzone') or right not in (4, 16, 64):
            return
        if int(current.get('qzone_right', 64)) != right or generation != self.state.get('last_input', 0):
            return
        latest = await asyncio.to_thread(runner.brain.qq_context, runner.router.persona(), False,
            str(settings.get('persona_id') or (runner.cfg.get('sync') or {}).get('persona_id') or ''))
        if (latest != session or not self.bound(runner, session)
                or dict(runner.cfg.get('proactive') or {}) != settings
                or not self.eligible(runner, current, now_local(), scheduling=False)):
            return
        if any(a.get('type') != 'text' for a in plan['actions']):
            return
        text = '\n'.join(str(a.get('text') or '') for a in plan['actions'])
        if not text.strip() or len(text) > 350:
            return
        gate = await asyncio.to_thread(runner.brain.qq_exploration)
        if (not publish_allowed(gate, plan.get('run_id')) or not self.bound(runner, session)
                or generation != self.state.get('last_input', 0)
                or not runner.client.connected.is_set()
                or dict(runner.cfg.get('proactive') or {}) != settings):
            return
        params = {'content': text, 'ugc_right': right}
        if right == 16:
            params['target_uins'] = [int(owner)]
        # 先记“尝试/未知”，网关超时、进程中止、回执丢失均不自动重发。
        rid = 'space-' + uuid.uuid4().hex
        record = {'event_id': rid, 'persona_id': session['persona_id'], 'source_feature_id': 'qq_bridge',
            'revision': 1, 'occurred_at': now_local().isoformat(), 'kind': 'exploration', 'role': 'activity',
            'text': '', 'character_ids': sorted({a['speaker'] for a in plan['actions']}),
            'scope': 'personal', 'context': 'real'}
        self.state['count'] = self.state.get('count', 0) + 1
        self.state['awaiting'] = True
        self.state['qzone_count'] = self.state.get('qzone_count', 0) + 1
        self.state.setdefault('publications', {})[rid] = {'status': 'unknown', 'right': right, 'text': text,
            'persona_id': session['persona_id'], 'character_ids': record['character_ids'],
            'occurred_at': record['occurred_at'], 'bot_id': runner.client.self_id}
        self.state['publications'] = dict(list(self.state['publications'].items())[-100:])
        if plan.get('video_id'):
            self.state['videos_today'] = 1
            self.state['videos'] = [*self.state.get('videos', []), plan['video_id']][-1000:]
        self.save()
        result = await runner.client.call('send_qzone_msg', params)
        if result.get('retcode', -1) != 0 or result.get('status') == 'failed':
            runner.log('⚠ 空间发布未确认，保留记录，不重发、不改可见权限')
            return
        tid = (result.get('data') or {}).get('tid') if isinstance(result.get('data'), dict) else ''
        if not tid:
            runner.log('⚠ 空间返回缺少说说编号，结果未知，不重发')
            return
        self.state['publications'][rid].update(status='sent', tid=str(tid or ''))
        record['text'] = '已发表QQ空间感想（可见权限 ' + str(right) + '）：\n' + text
        self.state.setdefault('activity_outbox', {})[rid] = record
        self.save()
        await self.flush_activities()
        return True

    async def flush_activities(self):
        for rid, payload in list(self.state.get('activity_outbox', {}).items()):
            try:
                await asyncio.to_thread(self.bridge.brain.activity_event, payload)
            except BrainError:
                continue
            self.state['activity_outbox'].pop(rid, None)
            self.save()
