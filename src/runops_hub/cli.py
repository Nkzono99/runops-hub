"""Install the existing hub helper and its assets into the user's workspace."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__


def runtime_source() -> Path:
    packaged = Path(__file__).resolve().parent / "_runtime"
    return packaged if packaged.is_dir() else Path(__file__).resolve().parents[2]


def initialize(directory: Path) -> None:
    """Copy runtime assets; never link a workspace to uv's disposable tool cache."""
    root = directory.expanduser().resolve()
    source = runtime_source()
    if root == source:
        raise ValueError("choose a workspace directory outside the runops-hub source directory")
    entry = root / "hub"
    state = root / ".hub"
    runtime = state / "runtime"
    if entry.exists() or entry.is_symlink():
        if not entry.is_symlink() or entry.resolve() not in (runtime / "hub", source / "hub"):
            raise ValueError(f"{entry} already exists and is not managed by this initializer; left alone")

    state.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".init-", dir=state) as staging_dir:
        staging = Path(staging_dir)
        replacement = staging / "runtime"
        replacement.mkdir()
        shutil.copy2(source / "hub", replacement / "hub")
        (replacement / "hub").chmod(0o755)
        shutil.copy2(source / "LICENSE", replacement / "LICENSE")
        for name in ("lib", "templates"):
            shutil.copytree(source / name, replacement / name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        previous = staging / "previous"
        if runtime.exists():
            runtime.rename(previous)
        try:
            replacement.rename(runtime)
        except OSError:
            if previous.exists():
                previous.rename(runtime)
            raise

        # init preserves AGENTS.md, CLAUDE.md, member instructions, and all project state.
        env = {k: v for k, v in os.environ.items() if k != "RUNOPS_HUB_DIR"}
        subprocess.run(["bash", str(runtime / "hub"), "init", str(root)], env=env, check=True)
        launcher = staging / "hub-link"
        launcher.symlink_to(".hub/runtime/hub")
        launcher.replace(entry)

        # Resolve the venv's Python symlink to the base interpreter, outside the uvx cache.
        interpreter = staging / "python"
        interpreter.write_text(str(Path(sys.executable).resolve()) + "\n", encoding="utf-8")
        interpreter.replace(state / "python")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="runops-hub",
        description="Create a hub directory, then open an agent there to manage your projects.",
    )
    parser.add_argument("--version", action="version", version=f"runops-hub {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="create or refresh a hub directory; preserve its instructions and state")
    init.add_argument("directory", type=Path, nargs="?", default=Path("."), help="workspace directory (default: .)")
    args = parser.parse_args(argv)
    try:
        initialize(args.directory)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"runops-hub: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
