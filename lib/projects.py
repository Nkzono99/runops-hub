#!/usr/bin/env python3
"""Who the members of the hub are, and the project table in the hub AGENTS.md.

    projects.py list                             member projects in use (not set aside), one path a line
    projects.py add NAME SIMULATOR DESCRIPTION   append a row (no-op if NAME is listed)
    projects.py remove NAME                      drop the row of NAME (and its notes on the shelf)
    projects.py shelf-line                       one line on what is set aside, or nothing

Where a link sits says what it is (lib/shelf.py moves it: ./hub defer / archive / restore):

    projects/NAME, papers/NAME                      in use
    deferred/projects/NAME, deferred/papers/NAME    set aside, back later
    archive/projects/NAME, archive/papers/NAME      set aside, done or stopped

``member_paths`` and ``paper_paths`` return those in use unless asked for all, so every view and
loop of the hub skips what is set aside. ``.hub/shelf.toml`` keeps notes on each one set aside
(since, until, why, its row of the table in AGENTS.md), keyed by where it sits in use
(``"projects/NAME"``, ``"papers/NAME"``); the location, not the notes, decides the state.
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
SHELVES = {"deferred": "deferred", "archived": "archive"}  # state -> the directory under the hub that holds it
KINDS = ("projects", "papers")


# ---------------------------------------------------------------- the shelf

def shelf_file(hub: Path) -> Path:
    return hub / ".hub" / "shelf.toml"


def load_shelf(hub: Path) -> dict[str, dict]:
    """{"projects/NAME" or "papers/NAME": notes} for what is set aside. Unreadable notes read as none."""
    path = shelf_file(hub)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(f"!! {path} is not readable ({exc}); the notes on what is set aside are ignored", file=sys.stderr)
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def save_shelf(hub: Path, entries: dict[str, dict]) -> None:
    out = ["# Notes on the members and papers set aside (archive/, deferred/). ./hub archive / defer write them,",
           "# ./hub restore removes them. why and until may be edited by hand; where the link sits decides the state.",
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
    """Hold .hub/shelf.lock while moving links and rewriting the notes (flock works across login nodes on /LARGE*)."""
    (hub / ".hub").mkdir(parents=True, exist_ok=True)
    with open(hub / ".hub" / "shelf.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def shelved(hub: Path) -> list[dict]:
    """What is set aside: {state, kind ("projects" | "papers"), name, key, link}, by state, kind and name.

    A link whose target is gone is listed too, so that it can be seen and restored or removed.
    """
    found = []
    for state, top in SHELVES.items():
        for kind in KINDS:
            parent = hub / top / kind
            if not parent.is_dir():
                continue
            for link in sorted(parent.iterdir()):
                if not link.name.startswith(".") and (link.is_symlink() or link.is_dir()):
                    found.append({"state": state, "kind": kind, "name": link.name, "key": f"{kind}/{link.name}",
                                  "link": link})
    return found


def shelved_projects(hub: Path) -> set[str]:
    """Names of the member projects set aside."""
    return {item["name"] for item in shelved(hub) if item["kind"] == "projects"}


def is_due(entry: dict) -> bool:
    try:
        return bool(entry.get("until")) and date.fromisoformat(entry["until"]) <= date.today()
    except ValueError:
        return False


def shelf_line(hub: Path) -> str:
    """``shelf: deferred 2 (a until 12-01, papers/b), archived 3 — ./hub shelf``, or "" when nothing is set aside."""
    items, notes = shelved(hub), load_shelf(hub)
    parts = []
    for state in SHELVES:
        keys = [i["key"] for i in items if i["state"] == state]
        if not keys:
            continue
        if state == "deferred":
            shown = []
            for key in keys:
                label, until = key.removeprefix("projects/"), notes.get(key, {}).get("until", "")
                if until:
                    label += f" until {until[5:]}" + (", due" if is_due(notes[key]) else "")
                shown.append(label)
            parts.append(f"deferred {len(keys)} ({', '.join(shown)})")
        else:
            parts.append(f"archived {len(keys)}")
    return f"shelf: {', '.join(parts)} — ./hub shelf" if parts else ""


# ---------------------------------------------------------------- members and papers

def member_paths(hub: Path, shelved: bool = False) -> list[Path]:
    """Member projects (links with a runops.toml) under projects/ and, from older hubs, the hub itself.

    With *shelved*, those under deferred/projects/ and archive/projects/ too. When a name sits in
    several places, projects/ wins, then the older layout.
    """
    parents = [hub / top / "projects" for top in SHELVES.values()] if shelved else []
    members = {}
    for parent in (*parents, hub, hub / "projects"):
        if parent.is_dir():
            for path in parent.iterdir():
                if not path.name.startswith(".") and (path / "runops.toml").is_file():
                    members[path.name] = path
    return [members[name] for name in sorted(members)]


def paper_paths(hub: Path, shelved: bool = False) -> list[Path]:
    """The directories under papers/ (links to paperops projects); with *shelved*, those set aside too."""
    parents = [hub / top / "papers" for top in SHELVES.values()] if shelved else []
    papers = {}
    for parent in (*parents, hub / "papers"):
        if parent.is_dir():
            for path in parent.iterdir():
                if path.is_dir() and not path.name.startswith("."):
                    papers[path.name] = path
    return [papers[name] for name in sorted(papers)]


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
