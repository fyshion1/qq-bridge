"""命令行入口：`python -m qqbridge <命令>`（在小窝前端点「跑起来」也是这个）。

    run     常驻：连 QQ 网关，收消息、回消息、同步进小窝（**平时就用这个**）
    check   自检：大脑通不通、要连几个号、白名单空不空、工具与识别是什么状态（不连 QQ，不花 token）
    whoami  这份配置里每个号演谁 + 家里有哪些人（人选来自大脑的角色介绍）
    example 写出 config.example.json（配置模板，别把 config.json 提交上去）

**改配置不用开终端**：白名单 / 演谁 / 音色 / 表情包 / 小工具都在小窝前端
「💬 QQ 桥设置」里改（存本体，桥每 30 秒拉一次，改完立刻生效）。这个 CLI 主要留给
「大脑没起也要检查一下」「想把默认值写进文件」这两种情形。

⚠ Windows 终端中文乱码：设 `PYTHONIOENCODING=utf-8`（本体的运行器已经帮你设了）。
"""

from __future__ import annotations

import argparse
import sys

from . import config as config_mod
from . import tools
from .brain import BrainClient, BrainError
from .bridge import Bridge
from .settings import SettingsSync


def _client() -> BrainClient:
    """大脑客户端。地址从 config.json 的 `brain.base_url` 来（默认本机 8000）。"""
    try:
        base = str(((config_mod.load().get("brain") or {}).get("base_url")) or "").strip()
        return BrainClient(base or "http://127.0.0.1:8000")
    except ValueError as exc:
        print(f"✗ {exc}")
        raise SystemExit(2) from exc


def cmd_run(args) -> int:
    import asyncio

    bridge = Bridge(brain=_client())
    # 小窝前端可以指定「这个号演谁」（一个账号 = 一个小糖人）。空 / auto = 用配置里那个。
    who = [c.strip() for c in (args.characters or "").split(",") if c.strip() and c.strip() != "auto"]
    if who:
        bridge.persona_override = who[0]
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        print("\n收工。")
    return 0


def _print_accounts(cfg: dict, roster: list[dict]) -> int:
    """把「这份配置要连哪几个 QQ 号、各演谁」列清楚。返回错误条数。"""
    names = {str(c.get("id")): str(c.get("name") or c.get("id")) for c in roster}
    rows = config_mod.accounts(cfg)
    problems = 0
    print(f"要连的号：{len(rows)} 个"
          + ("（单账号 = 顶层 onebot.* / persona.*）" if not (cfg.get("accounts") or []) else ""))
    for index, row in enumerate(rows):
        persona = row.get("persona") or str(((cfg.get("persona") or {}).get("character_id")) or "")
        who = "群聊（按小窝当前在场成员）" if persona == "group" else names.get(persona, persona) or "（没填 → 用名册第一个人，启动时会提醒）"
        print(f"  {index + 1}. {row.get('name')} · QQ {row.get('self_id') or '(连上后问网关)'} "
              f"· 端口 {row.get('listen_port') or '(顶层)'} · 演 {who}")
        if persona and persona != "group" and roster and persona not in names:
            print(f"     ✗ 名册里没有 `{persona}` 这个人 —— 大脑会 404，填角色介绍里的 id。")
            problems += 1
    return problems


