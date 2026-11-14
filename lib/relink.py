#!/usr/bin/env python3
"""Regenerate the hub's skill symlinks and Claude Code working directories from its members.

Skills: the hub exposes the union of every member's deployed skills. When several
members deploy the same skill, the member whose `.runops/skills.lock` records the
newest version wins (ties: first member name in sorted order; contents are then
identical). Only symlinks under .claude/skills and .agents/skills are managed;
a real directory there is left alone and reported.

Working directories: members and papers/* are symlinks to directories outside the hub,
so Claude Code counts their files as outside the working directory and asks before
every write, even in auto mode. relink keeps `permissions.additionalDirectories` in
`.claude/settings.json` equal to their real paths plus this repository. Other keys
and directories added by hand stay; a directory relink added earlier is removed once
it is no longer a member (the list relink manages is kept in .hub/permission_dirs.json).

    relink.py          apply: create/replace/remove symlinks, update the settings, then report
    relink.py --check  report only; exit 1 if relink would change something
    -v                 list every skill, every changed link and every working directory
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    sys.exit("relink.py needs Python >= 3.11 (tomllib)")

# The hub directory (members, AGENTS.md, .hub/ state). ./hub exports it; run by hand, it is the cwd.
HUB = Path(os.environ.get("RUNOPS_HUB_DIR") or Path.cwd()).resolve()
KINDS = (".claude", ".agents")
REPO = Path(__file__).resolve().parents[1]
SETTINGS = HUB / ".claude" / "settings.json"
DIRS_STATE = HUB / ".hub" / "permission_dirs.json"


def version_key(v: str) -> tuple:
    # (1, n) for numeric parts and (0, text) otherwise, so "local" (a project-local adapter)
    # compares with, and sorts below, a released version such as "4.22.1".
    return tuple((1, int(p)) if p.isdigit() else (0, p) for p in v.replace("-", ".").split("."))


def members() -> list[Path]:
    out = []
    for p in sorted(HUB.iterdir()):
        if p.name.startswith(".") or not p.is_dir():
            continue
        if (p / "runops.toml").is_file():
            out.append(p)
    return out


def load_lock(member: Path) -> list[dict]:
    lock = member / ".runops" / "skills.lock"
    if not lock.is_file():
        print(f"!! {member.name}: no .runops/skills.lock (run `runo update` there)")
        return []
    with lock.open("rb") as f:
        return tomllib.load(f).get("skills", [])


def choose(mems: list[Path]) -> tuple[dict[str, dict], list[str]]:
    """Return {skill name: {member, version, source, hash}} and skew notes."""
    seen: dict[str, list[dict]] = {}
    for m in mems:
        for s in load_lock(m):
            seen.setdefault(s["name"], []).append({**s, "member": m.name})
    chosen: dict[str, dict] = {}
    notes: list[str] = []
    for name, entries in sorted(seen.items()):
        newest = max(version_key(e["version"]) for e in entries)
        best = min((e for e in entries if version_key(e["version"]) == newest), key=lambda e: e["member"])
        chosen[name] = best
        for e in entries:
            if e is best:
                continue
            if version_key(e["version"]) < version_key(best["version"]):
                notes.append(f"{name}: {e['member']} has {e['version']}, {best['member']} has {best['version']}")
            elif e["version"] == best["version"] and e.get("hash") != best.get("hash"):
                notes.append(f"{name}: same version {e['version']} but different hash in {e['member']} and {best['member']}")
    return chosen, notes


def plan(chosen: dict[str, dict]) -> tuple[list[tuple], list[str]]:
    """Return (changes, warnings). A change is (action, link_path, target_or_None)."""
    changes: list[tuple] = []
    warnings: list[str] = []
    for kind in KINDS:
        skills_dir = HUB / kind / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        wanted: dict[str, str] = {}
        for name, e in chosen.items():
            src = HUB / e["member"] / kind / "skills" / name
            if not src.is_dir():
                warnings.append(f"{e['member']}/{kind}/skills/{name} missing; not linked")
                continue
            wanted[name] = os.path.join("..", "..", e["member"], kind, "skills", name)
        for name, target in wanted.items():
            link = skills_dir / name
            if link.is_symlink():
                if os.readlink(link) != target:
                    changes.append(("replace", link, target))
            elif link.exists():
                warnings.append(f"{kind}/skills/{name} is a real directory (hub-local); left alone")
            else:
                changes.append(("create", link, target))
        for link in sorted(skills_dir.iterdir()):
            if not link.is_symlink() or link.name in wanted:
                continue
            raw = os.readlink(link)
            if raw.startswith(os.path.join("..", "..")):
                changes.append(("remove", link, None))
            else:
                warnings.append(f"{kind}/skills/{link.name} -> {raw} is not a member link; left alone")
    return changes, warnings


def apply(changes: list[tuple]) -> None:
    for action, link, target in changes:
        if action in ("replace", "remove"):
            link.unlink()
        if action in ("replace", "create"):
            link.symlink_to(target)


def work_dirs(mems: list[Path]) -> list[str]:
    """Real paths an agent working from the hub writes to: members, papers/*, this repository.

    A path inside the hub is already part of the working directory and is left out.
    """
    paths = [m.resolve() for m in mems]
    papers = HUB / "papers"
    if papers.is_dir():
        paths += [p.resolve() for p in sorted(papers.iterdir()) if p.is_dir() and not p.name.startswith(".")]
    paths.append(REPO)
    out: list[str] = []
    for p in paths:
        if p == HUB or HUB in p.parents or str(p) in out:
            continue
        out.append(str(p))
    return out


def plan_dirs(wanted: list[str]) -> tuple[dict | None, list[str], list[str], str | None]:
    """Return (settings to write or None when unchanged, added, removed, error)."""
    settings: dict = {}
    if SETTINGS.exists():
        try:
            settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return None, [], [], f".claude/settings.json is not readable JSON ({exc}); working directories left alone"
    perms = settings.get("permissions", {}) if isinstance(settings, dict) else None
    current = perms.get("additionalDirectories", []) if isinstance(perms, dict) else None
    if not isinstance(current, list):
        return None, [], [], ".claude/settings.json has an unexpected shape; working directories left alone"
    managed: list[str] = []
    if DIRS_STATE.is_file():
        try:
            managed = json.loads(DIRS_STATE.read_text(encoding="utf-8")).get("dirs", [])
        except (OSError, json.JSONDecodeError, AttributeError):
            managed = []
    removed = [d for d in current if d in managed and d not in wanted]
    kept = [d for d in current if d not in removed]
    added = [d for d in wanted if d not in kept]
    if not added and not removed:
        return None, [], [], None
    new_perms = {**perms, "additionalDirectories": kept + added}
    return {**settings, "permissions": new_perms}, added, removed, None


def apply_dirs(new_settings: dict | None, wanted: list[str]) -> None:
    if new_settings is not None:
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS.with_name(SETTINGS.name + ".tmp")
        tmp.write_text(json.dumps(new_settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(SETTINGS)
    DIRS_STATE.parent.mkdir(parents=True, exist_ok=True)
    DIRS_STATE.write_text(json.dumps({"dirs": wanted}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    check = "--check" in argv
    mems = members()
    if not mems:
        print("no member projects (symlinks with runops.toml) in", HUB)
        return 2
    chosen, notes = choose(mems)
    changes, warnings = plan(chosen)
    wanted = work_dirs(mems)
    new_settings, added, removed, dirs_error = plan_dirs(wanted)
    if dirs_error:
        warnings.append(dirs_error)
    if not check:
        apply(changes)
        if not dirs_error:
            apply_dirs(new_settings, wanted)

    verbose = "-v" in argv
    groups: dict[tuple, list[str]] = {}
    for e in chosen.values():
        groups.setdefault((e["source"], e["version"], e["member"]), []).append(e["name"])
    print(f"members: {', '.join(m.name for m in mems)}")
    for (source, version, member), names in sorted(groups.items()):
        print(f"  {source} {version} <- {member} ({len(names)} skills)")
        if verbose:
            for n in sorted(names):
                print(f"      {n}")
    if notes:
        print("version skew between members:")
        for n in notes:
            print("  -", n)
    for w in warnings:
        print("!!", w)
    verb = "would change" if check else "changed"
    print(f"{verb}: {len(changes)} link(s)")
    for action, link, target in changes if verbose or len(changes) <= 8 else []:
        rel = link.relative_to(HUB)
        print(f"  {action:<7} {rel}" + (f" -> {target}" if target else ""))
    print(f"working directories (.claude/settings.json permissions.additionalDirectories): {len(wanted)}, "
          f"{verb} {len(added) + len(removed)}")
    for d in added:
        print(f"  add     {d}")
    for d in removed:
        print(f"  remove  {d}")
    if verbose:
        for d in wanted:
            print(f"      {d}")
    return 1 if (check and (changes or added or removed)) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
