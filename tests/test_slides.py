from __future__ import annotations

import contextlib
import io
import json
import struct
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from helpers import load, member

BOARD = """# 解析

<!-- board -->
- 表示名: 高さの研究
### 高さの走査

| 問い | いまの答え | 図 | 対象 run | 読む場所 | 確認日 |
|---|---|---|---|---|---|
| 高さは効くか | 効かない。差は 0.4% 以内。 | ![帯が目安。上段が電位](scan/out/a.png) | `runs/scan/coupled` R20261105-0009 | [結果](scan/README.md) | 2026-11-06 12:00 |
| 幅は効くか | 0.25 s で収束する。 | | `runs/scan/width/` | [結果](scan/README.md) | 2026-10-20 |
| 接合なしでも同じか | | | `runs/scan/open/` | | |
<!-- /board -->
"""

# Stands in for Typst: lists a font, answers the measuring query, and writes the files a compile would.
FAKE_TYPST = '''#!PYTHON
import json, sys
from pathlib import Path
args = sys.argv[1:]
if args[0] == "--version":
    print("typst 0.0-fake")
elif args[0] == "fonts":
    print("Noto Sans CJK JP")
elif args[0] == "query":
    lines = Path(next(a for a in args if a.endswith(".typ"))).read_text(encoding="utf-8").splitlines()
    out = []
    for n, line in enumerate(lines):
        if "<t>" in args and line.startswith("#mt("):
            size = int(line.split('", ', 1)[1].split(",")[0])
            out.append({"id": line.split('"')[1], "s": size, "w": 30.0 * size, "h": 12.0 * size})
        if "<t>" in args or not line.startswith("#mq("):
            continue
        ident = line.split('"')[1]
        width = float(line.split('", ', 1)[1].split(",")[0])
        block = []
        for later in lines[n + 1:]:
            if later.startswith("#mq("):
                break
            block.append(later)
        if ident == "ref":
            height = 50.0
        elif ident.endswith("/title"):
            height = 50.0 if len(line) < 60 else 100.0
        else:
            height = 3000.0 if "LONG" in "".join(block) else 60.0
        out.append({"id": ident, "w": width, "h": height})
    print(json.dumps(out))
elif args[0] == "compile":
    files = [a for a in args[1:] if a.endswith((".typ", ".pdf", ".png"))]
    source, target = Path(files[0]), files[1]
    if target.endswith(".pdf"):
        Path(target).write_bytes(b"%PDF-fake")
    else:
        for n in range(source.read_text(encoding="utf-8").count("#page[")):
            Path(target.replace("{0p}", f"{n + 1:02d}")).write_bytes(b"png")
'''