def cmd_check(args) -> int:
    """自检 —— 把「为什么它不理我」的常见原因一次列清楚。"""
    cfg = config_mod.load()
    ok = True
    print("== QQ 桥自检 ==")
    print(f"配置文件：{'config.json（已存在）' if config_mod.CONFIG_PATH.is_file() else '还没建 → 跑 `python -m qqbridge example` 再改名成 config.json'}")

    ob = cfg.get("onebot") or {}
    mode = str(ob.get("mode") or "server")
    if mode == "server":
        print(f"OneBot：反向 WS，等网关连 ws://{ob.get('listen_host')}:{ob.get('listen_port')}{ob.get('listen_path')}")
        print(f"  · 多个号就是多个端口（每个号一个网关连接，见下面的号列表）")
    else:
        print(f"OneBot：正向 WS，主动连 {ob.get('ws_url') or '(没填 ws_url)'}")
    token = str(ob.get("access_token") or "")
    print(f"access_token：{'已设（网关必须带对的 token 才连得上）' if token else '没设（谁都能连这个端口 —— 只在本机用时可以）'}")

    allow = cfg.get("allow") or {}
    groups, private = allow.get("groups") or [], allow.get("private") or []
    print(f"顶层白名单：群 {groups or '（空）'} · 私聊 {private or '（空）'}"
          + ("（号上自己写了白名单的，以号上的为准）" if (cfg.get("accounts") or []) else ""))
    if not groups and not private and not any(
            (row.get("groups") or row.get("private")) for row in (cfg.get("accounts") or [])):
        print("  ✗ 两个都空 = **谁都不理**。去小窝前端「💬 QQ 桥设置 → 白名单」里填，或改 config.json。")
        ok = False
    owner = str(((cfg.get("owner") or {}).get("qq")) or "")
    print(f"管理员：{owner or '（没填 → 管理命令全禁用，只有大老大的消息能触发小工具）'}")

    voice = cfg.get("voice") or {}
    asr = cfg.get("asr") or {}
    print(f"语音出：{'开' if voice.get('enabled', True) else '关'} · 引擎 {voice.get('engine')} · "
          f"音色 {voice.get('voice')} · 模式 {voice.get('auto')}")
    engine = str(asr.get("engine") or "qq")
    meanings = {
        "qq": "用 QQ 自己的转写（网关给了就用，不花钱；没给就记「[语音]」）",
        "openai": "先用 QQ 转写，没有时走外部 whisper（要花钱、音频过上游）",
        "none": "不识别，收到语音只当「[语音]」，不硬猜",
    }
    print(f"语音入（识别）：{engine} —— {meanings.get(engine, '？不认识的引擎，按 qq 处理')}")

    sticker = cfg.get("sticker") or {}
    print(f"表情包：{'开' if sticker.get('enabled', True) else '关'} · "
          f"主动发概率 {sticker.get('reply_chance')} · 自动收集{'开' if sticker.get('auto_collect', True) else '关'} "
          f"· 库里 {len(sticker.get('library') or [])} 张")
    if tools.enabled(cfg):
        print(f"可选联网工具：已开启 {tools.allowed_names(cfg)}，"
              + ("只有大老大能用" if (cfg.get("tools") or {}).get("require_owner", True) else "谁都能用 ⚠"))
    else:
        print("可选联网工具：关闭（统一由小窝各端的联网开关授权）")

    brain = _client()
    print("== 大脑 ==")
    try:
        caps = brain.capabilities()
        print(f"✓ 通（契约版本 {caps.get('contract_version')}）")
    except BrainError as exc:
        print(f"✗ 连不上大脑：{exc}")
        print("  → 先把后端跑起来（start.bat / uvicorn app.main:app --port 8000）")
        print("    没起也能跑：桥会用本地 config.json，只是收不到前端改的设置。")
        return 1
    try:
        provider = brain.api_config()
        print(f"✓ 模型配置：{provider.get('model')} @ {provider.get('base_url')}"
              f"（视觉：{'有' if provider.get('vision_model') else '没有'}）")
    except BrainError as exc:
        print(f"✗ 模型没配好：{exc}")
        ok = False
    roster = brain.characters()
    print(f"✓ 角色名册 {len(roster)} 位：{'、'.join(str(c.get('name')) for c in roster) or '（空）'}")
    if _print_accounts(cfg, roster):
        ok = False

    print("== 前端设置（§9.6.5）==")
    settings = SettingsSync(brain, logger=print)
    settings.pull(quiet=True)
    print(f"✓ 从大脑读到的：{settings.summary()}")

    print("\n" + ("✅ 检查过了，可以 `python -m qqbridge run`。" if ok else "⚠ 上面有红叉，先按提示补上。"))
    return 0 if ok else 1


def cmd_whoami(args) -> int:
    brain = _client()
    roster = brain.characters()
    cfg = config_mod.load()
    print("这份配置要连的号：")
    _print_accounts(cfg, roster)
    print("家里的人：")
    here = {row.get("persona") or str(((cfg.get("persona") or {}).get("character_id")) or "")
            for row in config_mod.accounts(cfg)}
    for c in roster:
        print(f"  {c.get('emoji', '')} {c.get('name')}（{c.get('id')}）"
              + (" ← 有号在演这个" if str(c.get("id")) in here else ""))
    print("\n一个账号 = 一个小糖人。哪个号演谁在小窝前端「💬 QQ 桥设置 → 谁在 QQ 上」里改。")
    return 0


def cmd_example(args) -> int:
    path = config_mod.write_example()
    print(f"写出模板：{path}")
    print("复制成 config.json 再改（config.json 已被 .gitignore 挡住，不会提交）。")
    print("其实白名单 / 演谁 / 音色这些**在小窝前端改更方便**，这个文件主要是留个默认值底稿。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qqbridge", description="小糖人 QQ 桥（独立项目，外设）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="常驻：连 QQ 网关，收消息、回消息、同步进小窝").set_defaults(func=cmd_run)
    sub.add_parser("check", help="自检：大脑通不通、要连几个号、白名单空不空（不连 QQ）").set_defaults(func=cmd_check)
    sub.add_parser("whoami", help="每个号演谁 + 家里有哪些人").set_defaults(func=cmd_whoami)
    sub.add_parser("example", help="写出 config.example.json 模板").set_defaults(func=cmd_example)

    # 小窝前端按注册表拼出来的参数（本体 routers/nest.py 的 runner 白名单里才有 --characters）。
    for name in ("run",):
        sub.choices[name].add_argument("--characters", default="",
                                       help="这个号演谁（角色 id，逗号分隔只用第一个；auto = 按配置）")

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
