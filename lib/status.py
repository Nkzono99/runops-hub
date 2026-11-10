#!/usr/bin/env python3
"""One compact view of every member project.

Shows run counts, all active runs with progress, the runs whose status changed since the
last look, and completed runs with no recorded conclusion. It reads ``runo context --json``
and ``runo list ... --brief --json`` in each member (without ``--brief`` on a runops older
than 2.7), each run's manifest, and ``LOG.md``, ``notes/`` and the analysis report files.

It never runs ``runo sync``; run ``./hub sync`` first for fresh states. The only thing it
writes is ``.hub/status_seen.json``, the run statuses at the last look. ``--peek`` leaves
that mark in place, for an agent that checks states in the middle of its own work.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

# The hub directory (members, AGENTS.md, .hub/ state). ./hub exports it; run by hand, it is the cwd.
HUB = Path(os.environ.get("RUNOPS_HUB_DIR") or Path.cwd()).resolve()
STATES = ["--status", "submitted", "--status", "running"]
SEEN = HUB / ".hub" / "status_seen.json"
SHOWN_IDS = 3
RECORD_SUFFIXES = {".md", ".csv", ".json"}
RECORD_LIMIT = 5_000_000  # bytes; large tables and data-URI reports hold no run ids to find
# Run ids as LOG entries write them: R20261104-0016, R20261104-0016〜0027, R20261104-0007/0008,
# R20261105-000{3..7}.
RUN_RE = re.compile(r"R(\d{8})-(\d{4})((?:\s*[〜～~–/,、・]\s*\d{4}(?!\d))*)")
RUN_BRACE_RE = re.compile(r"R(\d{8})-(\d{0,3})\{(\d+)\.\.(\d+)\}")
RUN_MORE_RE = re.compile(r"([〜～~–/,、・])\s*(\d{4})")
LOG_ENTRY_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})(.*)$", re.MULTILINE)


def members() -> list[Path]:
    return sorted(
        p for p in HUB.iterdir()
        if not p.name.startswith(".") and p.is_dir() and (p / "runops.toml").is_file()
    )


def runo_json(member: Path, args: list[str]) -> tuple[int, object]:
    res = subprocess.run(
        ["runo", *args, "--json"], cwd=member, capture_output=True, text=True,
        env={**os.environ, "COLUMNS": "200"},
    )
    if res.returncode != 0:
        return res.returncode, None
    try:
        return 0, json.loads(res.stdout)
    except json.JSONDecodeError:
        return 1, None


def active_runs(member: Path) -> tuple[list[dict], str]:
    code, data = runo_json(member, ["list", *STATES, "--brief"])
    if code == 2:  # runops < 2.7: no --brief
        code, data = runo_json(member, ["list", *STATES])
    if code != 0 or not isinstance(data, list):
        return [], f"runo list failed (exit {code})"
    return data, ""


def age(stamp: str) -> str:
    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return "?"
    seconds = int((datetime.now().astimezone() - then).total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def progress_text(progress: dict | None) -> str:
    if not progress:
        return "-"
    parts = []
    if progress.get("fraction") is not None:
        parts.append(f"{progress['fraction']:.0%}")
    else:
        parts.append(f"{progress.get('done', '?')} {progress.get('unit', '')}".strip())
    rate = progress.get("rate_recent_per_hour")
    if rate:
        parts.append(f"{3600 / rate:.3g} s/{progress.get('unit', 'unit')}")
    if progress.get("eta_hours") is not None:
        parts.append(f"eta {progress['eta_hours']:.1f}h")
    if progress.get("stalled_hours") is not None:
        parts.append(f"STALLED {progress['stalled_hours']:.1f}h")
    if progress.get("at"):
        parts.append(f"(obs {age(progress['at'])} ago)")
    return " ".join(parts)


def all_runs(member: Path) -> list[dict]:
    """Every run ``runo list`` keeps in view: scratch and archived runs stay out."""
    code, data = runo_json(member, ["list", "--all", "--brief"])
    return data if code == 0 and isinstance(data, list) else []


def run_ids_in(text: str) -> set[str]:
    found: set[str] = set()
    for day, prefix, low, high in RUN_BRACE_RE.findall(text):
        found.update(f"R{day}-{int(prefix + str(i)):04d}" for i in range(int(low), int(high) + 1))
    for day, number, more in RUN_RE.findall(text):
        current = int(number)
        found.add(f"R{day}-{current:04d}")
        for sep, value in RUN_MORE_RE.findall(more):
            last = int(value)
            first = current + 1 if sep in "〜～~–" else last
            found.update(f"R{day}-{i:04d}" for i in range(first, last + 1))
            current = last
    return found


def log_entries(member: Path) -> list[tuple[str, str]]:
    """``(date, text)`` of each dated LOG.md entry, leaving out status snapshots."""
    try:
        text = (member / "LOG.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    marks = list(LOG_ENTRY_RE.finditer(text))
    entries = []
    for mark, following in zip(marks, [*marks[1:], None]):
        body = text[mark.start() : following.start() if following else len(text)]
        entries.append((mark.group(1), mark.group(2), body))
    return [(day, body) for day, heading, body in entries if "進捗確認" not in heading]


def records_in(directory: Path) -> list[tuple[str, str]]:
    """``(modification date, text)`` of the report files directly in *directory*."""
    records = []
    for path in directory.glob("*"):
        try:
            info = path.stat()
            if path.suffix in RECORD_SUFFIXES and info.st_size <= RECORD_LIMIT:
                day = datetime.fromtimestamp(info.st_mtime).astimezone().date().isoformat()
                records.append((day, path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return records


def project_records(member: Path) -> list[tuple[str, str]]:
    topics = [p for p in (member / "analysis").glob("*") if p.is_dir() and p.name != "lib"]
    directories = [member / "notes", *topics, *(topic / "out" for topic in topics)]
    return [record for directory in directories for record in records_in(directory)]


def near_records(member: Path, run: Path) -> list[tuple[str, str]]:
    """Reports of the run itself and of the groups it sits in."""
    directories = [run / "analysis", *(run / "analysis").glob("*")]
    for parent in run.parents:
        if parent == member:
            break
        topics = list(parent.glob("_analysis/*"))
        directories += [*topics, *(topic / "out" for topic in topics)]
    return [record for directory in directories for record in records_in(directory)]


def finished(run: Path) -> tuple[str, list[str]]:
    """Finish time of the last attempt and the times of the run's notes, from its manifest."""
    try:
        data = tomllib.loads((run / "manifest.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return "", []
    attempts = data.get("job", {}).get("attempts") or [{}]
    return attempts[-1].get("finished_at", ""), [n.get("at", "") for n in data.get("notes", [])]


def unconcluded(member: Path, runs: list[dict]) -> list[tuple[str, str]]:
    """``(finish date, id)`` of completed runs that nothing mentions after they finished.

    A rough sign, derived so that it needs no one to file anything: a run counts as written
    up when its id appears in a LOG.md entry, ``notes/*.md`` or a report file of an analysis
    topic dated on or after the day it finished, or when it has a ``runo note`` from then on.
    Runs that finished before the first LOG.md entry are left out: their records predate it.
    """
    entries = log_entries(member)
    if not entries:
        return []
    start = min(day for day, _ in entries)
    shared = [*entries, *project_records(member)]
    open_runs = []
    for r in runs:
        if r.get("status") != "completed":
            continue
        run = member / r.get("run_dir", "")
        ended, notes = finished(run)
        day = ended[:10]
        if not day or day < start or any(at[:10] >= day for at in notes):
            continue
        texts = [*shared, *near_records(member, run)]
        if not any(when >= day and r["id"] in run_ids_in(text) for when, text in texts):
            open_runs.append((day, r["id"]))
    return sorted(open_runs)


def some_ids(ids: list[str]) -> str:
    shown = ", ".join(ids[:SHOWN_IDS])
    return f"{shown}, +{len(ids) - SHOWN_IDS}" if len(ids) > SHOWN_IDS else shown


def load_seen() -> dict:
    try:
        return json.loads(SEEN.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_seen(statuses: dict[str, dict[str, str]]) -> None:
    now = datetime.now().astimezone().isoformat(timespec="minutes")
    temporary = SEEN.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps({"at": now, "runs": statuses}, indent=0), encoding="utf-8")
    temporary.replace(SEEN)


def print_changes(seen: dict, statuses: dict[str, dict[str, str]]) -> None:
    if not seen:
        print("\nchanged since last look: no earlier look recorded")
        return
    print(f"\nchanged since last look ({seen.get('at', '?')}):")
    quiet = True
    for name, now in statuses.items():
        before = seen.get("runs", {}).get(name, {})
        moved: dict[str, list[str]] = defaultdict(list)
        for run_id, state in now.items():
            if before.get(run_id) != state:
                moved[state].append(run_id)
        if moved:
            quiet = False
            text = "; ".join(f"{state} {len(ids)} ({some_ids(ids)})" for state, ids in sorted(moved.items()))
            print(f"  {name}: {text}")
    if quiet:
        print("  nothing")


def print_unconcluded(unwritten: dict[str, list[tuple[str, str]]]) -> None:
    print("\ncompleted, no run id in a later LOG entry, note or analysis file:")
    if not any(unwritten.values()):
        print("  none")
    today = date.today()
    for name, runs in unwritten.items():
        if runs:
            oldest = (today - date.fromisoformat(runs[0][0])).days
            ids = [run_id for _, run_id in runs]
            print(f"  {name}: {len(runs)} runs, oldest {oldest}d ({some_ids(ids)})")


def main() -> int:
    mark = "--peek" not in sys.argv[1:]
    status = 0
    rows: list[tuple[str, ...]] = []
    statuses: dict[str, dict[str, str]] = {}
    unwritten: dict[str, list[tuple[str, str]]] = {}
    for member in members():
        name = member.name
        code, ctx = runo_json(member, ["context"])
        counts = ""
        if code == 0 and isinstance(ctx, dict):
            counts = " ".join(f"{k}={v}" for k, v in sorted(ctx.get("status_counts", {}).items()))
        else:
            counts = f"!! runo context failed (exit {code})"
            status = 1
        runs, problem = active_runs(member)
        if problem:
            counts += f"  !! {problem}"
            status = 1
        print(f"===== {name}: {counts}")
        everything = all_runs(member)
        statuses[name] = {r["id"]: r["status"] for r in everything}
        unwritten[name] = unconcluded(member, everything)
        for r in runs:
            resources = r.get("resources", {})
            res = ":".join(
                str(resources[k]) for k in ("processes", "threads", "cores", "ntasks", "nodes")
                if k in resources
            )
            rows.append((
                name, r["id"], r["status"], r.get("label", ""), r.get("case", ""),
                r.get("job_id", ""), res, (r.get("elapsed") or "-"), progress_text(r.get("progress")),
            ))
    if rows:
        header = ("project", "id", "status", "label", "case", "job", "rsc", "elapsed", "progress")
        widths = [max(len(str(x)) for x in col) for col in zip(header, *rows)]
        print()
        for row in (header, *rows):
            print("  ".join(str(x).ljust(w) for x, w in zip(row, widths)).rstrip())
    else:
        print("\nno active runs")
    print_changes(load_seen(), statuses)
    print_unconcluded(unwritten)
    if mark and status == 0:
        save_seen(statuses)
    return status


if __name__ == "__main__":
    sys.exit(main())
