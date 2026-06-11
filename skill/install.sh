#!/bin/bash
# Minimal installer for the Agent Telemetry skill.
# Pass --endpoint/--token/--tenant/--service to connect to your backend
set -e
cd "$(dirname "$0")"
BAKED=(--auto)
python3 scripts/setup.py "${BAKED[@]}" "$@"
