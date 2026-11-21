#!/usr/bin/env python3
"""Set a member project or a paper aside, and bring it back.

    shelf.py list                                       what is set aside, with why, since when and where
    shelf.py defer TARGET [--why TEXT] [--until DATE]   back later: a trial, or a line of work put off
    shelf.py archive TARGET [--why TEXT]                done or stopped
    shelf.py restore TARGET                             in use again
    (./hub shelf | defer | archive | restore; the last three relink afterwards)

TARGET is ``NAME``, ``projects/NAME`` or ``papers/NAME`` (a bare NAME that is both a project and a
paper must say which). Setting aside keeps the link and the data where they are, so paths written in
LOG.md, slides and other notes keep working and the files stay readable and writable from the hub
(the real directory stays in Claude Code's working directories). What changes:

- ``./hub list``, ``context``, ``sync``, ``doctor``, ``update``, ``status``, ``board``, ``sessions``
  and ``logs`` skip it; ``./hub status`` and ``list`` end with one line on the shelf.
- its row moves from the table in the hub AGENTS.md to the shelf (``.hub/shelf.toml``), and
  ``restore`` puts it back, so the table an agent reads holds only what is in use.
- its skills no longer count for the hub's skill links, and hub auto neither ticks nor submits there
  (``auto/config.toml`` is left as it is, so ``restore`` resumes auto too).

A project with waiting or running jobs is refused without ``--force``: they would drop out of
``./hub status``. ``./hub detach`` of a shelved project also drops its shelf entry.
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
    STATES, is_due, load_shelf, put_row, save_shelf, shelf_lock, take_row,
)

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    sys.exit("shelf.py needs Python >= 3.11 (tomllib)")

# The hub directory. ./hub exports it; run by hand, it is the cwd.
HUB = Path(os.environ.get("RUNOPS_HUB_DIR") or Path.cwd()).resolve()
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
MEANING = {"deferred": "back later", "archived": "done or stopped"}


class ShelfError(Exception):
    pass


def resolve(target: str) -> tuple[str, str, Path]:
    """(kind, key, link) of TARGET: kind is "project" or "paper", key the link's path from the hub."""
    text = target.strip().rstrip("/")
    kind_given, _, name = text.rpartition("/")
    if kind_given not in ("", "projects", "papers") or not NAME_RE.fullmatch(name):
        raise ShelfError(f"{target!r}: give NAME, projects/NAME or papers/NAME")
    found: list[tuple[str, str, Path]] = []
    if kind_given in ("", "projects"):
        for key in (f"projects/{name}", name):  # the second is an older hub's layout
            if (HUB / key / "runops.toml").is_file():
                found.append(("project", key, HUB / key))
                break
    if kind_given in ("", "papers") and (HUB / "papers" / name).is_dir():
        found.append(("paper", f"papers/{name}", HUB / "papers" / name))
    if not found:
        raise ShelfError(f"{target!r} is neither a member project nor a paper of {HUB}")
    if len(found) > 1:
        raise ShelfError(f"{name} is both {found[0][1]} and {found[1][1]}; say which")
    return found[0]


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
    entries = load_shelf(HUB)
    if not entries:
        print("the shelf is empty (./hub defer NAME / ./hub archive NAME set a project or paper aside)")
        return 0
    for state in STATES:
        keys = sorted(k for k, v in entries.items() if v["state"] == state)
        if not keys:
            continue
        print(f"{state} ({MEANING[state]}):")
        width = max(len(k) for k in keys)
        for key in keys:
            e = entries[key]
            until = f"  until {e['until']}" + (" (due)" if is_due(e) else "") if e.get("until") else ""
            print(f"  {key:<{width}}  since {e.get('since', '?')}{until}" + (f"  — {e['why']}" if e.get("why") else ""))
            link = HUB / key
            print(f"  {'':<{width}}  {link.resolve() if link.exists() else '!! the link is gone (./hub restore drops the entry)'}")
    print("./hub restore NAME brings one back; its link and data stay where they are meanwhile")
    return 0


