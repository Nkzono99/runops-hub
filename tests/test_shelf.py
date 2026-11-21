from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from helpers import REPO, load, member

PAPERS_TABLE = "\n| dir | source | 内容 |\n|---|---|---|\n"


class ShelfTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.hub = root / "hub"
        self.bin = root / "bin"
        self.bin.mkdir()
        (self.bin / "python").symlink_to(sys.executable)  # ./hub takes the Python beside runo
        runo = self.bin / "runo"
        runo.write_text('#!/bin/sh\n[ "$1" = list ] && echo "${FAKE_RUNO_LIST:-[]}"\nexit 0\n')
        runo.chmod(0o755)
        self.assertEqual(self.hub_cmd("init", str(self.hub), script=REPO / "hub").returncode, 0)
        self.a = member(self.hub, "a")
        member(self.hub, "b")
        projects = load("projects", self.hub)
        projects.main(["add", "a", "emses", "goal a"])
        projects.main(["add", "b", "beach", "goal b"])

    def hub_cmd(self, *args: str, script: Path | None = None, **env: str) -> subprocess.CompletedProcess[str]:
        clean = {k: v for k, v in os.environ.items() if k != "RUNOPS_HUB_DIR"}
        clean.update(env, PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}")
        return subprocess.run(["bash", str(script or self.hub / "hub"), *args], capture_output=True, text=True, env=clean)

    def paper(self, name: str, row: str) -> Path:
        real = self.hub.parent / f"paper-{name}"
        real.mkdir()
        (real / "paper.toml").write_text("[paper]\n", encoding="utf-8")
        (self.hub / "papers").mkdir(exist_ok=True)
        (self.hub / "papers" / name).symlink_to(f"../../paper-{name}")
        agents = self.hub / "AGENTS.md"
        text = agents.read_text(encoding="utf-8")
        if "| dir | source |" not in text:
            text += PAPERS_TABLE
        agents.write_text(text + row + "\n", encoding="utf-8")
        return real

    def agents(self) -> str:
        return (self.hub / "AGENTS.md").read_text(encoding="utf-8")

    def shelf(self) -> dict:
        path = self.hub / ".hub" / "shelf.toml"
        return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def test_archive_moves_the_link_out_of_projects_and_restore_moves_it_back(self) -> None:
        res = self.hub_cmd("archive", "a", "--why", "結論を LOG に書いた")
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        moved = self.hub / "archive/projects/a"
        self.assertFalse((self.hub / "projects/a").exists() or (self.hub / "projects/a").is_symlink())
        self.assertEqual(os.readlink(moved), "../../../project_a")  # still relative, one level deeper
        self.assertEqual(moved.resolve(), self.a)
        projects = load("projects", self.hub)
        self.assertEqual([p.name for p in projects.member_paths(self.hub)], ["b"])
        self.assertEqual(projects.member_paths(self.hub, shelved=True), [moved, self.hub / "projects/b"])
        self.assertNotIn("`projects/a/`", self.agents())
        entry = self.shelf()["projects/a"]
        self.assertEqual(entry["why"], "結論を LOG に書いた")
        self.assertEqual(entry["row"], "| `projects/a/` | emses | goal a |")
        dirs = json.loads((self.hub / ".claude/settings.json").read_text(encoding="utf-8"))
        self.assertIn(str(self.a), dirs["permissions"]["additionalDirectories"])  # still readable and writable
        listing = self.hub_cmd("list")
        self.assertNotIn("===== a ", listing.stdout)
        self.assertIn("shelf: archived 1", listing.stdout)
        shown = self.hub_cmd("shelf").stdout
        self.assertIn("archive/ (archived", shown)
        self.assertIn(str(self.a), shown)
        self.assertNotEqual(self.hub_cmd("attach", "a", str(self.a), "emses", "--desc", "x").returncode, 0)

        res = self.hub_cmd("restore", "archive/projects/a/")
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertEqual(os.readlink(self.hub / "projects/a"), "../../project_a")
        self.assertFalse((self.hub / "archive").exists())  # an empty shelf leaves no directory behind
        self.assertIn("| `projects/a/` | emses | goal a |", self.agents())
        self.assertEqual([p.name for p in projects.member_paths(self.hub)], ["a", "b"])
        self.assertEqual(self.shelf(), {})
        self.assertNotEqual(self.hub_cmd("restore", "a").returncode, 0)

    def test_a_name_shared_by_a_project_and_a_paper_must_say_which(self) -> None:
        real = self.paper("a", "| `papers/a/` | `projects/a/` | paper a |")
        res = self.hub_cmd("defer", "a")
        self.assertEqual(res.returncode, 2)
        self.assertIn("say which", res.stderr)
        res = self.hub_cmd("defer", "papers/a", "--until", "2000-01-01", "--why", "投稿後に再開")
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertEqual((self.hub / "deferred/papers/a").resolve(), real)
        self.assertIn("| `projects/a/` |", self.agents())
        self.assertNotIn("| `papers/a/` |", self.agents())
        projects = load("projects", self.hub)
        self.assertEqual(projects.paper_paths(self.hub), [])
        self.assertEqual(projects.paper_paths(self.hub, shelved=True), [self.hub / "deferred/papers/a"])
        self.assertEqual([p.name for p in projects.member_paths(self.hub)], ["a", "b"])
        self.assertIn("papers/a until 01-01, due", projects.shelf_line(self.hub))
        logs = load("logs", self.hub)
        self.assertEqual(logs.project_roots(self.hub), [self.hub / "projects/a", self.hub / "projects/b"])
        res = self.hub_cmd("archive", "a")  # still ambiguous: projects/a is in use, papers/a set aside
        self.assertEqual(res.returncode, 2)
        res = self.hub_cmd("archive", "papers/a")  # deferred -> archived keeps the row and the reason
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertFalse((self.hub / "deferred").exists())
        self.assertEqual((self.hub / "archive/papers/a").resolve(), real)
        entry = self.shelf()["papers/a"]
        self.assertEqual((entry["why"], entry.get("until")), ("投稿後に再開", None))
        self.assertEqual(entry["row"], "| `papers/a/` | `projects/a/` | paper a |")

    def test_paths_through_the_link_in_the_slides_become_real_paths(self) -> None:
        card = self.hub / "slides/20261127/cards/x/card.toml"
        card.parent.mkdir(parents=True)
        card.write_text('from = "projects/a/analysis/f.png"\n'
                        f'data = "{self.hub}/projects/a/out/*.npz"\n'
                        'read = "projects/a/analysis/README.md、projects/ab/analysis/README.md"\n', encoding="utf-8")
        table = card.parent / "values.csv"
        table.write_text("# projects/a/analysis/x.csv から写した\nk,v\n", encoding="utf-8")
        built = self.hub / "slides/20261127/out/deck.typ"
        built.parent.mkdir()
        built.write_text("projects/a/analysis/f.png\n", encoding="utf-8")
        res = self.hub_cmd("defer", "a")
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertEqual(card.read_text(encoding="utf-8"),
                         f'from = "{self.a}/analysis/f.png"\n'
                         f'data = "{self.a}/out/*.npz"\n'
                         f'read = "{self.a}/analysis/README.md、projects/ab/analysis/README.md"\n')
        self.assertEqual(table.read_text(encoding="utf-8"), f"# {self.a}/analysis/x.csv から写した\nk,v\n")
        self.assertEqual(built.read_text(encoding="utf-8"), "projects/a/analysis/f.png\n")  # build output is remade

    def test_waiting_or_running_jobs_need_force(self) -> None:
        busy = '[{"id": "R20261121-0001", "status": "running"}]'
        res = self.hub_cmd("defer", "a", FAKE_RUNO_LIST=busy)
        self.assertEqual(res.returncode, 2)
        self.assertIn("R20261121-0001", res.stderr)
        self.assertTrue((self.hub / "projects/a").is_symlink())
        self.assertEqual(self.shelf(), {})
        res = self.hub_cmd("defer", "a", "--force", FAKE_RUNO_LIST=busy)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertTrue((self.hub / "deferred/projects/a").is_symlink())

    def test_detach_of_a_shelved_project_drops_its_link_and_notes(self) -> None:
        self.assertEqual(self.hub_cmd("archive", "b").returncode, 0)
        res = self.hub_cmd("detach", "b")
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertFalse((self.hub / "archive").exists())
        self.assertEqual(self.shelf(), {})
        self.assertNotIn("`projects/b/`", self.agents())
        self.assertTrue((self.hub.parent / "project_b").is_dir())

    def test_hub_auto_skips_a_shelved_project_but_still_counts_it(self) -> None:
        auto = load("auto", self.hub)
        auto.list_runs = lambda path: []
        auto.allocation = lambda fresh=True: {"at": "x", "allocated": 100.0, "remaining": 50.0}
        auto.set_project_table("a", {"analyze": True, "continue": True})
        config = auto.load_config()
        self.assertIsNotNone(auto.project_settings(config, "a"))
        self.assertEqual(self.hub_cmd("defer", "a").returncode, 0)
        self.assertIsNone(auto.project_settings(config, "a"))
        self.assertEqual(auto.enabled(config), [])
        self.assertIn("a", auto.members())
        _, problems = auto.gate(config, "a", self.a, [], "continue")
        self.assertTrue(any("set aside" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
