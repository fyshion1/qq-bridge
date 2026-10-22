"""Owner-only explicit wake phrases; forwarded content cannot trigger controls."""
import re


def command(event, owner):
    if not owner or event.user_id != owner:
        return None
    if any(s.get('type') not in ('text', 'at') or (s.get('type') == 'at' and str((s.get('data') or {}).get('qq')) != event.self_id)
           for s in event.segments):
        return None
    text = ''.join(str((s.get('data') or {}).get('text', '')) for s in event.segments if s.get('type') == 'text').strip()
    match = re.fullmatch(r'/sandbox(?:\s+(start|continue|pause|status))?(?:\s+(.{1,400}))?', text, re.S | re.I)
    if match:
        return {'action': {'continue': 'start'}.get((match[1] or 'start').lower(), (match[1] or 'start').lower()), 'prompt': match[2] or ''}
    match = re.fullmatch(r'(去玩玩沙盒|去沙盒玩玩|继续玩沙盒|继续探索沙盒|启动沙盒|探索沙盒|玩沙盒)(?:吧|呀|啦)?[！!。.]?(?:[，,:：\s]+(.{1,400}))?', text, re.S)
    if match:
        return {'action': 'start', 'prompt': match[2] or ''}
    if text in ('暂停沙盒', '沙盒休息', '停止沙盒'):
        return {'action': 'pause', 'prompt': ''}
    if text in ('沙盒状态', '沙盒在干嘛', '沙盒在干什么'):
        return {'action': 'status', 'prompt': ''}
    return None
