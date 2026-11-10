#!/usr/bin/env python3
"""Keep the project table in the hub AGENTS.md in step with the members.

    projects.py add NAME SIMULATOR DESCRIPTION   append a row (no-op if NAME is listed)
    projects.py remove NAME                      drop the row of NAME
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# AGENTS.md of the hub directory. ./hub exports the directory; run by hand, it is the cwd.
AGENTS = Path(os.environ.get("RUNOPS_HUB_DIR") or Path.cwd()).resolve() / "AGENTS.md"
HEADER = "| dir | simulator | 内容 |"


def rows(lines: list[str]) -> tuple[int, int]:
    """Index of the header line and one past the last table row."""
    start = lines.index(HEADER)
    end = start + 2  # header + separator
    while end < len(lines) and lines[end].startswith("| `"):
        end += 1
    return start, end


def main(argv: list[str]) -> int:
    lines = AGENTS.read_text(encoding="utf-8").splitlines()
    try:
        start, end = rows(lines)
    except ValueError:
        print(f"!! project table header not found in {AGENTS}; edit the table by hand")
        return 1
    if len(argv) == 4 and argv[0] == "add":
        name, sim, desc = argv[1:]
        if any(line.startswith(f"| `{name}/` ") for line in lines[start:end]):
            return 0
        lines.insert(end, f"| `{name}/` | {sim} | {desc} |")
    elif len(argv) == 2 and argv[0] == "remove":
        name = argv[1]
        lines = [
            line
            for i, line in enumerate(lines)
            if not (start < i < end and line.startswith(f"| `{name}/` "))
        ]
    else:
        print(__doc__)
        return 2
    AGENTS.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
