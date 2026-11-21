from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import unittest.mock
from datetime import timedelta
from pathlib import Path

from helpers import REPO, load, member


def inline(value) -> str:
    """A TOML inline value (enough for the manifests these tests write)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(inline(v) for v in value) + "]"
    return "{ " + ", ".join(f"{json.dumps(k)} = {inline(v)}" for k, v in value.items()) + " }"


FAKE_AGENT = textwrap.dedent("""\
    import os, sys, time
    args = sys.argv[1:]
    sys.stdin.read()
    last = args[args.index("--output-last-message") + 1]
    with open(last, "w") as f:
        f.write("- 解析: fake agent was here\\n")
    time.sleep(float(os.environ.get("FAKE_AGENT_SLEEP", "0")))
""")


class AutoTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.hub = Path(tmp.name) / "hub"
        (self.hub / ".hub").mkdir(parents=True)
        self.auto = load("auto", self.hub)
        self.auto.POLL = 0.05
        self.project = member(self.hub, "demo")
        self.runs: list[dict] = []
        self.calls: list[tuple] = []
        self.auto.list_runs = lambda path: self.runs
        self.auto.allocation = lambda fresh=True: {"at": "x", "allocated": 100.0, "remaining": 50.0}
        self.auto.set_project_table("demo", {"analyze": True, "continue": True, "derive": True})
        self.config = self.auto.load_config()

    def add_run(self, rid: str, status: str = "created", parent: str = "", by: str = "hub-auto",
                params: dict | None = None, job: dict | None = None, attempts: list | None = None,
                input_hash: str = "", adapter: str = "beach") -> dict:
        run_dir = f"runs/{rid}"
        job = {"partition": "gr20001a", "walltime": "1:00:00", "processes": 8, "threads": 14, "cores": 14,
               **(job or {})}
        if attempts:
            job["attempts"] = attempts
        data = {
            "run": {"id": rid, "status": status, "why": f"why {rid}"},
            "origin": {"parent_run": parent, "created_by": by},
            "simulator": {"adapter": adapter},
            "job": job,
            "params": params or {"run.batch_count": 100},
        }
        (self.project / run_dir).mkdir(parents=True)
        (self.project / run_dir / "manifest.toml").write_text(
            "".join(f"{k} = {inline(v)}\n" for k, v in data.items()), encoding="utf-8")
        entry = {"id": rid, "status": status, "run_dir": run_dir, "parent_run": parent,
                 "input_hash": input_hash or f"sha256:{rid}", "label": rid}
        self.runs.append(entry)
        return entry

    def finished(self, hours_ago: float, hours: float = 1.0, job_id: str = "1") -> list[dict]:
        end = self.auto.now() - timedelta(hours=hours_ago)
        start = end - timedelta(hours=hours)
        return [{"job_id": job_id, "started_at": self.auto.stamp(start), "finished_at": self.auto.stamp(end),
                 "slurm_state": "COMPLETED"}]

    # ------------------------------------------------------------ core-hours and continuations

    def test_core_hours_follow_the_rsc_layout(self) -> None:
        job = {"processes": 8, "threads": 14, "cores": 14, "walltime": "120:00:00"}
        self.assertEqual(self.auto.job_cores(job), 112)
        self.assertEqual(self.auto.reserved(job), 13440)
        self.assertEqual(self.auto.walltime_hours("1-00:30:00"), 24.5)

    def test_a_continuation_changes_only_the_end_time_and_keeps_the_layout(self) -> None:
        parent = {"simulator": {"adapter": "beach"}, "params": {"run.batch_count": 100, "x": 1},
                  "job": {"processes": 8, "cores": 14, "walltime": "1:00:00"}}
        child = {"simulator": {"adapter": "beach"}, "params": {"run.batch_count": 200, "x": 1},
                 "job": {"processes": 8, "cores": 14, "walltime": "9:00:00", "pre_commands": ["check"]}}
        self.assertEqual(self.auto.continuation_problems(self.config, parent, child), [])
        child["params"]["x"] = 2
        self.assertIn("x", self.auto.continuation_problems(self.config, parent, child)[0])
        child["params"]["x"] = 1
        child["job"]["processes"] = 4
        self.assertIn("layout", self.auto.continuation_problems(self.config, parent, child)[0])

    def test_nested_params_are_compared_by_dotted_key(self) -> None:
        # emses records params as nested tables; the continuation keys are dotted
        job = {"processes": 256, "cores": 1, "walltime": "24:00:00"}
        parent = {"simulator": {"adapter": "emses"}, "job": job,
                  "params": {"jobcon": {"jobnum": [0, 1], "nstep": 240000},
                             "species": [{"wp": 2.0}, {"wp": 0.05}], "memo": {"note": "a"}}}
        child = copy.deepcopy(parent)
        child["params"]["jobcon"] = {"jobnum": [1, 1], "nstep": 480000}
        self.assertEqual(self.auto.continuation_problems(self.config, parent, child), [])
        child["params"]["species"][1]["wp"] = 0.1
        problems = self.auto.continuation_problems(self.config, parent, child)
        self.assertIn("species.1.wp", problems[0])
        self.assertNotIn("jobcon", problems[0].split(": ", 1)[1])
        self.assertEqual(self.auto.flat_params({"run.batch_count": 3, "a": {"b": [1, 2]}}),
                         {"run.batch_count": 3, "a.b": [1, 2]})
        # grain: run.steps is cumulative, the restart file and output settings may change
        grain = {"simulator": {"adapter": "grain"}, "job": {"processes": 1, "cores": 4, "walltime": "4:00:00"},
                 "params": {"run": {"steps": 100000, "dt": 1e-6}, "seed": 3}}
        longer = copy.deepcopy(grain)
        longer["params"]["run"]["steps"] = 300000
        longer["params"]["checkpoint"] = {"restart": "restart.bin"}
        self.assertEqual(self.auto.continuation_problems(self.config, grain, longer), [])
        longer["params"]["run"]["dt"] = 2e-6
        self.assertIn("run.dt", self.auto.continuation_problems(self.config, grain, longer)[0])

    # ------------------------------------------------------------ the gate

    def test_the_gate_submits_only_runs_auto_created(self) -> None:
        self.add_run("R1", by="agent")
        _, problems = self.auto.gate(self.config, "demo", self.project, ["R1"], "derive")
        self.assertTrue(any("created by agent" in p for p in problems), problems)

    def test_a_follow_up_is_capped_at_derived_per_run(self) -> None:
        self.add_run("R1", job={"walltime": "4:00:00"})  # 112 cores x 4 h = 448
        self.add_run("R2", job={"walltime": "5:00:00"})  # 560
        plans, problems = self.auto.gate(self.config, "demo", self.project, ["R1"], "derive")
        self.assertEqual(problems, [])
        self.assertEqual(plans[0]["reserved_core_h"], 448)
        _, problems = self.auto.gate(self.config, "demo", self.project, ["R2"], "derive")
        self.assertTrue(any("derived_per_run" in p for p in problems), problems)

    def test_a_continuation_may_use_no_more_than_its_parent_used(self) -> None:
        self.add_run("P", status="completed", by="human", job={"processes": 1, "cores": 100, "threads": 100},
                     attempts=self.finished(48, hours=1.0))  # used 100 core-h
        self.add_run("C1", parent="P", params={"run.batch_count": 200},
                     job={"processes": 1, "cores": 100, "threads": 100, "walltime": "2:00:00"})
        _, problems = self.auto.gate(self.config, "demo", self.project, ["C1"], "continue")
        self.assertTrue(any("continuation limit 100" in p for p in problems), problems)
        self.runs.pop()
        self.add_run("C2", parent="P", params={"run.batch_count": 200},
                     job={"processes": 1, "cores": 100, "threads": 100, "walltime": "1:00:00"})
        _, problems = self.auto.gate(self.config, "demo", self.project, ["C2"], "continue")
        self.assertEqual(problems, [])

    def test_a_parent_with_a_running_child_gets_no_second_continuation(self) -> None:
        self.add_run("P", status="completed", by="human", attempts=self.finished(48, hours=10))
        self.add_run("H", status="running", parent="P", by="human", params={"run.batch_count": 300})
        self.add_run("C", parent="P", params={"run.batch_count": 200})
        _, problems = self.auto.gate(self.config, "demo", self.project, ["C"], "continue")
        self.assertTrue(any("already has a waiting or running child (H)" in p for p in problems), problems)

    def test_a_derived_input_cannot_pass_as_a_continuation(self) -> None:
        self.add_run("P", status="completed", by="human", attempts=self.finished(48, hours=10))
        self.add_run("C", parent="P", params={"run.batch_count": 100, "seed": 2})
        _, problems = self.auto.gate(self.config, "demo", self.project, ["C"], "continue")
        self.assertTrue(any("seed" in p for p in problems), problems)

    def test_the_chain_of_auto_continuations_is_limited(self) -> None:
        self.add_run("H", status="completed", by="human", attempts=self.finished(90, hours=50))
        parent = "H"
        for i in range(3):
            self.add_run(f"A{i}", status="completed", parent=parent, params={"run.batch_count": 100 + i},
                         attempts=self.finished(80 - 10 * i, hours=5))
            parent = f"A{i}"
        self.add_run("A3", parent=parent, params={"run.batch_count": 200})
        _, problems = self.auto.gate(self.config, "demo", self.project, ["A3"], "continue")
        self.assertTrue(any("max_auto_chain" in p for p in problems), problems)

    def test_the_weekly_partition_and_core_limits_apply(self) -> None:
        self.config["budget"]["partition"] = {"gr20001a": 100}
        self.add_run("R1")  # 112 core-h
        _, problems = self.auto.gate(self.config, "demo", self.project, ["R1"], "derive")
        self.assertTrue(any("gr20001a would reach 112 of 100" in p for p in problems), problems)
        self.add_run("R2", job={"partition": "elsewhere"})
        _, problems = self.auto.gate(self.config, "demo", self.project, ["R2"], "derive")
        self.assertTrue(any("has no budget" in p for p in problems), problems)
        self.config["budget"]["max_cores"] = 100
        _, problems = self.auto.gate(self.config, "demo", self.project, ["R1"], "derive")
        self.assertTrue(any("max_cores" in p for p in problems), problems)

    def test_nothing_goes_to_a_floor_partition_when_the_allocation_runs_low(self) -> None:
        self.auto.allocation = lambda fresh=True: {"at": "x", "allocated": 100.0, "remaining": 5.0}
        self.add_run("R1")
        _, problems = self.auto.gate(self.config, "demo", self.project, ["R1"], "derive")
        self.assertTrue(any("floor_fraction" in p for p in problems), problems)

    def test_a_paused_auto_or_a_held_project_submits_nothing(self) -> None:
        self.add_run("R1")
        self.auto.set_control(want="paused")
        _, problems = self.auto.gate(self.config, "demo", self.project, ["R1"], "derive")
        self.assertIn("hub auto is paused", problems)
        self.auto.set_control(want="run")
        self.auto.write_json(self.auto.AUTO / "holds.json", {"demo": {"why": "rethinking"}})
        _, problems = self.auto.gate(self.config, "demo", self.project, ["R1"], "derive")
        self.assertTrue(any("rethinking" in p for p in problems), problems)

    def test_a_submission_records_its_job_id_or_its_failure(self) -> None:
        answer = json.dumps([{"id": "R1", "job_id": "9", "result": "submitted"},
                             {"id": "R2", "job_id": "", "result": "not_submitted", "error": "boom"}])
        calls = []

        def fake_runo(project, *args, **kw):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0 if args[0] != "submit" else 1, answer if args[0] == "submit" else "", "")

        self.auto.runo = fake_runo
        plans = [{"project": "demo", "run": rid, "run_dir": f"runs/{rid}", "kind": "derive", "partition": "gr20001a",
                  "cores": 8, "walltime_h": 1, "reserved_core_h": 8, "parent": ""} for rid in ("R1", "R2")]
        events = self.auto.submit_plans("demo", self.project, plans, "T1", "basis")
        self.assertEqual([(e["event"], e.get("job_id"), e.get("error")) for e in events],
                         [("submit", "9", None), ("submit-failed", None, "boom")])
        self.assertEqual(calls[0][:3], ("submit", "R1", "R2"))
        self.assertEqual(calls[1][:2], ("note", "R1"))
        self.assertEqual([e["event"] for e in self.auto.ledger()], ["planned", "planned", "submit", "submit-failed"])

    def test_proposals_questions_and_releases_from_the_command_line(self) -> None:
        os.environ.update(HUB_AUTO_PROJECT="demo", HUB_AUTO_TICK="T1")
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("HUB_AUTO_PROJECT", "HUB_AUTO_TICK")])
        quiet = open(os.devnull, "w")
        self.addCleanup(quiet.close)
        with unittest.mock.patch("sys.stdout", quiet):
            self.assertEqual(self.auto.main(["propose", "--kind", "derive", "--title", "seed 2", "--why", "w",
                                             "--core-h", "300"]), 0)
            self.assertEqual(self.auto.main(["propose", "--kind", "other", "--title", "x", "--why", "w"]), 2)
            self.assertEqual(self.auto.main(["defer", "R1", "--why", "質問: 10 eV?"]), 0)
            self.assertIn("R1", self.auto.deferred(self.auto.ledger(), "demo"))
            self.assertEqual(self.auto.main(["release", "demo/R1"]), 0)
            self.assertNotIn("R1", self.auto.deferred(self.auto.ledger(), "demo"))
            self.assertEqual(self.auto.main(["approve", "1", "--yes"]), 0)
            self.assertEqual(self.auto.main(["status"]), 0)
        self.assertEqual(self.auto.proposal(1)["status"], "accepted")
        work = self.auto.worklist(self.config, "demo", self.project, [], [], "T2")
        self.assertEqual([p["id"] for p in work["proposals"]], [1])

    # ------------------------------------------------------------ the budget

    def test_the_week_counts_finished_jobs_by_use_and_the_rest_by_walltime(self) -> None:
        done = self.add_run("D", status="completed", job={"processes": 1, "cores": 10, "threads": 10},
                            attempts=self.finished(2, hours=1.0, job_id="11"))
        live = self.add_run("L", status="running", job={"processes": 1, "cores": 10, "threads": 10},
                            attempts=[{"job_id": "12", "started_at": self.auto.stamp(), "finished_at": ""}])
        base = {"event": "submit", "project": "demo", "partition": "gr20001a", "cores": 10}
        old = self.auto.stamp(self.auto.now() - timedelta(days=8))
        events = [
            {**base, "kind": "continue", "run": "D", "run_dir": done["run_dir"], "job_id": "11", "reserved_core_h": 100},
            {**base, "kind": "derive", "run": "L", "run_dir": live["run_dir"], "job_id": "12", "reserved_core_h": 50},
            {**base, "kind": "derive", "run": "X", "run_dir": "", "job_id": "13", "reserved_core_h": 70, "at": old},
            {**base, "kind": "approved", "run": "Y", "run_dir": "", "job_id": "14", "reserved_core_h": 1000},
        ]
        events = [{"at": self.auto.stamp(), **e} for e in events]
        spent = self.auto.usage(self.config, events)
        self.assertAlmostEqual(spent["total"], 60, places=3)
        self.assertAlmostEqual(spent["derived"], 50, places=3)
        self.assertEqual(spent["approved"], 1000)
        self.assertEqual(spent["active_cores"], 10)

    # ------------------------------------------------------------ the sbatch wrapper

    def test_job_requests_read_directives_and_command_line_options(self) -> None:
        script = self.project / "plot.sh"
        script.write_text("#!/bin/bash\n#SBATCH -p gr10451a\n#SBATCH --rsc p=1:t=8:c=8\n"
                          "#SBATCH -t 00:30:00\npython plot.py\n#SBATCH -p ignored\n", encoding="utf-8")
        request = self.auto.job_request("sbatch", [str(script)])
        self.assertEqual((request["partition"], request["cores"], request["hours"]), ("gr10451a", 8, 0.5))
        self.assertEqual(self.auto.analysis_problems(self.config, request), [])
        request = self.auto.job_request("sbatch", ["-p", "gr20001a", "--rsc=p=4:t=16:c=16", str(script)])
        problems = self.auto.analysis_problems(self.config, request)
        self.assertEqual(len(problems), 2, problems)
        request = self.auto.job_request("tssrun", ["-p", "gr10451a", "--rsc", "p=1:t=4:c=4", "-t", "2:00", "python", "x.py"])
        self.assertTrue(any("2 h" in p for p in self.auto.analysis_problems(self.config, request)))

    def test_a_runs_own_job_script_is_refused(self) -> None:
        run = self.add_run("R1")
        job = self.project / run["run_dir"] / "submit" / "job.sh"
        job.parent.mkdir()
        job.write_text("#!/bin/bash\n#SBATCH -p gr10451a\n#SBATCH -t 0:10:00\n", encoding="utf-8")
        problems = self.auto.analysis_problems(self.config, self.auto.job_request("sbatch", [str(job)]))
        self.assertTrue(any("hub auto submit" in p for p in problems), problems)

    def test_the_wrapper_records_an_analysis_job_and_refuses_a_large_one(self) -> None:
        bin_dir = Path(self.hub.parent) / "fakebin"
        bin_dir.mkdir()
        fake = bin_dir / "sbatch"
        fake.write_text("#!/bin/sh\necho \"Submitted batch job 4242\"\n", encoding="utf-8")
        fake.chmod(0o755)
        shims = self.auto.write_shims()
        env = {**os.environ, "PATH": f"{shims}:{bin_dir}:{os.environ['PATH']}", "RUNOPS_HUB_DIR": str(self.hub),
               "HUB_AUTO_TICK": "T1", "HUB_AUTO_PROJECT": "demo"}
        ok = subprocess.run(["sbatch", "-p", "gr10451a", "--rsc", "p=1:t=4:c=4", "-t", "0:20:00", "--wrap", "true"],
                            capture_output=True, text=True, env=env)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("4242", ok.stdout)
        big = subprocess.run(["sbatch", "-p", "gr20001a", "--rsc", "p=8:t=14:c=14", "--wrap", "true"],
                             capture_output=True, text=True, env=env)
        self.assertEqual(big.returncode, 1)
        self.assertIn("refused", big.stderr)
        events = [e for e in self.auto.ledger() if e.get("kind") == "analysis"]
        self.assertEqual([(e["job_id"], e["cores"]) for e in events], [("4242", 4)])

    # ------------------------------------------------------------ claims, control, config

    def test_claims_expire_and_belong_to_their_holder(self) -> None:
        self.auto.claim("demo", "R1", "T1", hours=1)
        self.assertEqual(self.auto.claimed_by_other("demo", "R1", "T1"), "")
        self.assertIn("claimed by T1", self.auto.claimed_by_other("demo", "R1", "T2"))
        self.auto.claim("demo", "R2", "T1", hours=-1)
        self.assertEqual([c["run"] for c in self.auto.claims()], ["R1"])
        self.assertEqual(self.auto.release("demo", holder="T1"), 1)

    def test_control_lives_in_a_file_and_the_loop_lock_shows_a_live_loop(self) -> None:
        self.assertFalse(self.auto.loop_alive())
        handle = self.auto.try_lock("loop.lock")
        self.assertTrue(self.auto.loop_alive())
        self.auto.unlock(handle)
        self.auto.set_control(want="paused", now=True)
        self.assertEqual((self.auto.control()["want"], self.auto.control()["now"]), ("paused", True))

    def test_enable_and_disable_keep_the_rest_of_the_config(self) -> None:
        self.auto.set_project_table("other", {"analyze": True, "continue": False, "derive": False})
        self.auto.set_project_table("demo", {"analyze": True, "continue": True, "derive": False, "total": 5000})
        config = self.auto.load_config()
        self.assertEqual(self.auto.enabled(config), ["demo", "other"])
        self.assertEqual(self.auto.project_settings(config, "demo")["total"], 5000)
        self.assertFalse(self.auto.project_settings(config, "demo")["derive"])
        self.auto.set_project_table("demo", None)
        config = self.auto.load_config()
        self.assertEqual(self.auto.enabled(config), ["other"])
        self.assertEqual(config["budget"]["total"], 50000)
        self.assertIn("# 対象の project", (self.auto.AUTO / "config.toml").read_text(encoding="utf-8"))

    def test_the_member_note_goes_into_the_hub_section(self) -> None:
        agents = self.project / "AGENTS.md"
        agents.write_text("# p\n\n## runops hub\n\n- first rule; hub auto commits after each tick\n\n"
                          "## local rules\n\n- keep\n", encoding="utf-8")
        self.assertTrue(self.auto.add_member_note(self.project))
        self.assertFalse(self.auto.add_member_note(self.project))
        text = agents.read_text(encoding="utf-8")
        self.assertLess(text.index("hub auto may visit"), text.index("## local rules"))
        self.assertGreater(text.index("hub auto may visit"), text.index("- first rule"))

    # ------------------------------------------------------------ worklist and tick

    def write_log(self) -> None:
        (self.project / "LOG.md").write_text("# Log\n\n## 2026-01-01 — start\n", encoding="utf-8")

    def test_the_worklist_waits_out_quiet_hours_and_skips_what_it_must_leave(self) -> None:
        self.write_log()
        self.add_run("fresh", status="completed", attempts=self.finished(1))
        self.add_run("ready", status="completed", attempts=self.finished(30))
        self.add_run("asked", status="completed", attempts=self.finished(30))
        self.add_run("tried", status="completed", attempts=self.finished(30))
        self.add_run("kept", status="completed", attempts=self.finished(30))
        self.add_run("busy", status="completed", attempts=self.finished(30))
        self.add_run("broken", status="failed", attempts=self.finished(30))
        self.add_run("retry", status="completed", parent="broken", attempts=self.finished(20))
        events = [
            {"event": "defer", "project": "demo", "run": "asked", "why": "質問"},
            {"event": "work", "project": "demo", "runs": ["tried"]},
            {"event": "work", "project": "demo", "runs": ["tried"]},
        ]
        self.auto.write_json(self.auto.AUTO / "holds.json", {"demo/kept": {"why": "mine"}})
        self.auto.claim("demo", "busy", "someone", hours=1)
        work = self.auto.worklist(self.config, "demo", self.project, self.runs, events, "T1")
        self.assertEqual([r["id"] for r in work["runs"]], ["ready", "retry"])
        reasons = {s["id"]: s["reason"] for s in work["skipped"]}
        self.assertEqual(set(reasons), {"fresh", "asked", "tried", "kept", "busy"})
        self.assertIn("quiet_hours", reasons["fresh"])

    def test_a_planned_submission_is_settled_by_the_next_tick(self) -> None:
        self.add_run("S", status="submitted")
        self.runs[-1]["job_id"] = "77"
        self.add_run("N")
        events = [
            {"event": "planned", "project": "demo", "run": "S", "tick": "T0", "kind": "derive"},
            {"event": "planned", "project": "demo", "run": "N", "tick": "T0", "kind": "derive"},
        ]
        notes = self.auto.reconcile("demo", self.runs, events)
        self.assertEqual(len(notes), 2)
        recorded = {e["run"]: e for e in self.auto.ledger()}
        self.assertEqual((recorded["S"]["event"], recorded["S"]["job_id"]), ("submit", "77"))
        self.assertEqual(recorded["N"]["event"], "abandoned")

    def test_auto_yields_when_a_person_runs_the_same_continuation(self) -> None:
        self.add_run("P", status="completed", by="human")
        self.add_run("A", status="running", parent="P", params={"run.batch_count": 200})
        self.add_run("H", status="running", parent="P", by="human", params={"run.batch_count": 300})
        calls = []
        self.auto.runo = lambda project, *args, **kw: calls.append(args) or subprocess.CompletedProcess(args, 0, "", "")
        events = [{"event": "submit", "project": "demo", "run": "A", "kind": "continue", "at": "2026-01-01T00:00:00+09:00"}]
        notes = self.auto.cancel_duplicates(self.config, "demo", self.project, self.runs, events)
        self.assertEqual(calls[0][:2], ("cancel", "A"))
        self.assertIn("another continuation of P", notes[0])

    def configure_fake_agent(self, sleep: float) -> None:
        agent = Path(self.hub.parent) / "fake_agent.py"
        agent.write_text(FAKE_AGENT, encoding="utf-8")
        os.environ["FAKE_AGENT_SLEEP"] = str(sleep)
        self.addCleanup(os.environ.pop, "FAKE_AGENT_SLEEP", None)
        self.config["auto"]["runner"] = [sys.executable, str(agent)]

    def test_an_agent_is_stopped_at_its_time_limit_or_on_pause_now(self) -> None:
        self.configure_fake_agent(sleep=30)
        self.config["auto"]["agent_timeout_min"] = 0.01
        tick_dir = self.auto.AUTO / "ticks" / "T1"
        tick_dir.mkdir(parents=True)
        started = time.monotonic()
        result = self.auto.run_agent(self.config, "demo", self.project, "T1", tick_dir, "prompt",
                                     lambda: "", lambda **_: None)
        self.assertEqual(result["reason"], "timeout")
        self.assertLess(time.monotonic() - started, 20)
        self.config["auto"]["agent_timeout_min"] = 10
        result = self.auto.run_agent(self.config, "demo", self.project, "T1", tick_dir, "prompt",
                                     lambda: "pause --now", lambda **_: None)
        self.assertEqual(result["reason"], "pause --now")

    def test_a_tick_hands_unwritten_runs_to_the_agent_and_reports(self) -> None:
        self.write_log()
        self.add_run("ready", status="completed", attempts=self.finished(30))
        self.configure_fake_agent(sleep=0)
        result = self.auto.run_tick(self.config, sync=False, say=lambda _: None)
        self.assertEqual((result["result"], result["agents"], result["failures"]), ("ok", 1, 0))
        report = Path(result["report"]).read_text(encoding="utf-8")
        self.assertIn("### demo — agent exit 0", report)
        self.assertIn("fake agent was here", report)
        self.assertEqual(self.auto.claims(), [])
        prompt = next((self.auto.AUTO / "ticks").glob("T*/demo.prompt.md")).read_text(encoding="utf-8")
        self.assertIn('"id": "ready"', prompt)
        self.assertNotIn("$worklist", prompt)
        again = self.auto.run_tick(self.config, sync=False, say=lambda _: None)
        self.assertEqual(again["agents"], 1)  # still unwritten: a second try
        third = self.auto.run_tick(self.config, sync=False, say=lambda _: None)
        self.assertEqual(third["agents"], 0)  # given up after give_up_after tries

    def test_the_loop_follows_the_control_file(self) -> None:
        self.auto.set_project_table("demo", None)  # an empty tick; the loop itself is under test
        env = {**os.environ, "RUNOPS_HUB_DIR": str(self.hub), "HUB_AUTO_POLL": "0.1"}
        env.pop("TMUX", None)
        loop = subprocess.Popen([sys.executable, str(REPO / "lib" / "auto.py"), "loop"], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: loop.poll() is None and loop.kill())

        def wait_for(state: str) -> dict:
            for _ in range(100):
                beat = self.auto.heartbeat()
                if beat.get("state") == state and beat.get("pid") == loop.pid:
                    return beat
                time.sleep(0.1)
            self.fail(f"loop never reached {state}: {self.auto.heartbeat()}")

        wait_for("waiting")
        second = subprocess.run([sys.executable, str(REPO / "lib" / "auto.py"), "loop"], env=env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(second.returncode, 1)
        self.auto.set_control(want="paused")
        wait_for("paused")
        self.auto.set_control(want="run")
        wait_for("waiting")
        self.auto.set_control(want="stopped")
        self.assertEqual(loop.wait(timeout=30), 0)
        self.assertEqual(self.auto.heartbeat()["state"], "stopped")
        self.assertFalse(self.auto.loop_alive())


if __name__ == "__main__":
    unittest.main()


class AutoCommitTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.hub = Path(tmp.name) / "hub"
        (self.hub / ".hub").mkdir(parents=True)
        self.auto = load("auto", self.hub)
        self.project = Path(tmp.name) / "project"
        self.project.mkdir()
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        patcher = unittest.mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.git("init", "-q")
        (self.project / "LOG.md").write_text("# Log\n")
        (self.project / "notes.md").write_text("draft\n")
        self.git("add", "LOG.md")
        self.git("commit", "-q", "-m", "start")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.project), *args], capture_output=True, text=True,
                              check=True).stdout

    def test_commits_only_what_the_agent_changed(self) -> None:
        before = self.auto.git_changes(self.project)
        self.assertEqual(set(before), {"notes.md"})
        time.sleep(0.01)
        (self.project / "LOG.md").write_text("# Log\n\n## [auto] result\n")
        (self.project / "analysis").mkdir()
        (self.project / "analysis" / "fig[1].png").write_bytes(b"png")
        line = self.auto.commit_agent_changes(self.project, "T1", before)
        self.assertTrue(line.startswith("git commit "), line)
        self.assertIn("(2 files)", line)
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(), "[auto] T1 の記録")
        self.assertEqual(self.git("status", "--porcelain").split(), ["??", "notes.md"])

    def test_does_nothing_outside_a_repository_or_without_changes(self) -> None:
        plain = self.project.parent / "plain"
        plain.mkdir()
        self.assertIsNone(self.auto.git_changes(plain))
        self.assertEqual(self.auto.commit_agent_changes(plain, "T1", None), "")
        before = self.auto.git_changes(self.project)
        self.assertEqual(self.auto.commit_agent_changes(self.project, "T1", before), "")
