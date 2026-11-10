#!/usr/bin/env python3
"""Regenerate the hub's skill symlinks from its member projects.

Rule: the hub exposes the union of every member's deployed skills. When several
members deploy the same skill, the member whose `.runops/skills.lock` records the
newest version wins (ties: first member name in sorted order; contents are then
identical). Only symlinks under .claude/skills and .agents/skills are managed;
a real directory there is left alone and reported.

    relink.py          apply: create/replace/remove symlinks, then report
    relink.py --check  report only; exit 1 if relink would change something
    -v                 list every skill and every changed link
"""
from __future__ import annotations

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


def main(argv: list[str]) -> int:
    check = "--check" in argv
    mems = members()
    if not mems:
        print("no member projects (symlinks with runops.toml) in", HUB)
        return 2
    chosen, notes = choose(mems)
    changes, warnings = plan(chosen)
    if not check:
        apply(changes)

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
    return 1 if (check and changes) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