def cmd_set(state: str, args) -> int:
    kind, key, link = resolve(args.target)
    name = link.name
    if args.until:
        try:
            date.fromisoformat(args.until)
        except ValueError:
            raise ShelfError(f"--until {args.until!r}: give a date as YYYY-MM-DD")
    notes: list[str] = []
    with shelf_lock(HUB):
        entries = load_shelf(HUB)
        old = entries.get(key, {})
        if kind == "project" and not old:
            ids, problem = active_runs(link)
            if problem:
                notes.append(f"{key}: {problem}; set aside without checking")
            elif ids and not args.force:
                shown = ", ".join(ids[:5]) + (f", +{len(ids) - 5}" if len(ids) > 5 else "")
                raise ShelfError(f"{key} has {len(ids)} waiting or running runs ({shown}); they would drop out of "
                                 "./hub status and sync. Let them finish, cancel them in the project, or pass --force")
            elif ids:
                notes.append(f"{key}: {len(ids)} waiting or running runs; ./hub sync and status skip them until restored")
        row, note = (old.get("row", ""), "") if old else take_row(HUB, key)
        if note:
            notes.append(note)
        entry = {"state": state, "since": date.today().isoformat() if old.get("state") != state else old.get("since"),
                 "until": (args.until or old.get("until", "")) if state == "deferred" else "",
                 "why": args.why or old.get("why", ""), "by": by(), "row": row}
        entries[key] = entry
        try:
            save_shelf(HUB, entries)
        except Exception:
            if row and not old:
                put_row(HUB, key, row)
            raise
    verb = "kept" if old.get("state") == state else ("moved" if old else "set aside")
    print(f"{verb} {key} as {state} ({MEANING[state]}) since {entry['since']}"
          + (f" until {entry['until']}" if entry["until"] else "") + (f": {entry['why']}" if entry["why"] else ""))
    if not old:
        skipped = ("./hub list, sync, status, board, sessions, logs, the skill links and hub auto skip it"
                   if kind == "project" else "./hub logs skips it")
        print(f"  {skipped}; its link {key} and the data at {link.resolve()} stay")
        if row:
            print(f"  its row left the table in AGENTS.md (kept in .hub/shelf.toml for ./hub restore {key})")
    if kind == "project" and auto_enabled(name):
        print(f"  hub auto: {name} stays in auto/config.toml; ticks and hub auto submit skip it while it is set aside")
    for n in notes:
        print(f"!! {n}")
    return 0


def gone_key(target: str, entries: dict[str, dict]) -> str:
    """The shelf entry TARGET names when its link is gone, or ""."""
    text = target.strip().rstrip("/")
    keys = [k for k in (text, f"projects/{text}", f"papers/{text}") if k in entries and not (HUB / k).exists()]
    return keys[0] if len(keys) == 1 else ""


def cmd_restore(args) -> int:
    try:
        kind, key, link = resolve(args.target)
    except ShelfError:
        with shelf_lock(HUB):
            entries = load_shelf(HUB)
            key = gone_key(args.target, entries)
            if not key:
                raise
            entry = entries.pop(key)
            save_shelf(HUB, entries)
        print(f"dropped the shelf entry of {key}: its link is gone, so nothing was restored")
        if entry.get("row"):
            print(f"  its table row was:\n  {entry['row']}")
        return 0
    with shelf_lock(HUB):
        entries = load_shelf(HUB)
        entry = entries.pop(key, None)
        if entry is None:
            raise ShelfError(f"{key} is not set aside (./hub shelf lists what is)")
        save_shelf(HUB, entries)
    note = put_row(HUB, key, entry["row"]) if entry.get("row") else ""
    print(f"restored {key} (it was {entry['state']} since {entry.get('since', '?')})")
    if note:
        print(f"!! {note}")
    if kind == "project":
        print(f"  ./hub update skipped it while it was set aside; if skills are reported behind below, "
              f"run `runo update` in {key}")
        if auto_enabled(link.name):
            print(f"  hub auto: enabled in auto/config.toml, so the next tick looks at {link.name} again")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="./hub", description="Set a member project or a paper aside, and bring it back.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="what is set aside")
    for state, helptext in (("defer", "back later"), ("archive", "done or stopped")):
        p = sub.add_parser(state, help=helptext)
        p.add_argument("target", help="NAME, projects/NAME or papers/NAME")
        p.add_argument("--why", default="", help="one line on why, shown by ./hub shelf")
        if state == "defer":
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
