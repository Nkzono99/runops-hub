#!/usr/bin/env python3
"""One HTML page for the person: what each project knows so far, and what it does not know yet.

The page reads like a deck of slides, not like a report. Each project has its own tab (the hash
names it: ``#p-<name>``, or ``#f-<name>-<n>`` for one finding); the first tab lists the projects
folded, each opening onto its conclusions as one-line claims. A project's tab shows its setting as
a schematic beside a table, then one card per finding: a headline, one figure, the point in a
sentence or two, and the conditions in small type. Without JavaScript every tab is shown in turn. How to read the figure and the full answer stay in the table, for
agents and for whoever digs in; the page folds them away. Progress, queued runs and
the detail of work in progress are asked of an agent (``./hub status``).

The source is the table between ``<!-- board -->`` and ``<!-- /board -->`` in a member's
``analysis/README.md``::

    - 表示名: <short project title, optional>
    - 模式図: ![how to read it](path)      a drawing of the setup, beside the table below (optional)

    | 計算の設定 | 値 |                  the model behind the findings, one item a row (optional)

    ### <line of work>
    - 論文: papers/<name>

    | 設定 | 値 |                        what this line of work adds or changes (optional)

    | 問い | 見出し | 要点 | 条件 | いまの答え | 図 | 対象 run | 読む場所 | 確認日 |

A table ends at its first line that is not a row, so the tables are kept apart by a blank line.
``- 設定: <sentence>`` still works in place of a table, and a line of work may have its own
``- 模式図:``. A schematic is drawn by a script in the project (``analysis/board-setting/``) and
says 模式図 on itself.

A row with an answer is a finding. The card shows

- ``見出し``: the conclusion as a claim that stands without the question, like a slide title.
- ``要点``: one or two sentences with the deciding numbers, about 80 characters at most.
  ``**...**`` marks the one phrase to see first.
- ``条件``: what this result rests on and where it stops (window, seeds, what is not settled).
- ``図``: one figure that shows it, ``![how to read it](path)`` with a path from ``analysis/``
  (a figure the theme's report already has).

``いまの答え`` is the full answer with every number and caveat, folded away on the page. A row
without ``見出し`` still shows: the first sentence of the answer becomes the headline and the
rest the text. A row with an empty answer is a question still open; it is listed under the
findings of its line of work.

Two things are derived from the run ledger, so the summary does not go stale unnoticed.
``対象 run`` holds run directories in backticks (```runs/a/b/```) and run ids, and ``確認日`` is
``YYYY-MM-DD`` or ``YYYY-MM-DD HH:MM``, the time up to which the answer covers the results:
a finding whose runs completed after that time is marked as older than the results, and an
open question whose runs are waiting or running is marked as being worked on.

Writes ``board.html`` in the hub directory. Figures are embedded as data URIs; links are
relative to the hub directory. Projects follow the order of the project table in the hub
``AGENTS.md``.
"""
from __future__ import annotations

import base64
import html
import mimetypes
import os
import posixpath
import re
import struct
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import status as st  # noqa: E402

BLOCK_RE = re.compile(r"<!--\s*board\s*-->(.*?)<!--\s*/board\s*-->", re.DOTALL)
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
FIGURE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
TIME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:[ T](\d{1,2}:\d{2}))?")
QUESTION, ANSWER, FIGURE, RUNS, READ, ASOF = "問い", "いまの答え", "図", "対象 run", "読む場所", "確認日"
HEADLINE, POINT, CONDITIONS, SETTING, SCHEMATIC = "見出し", "要点", "条件", "設定", "模式図"
SETTING_HEADS = ("計算の設定", "設定")  # the first header cell of a table of settings
ACTIVE = ("running", "submitted", "created")
IMAGE_LIMIT = 4_000_000  # bytes; a larger figure is linked, not embedded
UPRIGHT = 1.4  # a figure narrower than this (width / height) sits beside its text
UPRIGHT_SCHEMATIC = 1.7  # a schematic narrower than this sits beside its table of settings


def esc(text: object) -> str:
    return html.escape(str(text), quote=True)


# ---------------------------------------------------------------- the hand-written table

def table_cells(line: str) -> list[str]:
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def meta_line(s: str) -> tuple[str, str] | None:
    """``- key: value`` as (key, value)."""
    if s.startswith("- ") and re.search(r"[:：]", s):
        key, value = re.split(r"[:：]", s[2:], maxsplit=1)
        return key.strip(), value.strip()
    return None


def read_tables(lines: list[str]) -> tuple[list[dict], list[tuple[str, str]]]:
    """The rows of the findings table (headed by 問い) and of a settings table (headed 計算の設定 or 設定).

    A table ends at the first line that is not a row, so a settings table can stand above the findings.
    """
    rows: list[dict] = []
    setting: list[tuple[str, str]] = []
    header: list[str] | None = None
    for raw in lines:
        s = raw.strip()
        if not s.startswith("|"):
            header = None
            continue
        row = table_cells(s)
        if header is None:
            header = row
        elif set("".join(row)) <= set("-: "):
            continue
        elif QUESTION in header:
            rows.append(dict(zip(header, row)))
        elif header[0] in SETTING_HEADS:
            setting.append((row[0], row[1] if len(row) > 1 else ""))
    return rows, setting


