#!/usr/bin/env python3
"""Who the members of the hub are, and the project table in the hub AGENTS.md.

    projects.py list                             member projects in use (not set aside), one path a line
    projects.py add NAME SIMULATOR DESCRIPTION   append a row (no-op if NAME is listed)
    projects.py remove NAME                      drop the row of NAME (and its entry on the shelf)
    projects.py shelf-line                       one line on what is set aside, or nothing

A member project or a paper can be set aside on the shelf (``./hub archive`` / ``defer``,
lib/shelf.py). Its link and data stay where they are, so paths written elsewhere keep working;
``member_paths`` and ``paper_paths`` leave it out unless asked, so every view and loop of the
hub skips it. The shelf is ``.hub/shelf.toml``: one table per link, keyed by its path from the
hub (``"projects/NAME"``, ``"papers/NAME"``), with ``state`` = ``deferred`` (back later) or
``archived`` (done or stopped).
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    sys.exit("projects.py needs Python >= 3.11 (tomllib)")

# AGENTS.md of the hub directory. ./hub exports the directory; run by hand, it is the cwd.
AGENTS = Path(os.environ.get("RUNOPS_HUB_DIR") or Path.cwd()).resolve() / "AGENTS.md"
HEADER = "| dir | simulator | 内容 |"
PAPER_HEADER = "| dir | source | 内容 |"
STATES = ("deferred", "archived")


# ---------------------------------------------------------------- the shelf

def shelf_file(hub: Path) -> Path:
    return hub / ".hub" / "shelf.toml"


def load_shelf(hub: Path) -> dict[str, dict]:
    """{path from the hub: entry} of what is set aside. An unreadable shelf hides nothing."""
    path = shelf_file(hub)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(f"!! {path} is not readable ({exc}); nothing is treated as set aside", file=sys.stderr)
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict) and v.get("state") in STATES}


def save_shelf(hub: Path, entries: dict[str, dict]) -> None:
    out = ["# Members and papers set aside. ./hub archive / defer write an entry, ./hub restore removes it.",
           "# state: deferred (back later) or archived (done or stopped). why and until may be edited by hand.",
           ""]
    for key in sorted(entries):
        out.append(f"[{json.dumps(key)}]")
        out += [f"{k} = {json.dumps(str(v), ensure_ascii=False)}" for k, v in entries[key].items() if v not in ("", None)]
        out.append("")
    text = "\n".join(out)
    tomllib.loads(text)  # fail loudly rather than leave a broken file
    path = shelf_file(hub)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


@contextmanager
def shelf_lock(hub: Path):
    """Hold .hub/shelf.lock while reading and rewriting the shelf (flock works across login nodes on /LARGE*)."""
    (hub / ".hub").mkdir(parents=True, exist_ok=True)
    with open(hub / ".hub" / "shelf.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def shelved_projects(hub: Path) -> set[str]:
    """Names of the member projects set aside."""
    return {key.split("/")[-1] for key in load_shelf(hub) if not key.startswith("papers/")}


def is_due(entry: dict) -> bool:
    try:
        return bool(entry.get("until")) and date.fromisoformat(entry["until"]) <= date.today()
    except ValueError:
        return False


def shelf_line(hub: Path) -> str:
    """``shelf: deferred 2 (a until 12-01, papers/b), archived 3 — ./hub shelf``, or "" when the shelf is empty."""
    entries = load_shelf(hub)
    if not entries:
        return ""
    parts = []
    for state in STATES:
        keys = sorted(k for k, v in entries.items() if v["state"] == state)
        if not keys:
            continue
        if state == "deferred":
            shown = []
            for key in keys:
                label = key.removeprefix("projects/")
                until = entries[key].get("until", "")
                if until:
                    label += f" until {until[5:]}" + (", due" if is_due(entries[key]) else "")
                shown.append(label)
            parts.append(f"deferred {len(keys)} ({', '.join(shown)})")
        else:
            parts.append(f"archived {len(keys)}")
    return f"shelf: {', '.join(parts)} — ./hub shelf"


# ---------------------------------------------------------------- members and papers

def member_paths(hub: Path, shelved: bool = False) -> list[Path]:
    """Member projects (links with a runops.toml) under projects/ and, from older hubs, the hub itself.

    The nested entry wins when both exist. Projects on the shelf are left out unless *shelved*.
    """
    hidden = set() if shelved else shelved_projects(hub)
    members = {}
    for parent in (hub, hub / "projects"):
        if parent.is_dir():
            for path in parent.iterdir():
                if not path.name.startswith(".") and path.name not in hidden and (path / "runops.toml").is_file():
                    members[path.name] = path
    return [members[name] for name in sorted(members)]


def paper_paths(hub: Path, shelved: bool = False) -> list[Path]:
    """The directories under papers/ (links to paperops projects). Papers on the shelf are left out unless *shelved*."""
    papers = hub / "papers"
    if not papers.is_dir():
        return []
    hidden = set() if shelved else set(load_shelf(hub))
    return sorted(p for p in papers.iterdir()
                  if p.is_dir() and not p.name.startswith(".") and f"papers/{p.name}" not in hidden)


# ---------------------------------------------------------------- the tables in AGENTS.md

def rows(lines: list[str], header: str = HEADER) -> tuple[int, int]:
    """Index of the header line and one past the last table row."""
    start = lines.index(header)
    end = start + 2  # header + separator
    while end < len(lines) and lines[end].startswith("| `"):
        end += 1
    return start, end


def row_prefixes(key: str) -> tuple[str, ...]:
    """How a table row of the member or paper at KEY (path from the hub) starts."""
    name = key.split("/")[-1]
    if key.startswith("papers/"):
        return (f"| `papers/{name}/` ",)
    return (f"| `projects/{name}/` ", f"| `{name}/` ")


def take_row(hub: Path, key: str) -> tuple[str, str]:
    """Remove the row of KEY from its table in the hub AGENTS.md. Returns (the row or "", a note when there was none)."""
    agents, header = hub / "AGENTS.md", PAPER_HEADER if key.startswith("papers/") else HEADER
    try:
        lines = agents.read_text(encoding="utf-8").splitlines()
        start, end = rows(lines, header)
    except (OSError, ValueError):
        return "", f"no table headed {header!r} in {agents}; no row was moved"
    found = [i for i in range(start + 2, end) if lines[i].startswith(row_prefixes(key))]
    if not found:
        return "", f"no row for {key} in the table of {agents}"
    row = lines.pop(found[0])
    agents.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return row, ""


def put_row(hub: Path, key: str, row: str) -> str:
    """Append ROW to its table in the hub AGENTS.md unless KEY already has one. Returns a note when it could not."""
    agents, header = hub / "AGENTS.md", PAPER_HEADER if key.startswith("papers/") else HEADER
    try:
        lines = agents.read_text(encoding="utf-8").splitlines()
        start, end = rows(lines, header)
    except (OSError, ValueError):
        return f"no table headed {header!r} in {agents}; add this row by hand:\n  {row}"
    if any(line.startswith(row_prefixes(key)) for line in lines[start:end]):
        return ""
    lines.insert(end, row)
    agents.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return ""


def main(argv: list[str]) -> int:
    hub = AGENTS.parent
    if argv == ["list"]:
        for member in member_paths(hub):
            print(member)
        return 0
    if argv == ["shelf-line"]:
        line = shelf_line(hub)
        if line:
            print(line)
        return 0
    lines = AGENTS.read_text(encoding="utf-8").splitlines()
    try:
        start, end = rows(lines)
    except ValueError:
        print(f"!! project table header not found in {AGENTS}; edit the table by hand")
        return 1
    if len(argv) == 4 and argv[0] == "add":
        name, sim, desc = argv[1:]
        if any(line.startswith((f"| `projects/{name}/` ", f"| `{name}/` ")) for line in lines[start:end]):
            return 0
        lines.insert(end, f"| `projects/{name}/` | {sim} | {desc} |")
    elif len(argv) == 2 and argv[0] == "remove":
        name = argv[1]
        lines = [
            line
            for i, line in enumerate(lines)
            if not (start < i < end and line.startswith((f"| `projects/{name}/` ", f"| `{name}/` ")))
        ]
        with shelf_lock(hub):
            entries = load_shelf(hub)
            gone = [k for k in (f"projects/{name}", name) if entries.pop(k, None) is not None]
            if gone:
                save_shelf(hub, entries)
    else:
        print(__doc__)
        return 2
    AGENTS.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
