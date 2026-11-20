#!/usr/bin/env python3
"""Progress slides kept in the hub: one card per result, one deck per presentation.

    slides.py status                      decks, and board answers newer than the last presentation
    slides.py new DECK [--title TEXT] [--presenter TEXT]
    slides.py add DECK SLUG --section PROJECT [--title TEXT] [--figure PATH]... [--board TEXT]
                            [--recap] [--library]
    slides.py add DECK _library/SECTION/SLUG      put a card of the library into the deck
    slides.py build DECK [--typst PATH] [--font-path DIR]
    slides.py archive DECK [--force]

Layout, under ``<hub>/slides/``::

    <deck>/INDEX.md            the order: one ``## section`` per project, one table row per card
    <deck>/cards/<slug>/       card.toml and its figures (copies, so the deck keeps what was shown)
    <deck>/out/                deck.pdf, png/NN.png, check.json
    _library/<section>/<slug>/ cards reused across decks (setups, schematics, model explanations)
    _archive/<deck>/           presented decks, with a copy of the library cards they used

A card says what a slide shows; this file decides where things go. The figure gets the whole
area under the title, and the text goes under a wide figure or beside a tall one, whichever
leaves the figure larger. Names of panels and curves, and the values a claim rests on, belong
on the figure (``marks``, or a figure drawn for the slide), not in the text. The source of a
card is recorded in card.toml and is not shown. Text heights are measured by Typst first
(``typst query``), so the choice uses real line breaks.

``build`` exits 1 when a check fails (text that does not fit, a title that is empty or longer
than one line, a missing card or figure) and prints softer notes for text that is long or
carries several numbers. Only the standard library is used; Typst >= 0.12 does the typesetting
(``--typst``, ``$HUB_TYPST``, PATH, or ~/.local/bin/typst).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tomllib
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import board as bd  # noqa: E402
from projects import member_paths  # noqa: E402

HUB = Path(os.environ.get("RUNOPS_HUB_DIR") or Path.cwd()).resolve()
SLIDES = HUB / "slides"

PAGE_W, PAGE_H = 960.0, 540.0
MX = 40.0                      # left and right margin
TITLE_Y, TITLE_PT = 24.0, 34
TOP, BOTTOM = 84.0, 514.0      # the area for figure and text
GAP = 14.0
BODY_PT, SMALL_PT = 20, 12
RIGHT_WIDTHS = (240.0, 280.0, 320.0)   # candidate widths of a text column beside the figure
INSET_H, INSET_MAX_W = 72.0, 200.0
TEXT_WARN = 80                 # characters of look + found + open on one slide
NUMBER_WARN = 2                # numbers in that text; more than this belongs in the figure
FIGURE_WARN = 0.60             # figure area with text, relative to the same figure alone on the slide
FONTS = ("Noto Sans CJK JP", "Noto Sans JP", "Hiragino Sans", "Yu Gothic")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
DECK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
KINDS = {"new": "新規", "recap": "おさらい"}
INDEX_HEADER = "| card | 区分 | タイトル | 追加日 |\n|---|---|---|---|\n"


def preamble(font: str) -> str:
    return f"""#set page(width: {PAGE_W}pt, height: {PAGE_H}pt, margin: 0pt, fill: white)
#set text(font: ("{font}",), lang: "ja", region: "jp", size: {BODY_PT}pt, fill: rgb("#111111"))
#set par(leading: 0.55em, spacing: 0.8em, justify: false)
#let accent = rgb("#E36C0A")
#let muted = rgb("#6B6B6B")
#let at(x, y, body) = place(top + left, dx: x * 1pt, dy: y * 1pt, body)
#let key(s) = text(fill: accent, weight: "bold", s)
#let title(body) = text(size: {TITLE_PT}pt, weight: "light", body)
#let mq(id, w, body) = context [#metadata((id: id, w: w, h: measure(width: w * 1pt, body).height.pt())) <m>]
"""


def die(msg: str) -> None:
    sys.exit(f"hub slides: {msg}")


def lit(s: str) -> str:
    """A Typst string literal; text never passes through Typst markup."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def rich(s: str) -> str:
    """``**key phrase**`` becomes the accent colour; everything else is literal text."""
    out = []
    for i, part in enumerate(s.split("**")):
        if part:
            out.append(f"#key({lit(part)});" if i % 2 else f"#{lit(part)};")
    return "".join(out)


def plain_len(s: str) -> int:
    return len(s.replace("**", ""))


