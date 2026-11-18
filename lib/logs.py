"""Find job logs across the workspace or collect loose logs in a selected directory."""
import argparse
import fcntl
import os
from pathlib import Path
import re
import subprocess
import sys


JOB_LOG = re.compile(r".+[-.](?P<job>[0-9]+(?:_[0-9]+)?)\.(?:out|err|log)")
FINISHED = {"BOOT_FAIL", "CANCELLED", "COMPLETED", "DEADLINE", "FAILED",
            "NODE_FAIL", "OUT_OF_MEMORY", "TIMEOUT"}


def finished_jobs(job_ids):
    finished = set()
    unconfirmed = set()
    for start in range(0, len(job_ids), 200):
        ids = ",".join(job_ids[start:start + 200])
        result = subprocess.run(
            ["sacct", "-X", "-n", "-P", "-j", ids, "-o", "JobID%64,State%40"],
            check=True, stdout=subprocess.PIPE, text=True,
        )
        for line in result.stdout.splitlines():
            job_id, _, state = line.partition("|")
            if state.partition(" ")[0] in FINISHED:
                finished.add(job_id)
            else:
                unconfirmed.add(job_id)
    return finished - unconfirmed


def project_roots(hub):
    roots = [p for p in hub.iterdir() if (p / "runops.toml").is_file()]
    papers = hub / "papers"
    if papers.is_dir():
        roots.extend(p for p in papers.iterdir() if (p / "paper.toml").is_file())
    return sorted(roots)


def inventory(hub, roots, all_dirs):
    # Enter each member's registered symlink once. Never follow links within a project.
    scans = [hub, *roots]
    internal = hub / ".hub"
    if internal.is_dir():
        scans.append(internal)
    total_jobs = total_other = loose = managed = 0
    print("job logs  other .log  location")
    for root in scans:
        run_work = set()
        for dirname, dirs, names in os.walk(root):
            directory = Path(dirname)
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "__pycache__"
                             and directory / d not in roots)
            if directory == internal and "runtime" in dirs:
                dirs.remove("runtime")
            if "manifest.toml" in names:
                dirs[:] = [d for d in dirs if d in ("work", "analysis")]
                run_work.add(directory / "work")
            if directory in run_work:
                dirs.clear()
            job_count = sum(bool(JOB_LOG.fullmatch(name)) for name in names)
            other_logs = sum(name.endswith(".log") and not JOB_LOG.fullmatch(name) for name in names)
            total_jobs += job_count
            total_other += other_logs
            in_logs = "logs" in directory.relative_to(root).parts
            if directory in run_work:
                managed += job_count
            elif not in_logs:
                loose += job_count
            if (all_dirs and (job_count or other_logs)) or (job_count and not in_logs and directory not in run_work):
                print(f"{job_count:8}  {other_logs:10}  {directory.relative_to(hub)}")
    print(f"{total_jobs} job logs ({loose} loose, {managed} in run work/), {total_other} other .log")


def tracked_names(directory):
    if not any((p / ".git").exists() for p in (directory, *directory.parents)):
        return set()
    output = subprocess.check_output(["git", "ls-files", "-z", "--", ":(glob)*"], cwd=directory)
    return set(os.fsdecode(output).split("\0"))


def collect(directory, apply):
    logs = directory / "logs"
    if logs.is_symlink():
        raise ValueError(f"{logs} is a symlink")
    if logs.exists() and not logs.is_dir():
        raise ValueError(f"{logs} is not a directory")
    files = {}
    tracked = tracked_names(directory)
    for path in sorted(directory.iterdir()):
        match = JOB_LOG.fullmatch(path.name)
        if match and not path.is_symlink() and path.is_file():
            if path.name in tracked:
                print(f"skip {path.name} (tracked by Git)")
            else:
                files[path] = match["job"]

    finished = finished_jobs(sorted(set(files.values())))
    count = 0
    for path, job_id in files.items():
        if job_id not in finished:
            print(f"skip {path.name} (job completion unconfirmed)")
            continue
        dest = logs / path.name
        # Linking before unlinking prevents overwrites and preserves the file on interruption.
        if dest.is_symlink() or (dest.exists() and not path.samefile(dest)):
            print(f"skip {path.name} (destination exists)")
            continue
        if apply:
            logs.mkdir(exist_ok=True)
            try:
                os.link(path, dest)
            except FileExistsError:
                if dest.is_symlink() or not path.samefile(dest):
                    print(f"skip {path.name} (destination exists)")
                    continue
            path.unlink()
        print(f"{path.name} -> logs/{path.name}")
        count += 1
    print(f"{'moved' if apply else 'would move'} {count} file(s) to {logs}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", help="workspace-relative directory to preview")
    parser.add_argument("--all-dirs", action="store_true", help="also list organized and unnumbered log locations")
    parser.add_argument("--apply", action="store_true", help="move the listed logs")
    args = parser.parse_args()
    hub = Path(os.environ["RUNOPS_HUB_DIR"]).resolve()
    roots = project_roots(hub)
    try:
        if args.directory is None:
            if args.apply:
                parser.error("--apply requires a directory")
            inventory(hub, roots, args.all_dirs)
            return 0
        if args.all_dirs:
            parser.error("--all-dirs is for the workspace inventory")
        directory = (hub / args.directory).resolve()
        if not directory.is_dir():
            parser.error("directory does not exist")
        root = next((r.resolve() for r in roots if directory.is_relative_to(r.resolve())), hub)
        if not directory.is_relative_to(root):
            parser.error("directory must belong to this hub or one of its projects")
        relative = directory.relative_to(root)
        if ("logs" in relative.parts or relative.parts[:2] == (".hub", "runtime")
                or any(part.startswith(".") and part != ".hub" for part in relative.parts)):
            parser.error("already organized or internal directory")
        for parent in (directory, *directory.parents):
            if (parent / "manifest.toml").is_file() and not directory.is_relative_to(parent / "analysis"):
                parser.error("run files are managed by runops; select its analysis directory")
            if parent == root:
                break
        # Serialize organizers on this directory, including invocations through different hubs.
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if args.apply else fcntl.LOCK_SH)
            collect(directory, args.apply)
        finally:
            os.close(fd)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"logs: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
