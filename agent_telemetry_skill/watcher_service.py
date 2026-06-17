"""Install a persistent log-watcher as an OS service (launchd / systemd --user).

Watcher-based runtimes (Codex, Hermes) need a resident process tailing session
logs — unlike hook/notify triggers, a service starts at login/boot and restarts
on failure, so "install once, it stays up". The watcher reads its backend
config (endpoint/token/service) from ``~/.agent-telemetry/config.json``; secrets
are NEVER written into the unit file.

Stdlib only. Every operation is best-effort and returns a short status string;
nothing here may raise into the installer.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path


SERVICE_PREFIX = "com.agent-telemetry"
DEFAULT_INTERVAL = 5.0
_RUN_TIMEOUT = 20.0


def service_label(runtime: str) -> str:
    return f"{SERVICE_PREFIX}.{runtime}-watcher"


# ---- pure renderers (unit-tested) -----------------------------------------

def render_launchd_plist(
    runtime: str,
    *,
    python_exe: str,
    watch_script: str,
    pythonpath: str,
    log_path: str,
    interval: float = DEFAULT_INTERVAL,
) -> bytes:
    """Return a launchd plist that keeps the watcher resident (RunAtLoad +
    KeepAlive). PYTHONPATH is pinned so the copy-installed package imports."""
    plist = {
        "Label": service_label(runtime),
        "ProgramArguments": [
            python_exe,
            watch_script,
            "--runtime",
            runtime,
            "--interval",
            str(interval),
        ],
        "EnvironmentVariables": {"PYTHONPATH": pythonpath},
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }
    return plistlib.dumps(plist)


def render_systemd_unit(
    runtime: str,
    *,
    python_exe: str,
    watch_script: str,
    pythonpath: str,
    interval: float = DEFAULT_INTERVAL,
) -> str:
    """Return a systemd --user unit that restarts the watcher on failure."""
    return (
        "[Unit]\n"
        f"Description=Agent Telemetry {runtime} watcher\n"
        "After=default.target\n\n"
        "[Service]\n"
        f"ExecStart={python_exe} {watch_script} --runtime {runtime} "
        f"--interval {interval}\n"
        f"Environment=PYTHONPATH={pythonpath}\n"
        "Restart=always\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


# ---- platform plumbing -----------------------------------------------------

def _launchd_plist_path(runtime: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{service_label(runtime)}.plist"


def _systemd_unit_path(runtime: str) -> Path:
    return (
        Path.home()
        / ".config"
        / "systemd"
        / "user"
        / f"agent-telemetry-{runtime}-watcher.service"
    )


def _has_systemd_user() -> bool:
    try:
        return (
            subprocess.run(
                ["systemctl", "--user", "--version"],
                capture_output=True,
                timeout=_RUN_TIMEOUT,
            ).returncode
            == 0
        )
    except Exception:
        return False


def supported() -> bool:
    """True when this platform has a service manager we can drive."""
    return sys.platform == "darwin" or _has_systemd_user()


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, capture_output=True, timeout=_RUN_TIMEOUT)


def _log_path() -> str:
    return str(Path.home() / ".agent-telemetry" / "watcher.log")


def install(
    runtime: str,
    *,
    watch_script: str,
    pythonpath: str,
    python_exe: str | None = None,
    interval: float = DEFAULT_INTERVAL,
) -> str:
    """Install + start a resident watcher service. Returns a status string."""
    python_exe = python_exe or sys.executable or "python3"
    try:
        Path(_log_path()).parent.mkdir(parents=True, exist_ok=True)
        if sys.platform == "darwin":
            return _install_launchd(runtime, python_exe, watch_script, pythonpath, interval)
        if _has_systemd_user():
            return _install_systemd(runtime, python_exe, watch_script, pythonpath, interval)
        return "unsupported platform (no launchd/systemd) — run watcher manually"
    except Exception as exc:  # never break the installer
        return f"service error: {exc}"


def _install_launchd(
    runtime: str, python_exe: str, watch_script: str, pythonpath: str, interval: float
) -> str:
    plist = _launchd_plist_path(runtime)
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(
        render_launchd_plist(
            runtime,
            python_exe=python_exe,
            watch_script=watch_script,
            pythonpath=pythonpath,
            log_path=_log_path(),
            interval=interval,
        )
    )
    _run(["launchctl", "unload", str(plist)])  # idempotent: drop any old copy
    _run(["launchctl", "load", "-w", str(plist)])
    return f"launchd loaded ({plist}); logs: {_log_path()}"


def _install_systemd(
    runtime: str, python_exe: str, watch_script: str, pythonpath: str, interval: float
) -> str:
    unit = _systemd_unit_path(runtime)
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(
        render_systemd_unit(
            runtime,
            python_exe=python_exe,
            watch_script=watch_script,
            pythonpath=pythonpath,
            interval=interval,
        ),
        encoding="utf-8",
    )
    _run(["systemctl", "--user", "daemon-reload"])
    _run(["systemctl", "--user", "enable", "--now", unit.name])
    return f"systemd --user enabled ({unit.name})"


def uninstall(runtime: str) -> str:
    """Stop + remove the resident watcher service. Returns a status string."""
    try:
        if sys.platform == "darwin":
            plist = _launchd_plist_path(runtime)
            if plist.exists():
                _run(["launchctl", "unload", str(plist)])
                plist.unlink()
                return f"launchd removed ({plist})"
            return "not installed"
        if _has_systemd_user():
            unit = _systemd_unit_path(runtime)
            if unit.exists():
                _run(["systemctl", "--user", "disable", "--now", unit.name])
                unit.unlink()
                _run(["systemctl", "--user", "daemon-reload"])
                return f"systemd --user removed ({unit.name})"
            return "not installed"
        return "unsupported platform"
    except Exception as exc:
        return f"service error: {exc}"
