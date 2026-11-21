from __future__ import annotations

import re
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
            self.assertIn(f'href="#p-{name}"', page)
            self.assertEqual(page.count(f'id="p-{name}"'), 1)
            self.assertIn(f'href="#f-{name}-1"', page)
            self.assertEqual(page.count(f'id="f-{name}-1"'), 1)
        card = page[page.index('id="f-demo-1"'):]
        self.assertLess(card.index('class="headline"'), card.index('<img src='))
        self.assertLess(card.index('<img src='), card.index("差は 0.4% 以内。"))
        self.assertLess(card.index("差は 0.4% 以内。"), card.index('<p class="howto">'))
        self.assertIn("帯が目安", card[card.index('<p class="howto">'):])
        self.assertIn('aria-label="図を拡大:', page)
        self.assertIn("分かっていること <span class=\"count\">4</span> 件", page)

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
        out = self.board.inline("[結果](scan/README.md) と [ノート](../notes/a.md) <b>", "projects/demo/analysis", self.hub)
        self.assertIn('href="projects/demo/analysis/scan/README.md"', out)
        self.assertIn('href="projects/demo/analysis/scan/report.html"', out)
        self.assertIn('href="projects/demo/notes/a.md"', out)
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
        page, stale = self.board.render_line(line, runs, "projects/demo/analysis", self.hub)
        self.assertEqual(stale, 1)
        self.assertIn('<h4 class="headline">効かない。</h4>', page)
        self.assertIn('<p class="point">差は 0.4% 以内。</p>', page)
        self.assertIn('src="data:image/png;base64,', page)
        self.assertIn("帯が目安", page)
        self.assertIn("この答えのあとに完了した計算が 1 本", page)
        self.assertIn("まだ分かっていないこと", page)
        self.assertIn('接合なしでも同じか<span class="tag">計算中</span>', page)

    def test_an_open_question_whose_runs_finished_waits_for_analysis(self) -> None:
        line = self.board.parse_board(TABLE)[0]
        runs = [run("R20261105-0007", "runs/scan/open", "completed", "2026-11-06T13:00:00+09:00")]
        page, _ = self.board.render_line(line, runs, "projects/demo/analysis", self.hub)
        self.assertIn('接合なしでも同じか<span class="tag">計算は完了、解析待ち</span>', page)

    def test_the_short_columns_make_the_card_and_the_full_answer_is_folded(self) -> None:
        short = TABLE.replace(
            "| 問い | いまの答え | 図 | 対象 run | 読む場所 | 確認日 |\n|---|---|---|---|---|---|\n| 高さは効くか |",
            "| 問い | 見出し | 要点 | 条件 | いまの答え | 図 | 対象 run | 読む場所 | 確認日 |\n"
            "|---|---|---|---|---|---|---|---|---|\n| 高さは効くか | 高さは電荷を変えない | 差は **0.4% 以内** | 単一 seed、定常前 |", 1)
        line = self.board.parse_board(short)[0]
        self.assertEqual(line["rows"][0]["見出し"], "高さは電荷を変えない")
        page, _ = self.board.render_line(line, [], "projects/demo/analysis", self.hub, ["f-demo-1"])
        self.assertIn('<h4 class="headline">高さは電荷を変えない</h4>', page)
        self.assertIn('<p class="point">差は <strong>0.4% 以内</strong></p>', page)
        self.assertIn('<p class="cond">単一 seed、定常前</p>', page)
        folded = page[page.index("<details"):page.index("</details>")]
        self.assertIn("効かない。差は 0.4% 以内。", folded)
        self.assertIn("R20261105-0009", folded)
        self.assertIn('id="f-demo-1"', page)
        # a row without the short columns keeps its first sentence as the headline
        self.assertIn('<h4 class="headline">0.25 s で収束する。</h4>', page)

    def test_the_setting_is_read_before_the_lines_and_under_a_line(self) -> None:
        text = TABLE.replace("<!-- board -->", "<!-- board -->\n- 表示名: シースの柱\n- 模式図: ![柱と上端](setting/s.png)\n\n"
                             "| 計算の設定 | 値 |\n|---|---|\n| コード | 3D EMSES |\n| 格子 | 0.5 m |\n")
        text = text.replace("- 論文: `papers/sheath`", "- 論文: `papers/sheath`\n\n| 設定 | 値 |\n|---|---|\n| 接合 | あり、2 s/batch |\n")
        self.assertEqual(self.board.preamble(text), {"表示名": "シースの柱", "模式図": "![柱と上端](setting/s.png)"})
        self.assertEqual(self.board.preamble_setting(text), [("コード", "3D EMSES"), ("格子", "0.5 m")])
        line = self.board.parse_board(text)[0]
        self.assertEqual(line["setting"], [("接合", "あり、2 s/batch")])
        self.assertEqual([row["問い"] for row in line["rows"]], ["高さは効くか", "幅は効くか", "接合なしでも同じか"])
        page, _ = self.board.render_line(line, [], "projects/demo/analysis", self.hub)
        self.assertIn('<th scope="row">接合</th><td>あり、2 s/batch</td>', page)
        self.assertLess(page.index('class="paper"'), page.index('class="setting"'))
        out = self.project / "analysis" / "setting"
        out.mkdir(parents=True)
        header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR"
        (out / "s.png").write_bytes(header + (900).to_bytes(4, "big") + (1000).to_bytes(4, "big"))
        block = self.board.render_setting(self.board.preamble(text), self.board.preamble_setting(text), "計算の設定",
                                          "projects/demo/analysis", self.hub)
        self.assertIn('<div class="setting side">', block)
        self.assertIn('data-title="計算の設定"', block)
        self.assertLess(block.index("<img"), block.index('<th scope="row">コード</th>'))
        self.assertEqual(self.board.render_setting({}, [], "計算の設定", "projects/demo/analysis", self.hub), "")

    def test_the_overview_lists_claims_then_open_questions_in_the_hub_table_order(self) -> None:
        second = member(self.hub, "second")
        for project in (self.project, second):
            (project / "analysis").mkdir(parents=True)
            (project / "analysis" / "README.md").write_text(TABLE, encoding="utf-8")
        (self.hub / "AGENTS.md").write_text(
            "| dir | simulator | 内容 |\n|---|---|---|\n| `projects/second/` | emses | 二つ目 |\n| `projects/demo/` | beach | 一つ目 |\n",
            encoding="utf-8")
        with patch.object(self.board, "load_runs", return_value=[]), patch.object(self.board.st, "HUB", self.hub):
            page, _ = self.board.build(self.hub, datetime.now().astimezone())
        start = page.index('<nav class="overview"')
        overview = page[start:page.index("</nav>", start)]
        self.assertLess(overview.index('<span class="tile-name">second</span>'), overview.index('<span class="tile-name">demo</span>'))
        tile = overview[overview.index('<span class="tile-name">demo</span>'):]
        self.assertTrue(overview.startswith('<nav class="overview" aria-label="テーマごとの結論"><details class="tile"><summary>'))
        self.assertIn("分かっていること 2・まだ 2", tile[:tile.index("</summary>")])
        self.assertLess(tile.index('<a href="#f-demo-1">効かない。</a>'), tile.index('<li class="ask">接合なしでも同じか'))
        self.assertIn('href="#p-demo"', tile)
        self.assertIn("二つ目", overview)
        # one tab per theme after the overview, in the same order; each theme is a view with links to its neighbours
        start = page.index('<nav class="tabs"')
        tabs = page[start:page.index("</nav>", start)]
        self.assertEqual(re.findall(r'data-tab="([^"]+)"', tabs), ["overview", "second", "demo"])
        self.assertIn('<span class="tab-count">結論 2・問い 2</span>', tabs)
        self.assertIn('id="p-demo" data-view="demo" data-title="demo"', page)
        self.assertIn('id="overview" data-view="overview"', page)
        demo = page[page.index('id="p-demo"'):]
        self.assertIn('<a class="prev" href="#p-second">← second</a><a href="#overview">一覧</a><span></span>', demo)

    def test_an_upright_figure_sits_beside_its_text(self) -> None:
        out = self.project / "analysis" / "scan" / "out"
        out.mkdir(parents=True)
        header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR"
        (out / "a.png").write_bytes(header + (1000).to_bytes(4, "big") + (1100).to_bytes(4, "big"))
        self.assertEqual(self.board.image_size(out / "a.png"), (1000, 1100))
        line = self.board.parse_board(TABLE)[0]
        page, _ = self.board.render_line(line, [], "projects/demo/analysis", self.hub)
        self.assertIn('<article class="card side"', page)
        self.assertIn('width="1000" height="1100"', page)
        (out / "a.png").write_bytes(header + (2000).to_bytes(4, "big") + (800).to_bytes(4, "big"))
        page, _ = self.board.render_line(line, [], "projects/demo/analysis", self.hub)
        self.assertIn('<article class="card wide"', page)


if __name__ == "__main__":
    unittest.main()
