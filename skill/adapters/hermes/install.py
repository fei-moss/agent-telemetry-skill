"""Installer for the agent-telemetry Hermes plugin.

Stdlib-only. Subcommands:

    python3 install.py status               show detection + install state
    python3 install.py install [--yes]      copy the plugin into Hermes
    python3 install.py uninstall [--yes]    remove the installed plugin

The installer detects a Hermes installation (``$HERMES_HOME`` or ``~/.hermes``)
and copies the plugin package contents into
``<hermes_home>/plugins/agent-telemetry/`` together with a vendored copy of the
``agent_telemetry_skill`` package so imports resolve without pip. It is
idempotent: only changed files are rewritten and every change is printed. Any
pre-existing differing top-level file is backed up once to
``<file>.bak-agent-telemetry``. If no Hermes installation is found, guidance is
printed and the script exits 0.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


PLUGIN_ID = "agent-telemetry"
PLUGIN_DIR_NAME = "plugins"
SKILL_PACKAGE_NAME = "agent_telemetry_skill"
BACKUP_SUFFIX = ".bak-agent-telemetry"
SKIP_DIR_NAMES = ("__pycache__",)


def hermes_home_candidates() -> list[Path]:
    candidates: list[Path] = []
    env_home = os.getenv("HERMES_HOME")
    if env_home:
        candidates.append(Path(env_home).expanduser())
    candidates.append(Path.home() / ".hermes")
    return candidates


def detect_hermes_home() -> Path | None:
    for candidate in hermes_home_candidates():
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    return None


def plugin_source_dir() -> Path:
    return Path(__file__).resolve().parent / "agent_telemetry"


def skill_package_source_dir() -> Path | None:
    here = Path(__file__).resolve().parent
    candidates = [here, *here.parents[:3]]
    for candidate in candidates:
        package = candidate / SKILL_PACKAGE_NAME
        if (package / "__init__.py").is_file():
            return package
    return None


def install_target(hermes_home: Path) -> Path:
    return hermes_home / PLUGIN_DIR_NAME / PLUGIN_ID


def collect_files(source: Path, target: Path) -> dict[Path, Path]:
    """Map each source file to its destination, skipping caches."""
    mapping: dict[Path, Path] = {}
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        if any(part in SKIP_DIR_NAMES for part in relative.parts):
            continue
        if path.suffix == ".pyc":
            continue
        mapping[path] = target / relative
    return mapping


def planned_files(target: Path) -> dict[Path, Path]:
    plan = collect_files(plugin_source_dir(), target)
    skill_source = skill_package_source_dir()
    if skill_source is not None:
        plan.update(collect_files(skill_source, target / SKILL_PACKAGE_NAME))
    return plan


def is_installed(target: Path) -> bool:
    return (
        (target / "__init__.py").is_file()
        and (target / "plugin.yaml").is_file()
        and (target / SKILL_PACKAGE_NAME / "__init__.py").is_file()
    )


def confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def backup_if_needed(destination: Path) -> Path | None:
    """Back up a pre-existing top-level file once before overwriting it."""
    backup = destination.with_name(destination.name + BACKUP_SUFFIX)
    if backup.exists():
        return None
    backup.write_bytes(destination.read_bytes())
    return backup


def print_manual_instructions() -> None:
    print("No Hermes installation detected.")
    print("Checked:")
    for candidate in hermes_home_candidates():
        print(f"  - {candidate}")
    print()
    print("Manual install steps:")
    print(f"  1. mkdir -p <hermes_home>/plugins/{PLUGIN_ID}")
    print(f"  2. cp -R {plugin_source_dir()}/. <hermes_home>/plugins/{PLUGIN_ID}/")
    skill_source = skill_package_source_dir()
    if skill_source is not None:
        print(f"  3. cp -R {skill_source} <hermes_home>/plugins/{PLUGIN_ID}/{SKILL_PACKAGE_NAME}")
    else:
        print(f"  3. copy the {SKILL_PACKAGE_NAME} package next to __init__.py")
    print("  4. Restart Hermes; the plugin registers its hooks automatically.")


def cmd_status() -> int:
    home = detect_hermes_home()
    if home is None:
        print("not-installed")
        print("hermes home: not found")
        print("checked: " + ", ".join(str(c) for c in hermes_home_candidates()))
        return 0
    target = install_target(home)
    print("installed" if is_installed(target) else "not-installed")
    print(f"hermes home: {home}")
    print(f"install target: {target}")
    return 0


def cmd_install(assume_yes: bool) -> int:
    source = plugin_source_dir()
    if not (source / "__init__.py").is_file():
        print(f"error: plugin source not found at {source}", file=sys.stderr)
        return 1
    if skill_package_source_dir() is None:
        print(f"error: {SKILL_PACKAGE_NAME} package not found near {source}", file=sys.stderr)
        return 1

    home = detect_hermes_home()
    if home is None:
        print_manual_instructions()
        return 0

    target = install_target(home)
    plan = planned_files(target)
    changed = {
        src: dst
        for src, dst in plan.items()
        if not dst.is_file() or dst.read_bytes() != src.read_bytes()
    }
    if not changed:
        print(f"already installed and up to date: {target}")
        return 0

    print(f"installing to {target}")
    print(f"files to write: {len(changed)}")
    if not confirm("Proceed?", assume_yes):
        print("aborted")
        return 1

    try:
        for src, dst in changed.items():
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.is_file() and dst.parent == target:
                backup = backup_if_needed(dst)
                if backup is not None:
                    print(f"  backed up: {dst} -> {backup}")
            dst.write_bytes(src.read_bytes())
            print(f"  wrote: {dst}")
    except OSError as exc:
        print(f"error: failed to write plugin files: {exc}", file=sys.stderr)
        return 1

    print("installed. Restart Hermes to load the plugin.")
    return 0


def cmd_uninstall(assume_yes: bool) -> int:
    home = detect_hermes_home()
    if home is None:
        print("No Hermes installation detected; nothing to uninstall.")
        return 0

    target = install_target(home)
    plan = planned_files(target)
    existing = [dst for dst in plan.values() if dst.is_file()]
    if not existing and not target.is_dir():
        print(f"not installed: {target}")
        return 0

    print(f"removing {len(existing)} managed file(s) from {target}")
    if not confirm("Proceed?", assume_yes):
        print("aborted")
        return 1

    try:
        for dst in existing:
            dst.unlink()
            print(f"  removed: {dst}")
        _prune_empty_dirs(target)
    except OSError as exc:
        print(f"error: failed to remove plugin files: {exc}", file=sys.stderr)
        return 1

    print("uninstalled.")
    return 0


def _prune_empty_dirs(target: Path) -> None:
    if not target.is_dir():
        return
    for path in sorted(target.rglob("*"), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    if not any(target.iterdir()):
        target.rmdir()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="install.py",
        description="Install the agent-telemetry plugin into a local Hermes installation.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show detection and install state")
    install = sub.add_parser("install", help="copy the plugin into Hermes")
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
