# 架构设计（Agent Telemetry Skill）

本文面向需要理解或扩展本仓库的开发者与后端实现者。线上协议细节见
[PROTOCOL.md](PROTOCOL.md)；各 runtime 的安装方式见仓库根
[README.md](../README.md)。

## 1. 数据流

```text
采集（三层，可并存）
  hook / plugin   ── Claude Code hooks、OpenClaw/Hermes 插件（运行时直采）
  log_watch       ── scripts/watch_sessions.py 尾随 transcripts/rollout 日志
  model_reported  ── 模型经 agent-telemetry CLI 自报 decision/事件
        │
        ▼  （宿主关键路径到此为止：本地写盘，零网络）
~/.agent-telemetry/spool/        每行一个 TelemetrySpan dict 的 JSONL 分片
        │
        ▼  agent-telemetry drain（detach 进程 / 机会式 / 定时）
OTLP HTTP 端点（Authorization: Bearer <token>）
  └─ 失败：数据原样留在 spool，下次 drain 重试
  └─ 无 endpoint：写 AGENT_TELEMETRY_OUTPUT 指定的本地 JSONL
        │
        ▼
Collector / server/minimal_ingest.py → trace 后端（去重、聚合、展示）
```

三个时机会触发上报：

1. **机会式 drain**：CLI 的 `decision` / `emit-event` / `session end` 在落
   spool 后，用约 3 秒预算尝试快速 drain（`cli.py` 的
   `_opportunistic_drain`），失败静默。
2. **detach drain**：Claude Code hook 在 `Stop` / `SessionEnd` 时 spawn 一个
   完全脱离的 `python -m agent_telemetry_skill.cli drain` 进程。
3. **watcher 周期 drain**：`watch_sessions.py` 每轮 poll 后顺手 drain。

进程内嵌 SDK（`TelemetryClient.from_env()`）默认走 `BackgroundExporter`：
spans 进内存队列由 daemon 线程批量发送，`atexit` 时 flush，失败落 spool。

## 2. Trace 连续性设计

难点：一次 agent 会话横跨多个短命进程（每个 hook 调用都是新进程），但必须
合成一条 trace。

解法是 `agent_telemetry_skill/session_trace.py` 的磁盘状态：

- `begin(session_id)` 在 `~/.agent-telemetry/state/` 原子创建一条
  `SessionTrace`（`session_id` → `trace_id` + `root_span_id` +
  `start_time_unix_nano`）。幂等且并发安全：谁先创建谁赢，后来者读到同一条。
- 任何层（hook、watcher、CLI）拿同一个 `session_id` 调 `begin()`/`get()`，
  就能把自己的 span 挂到同一 `trace_id` 下、以 `root_span_id` 为 parent。
  错过 `SessionStart` 也没关系——`begin()` 自动补建。
- `end(session_id)` 幂等地发出根 span `agent.run <agent_name>`（status 可标
  error）并清理状态；`record_open`/`pop_open` 用于跨进程配对
  PreToolUse/PostToolUse（拿到真实起止时间）。

所有 span 严格遵守命名约定：`agent.run <name>`、`execute_tool <tool>`、
`chat <model>`、`retrieve <source>`；事件：`agent.decision`、`tool.result`。
属性沿用 OTel GenAI 语义（`gen_ai.operation.name`、`gen_ai.tool.name`、
`gen_ai.usage.input_tokens` 等）。

### 后端去重指引

交付语义是 **at-least-once**（见第 3 节），且同一 runtime 可能同时开了多个
采集层，后端必须去重：

1. **按 `span_id` 精确去重**：重试导致的重复 span 的 `trace_id`/`span_id`
   完全相同，保留任意一条即可。
2. **跨层语义去重**：同一动作被不同层捕获时（如 hook 和 log_watch 都报了
   同一次工具调用），按 `telemetry.collection_layer` 可信度优先：
   `hook` / `plugin` / `sdk` ＞ `log_watch` ＞ `model_reported`。
   `log_watch` span 带 `telemetry.source.file` 可辅助溯源；跨层匹配可用
   `session.id` + 工具名 + 时间窗口近似。
3. `model_reported` 数据只作补充（decision/rationale 是其它层拿不到的），
   不要用它修正其它层的计时或状态。

## 3. 失败语义

设计目标按优先级排序：**绝不影响宿主 agent ＞ 不丢数据 ＞ 实时性**。

- **绝不破坏宿主**：每个入口（hook 脚本、CLI、插件 hook、watcher 循环、
  SDK exporter）都有 catch-all；CLI 与 hook 永远 exit 0；hook 不写 stdout
  （宿主会解析 stdout）；宿主关键路径上没有网络调用。
- **spool 持久化**：`Spool.append()` 永不抛错，先写盘再说。进程崩溃、断网、
  后端宕机都不丢数据——span 留在 `~/.agent-telemetry/spool/` 等下次 drain。
- **at-least-once**：drain 成功才删除 spool 分片，"发送成功但删除前崩溃"会
  导致重复投递，因此后端必须按 `span_id` 去重（见上节）。
- **超时上限**：所有 HTTP 上报都有短超时（默认 5 秒，机会式 drain 约 3 秒
  预算）；hook 在 Claude Code settings 里另有 10 秒硬超时兜底。
- **总开关**：`AGENT_TELEMETRY_ENABLED=0` 时所有入口是静默 no-op；未配
  endpoint 时是纯本地模式，永不联网。

## 4. 安全模型

- **认证**：客户端仅持 ingest token，以 `Authorization: Bearer <token>` 发送。
  token 只应授予"写入 trace"这一种能力。
- **租户归属以服务端为准**：客户端上报的 `tenant.id` 属性仅供参考，后端必须
  从 token 反查租户并覆盖/校验，防止本地配置伪造跨租户写入。
- **脱敏默认开启且在客户端完成**（`agent_telemetry_skill/redaction.py`，
  OpenClaw TS 插件移植了同一套规则）：敏感 key 整值替换 `[REDACTED]`；
  字符串内的 `sk-*` / `Bearer` / JWT 形态 token 替换；内容字段默认变成
  `{"content_omitted": true, "char_count": N}`；长字符串截断。完整内容仅在
  `AGENT_TELEMETRY_CAPTURE_CONTENT=1`（终端用户显式 opt-in）时离开本机。
  服务端仍建议二次扫描兜底。
- **本地文件权限**：`~/.agent-telemetry/config.json` 含 token，应保持
  `0600`（setup.py 写入时设置；手工创建请自行 `chmod 600`）。spool/state
  目录在用户 home 下，仅本用户可读写。
- **不信任输入**：hook stdin、日志行、config 文件全部按"可能是垃圾"处理，
  解析失败一律静默降级，不影响宿主。
