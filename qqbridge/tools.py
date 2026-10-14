"""本地小工具：**让 QQ 里那位在划定的院子里动动手，但别搞坏东西**。

大老大批注：「不碰本地工具的话，我倒是希望他能碰一下，只要别搞坏东西」。
所以这个模块的目标不是「不给能力」，而是**给能力 + 画出硬边界**。

参考项目（Derpyu520/qq-bridge）的 closed-agent 模式是靠「不给模型工具」实现的保险 ——
我们的做法比它更彻底也更有用：给工具，但**每一次调用都要过三道闸**。

三道闸（缺一不可，全部在**本进程**里跑，模型看不见实现）：

1. **谁能触发**（`tools.require_owner`）：默认只有大老大的消息能触发。
   群里别人说「让小糖人删个文件」不算 —— 那是「提示注入」，不是授权。
2. **白名单动作**（`tools.allow`）：只认登记表里那几个。登记表是代码常量（`ACTIONS`），
   配置只能从里面**挑**，不能自己加名字 —— 所以「填个 rm -rf 进去」是不可能的。
3. **沙箱路径**（`tools.root`）：`resolve()` 之后的真实路径必须仍在这个根里。
   越界（`..` / 符号链接 / 绝对路径直接指外面）一律拒绝，**在主进程里**拒绝。

另外还有一条兜底：`detect_dangerous()` 会把删除 / 关机 / 格式化 / 改注册表这类**永远拒绝**，
不管配置怎么填 —— 这条写死在代码里，不给配置留口子。

模型这一侧：工具通过大脑的 `/api/nest/tool-turn`（§9.6.6）声明给模型，
**大脑只透传模型的调用决定，一个动作都不执行**。执行永远发生在这里。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from . import config as config_mod

# 永远拒绝的动作名 / 命令名（写死，配置改不动）。不是「危险命令的详尽清单」——
# 只是把「模型自己造一个工具名」和「白名单里混进灾难」这两条路堵上。
FORBIDDEN = {
    "rm", "del", "erase", "rd", "rmdir", "format", "diskpart", "mkfs",
    "shutdown", "reboot", "poweroff", "halt", "reg", "regedit", "sc",
    "taskkill", "kill", "wmic", "netsh", "net", "attrib", "icacls", "takeown",
    "curl", "wget", "powershell", "pwsh", "cmd", "bash", "sh", "python", "python3",
    "pip", "npm", "node", "git", "ssh", "scp", "certutil", "bitsadmin",
}

# 命令名里出现这些词也拒（防 `cat > x` / `dir /s` 这类越界写法）。
_FORBIDDEN_HINTS = ("rm -", "del ", ">", ">>", "|", "&&", ";", "`", "$(", "sudo", "chmod", "chown")


def sandbox_root(cfg: dict) -> Path:
    """小工具能碰的根目录。没配就用 `data/sandbox/`（第一次用时自动建）。"""
    raw = str(((cfg.get("tools") or {}).get("root")) or "").strip()
    root = Path(raw).expanduser() if raw else (config_mod.STATE_DIR / "sandbox")
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def detect_dangerous(text: str) -> str:
    """扫一眼这段文本里有没有「怎么配都不该让它干」的动作。返回原因，空串 = 没看出来。"""
    low = (text or "").lower()
    for hint in _FORBIDDEN_HINTS:
        if hint in low:
            return f"命令里有 {hint!r}：只允许一个单纯的只读命令，不带管道 / 重定向 / 串联"
    return ""


def _inside(root: Path, target: Path) -> bool:
    """target 是不是在 root 里（**解析真实路径之后**再判，符号链接骗不过去）。"""
    try:
        real = target.resolve()
    except OSError:
        return False
    return real == root or root in real.parents


def safe_path(cfg: dict, raw: str) -> Path:
    """把模型给的路径收进沙箱。越界就抛 `ToolDenied` —— 不返回「就近的合法路径」，
    因为「猜它想指哪儿」正是最容易出事的地方。"""
    root = sandbox_root(cfg)
    text = str(raw or "").strip().strip('"').strip("'") or "."
    if detect_dangerous(text):
        raise ToolDenied(detect_dangerous(text))
    candidate = Path(text)
    if candidate.is_absolute():
        target = candidate
    else:
        target = root / text
    if not _inside(root, target):
        raise ToolDenied(f"越界了：`{text}` 不在允许的目录里（只能碰 {root}）")
    return target


class ToolDenied(RuntimeError):
    """这个动作被拒了（越界 / 不在白名单 / 只有大老大能用）。**要如实告诉模型**，
    别换个说法硬编 —— 它得知道做不到。"""


# ---- 动作登记表（唯一真相源；配置只能从这里面挑名字）----

def _act_dir(cfg: dict, args: dict) -> str:
    target = safe_path(cfg, args.get("path") or ".")
    if not target.exists():
        raise ToolDenied(f"没有这个路径：{args.get('path')}")
    if target.is_file():
        return f"{target.name} 是个文件（{target.stat().st_size} 字节）"
    rows = []
    for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name)):
        try:
            size = item.stat().st_size
        except OSError:
            size = 0
        rows.append(f"{'📁' if item.is_dir() else '📄'} {item.name}" + (f"  ({size} 字节)" if item.is_file() else ""))
    return f"{target.relative_to(sandbox_root(cfg)) or Path('.')} 里有 {len(rows)} 项：\n" + "\n".join(rows[:80])


def _act_read(cfg: dict, args: dict) -> str:
    target = safe_path(cfg, args.get("path") or "")
    if not target.is_file():
        raise ToolDenied(f"没有这个文件：{args.get('path')}")
    limit = int((cfg.get("tools") or {}).get("max_bytes") or 8000)
    raw = target.read_bytes()[:limit * 4]
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        raise ToolDenied("这个文件读不出来（不是文本？）") from None
    if len(text) > limit:
        text = text[:limit] + f"\n…（太长，截断了，只读了前 {limit} 字）"
    return text


def _act_find(cfg: dict, args: dict) -> str:
    """按名字找文件（只读，不动任何东西）。"""
    keyword = str(args.get("name") or args.get("path") or "").strip().lower()
    if not keyword:
        raise ToolDenied("要找什么？把名字给我")
    root = sandbox_root(cfg)
    hits = [p for p in root.rglob("*") if keyword in p.name.lower()][:60]
    if not hits:
        return f"{root} 里没有名字带 `{keyword}` 的东西"
    return f"找到 {len(hits)} 个：\n" + "\n".join(
        f"{'📁' if p.is_dir() else '📄'} {p.relative_to(root)}" for p in hits)


def _act_info(cfg: dict, args: dict) -> str:
    """看一眼沙箱里有什么 + 本事边界在哪（模型想动手前可以先问这个）。"""
    root = sandbox_root(cfg)
    tools_cfg = cfg.get("tools") or {}
    return (
        f"我能碰的目录：{root}\n"
        f"能做的事：{'、'.join(str(a) for a in (tools_cfg.get('allow') or []))}\n"
        f"单次最多读 {tools_cfg.get('max_bytes')} 字\n"
        "删除 / 关机 / 联网下载 / 跑别的程序：**永远不行**（不是配置问题，是写死的）。"
    )


# name → (执行函数, 给模型看的说明, 参数 schema)
ACTIONS: dict[str, tuple] = {
    "dir": (_act_dir, "列出某个目录里有什么（只读）", {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "相对于允许目录的路径，留空 = 根目录"}},
    }),
    "read": (_act_read, "读一个文本文件的内容（只读，太长会截断）", {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "相对于允许目录的文件路径"}},
        "required": ["path"],
    }),
    "find": (_act_find, "按名字在允许目录里找文件（只读）", {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "文件名的一部分"}},
        "required": ["name"],
    }),
    "info": (_act_info, "看看自己现在能碰哪些目录、能做哪些事", {"type": "object", "properties": {}}),
}


def enabled(cfg: dict) -> bool:
    return bool((cfg.get("tools") or {}).get("enabled"))


def allowed_names(cfg: dict) -> list[str]:
    """配置里挑出来的动作名 —— **必须同时出现在 ACTIONS 里**（配置加不进新动作）。

    工具整体关着时返回空表：这样 `tool_schema()` / `describe_for_model()` 自然什么都不给,
    也不会出现「声明了工具但一调就被拒」这种自相矛盾的局面。
    """
    if not enabled(cfg):
        return []
    wanted = [str(a).strip().lower() for a in ((cfg.get("tools") or {}).get("allow") or [])]
    return [name for name in wanted if name in ACTIONS and name not in FORBIDDEN]


def tool_schema(cfg: dict) -> list[dict]:
    """给模型的工具声明（OpenAI function calling 格式）。大脑只透传，不执行。"""
    out: list[dict] = []
    for name in allowed_names(cfg):
        _, desc, params = ACTIONS[name]
        out.append({"type": "function", "function": {
            "name": f"local_{name}", "description": desc, "parameters": params,
        }})
    return out


def describe_for_model(cfg: dict) -> str:
    """塞进 system prompt 的一段话：告诉它有什么、边界在哪（省得它反复试越界）。"""
    if not enabled(cfg):
        return ""
    names = allowed_names(cfg)
    if not names:
        return ""
    return (f"你有本地小工具可用：{'、'.join(names)}。只能在大老大划定的目录里活动，"
            f"越界和删除 / 关机这类动作会被直接拒绝 —— 被拒了就如实说做不到。"
            f"想确认边界可以先调 local_info。")


def run(cfg: dict, name: str, arguments: str | dict) -> tuple[bool, str]:
    """执行一个工具调用。返回 `(成功?, 给模型看的文本)`。

    **不抛异常**：被拒也要给模型一句人话，否则它会以为「沉默 = 成功了」。
    """
    short = str(name or "").strip().lower()
    if short.startswith("local_"):
        short = short[len("local_"):]
    if not enabled(cfg):
        return False, "本地小工具没打开（大老大可以在前端「小工具」里开）"
    if short in FORBIDDEN or short not in ACTIONS:
        return False, f"没有 `{name}` 这个工具（只有 {', '.join(allowed_names(cfg)) or '无'}）"
    if short not in allowed_names(cfg):
        return False, f"`{short}` 这个工具没被允许用（大老大可以在前端「小工具」里打开）"
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
    except (json.JSONDecodeError, TypeError, ValueError):
        return False, "参数不是合法的 JSON，重试一次把参数写对"
    if not isinstance(args, dict):
        return False, "参数得是一个对象"
    try:
        return True, ACTIONS[short][0](cfg, args)
    except ToolDenied as exc:
        return False, f"做不了：{exc}"
    except (OSError, ValueError) as exc:
        return False, f"做不了：{exc.__class__.__name__}: {exc}"


def probe() -> dict:
    """自检用：这个环境里工具能不能用（不真的动任何东西）。"""
    ffmpeg = shutil.which("ffmpeg")
    return {"ffmpeg": bool(ffmpeg), "cwd": os.getcwd(),
            "actions": sorted(ACTIONS), "forbidden": len(FORBIDDEN)}


# 保留一个入口，方便以后加「按白名单跑只读命令」—— 现在**不提供**：
# 参考项目能跑任意命令是因为它整台机器都是自己的；QQ 里来的话不该有这个权力。
def command_whitelist(cfg: dict) -> list[str]:
    return [str(c) for c in ((cfg.get("tools") or {}).get("allow") or []) if str(c).lower() not in FORBIDDEN]


def run_command(cfg: dict, argv: list[str], cwd: Path, timeout: int = 20) -> str:
    """**默认关**：只有 `tools.allow` 里显式列了命令名、且 `require_owner` 为真时才到这里。

    即便如此也只允许「单个只读命令、无参数魔法」：解析后的 argv[0] 必须在白名单里，
    且整条命令行不含管道 / 重定向 / 串联（`detect_dangerous`）。cwd 锁在沙箱里。
    """
    if not enabled(cfg):
        raise ToolDenied("本地小工具没打开")
    if not argv:
        raise ToolDenied("没给命令")
    name = Path(argv[0]).name.lower()
    if name in FORBIDDEN or name not in command_whitelist(cfg):
        raise ToolDenied(f"`{name}` 不在允许的命令里")
    reason = detect_dangerous(" ".join(argv))
    if reason:
        raise ToolDenied(reason)
    if not _inside(sandbox_root(cfg), cwd):
        raise ToolDenied("工作目录越界了")
    try:
        out = subprocess.run(  # noqa: S603 - argv[0] 已过白名单，且禁了管道/重定向
            argv, cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ToolDenied(f"跑不起来：{exc}") from exc
    body = (out.stdout or "") + (("\n" + out.stderr) if out.stderr else "")
    return body.strip()[:8000]