def parse_board(text: str) -> list[dict]:
    """Lines of work: ``{"title", "meta": {key: value}, "rows": [{column: cell}], "setting": [(item, value)]}``."""
    block = BLOCK_RE.search(text)
    if not block:
        return []
    lines: list[dict] = []
    body: list[list[str]] = []
    for raw in block.group(1).splitlines():
        s = raw.strip()
        if s.startswith("### "):
            lines.append({"title": s[4:].strip(), "meta": {}, "rows": [], "setting": []})
            body.append([])
        elif not lines:
            continue
        elif meta_line(s):
            key, value = meta_line(s)
            lines[-1]["meta"][key] = value
            body[-1].append("")
        else:
            body[-1].append(raw)
    for line, raw in zip(lines, body):
        line["rows"], line["setting"] = read_tables(raw)
    return lines


def preamble(text: str) -> dict[str, str]:
    """The ``- key: value`` lines before the first line of work (表示名, 設定, 模式図)."""
    block = BLOCK_RE.search(text)
    found: dict[str, str] = {}
    if block:
        for raw in block.group(1).split("### ", 1)[0].splitlines():
            pair = meta_line(raw.strip())
            if pair:
                found[pair[0]] = pair[1]
    return found


def preamble_setting(text: str) -> list[tuple[str, str]]:
    """The settings table before the first line of work."""
    block = BLOCK_RE.search(text)
    return read_tables(block.group(1).split("### ", 1)[0].splitlines())[1] if block else []


def project_title(text: str, fallback: str) -> str:
    """An optional human-readable title before the first line of work."""
    return preamble(text).get("表示名") or fallback


def selector(cell: str) -> tuple[list[str], set[str]]:
    """Run directories (with a trailing slash) and run ids named in a ``対象 run`` cell."""
    prefixes = [p.rstrip("/") + "/" for p in re.findall(r"`([^`]+)`", cell) if p.startswith("runs/")]
    return prefixes, st.run_ids_in(cell)


def selected(run: dict, prefixes: list[str], ids: set[str]) -> bool:
    return run["id"] in ids or any(run.get("run_dir", "").startswith(p) for p in prefixes)


def stated_time(cell: str) -> datetime | None:
    """``確認日`` as a local time; a bare date covers that whole day."""
    found = TIME_RE.search(cell)
    if not found:
        return None
    return datetime.fromisoformat(f"{found.group(1)}T{(found.group(2) or '23:59').zfill(5)}:00").astimezone()


