"""Load a lib/ script as a module bound to a temporary hub directory."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parent.parent


def load(name: str, hub: Path) -> ModuleType:
    os.environ["RUNOPS_HUB_DIR"] = str(hub)
    spec = importlib.util.spec_from_file_location(f"hub_{name}", REPO / "lib" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def member(hub: Path, name: str) -> Path:
    """A member project: a real directory beside the hub, linked into it."""
    project = hub.parent / f"project_{name}"
    project.mkdir()
    (project / "runops.toml").write_text("[project]\n", encoding="utf-8")
    (hub / name).symlink_to(f"../project_{name}")
    return project
