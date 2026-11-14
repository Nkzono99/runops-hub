from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime

from helpers import load, member

TABLE = """# 解析

<!-- board -->
### 高さの走査

- 論文: `papers/sheath`

| 問い | いまの答え | 図 | 対象 run | 読む場所 | 確認日 |
|---|---|---|---|---|---|
| 高さは効くか | 効かない。差は 0.4% 以内。 | ![帯が目安](scan/out/a.png) | `runs/scan/coupled` R20261105-0009 | [結果](scan/README.md) | 2026-11-06 12:00 |
| 幅は効くか | 0.25 s で収束する。 | | `runs/scan/width/` | [結果](scan/README.md) | 2026-11-06 |
| 接合なしでも同じか | | | `runs/scan/open/` | | |

### 別の線

| 問い | いまの答え | 図 | 対象 run | 読む場所 | 確認日 |
|---|---|---|---|---|---|
| 配置は効くか | | | | | |
<!-- /board -->

| 問い | board の外の表 |
|---|---|
"""


def run(run_id: str, directory: str, status: str, finished: str = "", **more: object) -> dict:
    return {"id": run_id, "run_dir": f"{directory}/{run_id}--x", "status": status, "finished_at": finished, **more}


class BoardTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.hub = Path(tmp.name) / "hub"
        (self.hub / ".hub").mkdir(parents=True)
        self.board = load("board", self.hub)
        self.project = member(self.hub, "demo")

    def test_the_table_is_read_as_lines_of_work_with_rows(self) -> None:
        lines = self.board.parse_board(TABLE)
        self.assertEqual([line["title"] for line in lines], ["高さの走査", "別の線"])
        self.assertEqual(lines[0]["meta"]["論文"], "`papers/sheath`")
        self.assertEqual([row["問い"] for row in lines[0]["rows"]], ["高さは効くか", "幅は効くか", "接合なしでも同じか"])
        self.assertEqual(len(lines[1]["rows"]), 1)
        self.assertEqual(self.board.parse_board("# no table\n"), [])

    def test_a_row_without_an_answer_is_an_open_question(self) -> None:
        rows = self.board.parse_board(TABLE)[0]["rows"]
        self.assertEqual([self.board.is_open(row) for row in rows], [False, False, True])

    def test_optional_project_title_preserves_existing_tables(self) -> None:
        self.assertEqual(self.board.project_title(TABLE, "demo"), "demo")
        titled = TABLE.replace("<!-- board -->", "<!-- board -->\n- 表示名: シースの柱")
        self.assertEqual(self.board.project_title(titled, "demo"), "シースの柱")
        self.assertEqual(self.board.parse_board(titled), self.board.parse_board(TABLE))

    def test_multiple_projects_have_distinct_navigation_and_reading_order(self) -> None:
        for name in ("demo", "second"):
            project = self.project if name == "demo" else member(self.hub, name)
            analysis = project / "analysis"
            analysis.mkdir(parents=True)
            (analysis / "README.md").write_text(TABLE, encoding="utf-8")
        with patch.object(self.board, "load_runs", return_value=[]), patch.object(self.board.st, "HUB", self.hub):
            page, stale = self.board.build(self.hub, datetime.now().astimezone())
        self.assertEqual(stale, 0)
        for name in ("demo", "second"):
            self.assertIn(f'href="#project-{name}"', page)
            self.assertEqual(page.count(f'id="project-{name}"'), 1)
        self.assertLess(page.index('class="lead"'), page.index("<figcaption>"))
        self.assertLess(page.index("<figcaption>"), page.index('<img src='))
        self.assertLess(page.index('<img src='), page.index("差は 0.4% 以内。"))
        self.assertIn('aria-label="図を拡大:', page)

    def test_a_row_selects_runs_by_directory_and_by_id(self) -> None:
        row = self.board.parse_board(TABLE)[0]["rows"][0]
        runs = [
            run("R20261105-0001", "runs/scan/coupled", "running"),
            run("R20261105-0002", "runs/scan/coupled-old", "running"),
            run("R20261105-0009", "runs/other", "submitted"),
        ]
        picked = self.board.row_state(row, runs)["runs"]
        self.assertEqual([r["id"] for r in picked], ["R20261105-0001", "R20261105-0009"])

    def test_a_run_completed_after_the_stated_time_makes_the_answer_stale(self) -> None:
        timed, dated, _ = self.board.parse_board(TABLE)[0]["rows"]
        before = run("R20261105-0001", "runs/scan/coupled", "completed", "2026-11-06T11:00:00+09:00")
        after = run("R20261105-0002", "runs/scan/coupled", "completed", "2026-11-06T13:00:00+09:00")
        failed = run("R20261105-0003", "runs/scan/coupled", "failed", "2026-11-07T13:00:00+09:00")
        self.assertEqual(self.board.row_state(timed, [before, after, failed])["newer"], [after])
        same_day = run("R20261105-0004", "runs/scan/width", "completed", "2026-11-06T20:00:00+09:00")
        next_day = run("R20261105-0005", "runs/scan/width", "completed", "2026-11-07T00:10:00+09:00")
        self.assertEqual(self.board.row_state(dated, [same_day, next_day])["newer"], [next_day])

    def test_links_are_rewritten_from_the_hub_and_get_the_html_report(self) -> None:
        topic = self.project / "analysis" / "scan"
        topic.mkdir(parents=True)
        (topic / "report.html").write_text("<p>", encoding="utf-8")
        out = self.board.inline("[結果](scan/README.md) と [ノート](../notes/a.md) <b>", "demo/analysis", self.hub)
        self.assertIn('href="demo/analysis/scan/README.md"', out)
        self.assertIn('href="demo/analysis/scan/report.html"', out)
        self.assertIn('href="demo/notes/a.md"', out)
        self.assertIn("&lt;b&gt;", out)

    def test_a_figure_cell_gives_how_to_read_it_and_the_path(self) -> None:
        self.assertEqual(self.board.figure_of("![線が帯を越えると離れる](scan/out/a.png)"), ("線が帯を越えると離れる", "scan/out/a.png"))
        self.assertEqual(self.board.figure_of(""), ("", ""))

    def test_the_first_sentence_of_an_answer_is_the_conclusion(self) -> None:
        self.assertEqual(self.board.split_answer("効かない。差は 0.4% 以内。定常ではない。"), ("効かない。", "差は 0.4% 以内。定常ではない。"))
        self.assertEqual(self.board.split_answer("効かない"), ("効かない", ""))

    def test_a_line_shows_findings_with_figures_and_lists_open_questions(self) -> None:
        out = self.project / "analysis" / "scan" / "out"
        out.mkdir(parents=True)
        (out / "a.png").write_bytes(b"\x89PNG")
        line = self.board.parse_board(TABLE)[0]
        runs = [
            run("R20261105-0002", "runs/scan/coupled", "completed", "2026-11-06T13:00:00+09:00"),
            run("R20261105-0007", "runs/scan/open", "running"),
        ]
        page, stale = self.board.render_line(line, runs, "demo/analysis", self.hub)
        self.assertEqual(stale, 1)
        self.assertIn('<p class="lead">効かない。</p>', page)
        self.assertIn('src="data:image/png;base64,', page)
        self.assertIn("帯が目安", page)
        self.assertIn("この答えのあとに完了した計算が 1 本", page)
        self.assertIn("まだ分かっていないこと", page)
        self.assertIn('接合なしでも同じか<span class="tag">計算中</span>', page)

    def test_an_open_question_whose_runs_finished_waits_for_analysis(self) -> None:
        line = self.board.parse_board(TABLE)[0]
        runs = [run("R20261105-0007", "runs/scan/open", "completed", "2026-11-06T13:00:00+09:00")]
        page, _ = self.board.render_line(line, runs, "demo/analysis", self.hub)
        self.assertIn('接合なしでも同じか<span class="tag">計算は完了、解析待ち</span>', page)


if __name__ == "__main__":
    unittest.main()
