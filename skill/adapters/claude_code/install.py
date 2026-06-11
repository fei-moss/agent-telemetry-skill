"""Installer for the agent-telemetry Claude Code hooks adapter.

Stdlib-only. Subcommands:

    python3 install.py status     [--settings-path PATH]
    python3 install.py install    [--yes] [--settings-path PATH]
    python3 install.py uninstall  [--yes] [--settings-path PATH]

``install`` merges the hook entries from ``hooks_fragment.json`` (with
absolute paths resolved at install time) into the Claude Code settings file
(default ``~/.claude/settings.json``, created if missing) without touching
any pre-existing user hooks. Every managed entry is identified by its command
containing ``claude_code_hook.py``, so ``uninstall`` removes exactly those
entries and nothing else. The settings file is backed up once to
``<file>.bak-agent-telemetry`` before the first modification.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
import shutil
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
FRAGMENT_PATH = Path(__file__).resolve().parent / "hooks_fragment.json"
HOOK_SCRIPT = REPO_ROOT / "scripts" / "hooks" / "claude_code_hook.py"
HOOK_MARKER = "claude_code_hook.py"
BACKUP_SUFFIX = ".bak-agent-telemetry"


def default_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def render_fragment() -> dict[str, list[Any]]:
    """Load the fragment with placeholder tokens resolved to absolute paths."""
    text = FRAGMENT_PATH.read_text(encoding="utf-8")
    replacements = {
        "__PYTHON__": shlex.quote(sys.executable or "python3"),
        "__HOOK_SCRIPT__": shlex.quote(str(HOOK_SCRIPT)),
        "__REPO_ROOT__": shlex.quote(str(REPO_ROOT)),
    }
    for token, value in replacements.items():
        text = text.replace(token, value)
    data = json.loads(text)
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        raise ValueError(f"{FRAGMENT_PATH} must contain a top-level 'hooks' object")
    return hooks


def is_our_group(group: Any) -> bool:
    """True when a matcher group was installed by this adapter."""
    if not isinstance(group, dict):
        return False
    entries = group.get("hooks")
    if not isinstance(entries, list):
        return False
    return any(
        isinstance(entry, dict) and HOOK_MARKER in str(entry.get("command", ""))
        for entry in entries
    )


def merge_fragment(
    settings: dict[str, Any],
    fragment_hooks: dict[str, list[Any]],
) -> tuple[dict[str, Any], list[str]]:
    """Return (new settings, change descriptions); the input is not mutated.

    User-managed matcher groups are preserved untouched; our groups are
    appended (or replaced in place when paths changed since the last install).
    """
    merged = json.loads(json.dumps(settings))
    hooks = merged.setdefault("hooks", {})
    changes: list[str] = []
    for event, desired_groups in fragment_hooks.items():
        existing = hooks.get(event)
        groups = list(existing) if isinstance(existing, list) else []
        ours = [group for group in groups if is_our_group(group)]
        if ours == desired_groups:
            continue
        hooks[event] = [group for group in groups if not is_our_group(group)] + list(
            desired_groups
        )
        changes.append(f"{event}: {'updated' if ours else 'added'} telemetry hook entry")
    return merged, changes


def remove_fragment(settings: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Return (new settings, removed group count); the input is not mutated."""
    cleaned = json.loads(json.dumps(settings))
    hooks = cleaned.get("hooks")
    removed = 0
    if not isinstance(hooks, dict):
        return cleaned, 0
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept = [group for group in groups if not is_our_group(group)]
        removed += len(groups) - len(kept)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        del cleaned["hooks"]
    return cleaned, removed


def installed_events(settings: dict[str, Any]) -> list[str]:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return []
    return [
        event
        for event, groups in hooks.items()
        if isinstance(groups, list) and any(is_our_group(group) for group in groups)
    ]


