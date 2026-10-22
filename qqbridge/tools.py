"""QQ adapter for the standalone process-local tool runtime (no backend imports)."""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from xiaotangren_tools import ToolContext, ToolDenied, ToolExecutor, ToolPolicy, web_search
from xiaotangren_tools.local_files import FORBIDDEN, LocalFiles, detect_dangerous

from . import config as config_mod

ACTIONS = ("dir", "read", "find", "info")
# Config uses short names; search is the one capability that is not a local file tool.
WEB_ACTIONS = {"search": "web_search"}
__all__ = ["ACTIONS", "WEB_ACTIONS", "FORBIDDEN", "ToolDenied", "detect_dangerous", "enabled",
           "allowed_names", "sandbox_root", "safe_path", "context_for", "tool_schema",
           "describe_for_model", "arun", "run", "probe", "command_whitelist", "run_command"]


def enabled(cfg: dict) -> bool:
    return bool((cfg.get("tools") or {}).get("enabled"))


def allowed_names(cfg: dict) -> list[str]:
    if not enabled(cfg):
        return []
    wanted = [str(a).strip().lower() for a in ((cfg.get("tools") or {}).get("allow") or [])]
    return list(dict.fromkeys(name for name in wanted if name in ACTIONS or name in WEB_ACTIONS))


def full_name(name: str) -> str:
    name = str(name or "").strip().lower()
    if name in WEB_ACTIONS:
        return WEB_ACTIONS[name]
    return name if name.startswith("local_") else f"local_{name}"


def sandbox_root(cfg: dict) -> Path:
    raw = str((cfg.get("tools") or {}).get("root") or "").strip()
    return (Path(raw).expanduser() if raw else config_mod.STATE_DIR / "sandbox").resolve()


def _files(cfg: dict) -> LocalFiles:
    return LocalFiles(sandbox_root(cfg), int((cfg.get("tools") or {}).get("max_bytes") or 8000))


def safe_path(cfg: dict, raw: str) -> Path:
    return _files(cfg).safe_path(raw)


def _executor(cfg: dict, audit=None) -> ToolExecutor:
    settings = cfg.get("tools") or {}
    names = {full_name(name) for name in allowed_names(cfg)}
    registry = _files(cfg).registry()
    registry.register(web_search.tool())
    policy = ToolPolicy(
        enabled=enabled(cfg),
        allowed_names=frozenset(names),
        require_owner=bool(settings.get("require_owner", True)),
        owner_tools_enabled=(cfg.get("owner") or {}).get("tools") is not False,
        max_output_chars=4000,
    )
    return ToolExecutor(registry, policy, **({"audit": audit} if audit else {}))


def context_for(cfg: dict, actor_id: str = "", *, conversation_id: str = "",
                call_id: str = "") -> ToolContext:
    return ToolContext(actor_id=str(actor_id),
                       owner_id=str((cfg.get("owner") or {}).get("qq") or ""),
                       conversation_id=conversation_id, call_id=call_id)


def tool_schema(cfg: dict, context: ToolContext | None = None) -> list[dict]:
    executor = _executor(cfg)
    if context is not None:
        return executor.schemas(context)
    # Inspect definitions only. Execution still requires an actual actor context.
    return executor.registry.schemas(executor.policy.allowed_names)


def describe_for_model(cfg: dict) -> str:
    names = allowed_names(cfg)
    if not names:
        return ""
    local = [name for name in names if name in ACTIONS]
    parts = []
    if local:
        parts.append(f"你有本地只读工具：{'、'.join(f'local_{name}' for name in local)}。只能在允许的目录里活动")
    if "search" in names:
        parts.append("你有联网搜索工具 web_search，可以查网页、公众号、B站、YouTube、GitHub、HackerNews 的摘要；"
                     "结果只是网页摘要，查不到或失败就如实说查不到，不要编")
    return "；".join(parts) + "。拒绝、失败或超时均不代表成功，不要声称做过没做的事。"


async def arun(cfg: dict, name: str, arguments: str | dict,
               context: ToolContext | None = None, *, audit=None) -> tuple[bool, str]:
    target = full_name(name)
    try:
        result = await _executor(cfg, audit).execute(target, arguments, context or context_for(cfg))
        if result.ok:
            return True, result.content
        known = target.removeprefix("local_") in ACTIONS or target in WEB_ACTIONS.values()
        return False, ("" if known else "没有这个工具；") + f"[{result.code}] {result.content}"
    except (OSError, ValueError, TypeError):
        return False, "工具配置无效，请检查目录和读取限制"


def run(cfg: dict, name: str, arguments: str | dict,
        context: ToolContext | None = None) -> tuple[bool, str]:
    """Synchronous compatibility entry. Async adapters must await arun()."""
    return asyncio.run(arun(cfg, name, arguments, context))


def probe() -> dict:
    return {"ffmpeg": bool(shutil.which("ffmpeg")), "cwd": os.getcwd(),
            "actions": sorted(ACTIONS), "forbidden": len(FORBIDDEN)}


def command_whitelist(cfg: dict) -> list[str]:
    return []


def run_command(cfg: dict, argv: list[str], cwd: Path, timeout: int = 20) -> str:
    raise ToolDenied("工具层只提供已注册的只读文件操作，不执行命令")
