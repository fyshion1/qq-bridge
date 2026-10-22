"""Legacy QQ tool adapter: optional network tools only; file tools were removed."""
from __future__ import annotations
import asyncio
import importlib.util
import sys
from pathlib import Path

ACTIONS = ()
WEB_ACTIONS = {'search': 'web_search'}


def runtime():
    folder = Path(__file__).resolve().parents[2] / 'tools' / 'src' / 'xiaotangren_tools'
    if not (folder / '__init__.py').is_file():
        return None
    if 'xiaotangren_tools' not in sys.modules:
        spec = importlib.util.spec_from_file_location('xiaotangren_tools', folder / '__init__.py',
                                                     submodule_search_locations=[str(folder)])
        package = importlib.util.module_from_spec(spec)
        sys.modules['xiaotangren_tools'] = package
        spec.loader.exec_module(package)
    return sys.modules['xiaotangren_tools']


def enabled(cfg):
    return bool((cfg.get('tools') or {}).get('enabled')) and runtime() is not None


def allowed_names(cfg):
    if not enabled(cfg):
        return []
    from xiaotangren_tools import web_search
    return ['search'] if web_search.available() and 'search' in ((cfg.get('tools') or {}).get('allow') or []) else []


def context_for(cfg, actor_id='', *, conversation_id='', call_id=''):
    sdk = runtime()
    if sdk is None:
        return None
    return sdk.ToolContext(actor_id=str(actor_id), owner_id=str((cfg.get('owner') or {}).get('qq') or ''),
                           conversation_id=conversation_id, call_id=call_id)


def _executor(cfg, audit=None):
    sdk = runtime()
    if sdk is None:
        raise RuntimeError('工具插件未安装')
    from xiaotangren_tools import web_search
    registry = sdk.ToolRegistry()
    registry.register(web_search.tool())
    policy = sdk.ToolPolicy(enabled=enabled(cfg), allowed_names=frozenset({'web_search'} if allowed_names(cfg) else ()),
        require_owner=bool((cfg.get('tools') or {}).get('require_owner', True)),
        owner_tools_enabled=(cfg.get('owner') or {}).get('tools') is not False, max_output_chars=4000)
    return sdk.ToolExecutor(registry, policy, **({'audit': audit} if audit else {}))


def tool_schema(cfg, context=None):
    if not allowed_names(cfg):
        return []
    executor = _executor(cfg)
    return executor.schemas(context) if context is not None else executor.registry.schemas(executor.policy.allowed_names)


def describe_for_model(cfg):
    return '可用 web_search 联网查询；查不到或失败须如实说明。' if allowed_names(cfg) else ''


async def arun(cfg, name, arguments, context=None, *, audit=None):
    if name not in ('search', 'web_search'):
        return False, '没有这个工具；文件工具已移除'
    if not allowed_names(cfg):
        return False, '工具插件未安装或联网未授权'
    result = await _executor(cfg, audit).execute('web_search', arguments, context or context_for(cfg))
    return result.ok, result.content if result.ok else f'[{result.code}] {result.content}'


def run(cfg, name, arguments, context=None):
    return asyncio.run(arun(cfg, name, arguments, context))


def command_whitelist(cfg):
    return []


def run_command(cfg, argv, cwd, timeout=20):
    raise RuntimeError('工具插件不执行命令')