def finished_time(run: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(run.get("finished_at") or "")
    except ValueError:
        return None


def row_state(row: dict, runs: list[dict]) -> dict:
    """The runs of a row, and those that completed after the row's answer was confirmed."""
    prefixes, ids = selector(row.get(RUNS, ""))
    mine = [r for r in runs if selected(r, prefixes, ids)]
    asof = stated_time(row.get(ASOF, ""))
    done = [r for r in mine if r["status"] == "completed" and finished_time(r)]
    newer = [r for r in done if asof is None or finished_time(r) > asof]
    return {"runs": mine, "asof": asof, "newer": newer}


def is_open(row: dict) -> bool:
    """A question without an answer yet."""
    return not row.get(ANSWER, "").strip()


def split_answer(text: str) -> tuple[str, str]:
    """The first sentence (the conclusion shown large) and the rest."""
    head, *rest = re.split(r"(?<=。)", text.strip(), maxsplit=1)
    return head, (rest[0].strip() if rest else "")


def figure_of(cell: str) -> tuple[str, str]:
    """``(how to read it, path)`` from a ``図`` cell written as ``![how to read it](path)``."""
    found = FIGURE_RE.search(cell)
    return (found.group(1).strip(), found.group(2)) if found else ("", "")


def card_text(row: dict) -> dict[str, str]:
    """What a card shows, and the full answer when the short columns stand in for it.

    A row with ``要点`` folds the full answer away. A row written before the short columns
    existed shows its first sentence as the headline and the rest as the text.
    """
    answer = row.get(ANSWER, "").strip()
    first, rest = split_answer(answer)
    headline, point = row.get(HEADLINE, "").strip(), row.get(POINT, "").strip()
    if point:
        shown, folded = point, answer
    elif headline:
        shown, folded = answer, ""
    else:
        shown, folded = rest, ""
    return {"headline": headline or first, "point": shown, "conditions": row.get(CONDITIONS, "").strip(),
            "folded": folded}


def open_tag(state: dict) -> str:
    """Whether an open question is being computed, or computed and waiting for analysis."""
    if any(r["status"] in ACTIVE for r in state["runs"]):
        return "計算中"
    return "計算は完了、解析待ち" if state["newer"] else ""


# ---------------------------------------------------------------- what the ledger knows

def load_runs(member: Path) -> list[dict]:
    """Every run in view, with its finish time; scratch runs only while active or pinned."""
    code, data = st.runo_json(member, ["list", "--all", "--scratch", "--brief"])
    if code != 0 or not isinstance(data, list):
        return []
    runs = []
    for r in data:
        if r.get("scratch") and r["status"] not in ACTIVE and not r.get("pinned"):
            continue
        if r["status"] == "completed":
            r["finished_at"], _ = st.finished(member / r.get("run_dir", ""))
        runs.append(r)
    return runs


def descriptions(hub: Path) -> dict[str, str]:
    """Member name -> the one-line goal in the project table of the hub AGENTS.md, in table order."""
    try:
        text = (hub / "AGENTS.md").read_text(encoding="utf-8")
    except OSError:
        return {}
    found = {}
    for line in text.splitlines():
        cell = table_cells(line) if line.startswith("| `") else []
        if len(cell) == 3 and cell[0].endswith("/`") and "papers/" not in cell[0]:
            found[cell[0].strip("`/").removeprefix("projects/")] = cell[2]
    return found


# ---------------------------------------------------------------- HTML pieces

def inline(text: str, base: str, hub: Path) -> str:
    """Inline Markdown as HTML. Relative links are read from *base* (a directory under the hub)."""
    def link(found: re.Match) -> str:
        label, target = found.group(1), html.unescape(found.group(2))
        if re.match(r"[a-z]+:|[#/]", target):
            return f'<a href="{esc(target)}">{label}</a>'
        path, _, fragment = target.partition("#")
        rel = posixpath.normpath(posixpath.join(base, path))
        out = f'<a href="{esc(rel + ("#" + fragment if fragment else ""))}">{label}</a>'
        report = posixpath.join(posixpath.dirname(rel), "report.html")
        if posixpath.basename(rel) == "README.md" and (hub / report).is_file():
            out += f'<a class="alt" href="{esc(report)}" title="図を内包した HTML 版">HTML</a>'
        return out

    out = LINK_RE.sub(link, esc(text))
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", out)


def embedded(path: Path) -> str:
    """The image as a data URI, so the page stays one file. Empty if missing or too large."""
    try:
        if path.stat().st_size > IMAGE_LIMIT:
            return ""
        data = path.read_bytes()
    except OSError:
        return ""
    kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{kind};base64,{base64.b64encode(data).decode('ascii')}"


def image_size(path: Path) -> tuple[int, int] | None:
    """Width and height of a PNG, read from its header; None for anything else."""
    try:
        with path.open("rb") as f:
            head = f.read(24)
    except OSError:
        return None
    if len(head) == 24 and head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
        width, height = struct.unpack(">II", head[16:24])
        return (width, height) if width and height else None
    return None


def render_figure(path: str, caption: str, label: str, base: str, hub: Path, upright_below: float = UPRIGHT) -> tuple[str, bool]:
    """The figure as a button that opens it large, and whether it is upright (sits beside the text)."""
    rel = posixpath.normpath(posixpath.join(base, path))
    size = image_size(hub / rel)
    upright = bool(size) and size[0] / size[1] < upright_below
    dims = f' width="{size[0]}" height="{size[1]}"' if size else ""
    return (f'<figure class="fig"><button class="figure-zoom" type="button" aria-label="図を拡大: {esc(label)}" data-title="{esc(label)}">'
            f'<img src="{embedded(hub / rel) or esc(rel)}" alt="{esc(caption)}"{dims} loading="lazy" decoding="async">'
            '</button></figure>'), upright


def render_finding(row: dict, state: dict, base: str, hub: Path, anchor: str = "") -> str:
    """One finding as a slide: headline, figure, the point, the conditions; the rest folded."""
    text = card_text(row)
    caption, path = figure_of(row.get(FIGURE, ""))
    figure, upright = render_figure(path, caption, text["headline"], base, hub) if path else ("", False)
    main = f'<p class="point">{inline(text["point"], base, hub)}</p>' if text["point"] else ""
    if text["conditions"]:
        main += f'<p class="cond">{inline(text["conditions"], base, hub)}</p>'
    if state["newer"]:
        ids = ", ".join(sorted(r["id"] for r in state["newer"])[:4])
        main += (f'<p class="stale">この答えのあとに完了した計算が {len(state["newer"])} 本あります'
                 f'（{esc(ids)}）。答えはまだ反映していません。</p>')
    folded, parts = "", []
    if figure and caption:
        folded += f'<p class="howto"><span class="label">図の見方</span>{inline(caption, base, hub)}</p>'
        parts.append("図の見方")
    if text["folded"]:
        folded += f'<p class="full"><span class="label">いまの答え</span>{inline(text["folded"], base, hub)}</p>'
        parts.append("くわしい答え")
    if row.get(RUNS, "").strip():
        folded += f'<p class="runs"><span class="label">対象 run</span>{inline(row[RUNS], base, hub)}</p>'
        parts.append("対象 run")
    side = f'<details class="more"><summary>{"・".join(parts)}</summary>{folded}</details>' if folded else ""
    meta = [f'<span class="source">{inline(row[READ], base, hub)}</span>'] if row.get(READ) else []
    if row.get(ASOF):
        meta.append(f'<span class="asof">確認 {esc(row[ASOF])}</span>')
    if meta:
        side += f'<p class="meta">{"".join(meta)}</p>'
    ident = f' id="{esc(anchor)}"' if anchor else ""
    return (
        f'<article class="card {"side" if upright else "wide"}"{ident}>'
        f'<p class="ask">{inline(row.get(QUESTION, ""), base, hub)}</p>'
        f'<h4 class="headline">{inline(text["headline"], base, hub)}</h4>'
        f'<div class="card-body">{figure}<div class="card-text"><div class="main">{main}</div>'
        f'<div class="side-notes">{side}</div></div></div></article>'
    )


def render_setting(meta: dict, table: list[tuple[str, str]], label: str, base: str, hub: Path) -> str:
    """The setting behind the findings: a schematic (``- 模式図: ![how to read it](path)``) with a table, or a sentence."""
    caption, path = figure_of(meta.get(SCHEMATIC, ""))
    figure, upright = render_figure(path, caption, label, base, hub, UPRIGHT_SCHEMATIC) if path else ("", False)
    text = f'<p class="setting-text">{inline(meta[SETTING], base, hub)}</p>' if meta.get(SETTING) else ""
    rows = "".join(f'<tr><th scope="row">{inline(item, base, hub)}</th><td>{inline(value, base, hub)}</td></tr>' for item, value in table)
    rows = f'<table class="setting-table"><tbody>{rows}</tbody></table>' if rows else ""
    if not (figure or text or rows):
        return ""
    layout = "" if not figure else (" side" if upright else " wide")
    return (f'<div class="setting{layout}"><p class="setting-label"><span class="label">{esc(label)}</span></p>'
            f'{figure}<div class="setting-body">{text}{rows}</div></div>')


def render_open(rows: list[tuple[dict, dict]], base: str, hub: Path) -> str:
    """The questions of a line of work that have no answer yet."""
    if not rows:
        return ""
    items = ""
    for row, state in rows:
        tag = open_tag(state)
        items += f'<li>{inline(row.get(QUESTION, ""), base, hub)}' + (f'<span class="tag">{tag}</span>' if tag else "") + "</li>"
    return f'<div class="open"><h5>まだ分かっていないこと</h5><ul>{items}</ul></div>'


def render_line(line: dict, runs: list[dict], base: str, hub: Path, anchors: list[str] | None = None) -> tuple[str, int]:
    """One line of work, and how many of its findings are older than the results.

    *anchors* name the finding cards in order (the overview links to them).
    """
    states = [(row, row_state(row, runs)) for row in line["rows"]]
    found = [(row, state) for row, state in states if not is_open(row)]
    names = iter(anchors or [])
    body = "".join(render_finding(row, state, base, hub, next(names, "")) for row, state in found)
    head = f'<h3>{esc(line["title"])}</h3>'
    if line["meta"].get("論文"):
        head += f'<p class="paper"><span class="label">論文</span>{inline(line["meta"]["論文"], base, hub)}</p>'
    head += render_setting(line["meta"], line.get("setting", []), "この系列の設定", base, hub)
    return (
        f'<section class="line"><header class="line-head">{head}</header>'
        f'{body}{render_open([(row, state) for row, state in states if is_open(row)], base, hub)}</section>'
    ), sum(bool(state["newer"]) for _, state in found)


def render_tile(project: dict) -> str:
    """A project in the overview, folded: its name and counts; opened, its claims and open questions."""
    base, hub = project["base"], project["hub"]
    items = ""
    for item in project["findings"]:
        flag = '<span class="flag" title="この答えのあとに完了した計算があります">新しい計算あり</span>' if item["stale"] else ""
        items += f'<li><a href="#{esc(item["anchor"])}">{inline(item["headline"], base, hub)}</a>{flag}</li>'
    for row, state in project["open"]:
        tag = open_tag(state)
        items += (f'<li class="ask">{inline(row.get(QUESTION, ""), base, hub)}'
                  + (f'<span class="tag">{tag}</span>' if tag else "") + "</li>")
    counts = [f"分かっていること {len(project['findings'])}"] if project["findings"] else []
    counts += [f"まだ {len(project['open'])}"] if project["open"] else []
    stale = sum(item["stale"] for item in project["findings"])
    return (f'<details class="tile"><summary><span class="tile-name">{esc(project["title"])}</span>'
            f'<span class="tile-count">{esc("・".join(counts))}</span>'
            + (f'<span class="flag">新しい計算あり {stale}</span>' if stale else "") + "</summary>"
            + '<div class="tile-body">'
            + (f'<p class="purpose">{esc(project["purpose"])}</p>' if project["purpose"] else "")
            + f'<ul>{items}</ul><p class="tile-go"><a href="#p-{esc(project["name"])}">このテーマのページへ →</a></p>'
            + "</div></details>")


CSS = """
:root{color-scheme:light;--page:#fff;--ink:#1f2328;--ink-2:#3d444d;--muted:#59636e;--hair:#d1d9e0;--rule:#e5e9ee;
--accent:#c2410c;--figure:#fff;
--sans:"Noto Sans CJK JP","Noto Sans JP","Hiragino Sans","Hiragino Kaku Gothic ProN","Yu Gothic UI","Yu Gothic",Meiryo,system-ui,sans-serif;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--page:#101317;--ink:#e6e8eb;--ink-2:#c9ced5;
--muted:#9aa3ad;--hair:#3a414a;--rule:#272d34;--accent:#f0883e;--figure:#fff}}
:root[data-theme="dark"]{color-scheme:dark;--page:#101317;--ink:#e6e8eb;--ink-2:#c9ced5;--muted:#9aa3ad;--hair:#3a414a;
--rule:#272d34;--accent:#f0883e;--figure:#fff}
*{box-sizing:border-box}
html{scroll-padding-top:1rem}
body{margin:0;background:var(--page);color:var(--ink);font:16px/1.8 var(--sans);-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
a{color:inherit;text-underline-offset:3px;text-decoration-thickness:1px;text-decoration-color:var(--hair)}
a:hover{color:var(--accent);text-decoration-color:currentColor}
:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
button{font:inherit;color:inherit}
code{font:.86em var(--mono);overflow-wrap:anywhere}
p{margin:0}
h1,h2,h3,h4,h5{margin:0;line-height:1.45;text-wrap:pretty}
.label{display:inline-block;font-size:.72rem;font-weight:600;letter-spacing:.04em;color:var(--muted);
border:1px solid var(--hair);border-radius:3px;padding:0 .45em;margin-right:.6em;line-height:1.6;vertical-align:.1em;white-space:nowrap}
.shell{max-width:90rem;margin:auto;padding:0 2rem}
.skip-link{position:absolute;top:-5rem;left:1rem;background:var(--page);padding:.5rem 1rem;z-index:5}
.skip-link:focus{top:1rem}

/* the top bar, the tabs beside the page, and the overview */
.topbar{display:flex;justify-content:space-between;align-items:baseline;gap:1rem;padding:.9rem 0 .8rem;border-bottom:1px solid var(--rule);
font-size:.8rem;color:var(--muted)}
.brand{font-weight:600;color:var(--ink-2);letter-spacing:.03em}
.brand .sep{color:var(--hair);font-weight:400;margin:0 .5rem}
.layout{display:grid;grid-template-columns:15rem minmax(0,1fr);gap:0 2.6rem;align-items:start}
.tabs{position:sticky;top:0;max-height:100vh;overflow-y:auto;padding:1.3rem 0 2rem}
.tabs ol{list-style:none;margin:0;padding:0}
.tabs a{display:block;text-decoration:none;padding:.45rem .6rem .45rem .85rem;border-left:2px solid var(--rule);color:var(--ink-2);
font-size:.9rem;line-height:1.45}
.tabs a:hover{color:var(--accent)}
.tabs a[aria-current="page"]{border-left-color:var(--ink);color:var(--ink);font-weight:700}
.tab-count{display:block;font-size:.72rem;font-weight:400;color:var(--muted);margin-top:.1rem}
.tab-count .new{color:var(--accent)}
main{min-width:0;padding-top:1.3rem}
.tabbed .project{margin-top:0}
.tabbed .project-head{border-top:0;padding-top:0}
.pager{display:flex;justify-content:space-between;align-items:baseline;gap:1rem;flex-wrap:wrap;border-top:1px solid var(--rule);
margin-top:2.6rem;padding-top:1rem;font-size:.88rem}
.pager a{text-decoration:none;color:var(--ink-2)}
.pager a:hover{color:var(--accent)}
.masthead{padding:.6rem 0 1.4rem}
h1{font-size:clamp(1.9rem,1.4rem + 1.6vw,2.6rem);font-weight:700;letter-spacing:.02em;margin:0 0 .5rem}
.lede{color:var(--ink-2);font-size:1rem}
.lede .count{font-weight:700;font-variant-numeric:tabular-nums}
.lede .hint{display:block;color:var(--muted);font-size:.85rem;margin-top:.2rem}
kbd{font:.78rem var(--mono);border:1px solid var(--hair);border-bottom-width:2px;border-radius:3px;padding:0 .35em}
.overview{border-top:2px solid var(--ink);margin:.2rem 0 1rem}
.tile{border-bottom:1px solid var(--rule)}
.tile>summary{display:flex;flex-wrap:wrap;align-items:baseline;gap:.2rem .9rem;padding:.7rem .2rem;cursor:pointer;list-style:none}
.tile>summary::-webkit-details-marker{display:none}
.tile>summary::before{content:"";align-self:center;width:.45rem;height:.45rem;border-right:1.5px solid var(--muted);
border-bottom:1.5px solid var(--muted);transform:rotate(-45deg);margin:0 .25rem 0 .15rem;transition:transform .15s}
.tile[open]>summary::before{transform:rotate(45deg)}
.tile>summary:hover .tile-name{color:var(--accent)}
.tile-name{font-weight:700;font-size:1.05rem}
.tile-count{font-size:.8rem;color:var(--muted);font-variant-numeric:tabular-nums}
.tile-body{padding:0 .2rem 1.1rem 1.55rem;max-width:56rem}
.tile .purpose{font-size:.82rem;line-height:1.65;color:var(--muted)}
.tile ul{list-style:none;padding:0;margin:.55rem 0 0}
.tile li{position:relative;padding-left:1.05rem;font-size:.94rem;line-height:1.6;margin:.38rem 0 0}
.tile li::before{content:"";position:absolute;left:.1rem;top:.62em;width:.4rem;height:.4rem;border-radius:50%;background:var(--ink-2)}
.tile li a{text-decoration:none}
.tile li a:hover{text-decoration:underline}
.tile li.ask{color:var(--muted);font-size:.88rem}
.tile li.ask::before{background:transparent;border:1.5px solid var(--muted);width:.44rem;height:.44rem;top:.58em;left:.08rem}
.tile li:not(.ask)+li.ask{margin-top:.75rem}
.tile-go{font-size:.8rem;color:var(--muted);margin-top:.7rem}
.tag,.flag{display:inline-block;font-size:.7rem;line-height:1.55;border:1px solid var(--hair);border-radius:3px;padding:0 .45em;
margin-left:.5em;white-space:nowrap;vertical-align:.1em;color:var(--muted)}
.flag{border-color:currentColor;color:var(--accent);font-weight:600}
.tile-count+.flag{margin-left:0}

/* a project, its lines of work and their findings */
.project{margin-top:3.4rem;scroll-margin-top:.5rem}
.project-head{border-top:2px solid var(--ink);padding-top:1rem}
.eyebrow{display:flex;gap:.8rem;align-items:baseline;font-size:.78rem;color:var(--muted)}
.eyebrow .num{font:600 .8rem var(--mono)}
.project-head h2{font-size:clamp(1.5rem,1.2rem + 1vw,2rem);font-weight:700;margin:.15rem 0 .2rem}
.project-head .purpose{color:var(--muted);font-size:.92rem}
.setting{margin-top:1.1rem;max-width:72rem}
.setting-label{margin-bottom:.55rem}
.setting.side{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.1fr);gap:.2rem 2.2rem;align-items:start}
.setting.side .setting-label{grid-column:1/-1}
.setting .figure-zoom img{max-height:min(58vh,560px)}
.setting.side .figure-zoom img{max-height:min(74vh,720px)}
.setting.wide .setting-body{margin-top:1rem;max-width:56rem}
.setting-text{font-size:.9rem;line-height:1.75;color:var(--ink-2);max-width:64rem}
.setting-text+.setting-table{margin-top:.6rem}
.setting-table{border-collapse:collapse;width:100%;font-size:.88rem;line-height:1.65}
.setting-table th{text-align:left;font-weight:600;color:var(--ink-2);padding:.42rem 1.2rem .42rem 0;border-top:1px solid var(--rule);
vertical-align:top;white-space:nowrap;width:8.5em}
.setting-table td{padding:.42rem 0;border-top:1px solid var(--rule)}
.setting-table tr:last-child>*{border-bottom:1px solid var(--rule)}
.line .setting{margin-top:.8rem;max-width:56rem}
.line{margin-top:2.4rem}
.line-head h3{font-size:1.2rem;font-weight:700}
.paper{font-size:.86rem;color:var(--ink-2);margin-top:.35rem;max-width:64rem}
.card{border-top:1px solid var(--rule);padding:1.7rem 0 1.6rem;margin-top:1rem;scroll-margin-top:.6rem}
.ask{font-size:.84rem;color:var(--muted)}
.headline{font-size:clamp(1.25rem,1.05rem + .75vw,1.65rem);font-weight:700;line-height:1.5;letter-spacing:.01em;margin:.15rem 0 .9rem}
.fig{margin:0;text-align:center;background:var(--figure);border-radius:4px}
.figure-zoom{display:block;width:100%;padding:0;border:0;background:transparent;cursor:zoom-in;border-radius:4px}
.figure-zoom img{display:block;margin:auto;width:auto;height:auto;max-width:100%;max-height:min(64vh,700px);background:var(--figure)}
.card-text{margin-top:1rem;max-width:52rem}
.card.side .card-body{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr);gap:2rem;align-items:start}
.card.side .card-text{margin-top:0}
.card.side .figure-zoom img{max-height:min(66vh,720px)}
.point{font-size:1.08rem;line-height:1.85;color:var(--ink)}
.point strong{color:var(--accent)}
.cond{font-size:.88rem;line-height:1.75;color:var(--muted);margin-top:.55rem}
.stale{font-size:.84rem;line-height:1.7;color:var(--ink-2);border-left:3px solid var(--accent);padding:.05rem 0 .05rem .8rem;margin-top:.8rem}
.more{margin-top:.8rem;font-size:.85rem;line-height:1.75;color:var(--ink-2)}
.more summary{cursor:pointer;color:var(--muted);font-size:.8rem;width:max-content;max-width:100%}
.more summary:hover{color:var(--accent)}
.more p{margin-top:.55rem}
.more .howto,.more .runs{color:var(--muted)}
.more .runs{font-size:.8rem}
.meta{display:flex;flex-wrap:wrap;gap:.3rem 1.1rem;font-size:.78rem;color:var(--muted);margin-top:.6rem}
.asof{font-variant-numeric:tabular-nums}
a.alt{margin-left:.55rem;font-size:.7rem}
.open{border-top:1px dashed var(--hair);padding:1.2rem 0 .4rem;margin-top:1rem}
.open h5{font-size:.82rem;font-weight:700;color:var(--ink-2);letter-spacing:.04em}
.open ul{list-style:none;padding:0;margin:.35rem 0 0}
.open li{position:relative;padding-left:1.05rem;font-size:.95rem;line-height:1.7;margin-top:.35rem}
.open li::before{content:"";position:absolute;left:.08rem;top:.62em;width:.44rem;height:.44rem;border-radius:50%;border:1.5px solid var(--muted)}
.missing{margin-top:3rem;color:var(--muted);font-size:.9rem}
.colophon{border-top:1px solid var(--rule);margin-top:3rem;padding:1.1rem 0 2.6rem;color:var(--muted);font-size:.78rem;line-height:1.9}
dialog{max-width:96vw;width:96vw;height:94vh;max-height:94vh;border:1px solid var(--hair);border-radius:4px;padding:0;background:var(--page);color:var(--ink)}
dialog::backdrop{background:rgb(15 18 22 / .72)}
.viewer-bar{display:flex;gap:1rem;align-items:center;justify-content:space-between;padding:.6rem 1rem;border-bottom:1px solid var(--rule)}
.viewer-bar p{font-size:.88rem;font-weight:600}
.viewer-bar button{border:1px solid var(--hair);background:var(--page);border-radius:3px;padding:.3rem .8rem;white-space:nowrap;cursor:pointer}
.viewer-scroll{overflow:auto;height:calc(100% - 3.4rem);background:#fff;padding:1rem}
.viewer-scroll img{max-width:none;width:auto;display:block;margin:auto}
@media (max-width:900px){
 .shell{padding:0 1rem}.card.side .card-body{display:block}.card.side .card-text{margin-top:1rem}
 .card.side .figure-zoom img,.figure-zoom img{max-height:none}.tile-body{padding-left:1.2rem}
 .setting.side{display:block}.setting.side .setting-body{margin-top:1rem}.setting-table th{white-space:normal;width:6.5em}
 .lede .hint{display:none}.topbar{flex-direction:column;gap:.1rem}
 html{scroll-padding-top:3.2rem}.layout{display:block}
 .tabs{position:sticky;top:0;z-index:4;background:var(--page);max-height:none;overflow-x:auto;overflow-y:hidden;padding:.25rem 0;
 border-bottom:1px solid var(--rule)}
 .tabs ol{display:flex;gap:.1rem;white-space:nowrap}
 .tabs a{border-left:0;border-bottom:2px solid transparent;padding:.4rem .6rem;font-size:.85rem}
 .tabs a[aria-current="page"]{border-bottom-color:var(--ink)}
 .tab-count{display:none}
}
@media (prefers-reduced-motion:reduce){.tile>summary::before{transition:none}}
@media print{
 .skip-link,dialog,.lede .hint,.tabs,.pager{display:none}.shell{max-width:none;padding:0}.layout{display:block}
 .view[hidden]{display:block!important}.card{break-inside:avoid}.project{break-before:page}.more:not([open]){display:none}
 a{text-decoration:none}
}
"""

SCRIPT = """
const viewer = document.querySelector('#figure-viewer');
const largeImage = viewer.querySelector('img');
const viewerTitle = viewer.querySelector('#viewer-title');
document.querySelectorAll('.figure-zoom').forEach(button => {
  button.addEventListener('click', () => {
    const source = button.querySelector('img');
    largeImage.src = source.src;
    largeImage.alt = source.alt;
    viewerTitle.textContent = button.dataset.title || '図の拡大';
    viewer.showModal();
    document.body.style.overflow = 'hidden';
    viewer.querySelector('.viewer-scroll').scrollTo(0, 0);
  });
});
viewer.querySelector('button').addEventListener('click', () => viewer.close());
viewer.addEventListener('click', event => { if (event.target === viewer) viewer.close(); });
viewer.addEventListener('close', () => { document.body.style.overflow = ''; });

// One theme per tab. The hash names the tab (#p-name) or a finding in it (#f-name-3).
document.documentElement.classList.add('tabbed');
const views = [...document.querySelectorAll('[data-view]')];
const tabs = [...document.querySelectorAll('.tabs a')];
const strip = document.querySelector('.tabs');
const baseTitle = document.title;
function show(view, target) {
  views.forEach(v => { v.hidden = v !== view; });
  let current = null;
  tabs.forEach(a => {
    const on = a.dataset.tab === view.dataset.view;
    if (on) { a.setAttribute('aria-current', 'page'); current = a; } else a.removeAttribute('aria-current');
  });
  if (current && strip.scrollWidth > strip.clientWidth) {
    strip.scrollLeft = current.offsetLeft - (strip.clientWidth - current.offsetWidth) / 2;
  }
  document.title = view.dataset.title ? view.dataset.title + ' — ' + baseTitle : baseTitle;
  if (target && target !== view) target.scrollIntoView({block: 'start'});
  else window.scrollTo(0, 0);
}
function route() {
  const id = decodeURIComponent(location.hash.slice(1));
  const target = id ? document.getElementById(id) : null;
  const view = target ? target.closest('[data-view]') : null;
  show(view || views[0], target);
}
window.addEventListener('hashchange', route);
route();

// j / k (or the right and left arrows) step through the findings of the open tab like slides.
const still = matchMedia('(prefers-reduced-motion: reduce)').matches;
document.addEventListener('keydown', event => {
  if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || viewer.open) return;
  const target = event.target;
  if (target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName))) return;
  const step = {j: 1, ArrowRight: 1, k: -1, ArrowLeft: -1}[event.key];
  const cards = [...document.querySelectorAll('[data-view]:not([hidden]) .card')];
  if (!step || !cards.length) return;
  const edge = (parseFloat(getComputedStyle(document.documentElement).scrollPaddingTop) || 0) + 40;
  const tops = cards.map(card => card.getBoundingClientRect().top);
  let current = -1;
  tops.forEach((top, i) => { if (top <= edge) current = i; });
  let next = current + step;
  if (step < 0 && current >= 0 && tops[current] < -40) next = current;
  next = Math.max(0, Math.min(cards.length - 1, next));
  event.preventDefault();
  cards[next].scrollIntoView({block: 'start', behavior: still ? 'auto' : 'smooth'});
});
"""


def order(members: list[Path], goal: dict[str, str]) -> list[Path]:
    """Members in the order of the hub's project table; the rest after them by name."""
    rank = {name: i for i, name in enumerate(goal)}
    return sorted(members, key=lambda m: (rank.get(m.name, len(rank)), m.name))


def render_tab(project: dict) -> str:
    """A project in the tab list: its name and how many findings and open questions it has."""
    counts = [f"結論 {len(project['findings'])}"] if project["findings"] else []
    counts += [f"問い {len(project['open'])}"] if project["open"] else []
    stale = sum(item["stale"] for item in project["findings"])
    more = f'<span class="new">・新しい計算 {stale}</span>' if stale else ""
    return (f'<li><a href="#p-{esc(project["name"])}" data-tab="{esc(project["name"])}"><span class="tab-name">{esc(project["title"])}</span>'
            f'<span class="tab-count">{esc("・".join(counts))}{more}</span></a></li>')


def render_pager(projects: list[dict], i: int) -> str:
    """Links to the themes before and after, and back to the overview."""
    before = (f'<a class="prev" href="#p-{esc(projects[i - 1]["name"])}">← {esc(projects[i - 1]["title"])}</a>'
              if i > 0 else "<span></span>")
    after = (f'<a class="next" href="#p-{esc(projects[i + 1]["name"])}">{esc(projects[i + 1]["title"])} →</a>'
             if i + 1 < len(projects) else "<span></span>")
    return f'<nav class="pager" aria-label="前後のテーマ">{before}<a href="#overview">一覧</a>{after}</nav>'


def build(hub: Path, now: datetime) -> tuple[str, int]:
    """The page, and how many findings are older than the results."""
    goal = descriptions(hub)
    projects: list[dict] = []
    missing: list[str] = []
    stale = 0
    for member in order(st.members(), goal):
        name = member.name
        try:
            text = (member / "analysis" / "README.md").read_text(encoding="utf-8")
        except OSError:
            text = ""
        lines = parse_board(text)
        if not lines:
            missing.append(name)
            continue
        runs = load_runs(member)
        base = str(member.relative_to(hub) / "analysis")
        meta = preamble(text)
        project = {"name": name, "title": meta.get("表示名") or name, "purpose": goal.get(name, ""), "base": base, "hub": hub,
                   "findings": [], "open": [], "body": ""}
        for line in lines:
            anchors = []
            for row in line["rows"]:
                if is_open(row):
                    project["open"].append((row, row_state(row, runs)))
                    continue
                anchors.append(f"f-{name}-{len(project['findings']) + 1}")
                project["findings"].append({"anchor": anchors[-1], "headline": card_text(row)["headline"],
                                            "stale": bool(row_state(row, runs)["newer"])})
            block, old = render_line(line, runs, base, hub, anchors)
            project["body"] += block
            stale += old
        project["setting"] = render_setting(meta, preamble_setting(text), "計算の設定", base, hub)
        projects.append(project)
    sections = []
    for i, project in enumerate(projects):
        name, title = project["name"], project["title"]
        sections.append(
            f'<section class="view project" id="p-{esc(name)}" data-view="{esc(name)}" data-title="{esc(title)}" '
            f'aria-labelledby="p-{esc(name)}-title">'
            f'<header class="project-head"><p class="eyebrow"><span class="num">{i + 1:02d}</span><code>{esc(name)}</code></p>'
            f'<h2 id="p-{esc(name)}-title">{esc(title)}</h2>'
            + (f'<p class="purpose">{esc(project["purpose"])}</p>' if project["purpose"] else "")
            + f'{project["setting"]}</header>{project["body"]}{render_pager(projects, i)}</section>')
    found = sum(len(p["findings"]) for p in projects)
    asked = sum(len(p["open"]) for p in projects)
    rest = (f'<p class="missing">まとめのまだ無い project: {esc("、".join(missing))}（<code>analysis/README.md</code> に '
            '<code>&lt;!-- board --&gt;</code> の表を作ると、ここに並びます）</p>' if missing else "")
    counts = (f'<span class="count">{len(projects)}</span> テーマ、分かっていること <span class="count">{found}</span> 件、'
              f'まだ分かっていないこと <span class="count">{asked}</span> 件'
              + (f'、答えのあとに新しい計算が出たもの <span class="count">{stale}</span> 件' if stale else "") + "。")
    tabs = '<li><a href="#overview" data-tab="overview"><span class="tab-name">一覧</span></a></li>' + "".join(map(render_tab, projects))
    page = f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>分かっていること — runops hub</title><style>{CSS}</style></head><body>
<a class="skip-link" href="#findings">本文へ</a>
<div class="shell">
<header class="topbar"><span class="brand">runops hub<span class="sep">/</span>分かっていること</span>
<span>生成 <time datetime="{esc(now.isoformat(timespec='minutes'))}">{esc(now.strftime("%Y.%m.%d %H:%M"))}</time></span></header>
<div class="layout">
<nav class="tabs" aria-label="テーマ"><ol>{tabs}</ol></nav>
<main id="findings">
<section class="view overview-view" id="overview" data-view="overview">
<header class="masthead"><h1>分かっていること</h1>
<p class="lede">{counts}<span class="hint">左でテーマを選ぶと、そのテーマのページに移ります。下の一覧はテーマを開くと結論が並び、結論を押すとその図と条件に移ります。<kbd>J</kbd> <kbd>K</kbd>（または <kbd>→</kbd> <kbd>←</kbd>）で結論を順に送れます。</span></p>
</header>
<nav class="overview" aria-label="テーマごとの結論">{"".join(render_tile(p) for p in projects)}</nav>
{rest}
</section>
{"".join(sections)}
<footer class="colophon">
<p>各 project の <code>analysis/README.md</code> の board の表から生成。くわしい答えと数値は各結論の「くわしい答え」に、計算の進捗は <code>./hub status</code> か agent に。</p>
<p>確認日は、その答えに反映した結果の時点です。</p>
</footer>
</main></div></div>
<dialog id="figure-viewer" aria-labelledby="viewer-title">
<div class="viewer-bar"><p id="viewer-title">図の拡大</p><button type="button" autofocus>閉じる ×</button></div>
<div class="viewer-scroll"><img alt=""></div></dialog>
<script>{SCRIPT}</script></body></html>
"""
    return page, stale


def main() -> int:
    hub = st.HUB
    page, stale = build(hub, datetime.now().astimezone())
    out = hub / "board.html"
    temporary = out.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(page, encoding="utf-8")
    temporary.replace(out)
    print(f"board: {out}")
    if stale and "--quiet" not in sys.argv[1:]:
        print(f"  結果より古い答えが {stale} 件あります")
    return 0


if __name__ == "__main__":
    sys.exit(main())
