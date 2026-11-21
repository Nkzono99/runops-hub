#!/usr/bin/env python3
"""hub auto: a periodic agent for the member projects.

Every ``interval_min`` the loop syncs the enabled projects (``runo sync``) and lists the runs
that completed or failed at least ``quiet_hours`` ago and that nothing records yet (the sign
``./hub status`` uses). When a project has such runs, the tick claims them and starts one agent
there (``codex exec`` in the project, sandboxed to the project and ``auto/``). The agent analyses
them, appends its conclusions to ``LOG.md``, and submits the follow-up runs that are clearly
needed through ``hub auto submit``. That gate checks the budget, the conditions of a
continuation and duplicates before it calls ``runo submit``; what does not pass becomes a
proposal for ``./hub auto approve``. A tick without such runs starts no agent.

All state is in files under ``<hub>/auto/`` on the shared file system, so every login node sees
the same state and can pause, resume or stop the loop:

    config.toml         what to do and how much (``enable``/``disable`` edit ``[projects.*]``)
    control.json        what the person wants: run, paused or stopped (``now``: stop the agent too)
    heartbeat.json      what the loop is doing, rewritten every few seconds
    ledger.jsonl        submissions (planned, submitted, failed), hand-overs, deferrals, releases
    claims/P/RUN.json   runs that a tick or a person holds, each with an expiry
    holds.json          projects and runs auto leaves alone
    proposals/N.json    follow-ups beyond the automatic limits
    reports/YYYY-MM.md  what each tick did
    ticks/ID/           worklist, prompt, agent events and final message of one tick
    loop.log            the loop's own output
    bin/                sbatch and tssrun wrappers put first on the agent's PATH

Run ``./hub auto help`` for the commands.
"""
from __future__ import annotations

import argparse
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import tomllib
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from string import Template

LIB = Path(__file__).resolve().parent
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import status as hubstatus  # noqa: E402  (finished runs nothing records, as ./hub status finds them)
from projects import member_paths, shelved_projects  # noqa: E402

CODE = LIB.parent
TEMPLATES = CODE / "templates"
# The hub directory. ./hub exports it; run by hand, it is the cwd.
HUB = Path(os.environ.get("RUNOPS_HUB_DIR") or Path.cwd()).resolve()
AUTO = HUB / "auto"
POLL = float(os.environ.get("HUB_AUTO_POLL", "10"))  # seconds between looks at control.json
STALE = max(12 * POLL, 120.0)  # a heartbeat older than this (seconds) means the loop is gone or hung
# One tmux session per hub directory, so two hubs on one node do not replace each other's loop.
TMUX_SESSION = "hub-auto-" + re.sub(r"[^A-Za-z0-9_-]", "-", HUB.name) + "-" + hashlib.sha1(str(HUB).encode()).hexdigest()[:4]
CREATOR = "hub-auto"  # runo new --created-by of the runs auto may submit
ACTIVE = {"submitted", "running"}
LAYOUT_KEYS = ("processes", "threads", "cores", "nodes", "ntasks", "cpus_per_task")
HOST = socket.gethostname()

DEFAULTS: dict = {
    "auto": {
        "interval_min": 120, "quiet_hours": 6, "claim_hours": 3, "agent_timeout_min": 90,
        "max_runs_per_project": 8, "max_auto_chain": 3, "give_up_after": 2,
        "pause_after_failures": 3, "runner": ["codex", "exec"], "runner_args": [],
        "sandbox": "workspace-write", "keep_tick_days": 30,
    },
    "budget": {
        "window_days": 7, "total": 50000, "derived_total": 10000, "derived_per_run": 500,
        "continue_per_run": 12000, "continue_parent_factor": 1.0, "max_cores": 560,
        "floor_fraction": 0.10, "floor_partitions": ["gr20001a"],
        "partition": {"gr20001a": 20000, "hpa": 30000, "gr10451a": 30000},
    },
    "analysis": {"partitions": ["gr10451a"], "max_cores": 32, "max_hours": 1.0},
    "continue": {
        "job_keys": ["walltime", "partition", "pre_commands", "memory", "job_name"],
        "keys": {
            "beach": ["run.batch_count"],
            "emses": ["jobcon.nstep", "jobcon.jobnum", "jobcon.resume", "jobcon.checkpoint_*"],
            "emses_checkpoint": ["jobcon.nstep", "jobcon.jobnum", "jobcon.resume", "jobcon.checkpoint_*"],
            "grain": ["run.steps", "checkpoint.*", "output.*"],
        },
    },
    "projects": {},
}

MEMBER_NOTE = (
    "- hub auto may visit this project on a schedule: it analyses finished runs, appends\n"
    "  `[auto]` entries to `LOG.md`, and submits the continuations and small follow-ups it\n"
    "  creates (`--created-by hub-auto`, tag `auto`). Inside a tick, `hub auto submit` stands\n"
    "  in for the confirmation before a submit. Leave the runs it has claimed alone (see\n"
    "  `{hub}/hub auto status`), or free one with\n"
    "  `./hub auto release PROJECT/RUN` in the hub; keep it away from a run or this project\n"
    "  with `./hub auto hold`."
)

USAGE = """usage: ./hub auto COMMAND [args]

for people (any login node; the state lives in auto/ on the shared file system)
  status                       loop, budget used, claims, holds, questions, proposals (default)
  start [--foreground]         start the loop in a detached tmux session on this node (hub-auto-<hub>-xxxx)
  pause [--now]                let the running agent finish, then wait (--now: stop it too)
  resume                       go on; starts the loop on this node when none is running
  stop [--now] [--force]       end the loop (--force: also kill it when it runs on this node)
  kick                         run the next tick now instead of at the next interval
  tick [--dry-run] [--project P] [--no-agent] [--no-sync]
                               one tick in this terminal (--dry-run: only show the worklist)
  enable NAME [--no-analyze] [--no-continue] [--no-derive] [--total CORE_H]
  disable NAME                 choose the projects auto works on (auto/config.toml)
  hold TARGET [--why TEXT]     keep auto away from PROJECT or PROJECT/RUN;  unhold TARGET
  claim PROJECT/RUN [--hours H]  hold one run for yourself for a while
  release PROJECT/RUN | --all  drop claims; also lets auto try a deferred or given-up run again
  proposals [--all]            proposals waiting for you
  approve N [--yes]            submit the runs of proposal N (or let the next tick make them)
  reject N [--keep-runs]       drop proposal N (and delete its created runs)
  cancel [--yes]               runo cancel every simulation auto submitted that waits or runs
  report [-n N]                the last N tick reports
  log [-f] [-n N]              the loop's own log

for the agent inside a tick (run in its project)
  submit RUN... --kind continue|derive|approved --basis TEXT [--proposal N] [--check]
  propose --kind continue|derive|other --title TEXT --why TEXT [--run RUN]... [--core-h N]
  defer RUN... --why TEXT      leave runs to the person (a question); release lifts it
"""


class AutoError(Exception):
    """A problem to report in one line, without a traceback."""


# ---------------------------------------------------------------- time, files, locks

def now() -> datetime:
    return datetime.now().astimezone()


def stamp(when: datetime | None = None) -> str:
    return (when or now()).isoformat(timespec="seconds")


def parse_time(text: object) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return when if when.tzinfo else when.astimezone()


def hm(text: object) -> str:
    """``MM-DD HH:MM`` of a time stamp, for the status lines."""
    when = parse_time(text)
    return when.strftime("%m-%d %H:%M") if when else "-"


def ago(when: datetime | None) -> str:
    if when is None:
        return "?"
    seconds = int((now() - when).total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if abs(seconds) >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{HOST}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    temporary.replace(path)


def try_lock(name: str, block: bool = False):
    """An exclusive flock on ``auto/NAME``, or ``None`` when another process holds it.

    flock on /LARGE0 and /LARGE1 (Lustre) holds across login nodes, and the kernel drops it
    when the process dies, so a crashed loop or tick leaves nothing to clean up.
    """
    AUTO.mkdir(parents=True, exist_ok=True)
    handle = open(AUTO / name, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if block else fcntl.LOCK_NB))
    except OSError:
        handle.close()
        return None
    return handle


def unlock(handle) -> None:
    try:
        fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


class Locked:
    """``with Locked("x.lock"):`` waits for the lock."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.handle = None

    def __enter__(self):
        self.handle = try_lock(self.name, block=True)
        if self.handle is None:
            raise AutoError(f"cannot lock auto/{self.name}")
        return self.handle

    def __exit__(self, *exc) -> None:
        unlock(self.handle)


def log(message: str) -> None:
    print(f"{stamp()} {message}", flush=True)


# ---------------------------------------------------------------- configuration

def ensure_config() -> Path:
    path = AUTO / "config.toml"
    if not path.exists():
        AUTO.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(TEMPLATES / "auto_config.toml", path)
    return path


def load_config() -> dict:
    """auto/config.toml over the defaults; a table given in the file replaces the default one."""
    path = AUTO / "config.toml"
    data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    config = {}
    for section, default in DEFAULTS.items():
        given = data.get(section, {})
        config[section] = {**default, **given} if isinstance(given, dict) else default
    return config


def project_settings(config: dict, name: str) -> dict | None:
    """What auto does in project NAME, or None when it is not enabled.

    Keys left out of a hand-written table mean: analyse, but submit nothing. A project set aside
    in the hub (./hub defer / archive) counts as not enabled until ./hub restore; its table stays.
    """
    table = config["projects"].get(name)
    if not isinstance(table, dict) or not table.get("enabled", True) or name in shelved_projects(HUB):
        return None
    return {
        "analyze": bool(table.get("analyze", True)),
        "continue": bool(table.get("continue", False)),
        "derive": bool(table.get("derive", False)),
        "total": table.get("total"),
    }


def enabled(config: dict) -> list[str]:
    return [name for name in sorted(config["projects"]) if project_settings(config, name)]


def toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(str(value), ensure_ascii=False)


def set_project_table(name: str, table: dict | None) -> None:
    """Write (or with None remove) ``[projects.NAME]`` in auto/config.toml, keeping the rest."""
    path = ensure_config()
    text = path.read_text(encoding="utf-8")
    block = re.compile(rf"^\[projects\.{re.escape(name)}\][^\n]*\n(?:(?!\[)[^\n]*(?:\n|$))*", re.M)
    new = ""
    if table is not None:
        new = f"[projects.{name}]\n" + "".join(f"{k} = {toml_value(v)}\n" for k, v in table.items()) + "\n"
    if block.search(text):
        text = block.sub(lambda _: new, text, count=1)
    elif table is not None:
        text = text.rstrip("\n") + "\n\n" + new
    path.write_text(text.rstrip("\n") + "\n", encoding="utf-8")
    tomllib.loads(path.read_text(encoding="utf-8"))  # fail loudly rather than leave a broken file


# ---------------------------------------------------------------- members and runs

def members() -> dict[str, Path]:
    """Every member, with those set aside: what auto submitted there still counts and can be cancelled."""
    return {path.name: path.resolve() for path in member_paths(HUB, shelved=True)}


def member(name: str) -> Path:
    found = members().get(name)
    if found is None:
        raise AutoError(f"{name} is not a member project of {HUB}")
    return found


def here(name: str = "") -> tuple[str, Path]:
    """The project an agent command acts in: --project, the tick's project, or the cwd."""
    found = members()
    name = name or os.environ.get("HUB_AUTO_PROJECT", "")
    if name:
        if name not in found:
            raise AutoError(f"{name} is not a member project of {HUB}")
        return name, found[name]
    cwd = Path.cwd().resolve()
    for each, path in found.items():
        if cwd == path or path in cwd.parents:
            return each, path
    raise AutoError("run this inside a member project, or give --project NAME")


def split_target(text: str) -> tuple[str, str]:
    """``PROJECT/RUN``, ``PROJECT``, or ``RUN`` in the current project."""
    if "/" in text:
        project, run = text.split("/", 1)
        return project, run
    if text in members():
        return text, ""
    return here()[0], text


