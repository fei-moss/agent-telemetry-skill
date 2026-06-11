# Agent Telemetry Skill

本地优先的 Agent 观测方案：把用户侧 agent（Claude Code、Codex CLI、OpenClaw、
Hermes 或自研 agent）每次运行中的工具调用、模型调用、决策、错误和耗时，以
OTLP/HTTP 协议上报到你的遥测后端。**默认只上传结构化元数据并脱敏**，完整
prompt / tool output 等内容必须显式 opt-in 才会采集。

核心原则：**遥测永远不能拖慢、阻塞或搞挂宿主 agent**。所有入口都有兜底异常
处理；宿主关键路径上零网络调用（数据先落本地 spool，再异步/机会式上报）。

## 三层采集架构

不再依赖"模型自觉调 SDK"。可靠数据由 hook/插件和日志监听自动产出，模型只负
责补充运行时看不到的决策信息：

```text
┌────────────────────────────────────────────────────────────────────┐
│ 第 1 层 可靠层（hook / plugin）                                      │
│   Claude Code hooks · OpenClaw TS 插件 · Hermes Python 插件          │
│   自动捕获：tool 调用、模型调用、session 生命周期                     │
├────────────────────────────────────────────────────────────────────┤
│ 第 2 层 日志监听层（log_watch）                                      │
│   scripts/watch_sessions.py 尾随 Claude Code transcripts /          │
│   Codex rollout 日志，解析出 execute_tool / chat span               │
├────────────────────────────────────────────────────────────────────┤
│ 第 3 层 模型自报层（model_reported）                                 │
│   模型按 SKILL.md 约定用 CLI 上报 decision / rationale / 异常事件     │
└──────────────────────────┬─────────────────────────────────────────┘
                           ▼
              ~/.agent-telemetry/spool/   （磁盘持久化，进程退出不丢）
                           ▼  agent-telemetry drain（异步/机会式）
              OTLP HTTP 端点（或本地 JSONL fallback）
                           ▼
              Collector / minimal_ingest → 你的 trace 后端
```

每个 span 都带 `telemetry.collection_layer` 属性（`hook` / `log_watch` /
`model_reported` / `plugin` / `sdk`），后端据此判断数据可信度并去重。

## 快速开始

```bash
cd agent-telemetry-skill
python3 scripts/setup.py --auto
```

`setup.py --auto` 会探测本机已安装的 runtime，调用对应的
`adapters/*/install.py` 安装适配器，并初始化 `~/.agent-telemetry/config.json`。
卸载用 `python3 scripts/setup.py --uninstall`（或逐个运行
`adapters/<runtime>/install.py uninstall`）。

配置上报目标（环境变量优先于 `~/.agent-telemetry/config.json`）：

```bash
export AGENT_TELEMETRY_ENDPOINT="https://telemetry.example.com/v1/traces"
export AGENT_TELEMETRY_TOKEN="your-ingest-token"
export AGENT_TELEMETRY_SERVICE="my-local-agent"
export AGENT_TELEMETRY_TENANT="tenant_123"
```

不配置 endpoint 即本地模式：数据持久化在 `~/.agent-telemetry/spool/`，随时可
`PYTHONPATH=. python3 -m agent_telemetry_skill.cli drain` 补传。

查看当前状态（配置、spool 深度、活跃 session 数）：

```bash
PYTHONPATH=. python3 -m agent_telemetry_skill.cli status
```

## 各 Runtime 支持矩阵

| Runtime | 适配器 | 采集层 (`telemetry.collection_layer`) | 采集内容 | 可信度 |
| --- | --- | --- | --- | --- |
| Claude Code | `adapters/claude_code/install.py`（hooks 写入 `~/.claude/settings.json`） | `hook` | `agent.run` 根 span、`execute_tool <tool>` 子 span（gen_ai.* 属性、脱敏参数、`tool.result` 事件）、`agent.turn` | 高（运行时事件直采） |
| Claude Code（备选） | `scripts/watch_sessions.py --runtime claude-code` | `log_watch` | transcripts 中的工具调用/模型用量 | 中（日志重建，带 `telemetry.source.file`） |
| Codex CLI | `adapters/codex/install.py`（notify 钩子 + 日志监听） | `log_watch` | rollout 日志中的 `execute_tool` / `chat <model>` span、token 用量 | 中（日志重建，带 `telemetry.source.file`） |
| OpenClaw | `adapters/openclaw/install.py`（TS 插件 `telemetry-plugin.ts`） | `plugin` | `agent.run` / `execute_tool` / `chat` span、message 事件、token 用量 | 高（运行时事件直采） |
| Hermes | `adapters/hermes/install.py`（Python 插件） | `plugin` | session / API request / tool call span | 高（运行时事件直采） |
| 任意（模型自报） | `SKILL.md` + `agent-telemetry` CLI | `model_reported` | `agent.decision`（rationale/confidence）、retry/异常事件 | 低（模型自述，仅作补充） |
| 任意（代码内嵌） | SDK `TelemetryClient` | `sdk` | run / decision / tool / llm / retrieval 全量 | 高 |

