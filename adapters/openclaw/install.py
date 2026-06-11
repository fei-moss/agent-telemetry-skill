"""Installer for the agent-telemetry OpenClaw plugin.

Stdlib-only. Subcommands:

    python3 install.py status               show detection + install state
    python3 install.py install [--yes]      copy the plugin into OpenClaw
    python3 install.py uninstall [--yes]    remove the installed plugin

The installer detects an OpenClaw installation (``$OPENCLAW_HOME``,
``~/.openclaw``, or ``~/.config/openclaw``) and copies the plugin into
``<openclaw_home>/extensions/agent-telemetry/``. It is idempotent: re-running
``install`` rewrites the same files; ``uninstall`` removes only the files this
script manages. If no OpenClaw installation is found, precise manual
instructions are printed instead.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


PLUGIN_ID = "agent-telemetry"
PLUGIN_SOURCE_NAME = "telemetry-plugin.ts"
INSTALL_DIR_NAME = "extensions"
MANAGED_FILES = ("index.ts", "package.json", "openclaw.plugin.json")

PACKAGE_JSON = {
    "name": "openclaw-agent-telemetry-plugin",
    "version": "0.1.0",
    "type": "module",
    "openclaw": {"extensions": ["./index.ts"]},
}

PLUGIN_MANIFEST = {
    "id": PLUGIN_ID,
    "name": "Agent Telemetry",
    "description": (
        "Reports OpenClaw sessions, messages, tool calls, and model usage "
        "as OTLP traces with privacy-first redaction."
    ),
    "activation": {"onStartup": True},
    "configSchema": {"type": "object", "additionalProperties": True},
}

CONFIG_SNIPPET = """{
  "plugins": {
    "enabled": true,
    "entries": {
      "agent-telemetry": { "enabled": true }
    },
    "load": { "paths": ["%(plugin_dir)s"] }
  }
}"""


def openclaw_home_candidates() -> list[Path]:
    candidates: list[Path] = []
    env_home = os.getenv("OPENCLAW_HOME")
    if env_home:
        candidates.append(Path(env_home).expanduser())
    candidates.append(Path.home() / ".openclaw")
    candidates.append(Path.home() / ".config" / "openclaw")
    return candidates


def detect_openclaw_home() -> Path | None:
    for candidate in openclaw_home_candidates():
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    return None


def plugin_source_path() -> Path:
    return Path(__file__).resolve().parent / PLUGIN_SOURCE_NAME


def install_target(openclaw_home: Path) -> Path:
    return openclaw_home / INSTALL_DIR_NAME / PLUGIN_ID


def managed_file_contents(source: Path) -> dict[str, str]:
    return {
        "index.ts": source.read_text(encoding="utf-8"),
        "package.json": json.dumps(PACKAGE_JSON, ensure_ascii=False, indent=2) + "\n",
        "openclaw.plugin.json": json.dumps(PLUGIN_MANIFEST, ensure_ascii=False, indent=2) + "\n",
    }


def is_installed(target: Path) -> bool:
    return all((target / name).is_file() for name in MANAGED_FILES)


def confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def print_manual_instructions(source: Path) -> None:
    print("No OpenClaw installation detected.")
    print("Checked:")
    for candidate in openclaw_home_candidates():
        print(f"  - {candidate}")
    print()
    print("Manual install steps:")
    print(f"  1. mkdir -p <openclaw_home>/extensions/{PLUGIN_ID}")
    print(f"  2. cp {source} <openclaw_home>/extensions/{PLUGIN_ID}/index.ts")
    print("  3. Add package.json + openclaw.plugin.json next to index.ts")
    print("     (run this script with OPENCLAW_HOME set to generate them).")
    print("  4. Enable the plugin in your OpenClaw config (openclaw.json):")
    print(CONFIG_SNIPPET % {"plugin_dir": f"<openclaw_home>/extensions/{PLUGIN_ID}"})
    print("  5. Restart OpenClaw, then verify with: openclaw plugins list")


def cmd_status() -> int:
    source = plugin_source_path()
    print(f"plugin source: {source} ({'found' if source.is_file() else 'MISSING'})")
    home = detect_openclaw_home()
    if home is None:
        print("openclaw home: not found")
        print("checked: " + ", ".join(str(c) for c in openclaw_home_candidates()))
        return 0
    target = install_target(home)
    print(f"openclaw home: {home}")
    print(f"install target: {target}")
    print(f"installed: {'yes' if is_installed(target) else 'no'}")
    return 0


def cmd_install(assume_yes: bool) -> int:
    source = plugin_source_path()
    if not source.is_file():
        print(f"error: plugin source not found at {source}", file=sys.stderr)
        return 1

    home = detect_openclaw_home()
    if home is None:
        print_manual_instructions(source)
        return 1

    target = install_target(home)
    contents = managed_file_contents(source)
    changed = [
        name
        for name, body in contents.items()
        if not (target / name).is_file()
        or (target / name).read_text(encoding="utf-8") != body
    ]
    if not changed:
        print(f"already installed and up to date: {target}")
        return 0

    print(f"installing to {target}")
    print("files to write: " + ", ".join(changed))
    if not confirm("Proceed?", assume_yes):
        print("aborted")
        return 1

    try:
        target.mkdir(parents=True, exist_ok=True)
        for name in changed:
            (target / name).write_text(contents[name], encoding="utf-8")
    except OSError as exc:
        print(f"error: failed to write plugin files: {exc}", file=sys.stderr)
        return 1

    print("installed.")
    print("Enable the plugin in your OpenClaw config (openclaw.json) if needed:")
    print(CONFIG_SNIPPET % {"plugin_dir": str(target)})
    print("Then restart OpenClaw and verify with: openclaw plugins list")
    return 0


def cmd_uninstall(assume_yes: bool) -> int:
    home = detect_openclaw_home()
    if home is None:
        print("No OpenClaw installation detected; nothing to uninstall.")
        return 0

    target = install_target(home)
    existing = [name for name in MANAGED_FILES if (target / name).is_file()]
    if not existing:
        print(f"not installed: {target}")
        return 0

    print(f"removing from {target}: " + ", ".join(existing))
    if not confirm("Proceed?", assume_yes):
        print("aborted")
        return 1

    try:
        for name in existing:
            (target / name).unlink()
        if target.is_dir() and not any(target.iterdir()):
            target.rmdir()
    except OSError as exc:
        print(f"error: failed to remove plugin files: {exc}", file=sys.stderr)
        return 1

    print("uninstalled. Remove the plugin entry from openclaw.json if you added one.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="install.py",
        description="Install the agent-telemetry plugin into a local OpenClaw installation.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show detection and install state")
    install = sub.add_parser("install", help="copy the plugin into OpenClaw")
    install.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    uninstall = sub.add_parser("uninstall", help="remove the installed plugin")
    uninstall.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "status":
        return cmd_status()
    if args.command == "install":
        return cmd_install(assume_yes=args.yes)
    if args.command == "uninstall":
        return cmd_uninstall(assume_yes=args.yes)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
