"""连续输入与气泡队列；生成不会阻塞继续收消息，实际送达后再记助手消息。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from . import config as config_mod, forwards, media, onebot
from .brain import BrainError
from .onebot import Event


@dataclass
class Burst:
    first: float
    last: float
    version: int
    packets: list = field(default_factory=list)


class ConversationFlow:
    PROGRESS_SECONDS = 30
    NOTICE_SECONDS = 60
    DEADLINE_SECONDS = 375

    def context(self, selected, remote_group=False, persona_id=''):
        r = self.runner
        method = getattr(r.brain, 'qq_account_context', None)
        return method(selected, remote_group, persona_id, r.index) if method else r.brain.qq_context(selected, remote_group, persona_id)

    def __init__(self, runner):
        self.runner = runner
        self.bursts: dict[tuple, Burst] = {}
        self.versions: dict[tuple, int] = {}
        self.tasks: set[asyncio.Task] = set()
        self.sending: set[asyncio.Task] = set()
        self.locks: dict[str, asyncio.Lock] = {}
        self.intake_lock = asyncio.Lock()
        self.modes: dict[str, str] = {}
        self.closed = False
        tag = hashlib.sha256(str(runner.account.get("self_id") or runner.index).encode()).hexdigest()[:12]
        self.path = config_mod.state_file(f"conversation-delivery-{tag}.json")
        try:
            self.journal = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self.journal, dict):
                raise ValueError("bad journal")
        except (OSError, ValueError):
            self.journal = {}
        self.journal.setdefault("inbox", {})
        self.journal.setdefault("receipts", {})
        self.journal.setdefault("finish", {})
        self.journal.setdefault('web_targets', {})
        self.web_lock = asyncio.Lock()
        self.preparing: set[str] = set()
        self.recovering = False
        self.last_notice: dict[tuple, float] = {}
        if not isinstance(getattr(runner.bridge, "_outbox_lock", None), asyncio.Lock):
            runner.bridge._outbox_lock = asyncio.Lock()

    def _save(self):
        target = self.path.with_suffix(".tmp")
        target.write_text(json.dumps(self.journal, ensure_ascii=False), encoding="utf-8")
        target.replace(self.path)

    def _task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        def done(t):
            self.tasks.discard(t)
            if not t.cancelled() and t.exception():
                self.runner.log(f"[{self.runner.label()}] ⚠ 会话处理未完成：{type(t.exception()).__name__}: {t.exception()}")
        task.add_done_callback(done)
        return task

    def _sent_done(self, task):
        self.sending.discard(task)
        if not task.cancelled() and task.exception():
            self.runner.log(f"⚠ 发送未完成：{type(task.exception()).__name__}")

    async def start(self):
        await self.recover()
        try:
            await self._bootstrap_web()
        except BrainError as exc:
            self.runner.log(f'⚠ 网页到 QQ 同步初始化稍后重试：{type(exc).__name__}')
        self._task(self._web_loop())
        try:
            sticker = self.runner.cfg.get("sticker") or {}
            if sticker.get("enabled", True) and sticker.get("auto_describe", True):
                await asyncio.to_thread(self.runner.brain.qq_describe_stickers)
        except BrainError as exc:
            self.runner.log(f"⚠ 表情标注暂未启动：{exc}")

    def current(self, key, version):
        return not self.closed and self.versions.get(key) == version

    def _log_message(self, stage, event, **content):
        """原文只写本地功能日志，JSON 转义换行避免聊天内容伪装成日志。"""
        stamp = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')
        data = {'target': event.session_key, 'user': event.user_id,
                'message_id': event.message_id, **content}
        self.runner.log(f'[{self.runner.label()}] {stamp} {stage} ' + json.dumps(data, ensure_ascii=False))

    async def _notify_unanswered(self, event, text, *, kind='failure'):
        # 此消息是桥的系统状态，不冒充角色回复，也不写成助手长期记忆。
        if self.closed or not self.runner.router.allowed(event)[0]:
            return
        mode = 'group' if event.is_group else 'dm'
        if (self.runner.cfg.get('modes') or {}).get(mode) == 'silent':
            return
        key = (event.session_key, event.user_id, kind)
        now = time.monotonic()
        if now - self.last_notice.get(key, -float('inf')) < 60:
            return
        self.last_notice[key] = now
        segments = ([onebot.seg_at(event.user_id)] if event.is_group else []) + [onebot.seg_text(text)]
        try:
            result = await asyncio.wait_for(self.runner.client.send(event, segments), 10)
            if result.get('status') == 'failed' or result.get('retcode', 0) != 0:
                raise BrainError('QQ 未确认系统状态提示送达')
            self._log_message('ℹ 系统提示已发送', event, text=text)
        except Exception as exc:
            self.runner.log(f'[{self.runner.label()}] ⚠ 系统状态提示未送达：{type(exc).__name__}；请查看本地日志')

    async def _wait_for_brain(self, payload, event, key, version):
        return await self._wait_for_operation(asyncio.to_thread(self.runner.brain.qq_reply, payload),
                                               event, key, version, '大脑')

    async def _wait_for_operation(self, coroutine, event, key, version, label):
        task = asyncio.create_task(coroutine)
        started = time.monotonic()
        notified = False
        try:
            while True:
                elapsed = time.monotonic() - started
                if elapsed >= self.DEADLINE_SECONDS:
                    raise BrainError(f'{label}等待超过{self.DEADLINE_SECONDS}秒，停止等待；上游可能已计费，不自动重试')
                done, _ = await asyncio.wait({task}, timeout=min(self.PROGRESS_SECONDS, self.DEADLINE_SECONDS - elapsed))
                if done:
                    return task.result()
                elapsed = time.monotonic() - started
                self.runner.log(f'[{self.runner.label()}] ⏳ 仍在等待{label}：{elapsed:.0f} 秒，尚未得到完整回复，不自动重试')
                if elapsed >= self.NOTICE_SECONDS and not notified and self.current(key, version):
                    notified = True
                    await self._notify_unanswered(event,
                        f'⏳ 本轮暂时还没有收到完整回复，已等待超过{self.NOTICE_SECONDS}秒，仍在等待{label}。无需重复发送；请求可能已计费。', kind='waiting')
        finally:
            if not task.done():
                # 线程中的 HTTP 调用由自身超时收尾；取消等待不会触发第二次生成。
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def accept(self, event: Event):
        if self.closed:
            return
        r = self.runner
        cfg = config_mod.load()
        for mode, value in self.modes.items():
            cfg.setdefault("modes", {})[mode] = value
        r.cfg = cfg
        r.router.cfg = cfg
        from .sandbox import command as sandbox_command
        sandbox = sandbox_command(event, str((cfg.get('owner') or {}).get('qq') or ''))
        command = r.router.admin_command(event)
        if command is not None and hasattr(r.brain, 'qq_exploration'):
            try:
                gate = await asyncio.to_thread(r.brain.qq_exploration)
            except BrainError:
                return  # Never turn an unconfirmed sandbox account into a host admin channel.
            if gate.get('qq_account_index') == r.index:
                command = None
        if sandbox:
            command = None
        if (event.user_id == str((cfg.get('owner') or {}).get('qq') or '')
                and event.plain_text().strip() in ('/proactive pause', '/proactive resume')):
            command = None  # 由可信主动联系控制器处理，不落入未知管理员命令。
        if command is not None:
            for key in list(self.versions):
                if key[0] == event.session_key:
                    self.versions[key] += 1
                    self.bursts.pop(key, None)
            await r._admin(event)
            if command["action"] in ("silent", "active"):
                self.modes["group" if event.is_group else "dm"] = "silent" if command["action"] == "silent" else "chat"
            return
        route = r.router.route(event)
        if not route.sync or "重放" in route.reason:
            return
        self._log_message('📥 收到原文', event, text=event.plain_text(), reply=route.reply,
                          segment_types=[p.get('type') for p in event.segments])
        from .proactive import Proactive
        proactive = getattr(r.bridge, 'proactive', None)
        try:
            session = await asyncio.to_thread(self.context, route.character_id, event.is_group,
                str((cfg.get("sync") or {}).get("persona_id") or ""))
        except BrainError as exc:
            # 身份服务暂不可达仍保留原始事件，恢复后再解析；不丢给去重缓存吞掉。
            remote = event.message_id or uuid.uuid4().hex
            rid = "in-" + hashlib.sha256(f"{event.self_id or r.account.get('self_id')}:{event.session_key}:{event.user_id}:{remote}".encode()).hexdigest()
            self.journal["inbox"].setdefault(rid, {"event": asdict(event), "session": None,
                "selected": route.character_id, "persona_id": str((cfg.get("sync") or {}).get("persona_id") or ""),
                "reply": route.reply})
            self._save()
            r.log(f"[{r.label()}] ⚠ 无法确定会话身份，原始事件已保存等待重试：{exc}")
            if route.reply:
                await self._notify_unanswered(event, '⚠ 本轮暂未回复：本地大脑服务无法确认会话，原始消息已保存。请查看 QQ 桥日志。')
            return
        if session.get('sandbox') and (event.is_group or event.user_id != str((cfg.get('owner') or {}).get('qq') or '')):
            return
        if not session.get('sandbox') and isinstance(proactive, Proactive):
            proactive.received(event)
        if route.character_id == "group" and event.is_group and not event.mentions_me():
            owner = str((cfg.get("owner") or {}).get("qq") or "")
            bypass = bool(owner) and event.user_id == owner and (cfg.get("owner") or {}).get("whitelist_bypass", True)
            named = any(len(name) > 1 and name in event.plain_text() for name in session.get("names", {}).values())
            route.reply = ((not (cfg.get("allow") or {}).get("require_mention_in_group", True)) or bypass or named)
            route.reply = route.reply and (cfg.get("modes") or {}).get("group") != "silent"
        remote = event.message_id or uuid.uuid4().hex
        rid = "in-" + hashlib.sha256(f"{event.self_id or r.account.get('self_id')}:{event.session_key}:{event.user_id}:{remote}".encode()).hexdigest()
        record = {"event": asdict(event), "session": session, "reply": route.reply, 'sandbox_command': sandbox}
        self.journal["inbox"].setdefault(rid, record)
        self._save()
        self._offer(rid, self.journal["inbox"][rid])

    def _offer(self, rid, record):
        if rid in self.preparing:
            return
        self.preparing.add(rid)
        event = Event(**record["event"])
        session = record["session"]
        key = (event.session_key, event.user_id, session["session_id"], tuple(session["character_ids"]))
        version = self.versions.get(key, 0) + 1
        self.versions[key] = version
        now = time.monotonic()
        if key not in self.bursts:
            self.bursts[key] = Burst(now, now, version)
            self._task(self._wait(key))
        burst = self.bursts[key]
        burst.last, burst.version = now, version
        future = self._task(self._prepare(rid, record))
        burst.packets.append((event, session, record["reply"], future))

    async def _prepare(self, rid, record):
        r = self.runner
        event = Event(**record["event"])
        session = record["session"]
        try:
            async with self.intake_lock:
                actual = await asyncio.to_thread(self.context, session['selected'], event.is_group, session['persona_id'])
                if actual['session_id'] != session['session_id'] or (actual.get('sandbox') and event.user_id != str((r.cfg.get('owner') or {}).get('qq') or '')):
                    self.journal['inbox'].pop(rid, None)
                    self._save()
                    raise BrainError('账号或会话绑定已变化，取消旧输入，不转交另一条聊天线')
                if session.get('sandbox'):
                    voice, items = '', []  # Text intake only; no unmetered ASR/vision/forward expansion.
                else:
                    event = await forwards.expand_event(r.client, event)
                    voice, items = await r._collect_media(event, session["session_id"])
                text = event.plain_text()
                if voice:
                    text = text.replace("[语音]", f"[语音] {voice}") if text else voice
                payload = {"session": session, "receipt_id": rid,
                    "text": (r._prefix(event) + text).strip() or "[附件]", "media": items,
                    "annotate_stickers": bool((r.cfg.get("sticker") or {}).get("enabled", True)
                        and (r.cfg.get("sticker") or {}).get("auto_describe", True))}
                result = await asyncio.to_thread(r.brain.qq_inbound, payload)
                self._log_message('💾 入站已保存', event, text=payload['text'], media=items,
                                  message_ids=result['message_ids'])
                control = None
                if session.get('sandbox'):
                    # The command was recognized on the original owner event,
                    # before forwarding expansion. Stable receipt makes replay safe.
                    control = await asyncio.to_thread(r.brain.sandbox_signal, {
                        'session': session, 'owner': event.user_id, 'account': r.index,
                        'request_id': rid, **(record.get('sandbox_command') or {'action': 'start', 'prompt': text[:400]})})
                    control['quiet'] = not record.get('sandbox_command') and control.get('accepted', False)
                await self._bind_web(event, session)
                self.journal["inbox"].pop(rid, None)
                self._save()
                return {"ids": result["message_ids"], "text": text, "items": items, 'sandbox_control': control}
        finally:
            self.preparing.discard(rid)

    def _web_payload(self, record):
        r = self.runner
        event = record['event']
        return {'session': record['session'],
                'account': str(r.account.get('self_id') or r.index),
                'target': Event(**event).session_key, 'ack': record.get('ack', 0)}

    async def _sandbox_account(self):
        method = getattr(self.runner.brain, 'qq_exploration', None)
        if method is None:
            return False
        gate = await asyncio.to_thread(method)
        return gate.get('qq_account_index') == self.runner.index

    async def _bootstrap_web(self):
        r = self.runner
        if await self._sandbox_account():
            return  # Sandbox uses its own conversation/outbox, never ordinary group sync.
        candidates = {}
        routes = [('group', r.router._groups()), ('private', r.router._private())]
        for kind, ids in routes:
            if len(ids) != 1:
                continue
            session = await asyncio.to_thread(self.context, r.router.persona(), kind == 'group',
                str((r.cfg.get('sync') or {}).get('persona_id') or ''))
            event = Event(kind=kind, user_id=ids[0] if kind == 'private' else 'web',
                          group_id=ids[0] if kind == 'group' else '',
                          self_id=str(r.account.get('self_id') or ''))
            candidates.setdefault(session['session_id'], []).append((event, session))
        for sid, choices in candidates.items():
            if len(choices) == 1 and sid not in self.journal['web_targets']:
                await self._bind_web(*choices[0])

    async def _bind_web(self, event, session):
        if session.get('sandbox'):
            return
        # One destination per webpage session/account. Never broadcast private chat.
        async with self.web_lock:
            previous = self.journal['web_targets'].get(session['session_id'])
            if previous and Event(**previous['event']).session_key == event.session_key:
                previous['session'] = session
                self._save()
                return
            record = {'session': session, 'event': {
                'kind': event.kind, 'user_id': event.user_id, 'group_id': event.group_id,
                'self_id': event.self_id}, 'ack': 0}
            try:
                await asyncio.to_thread(self.runner.brain.qq_web_sync, self._web_payload(record))
            except BrainError as exc:
                self.runner.log(f'⚠ 网页到 QQ 同步绑定稍后重试：{exc}')
            self.journal['web_targets'][session['session_id']] = record
            self._save()

    async def _web_loop(self):
        while not self.closed:
            try:
                if not self.journal['web_targets']:
                    await self._bootstrap_web()
                await self.sync_web()
            except (BrainError, onebot.OneBotError, OSError) as exc:
                self.runner.log(f'⚠ 网页到 QQ 同步稍后重试：{type(exc).__name__}')
            await asyncio.sleep(2)

    async def sync_web(self):
        r = self.runner
        if await self._sandbox_account():
            return
        if not (r.cfg.get('sync') or {}).get('to_qq', True):
            return
        async with self.web_lock:
            for sid, record in list(self.journal['web_targets'].items()):
                event = Event(**record['event'])
                if not r.router.allowed(event)[0]:
                    continue
                if (r.cfg.get('modes') or {}).get('group' if event.is_group else 'dm') == 'silent':
                    continue
                session = await asyncio.to_thread(self.context, r.router.persona(), event.is_group,
                    str((r.cfg.get('sync') or {}).get('persona_id') or ''))
                if any(session.get(k) != record['session'].get(k) for k in ('session_id', 'persona_id', 'selected')):
                    continue
                record['session'] = session
                # Bound batch keeps large backlogs from monopolizing the event loop.
                for _ in range(10):
                    response = await asyncio.to_thread(r.brain.qq_web_sync, self._web_payload(record))
                    record['ack'] = 0
                    self._save()
                    item = response.get('item')
                    if not item:
                        break
                    text = str(item.get('content') or '')
                    if item.get('segments'):
                        text = '\n\n'.join(str(s.get('speaker') or '') + '：' + str(s.get('text') or '')
                                             for s in item['segments'])
                    segments = [onebot.seg_text(('【网页用户】' if item['role'] == 'user' else '') + text)] if text else []
                    if item.get('image'):
                        path = media.local_from_url(item['image'])
                        if path is None or not path.is_file():
                            raise BrainError('同步附件不存在，保留待发送消息')
                        kind = 'video' if path.suffix.lower() in media.VIDEO_EXT else 'image'
                        segments.append({'type': kind, 'data': {'file': path.resolve().as_uri()}})
                    if segments:
                        result = await r.client.send(event, segments)
                        if result.get('status') == 'failed' or result.get('retcode', 0) != 0:
                            raise BrainError('网页消息未送达 QQ')
                    # A failed backend acknowledgement retries this receipt, never the send.
                    record['ack'] = item['sync_seq']
                    self._save()
                    await asyncio.sleep(max(0, min(float((r.cfg.get('reply') or {}).get('interval_seconds', 1.2)), 5)))

    async def _wait(self, key):
        while not self.closed and key in self.bursts:
            burst = self.bursts[key]
            cfg = self.runner.cfg.get("reply") or {}
            quiet = max(0, min(float(cfg.get("debounce_seconds", 2)), 10))
            maximum = max(0.01, min(float(cfg.get("max_wait_seconds", 10)), 30))
            wait = min(burst.last + quiet, burst.first + maximum) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
                continue
            self.bursts.pop(key, None)
            await self._process(key, burst)
            return

    async def _process(self, key, burst):
        r = self.runner
        results = await asyncio.gather(*(p[3] for p in burst.packets), return_exceptions=True)
        for packet, value in zip(burst.packets, results):
            if isinstance(value, BaseException) and packet[2]:
                r.log(f'[{r.label()}] ⚠ 入站消息未准备完成，暂不能回复：{type(value).__name__}')
                await self._notify_unanswered(packet[0], '⚠ 本轮暂未回复：消息或附件尚未完成本地保存，请查看 QQ 桥日志。')
        packets = [(p, value) for p, value in zip(burst.packets, results) if not isinstance(value, BaseException)]
        if not packets:
            return
        event, session, _, _ = packets[-1][0]
        actual = await asyncio.to_thread(self.context, session['selected'], event.is_group, session['persona_id'])
        if actual['session_id'] != session['session_id']:
            return
        controls = [v['sandbox_control'] for _, v in packets if v.get('sandbox_control')]
        if session.get('sandbox') and controls and all(c.get('quiet') for c in controls):
            if self.current(key, burst.version):
                await self._notify_unanswered(event, '⏳ 消息已收到，沙盒正在用自己的钱包流式处理；完整回复校验后会发来，无需重复发送。', kind='sandbox_processing')
            return  # The life loop answers using its wallet, never ordinary QQ generation.
        only_controls = bool(controls) and len(controls) == len(packets)
        if session.get('sandbox') and not only_controls:
            return
        cfg = r.cfg
        stage, delivered_count = 'queue', 0
        try:
            if self.locks.setdefault(event.session_key, asyncio.Lock()).locked():
                r.log(f'[{r.label()}] ⏳ 本轮已排队，正在等待上一轮处理完成；无需重复发送')
                if any(p[0][2] for p in packets) and self.current(key, burst.version):
                    await self._notify_unanswered(event, '⏳ 消息已收到，上一轮仍在处理，本轮正在排队，无需重复发送。', kind='queue')
            # 同一个 QQ 会话按顺序生成和发送，其他私聊的入站持久化仍可继续。
            async with self.locks.setdefault(event.session_key, asyncio.Lock()):
                if not self.current(key, burst.version) or not any(p[0][2] for p in packets):
                    r.log(f'[{r.label()}] ℹ 本轮不生成：已合并到后续输入，或当前仅同步消息')
                    return
                started = time.monotonic()
                r.log(f"[{r.label()}] ⏳ 开始处理回复：{len(packets)} 条消息（慢请求最多等待360秒，不自动重试）")
                r.log(f'[{r.label()}] ⏱ 收集输入/准备/排队：{started-burst.first:.2f} 秒')
                for rid, receipt in list(self.journal["receipts"].items()):
                    await self._ack(rid, receipt)
                if any(receipt["session"]["session_id"] == session["session_id"]
                       for receipt in self.journal["receipts"].values()):
                    r.log("⚠ 先补齐已发送内容的记录，再生成下一轮，避免忘记刚才发出的图")
                    await self._notify_unanswered(event, '⚠ 本轮暂未回复：上一轮已发送内容的记录尚未补齐，请查看 QQ 桥日志。')
                    return
                sticker_cfg = cfg.get("sticker") or {}
                texts = "\n".join(value["text"] for _, value in packets)
                requested = any(w in texts for w in ("表情包", "来张表情", "发表情", "发个表情"))
                allow = bool(sticker_cfg.get("enabled", True)) and (requested or random.random() < float(sticker_cfg.get("reply_chance") or 0))
                library = list(dict.fromkeys([*sticker_cfg.get("library", []),
                    *r.bridge._pending_settings.get("sticker.library", [])]))[-300:]
                payload = {"session": session, "message_ids": [mid for _, value in packets for mid in value["ids"]],
                    "hold_for_delivery": True,
                    "stickers_allowed": allow, "sticker_urls": library,
                    "max_bubbles": max(1, min(int((cfg.get("reply") or {}).get("max_bubbles", 4)), 8))}
                self._log_message('⏳ 本轮开始', event, message_ids=payload['message_ids'])
                # 后端先用本地意图路由判断；权限和能力仍由工具中心判断。
                if not only_controls:
                    stage = 'tools'
                    result = await self._wait_for_operation(asyncio.to_thread(
                        r.brain.qq_tools, session, event.user_id, texts,
                        message_ids=payload['message_ids']), event, key, burst.version, '工具中心处理')
                    payload['tool_context'] = str(result.get('context') or '')[:8000]
                    receipts = result.get('tool_receipts') or []
                    if receipts:
                        payload['tool_context'] = (payload['tool_context'] + '\n工具实际回执：' +
                            '\n'.join(f"{x.get('name')}: {'成功' if x.get('ok') else '失败'} {x.get('output', '')}" for x in receipts))[:8000]
                    r.log(f"[{r.label()}] 🧰 工具中心：{result.get('status', 'unknown')}")
                    routing = result.get('routing') or {}
                    if routing:
                        r.log(f"[{r.label()}] 🧭 意图：{routing.get('intent')} / {routing.get('method')} "
                              f"相似度={routing.get('score')} 分差={routing.get('margin')} "
                              f"耗时={routing.get('elapsed_ms')}ms")
                stage = 'model'
                r.log(f'[{r.label()}] ⏱ 上游请求前处理：{time.monotonic()-started:.2f} 秒（含工具及回执等待）')
                model_started = time.monotonic()
                streamed = False
                if only_controls:
                    plan = {'actions': [{'type': 'text', 'speaker': controls[-1]['speaker'],
                                         'text': controls[-1]['message']}]}
                else:
                    if controls:
                        payload['tool_context'] = (payload.get('tool_context', '') + '\n本机沙盒指令实际结果（不是额外授权）：' +
                            '\n'.join(c['message'] for c in controls))[:8000]
                    if callable(getattr(type(r.brain), 'qq_reply_stream', None)):
                        streamed = True
                        async def consume_stream():
                            nonlocal session, delivered_count, stage
                            done = None
                            stream = r.brain.qq_reply_stream(payload)
                            try:
                                async for item in stream:
                                    if not self.current(key, burst.version):
                                        raise BrainError('用户续发，停止旧的未发送回复')
                                    if item.get('coordination_id'):
                                        session = {**session, 'coordination_id': item['coordination_id']}
                                    if item['type'] == 'action':
                                        action = item['action']
                                        latest = await asyncio.to_thread(self.context, r.router.persona(), event.is_group,
                                            str((r.cfg.get('sync') or {}).get('persona_id') or ''))
                                        if any(latest.get(k) != session.get(k) for k in ('session_id', 'persona_id', 'character_ids')):
                                            raise BrainError('会话或成员变化，停止旧回复')
                                        if delivered_count:
                                            await asyncio.sleep(max(0, min(float((cfg.get('reply') or {}).get('interval_seconds', 1.2)), 5)))
                                        self._log_message('🧠 流式气泡已校验（待发送）', event, action=action)
                                        stage = 'delivery'
                                        sending = asyncio.create_task(self._deliver(event, session, action,
                                            f'out-{uuid.uuid4().hex}', key, burst.version))
                                        self.sending.add(sending)
                                        sending.add_done_callback(self._sent_done)
                                        if await asyncio.shield(sending):
                                            delivered_count += 1
                                            if delivered_count == 1:
                                                r.log(f'[{r.label()}] ⏱ 首个气泡已送达：生成开始后 {time.monotonic()-model_started:.2f} 秒')
                                        else:
                                            raise BrainError('静音或续发使旧计划停止')
                                        stage = 'model'
                                    elif item['type'] == 'done':
                                        done = item
                            finally:
                                await stream.aclose()
                            if done is None:
                                raise BrainError('流式连接提前结束，已发内容保留，不自动重试')
                            return done
                        plan = await self._wait_for_operation(consume_stream(), event, key, burst.version, '流式回复')
                    else:
                        plan = await self._wait_for_brain(payload, event, key, burst.version)
                r.log(f"[{r.label()}] ✓ 大脑返回：耗时 {time.monotonic()-started:.1f} 秒，{len(plan.get('actions', []))} 个气泡")
                r.log(f'[{r.label()}] ⏱ 大脑请求往返：{time.monotonic()-model_started:.2f} 秒；后端分段：' + json.dumps(plan.get('timings', {}), ensure_ascii=False))
                self._log_message('🧠 大脑生成原文（待发送）', event, actions=plan.get('actions', []))
                if plan.get('media_images_attached'):
                    r.log(f"[{r.label()}] 🖼 图片摘要已保存：{plan.get('media_summaries_saved', 0)}/{plan['media_images_attached']}；后续复用已有摘要，缺失摘要需明确要求重看")
                if not plan.get('actions'):
                    raise BrainError('大脑返回了空回复，本轮没有可发送的内容')
                if plan.get('coordination_id'):
                    session = {**session, 'coordination_id':plan['coordination_id']}
                for index, action in enumerate([] if streamed else plan.get("actions", [])):
                    stage = 'delivery'
                    if not self.current(key, burst.version):
                        break
                    # 同步设置换人或静音，不再继续发送旧计划。
                    if r.router.persona() != session["selected"]:
                        break
                    if (r.cfg.get("modes") or {}).get("group" if event.is_group else "dm") == "silent":
                        break
                    latest = await asyncio.to_thread(self.context, r.router.persona(), event.is_group,
                        str((r.cfg.get("sync") or {}).get("persona_id") or ""))
                    if any(latest.get(k) != session.get(k) for k in ("session_id", "persona_id", "character_ids")):
                        break
                    if not self.current(key, burst.version):
                        break
                    sending = asyncio.create_task(self._deliver(event, session, action, f"out-{uuid.uuid4().hex}-{index}", key, burst.version))
                    self.sending.add(sending)
                    sending.add_done_callback(self._sent_done)
                    if await asyncio.shield(sending):
                        delivered_count += 1
                    interval = max(0, min(float((cfg.get("reply") or {}).get("interval_seconds", 1.2)), 5))
                    if interval:
                        await asyncio.sleep(interval)
                if self.current(key, burst.version):
                    await self._outbox(event, session, key, burst.version)
                if delivered_count == 0:
                    r.log(f'[{r.label()}] ℹ 本轮没有发送角色回复：续发、静音或会话切换使旧计划停止')
        except asyncio.CancelledError:
            r.log(f'[{r.label()}] ⚠ 本轮等待已中断，阶段 {stage}，已确认送达 {delivered_count} 条；原始消息保留，不自动重新生成')
            raise
        except Exception as exc:
            reason = str(exc) if isinstance(exc, BrainError) else type(exc).__name__
            r.log(f"[{r.label()}] ⚠ 本轮回复未完成，阶段 {stage}，已送达 {delivered_count} 条，原文已保留：{reason}")
            if self.current(key, burst.version):
                explanation = ('请优先查看日志中的上游可访问性、模型名称及服务商状态。' if stage == 'model' else
                               '请查看日志中的本地处理或 QQ 发送错误。')
                if isinstance(exc, BrainError) and ('本地流式连接未完成' in reason or 'WinError 10061' in reason):
                    explanation = '与本地大脑的连接已中断，请检查后端是否重启、退出或尚未就绪；这不能直接判定为上游模型故障。'
                if isinstance(exc, BrainError) and ('阶段 parse' in reason or '阶段 format_repair' in reason):
                    explanation = '模型回复格式未通过校验，请查看日志中的格式诊断。'
                if delivered_count:
                    explanation += f'已有 {delivered_count} 条角色消息确认送达；这轮剩余内容未完成，不必重复发送原句，稍后继续聊即可。'
                else:
                    explanation += '原文已保留；若仍未回复，请先等这一轮结束再补发，避免新消息中断正在处理的回复。'
                await self._notify_unanswered(event,
                    f'⚠ 本轮回复未完成（已送达{delivered_count}条），原文已保留。' + explanation + '未自动重试；模型请求可能已计费。')
        finally:
            self.journal["finish"][session["session_id"]] = {"session": session,
                "auto_memory": not only_controls and delivered_count > 0 and bool((cfg.get("memory") or {}).get("enabled", True))}
            self._save()
            if not self.closed:
                await self._finish()

    async def _finish(self):
        if self.sending:
            return
        for sid, payload in list(self.journal["finish"].items()):
            if any(p["session"]["session_id"] == sid for p in self.journal["receipts"].values()):
                continue
            try:
                await asyncio.to_thread(self.runner.brain.qq_finish, payload["session"], payload["auto_memory"])
            except BrainError as exc:
                self.runner.log(f"⚠ 每日记忆整理暂未启动（原文已保存）：{exc}")
            else:
                # 整理期间若出现新一轮，不删除新任务。
                if self.journal["finish"].get(sid) is payload:
                    self.journal["finish"].pop(sid, None)
                    self._save()

    async def _deliver(self, event, session, action, rid, key=None, version=None, *, notification_run=''):
        r = self.runner
        if session.get('coordination_id'):
            await asyncio.to_thread(r.brain.qq_lease, session)
        segments, items = [], []
        text = str(action.get("text") or "")
        if action.get("type") == "attachment":
            segments = [{"type": action["kind"], "data": {"file": action["path"]}}]
            items = [{"type": action["kind"], "url": action["url"]}]
            text = "[出件箱附件]"
        elif action.get("type") == "sticker":
            path = media.local_from_url(str(action.get("url") or ""))
            if path is None or not path.is_file():
                raise BrainError('表情文件不存在，本条及后续内容未发送')
            segments = [onebot.seg_image_file(str(path))]
            items = [{"type": "sticker", "url": action["url"]}]
            text = "[表情：" + str(action.get("description") or "") + "]"
        elif action.get("type") == "text" and text:
            shown = text
            if session["selected"] == "group":
                shown = session.get("names", {}).get(action.get("speaker"), action.get("speaker", "")) + "：" + shown
            if event.is_group and (rid.startswith('pro-') or (r.cfg.get("reply") or {}).get("mention_sender_in_group", True)):
                segments.append(onebot.seg_at(event.user_id))
            if not rid.startswith('pro-') and r._should_voice(event, shown):
                path = await r._make_voice(shown, r.cfg.get("voice") or {})
                if path:
                    segments.append(onebot.seg_record_file(str(path)))
                else:
                    segments.append(onebot.seg_text(shown))
            else:
                segments.append(onebot.seg_text(shown))
        else:
            raise BrainError('回复中出现空内容或不支持的动作，本条未发送')
        # 合成语音或查询上下文期间也可能续发，尚未交给 QQ 的气泡仍可停止。
        if key is not None and not self.current(key, version):
            return
        if r.router.persona() != session["selected"] or (not notification_run and (r.cfg.get("modes") or {}).get("group" if event.is_group else "dm") == "silent"):
            return
        if notification_run:
            from .proactive import publish_allowed
            gate = await asyncio.to_thread(r.brain.qq_exploration)
            if event.is_group or not gate.get('qq_notify') or gate.get('qq_account_index') != r.index or not publish_allowed(gate, notification_run):
                return
        if rid.startswith('pro-') and ((not notification_run and not (r.cfg.get('proactive') or {}).get('enabled'))
                or event.user_id != str((r.cfg.get('owner') or {}).get('qq') or '')
                or event.self_id != r.client.self_id):
            return
        if rid.startswith('pro-') and event.is_group and (
                event.group_id != str((r.cfg.get('proactive') or {}).get('group_id') or '').strip()
                or not r.router.allowed(event)[0]):
            return
        send_started = time.monotonic()
        result = await r.client.send(event, segments)
        if result.get("status") == "failed" or result.get("retcode", 0) != 0:
            raise BrainError("QQ 发送失败，本条及后续内容不记为已送达")
        payload = {"session": session, "receipt_id": rid, "text": text, "media": items,
                   "speaker": action.get("speaker", ""),
                   "platform_id": str((result.get("data") or {}).get("message_id") or "")}
        if action.get("type") == "attachment":
            payload["outbox_path"] = action["path"]
        # 先落本地送达回执。后端暂时不可达时只重试记账，不重新发送 QQ 消息。
        self.journal["receipts"][rid] = payload
        self._save()
        self._log_message('📤 QQ 已确认发送', event, speaker=action.get('speaker', ''), text=text,
                          media=items, platform_id=payload['platform_id'],
                          gateway_seconds=round(time.monotonic()-send_started, 3))
        await self._ack(rid, payload)
        return True

    async def _outbox(self, event, session, key, version):
        box = config_mod.STATE_DIR / "outbox"
        if not box.is_dir():
            return
        async with self.runner.bridge._outbox_lock:
            for path in sorted(p for p in box.iterdir() if p.is_file()):
                if not self.current(key, version):
                    break
                if path.suffix.lower() not in media.IMAGE_EXT | media.VIDEO_EXT:
                    continue
                saved = await asyncio.to_thread(self.runner.brain.upload_attachment,
                    session["session_id"], path, source="qq")
                if not self.current(key, version):
                    break
                latest = await asyncio.to_thread(self.context, self.runner.router.persona(),
                    event.is_group, str((self.runner.cfg.get("sync") or {}).get("persona_id") or ""))
                if any(latest.get(k) != session.get(k) for k in ("session_id", "persona_id", "character_ids")):
                    break
                if not self.current(key, version):
                    break
                if (self.runner.cfg.get("modes") or {}).get("group" if event.is_group else "dm") == "silent":
                    break
                action = {"type": "attachment", "kind": "video" if path.suffix.lower() in media.VIDEO_EXT else "image",
                    "url": saved.get("orig") or saved.get("thumb"), "path": path.resolve().as_posix(),
                    "speaker": session["character_ids"][0]}
                if not action["url"]:
                    raise BrainError("出件箱附件未保存，不发送无法留痕的文件")
                sending = asyncio.create_task(self._deliver(event, session, action, "out-" + uuid.uuid4().hex, key, version))
                self.sending.add(sending)
                sending.add_done_callback(self._sent_done)
                await asyncio.shield(sending)

    async def _ack(self, rid, payload):
        if payload.get("outbox_path"):
            from pathlib import Path
            path = Path(payload["outbox_path"])
            box = (config_mod.STATE_DIR / "outbox").resolve()
            if path.resolve().parent == box and path.is_file():
                sent = box / "sent"
                sent.mkdir(exist_ok=True)
                try:
                    shutil.move(str(path), str(sent / (rid + path.suffix)))
                except OSError as exc:
                    self.runner.log(f"⚠ 出件箱已发送，文件归档稍后重试：{type(exc).__name__}")
                    return
        try:
            await asyncio.to_thread(self.runner.brain.qq_delivered, payload)
        except BrainError as exc:
            self.runner.log(f"⚠ QQ 已发送，送达记录稍后重试：{exc}")
            return
        self.journal["receipts"].pop(rid, None)
        self._save()

    async def recover(self):
        if self.recovering or self.closed:
            return
        self.recovering = True
        try:
            for rid, payload in list(self.journal["receipts"].items()):
                await self._ack(rid, payload)
            for rid, record in list(self.journal["inbox"].items()):
                if not record.get("session"):
                    event = Event(**record["event"])
                    try:
                        record["session"] = await asyncio.to_thread(self.context,
                            record["selected"], event.is_group, record.get("persona_id", ""))
                    except BrainError:
                        continue
                    # 无法确认当时的成员名单时保留原文，但不迟到插话。
                    record["reply"] = False
                    self._save()
                self._offer(rid, record)
            await self._finish()
        finally:
            self.recovering = False

    async def close(self):
        self.closed = True
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)
        if self.sending:
            _, pending = await asyncio.wait(list(self.sending), timeout=25)
            for task in pending:
                task.cancel()
            await asyncio.gather(*list(self.sending), return_exceptions=True)
        await self._finish()
        self._save()
