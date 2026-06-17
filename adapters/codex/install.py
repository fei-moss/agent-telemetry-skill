"""Installer for the agent-telemetry Codex CLI adapter.

Stdlib-only. Subcommands:

    python3 install.py status
    python3 install.py install [--yes]
    python3 install.py uninstall [--yes]

The Codex adapter is watcher-based: ``scripts/watch_sessions.py --runtime
codex`` tails ``~/.codex/sessions/**/rollout-*.jsonl`` and converts entries
into spans (collection layer ``log_watch``). ``install`` (a) verifies the
watcher can see the Codex session files and (b) wires Codex's top-level
``notify`` key in ``config.toml`` to ``notify_hook.py`` so every turn-end
triggers one "watch once + drain" cycle. The notify line lives in a
clearly-delimited managed block; ``uninstall`` removes exactly that block.
``config.toml`` is backed up once to ``<file>.bak-agent-telemetry`` before
the first modification.

Codex supports only ONE notify program. If ``notify`` is already configured
by something else, the installer leaves it untouched and explains how to run
the watcher persistently instead (watcher-only mode).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
NOTIFY_HOOK = Path(__file__).resolve().parent / "notify_hook.py"
WATCH_SCRIPT = REPO_ROOT / "scripts" / "watch_sessions.py"
CONFIG_FILE_NAME = "config.toml"
BACKUP_SUFFIX = ".bak-agent-telemetry"
BLOCK_BEGIN = "# >>> agent-telemetry codex adapter (managed) >>>"
BLOCK_END = "# <<< agent-telemetry codex adapter (managed) <<<"
WATCHER_CHECK_TIMEOUT_SECONDS = 30.0

_NOTIFY_KEY_RE = re.compile(r"^\s*notify\s*=")
_TABLE_HEADER_RE = re.compile(r"^\s*\[")


def default_codex_home() -> Path:
    env_home = os.getenv("CODEX_HOME")
    if env_home:
        return Path(env_home).expanduser()
    return Path.home() / ".codex"


def config_path(codex_home: Path) -> Path:
    return codex_home / CONFIG_FILE_NAME


def render_block() -> str:
    """The managed config.toml block; JSON string escaping is valid TOML."""
    notify_value = json.dumps([sys.executable or "python3", str(NOTIFY_HOOK)])
    return "\n".join(
        (
            BLOCK_BEGIN,
            "# Each Codex turn-end triggers one telemetry watcher poll + spool drain.",
            "# Managed by adapters/codex/install.py — do not edit between these markers.",
            f"notify = {notify_value}",
            BLOCK_END,
        )
    )


def split_managed_block(text: str) -> tuple[str, str | None]:
    """Return (text without our block, the block) without mutating input."""
    lines = text.splitlines()
    begin = end = None
    for index, line in enumerate(lines):
        if begin is None and line.strip() == BLOCK_BEGIN:
            begin = index
        elif begin is not None and line.strip() == BLOCK_END:
            end = index
            break
    if begin is None or end is None:
        return text, None
    block = "\n".join(lines[begin:end + 1])
    remaining = lines[:begin] + lines[end + 1:]
    if begin < len(remaining) and remaining[begin] == "":
        del remaining[begin]  # drop the blank separator the block carried
    return "\n".join(remaining), block


def find_foreign_notify(text_without_block: str) -> str | None:
    """Return a top-level ``notify = ...`` line not managed by us, if any."""
    for line in text_without_block.splitlines():
        if _TABLE_HEADER_RE.match(line):
            return None  # keys below a [table] header are not top-level
        if _NOTIFY_KEY_RE.match(line):
            return line.strip()
    return None


def merge_block(text: str) -> str:
    """Return config text with the managed block fresh at the top.

    The block sits before any ``[table]`` header so ``notify`` stays a
    top-level key. The input text is not mutated.
    """
    remaining, _ = split_managed_block(text)
    remaining = remaining.strip("\n")
    if remaining:
        return f"{render_block()}\n\n{remaining}\n"
    return f"{render_block()}\n"


def validate_toml(text: str) -> str | None:
    """Return an error message when the text is invalid TOML, else None.

    Uses ``tomllib`` when available (Python 3.11+); on 3.10 validation is
    skipped — the managed block only contains comments and one JSON-escaped
    array, which is valid TOML by construction.
    """
    try:
        import tomllib
    except ModuleNotFoundError:
        return None
    try:
        tomllib.loads(text)
    except Exception as exc:
        return str(exc)
    return None


def read_config_text(path: Path) -> str | None:
    """Return file text, '' when absent, None when unreadable."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError:
        return None


def backup_config(path: Path) -> Path | None:
    """Copy config.toml to its backup once; return the backup path if made."""
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


def run_watcher_check() -> None:
    """Best-effort sanity check that the watcher sees Codex session files."""
    if not WATCH_SCRIPT.is_file():
        print(f"watcher check: SKIPPED ({WATCH_SCRIPT} missing)")
        return
    try:
        proc = subprocess.run(
            [
                sys.executable or "python3",
                str(WATCH_SCRIPT),
                "--runtime",
                "codex",
                "--status",
            ],
            capture_output=True,
            text=True,
            timeout=WATCHER_CHECK_TIMEOUT_SECONDS,
        )
        payload = json.loads(proc.stdout)
        offsets = payload.get("offsets") or {}
        print(
            "watcher check: OK "
            f"(enabled={payload.get('enabled')}, tracked_files={len(offsets)}, "
            f"spool_depth={payload.get('spool_depth')})"
        )
    except Exception as exc:  # telemetry must never break the installer
        print(f"watcher check: WARNING ({exc})")