同一 runtime 只建议启用一个自动采集层（hook **或** watcher）；如果重复，
后端按 `hook > log_watch > model_reported` 优先去重（见
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)）。

模型自报示例（采集层 `model_reported`）：

```bash
agent-telemetry decision use_search \
  --rationale "Need external context" --confidence 0.8 \
  --session-id <SESSION_ID>
```

## SDK 用法（自研 agent 直接内嵌）

```python
from agent_telemetry_skill import TelemetryClient

client = TelemetryClient.from_env()  # 读取 AGENT_TELEMETRY_* / config.json

with client.run("answer-user", user_id="user_abc", agent_name="my-agent"):
    client.decision("call_search", rationale="Need external context", confidence=0.76)
    with client.tool_call("search_web", {"query": "agent telemetry"}):
        client.record_event("tool.output", {"result_count": 3})
    with client.llm_call(
        provider="openai",
        model="gpt-5-mini",
        prompt={"messages": [{"role": "user", "content": "hello"}]},
    ) as span:
        span.set_result({"finish_reason": "stop"})
```

默认情况下 `prompt.messages[].content` 会变成
`{"content_omitted": true, "char_count": 5}`；`api_key` 等敏感 key 变成
`[REDACTED]`。装饰器接入见 `agent_telemetry_skill/adapters.py` 与
`examples/framework_adapter.py`。

## 隐私与安全默认值

- 默认省略内容字段：`prompt`、`message`、`query`、`completion`、`text`、
  `input`、`output`、`response`、`result`（只保留 `content_omitted` + 长度）。
- 默认脱敏 key（含片段匹配）：`api_key`、`authorization`、`cookie`、
  `password`、`private_key`、`secret`、`session`、`token`。
- 默认识别并替换 token 形态：`sk-...`、`sk-proj-...`、`sk-ant-...`、
  `Bearer ...`、JWT 三段式。
- 完整内容采集仅在 `AGENT_TELEMETRY_CAPTURE_CONTENT=1`（用户显式 opt-in）
  时生效；`AGENT_TELEMETRY_ENABLED=0` 让所有入口静默 no-op。

## 开发循环

```bash
# 全量测试
PYTHONPATH=. python3 -m unittest discover -s tests

# 本地最小接收服务（终端 A）
PYTHONPATH=. python3 server/minimal_ingest.py --port 4318 --output traces.jsonl

# 发送 demo trace（终端 B）
PYTHONPATH=. python3 -m agent_telemetry_skill.cli demo \
  --otlp-endpoint http://127.0.0.1:4318/v1/traces

# 日志监听单轮验证
PYTHONPATH=. python3 scripts/watch_sessions.py --runtime all --once
PYTHONPATH=. python3 scripts/watch_sessions.py --status
```

OpenTelemetry Collector 示例配置在 `collector/otel-collector.yaml`：

```bash
docker run --rm -p 4318:4318 \
  -v "$PWD/collector/otel-collector.yaml:/etc/otelcol/config.yaml" \
  otel/opentelemetry-collector-contrib:latest \
  --config /etc/otelcol/config.yaml
```

## 文档导航

| 文档 | 内容 |
| --- | --- |
| [SKILL.md](SKILL.md) | 给模型读的 skill 入口（英文、指令式） |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 数据流、trace 连续性、失败语义、安全模型 |
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | 与语言无关的 OTLP/HTTP 上报协议（任何语言可自行实现） |
| [用户侧使用文档.md](用户侧使用文档.md) | 终端用户视角：启用、隐私、卸载、FAQ |
| [使用说明.md](使用说明.md) | 产品方/运维视角：ingest 搭建、token 发放、后端去重 |
| [adapters/codex/README.md](adapters/codex/README.md) | Codex 适配器细节 |
| [adapters/openclaw/README.md](adapters/openclaw/README.md) | OpenClaw 插件细节 |

## 测试

```bash
PYTHONPATH=. python3 -m unittest discover -s tests
```
