"""命令行入口：`python -m qqbridge <命令>`（在小窝前端点「跑起来」也是这个）。

    run     常驻：连 QQ 网关，收消息、回消息、同步进小窝（**平时就用这个**）
    check   自检：大脑通不通、配置缺什么、白名单空不空（不连 QQ，不花 token）
    whoami  这个号演谁 + 家里有哪些人（人选来自大脑的角色介绍）
    example 写出 config.example.json（配置模板，别把 config.json 提交上去）

⚠ Windows 终端中文乱码：设 `PYTHONIOENCODING=utf-8`（本体的运行器已经帮你设了）。
"""

from __future__ import annotations

import argparse
import sys

from . import config as config_mod
from .brain import BrainClient, BrainError
from .bridge import Bridge


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
    # 小窝前端可以指定「这个号演谁」（一个账号 = 一个小糖人）。空 / auto = 用 config.json 里那个。
    who = [c.strip() for c in (args.characters or "").split(",") if c.strip() and c.strip() != "auto"]
    if who:
        bridge.persona_override = who[0]
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        print("\n收工。")
    return 0


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
    else:
        print(f"OneBot：正向 WS，主动连 {ob.get('ws_url') or '(没填 ws_url)'}")
    if not ob.get("self_id"):
        print("  · self_id 留空 —— 连上后会问网关要；要是网关不回，群里 @ 判断会失效")

    allow = cfg.get("allow") or {}
    groups, private = allow.get("groups") or [], allow.get("private") or []
    print(f"白名单：群 {groups or '（空）'} · 私聊 {private or '（空）'}")
    if not groups and not private:
        print("  ✗ 两个都空 = **谁都不理**。填上要接的群号 / QQ 号。")
        ok = False
    owner = (cfg.get("owner") or {}).get("qq")
    print(f"管理员：{owner or '（没填 → 管理命令全禁用）'}")

    voice = cfg.get("voice") or {}
    asr = cfg.get("asr") or {}
    print(f"语音出：{'开' if voice.get('enabled', True) else '关'} · 引擎 {voice.get('engine')} · "
          f"音色 {voice.get('voice')} · 模式 {voice.get('auto')}")
    engine = str(asr.get("engine") or "none")
    print(f"语音入（识别）：{engine}" + ("（没配 → 收到语音只当「[语音]」，不硬猜）" if engine == "none" else ""))

    brain = _client()
    print("== 大脑 ==")
    try:
        caps = brain.capabilities()
        print(f"✓ 通（契约版本 {caps.get('contract_version')}）")
    except BrainError as exc:
        print(f"✗ 连不上大脑：{exc}")
        print("  → 先把后端跑起来（start.bat / uvicorn app.main:app --port 8000）")
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
    persona = str(((cfg.get('persona') or {}).get('character_id')) or '')
    print(f"这个号演：{persona or '（没填 → 用名册第一个人，启动时会提醒）'}")
    if persona and roster and persona not in {str(c.get('id')) for c in roster}:
        print(f"  ✗ 名册里没有 `{persona}` 这个人 —— 大脑会 404，填角色介绍里的 id。")
        ok = False
    print("\n" + ("✅ 检查过了，可以 `python -m qqbridge run`。" if ok else "⚠ 上面有红叉，先按提示补上。"))
    return 0 if ok else 1


def cmd_whoami(args) -> int:
    brain = _client()
    roster = brain.characters()
    cfg = config_mod.load()
    persona = str(((cfg.get("persona") or {}).get("character_id")) or "")
    print(f"这个号演：{persona or (roster[0].get('id') if roster else '（名册也拉不到）')}")
    print("家里的人：")
    for c in roster:
        mark = " ← 本号" if c.get("id") == persona else ""
        print(f"  {c.get('emoji', '')} {c.get('name')}（{c.get('id')}）{mark}")
    print("\n一个账号 = 一个小糖人。想让几个小糖人都在 QQ 上，就多跑几份、各配各的 config.json。")
    return 0


def cmd_example(args) -> int:
    path = config_mod.write_example()
    print(f"写出模板：{path}")
    print("复制成 config.json 再改（config.json 已被 .gitignore 挡住，不会提交）。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qqbridge", description="小糖人 QQ 桥（独立项目，外设）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="常驻：连 QQ 网关，收消息、回消息、同步进小窝").set_defaults(func=cmd_run)
    sub.add_parser("check", help="自检：大脑通不通、配置缺什么（不连 QQ）").set_defaults(func=cmd_check)
    sub.add_parser("whoami", help="这个号演谁 + 家里有哪些人").set_defaults(func=cmd_whoami)
    sub.add_parser("example", help="写出 config.example.json 模板").set_defaults(func=cmd_example)

    # 小窝前端按注册表拼出来的参数（本体 routers/nest.py 的白名单里才有 --characters）。
    for name in ("run",):
        sub.choices[name].add_argument("--characters", default="",
                                       help="这个号演谁（角色 id，逗号分隔只用第一个；auto = 按配置）")

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())