# 安装(最小化)

这是给最终用户的最小安装包,只含运行所需:SDK、安装器、各 runtime 适配器、入口。

## 一键安装

```bash
./install.sh --auto          # 或: python3 scripts/setup.py --auto
```

自动探测已装的 Claude Code / Codex / OpenClaw / Hermes,安装对应适配器,
并初始化 `~/.agent-telemetry/config.json`。

## 配置上报地址(产品方提供)

```bash
./install.sh \
  --endpoint "https://telemetry.example.com/v1/traces" \
  --token "<接入 token>" --tenant "<租户ID>" --service "<服务名>"
```

或用环境变量(优先级更高):`AGENT_TELEMETRY_ENDPOINT` / `AGENT_TELEMETRY_TOKEN`
/ `AGENT_TELEMETRY_TENANT` / `AGENT_TELEMETRY_SERVICE`。

## 验证

```bash
python3 scripts/setup.py --status
PYTHONPATH=. python3 -m agent_telemetry_skill.cli demo   # 密钥应显示 [REDACTED]
```

## 富内容(思考/进度/工具,给人看)

在 `~/.agent-telemetry/config.json` 加:

```json
{ "capture_narrative": true, "capture_content": true, "max_content_chars": 4000 }
```

再跑日志监听器(近实时):

```bash
PYTHONPATH=. python3 scripts/watch_sessions.py --runtime all --interval 5
```

## 关闭 / 卸载

```bash
export AGENT_TELEMETRY_ENABLED=0          # 临时静默
python3 scripts/setup.py --uninstall      # 卸载适配器
```

隐私默认:不上传密钥与完整正文(除非显式开启 capture_content);完整说明见
`用户侧使用文档.md`。本目录由 `scripts/build_skill_dir.py` 从主仓库生成。
