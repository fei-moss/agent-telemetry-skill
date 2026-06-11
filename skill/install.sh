#!/bin/bash
# Minimal installer for the Agent Telemetry skill.
# Detects installed runtimes (Claude Code / Codex / OpenClaw / Hermes) and
# installs the matching adapter, then writes ~/.agent-telemetry/config.json.
set -e
cd "$(dirname "$0")"
python3 scripts/setup.py "$@"