def png(path: Path, width: int, height: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I4sII", 13, b"IHDR", width, height) + b"\0" * 16)


class SlidesTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.hub = Path(tmp.name).resolve() / "hub"
        self.hub.mkdir()
        self.alpha = member(self.hub, "alpha")
        (self.alpha / "analysis").mkdir()
        (self.alpha / "analysis" / "README.md").write_text(BOARD, encoding="utf-8")
        png(self.alpha / "analysis" / "scan" / "out" / "a.png", 1600, 400)
        member(self.hub, "beta")
        self.slides = load("slides", self.hub)
        self.typst = Path(tmp.name) / "typst"
        self.typst.write_text(FAKE_TYPST.replace("PYTHON", sys.executable), encoding="utf-8")
        self.typst.chmod(0o755)

    def call(self, command, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = command(list(args))
        return code, out.getvalue()

    def card(self, deck: str, slug: str) -> Path:
        return self.hub / "slides" / deck / "cards" / slug / "card.toml"

    def fill(self, path: Path, title: str, found: str) -> None:
        text = path.read_text(encoding="utf-8")
        text = text.replace('title = ""', f'title = "{title}"').replace('found = ""', f'found = "{found}"')
        path.write_text(text, encoding="utf-8")

    def test_a_board_row_becomes_a_card_with_its_figure_and_source(self) -> None:
        s = self.slides
        self.call(s.cmd_new, "20261127", "--title", "個別ミーティング", "--presenter", "誰か")
        self.call(s.cmd_add, "20261127", "height", "--section", "alpha", "--board", "高さは効くか")
        raw = self.card("20261127", "height").read_text(encoding="utf-8")
        card = tomllib.loads(raw)
        self.assertEqual(card["section"], "alpha")
        self.assertEqual(card["figures"][0]["file"], "a.png")
        self.assertEqual(card["figures"][0]["from"], "projects/alpha/analysis/scan/out/a.png")
        self.assertEqual(card["source"]["board"], "高さは効くか")
        self.assertEqual(card["source"]["read"], "projects/alpha/analysis/scan/README.md")
        self.assertIn("#   効かない。", raw)            # the answer is left as a comment to condense
        self.assertIn("帯が目安", raw)
        self.assertTrue((self.card("20261127", "height").parent / "a.png").is_file())
        index = (self.hub / "slides/20261127/INDEX.md").read_text(encoding="utf-8")
        self.assertIn("- 表示名: 高さの研究", index)
        self.assertIn("| `height` | 新規 |", index)

    def test_cards_are_filed_under_their_section_whatever_the_order_added(self) -> None:
        s = self.slides
        self.call(s.cmd_new, "20261127")
        self.call(s.cmd_add, "20261127", "first", "--section", "alpha", "--title", "一つ目")
        self.call(s.cmd_add, "20261127", "other", "--section", "beta", "--title", "別の話")
        self.call(s.cmd_add, "20261127", "second", "--section", "alpha", "--title", "二つ目")
        _, sections = s.read_index(self.hub / "slides/20261127")
        self.assertEqual([(x["name"], x["cards"]) for x in sections],
                         [("alpha", ["first", "second"]), ("beta", ["other"])])
        with self.assertRaises(SystemExit):
            self.call(s.cmd_add, "20261127", "first", "--section", "alpha")

    def test_the_figure_is_as_large_as_the_text_allows(self) -> None:
        s = self.slides
        wide = {"figure": Path("x"), "px": (3600, 1000)}
        square = {"figure": Path("x"), "px": (1000, 1000)}
        heights = {880.0: 60.0, 240.0: 200.0, 280.0: 170.0, 320.0: 150.0}
        self.assertEqual(s.choose_layout(wide, heights)["mode"], "below")
        self.assertEqual(s.choose_layout(square, heights)["mode"], "beside")
        self.assertEqual(s.choose_layout(wide, {})["mode"], "figure")
        long = {880.0: 900.0, 240.0: 3000.0, 280.0: 2600.0, 320.0: 2300.0}
        self.assertFalse(s.choose_layout(wide, long)["fits"])
        short, more = s.choose_layout(wide, heights), s.choose_layout(wide, {**heights, 880.0: 300.0})
        self.assertGreater(short["fig"][2] * short["fig"][3], more["fig"][2] * more["fig"][3])

    def test_text_reaches_typst_as_literals_and_marks_as_placed_shapes(self) -> None:
        s = self.slides
        self.assertEqual(s.rich('a "b" **key** #c'), '#"a \\"b\\" ";#key("key");#" #c";')
        arrow = s.mark_typst({"type": "arrow", "x1": 0.1, "y1": 0.1, "x2": 0.5, "y2": 0.1}, 800, 400)
        self.assertIn("line(start: (80.0pt, 40.0pt), end: (389.0pt, 40.0pt)", arrow)
        self.assertIn("polygon(", arrow)
        label = s.mark_typst({"type": "label", "x": 0.5, "y": 0.5, "text": "電位"}, 800, 400)
        self.assertIn('dx: 400.0pt, dy: 200.0pt', label)
        self.assertIn("white.transparentize", label)

    def test_build_writes_the_deck_and_reports_what_does_not_fit(self) -> None:
        s = self.slides
        self.call(s.cmd_new, "20261127", "--title", "個別ミーティング")
        self.call(s.cmd_add, "20261127", "height", "--section", "alpha", "--board", "高さは効くか")
        code, out = self.call(s.cmd_build, "20261127", "--typst", str(self.typst))
        self.assertEqual(code, 1)
        self.assertIn("タイトルが空", out)
        self.fill(self.card("20261127", "height"), "高さは効かない", "箱を高くしても**変わらない**。差は0.4%以内で、0.25 s、3 mまで見た。")
        code, out = self.call(s.cmd_build, "20261127", "--typst", str(self.typst))
        self.assertEqual(code, 0, out)
        deck = self.hub / "slides/20261127"
        check = json.loads((deck / "out/check.json").read_text(encoding="utf-8"))
        self.assertEqual((check["pages"], check["problems"]), (4, []))     # title, agenda, section cover, card
        self.assertTrue((deck / "out/deck.pdf").is_file() and (deck / "out/png/04.png").is_file())
        self.assertEqual([(x["page"], x["layout"]) for x in check["slides"]], [(3, "section"), (4, "below")])
        self.assertIn("文に数値が3個", "".join(check["slides"][1]["notes"]))
        source = (deck / "out/deck.typ").read_text(encoding="utf-8")
        self.assertIn("今回行ったこと", source)
        self.assertIn("/20261127/cards/height/a.png", source)
        self.assertNotIn("scan/README.md", source)       # the source of a card is not shown
        self.assertIn('#text(size: 44pt, weight: "light", "高さの研究")', source)   # the cover of the section
        index = deck / "INDEX.md"
        index.write_text(index.read_text(encoding="utf-8").replace("- 節の表紙: あり", "- 節の表紙: なし"), encoding="utf-8")
        code, out = self.call(s.cmd_build, "20261127", "--typst", str(self.typst))
        self.assertEqual((code, json.loads((deck / "out/check.json").read_text(encoding="utf-8"))["pages"]), (0, 3))

        text = self.card("20261127", "height").read_text(encoding="utf-8").replace("高さは効かない", "高" * 40)
        self.card("20261127", "height").write_text(text.replace('open = ""', 'open = "LONG"'), encoding="utf-8")
        code, out = self.call(s.cmd_build, "20261127", "--typst", str(self.typst))
        self.assertEqual(code, 1)
        self.assertIn("タイトルが1行に収まらない", out)
        self.assertIn("文が収まらない", out)

    def test_a_table_is_set_in_the_largest_type_that_fits(self) -> None:
        s = self.slides
        self.call(s.cmd_new, "20261127")
        self.call(s.cmd_add, "20261127", "values", "--section", "alpha", "--title", "条件ごとの値")
        card = self.card("20261127", "values")
        (card.parent / "t.csv").write_text("# 写し\n条件,放出分布,表面電位 [V]\n通常,Maxwell,+5.783\n通常,月試料,**+0.350**\n希薄,Maxwell,−7.5\n", encoding="utf-8")
        card.write_text(card.read_text(encoding="utf-8").replace('# [[figures]]\n# file = "fig.png"', '[[figures]]\ntable = "t.csv"'), encoding="utf-8")
        code, out = self.call(s.cmd_build, "20261127", "--typst", str(self.typst))
        self.assertEqual(code, 0, out)
        deck = self.hub / "slides/20261127"
        self.assertEqual(json.loads((deck / "out/check.json").read_text(encoding="utf-8"))["slides"][-1]["layout"], "table")
        source = (deck / "out/deck.typ").read_text(encoding="utf-8")
        self.assertIn("#text(size: 20pt)[#table(columns: 3,", source)     # 600 x 240 pt at 20 pt fits
        self.assertIn("align: (left, left, right, )", source)             # the column of numbers
        self.assertIn('#key("+0.350");', source)
        self.assertEqual(source.count("table.hline(stroke: 0.5pt + muted)"), 1)   # between 通常 and 希薄
        rows = [["a", "b"]] + [["x", "1"]] * 3
        self.assertEqual(s.table_layout(0.0, {z: (40.0 * z, 30.0 * z) for z in s.TABLE_SIZES})["table"][2], 14)
        self.assertFalse(s.table_layout(0.0, {z: (90.0 * z, 30.0 * z) for z in s.TABLE_SIZES})["fits"])
        self.assertIn("columns: 2", s.table_typst(rows, 16))

    def test_an_archived_deck_keeps_the_library_cards_it_used(self) -> None:
        s = self.slides
        self.call(s.cmd_new, "20261127")
        self.call(s.cmd_add, "20261127", "setup", "--section", "alpha", "--title", "配置", "--library",
                  "--figure", str(self.alpha / "analysis/scan/out/a.png"))
        library = self.hub / "slides/_library/alpha/setup"
        self.assertEqual(tomllib.loads((library / "card.toml").read_text(encoding="utf-8"))["kind"], "recap")
        self.assertIn("| `_library/alpha/setup` | おさらい | 配置 |", (self.hub / "slides/20261127/INDEX.md").read_text(encoding="utf-8"))
        with self.assertRaises(SystemExit):
            self.call(s.cmd_archive, "20261127")            # not built yet
        self.call(s.cmd_archive, "20261127", "--force")
        kept = self.hub / "slides/_archive/20261127"
        self.assertFalse((self.hub / "slides/20261127").exists())
        self.assertTrue((kept / "_library/alpha/setup/a.png").is_file())
        edited = (library / "card.toml").read_text(encoding="utf-8").replace('title = "配置"', 'title = "後で直した"')
        (library / "card.toml").write_text(edited, encoding="utf-8")
        self.assertEqual(s.load_card(kept, "_library/alpha/setup")["title"], "配置")

        self.call(s.cmd_new, "20261204")
        self.call(s.cmd_add, "20261204", "_library/alpha/setup")
        _, sections = s.read_index(self.hub / "slides/20261204")
        self.assertEqual(sections[0]["cards"], ["_library/alpha/setup"])

    def test_status_lists_answers_newer_than_the_last_presentation(self) -> None:
        s = self.slides
        self.call(s.cmd_new, "20261101")
        self.call(s.cmd_add, "20261101", "old", "--section", "alpha", "--title", "前回")
        self.call(s.cmd_archive, "20261101", "--force")
        _, out = self.call(s.cmd_status)
        self.assertIn("高さは効くか", out)                  # confirmed 11-06, after the deck of 11-01
        self.assertNotIn("幅は効くか", out)                 # confirmed 10-20, before it
        self.assertNotIn("接合なしでも同じか", out)         # no answer yet
        self.call(s.cmd_new, "20261127")
        self.call(s.cmd_add, "20261127", "height", "--section", "alpha", "--board", "高さは効くか")
        _, out = self.call(s.cmd_status)
        self.assertIn("20261127", out)
        self.assertNotIn("2026-11-06  高さは効くか", out)   # already on a card


if __name__ == "__main__":
    unittest.main()