def print_runtime_guidance(codex_home: Path) -> None:
    print(f"Codex CLI not detected: {codex_home} does not exist.")
    print("Install Codex first (https://developers.openai.com/codex), or set")
    print("CODEX_HOME to point at the Codex home directory to manage explicitly.")


def _watcher_service():
    """Import the cross-platform persistent-watcher service helper."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from agent_telemetry_skill import watcher_service

    return watcher_service


def install_persistent_watcher() -> str:
    """Install + start a resident watcher service (launchd/systemd). Best effort."""
    try:
        return _watcher_service().install(
            "codex", watch_script=str(WATCH_SCRIPT), pythonpath=str(REPO_ROOT)
        )
    except Exception as exc:  # never break the installer
        return f"service error: {exc}"


def uninstall_persistent_watcher() -> str:
    try:
        return _watcher_service().uninstall("codex")
    except Exception as exc:
        return f"service error: {exc}"


def print_watcher_only_guidance(foreign_notify: str) -> None:
    print("notify wiring: SKIPPED — config.toml already has a notify program:")
    print(f"  {foreign_notify}")
    print("Codex supports only one notify program; yours was left untouched.")
    print("Installing a RESIDENT watcher instead (no notify needed):")
    print(f"  resident watcher: {install_persistent_watcher()}")


def cmd_status(codex_home: Path) -> int:
    path = config_path(codex_home)
    if not codex_home.is_dir():
        print("not-installed")
        print(f"detail: codex home not found: {codex_home}")
        return 0
    text = read_config_text(path)
    if text is None:
        print("not-installed")
        print(f"detail: config unreadable: {path}")
        return 0
    remaining, block = split_managed_block(text)
    print("installed" if block == render_block() else "not-installed")
    print(f"detail: codex home={codex_home}")
    print(f"detail: config={path}")
    print(
        f"detail: notify hook={NOTIFY_HOOK} "
        f"({'found' if NOTIFY_HOOK.is_file() else 'MISSING'})"
    )
    if block is not None and block != render_block():
        print("detail: managed notify block present but stale (re-run install)")
    foreign = find_foreign_notify(remaining)
    if foreign:
        print(f"detail: foreign notify present: {foreign} (watcher-only mode)")
    sessions = codex_home / "sessions"
    print(f"detail: sessions dir={sessions} ({'found' if sessions.is_dir() else 'missing'})")
    return 0


def cmd_install(codex_home: Path, *, assume_yes: bool) -> int:
    if not NOTIFY_HOOK.is_file():
        print(f"error: notify hook not found at {NOTIFY_HOOK}", file=sys.stderr)
        return 1
    if not codex_home.is_dir():
        print_runtime_guidance(codex_home)
        return 0
    run_watcher_check()

    path = config_path(codex_home)
    text = read_config_text(path)
    if text is None:
        print(f"error: cannot read {path}", file=sys.stderr)
        return 1
    remaining, block = split_managed_block(text)
    if block == render_block():
        print(f"already installed and up to date: {path}")
        return 0
    foreign = find_foreign_notify(remaining)
    if foreign:
        print_watcher_only_guidance(foreign)
        return 0

    merged = merge_block(text)
    toml_error = validate_toml(merged)
    if toml_error:
        print(f"error: refusing to write invalid TOML: {toml_error}", file=sys.stderr)
        return 1
    action = "updating managed notify block in" if block else "adding managed notify block to"
    print(f"{action} {path}")
    print(f"  - notify -> {sys.executable or 'python3'} {NOTIFY_HOOK}")
    if not confirm("Proceed?", assume_yes):
        print("aborted")
        return 1
    try:
        backup = backup_config(path)
        if backup is not None:
            print(f"backed up existing config to {backup}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(merged, encoding="utf-8")
    except OSError as exc:
        print(f"error: failed to write {path}: {exc}", file=sys.stderr)
        return 1
    print("installed. Each Codex turn-end now triggers a telemetry watcher poll.")
    print("For continuous capture while Codex is idle, also run:")
    print(f"  python3 {WATCH_SCRIPT} --runtime codex")
    return 0


def cmd_uninstall(codex_home: Path, *, assume_yes: bool) -> int:
    if not codex_home.is_dir():
        print(f"Codex CLI not detected: {codex_home}; nothing to uninstall.")
        return 0
    # Always tear down the resident watcher (installed when notify was occupied),
    # independent of whether a managed notify block exists.
    print(f"resident watcher: {uninstall_persistent_watcher()}")
    path = config_path(codex_home)
    text = read_config_text(path)
    if text is None:
        print(f"error: cannot read {path}", file=sys.stderr)
        return 1
    remaining, block = split_managed_block(text)
    if block is None:
        print(f"not installed: {path} has no managed notify block")
        return 0
    print(f"removing managed notify block from {path}")
    if not confirm("Proceed?", assume_yes):
        print("aborted")
        return 1
    cleaned = remaining.strip("\n")
    try:
        backup = backup_config(path)
        if backup is not None:
            print(f"backed up existing config to {backup}")
        path.write_text(f"{cleaned}\n" if cleaned else "", encoding="utf-8")
    except OSError as exc:
        print(f"error: failed to write {path}: {exc}", file=sys.stderr)
        return 1
    print("uninstalled. Pre-existing config keys were left untouched.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="install.py",
        description="Wire agent-telemetry into the Codex CLI (log watcher + notify hook).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show detection and install state")
    install = sub.add_parser("install", help="wire notify and verify the watcher")
    install.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    uninstall = sub.add_parser("uninstall", help="remove the managed notify block")
    uninstall.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    codex_home = default_codex_home()
    if args.command == "status":
        return cmd_status(codex_home)
    if args.command == "install":
        return cmd_install(codex_home, assume_yes=args.yes)
    if args.command == "uninstall":
        return cmd_uninstall(codex_home, assume_yes=args.yes)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
