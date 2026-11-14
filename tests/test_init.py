from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import REPO

sys.path.insert(0, str(REPO / "src"))
from runops_hub import cli  # noqa: E402


class InitTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.hub = self.base / "hub with spaces"

    def test_init_installs_assets_and_agent_onboarding(self) -> None:
        cli.initialize(self.hub)
        self.assertEqual(os.readlink(self.hub / "hub"), ".hub/runtime/hub")
        self.assertEqual((self.hub / "hub").resolve(), self.hub / ".hub/runtime/hub")
        self.assertTrue((self.hub / ".hub/runtime/lib/board.py").is_file())
        self.assertTrue((self.hub / ".hub/runtime/LICENSE").is_file())
        self.assertIn("人が行う準備は hub の `init`", (self.hub / "AGENTS.md").read_text())
        self.assertIn("uv tool install", (self.hub / "AGENTS.md").read_text())
        self.assertEqual((self.hub / "CLAUDE.md").read_text(), "@AGENTS.md\n")

    def test_hub_works_after_the_bootstrap_cache_is_deleted_without_runo_on_path(self) -> None:
        cache = self.base / "uv-cache"
        cache.mkdir()
        for name in ("hub", "LICENSE"):
            shutil.copy2(REPO / name, cache / name)
        for name in ("lib", "templates"):
            shutil.copytree(REPO / name, cache / name)
        with patch.object(cli, "runtime_source", return_value=cache):
            cli.initialize(self.hub)
        shutil.rmtree(cache)
        env = {k: v for k, v in os.environ.items() if k != "RUNOPS_HUB_DIR"}
        env["PATH"] = "/usr/bin:/bin"
        result = subprocess.run(["bash", str(self.hub / "hub"), "board"], env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.hub / "board.html").is_file())
        interpreter = Path((self.hub / ".hub/python").read_text().strip())
        self.assertEqual(interpreter, Path(sys.executable).resolve())

    def test_refresh_preserves_user_instructions_settings_and_project_links(self) -> None:
        cli.initialize(self.hub)
        custom = {
            "AGENTS.md": "# My projects and rules\n",
            "CLAUDE.md": "@AGENTS.md\n@MY_RULES.md\n",
            ".hub/member_section.md": "My member instructions\n",
            ".hub/status_seen.json": json.dumps({"seen": "keep"}),
            ".claude/settings.json": json.dumps({"model": "custom"}),
        }
        for name, content in custom.items():
            target = self.hub / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        project = self.base / "project_example"
        project.mkdir()
        (self.hub / "example").symlink_to("../project_example")
        (self.hub / ".hub/runtime/lib/obsolete.py").write_text("old")
        cli.initialize(self.hub)
        for name, content in custom.items():
            self.assertEqual((self.hub / name).read_text(), content)
        self.assertEqual((self.hub / "example").resolve(), project)
        self.assertFalse((self.hub / ".hub/runtime/lib/obsolete.py").exists())

    def test_init_refuses_an_unrelated_launcher_without_overwriting_it(self) -> None:
        self.hub.mkdir()
        (self.hub / "hub").write_text("my script")
        self.assertEqual(cli.main(["init", str(self.hub)]), 2)
        self.assertEqual((self.hub / "hub").read_text(), "my script")
        self.assertFalse((self.hub / ".hub").exists())

    def test_a_failed_copy_keeps_the_installed_runtime(self) -> None:
        cli.initialize(self.hub)
        original = (self.hub / "hub").read_bytes()
        with patch.object(cli.shutil, "copytree", side_effect=OSError("copy failed")):
            self.assertEqual(cli.main(["init", str(self.hub)]), 2)
        self.assertEqual((self.hub / "hub").read_bytes(), original)
        self.assertFalse(list((self.hub / ".hub").glob(".init-*")))


if __name__ == "__main__":
    unittest.main()