def image_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()[:65536]
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", data[16:24])
    if data[:2] == b"\xff\xd8":  # JPEG: the first SOF marker carries the size
        i = 2
        while i + 9 < len(data):
            marker, size = data[i + 1], struct.unpack(">H", data[i + 2:i + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            i += 2 + size
    die(f"{path}: only PNG and JPEG figures are supported")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- decks, INDEX.md and cards

def deck_dir(name: str) -> Path:
    for cand in (SLIDES / name, SLIDES / "_archive" / name, Path(name)):
        if (cand / "INDEX.md").is_file():
            return cand.resolve()
    die(f"no deck {name!r} (looked for {SLIDES / name}/INDEX.md and in _archive/)")


def slides_root(deck: Path) -> Path:
    """The slides directory a deck belongs to: also the Typst root, so figures resolve from it."""
    return deck.parent.parent if deck.parent.name == "_archive" else deck.parent


def card_dir(deck: Path, ref: str) -> Path:
    """A library card is read from the copy inside an archived deck when there is one."""
    if ref.startswith("_library/"):
        kept = deck / ref
        return kept if (kept / "card.toml").is_file() else slides_root(deck) / ref
    return deck / "cards" / ref


def decks() -> tuple[list[Path], list[Path]]:
    def listing(parent: Path) -> list[Path]:
        if not parent.is_dir():
            return []
        return sorted(p for p in parent.iterdir() if not p.name.startswith("_") and (p / "INDEX.md").is_file())
    return listing(SLIDES), listing(SLIDES / "_archive")


def read_index(deck: Path) -> tuple[dict, list[dict]]:
    meta: dict[str, str] = {}
    sections: list[dict] = []
    for raw in (deck / "INDEX.md").read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if s.startswith("## "):
            sections.append({"name": s[3:].strip(), "meta": {}, "cards": []})
        elif s.startswith("- ") and re.search(r"[:：]", s):
            key, value = re.split(r"[:：]", s[2:], maxsplit=1)
            (sections[-1]["meta"] if sections else meta)[key.strip()] = value.strip()
        elif s.startswith("|") and sections:
            cell = s.strip("|").split("|")[0].strip().strip("`")
            if cell and cell != "card" and not set(cell) <= set("-: "):
                sections[-1]["cards"].append(cell)
    return meta, sections


def load_card(deck: Path, ref: str) -> dict | None:
    path = card_dir(deck, ref) / "card.toml"
    if not path.is_file():
        return None
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        die(f"{path}: {exc}")


def load_pages(deck: Path, sections: list[dict], problems: list[str]) -> list[dict]:
    """One dict per slide. A card with several figures gives several slides."""
    root = slides_root(deck)
    pages: list[dict] = []
    for sec in sections:
        for ref in sec["cards"]:
            cdir = card_dir(deck, ref)
            card = load_card(deck, ref)
            if card is None:
                problems.append(f"{ref}: card.toml が無い（{cdir}）")
                continue
            if not str(card.get("title", "")).strip():
                problems.append(f"{ref}: タイトルが空")
            figures = card.get("figures") or [{}]
            for n, fig in enumerate(figures):
                page = {
                    "id": ref if len(figures) == 1 else f"{ref}#{n + 1}",
                    "card": ref, "first": n == 0, "card_title": str(card.get("title", "")),
                    "section": sec["meta"].get("表示名", sec["name"]),
                    "kind": card.get("kind", "new"),
                    "title": fig.get("title") or str(card.get("title", "")) + (f"：{fig['label']}" if fig.get("label") else ""),
                    "text": fig.get("text") or (card.get("text", {}) if n == 0 else {}),
                    "marks": fig.get("marks", []), "figure": None, "inset": None,
                }
                if fig.get("file"):
                    path = cdir / fig["file"]
                    if path.is_file():
                        page["figure"] = path
                        page["px"] = image_size(path)
                        orig = HUB / fig["from"] if fig.get("from") else None
                        if orig and orig.is_file():
                            page["stale"] = sha256(orig) != sha256(path)
                    else:
                        problems.append(f"{page['id']}: 図が無い（{path}）")
                if fig.get("inset"):
                    ipath = deck / fig["inset"] if (deck / fig["inset"]).is_file() else root / fig["inset"]
                    if ipath.is_file():
                        page["inset"] = ipath
                    else:
                        problems.append(f"{page['id']}: 隅に置く図が無い（{ipath}）")
                pages.append(page)
    return pages


# ---------------------------------------------------------------- Typst pieces

def text_block(t: dict) -> str | None:
    rows = []
    if t.get("look"):
        rows.append(f"grid.cell(colspan: 2)[{rich(t['look'])}]")
    if t.get("found"):
        rows.append(f"[→], [{rich(t['found'])}]")
    if t.get("open"):
        mark = "？" if t["open"].rstrip().endswith(("？", "?")) else "次"
        rows.append(f"[#text(fill: accent)[{mark}]], [{rich(t['open'])}]")
    if not rows and not t.get("limits"):
        return None
    out = ""
    if rows:
        out += "#grid(columns: (auto, 1fr), column-gutter: 7pt, row-gutter: 11pt, " + ", ".join(rows) + ")\n"
    if t.get("limits"):
        out += f"#text(size: {SMALL_PT}pt, fill: muted)[{rich(t['limits'])}]\n"
    return out


def rel(path: Path, root: Path) -> str:
    return "/" + path.resolve().relative_to(root.resolve()).as_posix()


def inset_width(page: dict) -> float:
    if not page["inset"]:
        return 0.0
    w, h = image_size(page["inset"])
    return min(INSET_MAX_W, INSET_H * w / h)


def title_width(page: dict) -> float:
    iw = inset_width(page)
    return PAGE_W - 2 * MX - (iw + GAP if iw else 0.0)


def contain(bw: float, bh: float, ar: float) -> tuple[float, float]:
    return (bw, bw / ar) if bw / ar <= bh else (bh * ar, bh)


def choose_layout(page: dict, heights: dict[float, float]) -> dict:
    """Where figure and text go: the candidate that leaves the figure the largest area."""
    aw, ah = PAGE_W - 2 * MX, BOTTOM - TOP
    if not page["figure"]:
        return {"mode": "text", "text": (MX, TOP, aw, ah), "fits": heights.get(aw, 0) <= ah}
    ar = page["px"][0] / page["px"][1]
    if not heights:
        fw, fh = contain(aw, ah, ar)
        return {"mode": "figure", "fig": (MX + (aw - fw) / 2, TOP + (ah - fh) / 2, fw, fh), "fits": True}
    cands = []
    th = heights[aw]
    if ah - th - GAP > 80:
        fw, fh = contain(aw, ah - th - GAP, ar)
        y = TOP + (ah - fh - GAP - th) / 4   # a width-limited figure leaves room: do not hang it from the top
        cands.append({"mode": "below", "fig": (MX + (aw - fw) / 2, y, fw, fh),
                      "text": (MX, y + fh + GAP, aw, th), "fits": True})
    for tw in RIGHT_WIDTHS:
        if heights[tw] <= ah:
            fw, fh = contain(aw - tw - 1.5 * GAP, ah, ar)
            x = MX + fw + 1.5 * GAP
            cands.append({"mode": "beside", "fig": (MX, TOP + (ah - fh) / 2, fw, fh),
                          "text": (x, TOP + max(0.0, (ah - heights[tw]) / 2 - 10), PAGE_W - MX - x, heights[tw]), "fits": True})
    if not cands:  # the text is too long for every arrangement: show it anyway and report it
        fw, fh = contain(aw, ah * 0.5, ar)
        return {"mode": "below", "fig": (MX + (aw - fw) / 2, TOP, fw, fh),
                "text": (MX, TOP + fh + GAP, aw, th), "fits": False}
    return max(cands, key=lambda c: c["fig"][2] * c["fig"][3])


def mark_typst(m: dict, fw: float, fh: float) -> str:
    kind = m.get("type")
    if kind == "label":   # ink on a white plate unless a colour is asked for (naming a curve by its colour)
        color = f'rgb({lit(m["color"])})' if m.get("color") else 'rgb("#111111")'
        body = f"text(size: {m.get('size', 17)}pt, fill: {color}, weight: \"medium\", {lit(str(m['text']))})"
        if m.get("bg", True):
            body = f"box(fill: white.transparentize(12%), inset: (x: 4pt, y: 2.5pt), radius: 2pt, {body})"
        return f"#place(top + left, dx: {m['x'] * fw:.1f}pt, dy: {m['y'] * fh:.1f}pt, {body})"
    color = f'rgb({lit(m["color"])})' if m.get("color") else "accent"
    if kind == "arrow":   # from (x1, y1) to the head at (x2, y2)
        x1, y1, x2, y2 = m["x1"] * fw, m["y1"] * fh, m["x2"] * fw, m["y2"] * fh
        n = max(1e-6, ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5)
        ux, uy = (x2 - x1) / n, (y2 - y1) / n
        bx, by = x2 - ux * 11, y2 - uy * 11
        pts = ", ".join(f"({px:.1f}pt, {py:.1f}pt)" for px, py in ((x2, y2), (bx - uy * 4.5, by + ux * 4.5), (bx + uy * 4.5, by - ux * 4.5)))
        return (f"#place(top + left, line(start: ({x1:.1f}pt, {y1:.1f}pt), end: ({bx:.1f}pt, {by:.1f}pt), stroke: 2pt + {color}))"
                f"#place(top + left, polygon(fill: {color}, {pts}))")
    shape = {"rect": "rect", "ellipse": "ellipse"}.get(kind)
    if not shape:
        die(f"unknown mark type {kind!r} (rect, ellipse, label, arrow)")
    return (f"#place(top + left, dx: {m['x'] * fw:.1f}pt, dy: {m['y'] * fh:.1f}pt, {shape}(width: {m['w'] * fw:.1f}pt, height: {m['h'] * fh:.1f}pt, "
            f"stroke: (paint: {color}, thickness: 2pt, dash: \"dashed\")))")


def page_typst(page: dict, lay: dict, number: int, root: Path) -> str:
    out = ["#page["]
    out.append(f"#at({MX}, {TITLE_Y})[#block(width: {title_width(page):.1f}pt)[#title[{rich(page['title'])}]]]")
    if page["inset"]:
        iw = inset_width(page)
        out.append(f"#at({PAGE_W - MX - iw:.1f}, 12)[#image({lit(rel(page['inset'], root))}, width: {iw:.1f}pt, height: {INSET_H}pt, fit: \"contain\")]")
    if "fig" in lay:
        x, y, fw, fh = lay["fig"]
        try:
            marks = "".join(mark_typst(m, fw, fh) for m in page["marks"])
        except KeyError as exc:
            die(f"{page['id']}: a mark lacks {exc}")
        out.append(f"#at({x:.1f}, {y:.1f})[#box(width: {fw:.1f}pt, height: {fh:.1f}pt)[#place(top + left, image({lit(rel(page['figure'], root))}, width: {fw:.1f}pt, height: {fh:.1f}pt, fit: \"stretch\")){marks}]]")
    if "text" in lay:
        x, y, tw, _ = lay["text"]
        out.append(f"#at({x:.1f}, {y:.1f})[#block(width: {tw:.1f}pt)[\n{text_block(page['text'])}]]")
    out.append(f"#place(bottom + right, dx: -{MX}pt, dy: -10pt, text(size: 11pt, fill: muted, {lit(str(number))}))")
    out.append("]")
    return "\n".join(out)


def front_typst(meta: dict, sections: list[dict], pages: list[dict]) -> str:
    """The title page, and what was done this time: the new cards of each section."""
    out = ["#page[#align(center + horizon)[",
           f"#text(size: 48pt, weight: \"light\", {lit(meta.get('題', '進捗報告'))})\\",
           f"#v(6pt)#text(size: 22pt, {lit('　'.join(x for x in (meta.get('日付', ''), meta.get('発表者', '')) if x))})",
           "]]", "#page[", f"#at({MX}, {TITLE_Y})[#title[今回行ったこと]]", f"#at({MX + 10}, {TOP + 12})[#block(width: {PAGE_W - 2 * MX - 20}pt)["]
    for sec in sections:
        name = sec["meta"].get("表示名", sec["name"])
        new = [p for p in pages if p["section"] == name and p["kind"] != "recap" and p["first"]]
        if not new:
            continue
        subs = ", ".join(f"[#text(size: 18pt, {lit(p['card_title'])})]" for p in new)
        out.append(f"#list(marker: [•], spacing: 10pt, [#text(size: 24pt, {lit(name)}) #list(marker: [–], spacing: 8pt, {subs})])")
        out.append("#v(10pt)")
    out += ["]]", "]"]
    return "\n".join(out)


# ---------------------------------------------------------------- build

def run(cmd: list[str]) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        die("failed: " + " ".join(cmd) + "\n" + r.stderr)
    return r


def find_typst(given: str) -> str:
    for cand in (given, os.environ.get("HUB_TYPST", ""), shutil.which("typst") or "", str(Path.home() / ".local/bin/typst")):
        if cand and (shutil.which(cand) or Path(cand).is_file()):
            return cand
    die("typst not found (install it from https://github.com/typst/typst/releases, or pass --typst PATH)")


def cmd_build(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="./hub slides build")
    ap.add_argument("deck")
    ap.add_argument("--typst", default="")
    ap.add_argument("--font-path", default="")
    args = ap.parse_args(argv)
    deck = deck_dir(args.deck)
    root = slides_root(deck)
    out = deck / "out"
    (out / "png").mkdir(parents=True, exist_ok=True)
    typst = [find_typst(args.typst)]
    font_args = ["--font-path", args.font_path] if args.font_path else []
    common = ["--root", str(root)] + font_args
    listed = run(typst + ["fonts"] + font_args).stdout.splitlines()
    font = next((f for f in FONTS if f in listed), None)
    if font is None:
        die(f"none of the fonts {', '.join(FONTS)} is visible to typst here; pass --font-path DIR")
    head = preamble(font)

    problems: list[str] = []
    meta, sections = read_index(deck)
    pages = load_pages(deck, sections, problems)

    # Pass 1: measure every text block at every width it may get, and every title.
    aw = PAGE_W - 2 * MX
    probe = [head, f'#mq("ref", {aw}, title[あ])']
    for p in pages:
        shown = rich(p["title"]) or '#"";'
        probe.append(f"#mq({lit(p['id'] + '/title')}, {title_width(p):.1f}, title[{shown}])")
        block = text_block(p["text"])
        if block:
            for w in (aw,) + RIGHT_WIDTHS:
                probe.append(f"#mq({lit(p['id'])}, {w}, [\n{block}])")
    (out / "measure.typ").write_text("\n".join(probe) + "\n", encoding="utf-8")
    measured = json.loads(run(typst + ["query"] + common + [str(out / "measure.typ"), "<m>", "--field", "value"]).stdout)
    heights: dict[str, dict[float, float]] = {}
    for m in measured:
        heights.setdefault(m["id"], {})[float(m["w"])] = m["h"]
    line = heights["ref"][aw]

    # Pass 2: place everything.
    body, report = [head, front_typst(meta, sections, pages)], []
    for n, p in enumerate(pages, 3):
        lay = choose_layout(p, heights.get(p["id"], {}))
        body.append(page_typst(p, lay, n, root))
        said = "".join(p["text"].get(k, "") for k in ("look", "found", "open"))
        chars = plain_len(said)
        notes = []
        if not lay["fits"]:
            problems.append(f"{p['id']}: 文が収まらない（{chars}字）。文を削るか、card を分ける")
        if max(heights[p["id"] + "/title"].values()) > line * 1.3:
            problems.append(f"{p['id']}: タイトルが1行に収まらない（{plain_len(p['title'])}字）")
        if chars > TEXT_WARN:
            notes.append(f"文が{chars}字（目安{TEXT_WARN}字）")
        numbers = len(re.findall(r"\d+(?:\.\d+)?(?![\d.°])", said))   # an angle such as 60° names a condition
        if numbers > NUMBER_WARN:
            notes.append(f"文に数値が{numbers}個（目安{NUMBER_WARN}個）。図に載せる")
        share = None
        if "fig" in lay:
            fw, fh = lay["fig"][2:]
            share = fw * fh / (PAGE_W * PAGE_H)
            alone = contain(aw, BOTTOM - TOP, p["px"][0] / p["px"][1])
            if "text" in lay and fw * fh < FIGURE_WARN * alone[0] * alone[1]:
                notes.append(f"図が文なしのときの{fw * fh / (alone[0] * alone[1]):.0%}（目安{FIGURE_WARN:.0%}以上）。文を減らすと図が大きくなる")
            if p["px"][0] < fw * 2 * 0.7:
                notes.append(f"図の解像度が低い（幅{p['px'][0]}px を {fw * 2:.0f}px に拡大）")
        if p.get("stale"):
            notes.append("元の図が写しの後に更新されている")
        report.append({"page": n, "id": p["id"], "section": p["section"], "kind": p["kind"], "title": p["title"],
                       "layout": lay["mode"], "figure_share": round(share, 3) if share else None,
                       "text_chars": chars, "notes": notes})
    (out / "deck.typ").write_text("\n".join(body) + "\n", encoding="utf-8")
    for old in (out / "png").glob("*.png"):
        old.unlink()
    # Untagged: an older poppler reports a syntax error for the tags Typst writes by default.
    run(typst + ["compile", "--no-pdf-tags"] + common + [str(out / "deck.typ"), str(out / "deck.pdf")])
    run(typst + ["compile"] + common + ["--ppi", "144", str(out / "deck.typ"), str(out / "png" / "{0p}.png")])
    made = len(list((out / "png").glob("*.png")))
    if made != len(pages) + 2:
        problems.append(f"ページ数が合わない（{made} 枚、予定は {len(pages) + 2} 枚）")
    (out / "check.json").write_text(json.dumps({
        "built": datetime.now().astimezone().isoformat(timespec="seconds"), "typst": run(typst + ["--version"]).stdout.strip(),
        "font": font, "pages": made, "problems": problems, "slides": report}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"{deck.name}: {made} 枚 -> {out / 'deck.pdf'}")
    for r in report:
        share = f"{r['figure_share']:.0%}" if r["figure_share"] else " - "
        print(f"  {r['page']:>2} {r['layout']:<7} 図{share:>4} 文{r['text_chars']:>4}字  {r['id']}" + ("  ※ " + "、".join(r["notes"]) if r["notes"] else ""))
    for msg in problems:
        print(f"  !! {msg}")
    return 1 if problems else 0


# ---------------------------------------------------------------- new, add

def cmd_new(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="./hub slides new")
    ap.add_argument("deck", help="directory name, usually the date of the presentation (YYYYMMDD)")
    ap.add_argument("--title", default="")
    ap.add_argument("--presenter", default="")
    args = ap.parse_args(argv)
    if not DECK_RE.match(args.deck):
        die("DECK is a directory name such as 20261127 (letters, digits, - and _; not starting with _)")
    deck = SLIDES / args.deck
    if deck.exists() or (SLIDES / "_archive" / args.deck).exists():
        die(f"{args.deck} already exists")
    active, archived = decks()
    previous = (active + archived)[-1:]   # the presenter rarely changes: take it from the latest deck
    presenter = args.presenter or (read_index(previous[0])[0].get("発表者", "") if previous else "")
    day = re.match(r"(\d{4})(\d{2})(\d{2})", args.deck)
    (deck / "cards").mkdir(parents=True)
    (deck / "logs").mkdir()
    (deck / "INDEX.md").write_text(
        f"# 進捗スライド {args.deck}\n\n"
        f"- 題: {args.title or '進捗報告'}\n"
        f"- 日付: {'/'.join(day.groups()) if day else date.today().strftime('%Y/%m/%d')}\n"
        f"- 発表者: {presenter}\n\n"
        "この表がスライドの並び順です。節は project ごとで、追加日に関係なくその project の節に入れます。\n"
        "内容の正本は各 card の `card.toml` で、タイトルと区分の列はその写しです。\n"
        "`_library/` で始まる card は使い回す素材、それ以外は `cards/` にあります。\n", encoding="utf-8")
    print(f"created {deck}")
    if active:
        print("other decks in progress: " + ", ".join(p.name for p in active))
    return 0


def toml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def commented(label: str, text: str) -> str:
    return f"# {label}\n" + "".join(f"#   {line}\n" for line in re.split(r"(?<=。)", text.strip()) if line.strip())


def append_row(deck: Path, section: str, display: str, ref: str, kind: str, title: str, added: str) -> None:
    """Add the card at the end of its section; a section that is not there yet goes last."""
    index = deck / "INDEX.md"
    lines = index.read_text(encoding="utf-8").splitlines()
    row = f"| `{ref}` | {KINDS.get(kind, kind)} | {title} | {added} |"
    heads = [i for i, s in enumerate(lines) if s.startswith("## ")]
    start = next((i for i in heads if lines[i][3:].strip() == section), None)
    if start is None:
        block = ["", f"## {section}", ""] + ([f"- 表示名: {display}", ""] if display else []) + INDEX_HEADER.splitlines() + [row]
        lines = lines + block
    else:
        end = next((i for i in heads if i > start), len(lines))
        rows = [i for i in range(start, end) if lines[i].startswith("|")]
        if rows:
            lines.insert(rows[-1] + 1, row)
        else:
            lines[end:end] = INDEX_HEADER.splitlines() + [row, ""]
    index.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")


def board_row(member: Path, text: str) -> dict:
    readme = member / "analysis" / "README.md"
    if not readme.is_file():
        die(f"{readme} not found")
    rows = [r for line in bd.parse_board(readme.read_text(encoding="utf-8")) for r in line["rows"]
            if r.get(bd.QUESTION) and text in r[bd.QUESTION]]
    if len(rows) != 1:
        found = "\n".join("  " + r[bd.QUESTION] for r in rows) or "  (none)"
        die(f"--board {text!r} must match one 問い in {readme}; matched:\n{found}")
    if bd.is_open(rows[0]):
        die(f"the board row {rows[0][bd.QUESTION]!r} has no answer yet")
    return rows[0]


def origin_of(path: Path, members: dict[str, Path]) -> str:
    """Where a figure came from, written from the hub: through the member link when it is in a project."""
    for link in members.values():
        real = link.resolve()
        if real in path.parents:
            return f"{link.relative_to(HUB).as_posix()}/{path.relative_to(real).as_posix()}"
    return path.relative_to(HUB).as_posix() if HUB in path.parents else str(path)


def cmd_add(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="./hub slides add")
    ap.add_argument("deck")
    ap.add_argument("slug", help="name of the new card, or _library/SECTION/SLUG to use a library card")
    ap.add_argument("--section", default="", help="member project (or any label) the card is filed under")
    ap.add_argument("--title", default="")
    ap.add_argument("--figure", action="append", default=[], help="figure to copy into the card (repeatable)")
    ap.add_argument("--board", default="", help="text of the 問い of a board row: its figure and source are taken")
    ap.add_argument("--recap", action="store_true", help="a reminder slide: not listed under 今回行ったこと")
    ap.add_argument("--library", action="store_true", help="create the card in _library/ (reused across decks)")
    args = ap.parse_args(argv)
    deck = deck_dir(args.deck)
    if deck.parent.name == "_archive":
        die(f"{deck.name} is archived")
    members = {m.name: m for m in member_paths(HUB)}
    today = date.today().isoformat()

    def display_of(section: str) -> str:
        readme = members[section] / "analysis" / "README.md" if section in members else None
        return bd.project_title(readme.read_text(encoding="utf-8"), "") if readme and readme.is_file() else ""

    listed = [c for sec in read_index(deck)[1] for c in sec["cards"]]
    if args.slug.startswith("_library/"):   # an existing library card: only the row is added
        card = load_card(deck, args.slug)
        if card is None:
            die(f"no library card {SLIDES / args.slug}/card.toml")
        if args.slug in listed:
            die(f"{args.slug} is already in {deck.name}")
        section = args.section or card.get("section", "")
        if not section:
            die(f"{args.slug} names no section; pass --section")
        append_row(deck, section, display_of(section), args.slug, card.get("kind", "recap"), card.get("title", ""), today)
        print(f"added {args.slug} to {deck.name} ({section})")
        return 0

    if not SLUG_RE.match(args.slug):
        die("SLUG is a short lower-case name such as offset-uv")
    if not args.section:
        die("--section PROJECT is required for a new card")
    ref = f"_library/{args.section}/{args.slug}" if args.library else args.slug
    cdir = SLIDES / ref if args.library else deck / "cards" / args.slug
    if cdir.exists() or ref in listed:
        die(f"card {ref} already exists")

    notes, source, figures = "", {}, []
    for raw in args.figure:
        path = next((p for p in (Path(raw), HUB / raw) if p.is_file()), None)
        if path is None:
            die(f"figure {raw} not found")
        figures.append(path.resolve())
    if args.board:
        if args.section not in members:
            die(f"--board needs --section to be a member project ({', '.join(members)})")
        row = board_row(members[args.section], args.board)
        how, fig = bd.figure_of(row.get(bd.FIGURE, ""))
        base = f"projects/{args.section}" if (HUB / "projects" / args.section).exists() else args.section
        if fig and not figures:
            path = (members[args.section] / "analysis" / fig)
            if not path.is_file():
                die(f"the figure of the board row is missing: {path}")
            figures.append(path.resolve())
        notes = commented("board の答え（ここから主張を 1〜2 文にする。数値は文に並べず、図に載せる）:", row[bd.ANSWER])
        if how:
            notes += commented("図の読み方（段や曲線の名前は [[figures.marks]] の label で図に書く）:", how)
        read = bd.LINK_RE.search(row.get(bd.READ, ""))
        source = {"board": row[bd.QUESTION], "read": f"{base}/analysis/{read.group(2)}" if read else "",
                  "runs": row.get(bd.RUNS, ""), "asof": row.get(bd.ASOF, "")}

    cdir.mkdir(parents=True)
    blocks = []
    for path in figures:
        name = path.name
        while (cdir / name).exists():
            name = f"{Path(name).stem}-{len(blocks) + 1}{Path(name).suffix}"
        shutil.copy2(path, cdir / name)
        origin = origin_of(path, members)
        blocks.append(f"[[figures]]\nfile = {toml_str(name)}\nfrom = {toml_str(origin)}        # 元の図（hub からの path）\n"
                      f"sha256 = {toml_str(sha256(path))}\n")
    kind = "recap" if args.recap or args.library else "new"
    card = (notes + ("\n" if notes else "")
            + f"section = {toml_str(args.section)}\nkind = {toml_str(kind)}          # new（新規）| recap（おさらい）\n"
            + f"title = {toml_str(args.title)}        # 1 行に収まる短い語句。結果が言えるなら結果の形で\nadded = {toml_str(today)}\n\n"
            + "[text]              # 主張を 1〜2 文で。合わせて 80 字、数値は 2 個までが目安\n"
            + 'look = ""           # 図の見方のうち、図に書けないことだけ\n'
            + 'found = ""          # 分かったこと。**鍵になる語句** は色が付く\n'
            + 'open = ""           # 疑問、または次にやること\n'
            + 'limits = ""         # 単一 seed など、結果の限界（小さく出る）\n\n'
            + ("\n".join(blocks) if blocks else "# [[figures]]\n# file = \"fig.png\"\n")
            + "\n# 出どころ（スライドには出さない）\n[source]\n"
            + "".join(f"{k} = {toml_str(v)}\n" for k, v in source.items() if v))
    (cdir / "card.toml").write_text(card, encoding="utf-8")
    append_row(deck, args.section, display_of(args.section), ref, kind, args.title or "（タイトル未記入）", today)
    print(f"created {cdir / 'card.toml'}" + (f" with {len(figures)} figure(s)" if figures else ""))
    print("next: write title and [text] in card.toml, build, then open the PNG of the new slide")
    return 0


# ---------------------------------------------------------------- status, archive

def deck_day(deck: Path, meta: dict) -> datetime | None:
    """The day a deck was presented: from its name (YYYYMMDD...), else from 日付 in INDEX.md."""
    for text in (deck.name, meta.get("日付", "")):
        m = re.search(r"(\d{4})[-/]?(\d{2})[-/]?(\d{2})", text)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 23, 59).astimezone()
            except ValueError:
                continue
    return None


def cmd_status(argv: list[str]) -> int:
    active, archived = decks()
    if not SLIDES.is_dir():
        print("no slides/ yet: ./hub slides new YYYYMMDD")
        return 0
    shown: dict[str, set[str]] = {}     # section -> 問い already on a card of a deck in progress
    print("decks in progress:" if active else "decks in progress: none (./hub slides new YYYYMMDD)")
    for deck in active:
        meta, sections = read_index(deck)
        check = deck / "out" / "check.json"
        state = "not built"
        if check.is_file():
            data = json.loads(check.read_text(encoding="utf-8"))
            newer = any(p.stat().st_mtime > check.stat().st_mtime for p in [deck / "INDEX.md", *deck.glob("cards/*/card.toml")])
            state = f"{data['pages']} 枚, built {data.get('built', '?')[:16]}" + (f", {len(data['problems'])} problem(s)" if data["problems"] else "") + (", cards changed since" if newer else "")
        print(f"  {deck.name}  {meta.get('題', '')}  ({state})")
        for sec in sections:
            new = 0
            for ref in sec["cards"]:
                card = load_card(deck, ref) or {}
                new += card.get("kind", "new") != "recap"
                if card.get("source", {}).get("board"):
                    shown.setdefault(sec["name"], set()).add(card["source"]["board"])
            print(f"      {sec['name']:<26} {len(sec['cards'])} card(s), {new} new")
    last: dict[str, datetime] = {}      # section -> day it was last presented
    for deck in archived:
        meta, sections = read_index(deck)
        day = deck_day(deck, meta)
        for sec in sections:
            if day and (sec["name"] not in last or day > last[sec["name"]]):
                last[sec["name"]] = day
    print(f"archived: {', '.join(p.name for p in archived) or 'none'}")
    print("board answers confirmed after the last presentation, not on a card yet:")
    any_row = False
    for member in member_paths(HUB):
        readme = member / "analysis" / "README.md"
        if not readme.is_file():
            continue
        since = last.get(member.name)
        rows = []
        for line in bd.parse_board(readme.read_text(encoding="utf-8")):
            for r in line["rows"]:
                asof = bd.stated_time(r.get(bd.ASOF, ""))
                if not r.get(bd.QUESTION) or bd.is_open(r) or r[bd.QUESTION] in shown.get(member.name, ()) or (since and (asof is None or asof <= since)):
                    continue
                rows.append((asof, r[bd.QUESTION]))
        if not rows:
            continue
        any_row = True
        rows.sort(key=lambda x: x[0].timestamp() if x[0] else 0.0, reverse=True)
        print(f"  {member.name}  ({len(rows)}; last presented {since.date().isoformat() if since else 'never'})")
        for asof, question in rows[:5]:
            print(f"      {asof.date().isoformat() if asof else '----------'}  {question}")
        if len(rows) > 5:
            print(f"      ... {len(rows) - 5} more")
    if not any_row:
        print("  none")
    return 0


def cmd_archive(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="./hub slides archive")
    ap.add_argument("deck")
    ap.add_argument("--force", action="store_true", help="archive although the last build is missing, stale or has problems")
    args = ap.parse_args(argv)
    deck = deck_dir(args.deck)
    if deck.parent.name == "_archive":
        die(f"{deck.name} is already archived")
    check = deck / "out" / "check.json"
    if not args.force:
        if not check.is_file() or not (deck / "out" / "deck.pdf").is_file():
            die("build the deck first (the archive keeps the PDF that was shown), or pass --force")
        if json.loads(check.read_text(encoding="utf-8"))["problems"]:
            die("the last build reported problems; fix them or pass --force")
    target = SLIDES / "_archive" / deck.name
    if target.exists():
        die(f"{target} already exists")
    kept = 0
    for sec in read_index(deck)[1]:
        for ref in sec["cards"]:
            src = SLIDES / ref
            if ref.startswith("_library/") and src.is_dir() and not (deck / ref).exists():
                shutil.copytree(src, deck / ref)   # the library may change later; the deck keeps what was shown
                kept += 1
    target.parent.mkdir(exist_ok=True)
    deck.rename(target)
    print(f"archived {deck.name} -> {target} ({kept} library card(s) copied into it)")
    print("cards worth reusing (a setup, a schematic, a model explanation) can be moved to slides/_library/<section>/")
    return 0


def main(argv: list[str]) -> int:
    commands = {"status": cmd_status, "new": cmd_new, "add": cmd_add, "build": cmd_build, "archive": cmd_archive}
    if not argv:
        return cmd_status([])
    if argv[0] not in commands:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    return commands[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
