#!/usr/bin/env python3
"""Universal installer for the agent telemetry skill.

Detects which agent runtimes exist on this machine (Claude Code, Codex CLI,
OpenClaw, Hermes), shells out to the per-runtime installers under
``adapters/<name>/install.py``, optionally writes the shared
``~/.agent-telemetry/config.json``, and prints a final summary table.

Usage examples::

    python3 scripts/setup.py                    # interactive-lite: detect + confirm
    python3 scripts/setup.py --auto             # install everything detected
    python3 scripts/setup.py --runtime codex    # only one runtime (repeatable)
    python3 scripts/setup.py --uninstall --auto # remove everything detected
    python3 scripts/setup.py --status           # config + per-runtime state
    python3 scripts/setup.py --endpoint URL --token TOKEN --auto
    python3 scripts/setup.py --endpoint URL --print-env  # echo exports instead

Exit code is 0 even when individual adapters fail (their failures show up in
the summary table); nonzero only on unusable command-line arguments.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Callable
import uuid

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agent_telemetry_skill.config import (  # noqa: E402
    CONFIG_FILE_NAME,
    HOME_DIR_NAME,
    TelemetryConfig,
    load_config,
    spool_dir,
    state_dir,
)
from agent_telemetry_skill.spool import Spool  # noqa: E402


RUNTIME_CLAUDE = "claude-code"
RUNTIME_CODEX = "codex"
RUNTIME_OPENCLAW = "openclaw"
RUNTIME_HERMES = "hermes"
RUNTIMES = (RUNTIME_CLAUDE, RUNTIME_CODEX, RUNTIME_OPENCLAW, RUNTIME_HERMES)

ADAPTER_DIR_NAMES = {
    RUNTIME_CLAUDE: "claude_code",
    RUNTIME_CODEX: "codex",
    RUNTIME_OPENCLAW: "openclaw",
    RUNTIME_HERMES: "hermes",
}
LOG_WATCH_RUNTIMES = (RUNTIME_CODEX,)

CONFIG_FLAG_KEYS = ("endpoint", "token", "tenant", "service")
ENV_NAMES = {
    "endpoint": "AGENT_TELEMETRY_ENDPOINT",
    "token": "AGENT_TELEMETRY_TOKEN",
    "tenant": "AGENT_TELEMETRY_TENANT",
    "service": "AGENT_TELEMETRY_SERVICE",
}

CONFIG_FILE_MODE = 0o600
ADAPTER_TIMEOUT_SECONDS = 120.0
CONNECTIVITY_BUDGET_SECONDS = 5.0
WATCH_SCRIPT = _REPO_ROOT / "scripts" / "watch_sessions.py"

# Module-level so tests can point this at a stub adapters directory.
ADAPTERS_DIR = _REPO_ROOT / "adapters"


@dataclass(frozen=True)
class SummaryRow:
    runtime: str
    detected: bool
    action: str
    result: str


# ---------------------------------------------------------------------------
# Runtime detection
# ---------------------------------------------------------------------------


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _env_home_is_dir(env_name: str) -> bool:
    value = os.environ.get(env_name)
    return bool(value) and _is_dir(Path(value).expanduser())


def detect_claude_code() -> bool:
    return bool(os.environ.get("CLAUDECODE")) or _is_dir(Path.home() / ".claude")


def detect_codex() -> bool:
    return _env_home_is_dir("CODEX_HOME") or _is_dir(Path.home() / ".codex")


def detect_hermes() -> bool:
    return _env_home_is_dir("HERMES_HOME") or _is_dir(Path.home() / ".hermes")


def detect_openclaw() -> bool:
    # Mirrors the candidates probed by adapters/openclaw/install.py.
    return (
        _env_home_is_dir("OPENCLAW_HOME")
        or _is_dir(Path.home() / ".openclaw")
        or _is_dir(Path.home() / ".config" / "openclaw")
    )


DETECTORS: dict[str, Callable[[], bool]] = {
    RUNTIME_CLAUDE: detect_claude_code,
    RUNTIME_CODEX: detect_codex,
    RUNTIME_OPENCLAW: detect_openclaw,
    RUNTIME_HERMES: detect_hermes,
}


def detect_runtimes() -> dict[str, bool]:
    return {runtime: DETECTORS[runtime]() for runtime in RUNTIMES}


# ---------------------------------------------------------------------------
# Config file handling
# ---------------------------------------------------------------------------


def config_file_path() -> Path:
    return Path.home() / HOME_DIR_NAME / CONFIG_FILE_NAME


def collect_config_values(args: argparse.Namespace) -> dict[str, str]:
    values: dict[str, str] = {}
    for key in CONFIG_FLAG_KEYS:
        value = getattr(args, key)
        if value:
            values[key] = value
    return values


def write_config(values: dict[str, str]) -> Path:
    """Merge the given keys into ~/.agent-telemetry/config.json (mode 600)."""
    path = config_file_path()
    existing: dict[str, object] = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            existing = data
        else:
            print(f"setup: warning: {path} is not a JSON object; rewriting", file=sys.stderr)
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"setup: warning: could not parse {path}: {exc}; rewriting", file=sys.stderr)
    merged = {**existing, **values}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, CONFIG_FILE_MODE)
    return path


def print_env_exports(values: dict[str, str]) -> None:
    if not values:
        print("# no config flags given; nothing to export")
        return
    for key in CONFIG_FLAG_KEYS:
        if key in values:
            print(f"export {ENV_NAMES[key]}={shlex.quote(values[key])}")


def mask_token(token: str | None) -> str | None:
    if not token:
        return None
    if len(token) <= 4:
        return "***"
    return f"{token[:4]}***"


# ---------------------------------------------------------------------------
# Adapter dispatch
# ---------------------------------------------------------------------------


def adapter_script(runtime: str) -> Path:
    return ADAPTERS_DIR / ADAPTER_DIR_NAMES[runtime] / "install.py"


def _python_executable() -> str:
    return sys.executable or "python3"


def _surface_output(stdout: str, stderr: str) -> None:
    for stream in (stdout, stderr):
        for line in stream.splitlines():
            print(f"    {line}")


def run_adapter(runtime: str, action: str) -> str:
    """Run adapters/<name>/install.py <action> --yes; return a result string."""
    script = adapter_script(runtime)
    if not script.is_file():
        return f"installer missing ({script})"
    cmd = [_python_executable(), str(script), action, "--yes"]
    print(f"\n==> {runtime}: {' '.join(cmd)}")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=ADAPTER_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return f"failed (timeout after {ADAPTER_TIMEOUT_SECONDS:.0f}s)"
    except Exception as exc:
        return f"failed ({exc})"
    _surface_output(proc.stdout, proc.stderr)
    if proc.returncode == 0:
        return "ok"
    return f"failed (exit {proc.returncode})"


def run_adapter_status(runtime: str) -> str:
    script = adapter_script(runtime)
    if not script.is_file():
        return f"installer missing ({script})"
    cmd = [_python_executable(), str(script), "status"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=ADAPTER_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except Exception as exc:
        return f"status check failed ({exc})"
    _surface_output(proc.stdout, proc.stderr)
    return "ok" if proc.returncode == 0 else f"status exit {proc.returncode}"


# ---------------------------------------------------------------------------
# Connectivity self-test
# ---------------------------------------------------------------------------


def connectivity_self_test(config: TelemetryConfig) -> str:
    """Emit one test event through the CLI; report whether it left the spool."""
    cmd = [
        _python_executable(),
        "-m",
        "agent_telemetry_skill.cli",
        "emit-event",
        "setup.connectivity-test",
        "--trace-id",
        uuid.uuid4().hex,
    ]
    env = dict(os.environ)
    existing_path = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{_REPO_ROOT}{os.pathsep}{existing_path}" if existing_path else str(_REPO_ROOT)
    )
    try:
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CONNECTIVITY_BUDGET_SECONDS,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except Exception:
        pass  # outcome is judged by the spool depth below
    try:
        depth = Spool(spool_dir(config)).depth()
    except Exception:
        return "failed-spooled (could not read spool)"
    if depth == 0:
        return "ok"
    return f"failed-spooled ({depth} span(s) remain in spool)"


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render_summary(rows: list[SummaryRow]) -> str:
    headers = ("runtime", "detected", "action", "result")
    table = [headers] + [
        (row.runtime, "yes" if row.detected else "no", row.action, row.result) for row in rows
    ]
    widths = [max(len(line[col]) for line in table) for col in range(len(headers))]
    rendered = [
        "  ".join(cell.ljust(width) for cell, width in zip(line, widths)).rstrip()
        for line in table
    ]
    rendered.insert(1, "  ".join("-" * width for width in widths))
    return "\n".join(rendered)


def print_post_install_info(config: TelemetryConfig, targets: list[str]) -> None:
    print(f"\nspool dir: {spool_dir(config)}")
    print(f"state dir: {state_dir(config)}")
    if config.endpoint:
        print(f"connectivity self-test: {connectivity_self_test(config)}")
    else:
        print("no endpoint configured; spans stay in the local spool (local-only mode)")
    for runtime in targets:
        if runtime in LOG_WATCH_RUNTIMES:
            print(f"start the {runtime} log watcher (log_watch collection layer):")
            print(
                f"  PYTHONPATH={shlex.quote(str(_REPO_ROOT))} "
                f"{_python_executable()} {WATCH_SCRIPT} --runtime {runtime}"
            )


def cmd_status(config: TelemetryConfig) -> int:
    payload = {
        "endpoint": config.endpoint,
        "token": mask_token(config.token),
        "service": config.service,
        "tenant": config.tenant,
        "environment": config.environment,
        "capture_content": config.capture_content,
        "output": config.output,
        "home": str(config.home),
        "enabled": config.enabled,
        "spool_depth": Spool(spool_dir(config)).depth(),
        "config_file": str(config_file_path()),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    detections = detect_runtimes()
    for runtime in RUNTIMES:
        detected = detections[runtime]
        print(f"\n==> {runtime}: detected={'yes' if detected else 'no'}")
        if detected:
            run_adapter_status(runtime)
    return 0


# ---------------------------------------------------------------------------
# Target selection
# ---------------------------------------------------------------------------


def _confirm(prompt: str) -> bool:
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


def resolve_targets(
    args: argparse.Namespace,
    detections: dict[str, bool],
    action: str,
) -> list[str]:
    if args.runtimes:
        ordered: list[str] = []
        for runtime in args.runtimes:
            if runtime not in ordered:
                ordered.append(runtime)
        return ordered
    detected = [runtime for runtime in RUNTIMES if detections[runtime]]
    if args.auto:
        return detected
    if not detected:
        print("no supported agent runtimes detected.")
        print("use --runtime <name> to force one of: " + ", ".join(RUNTIMES))
        return []
    print("detected runtimes: " + ", ".join(detected))
    if _confirm(f"{action} agent telemetry for these {len(detected)} runtime(s)?"):
        return detected
    print("nothing selected; exiting.")
    return []


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="setup",
        description="Detect agent runtimes and install the telemetry adapters.",
    )
    parser.add_argument(
        "--auto", action="store_true", help="non-interactive: install all detected runtimes"
    )
    parser.add_argument(
        "--runtime",
        dest="runtimes",
        action="append",
        choices=RUNTIMES,
        help="target a specific runtime (repeatable)",
    )
    parser.add_argument("--uninstall", action="store_true", help="remove instead of install")
    parser.add_argument(
        "--status", action="store_true", help="show config + per-runtime install state"
    )
    parser.add_argument("--endpoint", default=None, help="OTLP HTTP traces URL")
    parser.add_argument("--token", default=None, help="bearer token for the endpoint")
    parser.add_argument("--tenant", default=None, help="tenant id")
    parser.add_argument("--service", default=None, help="service name")
    parser.add_argument(
        "--print-env",
        action="store_true",
        help="echo export lines for the config flags instead of writing config.json",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _run(args)
    except Exception as exc:  # setup must never crash with a traceback
        print(f"setup: warning: {exc}", file=sys.stderr)
        return 0


def _run(args: argparse.Namespace) -> int:
    values = collect_config_values(args)
    if args.print_env:
        print_env_exports(values)
        if not (args.auto or args.runtimes or args.uninstall or args.status):
            return 0
    elif values:
        path = write_config(values)
        printable = {
            key: (mask_token(value) if key == "token" else value)
            for key, value in values.items()
        }
        print(f"wrote {path} (mode 600): {json.dumps(printable, ensure_ascii=False)}")

    config = load_config()
    if args.status:
        return cmd_status(config)

    detections = detect_runtimes()
    action = "uninstall" if args.uninstall else "install"
    targets = resolve_targets(args, detections, action)
    if not targets:
        return 0

    rows = [
        SummaryRow(
            runtime=runtime,
            detected=detections.get(runtime, False),
            action=action,
            result=run_adapter(runtime, action),
        )
        for runtime in targets
    ]
    print("\nsummary:")
    print(render_summary(rows))
    if action == "install":
        print_post_install_info(config, targets)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
