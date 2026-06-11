"""Build a distributable Agent Telemetry skill package.

Produces a clean, self-contained tree (SKILL.md entrypoint + SDK + adapters +
installer + docs) under ``dist/`` and archives it as ``.tar.gz`` and ``.zip``,
excluding tests, caches, and build artifacts. Stdlib only.

Usage:
    python3 scripts/build_package.py [--outdir dist] [--no-zip] [--no-targz]
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import tarfile
from pathlib import Path
import zipfile


REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_NAME = "agent-telemetry-skill"

# Top-level files and directories that ship to end users.
INCLUDE_FILES = (
    "SKILL.md",
    "README.md",
    "产品说明.md",
    "用户侧使用文档.md",
    "使用说明.md",
    "pyproject.toml",
)
INCLUDE_DIRS = (
    "agent_telemetry_skill",
    "adapters",
    "scripts",
    "server",
    "collector",
    "docs",
    "examples",
)

# Names excluded anywhere in the tree.
EXCLUDE_NAMES = {"__pycache__", ".git", ".DS_Store", ".pytest_cache", "dist"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}
# Files excluded by repo-relative path (the build script itself is not shipped).
EXCLUDE_RELPATHS = {"scripts/build_package.py"}


INSTALL_MD = """# 安装 Agent Telemetry Skill

本包让本地运行的 Agent(Claude Code / Codex / OpenClaw / Hermes 等)把每次
运行的工具调用、模型调用、决策与错误上报到统一的遥测后端。默认只上报结构化
元数据并对密钥脱敏,完整内容需显式开启。

## 1. 一键安装

解压后进入目录,运行:

```bash
python3 scripts/setup.py --auto
```

它会自动探测本机已装的 Agent runtime,安装对应的 hook / plugin / 日志采集器,
并生成 `~/.agent-telemetry/config.json`。

## 2. 配置上报地址(由产品方提供)

```bash
python3 scripts/setup.py \\
  --endpoint "https://telemetry.example.com/v1/traces" \\
  --token "<你的接入 token>" \\
  --tenant "<租户 ID>" \\
  --service "my-local-agent"
```

或用环境变量(env 优先于配置文件):
`AGENT_TELEMETRY_ENDPOINT` / `AGENT_TELEMETRY_TOKEN` /
`AGENT_TELEMETRY_TENANT` / `AGENT_TELEMETRY_SERVICE`。

未配置 endpoint 时为**本地模式**:数据暂存在 `~/.agent-telemetry/spool/`,
执行 `python3 scripts/setup.py --status` 可查看状态。

## 3. 验证

```bash
python3 scripts/setup.py --status
PYTHONPATH=. python3 -m agent_telemetry_skill.cli demo
```

`demo` 会打印若干 JSON span,密钥应显示为 `[REDACTED]`,正文应显示为
`{"content_omitted": true, ...}`。

## 4. 关闭 / 卸载

```bash
export AGENT_TELEMETRY_ENABLED=0          # 临时全局静默
python3 scripts/setup.py --uninstall      # 卸载所有已安装的 adapter
```

## 隐私默认值

- 默认不上传完整 prompt / 模型回复 / 工具输出 / 文件内容。
- 密钥(api_key、token、password、sshpass/--password 等命令行内联凭证)自动脱敏。
- 完整内容采集需显式 `AGENT_TELEMETRY_CAPTURE_CONTENT=1`,建议仅排障时短时开启。

更详细的说明见 `用户侧使用文档.md`;接入协议见 `docs/PROTOCOL.md`;
架构见 `docs/ARCHITECTURE.md`。
"""


def read_version() -> str:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else "0.0.0"


def _is_excluded(path: Path) -> bool:
    rel = path.relative_to(REPO_ROOT).as_posix()
    if rel in EXCLUDE_RELPATHS:
        return True
    if path.suffix in EXCLUDE_SUFFIXES:
        return True
    return any(part in EXCLUDE_NAMES for part in path.parts)


def _copy_into(staging: Path) -> int:
    count = 0
    for name in INCLUDE_FILES:
        src = REPO_ROOT / name
        if src.is_file():
            shutil.copy2(src, staging / name)
            count += 1
    for name in INCLUDE_DIRS:
        src = REPO_ROOT / name
        if not src.is_dir():
            continue
        for item in src.rglob("*"):
            if item.is_dir() or _is_excluded(item):
                continue
            dst = staging / item.relative_to(REPO_ROOT)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dst)
            count += 1
    return count


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _make_targz(staging: Path, out: Path, arcroot: str) -> None:
    with tarfile.open(out, "w:gz") as tar:
        tar.add(staging, arcname=arcroot)


def _make_zip(staging: Path, out: Path, arcroot: str) -> None:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in sorted(staging.rglob("*")):
            if item.is_file():
                zf.write(item, Path(arcroot) / item.relative_to(staging))


def build(outdir: Path, *, make_targz: bool, make_zip: bool) -> None:
    version = read_version()
    arcroot = f"{PACKAGE_NAME}-{version}"
    staging = outdir / arcroot
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    file_count = _copy_into(staging)
    (staging / "INSTALL.md").write_text(INSTALL_MD, encoding="utf-8")
    file_count += 1

    print(f"package : {arcroot}")
    print(f"staged  : {file_count} files -> {staging}")

    artifacts: list[Path] = []
    if make_targz:
        targz = outdir / f"{arcroot}.tar.gz"
        _make_targz(staging, targz, arcroot)
        artifacts.append(targz)
    if make_zip:
        zip_path = outdir / f"{arcroot}.zip"
        _make_zip(staging, zip_path, arcroot)
        artifacts.append(zip_path)

    print("\nartifacts:")
    for art in artifacts:
        size_kb = art.stat().st_size / 1024
        print(f"  {art}  ({size_kb:.0f} KB)")
        print(f"    sha256: {_sha256(art)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Agent Telemetry skill package.")
    parser.add_argument("--outdir", default=str(REPO_ROOT / "dist"))
    parser.add_argument("--no-zip", action="store_true")
    parser.add_argument("--no-targz", action="store_true")
    args = parser.parse_args(argv)

    outdir = Path(args.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    build(outdir, make_targz=not args.no_targz, make_zip=not args.no_zip)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
