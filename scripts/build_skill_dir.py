"""Assemble the minimal end-user `skill/` directory from canonical sources.

`skill/` is a tracked, self-contained, minimal install bundle: SDK + installer +
per-runtime adapters + entry doc, WITHOUT dev/backend extras (tests, server,
docs, examples, build scripts). It is GENERATED — re-run this after changing the
runtime so `skill/` stays in sync, then commit. Stdlib only.

Usage:
    python3 scripts/build_skill_dir.py
"""

from __future__ import annotations

import shutil
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skill"

# Minimal runtime the end user needs. (relative to repo root)
COPY_FILES = (
    "SKILL.md",
    "pyproject.toml",
    "用户侧使用文档.md",
)
COPY_TREES = (
    "agent_telemetry_skill",
    "adapters",
)
# Only the runtime scripts (NOT build_package.py / build_skill_dir.py).
COPY_SCRIPTS = (
    "scripts/setup.py",
    "scripts/watch_sessions.py",
    "scripts/hooks/claude_code_hook.py",
)

EXCLUDE_NAMES = {"__pycache__", ".pytest_cache"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}


def _ignore(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n in EXCLUDE_NAMES or Path(n).suffix in EXCLUDE_SUFFIXES}


INSTALL_SH = """#!/bin/bash
# Minimal installer for the Agent Telemetry skill.
# Detects installed runtimes (Claude Code / Codex / OpenClaw / Hermes) and
# installs the matching adapter, then writes ~/.agent-telemetry/config.json.
set -e
cd "$(dirname "$0")"
python3 scripts/setup.py "$@"
"""

INSTALL_MD = """# 安装(最小化)

这是给最终用户的最小安装包,只含运行所需:SDK、安装器、各 runtime 适配器、入口。

## 一键安装

```bash
./install.sh --auto          # 或: python3 scripts/setup.py --auto
```

自动探测已装的 Claude Code / Codex / OpenClaw / Hermes,安装对应适配器,
并初始化 `~/.agent-telemetry/config.json`。

## 配置上报地址(产品方提供)

```bash
./install.sh \\
  --endpoint "https://telemetry.example.com/v1/traces" \\
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
"""


def build() -> None:
    if SKILL_DIR.exists():
        shutil.rmtree(SKILL_DIR)
    SKILL_DIR.mkdir(parents=True)

    count = 0
    for name in COPY_FILES:
        src = REPO_ROOT / name
        if src.is_file():
            shutil.copy2(src, SKILL_DIR / name)
            count += 1
    for tree in COPY_TREES:
        src = REPO_ROOT / tree
        if src.is_dir():
            shutil.copytree(src, SKILL_DIR / tree, ignore=_ignore)
            count += sum(1 for item in (SKILL_DIR / tree).rglob("*") if item.is_file())
    for rel in COPY_SCRIPTS:
        src = REPO_ROOT / rel
        dst = SKILL_DIR / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_file():
            shutil.copy2(src, dst)
            count += 1

    (SKILL_DIR / "INSTALL.md").write_text(INSTALL_MD, encoding="utf-8")
    sh = SKILL_DIR / "install.sh"
    sh.write_text(INSTALL_SH, encoding="utf-8")
    sh.chmod(0o755)
    count += 2

    print(f"generated skill/ — {count} files")
    for p in sorted(SKILL_DIR.rglob("*")):
        if p.is_file():
            print("  " + str(p.relative_to(SKILL_DIR)))


if __name__ == "__main__":
    build()
