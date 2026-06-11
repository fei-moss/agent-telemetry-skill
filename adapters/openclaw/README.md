# OpenClaw Agent Telemetry 插件

单文件、零依赖的 OpenClaw 遥测插件（`telemetry-plugin.ts`）。它监听 OpenClaw 的
生命周期 hook，把 agent 运行链路按 [`docs/PROTOCOL.md`](../../docs/PROTOCOL.md)
定义的 OTLP/HTTP JSON 协议上报到遥测后端；没有配置 endpoint 时退化为本地
JSONL 文件，绝不影响宿主 agent 运行。

所有 span 都带 `telemetry.collection_layer = "plugin"`。

## 采集内容

| OpenClaw hook | 产出 |
| --- | --- |
| `session_start` / `session_end` | 根 span `agent.run openclaw:<session>`（tenant.id、service.name、session.id、enduser.id 等） |
| `message_received` / `message_sent` | 根 span 上的 `message.received` / `message.sent` 事件（内容默认脱敏省略） |
| `before_tool_call` / `after_tool_call` | 子 span `execute_tool <tool>`，含 `gen_ai.tool.name`、`tool.call.id`、脱敏后的 `tool.arguments.*`、`tool.result` 事件、错误状态 |
| `model_call_started` / `model_call_ended` | 子 span `chat <model>`，含 `gen_ai.provider.name`、`gen_ai.request.model`、`duration.ms` |
| `llm_output` | 把 `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens` 附加到对应 `chat` span |
| `agent_end` | 根 span 上的 `agent.end` 事件并触发一次 flush |

缓冲策略：span 按 session 缓冲，在 session 结束时整体上报，另有 5 秒定时器
定期 flush 已完成的子 span。上报失败或没有 endpoint 时写入本地 JSONL。

## 隐私默认值（默认开启，无法绕过）

与 `agent_telemetry_skill/redaction.py` 行为一致：

- 敏感 key（`api_key`、`token`、`password`、`secret`、`cookie`、`authorization` 等）整体替换为 `[REDACTED]`；
- 字符串里的 `sk-...`、`sk-ant-...`、`Bearer ...`、JWT 形态 token 替换为 `[REDACTED]`；
- `prompt` / `content` / `query` / `result` 等内容字段默认替换为
  `{"content_omitted": true, "char_count": N}`；
- 超过 500 字符的字符串截断并追加 `...[TRUNCATED]`。

只有显式设置 `AGENT_TELEMETRY_CAPTURE_CONTENT=1` 才会上传完整内容。

## 安装

### 方式一：安装脚本（推荐）

```bash
cd agent-telemetry-skill/adapters/openclaw
python3 install.py status          # 查看检测结果
python3 install.py install --yes   # 安装
python3 install.py uninstall --yes # 卸载
```

脚本会按 `$OPENCLAW_HOME` → `~/.openclaw` → `~/.config/openclaw` 的顺序探测
OpenClaw 安装目录，把插件复制到 `<openclaw_home>/extensions/agent-telemetry/`
（`index.ts` + `package.json` + `openclaw.plugin.json`），并打印需要加进
OpenClaw 配置（openclaw.json）的片段：

```json
{
  "plugins": {
    "enabled": true,
    "entries": { "agent-telemetry": { "enabled": true } },
    "load": { "paths": ["~/.openclaw/extensions/agent-telemetry"] }
  }
}
```

脚本是幂等的：重复 install 只重写有差异的文件；uninstall 只删除脚本自己
写入的三个文件。

### 方式二：OpenClaw 插件命令

```bash
openclaw plugins install ./agent-telemetry-skill/adapters/openclaw
openclaw plugins enable agent-telemetry
openclaw plugins list
```

## 环境变量

解析顺序：环境变量 → `~/.agent-telemetry/config.json` 同名 key → 默认值。

| 环境变量 | config.json key | 默认值 | 说明 |
| --- | --- | --- | --- |
| `AGENT_TELEMETRY_ENDPOINT` | `endpoint` | 无 | OTLP HTTP traces URL（含 `/v1/traces`）。缺省时本地模式，只写 JSONL |
| `AGENT_TELEMETRY_TOKEN` | `token` | 无 | 以 `Authorization: Bearer <token>` 发送 |
| `AGENT_TELEMETRY_SERVICE` | `service` | `local-agent` | `service.name` |
| `AGENT_TELEMETRY_TENANT` | `tenant` | `local-dev` | `tenant.id` |
| `AGENT_TELEMETRY_ENVIRONMENT` | `environment` | `local` | `deployment.environment` |
| `AGENT_TELEMETRY_CAPTURE_CONTENT` | `capture_content` | 关 | `1`/`true` 时采集完整内容 |
| `AGENT_TELEMETRY_OUTPUT` | `output` | `<home>/openclaw-telemetry.jsonl` | 本地 JSONL 落盘路径 |
| `AGENT_TELEMETRY_HOME` | `home` | `~/.agent-telemetry` | 状态根目录 |
| `AGENT_TELEMETRY_ENABLED` | `enabled` | 开 | `0`/`false` 时所有入口静默 no-op |

示例：

```bash
export AGENT_TELEMETRY_ENDPOINT="https://telemetry.example.com/v1/traces"
export AGENT_TELEMETRY_TOKEN="YOUR_INGEST_TOKEN"
export AGENT_TELEMETRY_SERVICE="openclaw-local"
export AGENT_TELEMETRY_TENANT="tenant_123"
```

## 手动接线 fallback

插件默认导出对象按 OpenClaw 文档
（https://docs.openclaw.ai/plugins/building-plugins 与
https://docs.openclaw.ai/plugins/hooks ，2026-06 版本）的
`definePluginEntry` 形态编写：`{ id, name, description, register(api) }`，
在 `register` 内通过 `api.on(hookName, handler, { priority })` 注册。如果你的
OpenClaw 版本注册 API 有差异，可以在自己的插件入口里手动接线这些具名导出
（它们都不会抛异常）：

```ts
import telemetry, {
  onSessionStart,
  onSessionEnd,
  onMessageReceived,
  onMessageSent,
  onToolStart,
  onToolEnd,
  onModelCallStarted,
  onModelCallEnded,
  onLlmOutput,
  onAgentEnd,
  flushAll,
} from "./agent-telemetry/index.js";

// 任意框架的 glue code 示例：
myFramework.on("sessionStarted", (e) => onSessionStart(e));
myFramework.on("toolWillRun", (e) => onToolStart(e));
myFramework.on("toolDidRun", (e) => onToolEnd(e));
myFramework.on("sessionClosed", (e) => onSessionEnd(e));
process.on("beforeExit", () => flushAll());
```

事件对象按需提供这些字段（缺失时插件自动兜底）：`sessionKey` / `sessionId`、
`runId`、`senderId`、`toolName`、`toolCallId`、`params`、`result`、`error`、
`durationMs`、`provider`、`model`、`usage.input_tokens`、`usage.output_tokens`、
`content`、`reason`、`success`。

## 校验

```bash
npx --yes -p typescript@5 tsc --noEmit --strict --target es2022 \
  --module es2022 --moduleResolution node telemetry-plugin.ts
```
