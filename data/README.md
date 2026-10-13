# QQ 桥的运行状态（不进 git）

配置模板见 `config.example.json`（`python -m qqbridge example` 会重新生成一份）。
这个文件是桥自己的运行时数据目录说明 —— 里面的东西都不该提交。

```
data/
├── conversations.json   每个 QQ 会话最近聊过的上下文（进程重启后小糖人不至于失忆）
├── seen_messages.json   见过的消息 id（挡网关重连时的重放，避免重复回一遍）
├── stickers/            小糖人的表情库（自动学来的 + 你手动丢进来的）
├── outbox/              出件箱：往这里丢文件，下一轮回话时发出去
│   └── sent/            已经发出去的（留着，不重复发）
└── tmp/                 临时文件（下载的图 / 合成的语音，用完就删）
```