def read_settings(path: Path) -> dict[str, Any] | None:
    """Return the settings dict, {} when the file is absent, None when unreadable."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def write_settings(path: Path, settings: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def backup_settings(path: Path) -> Path | None:
    """Copy the settings file to its backup once; return the backup path if made."""
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if not path.is_file() or backup.exists():
        return None
    shutil.copy2(path, backup)
    return backup


def confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def print_runtime_guidance(settings_path: Path) -> None:
    print(f"Claude Code not detected: {settings_path.parent} does not exist.")
    print("Install Claude Code first (https://code.claude.com), or pass")
    print("--settings-path pointing at the settings file to manage explicitly.")


def cmd_status(settings_path: Path) -> int:
    settings = read_settings(settings_path)
    if settings is None:
        print("not-installed")
        print(f"detail: settings file unreadable or invalid JSON: {settings_path}")
        return 0
    expected = tuple(render_fragment())
    present = [event for event in expected if event in installed_events(settings)]
    print("installed" if len(present) == len(expected) else "not-installed")
    print(f"detail: settings={settings_path}")
    print(
        f"detail: hook script={HOOK_SCRIPT} "
        f"({'found' if HOOK_SCRIPT.is_file() else 'MISSING'})"
    )
    print(f"detail: managed events present={','.join(present) if present else 'none'}")
    return 0


def cmd_install(settings_path: Path, *, assume_yes: bool, explicit_path: bool) -> int:
    if not HOOK_SCRIPT.is_file():
        print(f"error: hook script not found at {HOOK_SCRIPT}", file=sys.stderr)
        return 1
    if not explicit_path and not settings_path.parent.is_dir():
        print_runtime_guidance(settings_path)
        return 0
    settings = read_settings(settings_path)
    if settings is None:
        print(
            f"error: {settings_path} exists but is not a valid JSON object; "
            "refusing to modify it",
            file=sys.stderr,
        )
        return 1
    merged, changes = merge_fragment(settings, render_fragment())
    if not changes:
        print(f"already installed and up to date: {settings_path}")
        return 0
    print(f"merging telemetry hooks into {settings_path}")
    for change in changes:
        print(f"  - {change}")
    if not confirm("Proceed?", assume_yes):
        print("aborted")
        return 1
    try:
        backup = backup_settings(settings_path)
        if backup is not None:
            print(f"backed up existing settings to {backup}")
        write_settings(settings_path, merged)
    except OSError as exc:
        print(f"error: failed to write {settings_path}: {exc}", file=sys.stderr)
        return 1
    print("installed. Restart Claude Code (or start a new session) to activate the hooks.")
    return 0


def cmd_uninstall(settings_path: Path, *, assume_yes: bool) -> int:
    settings = read_settings(settings_path)
    if settings is None:
        print(
            f"error: {settings_path} is not a valid JSON object; refusing to modify it",
            file=sys.stderr,
        )
        return 1
    cleaned, removed = remove_fragment(settings)
    if removed == 0:
        print(f"not installed: {settings_path} has no telemetry hook entries")
        return 0
    plural = "y" if removed == 1 else "ies"
    print(f"removing {removed} telemetry hook entr{plural} from {settings_path}")
    if not confirm("Proceed?", assume_yes):
        print("aborted")
        return 1
    try:
        backup = backup_settings(settings_path)
        if backup is not None:
            print(f"backed up existing settings to {backup}")
        write_settings(settings_path, cleaned)
    except OSError as exc:
        print(f"error: failed to write {settings_path}: {exc}", file=sys.stderr)
        return 1
    print("uninstalled. Pre-existing user hooks were left untouched.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="install.py",
        description="Install agent-telemetry hooks into Claude Code settings.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, needs_yes in (("status", False), ("install", True), ("uninstall", True)):
        command = sub.add_parser(name)
        command.add_argument(
            "--settings-path",
            default=None,
            help="settings file to manage (default: ~/.claude/settings.json)",
        )
        if needs_yes:
            command.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    explicit_path = args.settings_path is not None
    settings_path = (
        Path(args.settings_path).expanduser() if explicit_path else default_settings_path()
    )
    if args.command == "status":
        return cmd_status(settings_path)
    if args.command == "install":
        return cmd_install(settings_path, assume_yes=args.yes, explicit_path=explicit_path)
    if args.command == "uninstall":
        return cmd_uninstall(settings_path, assume_yes=args.yes)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
