# Codex CLI 适配器

把 OpenAI Codex CLI 的会话变成 OTLP telemetry。本适配器基于 **日志监听**（log
watcher）：`scripts/watch_sessions.py --runtime codex` 持续尾随
`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`，把新行解析成 span 写入本地
spool，并在配置了 endpoint/output 时机会式上报（失败的数据留在 spool，可用
`agent-telemetry drain` 重试）。

## 采集内容

来自 rollout 日志（采集层 `telemetry.collection_layer = "log_watch"`）：

| 数据 | Span / 属性 |
| --- | --- |
| 工具调用（`function_call` / `custom_tool_call` 及其 output，按 call_id 关联） | `execute_tool <tool>`，含 `gen_ai.tool.name`、起止时间 |
| 模型用量（`token_count` 事件 + `turn_context` 里的 model） | `chat <model>`，含 `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens` |
| 会话归属 | 同一 session 的 span 共享 trace（root span `agent.run codex`，session id 取自 `session_meta` 或文件名） |
| 溯源 | 每个 span 带 `telemetry.source.file`，便于后端去重 |

脱敏默认开启：工具参数、输出等内容默认只保留结构化元数据（
`content_omitted` / 长度），仅在 `AGENT_TELEMETRY_CAPTURE_CONTENT=1` 时采集
完整内容。`AGENT_TELEMETRY_ENABLED=0` 时所有入口都是静默 no-op。

## 安装

```bash
python3 adapters/codex/install.py status
python3 adapters/codex/install.py install --yes
python3 adapters/codex/install.py uninstall --yes
```

`install` 做两件事：

1. **校验 watcher**：调用 `scripts/watch_sessions.py --runtime codex --status`，
   确认能看到 Codex 的会话文件（打印 tracked_files / spool_depth）。
2. **接管 notify（可选自动化）**：在 `~/.codex/config.toml`（可用 `CODEX_HOME`
   覆盖）顶部写入一个清晰分隔的托管块，把 Codex 的顶层 `notify` 指向
   `adapters/codex/notify_hook.py`：

   ```toml
   # >>> agent-telemetry codex adapter (managed) >>>
   # Each Codex turn-end triggers one telemetry watcher poll + spool drain.
   # Managed by adapters/codex/install.py — do not edit between these markers.
   notify = ["/usr/bin/python3", "/abs/path/adapters/codex/notify_hook.py"]
   # <<< agent-telemetry codex adapter (managed) <<<
   ```

   Codex 在每个 turn 结束时会以 JSON payload（`{"type":
   "agent-turn-complete", ...}`）作为最后一个参数调用该程序；
   `notify_hook.py` 只是 detach 启动一次 `watch_sessions.py --runtime codex
   --once`（stdio 全部指向 devnull）后立刻退出，**绝不阻塞 Codex**。

安全性说明：

- 首次修改前会把 `config.toml` 备份到 `config.toml.bak-agent-telemetry`
  （已有备份则不覆盖）；`uninstall` 只移除托管块，其余配置原样保留。
- Codex 只支持一个 notify 程序。如果你的 `config.toml` 里已有别的
  `notify`（例如 Codex 桌面通知），安装器**不会改动它**，转为
  watcher-only 模式并打印指引；也可以让你现有的 notify 程序在收到
  `agent-turn-complete` 后顺手执行一次
  `python3 adapters/codex/notify_hook.py '<payload>'` 实现链式触发。
- 未检测到 Codex（`~/.codex` 不存在）时打印安装指引并以 0 退出。

## 持久运行 watcher

notify 钩子只在 turn 结束时触发一次轮询；想要持续采集（包括 Codex 空闲时补
传 spool），手动跑一个常驻 watcher：

```bash
cd /path/to/agent-telemetry-skill
python3 scripts/watch_sessions.py --runtime codex --interval 5
```

macOS 上可以用 launchd 常驻（示例 plist，仅供参考，安装器**不会**自动安装；
保存为 `~/Library/LaunchAgents/com.agent-telemetry.codex-watcher.plist` 后
`launchctl load` 即可）：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.agent-telemetry.codex-watcher</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/ABS/PATH/agent-telemetry-skill/scripts/watch_sessions.py</string>
    <string>--runtime</string>
    <string>codex</string>
    <string>--interval</string>
    <string>5</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/agent-telemetry-codex-watcher.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/agent-telemetry-codex-watcher.log</string>
</dict>
</plist>
```

上报目标通过环境变量或 `~/.agent-telemetry/config.json` 配置
（`AGENT_TELEMETRY_ENDPOINT` / `AGENT_TELEMETRY_TOKEN` /
`AGENT_TELEMETRY_OUTPUT` 等，见仓库 README）。

## 局限

- **没有 decision/rationale**：rollout 日志只有工具调用和 token 用量，拿不到
  模型的决策理由。如需决策数据，让模型自己通过 CLI 上报（采集层会是
  `model_reported`）：

  ```bash
  agent-telemetry decision use_search --rationale "need external context" --confidence 0.8
  ```

- **Codex skill 支持**：Codex 支持 skill 目录（`~/.codex/skills/`）。把本仓库
  作为 skill 放进去后，模型可按 `SKILL.md` 的约定主动上报 decision / 自定义
  span，与本适配器的 `log_watch` 数据互补（span 带 `telemetry.source.file`，
  后端可去重）。
- 日志监听有秒级延迟（notify 触发的轮询在 turn 结束后、常驻 watcher 按
  `--interval` 轮询），span 时间取自日志内时间戳，不受轮询延迟影响。
- 同一 runtime 只建议启用一个采集层（本 watcher 即 Codex 的采集层；不要再为
  Codex 启用其它 hook 采集）。
