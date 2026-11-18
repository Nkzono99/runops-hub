import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from helpers import REPO, member


class LogTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.hub = base / "hub"
        self.env = dict(os.environ)
        self.env.pop("RUNOPS_HUB_DIR", None)
        subprocess.run([REPO / "hub", "init", self.hub], env=self.env, check=True, capture_output=True)
        self.project = member(self.hub, "column")
        self.directory = self.project / "analysis" / "with spaces"
        self.directory.mkdir(parents=True)
        sacct = base / "sacct"
        sacct.write_text("#!/bin/sh\nprintf '%s\\n' '101|COMPLETED' '102|FAILED' '103|RUNNING' '105|CANCELLED by 17' '106_4|TIMEOUT'\n")
        sacct.chmod(0o755)
        self.env["PATH"] = str(base) + os.pathsep + self.env["PATH"]

    def logs(self, *args):
        return subprocess.run([self.hub / "hub", "logs", *args], env=self.env,
                              capture_output=True, text=True)

    def test_apply_after_preview_retains_contents_and_protected_paths(self):
        directory = self.directory
        for name in ("plot.101.out", "plot.101.err", "proof-102.log", "new-103.out",
                     "unknown-104.out", "collision-105.out", "array-106_4.err",
                     "tracked-101.out", "main.out", "science.log", "main.pdf"):
            (directory / name).write_text(name)
        (directory / "link-101.out").symlink_to("science.log")
        (directory / "logs").mkdir()
        (directory / "logs/collision-105.out").write_text("older")
        git = ["git", "-C", str(self.project)]
        subprocess.run(git + ["init", "-q"], check=True)
        subprocess.run(git + ["add", "--", "analysis/with spaces/tracked-101.out"], check=True)
        target = "column/analysis/with spaces"
        preview = self.logs(target)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertIn("would move 4", preview.stdout)
        self.assertTrue((directory / "proof-102.log").exists())
        applied = self.logs(target, "--apply")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        for name in ("plot.101.out", "plot.101.err", "proof-102.log", "array-106_4.err"):
            self.assertEqual((directory / "logs" / name).read_text(), name)
            self.assertFalse((directory / name).exists())
        for name in ("new-103.out", "unknown-104.out", "collision-105.out", "tracked-101.out",
                     "main.out", "science.log", "main.pdf", "link-101.out"):
            self.assertTrue((directory / name).exists())
        self.assertEqual((directory / "logs/collision-105.out").read_text(), "older")
        self.assertIn("moved 0", self.logs(target, "--apply").stdout)

    def test_hub_inventory_and_run_files(self):
        run = self.project / "runs/example/R1"
        (run / "work").mkdir(parents=True)
        (run / "analysis").mkdir()
        (run / "manifest.toml").touch()
        (run / "work/stdout.101.log").touch()
        (run / "analysis/render-101.out").touch()
        (self.directory / "science.log").touch()
        (self.directory / "loop").symlink_to(self.project)
        (self.hub / "papers").mkdir()
        paper = self.hub.parent / "paper"
        (paper / "figures").mkdir(parents=True)
        (paper / "paper.toml").touch()
        (paper / "figures/draw-102.err").touch()
        (self.hub / "papers/example").symlink_to(paper)
        result = self.logs("--all-dirs")
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("papers/example/figures", "column/analysis/with spaces", "column/runs/example/R1/analysis"):
            self.assertEqual(result.stdout.count(name), 1)
        self.assertIn("column/runs/example/R1/work", result.stdout)
        self.assertIn("1 in run work/", result.stdout)
        self.assertNotIn("R1/work", self.logs().stdout)
        self.assertNotEqual(self.logs("column/runs/example/R1/work", "--apply").returncode, 0)
        self.assertNotEqual(self.logs("--apply").returncode, 0)
        self.assertNotEqual(self.logs("..", "--apply").returncode, 0)
        self.assertTrue((run / "work/stdout.101.log").exists())

    def test_existing_vscode_preferences_survive_init(self):
        settings = self.hub / ".vscode/settings.json"
        self.assertIn("files.exclude", json.loads(settings.read_text()))
        settings.write_text('// personal settings with comments\n{}\n')
        subprocess.run([REPO / "hub", "init", self.hub], env=self.env, check=True, capture_output=True)
        self.assertTrue(settings.read_text().startswith("// personal"))