def runo(project: Path, *args: str, timeout: float = 600, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["runo", *args], cwd=project, capture_output=True, text=True, timeout=timeout,
        env={**(env if env is not None else os.environ), "COLUMNS": "200"},
    )


def list_runs(project: Path) -> list[dict]:
    """``runo list --all --brief --json``: every run in view (not archived, not scratch)."""
    try:
        res = runo(project, "list", "--all", "--brief", "--json")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AutoError(f"runo list in {project}: {exc}") from None
    if res.returncode != 0:
        raise AutoError(f"runo list in {project} failed: {(res.stderr or res.stdout).strip()[-300:]}")
    try:
        data = json.loads(res.stdout)
    except ValueError:
        raise AutoError(f"runo list in {project} printed no JSON") from None
    return data if isinstance(data, list) else []


def manifest(project: Path, run_dir: str) -> dict:
    if not run_dir:
        return {}
    try:
        return tomllib.loads((project / run_dir / "manifest.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def clean_env() -> dict:
    """The environment without auto's wrappers: for runo submit and the real sbatch."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("HUB_AUTO_")}
    shims = (AUTO / "bin").resolve()
    env["PATH"] = os.pathsep.join(
        p for p in env.get("PATH", "").split(os.pathsep) if p and Path(p).resolve() != shims
    )
    return env


# ---------------------------------------------------------------- core-hours

def walltime_hours(text: object) -> float:
    match = re.fullmatch(r"(?:(\d+)-)?(\d+):([0-5]\d):([0-5]\d)", str(text).strip())
    if not match:
        return 0.0
    days, hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return days * 24 + hours + minutes / 60 + seconds / 3600


def job_cores(job: dict) -> int:
    """Physical cores of a run's job, as runops counts them for --rsc: processes × cores."""
    processes = int(job.get("processes", job.get("ntasks", 1)) or 1)
    cores = int(job.get("cores", job.get("cpus_per_task", job.get("threads", 1))) or 1)
    return max(processes * cores, 1)


def reserved(job: dict) -> float:
    """The most a run's job can use: cores × walltime."""
    return job_cores(job) * walltime_hours(job.get("walltime", ""))


def used(data: dict) -> float:
    """Core-hours a run has used: cores × (finish − start) over its attempts (a running one up to now)."""
    job = data.get("job", {})
    cores = job_cores(job)
    total = 0.0
    for attempt in job.get("attempts", []) or []:
        start = parse_time(attempt.get("started_at", ""))
        if start is None:
            continue
        end = parse_time(attempt.get("finished_at", "")) or now()
        total += max((end - start).total_seconds(), 0.0) / 3600 * cores
    return total


# ---------------------------------------------------------------- ledger

def ledger_add(**event) -> dict:
    event = {"at": stamp(), **event}
    with Locked("ledger.lock"):
        with open(AUTO / "ledger.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def ledger() -> list[dict]:
    try:
        lines = (AUTO / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def outcome(event: dict, cache: dict, found: dict[str, Path]) -> tuple[float, bool]:
    """Core-hours one submission counts for now, and whether its job still waits or runs.

    A finished job counts what it used; a waiting or running one, or one whose run is gone,
    counts cores × walltime.
    """
    cost = float(event.get("reserved_core_h", 0) or 0)
    if event.get("kind") == "analysis" or not event.get("run_dir"):
        return cost, False
    key = (event.get("project", ""), event["run_dir"])
    if key not in cache:
        path = found.get(key[0])
        cache[key] = manifest(path, key[1]) if path else {}
    data = cache[key]
    for attempt in data.get("job", {}).get("attempts", []) or []:
        if str(attempt.get("job_id")) != str(event.get("job_id")):
            continue
        if attempt.get("finished_at"):
            start, end = parse_time(attempt.get("started_at", "")), parse_time(attempt["finished_at"])
            hours = (end - start).total_seconds() / 3600 if start and end else 0.0
            return max(hours, 0.0) * int(event.get("cores") or 1), False
        return cost, data.get("run", {}).get("status") in ACTIVE
    return cost, False


def usage(config: dict, events: list[dict] | None = None) -> dict:
    """What auto submitted in the budget window, by kind, partition and project."""
    events = ledger() if events is None else events
    since = now() - timedelta(days=float(config["budget"]["window_days"]))
    cache: dict = {}
    found = members()
    total = derived = approved = 0.0
    partition: dict[str, float] = defaultdict(float)
    project: dict[str, float] = defaultdict(float)
    cores, active = 0, []
    for event in events:
        if event.get("event") != "submit":
            continue
        cost, running = outcome(event, cache, found)
        if running and event.get("kind") in ("continue", "derive"):
            cores += int(event.get("cores", 0) or 0)
            active.append(event)
        at = parse_time(event.get("at", ""))
        if at is None or at < since:
            continue
        if event.get("kind") == "approved":
            approved += cost
            continue
        total += cost
        if event.get("kind") == "derive":
            derived += cost
        partition[event.get("partition", "")] += cost
        project[event.get("project", "")] += cost
    return {
        "total": total, "derived": derived, "approved": approved, "partition": dict(partition),
        "project": dict(project), "active_cores": cores, "active": active,
    }


def allocation(fresh: bool = True) -> dict | None:
    """``show_my_time``'s allocated and remaining time, cached in auto/allocation.json."""
    cache = AUTO / "allocation.json"
    if fresh:
        try:
            res = subprocess.run(["show_my_time"], capture_output=True, text=True, timeout=60)
            text = res.stdout
        except (OSError, subprocess.TimeoutExpired):
            text = ""
        given = re.search(r"Allocated time\s*:\s*([\d.]+)", text)
        left = re.search(r"Remaining time\s*:\s*(-?[\d.]+)", text)
        if given and left and float(given.group(1)) > 0:
            data = {"at": stamp(), "allocated": float(given.group(1)), "remaining": float(left.group(1))}
            try:
                write_json(cache, data)
            except OSError:
                pass
            return data
    data = read_json(cache, None)
    at = parse_time(data.get("at", "")) if isinstance(data, dict) else None
    if at is None or (fresh and now() - at > timedelta(hours=6)):
        return None
    return data


# ---------------------------------------------------------------- claims, holds, deferrals

def claim_file(project: str, run: str) -> Path:
    return AUTO / "claims" / project / f"{run}.json"


def claims() -> list[dict]:
    """Live claims; expired ones are removed on the way."""
    found = []
    for path in sorted((AUTO / "claims").glob("*/*.json")):
        data = read_json(path, None)
        until = parse_time(data.get("until", "")) if isinstance(data, dict) else None
        if until is None or until < now():
            path.unlink(missing_ok=True)
            continue
        found.append(data)
    return found


def claim(project: str, run: str, holder: str, hours: float, why: str = "") -> None:
    write_json(claim_file(project, run), {
        "project": project, "run": run, "holder": holder, "at": stamp(),
        "until": stamp(now() + timedelta(hours=hours)), "why": why,
    })


def claimed_by_other(project: str, run: str, holder: str) -> str:
    data = read_json(claim_file(project, run), None)
    if not isinstance(data, dict) or data.get("holder") == holder:
        return ""
    until = parse_time(data.get("until", ""))
    if until is None or until < now():
        return ""
    return f"claimed by {data.get('holder')} until {hm(data.get('until'))}"


def release(project: str = "", run: str = "", holder: str = "") -> int:
    count = 0
    for path in (AUTO / "claims").glob(f"{project or '*'}/{run or '*'}.json"):
        data = read_json(path, {})
        if holder and data.get("holder") != holder:
            continue
        path.unlink(missing_ok=True)
        count += 1
    return count


def holds() -> dict:
    data = read_json(AUTO / "holds.json", {})
    return data if isinstance(data, dict) else {}


def held(project: str, run: str = "") -> str:
    found = holds()
    for key in (project, f"{project}/{run}" if run else ""):
        if key and key in found:
            return f"{key} is on hold ({found[key].get('why') or 'no reason given'})"
    return ""


def deferred(events: list[dict], project: str) -> dict[str, dict]:
    """Runs an agent left to the person and nobody released since."""
    waiting: dict[str, dict] = {}
    for event in events:
        if event.get("project") != project:
            continue
        if event.get("event") == "defer":
            waiting[event.get("run", "")] = event
        elif event.get("event") == "release":
            if event.get("run"):
                waiting.pop(event["run"], None)
            else:
                waiting.clear()
    return waiting


def tries(events: list[dict], project: str) -> Counter:
    """How many ticks handed each run to an agent since it was last released."""
    count: Counter = Counter()
    for event in events:
        if event.get("project") != project:
            continue
        if event.get("event") == "work":
            count.update(event.get("runs", []))
        elif event.get("event") == "release":
            if event.get("run"):
                count.pop(event["run"], None)
            else:
                count.clear()
    return count


# ---------------------------------------------------------------- control and heartbeat

def control() -> dict:
    data = read_json(AUTO / "control.json", {})
    return {"want": "run", "now": False, "kick": False, **(data if isinstance(data, dict) else {})}


def set_control(**changes) -> dict:
    with Locked("control.lock"):
        data = control()
        if "want" in changes:
            data.pop("reason", None)
        data.update(changes)
        data.update(at=stamp(), by=f"{os.environ.get('USER', '?')}@{HOST}")
        write_json(AUTO / "control.json", data)
    return data


def heartbeat() -> dict:
    data = read_json(AUTO / "heartbeat.json", {})
    return data if isinstance(data, dict) else {}


def loop_alive() -> bool:
    """Whether a loop holds auto/loop.lock (on any login node)."""
    handle = try_lock("loop.lock")
    if handle is None:
        return True
    unlock(handle)
    return False


def loop_state() -> tuple[str, dict]:
    """``running``, ``hung`` (the lock is held but the heartbeat stopped) or ``stopped``."""
    beat = heartbeat()
    at = parse_time(beat.get("at", ""))
    fresh = at is not None and (now() - at).total_seconds() <= STALE
    if loop_alive():
        return ("running" if fresh else "hung"), beat
    return "stopped", beat


def running_tick() -> str:
    """A tick in progress (the loop's or one run by hand), from auto/current_tick.json."""
    handle = try_lock("tick.lock")
    if handle is not None:
        unlock(handle)
        return ""
    data = read_json(AUTO / "current_tick.json", {})
    doing = f", {data.get('state')} in {data.get('project')}" if data.get("project") else ""
    return (f"tick {data.get('tick', '?')} running on {data.get('host', '?')} (pid {data.get('pid', '?')}) "
            f"since {hm(data.get('started'))}{doing}")


def describe_loop() -> str:
    state, beat = loop_state()
    where = f"{beat.get('host', '?')} (pid {beat.get('pid', '?')}, {beat.get('session', '?')})"
    if state == "stopped":
        last = f", last seen {ago(parse_time(beat.get('at', '')))} ago on {beat.get('host')}" if beat else ""
        tick = running_tick()
        return f"loop: not running{last}" + (f"; {tick} (by hand)" if tick else "")
    if state == "hung":
        return (f"loop: HUNG? it holds auto/loop.lock on {where} but its heartbeat is "
                f"{ago(parse_time(beat.get('at', '')))} old; on that node: ./hub auto stop --force")
    doing = beat.get("state", "?")
    if doing == "waiting":
        doing = f"waiting, next tick {hm(beat.get('next_tick'))}"
    elif doing in ("agent", "sync"):
        doing = f"{doing} in {beat.get('project')} (tick {beat.get('tick')})"
    return f"loop: running on {where} — {doing}"


# ---------------------------------------------------------------- proposals

def proposals() -> list[dict]:
    found = []
    for path in sorted((AUTO / "proposals").glob("*.json")):
        data = read_json(path, None)
        if isinstance(data, dict):
            found.append(data)
    return found


def save_proposal(data: dict) -> None:
    write_json(AUTO / "proposals" / f"{int(data['id']):04d}.json", data)


def add_proposal(**fields) -> dict:
    with Locked("proposals.lock"):
        number = max((int(p.get("id", 0)) for p in proposals()), default=0) + 1
        data = {"id": number, "at": stamp(), "status": "open", **fields}
        save_proposal(data)
    return data


def proposal(number: int) -> dict:
    data = read_json(AUTO / "proposals" / f"{int(number):04d}.json", None)
    if not isinstance(data, dict):
        raise AutoError(f"no proposal #{number}")
    return data


# ---------------------------------------------------------------- the gate

def flat_params(value, prefix: str = "") -> dict:
    """Params as dotted keys, the form of case params and ``--set``: ``jobcon.nstep``, ``species.0.wp``.

    Adapters record params either flat (beach: ``run.batch_count``) or as nested tables
    (emses: ``jobcon = {nstep = ...}``); both come out the same. A list of tables is indexed,
    any other list is one value.
    """
    if isinstance(value, dict):
        items = [(str(k), v) for k, v in value.items()]
    elif isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
        items = [(str(i), v) for i, v in enumerate(value)]
    else:
        return {prefix: value}
    out: dict = {}
    for key, sub in items:
        out.update(flat_params(sub, f"{prefix}.{key}" if prefix else key))
    return out


def changed_params(parent: dict, child: dict) -> list[str]:
    a, b = flat_params(parent.get("params", {})), flat_params(child.get("params", {}))
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def continuation_problems(config: dict, parent: dict, child: dict) -> list[str]:
    """Why CHILD is not a continuation of PARENT; empty when it is one.

    A continuation changes only the end time and checkpoint keys of its adapter
    (``[continue.keys]``) and the job keys in ``[continue].job_keys``, and keeps the layout.
    """
    adapter = child.get("simulator", {}).get("adapter", "")
    keys = config["continue"]["keys"]
    patterns = keys.get(adapter, keys.get("*", []))
    other = [k for k in changed_params(parent, child) if not any(fnmatch.fnmatchcase(k, p) for p in patterns)]
    problems = []
    if other:
        shown = ", ".join(other[:8]) + (f" and {len(other) - 8} more" if len(other) > 8 else "")
        problems.append(
            f"inputs other than the continuation keys ({', '.join(patterns) or 'none for ' + adapter}) "
            f"differ from the parent: {shown}"
        )
    pj, cj = parent.get("job", {}), child.get("job", {})
    layout = [k for k in LAYOUT_KEYS if pj.get(k) != cj.get(k)]
    if layout:
        problems.append(f"the MPI/thread layout differs from the parent: {', '.join(layout)}")
    allowed = set(config["continue"]["job_keys"]) | set(LAYOUT_KEYS)
    runtime = {"scheduler", "job_id", "attempt", "attempts", "submitted_at", "finished_at",
               "last_slurm_state", "exit_code", "pending_reason"}
    job = [k for k in set(pj) | set(cj) if k not in (allowed | runtime) and pj.get(k) != cj.get(k)]
    if job:
        problems.append(f"job settings other than {', '.join(sorted(allowed - set(LAYOUT_KEYS)))} differ: {', '.join(sorted(job))}")
    return problems


def chain_depth(project: Path, by_id: dict[str, dict], run: dict) -> int:
    """1 + how many runs auto created in a row above RUN (RUN is auto's own)."""
    depth, seen = 1, {run["id"]}
    parent = run.get("parent_run", "")
    while parent and parent in by_id and parent not in seen:
        seen.add(parent)
        data = manifest(project, by_id[parent].get("run_dir", ""))
        if data.get("origin", {}).get("created_by") != CREATOR:
            break
        depth += 1
        parent = by_id[parent].get("parent_run", "")
    return depth


def gate(config: dict, name: str, project: Path, run_ids: list[str], kind: str,
         number: int | None = None, holder: str = "") -> tuple[list[dict], list[str]]:
    """The submissions RUN_IDS would make, and every reason they may not. Submits nothing."""
    problems: list[str] = []
    settings = project_settings(config, name)
    if settings is None and name in shelved_projects(HUB):
        problems.append(f"{name} is set aside in the hub (./hub shelf); ./hub restore {name} first")
    elif settings is None:
        problems.append(f"{name} is not enabled in auto/config.toml")
    elif kind in ("continue", "derive") and not settings[kind]:
        problems.append(f"{kind} is off for {name} in auto/config.toml; propose it instead")
    if kind not in ("continue", "derive", "approved"):
        problems.append(f"unknown kind {kind!r}")
    want = control()["want"]
    if want != "run":
        problems.append(f"hub auto is {want}")
    problems += [p for p in [held(name)] if p]
    wanted = None
    if kind == "approved":
        if number is None:
            problems.append("--kind approved needs --proposal N")
        else:
            wanted = proposal(number)
            if wanted.get("project") != name or wanted.get("status") != "accepted":
                problems.append(f"proposal #{number} is {wanted.get('status')} in {wanted.get('project')}, "
                                f"not an accepted proposal of {name}")
    runs = list_runs(project)
    by_id = {r["id"]: r for r in runs}
    budget = config["budget"]
    plans = []
    for rid in run_ids:
        run = by_id.get(rid)
        if run is None:
            problems.append(f"{rid}: no such run in {name}")
            continue
        data = manifest(project, run.get("run_dir", ""))
        job = data.get("job", {})
        cost, hours, cores = reserved(job), walltime_hours(job.get("walltime", "")), job_cores(job)
        parent = run.get("parent_run", "") or ""
        if run.get("status") != "created":
            problems.append(f"{rid} is {run.get('status')}; only created runs are submitted")
        if data.get("origin", {}).get("created_by") != CREATOR:
            problems.append(f"{rid} was created by {data.get('origin', {}).get('created_by') or '?'}; "
                            f"auto submits only runs made with runo new --created-by {CREATOR}")
        if hours <= 0:
            problems.append(f"{rid} has no walltime")
        problems += [f"{rid}: {p}" for p in [held(name, rid), parent and held(name, parent)] if p]
        twins = [o["id"] for o in runs if o["id"] != rid and run.get("input_hash")
                 and o.get("input_hash") == run.get("input_hash") and o.get("status") in ACTIVE | {"completed"}]
        if twins:
            problems.append(f"{rid} has the same input as {', '.join(twins)}")
        if kind == "continue":
            if parent not in by_id:
                problems.append(f"{rid}: a continuation needs its parent run (runo new --from PARENT)")
            else:
                parent_data = manifest(project, by_id[parent].get("run_dir", ""))
                problems += [f"{rid}: {p}" for p in continuation_problems(config, parent_data, data)]
                busy = [o["id"] for o in runs if o["id"] != rid and o.get("parent_run") == parent
                        and o.get("status") in ACTIVE]
                if busy:
                    problems.append(f"{rid}: {parent} already has a waiting or running child ({', '.join(busy)})")
                other = claimed_by_other(name, parent, holder)
                if other:
                    problems.append(f"{rid}: its parent {parent} is {other}")
                limit = float(budget["continue_per_run"])
                spent = used(parent_data)
                if spent > 0:
                    limit = min(limit, spent * float(budget["continue_parent_factor"]))
                if cost > limit:
                    problems.append(f"{rid}: {cost:,.0f} core-h is over the continuation limit {limit:,.0f} "
                                    f"(continue_per_run, and the parent's {spent:,.0f} core-h used)")
                depth = chain_depth(project, by_id, run)
                if depth > int(config["auto"]["max_auto_chain"]):
                    problems.append(f"{rid} would be auto's continuation number {depth} in a row "
                                    f"(max_auto_chain {config['auto']['max_auto_chain']}); propose it")
        elif kind == "derive" and cost > float(budget["derived_per_run"]):
            problems.append(f"{rid}: {cost:,.0f} core-h is over derived_per_run {budget['derived_per_run']:,}")
        plans.append({
            "project": name, "run": rid, "run_dir": run.get("run_dir", ""), "kind": kind,
            "partition": job.get("partition", ""), "cores": cores, "walltime_h": round(hours, 3),
            "reserved_core_h": round(cost, 1), "parent": parent,
        })
    adding = sum(p["reserved_core_h"] for p in plans)
    if kind == "approved" and wanted is not None and wanted.get("core_h"):
        if adding > 1.2 * float(wanted["core_h"]):
            problems.append(f"{adding:,.0f} core-h is over 1.2 × the {wanted['core_h']:,.0f} core-h of proposal #{number}")
    if kind in ("continue", "derive") and plans:
        spent = usage(config)
        if spent["total"] + adding > budget["total"]:
            problems.append(f"the week's total would reach {spent['total'] + adding:,.0f} of {budget['total']:,} core-h")
        if kind == "derive" and spent["derived"] + adding > budget["derived_total"]:
            problems.append(f"follow-ups would reach {spent['derived'] + adding:,.0f} of {budget['derived_total']:,} core-h")
        by_partition: dict[str, float] = defaultdict(float)
        for plan in plans:
            by_partition[plan["partition"]] += plan["reserved_core_h"]
        for part, amount in by_partition.items():
            cap = budget["partition"].get(part)
            if cap is None:
                problems.append(f"partition {part or '(none)'} has no budget in auto/config.toml")
            elif spent["partition"].get(part, 0) + amount > cap:
                problems.append(f"{part} would reach {spent['partition'].get(part, 0) + amount:,.0f} of {cap:,} core-h")
        cap = settings.get("total") if settings else None
        if cap and spent["project"].get(name, 0) + adding > cap:
            problems.append(f"{name} would reach {spent['project'].get(name, 0) + adding:,.0f} of its {cap:,} core-h")
        cores = sum(p["cores"] for p in plans)
        if spent["active_cores"] + cores > budget["max_cores"]:
            problems.append(f"auto's waiting and running jobs would hold {spent['active_cores'] + cores} "
                            f"of max_cores {budget['max_cores']} cores")
    floors = {p["partition"] for p in plans} & set(budget["floor_partitions"])
    if floors:
        left = allocation(fresh=True) or allocation(fresh=False)
        if left is None:
            problems.append(f"show_my_time gave no remaining time; nothing goes to {', '.join(sorted(floors))}")
        elif left["remaining"] < float(budget["floor_fraction"]) * left["allocated"]:
            problems.append(f"show_my_time: {left['remaining']:,.0f} of {left['allocated']:,.0f} days left "
                            f"(below floor_fraction {budget['floor_fraction']})")
    return plans, problems


def submit_plans(name: str, project: Path, plans: list[dict], tick: str, basis: str,
                 number: int | None = None) -> list[dict]:
    """runo submit the planned runs and record each outcome in the ledger."""
    extra = {"tick": tick, "basis": basis, **({"proposal": number} if number is not None else {})}
    for plan in plans:
        ledger_add(event="planned", **extra, **plan)
    ids = [p["run"] for p in plans]
    try:
        res = runo(project, "submit", *ids, "--json", "-y", timeout=900, env=clean_env())
        out, err = res.stdout, res.stderr
    except subprocess.TimeoutExpired:
        out, err = "", "runo submit timed out"
    try:
        results = {r.get("id"): r for r in json.loads(out) if isinstance(r, dict)}
    except (ValueError, TypeError):
        results = {}
    recorded = []
    for plan in plans:
        result = results.get(plan["run"], {})
        if result.get("job_id"):
            event = ledger_add(event="submit", job_id=str(result["job_id"]), **extra, **plan)
            try:
                runo(project, "note", plan["run"],
                     f"[auto {tick}] submitted by hub auto ({plan['kind']}): {basis}", "--by", CREATOR, env=clean_env())
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            what = "submit-unknown" if result.get("result") == "unknown" else "submit-failed"
            event = ledger_add(event=what, error=(result.get("error") or err.strip())[-500:], **extra, **plan)
        recorded.append(event)
    return recorded


# ---------------------------------------------------------------- sbatch / tssrun wrapper

SBATCH_FLAGS = {
    "--parsable", "--test-only", "-W", "--wait", "-H", "--hold", "-Q", "--quiet", "--requeue",
    "--no-requeue", "--exclusive", "-v", "--verbose", "-h", "--help", "-V", "--version", "--usage",
    "--ignore-pbs", "--spread-job", "--use-min-nodes", "-O", "--overcommit", "--contiguous",
    "--get-user-env", "--no-kill", "-k",
}
INFO_FLAGS = {"-h", "--help", "-V", "--version", "--usage", "--test-only"}
SBATCH_NAMES = {
    "-p": "partition", "--partition": "partition", "-t": "time", "--time": "time", "--rsc": "rsc",
    "-n": "ntasks", "--ntasks": "ntasks", "-c": "cpus", "--cpus-per-task": "cpus", "--wrap": "wrap",
}


def parse_options(args: list[str], flags: set[str]) -> tuple[dict[str, str], list[str]]:
    """Leading options of a command line as {option: value}, and the rest from the first operand."""
    found: dict[str, str] = {}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            return found, args[i + 1:]
        if not arg.startswith("-") or arg == "-":
            return found, args[i:]
        if arg in flags:
            found[arg] = ""
            i += 1
        elif arg.startswith("--") and "=" in arg:
            key, _, value = arg.partition("=")
            found[key] = value
            i += 1
        elif not arg.startswith("--") and len(arg) > 2:
            found[arg[:2]] = arg[2:]
            i += 1
        else:
            found[arg] = args[i + 1] if i + 1 < len(args) else ""
            i += 2
    return found, []


def directives(script: Path) -> list[str]:
    """The ``#SBATCH`` options of a job script (Slurm reads them up to the first command)."""
    words: list[str] = []
    try:
        lines = script.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return words
    for line in lines[1:] if lines and lines[0].startswith("#!") else lines:
        text = line.strip()
        if not text:
            continue
        if not text.startswith("#"):
            break
        if text.startswith("#SBATCH"):
            try:
                words += shlex.split(text[len("#SBATCH"):], comments=True)
            except ValueError:
                continue
    return words


def slurm_hours(text: str, tssrun: bool = False) -> float:
    """Hours of a Slurm time limit (minutes, M:S, H:M:S, D-H[:M[:S]]); tssrun -t is H:M."""
    text = text.strip()
    if not text or text.upper() in ("UNLIMITED", "INFINITE", "-1"):
        return float("inf")
    try:
        if tssrun:
            parts = [int(x) for x in text.split(":")]
            return parts[0] + (parts[1] / 60 if len(parts) > 1 else 0)
        days = 0
        if "-" in text:
            head, text = text.split("-", 1)
            days = int(head)
            parts = [int(x) for x in text.split(":")] + [0, 0]
            return days * 24 + parts[0] + parts[1] / 60 + parts[2] / 3600
        parts = [int(x) for x in text.split(":")]
    except ValueError:
        return float("inf")
    if len(parts) == 1:
        return parts[0] / 60
    if len(parts) == 2:
        return parts[0] / 60 + parts[1] / 3600
    return parts[0] + parts[1] / 60 + parts[2] / 3600


def job_request(tool: str, args: list[str]) -> dict:
    """Partition, cores, hours and script of an sbatch or tssrun command line."""
    if tool == "tssrun":
        found: dict[str, str] = {}
        i = 0
        while i < len(args):
            arg = args[i]
            if arg in ("-p", "--rsc", "-t", "-stdin"):
                found[arg] = args[i + 1] if i + 1 < len(args) else ""
                i += 2
            elif arg.startswith("--rsc="):
                found["--rsc"] = arg.split("=", 1)[1]
                i += 1
            elif arg.startswith("-") and arg not in ("-sopt",):
                i += 1
            else:
                break
        request = {"partition": found.get("-p", ""), "rsc": found.get("--rsc", ""), "script": ""}
        hours = slurm_hours(found["-t"], tssrun=True) if found.get("-t") else None
    else:
        options, rest = parse_options(args, SBATCH_FLAGS)
        named = {SBATCH_NAMES[k]: v for k, v in options.items() if k in SBATCH_NAMES}
        script = rest[0] if rest and "wrap" not in named else ""
        request = {}
        if script:
            inner, _ = parse_options(directives(Path(script)), SBATCH_FLAGS)
            request.update({SBATCH_NAMES[k]: v for k, v in inner.items() if k in SBATCH_NAMES})
        request.update(named)
        request["script"] = script
        hours = slurm_hours(request["time"]) if request.get("time") else None
    if request.get("rsc"):
        fields = dict(part.split("=", 1) for part in request["rsc"].split(":") if "=" in part)
        try:
            cores = int(fields.get("p", 1)) * int(fields.get("c", fields.get("t", 1)))
        except ValueError:
            cores = 10**6
    else:
        try:
            cores = int(request.get("ntasks") or 1) * int(request.get("cpus") or 1)
        except ValueError:
            cores = 10**6
    return {"partition": request.get("partition", ""), "cores": cores, "hours": hours,
            "script": request.get("script", ""), "wrap": request.get("wrap", "")}


def is_run_job(script: str) -> bool:
    """A runops run's own job script (``<run>/submit/job.sh``)."""
    if not script:
        return False
    path = Path(script).resolve()
    return path.parent.name == "submit" and (path.parent.parent / "manifest.toml").is_file()


def analysis_problems(config: dict, request: dict) -> list[str]:
    limits = config["analysis"]
    problems = []
    if is_run_job(request["script"]):
        problems.append("this is a run's job script; simulation runs go through `hub auto submit`")
    if request["partition"] not in limits["partitions"]:
        problems.append(f"partition {request['partition'] or '(not given)'} is not one of "
                        f"{', '.join(limits['partitions'])} (give -p)")
    if request["cores"] > int(limits["max_cores"]):
        problems.append(f"{request['cores']} cores is over {limits['max_cores']}")
    hours = 1.0 if request["hours"] is None else request["hours"]  # the partitions' DefTime is 1 h
    if hours > float(limits["max_hours"]):
        problems.append(f"{hours:g} h is over {limits['max_hours']:g} h")
    return problems


def find_real(tool: str) -> str:
    shims = (AUTO / "bin").resolve()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry or Path(entry).resolve() == shims:
            continue
        candidate = Path(entry) / tool
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return ""


def write_shims() -> Path:
    """auto/bin/{sbatch,tssrun}: wrappers that send an agent's jobs through the analysis check."""
    directory = AUTO / "bin"
    directory.mkdir(parents=True, exist_ok=True)
    for tool in ("sbatch", "tssrun"):
        path = directory / tool
        text = (f"#!/bin/sh\n# hub auto: {tool} inside a tick goes through the analysis-job check\n"
                f"exec {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} "
                f"shim {tool} \"$@\"\n")
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
    return directory


def shim(tool: str, args: list[str]) -> int:
    real = find_real(tool)
    if not real:
        print(f"hub auto: no {tool} on PATH", file=sys.stderr)
        return 127
    env = clean_env()
    if not os.environ.get("HUB_AUTO_TICK") or INFO_FLAGS & set(args):
        os.execve(real, [real, *args], env)
    request = job_request(tool, args)
    problems = analysis_problems(load_config(), request)
    if problems:
        limits = load_config()["analysis"]
        print(f"hub auto refused this {tool}:\n  - " + "\n  - ".join(problems) +
              f"\n  Inside a tick, agents may submit analysis jobs only ({', '.join(limits['partitions'])}, "
              f"<= {limits['max_cores']} cores, <= {limits['max_hours']:g} h). Simulation runs go through "
              "`hub auto submit`; anything else, `hub auto propose`.", file=sys.stderr)
        return 1
    hours = 1.0 if request["hours"] is None else request["hours"]
    event = {
        "tick": os.environ.get("HUB_AUTO_TICK", ""), "project": os.environ.get("HUB_AUTO_PROJECT", ""),
        "kind": "analysis", "tool": tool, "partition": request["partition"], "cores": request["cores"],
        "walltime_h": hours, "reserved_core_h": round(request["cores"] * hours, 1),
        "script": request["script"] or request["wrap"][:200], "cwd": os.getcwd(),
    }
    if tool == "tssrun":
        ledger_add(event="submit", **event)
        os.execve(real, [real, *args], env)
    res = subprocess.run([real, *args], env=env, capture_output=True, text=True)
    sys.stdout.write(res.stdout)
    sys.stderr.write(res.stderr)
    match = re.search(r"Submitted batch job (\d+)", res.stdout) or re.match(r"\s*(\d+)\s*(?:;|$)", res.stdout)
    if res.returncode == 0 and match:
        ledger_add(event="submit", job_id=match.group(1), **event)
    return res.returncode


# ---------------------------------------------------------------- worklist

def is_continuation(config: dict, project: Path, by_id: dict[str, dict], run: dict) -> bool:
    parent = by_id.get(run.get("parent_run", "") or "")
    if parent is None:
        return False
    return not continuation_problems(
        config, manifest(project, parent.get("run_dir", "")), manifest(project, run.get("run_dir", ""))
    )


def worklist(config: dict, name: str, project: Path, runs: list[dict], events: list[dict], holder: str) -> dict:
    """What a tick hands to the agent of one project."""
    settings = project_settings(config, name) or {"analyze": False}
    limits = config["auto"]
    quiet = timedelta(hours=float(limits["quiet_hours"]))
    children: dict[str, list[dict]] = defaultdict(list)
    for run in runs:
        if run.get("parent_run"):
            children[run["parent_run"]].append(run)
    done = [r for r in runs if r.get("status") in ("completed", "failed")]
    unwritten = {rid for _, rid in hubstatus.unconcluded(project, [dict(r, status="completed") for r in done])}
    waiting = deferred(events, name)
    count = tries(events, name)
    items, skipped = [], []
    for run in done:
        rid = run["id"]
        if rid not in unwritten:
            continue
        if run.get("status") == "failed" and (children[rid] or run.get("superseded_by")):
            continue  # someone already retried or replaced it
        data = manifest(project, run.get("run_dir", ""))
        last = (data.get("job", {}).get("attempts") or [{}])[-1]
        ended = parse_time(last.get("finished_at", ""))
        reason = ""
        if ended is not None and now() - ended < quiet:
            reason = f"finished {ago(ended)} ago (quiet_hours {limits['quiet_hours']})"
        reason = (reason or held(name, rid) or claimed_by_other(name, rid, holder)
                  or (f"deferred: {waiting[rid].get('why', '')}" if rid in waiting else "")
                  or (f"{count[rid]} ticks did not write it up; ./hub auto release {name}/{rid} to retry"
                      if count[rid] >= int(limits["give_up_after"]) else "")
                  or ("" if settings["analyze"] else "analyze is off"))
        if reason:
            skipped.append({"id": rid, "reason": reason})
            continue
        items.append({
            "id": rid, "status": run.get("status"), "label": run.get("label", ""), "case": run.get("case", ""),
            "run_dir": run.get("run_dir", ""), "finished_at": last.get("finished_at", ""),
            "slurm_state": last.get("slurm_state", ""), "why": data.get("run", {}).get("why", ""),
            "created_by": data.get("origin", {}).get("created_by", ""), "tags": run.get("tags", []),
            "parent_run": run.get("parent_run", ""),
            "children": [{"id": c["id"], "status": c.get("status"), "label": c.get("label", "")} for c in children[rid]],
            "resources": run.get("resources", {}), "walltime_used": run.get("walltime_used", ""),
            "core_h_used": round(used(data), 1),
        })
    items.sort(key=lambda item: item["finished_at"])
    most = int(limits["max_runs_per_project"])
    skipped += [{"id": item["id"], "reason": "over max_runs_per_project; next tick"} for item in items[most:]]
    stalled = [
        {"id": r["id"], "label": r.get("label", ""), "stalled_hours": r["progress"]["stalled_hours"]}
        for r in runs if r.get("status") == "running" and (r.get("progress") or {}).get("stalled_hours")
    ]
    accepted = [p for p in proposals() if p.get("project") == name and p.get("status") == "accepted"]
    return {"project": name, "dir": str(project), "runs": items[:most], "skipped": skipped,
            "stalled": stalled, "proposals": accepted}


# ---------------------------------------------------------------- tick

def reconcile(name: str, runs: list[dict], events: list[dict]) -> list[str]:
    """Settle submissions a stopped tick planned but never recorded."""
    by_id = {r["id"]: r for r in runs}
    last: dict[str, dict] = {}
    for event in events:
        if event.get("project") == name and event.get("event") in (
            "planned", "submit", "submit-failed", "submit-unknown", "abandoned"
        ):
            last[event.get("run", "")] = event
    notes = []
    keep = ("tick", "project", "run", "run_dir", "kind", "partition", "cores", "walltime_h",
            "reserved_core_h", "parent", "basis", "proposal")
    for rid, event in last.items():
        if event["event"] not in ("planned", "submit-unknown") or not rid:
            continue
        fields = {k: event[k] for k in keep if k in event}
        run = by_id.get(rid, {})
        if run.get("job_id") and run.get("status") != "created":
            ledger_add(event="submit", job_id=str(run["job_id"]), recovered=True, **fields)
            notes.append(f"{name}: {rid} was submitted (job {run['job_id']}) by a tick that stopped before recording it")
        else:
            ledger_add(event="abandoned", **fields)
            hint = f"; check squeue for a job named {rid}" if event["event"] == "submit-unknown" else ""
            notes.append(f"{name}: {rid} was planned by {event.get('tick')} but not submitted; left as created{hint}")
    return notes


def cancel_duplicates(config: dict, name: str, project: Path, runs: list[dict], events: list[dict]) -> list[str]:
    """Cancel auto's run when another waiting or running run does the same work.

    Same work: the same input, or another continuation of the same parent. Auto yields to a
    person's run; of two auto runs the later one goes.
    """
    mine = {e["run"]: e for e in events if e.get("event") == "submit" and e.get("project") == name
            and e.get("kind") in ("continue", "derive")}
    by_id = {r["id"]: r for r in runs}
    active = [r for r in runs if r.get("status") in ACTIVE]
    notes, gone = [], set()
    for run in active:
        own = mine.get(run["id"])
        if own is None or run["id"] in gone:
            continue
        for other in active:
            if other["id"] == run["id"] or other["id"] in gone:
                continue
            same_input = bool(run.get("input_hash")) and run.get("input_hash") == other.get("input_hash")
            same_step = (own.get("kind") == "continue" and run.get("parent_run")
                         and other.get("parent_run") == run.get("parent_run")
                         and is_continuation(config, project, by_id, other))
            if not (same_input or same_step):
                continue
            theirs = mine.get(other["id"])
            if theirs is not None and theirs.get("at", "") > own.get("at", ""):
                continue  # the other auto run is the later one; it goes on its own turn
            why = (f"hub auto: duplicate of {other['id']} "
                   f"({'same input' if same_input else 'another continuation of ' + run['parent_run']})")
            res = runo(project, "cancel", run["id"], "--why", why, "-y", env=clean_env())
            ledger_add(event="cancel", project=name, run=run["id"], duplicate_of=other["id"], why=why,
                       ok=res.returncode == 0)
            notes.append(f"{name}: cancelled {run['id']} — {why}" + ("" if res.returncode == 0 else
                         f" (runo cancel failed: {res.stderr.strip()[-200:]})"))
            gone.add(run["id"])
            break
    return notes


def budget_text(config: dict, name: str) -> str:
    spent = usage(config)
    budget = config["budget"]
    settings = project_settings(config, name) or {}
    lines = [
        f"- 合計: {spent['total']:,.0f} / {budget['total']:,}（残り {max(budget['total'] - spent['total'], 0):,.0f}）",
        f"- うち派生: {spent['derived']:,.0f} / {budget['derived_total']:,}。派生 1 本は {budget['derived_per_run']:,} 以下",
        f"- 継続 1 本: {budget['continue_per_run']:,} 以下、かつ親 run の実績 × {budget['continue_parent_factor']:g} 以下。"
        f"auto の継続が続くのは {config['auto']['max_auto_chain']} 回まで",
        "- partition ごと: " + ", ".join(
            f"{p} {spent['partition'].get(p, 0):,.0f} / {cap:,}" for p, cap in budget["partition"].items()
        ) + "（ここに無い partition には投入できない）",
        f"- auto が投入して待機・実行中のコア: {spent['active_cores']} / {budget['max_cores']}",
    ]
    if settings.get("total"):
        lines.append(f"- この project: {spent['project'].get(name, 0):,.0f} / {settings['total']:,}")
    return "\n".join(lines)


def build_prompt(config: dict, work: dict, tick: str) -> str:
    settings = project_settings(config, work["project"]) or {}
    limits = config["analysis"]
    keys = "; ".join(f"{adapter}: {', '.join(k)}" for adapter, k in config["continue"]["keys"].items())
    accepted = "\n".join(
        f"- #{p['id']}（{p.get('kind')}、見積もり {float(p.get('core_h') or 0):,.0f} core-h）{p.get('title')}: {p.get('why')}"
        for p in work["proposals"]
    ) or "なし"
    off = "この project では無効。必要なら提案に回す"
    mapping = {
        "project": work["project"], "project_dir": work["dir"], "tick": tick,
        "today": now().strftime("%Y-%m-%d"), "hub_auto": f"{HUB}/hub auto",
        "analysis_limit": f"{'/'.join(limits['partitions'])}、{limits['max_cores']} コア以内、{limits['max_hours']:g} 時間以内",
        "continue_state": "この project では有効" if settings.get("continue") else off,
        "derive_state": "この project では有効" if settings.get("derive") else off,
        "derived_per_run": f"{config['budget']['derived_per_run']:,}", "continue_keys": keys,
        "window_days": f"{config['budget']['window_days']:g}", "budget": budget_text(config, work["project"]),
        "accepted": accepted, "quiet_hours": f"{config['auto']['quiet_hours']:g}",
        "worklist": json.dumps(work["runs"], ensure_ascii=False, indent=1),
    }
    return Template((TEMPLATES / "auto_tick.md").read_text(encoding="utf-8")).safe_substitute(mapping)


def stop_group(proc: subprocess.Popen) -> None:
    """TERM the agent's process group, then KILL what is left after 20 s."""
    for sig, wait in ((signal.SIGTERM, 20), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def run_agent(config: dict, name: str, project: Path, tick: str, tick_dir: Path, prompt: str,
              should_stop, beat) -> dict:
    settings = config["auto"]
    last = tick_dir / f"{name}.last.md"
    command = [
        *settings["runner"], "--skip-git-repo-check", "--cd", str(project), "--sandbox", settings["sandbox"],
        "--add-dir", str(AUTO), "--output-last-message", str(last), "--json", *settings["runner_args"], "-",
    ]
    env = {
        **os.environ, "PATH": f"{write_shims()}{os.pathsep}{os.environ.get('PATH', '')}",
        "HUB_AUTO_TICK": tick, "HUB_AUTO_PROJECT": name, "RUNOPS_HUB_DIR": str(HUB),
    }
    start = time.monotonic()
    reason = ""
    with open(tick_dir / f"{name}.events.jsonl", "w", encoding="utf-8") as out, \
            open(tick_dir / f"{name}.stderr.txt", "w", encoding="utf-8") as err:
        try:
            proc = subprocess.Popen(command, cwd=project, stdin=subprocess.PIPE, stdout=out, stderr=err,
                                    text=True, env=env, start_new_session=True)
        except OSError as exc:
            return {"exit": 127, "reason": f"could not start {command[0]}: {exc}", "minutes": 0.0, "last": ""}
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        deadline = start + float(settings["agent_timeout_min"]) * 60
        try:
            while proc.poll() is None:
                reason = should_stop() or ("timeout" if time.monotonic() > deadline else "")
                if reason:
                    stop_group(proc)
                    break
                beat(state="agent", project=name, tick=tick, agent_pid=proc.pid)
                try:
                    proc.wait(timeout=POLL)
                except subprocess.TimeoutExpired:
                    pass
        except BaseException:
            stop_group(proc)
            raise
        code = proc.wait()
    message = last.read_text(encoding="utf-8", errors="replace") if last.is_file() else ""
    return {"exit": code, "reason": reason, "minutes": round((time.monotonic() - start) / 60, 1), "last": message}


def describe_event(event: dict) -> str:
    kind = event.get("event")
    hours, cost = float(event.get("walltime_h") or 0), float(event.get("reserved_core_h") or 0)
    size = (f"{event.get('partition')} {event.get('cores')} cores × {hours:.3g} h"
            f" = {cost:,.0f} core-h" if cost >= 10 else
            f"{event.get('partition')} {event.get('cores')} cores × {hours:.3g} h = {cost:.2g} core-h")
    if kind == "submit" and event.get("kind") == "analysis":
        return f"analysis job {event.get('job_id', '(tssrun)')} ({size})"
    if kind == "submit":
        return f"submitted {event.get('run')} ({event.get('kind')}, {size}, job {event.get('job_id')}): {event.get('basis', '')}"
    if kind in ("submit-failed", "submit-unknown"):
        return f"{kind} {event.get('run')}: {event.get('error', '')}"
    if kind == "propose":
        return f"proposal #{event.get('proposal')}: {event.get('title', '')}"
    if kind == "defer":
        return f"deferred {event.get('run')}: {event.get('why', '')}"
    return json.dumps(event, ensure_ascii=False)


def git_changes(project: Path) -> dict[str, float] | None:
    """Changed and untracked paths of a git project with their mtimes; None outside a repository."""
    res = subprocess.run(["git", "--literal-pathspecs", "-C", str(project), "status", "--porcelain=v1", "-z",
                          "--untracked-files=all"], capture_output=True, check=False)
    if res.returncode != 0:
        return None
    changes: dict[str, float] = {}
    entries = iter(res.stdout.split(b"\0"))
    for entry in entries:
        if len(entry) < 4:
            continue
        code, path = entry[:2].decode(), os.fsdecode(entry[3:])
        if code[0] in "RC":
            next(entries, None)  # the original path of a rename or copy
        try:
            changes[path] = (project / path).stat().st_mtime
        except OSError:
            changes[path] = -1.0  # deleted
    return changes


def commit_agent_changes(project: Path, tick: str, before: dict[str, float] | None) -> str:
    """Commit the paths the tick's agent changed (new or changed since ``before``).

    Paths that were already changed and are untouched are left for the people working there.
    Returns one line for the report, or "" when there is nothing to do.
    """
    if before is None:
        return ""
    after = git_changes(project) or {}
    paths = sorted(p for p, mtime in after.items() if before.get(p) != mtime)
    if not paths:
        return ""
    spec = b"".join(os.fsencode(p) + b"\0" for p in paths)
    base = ["git", "--literal-pathspecs", "-C", str(project)]
    added = subprocess.run([*base, "add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul"],
                           input=spec, capture_output=True, check=False)
    message = f"[auto] {tick} の記録\n\nhub auto の agent が書いた LOG・note・解析の file と run の状態。\n"
    done = added.returncode == 0 and subprocess.run(
        [*base, "commit", "-q", "-m", message, "--pathspec-from-file=-", "--pathspec-file-nul"],
        input=spec, capture_output=True, check=False)
    if not done or done.returncode != 0:
        why = (added if added.returncode != 0 else done).stderr.decode(errors="replace").strip().splitlines()
        return f"git commit failed ({len(paths)} files left uncommitted): {why[-1] if why else 'unknown error'}"
    head = subprocess.run([*base, "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False)
    return f"git commit {head.stdout.strip()} ({len(paths)} files)"


def tick_report(tick: str, started: datetime, ended: datetime, works: list[dict], notes: list[str],
                results: list[dict], stop_reason: str) -> str:
    minutes = (ended - started).total_seconds() / 60
    head = f"## {started:%Y-%m-%d %H:%M} {tick} ({HOST}, {minutes:.0f} min)"
    lines = [head + (f" — stopped: {stop_reason}" if stop_reason else ""), ""]
    lines += [f"- {note}" for note in notes]
    for work in works:
        parts = []
        if work["runs"]:
            parts.append(f"{len(work['runs'])} runs for the agent ({', '.join(r['id'] for r in work['runs'])})")
        if work["proposals"]:
            parts.append(f"accepted proposals {', '.join('#' + str(p['id']) for p in work['proposals'])}")
        if work["skipped"]:
            parts.append(f"{len(work['skipped'])} left ({'; '.join(s['id'] + ': ' + s['reason'] for s in work['skipped'][:4])}"
                         + (", ..." if len(work["skipped"]) > 4 else "") + ")")
        if work["stalled"]:
            parts.append("STALLED " + ", ".join(f"{s['id']} {s['stalled_hours']:.1f} h" for s in work["stalled"]))
        if parts:
            lines.append(f"- {work['project']}: " + "; ".join(parts))
    for result in results:
        status = f"agent exit {result.get('exit')}" + (f" ({result['reason']})" if result.get("reason") else "")
        lines += ["", f"### {result['project']} — {status}, {result.get('minutes', 0):g} min", ""]
        lines += [f"- {describe_event(e)}" for e in result.get("events", [])]
        if result.get("commit"):
            lines.append(f"- {result['commit']}")
        if result.get("events"):
            lines.append("")
        lines.append(result.get("last", "").strip() or "(no final message)")
    return "\n".join(lines).rstrip() + "\n"


def append_report(started: datetime, text: str) -> Path:
    path = AUTO / "reports" / f"{started:%Y-%m}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    with Locked("reports.lock"):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(("\n" if path.stat().st_size else "") + text)
    return path


def prune_ticks(config: dict) -> None:
    keep = now() - timedelta(days=float(config["auto"]["keep_tick_days"]))
    for path in (AUTO / "ticks").glob("T*"):
        try:
            when = datetime.strptime(path.name, "T%Y%m%d-%H%M%S").astimezone()
        except ValueError:
            continue
        if when < keep:
            shutil.rmtree(path, ignore_errors=True)


def print_worklist(config: dict, works: list[dict], notes: list[str]) -> None:
    for note in notes:
        print(f"!! {note}")
    for work in works:
        settings = project_settings(config, work["project"]) or {}
        flags = ", ".join(k for k in ("analyze", "continue", "derive") if settings.get(k)) or "nothing"
        start = "an agent would start" if work["runs"] or work["proposals"] else "no agent"
        print(f"===== {work['project']} ({flags}): {start}")
        for run in work["runs"]:
            print(f"  {run['id']}  {run['status']:<9} finished {hm(run['finished_at'])}  {run['label']}")
        for item in work["skipped"]:
            print(f"  -- {item['id']}: {item['reason']}")
        for item in work["stalled"]:
            print(f"  STALLED {item['id']} {item['label']}: {item['stalled_hours']:.1f} h")
        for item in work["proposals"]:
            print(f"  accepted proposal #{item['id']}: {item.get('title')}")
    print("\nbudget (what the agent would be told):")
    print(budget_text(config, works[0]["project"] if works else ""))


def run_tick(config: dict, *, dry_run: bool = False, only: str = "", agents: bool = True, sync: bool = True,
             should_stop=None, beat=None, respect_control: bool = False, say=print) -> dict:
    handle = None
    for _ in range(6):  # ./hub auto status holds the lock for an instant to see whether a tick runs
        handle = try_lock("tick.lock")
        if handle is not None:
            break
        time.sleep(0.5)
    if handle is None:
        say("another tick is running (auto/tick.lock); nothing done")
        return {"result": "busy"}
    try:
        return _tick(config, dry_run, only, agents, sync, should_stop or (lambda: ""),
                     beat or (lambda **_: None), respect_control, say)
    finally:
        unlock(handle)


def _tick(config, dry_run, only, agents, sync, should_stop, beat, respect_control, say) -> dict:
    started = now()
    tick = f"T{started:%Y%m%d-%H%M%S}"
    tick_dir = AUTO / "ticks" / tick
    if not dry_run:
        loop_beat = beat

        def beat(**fields) -> None:  # noqa: F811  (also shows a tick run by hand in ./hub auto status)
            write_json(AUTO / "current_tick.json", {"tick": tick, "host": HOST, "pid": os.getpid(),
                                                    "started": stamp(started), "at": stamp(), **fields})
            loop_beat(**fields)

        try:
            return _tick_body(config, dry_run, only, agents, sync, should_stop, beat, respect_control, say,
                              started, tick, tick_dir)
        finally:
            (AUTO / "current_tick.json").unlink(missing_ok=True)
    return _tick_body(config, dry_run, only, agents, sync, should_stop, beat, respect_control, say,
                      started, tick, tick_dir)


def _tick_body(config, dry_run, only, agents, sync, should_stop, beat, respect_control, say,
               started, tick, tick_dir) -> dict:
    names = enabled(config)
    if only:
        if only not in names:
            raise AutoError(f"{only} is not enabled (./hub auto enable {only})")
        names = [only]
    found = members()
    events = ledger()
    notes: list[str] = []
    works: list[dict] = []
    for name in names:
        path = found.get(name)
        if path is None:
            notes.append(f"{name}: not a member project; skipped")
            continue
        hold = held(name)
        if hold:
            notes.append(hold)
            continue
        beat(state="sync", project=name, tick=tick)
        if sync:
            try:
                res = runo(path, "sync", timeout=900)
                if res.returncode != 0:
                    notes.append(f"{name}: runo sync failed (exit {res.returncode}): {res.stderr.strip()[-200:]}")
            except (OSError, subprocess.TimeoutExpired) as exc:
                notes.append(f"{name}: runo sync: {exc}")
        try:
            runs = list_runs(path)
        except AutoError as exc:
            notes.append(f"{name}: {exc}")
            continue
        if not dry_run:
            notes += reconcile(name, runs, events)
            notes += cancel_duplicates(config, name, path, runs, events)
        works.append(worklist(config, name, path, runs, ledger() if not dry_run else events, tick))
    if dry_run:
        print_worklist(config, works, notes)
        return {"result": "dry-run", "tick": tick, "works": works}
    busy = [w for w in works if w["runs"] or w["proposals"]]
    if busy or notes:
        tick_dir.mkdir(parents=True, exist_ok=True)
        write_json(tick_dir / "worklist.json", {"tick": tick, "host": HOST, "notes": notes, "works": works})
    if busy:
        allocation(fresh=True)
    results: list[dict] = []
    stop_reason = ""
    try:
        for work in busy:
            want = control()["want"]
            stop_reason = should_stop() or (f"hub auto is {want}" if respect_control and want != "run" else "")
            if stop_reason:
                break
            name, path = work["project"], Path(work["dir"])
            ids = [r["id"] for r in work["runs"]]
            for rid in ids:
                claim(name, rid, tick, float(config["auto"]["claim_hours"]), "hub auto tick")
            ledger_add(event="work", tick=tick, project=name, runs=ids,
                       proposals=[p["id"] for p in work["proposals"]])
            prompt = build_prompt(config, work, tick)
            (tick_dir / f"{name}.prompt.md").write_text(prompt, encoding="utf-8")
            result: dict = {"project": name, "runs": ids}
            if agents:
                say(f"agent in {name} for {len(ids)} runs")
                beat(state="agent", project=name, tick=tick)
                before = git_changes(path)
                result.update(run_agent(config, name, path, tick, tick_dir, prompt, should_stop, beat))
                say(f"agent in {name} ended: exit {result['exit']} {result['reason']} ({result['minutes']} min)")
                result["commit"] = commit_agent_changes(path, tick, before)
                if result["commit"]:
                    say(f"{name}: {result['commit']}")
            else:
                result.update(exit=None, reason="no agent (--no-agent)", minutes=0, last="")
            release(name, holder=tick)
            result["events"] = [e for e in ledger() if e.get("tick") == tick and e.get("project") == name
                                and e.get("event") not in ("work", "planned")]
            results.append(result)
            if result.get("reason") and result["reason"] != "timeout":
                stop_reason = result["reason"]
                break
    finally:
        for work in busy:
            release(work["project"], holder=tick)
    ended = now()
    stopped = ("pause --now", "stop --now", "signal")  # stopped on request: not a failure of the agent
    failures = sum(1 for r in results if r.get("reason") == "timeout"
                   or (r.get("exit") not in (0, None) and r.get("reason") not in stopped))
    last = {"tick": tick, "host": HOST, "started": stamp(started), "ended": stamp(ended),
            "result": "interrupted" if stop_reason else "ok", "agents": len(results), "failures": failures}
    if busy or notes:
        report = tick_report(tick, started, ended, works, notes, results, stop_reason)
        (tick_dir / "report.md").write_text(report, encoding="utf-8")
        last["report"] = str(append_report(started, report))
    write_json(AUTO / "last_tick.json", last)
    prune_ticks(config)
    return last


# ---------------------------------------------------------------- the loop

def next_due(config: dict) -> datetime:
    last = read_json(AUTO / "last_tick.json", {})
    started = parse_time(last.get("started", "")) if isinstance(last, dict) else None
    if started is None or last.get("result") == "interrupted":
        return now()
    return started + timedelta(minutes=float(config["auto"]["interval_min"]))


def stopper(flag: dict):
    def check() -> str:
        if flag.get("signal"):
            return "signal"
        wanted = control()
        if wanted["want"] != "run" and wanted.get("now"):
            return "pause --now" if wanted["want"] == "paused" else "stop --now"
        return ""
    return check


def loop() -> int:
    handle = None
    for _ in range(6):  # a status check may hold the lock for an instant
        handle = try_lock("loop.lock")
        if handle is not None:
            break
        time.sleep(0.5)
    if handle is None:
        beat = heartbeat()
        log(f"another loop holds auto/loop.lock ({beat.get('host', '?')} pid {beat.get('pid', '?')}); exiting")
        return 1
    flag: dict = {}

    def on_signal(signum, _frame) -> None:
        flag["signal"] = signum

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, on_signal)
    started = stamp()
    session = TMUX_SESSION if os.environ.get("TMUX") else "foreground"

    def beat(**fields) -> None:
        write_json(AUTO / "heartbeat.json", {"host": HOST, "pid": os.getpid(), "session": session,
                                             "started": started, "at": stamp(), **fields})

    log(f"loop started on {HOST} pid {os.getpid()} ({session})")
    failures = 0
    idle_until = now()
    try:
        while not flag.get("signal"):
            try:
                config = load_config()
            except (OSError, tomllib.TOMLDecodeError) as exc:
                beat(state="config-error", error=str(exc))
                log(f"auto/config.toml: {exc}")
                time.sleep(POLL)
                continue
            wanted = control()
            if wanted["want"] == "stopped":
                break
            if wanted["want"] == "paused":
                beat(state="paused", since=wanted.get("at"), by=wanted.get("by"))
                time.sleep(POLL)
                continue
            due = max(next_due(config), idle_until)
            if not wanted.get("kick") and now() < due:
                beat(state="waiting", next_tick=stamp(due))
                time.sleep(POLL)
                continue
            if wanted.get("kick"):
                set_control(kick=False)
            log("tick")
            try:
                result = run_tick(config, should_stop=stopper(flag), beat=beat, respect_control=True, say=log)
            except Exception as exc:  # a broken tick must not end the loop
                log(f"tick failed: {exc!r}\n{traceback.format_exc()}")
                result = {"result": "error", "agents": 1, "failures": 1}
                write_json(AUTO / "last_tick.json", {"tick": "-", "host": HOST, "started": stamp(),
                                                     "ended": stamp(), "result": "error", "error": repr(exc)})
            if result.get("result") == "busy":
                idle_until = now() + timedelta(minutes=5)
                continue
            log(f"tick {result.get('tick')}: {result.get('result')}, {result.get('agents', 0)} agents, "
                f"{result.get('failures', 0)} failed")
            if result.get("agents"):
                failures = failures + 1 if result.get("failures") == result.get("agents") else 0
            if failures >= int(config["auto"]["pause_after_failures"]):
                set_control(want="paused", now=False,
                            reason=f"{failures} ticks in a row where every agent failed; see auto/ticks/{result.get('tick')}")
                log("paused: agents keep failing")
                failures = 0
    finally:
        beat(state="stopped", signal=flag.get("signal"))
        log("loop stopped")
        unlock(handle)
    return 0


def start_loop() -> str:
    """Start the loop in a detached tmux session on this node; returns what happened."""
    if shutil.which("tmux") is None:
        raise AutoError("tmux is not on PATH; run ./hub auto start --foreground under nohup instead")
    subprocess.run(["tmux", "kill-session", "-t", f"={TMUX_SESSION}"], capture_output=True)
    command = " ".join(shlex.quote(x) for x in [
        "env", f"PATH={os.environ.get('PATH', '')}", f"RUNOPS_HUB_DIR={HUB}",
        sys.executable, str(Path(__file__).resolve()), "loop",
    ]) + f" >> {shlex.quote(str(AUTO / 'loop.log'))} 2>&1"
    res = subprocess.run(["tmux", "new-session", "-d", "-s", TMUX_SESSION, "-c", str(HUB), command],
                         capture_output=True, text=True)
    if res.returncode != 0:
        raise AutoError(f"tmux new-session failed: {res.stderr.strip()}")
    for _ in range(40):
        time.sleep(0.5)
        beat = heartbeat()
        if loop_alive() and beat.get("host") == HOST and beat.get("state") != "stopped":
            return f"started the loop in tmux session {TMUX_SESSION} on {HOST} (pid {beat.get('pid')})"
    raise AutoError(f"the loop did not come up; see {AUTO / 'loop.log'}")


# ---------------------------------------------------------------- commands for people

def cmd_status(_args) -> int:
    if not AUTO.exists():
        print("hub auto is not set up here; choose a project with ./hub auto enable NAME")
        return 0
    config = load_config()
    print(describe_loop())
    wanted = control()
    print(f"  control: {wanted['want']}{' (now)' if wanted.get('now') else ''}"
          f"{' + kick' if wanted.get('kick') else ''} since {hm(wanted.get('at'))} by {wanted.get('by', '-')}"
          + (f" — {wanted['reason']}" if wanted.get("reason") else ""))
    last = read_json(AUTO / "last_tick.json", {})
    if last:
        print(f"  last tick: {last.get('tick')} {hm(last.get('started'))}–{hm(last.get('ended'))[6:]} on "
              f"{last.get('host')}: {last.get('result')}, {last.get('agents', 0)} agents, {last.get('failures', 0)} failed")
    names = enabled(config)
    print("  projects: " + (", ".join(
        f"{n} ({', '.join(k for k in ('analyze', 'continue', 'derive') if project_settings(config, n)[k]) or 'sync only'})"
        for n in names) or "none (./hub auto enable NAME)"))
    spent = usage(config)
    budget = config["budget"]
    print(f"budget, last {budget['window_days']:g} days (core-h): total {spent['total']:,.0f}/{budget['total']:,}, "
          f"derived {spent['derived']:,.0f}/{budget['derived_total']:,}, "
          + ", ".join(f"{p} {spent['partition'].get(p, 0):,.0f}/{c:,}" for p, c in budget["partition"].items())
          + f"; cores now {spent['active_cores']}/{budget['max_cores']}"
          + (f"; approved by you {spent['approved']:,.0f}" if spent["approved"] else ""))
    left = allocation(fresh=False)
    if left:
        print(f"  show_my_time ({hm(left['at'])}): {left['remaining']:,.0f} of {left['allocated']:,.0f} days left "
              f"({left['remaining'] / left['allocated']:.0%}; auto stops below {budget['floor_fraction']:.0%})")
    for event in spent["active"]:
        print(f"  running: {event['project']}/{event['run']} {event.get('kind')} job {event.get('job_id')} "
              f"{event.get('cores')} cores × {float(event.get('walltime_h') or 0):g} h")
    for item in claims():
        print(f"claim: {item['project']}/{item['run']} by {item['holder']} until {hm(item['until'])}")
    for key, item in holds().items():
        print(f"hold: {key} since {hm(item.get('at'))}: {item.get('why') or '-'}")
    events = ledger()
    for name in sorted({e.get("project", "") for e in events if e.get("event") == "defer"}):
        for rid, event in deferred(events, name).items():
            print(f"question: {name}/{rid} ({event.get('tick')}): {event.get('why')}")
    for item in proposals():
        if item.get("status") in ("open", "accepted"):
            print(f"proposal #{item['id']} [{item['status']}] {item['project']} {item.get('kind')} "
                  f"~{float(item.get('core_h') or 0):,.0f} core-h: {item.get('title')}")
    return 0


def summary_lines() -> list[str]:
    """A few lines for ./hub status."""
    if not (AUTO / "config.toml").exists():
        return []
    config = load_config()
    spent = usage(config)
    lines = [describe_loop().replace("loop:", "hub auto:", 1)]
    aside = sorted(n for n in shelved_projects(HUB) if isinstance(config["projects"].get(n), dict))
    lines.append(f"  week: {spent['total']:,.0f}/{config['budget']['total']:,} core-h, "
                 f"cores now {spent['active_cores']}/{config['budget']['max_cores']}; "
                 f"projects: {', '.join(enabled(config)) or 'none'}"
                 + (f" (set aside, skipped: {', '.join(aside)})" if aside else ""))
    for item in claims():
        lines.append(f"  claimed (leave alone): {item['project']}/{item['run']} by {item['holder']} until {hm(item['until'])}")
    events = ledger()
    questions = sum(len(deferred(events, n)) for n in {e.get("project", "") for e in events})
    waiting = [p for p in proposals() if p.get("status") == "open"]
    if questions or waiting:
        lines.append(f"  waiting for you: {questions} questions, {len(waiting)} proposals (./hub auto status)")
    return lines


def cmd_start(args) -> int:
    ensure_config()
    set_control(want="run", now=False, kick=False)
    state, beat = loop_state()
    if state != "stopped":
        print(f"the loop is already {state} on {beat.get('host')} (pid {beat.get('pid')}); control set to run")
        return 0
    if args.foreground:
        return loop()
    print(start_loop())
    if not enabled(load_config()):
        print("no project is enabled yet: ./hub auto enable NAME")
    return 0


def cmd_pause(args) -> int:
    set_control(want="paused", now=args.now)
    state, beat = loop_state()
    if state == "stopped":
        print("paused (no loop is running)")
    elif beat.get("state") == "agent" and not args.now:
        print(f"paused: the agent in {beat.get('project')} finishes first, then the loop waits "
              "(./hub auto pause --now stops it too)")
    else:
        print(f"paused: the loop on {beat.get('host')} waits within {POLL:g} s"
              + (", stopping the running agent" if args.now and beat.get("state") == "agent" else ""))
    return 0


def cmd_resume(_args) -> int:
    set_control(want="run", now=False)
    state, beat = loop_state()
    if state == "running":
        print(f"resumed: the loop on {beat.get('host')} goes on")
    elif state == "hung":
        print(describe_loop())
        return 1
    else:
        print(start_loop())
    return 0


def cmd_stop(args) -> int:
    set_control(want="stopped", now=args.now)
    state, beat = loop_state()
    if state == "stopped":
        print("no loop is running; control set to stopped")
        return 0
    if args.force:
        if beat.get("host") != HOST:
            print(f"the loop runs on {beat.get('host')}; from there: ./hub auto stop --force "
                  f"(e.g. ssh {beat.get('host')} 'cd {HUB} && ./hub auto stop --force')")
            return 1
        # TERM first; a loop that does not react (hung, or stopped with SIGSTOP) gets KILL after 15 s.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            targets = [(os.kill, beat.get("pid"))]
            if beat.get("state") == "agent":
                targets.append((os.killpg, beat.get("agent_pid")))  # the agent has its own process group
            for kill, pid in targets:
                try:
                    kill(int(pid), sig)
                except (ProcessLookupError, PermissionError, TypeError, ValueError):
                    pass
            for _ in range(30):
                if not loop_alive():
                    break
                time.sleep(0.5)
            if not loop_alive():
                break
        subprocess.run(["tmux", "kill-session", "-t", f"={TMUX_SESSION}"], capture_output=True)
        print(f"{'stopped' if not loop_alive() else '!! could not stop'} the loop (pid {beat.get('pid')}) on {HOST}")
        return 0 if not loop_alive() else 1
    if beat.get("state") == "agent" and not args.now:
        print(f"stopping after the agent in {beat.get('project')} finishes (--now stops it too)")
    else:
        print(f"the loop on {beat.get('host')} stops within {POLL:g} s")
    return 0


def cmd_kick(_args) -> int:
    if control()["want"] != "run":
        print(f"hub auto is {control()['want']}; ./hub auto resume first")
        return 1
    set_control(kick=True)
    state, _ = loop_state()
    print("the loop runs a tick within a few seconds" if state == "running"
          else "no loop is running; ./hub auto tick runs one in this terminal")
    return 0


def cmd_tick(args) -> int:
    ensure_config()
    config = load_config()
    if not enabled(config):
        print("no project is enabled: ./hub auto enable NAME")
        return 1
    result = run_tick(config, dry_run=args.dry_run, only=args.project or "", agents=not args.no_agent,
                      sync=not args.no_sync, should_stop=stopper({}))
    if args.dry_run or result.get("result") == "busy":
        return 0
    print(f"tick {result.get('tick')}: {result.get('result')}, {result.get('agents', 0)} agents, "
          f"{result.get('failures', 0)} failed" + (f"; report in {result['report']}" if result.get("report") else
                                                   "; nothing to do"))
    return 0


def add_member_note(path: Path) -> bool:
    """Put the hub auto note into the member's ``## runops hub`` section (once)."""
    agents = path / "AGENTS.md"
    text = agents.read_text(encoding="utf-8") if agents.is_file() else ""
    if "hub auto" in text:
        return False
    note = MEMBER_NOTE.format(hub=HUB)
    lines = text.splitlines()
    if "## runops hub" in lines:
        start = lines.index("## runops hub")
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        lines[end:end] = note.splitlines()
        agents.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        agents.write_text(text.rstrip("\n") + ("\n\n" if text else "") + "## runops hub\n\n" + note + "\n",
                          encoding="utf-8")
    return True


def cmd_enable(args) -> int:
    path = member(args.name)
    table = {"analyze": not args.no_analyze, "continue": not args.no_continue, "derive": not args.no_derive}
    if args.total:
        table["total"] = args.total
    set_project_table(args.name, table)
    print(f"enabled {args.name}: " + ", ".join(f"{k}={'on' if v else 'off'}" if isinstance(v, bool) else f"{k}={v}"
                                               for k, v in table.items()))
    if add_member_note(path):
        print(f"added the hub auto note to {path / 'AGENTS.md'} (## runops hub)")
    return 0


def cmd_disable(args) -> int:
    set_project_table(args.name, None)
    print(f"disabled {args.name}; its runs, claims and ledger entries stay as they are")
    return 0


def cmd_hold(args, on: bool) -> int:
    project, run = split_target(args.target)
    member(project)
    key = f"{project}/{run}" if run else project
    with Locked("holds.lock"):
        found = holds()
        if on:
            found[key] = {"at": stamp(), "why": args.why, "by": f"{os.environ.get('USER', '?')}@{HOST}"}
        elif found.pop(key, None) is None:
            print(f"{key} was not on hold")
            return 1
        write_json(AUTO / "holds.json", found)
    print(f"{'holding' if on else 'released hold on'} {key}")
    return 0


def cmd_claim(args) -> int:
    project, run = split_target(args.target)
    if not run:
        raise AutoError("claim takes PROJECT/RUN")
    member(project)
    claim(project, run, f"{os.environ.get('USER', '?')}@{HOST}", args.hours, args.why)
    print(f"claimed {project}/{run} for {args.hours:g} h")
    return 0


def cmd_release(args) -> int:
    if args.all:
        count = release()
        print(f"released {count} claims")
        return 0
    if not args.target:
        raise AutoError("release takes PROJECT/RUN, PROJECT or --all")
    project, run = split_target(args.target)
    count = release(project, run)
    ledger_add(event="release", project=project, run=run, by=f"{os.environ.get('USER', '?')}@{HOST}")
    print(f"released {count} claims on {project}{'/' + run if run else ''}; auto may take it up again")
    return 0


def cmd_proposals(args) -> int:
    shown = 0
    for item in proposals():
        if not args.all and item.get("status") not in ("open", "accepted"):
            continue
        shown += 1
        print(f"#{item['id']} [{item.get('status')}] {item.get('project')} {item.get('kind')} "
              f"~{float(item.get('core_h') or 0):,.0f} core-h ({hm(item.get('at'))}, {item.get('tick', '-')})")
        print(f"   {item.get('title')}")
        print(f"   {item.get('why')}")
        if item.get("runs"):
            print(f"   runs: {', '.join(item['runs'])}")
    if not shown:
        print("no proposals waiting" if not args.all else "no proposals")
    return 0


def confirm(question: str, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        raise AutoError(f"{question} — answer with --yes (no terminal to ask in)")
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def cmd_approve(args) -> int:
    item = proposal(args.number)
    if item.get("status") != "open":
        raise AutoError(f"proposal #{args.number} is {item.get('status')}")
    path = member(item["project"])
    item["decided_at"] = stamp()
    if not item.get("runs"):
        item["status"] = "accepted"
        save_proposal(item)
        print(f"accepted #{args.number}: no runs are attached, so the next tick in {item['project']} creates them "
              f"and submits up to 1.2 × {float(item.get('core_h') or 0):,.0f} core-h")
        return 0
    by_id = {r["id"]: r for r in list_runs(path)}
    total = 0.0
    for rid in item["runs"]:
        run = by_id.get(rid)
        if run is None or run.get("status") != "created":
            raise AutoError(f"{rid} is {run.get('status') if run else 'gone'}; nothing submitted")
        job = manifest(path, run.get("run_dir", "")).get("job", {})
        total += reserved(job)
        print(f"  {rid} {run.get('label', '')}: {job.get('partition')} {job_cores(job)} cores × "
              f"{walltime_hours(job.get('walltime', '')):g} h = {reserved(job):,.0f} core-h")
    if not confirm(f"submit {len(item['runs'])} runs of #{args.number} ({total:,.0f} core-h)?", args.yes):
        print("nothing submitted")
        return 1
    plans = []
    for rid in item["runs"]:
        run = by_id[rid]
        job = manifest(path, run.get("run_dir", "")).get("job", {})
        plans.append({"project": item["project"], "run": rid, "run_dir": run.get("run_dir", ""), "kind": "approved",
                      "partition": job.get("partition", ""), "cores": job_cores(job),
                      "walltime_h": round(walltime_hours(job.get("walltime", "")), 3),
                      "reserved_core_h": round(reserved(job), 1), "parent": run.get("parent_run", "")})
    events = submit_plans(item["project"], path, plans, tick="approve", basis=f"proposal #{args.number} approved",
                          number=args.number)
    item["status"] = "approved"
    item["jobs"] = {e["run"]: e.get("job_id", "") for e in events}
    save_proposal(item)
    for event in events:
        print(f"  {describe_event(event)}")
    return 0 if all(e.get("event") == "submit" for e in events) else 1


def cmd_reject(args) -> int:
    item = proposal(args.number)
    if item.get("status") not in ("open", "accepted"):
        raise AutoError(f"proposal #{args.number} is {item.get('status')}")
    item.update(status="rejected", decided_at=stamp())
    save_proposal(item)
    print(f"rejected #{args.number}")
    if item.get("runs") and not args.keep_runs:
        path = member(item["project"])
        by_id = {r["id"]: r for r in list_runs(path)}
        doomed = [rid for rid in item["runs"] if by_id.get(rid, {}).get("status") == "created"
                  and manifest(path, by_id[rid].get("run_dir", "")).get("origin", {}).get("created_by") == CREATOR]
        if doomed:
            res = runo(path, "delete", *doomed, "-y")
            print(f"deleted the created runs {', '.join(doomed)}" if res.returncode == 0
                  else f"!! runo delete failed: {res.stderr.strip()[-300:]}")
    return 0


def cmd_cancel(args) -> int:
    config = load_config()
    active = usage(config)["active"]
    if not active:
        print("no simulation auto submitted is waiting or running")
        return 0
    for event in active:
        print(f"  {event['project']}/{event['run']} {event.get('kind')} job {event.get('job_id')}")
    if not confirm(f"cancel these {len(active)} runs?", args.yes):
        return 1
    found = members()
    for event in active:
        res = runo(found[event["project"]], "cancel", event["run"], "--why", "hub auto cancel (by the person)", "-y",
                   env=clean_env())
        ledger_add(event="cancel", project=event["project"], run=event["run"], why="./hub auto cancel",
                   ok=res.returncode == 0)
        print(f"  {'cancelled' if res.returncode == 0 else '!! failed to cancel'} {event['project']}/{event['run']}")
    return 0


def cmd_report(args) -> int:
    files = sorted((AUTO / "reports").glob("*.md"))
    sections: list[str] = []
    for path in files[-2:]:
        text = path.read_text(encoding="utf-8")
        sections += ["## " + s for s in re.split(r"(?m)^## ", text) if s.strip()]
    if not sections:
        print("no tick has reported yet")
        return 0
    print("\n".join(s.rstrip() + "\n" for s in sections[-args.n:]))
    return 0


def cmd_log(args) -> int:
    path = AUTO / "loop.log"
    if not path.exists():
        print("no loop log yet")
        return 0
    os.execvp("tail", ["tail", "-n", str(args.n), *(["-f"] if args.follow else []), str(path)])
    return 0


# ---------------------------------------------------------------- commands for the agent

def cmd_submit(args) -> int:
    name, path = here(args.project)
    tick = os.environ.get("HUB_AUTO_TICK", "")
    if not tick and not args.check:
        raise AutoError("hub auto submit is the door for the tick's agent; people submit with runo submit "
                        "or ./hub auto approve N (add --check to only check)")
    config = load_config()
    with Locked("budget.lock"):
        plans, problems = gate(config, name, path, args.runs, args.kind, args.proposal, holder=tick)
        for plan in plans:
            print(f"  {plan['run']} {plan['kind']}: {plan['partition']} {plan['cores']} cores × "
                  f"{plan['walltime_h']:g} h = {plan['reserved_core_h']:,.0f} core-h")
        if problems:
            print("refused, nothing submitted:\n  - " + "\n  - ".join(problems))
            return 3
        if args.check:
            print("the check passes; nothing submitted (--check)")
            return 0
        events = submit_plans(name, path, plans, tick, args.basis, args.proposal)
    if args.kind == "approved" and args.proposal is not None:
        item = proposal(args.proposal)
        item["status"] = "approved"
        item["jobs"] = {e["run"]: e.get("job_id", "") for e in events}
        save_proposal(item)
    for event in events:
        print(f"  {describe_event(event)}")
    return 0 if all(e.get("event") == "submit" for e in events) else 1


def cmd_propose(args) -> int:
    name, path = here(args.project)
    cost = args.core_h
    if args.run:
        by_id = {r["id"]: r for r in list_runs(path)}
        total = 0.0
        for rid in args.run:
            run = by_id.get(rid)
            if run is None or run.get("status") != "created":
                raise AutoError(f"{rid}: attach only created runs (it is {run.get('status') if run else 'missing'})")
            data = manifest(path, run.get("run_dir", ""))
            if data.get("origin", {}).get("created_by") != CREATOR:
                raise AutoError(f"{rid} was not created by hub auto (runo new --created-by {CREATOR})")
            total += reserved(data.get("job", {}))
        cost = cost or round(total, 1)
    elif not cost:
        raise AutoError("without --run, give --core-h with an estimate")
    item = add_proposal(project=name, tick=os.environ.get("HUB_AUTO_TICK", ""), kind=args.kind,
                        title=args.title, why=args.why, runs=args.run or [], core_h=cost)
    ledger_add(event="propose", tick=item["tick"], project=name, proposal=item["id"], title=args.title,
               runs=item["runs"], core_h=cost)
    print(f"proposal #{item['id']} written ({cost:,.0f} core-h); the person approves it with ./hub auto approve {item['id']}")
    return 0


def cmd_defer(args) -> int:
    name, _ = here(args.project)
    for rid in args.runs:
        ledger_add(event="defer", tick=os.environ.get("HUB_AUTO_TICK", ""), project=name, run=rid, why=args.why)
    print(f"deferred {', '.join(args.runs)}; auto leaves them until ./hub auto release {name}/RUN")
    return 0


# ---------------------------------------------------------------- main

def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="./hub auto", add_help=False)
    sub = top.add_subparsers(dest="command")
    sub.add_parser("status")
    sub.add_parser("help")
    p = sub.add_parser("start")
    p.add_argument("--foreground", action="store_true")
    sub.add_parser("loop")
    p = sub.add_parser("pause")
    p.add_argument("--now", action="store_true")
    sub.add_parser("resume")
    p = sub.add_parser("stop")
    p.add_argument("--now", action="store_true")
    p.add_argument("--force", action="store_true")
    sub.add_parser("kick")
    p = sub.add_parser("tick")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--project", default="")
    p.add_argument("--no-agent", action="store_true")
    p.add_argument("--no-sync", action="store_true")
    p = sub.add_parser("enable")
    p.add_argument("name")
    p.add_argument("--no-analyze", action="store_true")
    p.add_argument("--no-continue", action="store_true")
    p.add_argument("--no-derive", action="store_true")
    p.add_argument("--total", type=float, default=0)
    p = sub.add_parser("disable")
    p.add_argument("name")
    for name in ("hold", "unhold"):
        p = sub.add_parser(name)
        p.add_argument("target")
        p.add_argument("--why", default="")
    p = sub.add_parser("claim")
    p.add_argument("target")
    p.add_argument("--hours", type=float, default=24)
    p.add_argument("--why", default="")
    p = sub.add_parser("release")
    p.add_argument("target", nargs="?", default="")
    p.add_argument("--all", action="store_true")
    p = sub.add_parser("proposals")
    p.add_argument("--all", action="store_true")
    p = sub.add_parser("approve")
    p.add_argument("number", type=int)
    p.add_argument("--yes", "-y", action="store_true")
    p = sub.add_parser("reject")
    p.add_argument("number", type=int)
    p.add_argument("--keep-runs", action="store_true")
    p = sub.add_parser("cancel")
    p.add_argument("--yes", "-y", action="store_true")
    p = sub.add_parser("report")
    p.add_argument("-n", type=int, default=1)
    p = sub.add_parser("log")
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("-n", type=int, default=40)
    p = sub.add_parser("submit")
    p.add_argument("runs", nargs="+")
    p.add_argument("--kind", required=True, choices=["continue", "derive", "approved"])
    p.add_argument("--basis", default="")
    p.add_argument("--proposal", type=int)
    p.add_argument("--project", default="")
    p.add_argument("--check", action="store_true")
    p = sub.add_parser("propose")
    p.add_argument("--kind", required=True, choices=["continue", "derive", "other"])
    p.add_argument("--title", required=True)
    p.add_argument("--why", required=True)
    p.add_argument("--run", action="append", default=[])
    p.add_argument("--core-h", type=float, default=0)
    p.add_argument("--project", default="")
    p = sub.add_parser("defer")
    p.add_argument("runs", nargs="+")
    p.add_argument("--why", required=True)
    p.add_argument("--project", default="")
    return top


def main(argv: list[str]) -> int:
    if argv[:1] == ["shim"]:
        return shim(argv[1], argv[2:])
    if not argv:
        argv = ["status"]
    if argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    try:
        args = parser().parse_args(argv)
    except SystemExit as exc:
        print(USAGE, file=sys.stderr)
        return int(exc.code or 2)
    commands = {
        "status": cmd_status, "start": cmd_start, "loop": lambda _: loop(), "pause": cmd_pause,
        "resume": cmd_resume, "stop": cmd_stop, "kick": cmd_kick, "tick": cmd_tick, "enable": cmd_enable,
        "disable": cmd_disable, "hold": lambda a: cmd_hold(a, True), "unhold": lambda a: cmd_hold(a, False),
        "claim": cmd_claim, "release": cmd_release, "proposals": cmd_proposals, "approve": cmd_approve,
        "reject": cmd_reject, "cancel": cmd_cancel, "report": cmd_report, "log": cmd_log,
        "submit": cmd_submit, "propose": cmd_propose, "defer": cmd_defer,
    }
    try:
        return commands[args.command](args)
    except AutoError as exc:
        print(f"hub auto: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
