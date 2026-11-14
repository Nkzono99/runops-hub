"""Exercise built distributions through uvx, outside the source checkout and its cache."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def check(artifact: Path, uv: str, base: Path) -> dict:
    workspace = base / "hub with spaces"
    cache = base / "uv-cache"
    env = {k: v for k, v in os.environ.items() if k not in ("RUNOPS_HUB_DIR", "PYTHONPATH")}
    env["PATH"] = "/usr/bin:/bin"  # Neither runo nor the developer's Python may mask bootstrap problems.

    def run(*command: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(command, cwd=base, env=env, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(f"{command!r}\n{result.stdout}\n{result.stderr}")
        return result

    init = [uv, "tool", "run", "--cache-dir", str(cache), "--python", sys.executable,
            "--from", str(artifact), "runops-hub", "init", str(workspace)]
    run(*init)
    assert (workspace / ".hub/runtime/templates/AGENTS.md").is_file()
    assert (workspace / ".hub/runtime/lib/board.py").is_file()
    assert os.readlink(workspace / "hub") == ".hub/runtime/hub"
    shutil.rmtree(cache)
    run("bash", str(workspace / "hub"), "board")
    assert "分かっていること" in (workspace / "board.html").read_text()

    project = base / "existing project"
    project.mkdir()
    (project / "runops.toml").write_text("[project]\n")
    (project / "AGENTS.md").write_text("# Existing project\n")
    run("bash", str(workspace / "hub"), "attach", "example", str(project), "generic", "--desc", "existing project")
    run("bash", str(workspace / "hub"), "relink", "--check")
    custom = (workspace / "AGENTS.md").read_text() + "\nMy custom rule\n"
    (workspace / "AGENTS.md").write_text(custom)
    run(*init)
    assert (workspace / "AGENTS.md").read_text() == custom
    assert (workspace / "example").resolve() == project
    run("bash", str(workspace / "hub"), "detach", "example")
    assert project.is_dir() and not (workspace / "example").is_symlink()
    return {"artifact": artifact.name, "cache_removal": "passed", "attach_detach": "passed", "refresh": "passed"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist", type=Path)
    args = parser.parse_args()
    uv = shutil.which("uv")
    if uv is None:
        parser.error("uv is required")
    artifacts = sorted(args.dist.resolve().glob("runops_hub-*.whl")) + sorted(args.dist.resolve().glob("runops_hub-*.tar.gz"))
    if len(artifacts) != 2:
        parser.error("use a directory containing exactly one wheel and one sdist")
    results = []
    for artifact in artifacts:
        with tempfile.TemporaryDirectory(prefix="runops-hub-dist-") as tmp:
            results.append(check(artifact, uv, Path(tmp).resolve()))
    print(json.dumps({"checks": results, "result": "passed"}, indent=2))


if __name__ == "__main__":
    main()
