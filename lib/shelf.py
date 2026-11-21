#!/usr/bin/env python3
"""Set a member project or a paper aside, and bring it back.

    shelf.py list                                       what is set aside, with why, since when and where
    shelf.py defer TARGET [--why TEXT] [--until DATE]   back later: a trial, or a line of work put off
    shelf.py archive TARGET [--why TEXT]                done or stopped
    shelf.py restore TARGET                             in use again
    (./hub shelf | defer | archive | restore; the last three relink afterwards)

TARGET is ``NAME``, ``projects/NAME`` or ``papers/NAME`` (a bare NAME that is both a project and a
paper must say which); ``archive/projects/NAME`` and the like are understood too. Setting aside
moves the link, not the data: ``projects/NAME`` becomes ``deferred/projects/NAME`` or
``archive/projects/NAME`` (``papers/`` likewise), so ``projects/`` and ``papers/`` hold only what
is in use. The real directory stays where it is and stays in Claude Code's working directories.

- ``./hub list``, ``context``, ``sync``, ``doctor``, ``update``, ``status``, ``board``, ``sessions``
  and ``logs`` skip it; ``./hub status`` and ``list`` end with one line on what is set aside.
- its row moves from the table in the hub AGENTS.md to ``.hub/shelf.toml``; ``restore`` puts it back.
- its skills no longer count for the hub's skill links, and hub auto neither ticks nor submits there
  (``auto/config.toml`` is left as it is, so ``restore`` resumes auto too).
- paths through the link in the slides' cards and tables (``projects/NAME/...``) are rewritten to
  the real path first, so they keep working; new cards are written with real paths already.

A project with waiting or running jobs is refused without ``--force``: they would drop out of
``./hub status``. ``./hub detach`` of a project set aside also drops its notes.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from projects import (  # noqa: E402
    KINDS, SHELVES, is_due, load_shelf, put_row, save_shelf, shelf_lock, shelved, take_row,
)

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    sys.exit("shelf.py needs Python >= 3.11 (tomllib)")

# The hub directory. ./hub exports it; run by hand, it is the cwd.
HUB = Path(os.environ.get("RUNOPS_HUB_DIR") or Path.cwd()).resolve()
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
MEANING = {"deferred": "back later", "archived": "done or stopped"}
# Files under slides/ that may name a file of a member or a paper (card.toml, tables, notes).
SLIDE_SOURCES = ("card.toml", "*.csv", "*.md")
SLIDE_SKIP = {"out", "logs", "_tools"}


class ShelfError(Exception):
    pass


def places(kind: str, name: str) -> list[tuple[str, Path]]:
    """(state, link) for every place KIND/NAME may sit: in use first, then the older layout, then set aside."""
    found = [("active", HUB / kind / name)]
    if kind == "projects":
        found.append(("active", HUB / name))
    return found + [(state, HUB / top / kind / name) for state, top in SHELVES.items()]


def present(kind: str, state: str, link: Path) -> bool:
    if state != "active":
        return link.is_symlink() or link.is_dir()  # a broken link set aside can still be restored or listed
    return (link / "runops.toml").is_file() if kind == "projects" else link.is_dir()


def resolve(target: str) -> dict:
    """{kind, name, key, state, link} of TARGET. state is "active", "deferred" or "archived"."""
    parts = target.strip().rstrip("/").split("/")
    if len(parts) == 3 and parts[0] in SHELVES.values():
        parts = parts[1:]
    if len(parts) > 2 or (len(parts) == 2 and parts[0] not in KINDS) or not NAME_RE.fullmatch(parts[-1]):
        raise ShelfError(f"{target!r}: give NAME, projects/NAME or papers/NAME")
    name = parts[-1]
    found = []
    for kind in ([parts[0]] if len(parts) == 2 else KINDS):
        here = [(state, link) for state, link in places(kind, name) if present(kind, state, link)]
        if len({state for state, _ in here}) > 1:
            raise ShelfError(f"{kind}/{name} sits in more than one place ({', '.join(str(link.relative_to(HUB)) for _, link in here)}); "
                             "keep one by hand")
        if here:
            state, link = here[0]
            found.append({"kind": kind, "name": name, "key": f"{kind}/{name}", "state": state, "link": link})
    if not found:
        raise ShelfError(f"{target!r} is neither a member project nor a paper of {HUB}")
    if len(found) > 1:
        raise ShelfError(f"{name} is both projects/{name} and papers/{name}; say which")
    return found[0]


def move_link(src: Path, dst: Path) -> None:
    """Move the link SRC to DST, keeping a relative target relative (it is one level deeper or shallower)."""
    if dst.exists() or dst.is_symlink():
        raise ShelfError(f"{dst.relative_to(HUB)} already exists; move it away first")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_symlink():
        raw = os.readlink(src)
        target = raw if os.path.isabs(raw) else os.path.relpath(os.path.normpath(os.path.join(src.parent, raw)), dst.parent)
        dst.symlink_to(target)
        src.unlink()
    else:
        src.rename(dst)  # a member kept as a real directory inside the hub
    top = src.parent.parent
    if top.parent == HUB and top.name in SHELVES.values():  # drop archive/projects/ and archive/ once empty
        for empty in (src.parent, top):
            try:
                empty.rmdir()
            except OSError:
                break


def slide_sources(hub: Path) -> list[Path]:
    root = hub / "slides"
    if not root.is_dir():
        return []
    found = []
    for dirname, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SLIDE_SKIP and not d.startswith("."))
        here = Path(dirname)
        if here == root:
            continue  # slides/README.md describes the slides, it does not name sources
        found += [here / n for n in sorted(names) if any(Path(n).match(p) for p in SLIDE_SOURCES) and n != "INDEX.md"]
    return found


def absolutize(hub: Path, key: str, real: Path) -> list[Path]:
    """Rewrite KEY (``projects/NAME``) and HUB/KEY where they start a path in the slides' sources to REAL."""
    pattern = re.compile(rf"(?<![\w./-])(?:{re.escape(str(hub))}/)?{re.escape(key)}(?![\w.-])")
    changed = []
    for path in slide_sources(hub):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        new = pattern.sub(lambda _: str(real), text)
        if new != text:
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            tmp.write_text(new, encoding="utf-8")
            os.chmod(tmp, path.stat().st_mode & 0o7777)
            tmp.replace(path)
            changed.append(path)
    return changed


