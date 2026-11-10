#!/usr/bin/env python3
"""Show which hub members have a direct session on this login node.

Looks at (a) tmux panes whose cwd is inside a member and (b) claude/codex
processes of this user whose cwd is inside a member. Only this node is visible:
tmux servers and processes on other login nodes are not.
"""
from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

# The hub directory (members, AGENTS.md, .hub/ state). ./hub exports it; run by hand, it is the cwd.
HUB = Path(os.environ.get("RUNOPS_HUB_DIR") or Path.cwd()).resolve()
AGENTS = {"claude", "codex"}


def members() -> dict[str, Path]:
    out = {}
    for p in sorted(HUB.iterdir()):
        if not p.name.startswith(".") and p.is_dir() and (p / "runops.toml").is_file():
            out[p.name] = p.resolve()
    return out


def owner(path: str, mems: dict[str, Path]) -> str | None:
    for name, root in mems.items():
        if path == str(root) or path.startswith(str(root) + os.sep):
            return name
    return None


def tmux_panes() -> list[tuple[str, str, str]]:
    try:
        res = subprocess.run(
            ["tmux", "list-panes", "-a", "-F",
             "#{session_name}\t#{pane_current_path}\t#{pane_current_command}"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if res.returncode != 0:
        return []
    return [tuple(line.split("\t", 2)) for line in res.stdout.splitlines() if line.count("\t") == 2]


def agent_procs() -> list[tuple[int, str, str]]:
    uid = os.getuid()
    out = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            if d.stat().st_uid != uid:
                continue
            comm = (d / "comm").read_text().strip()
            if comm not in AGENTS:
                continue
            out.append((int(d.name), comm, os.readlink(d / "cwd")))
        except OSError:
            continue
    return out


def main() -> int:
    mems = members()
    found: dict[str, list[str]] = {name: [] for name in mems}
    for session, path, cmd in tmux_panes():
        name = owner(path, mems)
        if name:
            found[name].append(f"tmux:{session} ({cmd})")
    for pid, comm, cwd in agent_procs():
        name = owner(cwd, mems)
        if name:
            found[name].append(f"{comm} pid {pid}")
    print(f"direct sessions on {socket.gethostname()} (other login nodes not visible):")
    for name, items in found.items():
        print(f"  {name:<24} {', '.join(sorted(set(items))) if items else '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
