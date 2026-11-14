from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import REPO, load, member


class HubTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.hub = Path(tmp.name).resolve() / "hub"
        res = self.run_hub(REPO / "hub", "init", str(self.hub))
        self.assertEqual(res.returncode, 0, res.stderr)

    def run_hub(self, script: Path, *args: str) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if k != "RUNOPS_HUB_DIR"}
        return subprocess.run(["bash", str(script), *args], capture_output=True, text=True, env=env)

    def test_init_links_the_script_and_writes_the_skeleton(self) -> None:
        self.assertEqual((self.hub / "hub").resolve(), REPO / "hub")
        self.assertIn("| dir | simulator | 内容 |", (self.hub / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertIn(str(self.hub), (self.hub / ".hub" / "member_section.md").read_text(encoding="utf-8"))

    def test_the_repository_itself_is_not_a_hub(self) -> None:
        res = self.run_hub(REPO / "hub", "status")
        self.assertEqual(res.returncode, 2)
        self.assertIn("init DIR", res.stderr)

    def test_attach_and_detach_keep_link_table_and_member_notes_in_step(self) -> None:
        project = self.hub.parent / "old_project"
        project.mkdir()
        (project / "runops.toml").write_text("[project]\n", encoding="utf-8")
        (project / "AGENTS.md").write_text("# old\n", encoding="utf-8")
        res = self.run_hub(self.hub / "hub", "attach", "old", str(project), "emses", "--desc", "goal")
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertEqual(os.readlink(self.hub / "old"), "../old_project")
        self.assertIn("| `old/` | emses | goal |", (self.hub / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertIn("## runops hub", (project / "AGENTS.md").read_text(encoding="utf-8"))
        res = self.run_hub(self.hub / "hub", "detach", "old")
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertFalse((self.hub / "old").is_symlink())
        self.assertNotIn("`old/`", (self.hub / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertTrue(project.is_dir())

    def test_relink_exposes_the_newest_copy_of_each_skill(self) -> None:
        for name, version in (("a", "2.7.0"), ("b", "2.8.0")):
            project = member(self.hub, name)
            (project / ".runops").mkdir()
            (project / ".runops" / "skills.lock").write_text(
                f'[[skills]]\nname = "runops-x"\nversion = "{version}"\nsource = "runops"\nhash = "{name}"\n',
                encoding="utf-8",
            )
            (project / ".claude" / "skills" / "runops-x").mkdir(parents=True)
        relink = load("relink", self.hub)
        chosen, notes = relink.choose(relink.members())
        self.assertEqual(chosen["runops-x"]["member"], "b")
        self.assertEqual(len(notes), 1)
        relink.apply(relink.plan(chosen)[0])
        self.assertEqual(os.readlink(self.hub / ".claude" / "skills" / "runops-x"), "../../b/.claude/skills/runops-x")

    def test_relink_keeps_the_real_paths_of_members_and_papers_as_working_directories(self) -> None:
        project = member(self.hub, "a")
        paper = self.hub.parent / "paper-x"
        paper.mkdir()
        (self.hub / "papers").mkdir()
        (self.hub / "papers" / "x").symlink_to("../../paper-x")
        settings = self.hub / ".claude" / "settings.json"
        settings.parent.mkdir(exist_ok=True)
        settings.write_text(json.dumps({"model": "opus", "permissions": {
            "allow": ["Bash(cd *)"], "additionalDirectories": ["/by/hand"]}}), encoding="utf-8")
        relink = load("relink", self.hub)
        self.assertEqual(relink.main([]), 0)
        data = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(data["model"], "opus")
        self.assertEqual(data["permissions"]["allow"], ["Bash(cd *)"])
        self.assertEqual(data["permissions"]["additionalDirectories"],
                         ["/by/hand", str(project.resolve()), str(paper.resolve()), str(REPO)])
        self.assertEqual(relink.main(["--check"]), 0)  # nothing left to change
        (self.hub / "a").unlink()
        member(self.hub, "b")
        self.assertEqual(relink.main(["--check"]), 1)
        self.assertEqual(relink.main([]), 0)
        dirs = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["additionalDirectories"]
        self.assertNotIn(str(project.resolve()), dirs)
        self.assertIn("/by/hand", dirs)
        self.assertIn(str((self.hub.parent / "project_b").resolve()), dirs)

    def test_relink_leaves_unreadable_settings_alone(self) -> None:
        member(self.hub, "a")
        settings = self.hub / ".claude" / "settings.json"
        settings.parent.mkdir(exist_ok=True)
        settings.write_text("{not json", encoding="utf-8")
        relink = load("relink", self.hub)
        self.assertEqual(relink.main([]), 0)
        self.assertEqual(settings.read_text(encoding="utf-8"), "{not json")


if __name__ == "__main__":
    unittest.main()