def active_runs(project: Path) -> tuple[list[str], str]:
    """Ids of the runs whose jobs wait or run, and a note when they could not be listed."""
    cmd = ["runo", "list", "--status", "submitted", "--status", "running", "--json"]  # no --brief: runops < 2.7
    try:
        res = subprocess.run(cmd, cwd=project, capture_output=True, text=True, timeout=120)
        data = json.loads(res.stdout) if res.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return [], f"could not list its active runs ({exc})"
    if not isinstance(data, list):
        return [], f"could not list its active runs (runo list exit {res.returncode})"
    return [str(r.get("id", "?")) for r in data if isinstance(r, dict)], ""


def auto_enabled(name: str) -> bool:
    try:
        config = tomllib.loads((HUB / "auto" / "config.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    table = config.get("projects", {}).get(name)
    return isinstance(table, dict) and table.get("enabled", True) is not False


def by() -> str:
    return f"{os.environ.get('USER', '?')}@{socket.gethostname().split('.')[0]}"


def cmd_list(_args) -> int:
    items, notes = shelved(HUB), load_shelf(HUB)
    if not items:
        print("nothing is set aside (./hub defer NAME / ./hub archive NAME move a project or paper to deferred/ or archive/)")
    for state, top in SHELVES.items():
        here = [i for i in items if i["state"] == state]
        if not here:
            continue
        print(f"{top}/ ({state}: {MEANING[state]}):")
        width = max(len(i["key"]) for i in here)
        for item in here:
            e = notes.get(item["key"], {})
            until = f"  until {e['until']}" + (" (due)" if is_due(e) else "") if e.get("until") else ""
            print(f"  {item['key']:<{width}}  since {e.get('since', '?')}{until}" + (f"  — {e['why']}" if e.get("why") else ""))
            real = item["link"].resolve()
            print(f"  {'':<{width}}  {real if real.exists() else f'!! {real} is gone'}")
    orphans = sorted(set(notes) - {i["key"] for i in items})
    if orphans:
        print(f"notes without a link set aside (in use again, or removed by hand): {', '.join(orphans)}")
    if items:
        print("./hub restore NAME puts one back under projects/ or papers/")
    return 0


def cmd_set(state: str, args) -> int:
    item = resolve(args.target)
    kind, key, name = item["kind"], item["key"], item["name"]
    if args.until:
        try:
            date.fromisoformat(args.until)
        except ValueError:
            raise ShelfError(f"--until {args.until!r}: give a date as YYYY-MM-DD")
    warnings: list[str] = []
    with shelf_lock(HUB):
        item = resolve(args.target)  # again, under the lock
        entries = load_shelf(HUB)
        old = entries.get(key, {}) if item["state"] != "active" else {}
        real = item["link"].resolve()
        if kind == "projects" and item["state"] == "active":
            ids, problem = active_runs(item["link"])
            if problem:
                warnings.append(f"{key}: {problem}; set aside without checking")
            elif ids and not args.force:
                shown = ", ".join(ids[:5]) + (f", +{len(ids) - 5}" if len(ids) > 5 else "")
                raise ShelfError(f"{key} has {len(ids)} waiting or running runs ({shown}); they would drop out of "
                                 "./hub status and sync. Let them finish, cancel them in the project, or pass --force")
            elif ids:
                warnings.append(f"{key}: {len(ids)} waiting or running runs; ./hub sync and status skip them until restored")
        dst = HUB / SHELVES[state] / kind / name
        rewritten = absolutize(HUB, key, real) if item["state"] == "active" else []
        if item["link"] != dst:
            move_link(item["link"], dst)
        row = old.get("row", "")
        if item["state"] == "active":
            row, note = take_row(HUB, key)
            if note:
                warnings.append(note)
        entries[key] = {"since": old.get("since") if item["state"] == state else date.today().isoformat(),
                        "until": (args.until or old.get("until", "")) if state == "deferred" else "",
                        "why": args.why or old.get("why", ""), "by": by(), "row": row}
        save_shelf(HUB, entries)
    entry = entries[key]
    verb = "kept" if item["state"] == state else ("moved" if item["state"] != "active" else "set aside")
    print(f"{verb} {key} as {state} ({MEANING[state]}) since {entry['since']}"
          + (f" until {entry['until']}" if entry["until"] else "") + (f": {entry['why']}" if entry["why"] else ""))
    if item["link"] != dst:
        print(f"  link: {item['link'].relative_to(HUB)} -> {dst.relative_to(HUB)} (the data stays at {real})")
    if item["state"] == "active":
        skipped = ("./hub list, sync, status, board, sessions, logs, the skill links and hub auto skip it"
                   if kind == "projects" else "./hub logs skips it")
        print(f"  {skipped}")
        if row:
            print(f"  its row left the table in AGENTS.md (kept in .hub/shelf.toml for ./hub restore {key})")
        if rewritten:
            print(f"  wrote the real path for {key}/ in {len(rewritten)} slide file(s): "
                  + ", ".join(str(p.relative_to(HUB)) for p in rewritten[:4]) + (", ..." if len(rewritten) > 4 else ""))
    if kind == "projects" and auto_enabled(name):
        print(f"  hub auto: {name} stays in auto/config.toml; ticks and hub auto submit skip it while it is set aside")
    for w in warnings:
        print(f"!! {w}")
    return 0


def cmd_restore(args) -> int:
    with shelf_lock(HUB):
        item = resolve(args.target)
        key = item["key"]
        if item["state"] == "active":
            raise ShelfError(f"{key} is in use, not set aside (./hub shelf lists what is)")
        dst = HUB / item["kind"] / item["name"]
        move_link(item["link"], dst)
        entries = load_shelf(HUB)
        entry = entries.pop(key, {})
        save_shelf(HUB, entries)
    note = put_row(HUB, key, entry["row"]) if entry.get("row") else ""
    print(f"restored {key} (it was {item['state']} since {entry.get('since', '?')}); "
          f"link: {item['link'].relative_to(HUB)} -> {key}")
    if note:
        print(f"!! {note}")
    if item["kind"] == "projects" and not entry.get("row"):
        print(f"!! no table row was kept for {key}; add one to AGENTS.md by hand")
    if item["kind"] == "projects":
        print(f"  ./hub update skipped it while it was set aside; if skills are reported behind below, "
              f"run `runo update` in {key}")
        if auto_enabled(item["name"]):
            print(f"  hub auto: enabled in auto/config.toml, so the next tick looks at {item['name']} again")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="./hub", description="Set a member project or a paper aside, and bring it back.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="what is set aside")
    for verb, helptext in (("defer", "back later (deferred/)"), ("archive", "done or stopped (archive/)")):
        p = sub.add_parser(verb, help=helptext)
        p.add_argument("target", help="NAME, projects/NAME or papers/NAME")
        p.add_argument("--why", default="", help="one line on why, shown by ./hub shelf")
        if verb == "defer":
            p.add_argument("--until", default="", help="YYYY-MM-DD to look at it again (./hub status marks it due)")
        p.add_argument("--force", action="store_true", help="set aside even with waiting or running jobs")
    p = sub.add_parser("restore", help="in use again")
    p.add_argument("target", help="NAME, projects/NAME or papers/NAME")
    args = ap.parse_args(argv)
    if args.cmd == "archive":
        args.until = ""
    try:
        if args.cmd == "list":
            return cmd_list(args)
        if args.cmd == "restore":
            return cmd_restore(args)
        return cmd_set({"defer": "deferred", "archive": "archived"}[args.cmd], args)
    except ShelfError as exc:
        print(f"!! {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
