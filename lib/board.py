#!/usr/bin/env python3
"""One HTML page for the person: what each project knows so far, and what it does not know yet.

The page is the project's standing summary, not a progress view. Progress, queued runs and
the detail of work in progress are asked of an agent (``./hub status``). The source is the
table between ``<!-- board -->`` and ``<!-- /board -->`` in a member's ``analysis/README.md``::

    - 表示名: <short project title, optional>
    ### <line of work>
    - 論文: papers/<name>
    | 問い | いまの答え | 図 | 対象 run | 読む場所 | 確認日 |

A row with an answer is a finding: the first sentence of ``いまの答え`` is the conclusion, shown
large, and ``図`` is one figure that shows it, written ``![how to read it](path)`` with a path
from ``analysis/`` (a figure the theme's report already has). A row with an empty answer is a
question still open; it is listed under the findings of its line of work.

Two things are derived from the run ledger, so the summary does not go stale unnoticed.
``対象 run`` holds run directories in backticks (```runs/a/b/```) and run ids, and ``確認日`` is
``YYYY-MM-DD`` or ``YYYY-MM-DD HH:MM``, the time up to which the answer covers the results:
a finding whose runs completed after that time is marked as older than the results, and an
open question whose runs are waiting or running is marked as being worked on.

Writes ``board.html`` in the hub directory. Figures are embedded as data URIs; links are
relative to the hub directory.
"""
from __future__ import annotations

import base64
import html
import mimetypes
import os
import posixpath
import re
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
ACTIVE = ("running", "submitted", "created")
IMAGE_LIMIT = 4_000_000  # bytes; a larger figure is linked, not embedded


def esc(text: object) -> str:
    return html.escape(str(text), quote=True)


# ---------------------------------------------------------------- the hand-written table

def table_cells(line: str) -> list[str]:
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def parse_board(text: str) -> list[dict]:
    """Lines of work: ``{"title", "meta": {key: value}, "rows": [{column: cell}]}``."""
    block = BLOCK_RE.search(text)
    if not block:
        return []
    lines: list[dict] = []
    header: list[str] | None = None
    for raw in block.group(1).splitlines():
        s = raw.strip()
        if s.startswith("### "):
            lines.append({"title": s[4:].strip(), "meta": {}, "rows": []})
            header = None
        elif not lines:
            continue
        elif s.startswith("- ") and re.search(r"[:：]", s):
            key, value = re.split(r"[:：]", s[2:], maxsplit=1)
            lines[-1]["meta"][key.strip()] = value.strip()
        elif s.startswith("|"):
            row = table_cells(s)
            if header is None:
                header = row
            elif not set("".join(row)) <= set("-: "):
                lines[-1]["rows"].append(dict(zip(header, row)))
    return lines


def project_title(text: str, fallback: str) -> str:
    """An optional human-readable title before the first line of work."""
    block = BLOCK_RE.search(text)
    if block:
        preamble = block.group(1).split("### ", 1)[0]
        title = re.search(r"^- 表示名[:：]\s*(.+)$", preamble, re.MULTILINE)
        if title:
            return title.group(1).strip()
    return fallback


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
    """Member name -> the one-line goal in the project table of the hub AGENTS.md."""
    try:
        text = (hub / "AGENTS.md").read_text(encoding="utf-8")
    except OSError:
        return {}
    found = {}
    for line in text.splitlines():
        cell = table_cells(line) if line.startswith("| `") else []
        if len(cell) == 3 and cell[0].endswith("/`") and "papers/" not in cell[0]:
            found[cell[0].strip("`/")] = cell[2]
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


def render_finding(row: dict, state: dict, base: str, hub: Path) -> str:
    """One finding the way a report shows it: the conclusion, the figure, how to read it."""
    head, rest = split_answer(row.get(ANSWER, ""))
    caption, path = figure_of(row.get(FIGURE, ""))
    figure = ""
    if path:
        rel = posixpath.normpath(posixpath.join(base, path))
        figure = ('<figure>'
                  + (f'<figcaption>{esc(caption)}</figcaption>' if caption else "")
                  + f'<button class="figure-zoom" type="button" aria-label="図を拡大: {esc(row.get(QUESTION, ""))}">'
                  f'<img src="{embedded(hub / rel) or esc(rel)}" alt="{esc(caption)}" loading="lazy">'
                  '<span class="zoom-hint">図を拡大 ↗</span></button></figure>')
    stale = ""
    if state["newer"]:
        ids = ", ".join(sorted(r["id"] for r in state["newer"])[:4])
        stale = (f'<p class="stale"><span aria-hidden="true">⚠</span> この答えのあとに完了した計算が '
                 f'{len(state["newer"])} 本あります（{esc(ids)}）。答えはまだ反映していません。</p>')
    more = [f'<span class="source">{inline(row[READ], base, hub)}</span>'] if row.get(READ) else []
    if row.get(ASOF):
        more.append(f'<span class="asof">確認 {esc(row[ASOF])}</span>')
    return (
        f'<section class="finding"><h4>{inline(row.get(QUESTION, ""), base, hub)}</h4>'
        f'<p class="lead">{inline(head, base, hub)}</p>{stale}{figure}'
        + (f"<p>{inline(rest, base, hub)}</p>" if rest else "")
        + f'<p class="more">{"".join(more)}</p></section>'
    )


def render_open(rows: list[tuple[dict, dict]], base: str, hub: Path) -> str:
    """The questions of a line of work that have no answer yet."""
    if not rows:
        return ""
    items = ""
    for row, state in rows:
        working = any(r["status"] in ACTIVE for r in state["runs"])
        done = len(state["newer"])
        tag = '<span class="tag">計算中</span>' if working else (f'<span class="tag">計算は完了、解析待ち</span>' if done else "")
        items += f'<li>{inline(row.get(QUESTION, ""), base, hub)}{tag}</li>'
    return f'<div class="open"><h4>まだ分かっていないこと</h4><ul>{items}</ul></div>'


def render_line(line: dict, runs: list[dict], base: str, hub: Path) -> tuple[str, int]:
    """One line of work, and how many of its findings are older than the results."""
    states = [(row, row_state(row, runs)) for row in line["rows"]]
    found = [(row, state) for row, state in states if not is_open(row)]
    paper = line["meta"].get("論文", "")
    body = "".join(render_finding(row, state, base, hub) for row, state in found)
    if not found:
        body = '<p class="sub">分かったことは、まだありません。</p>'
    return (
        f'<section class="line"><h3>{esc(line["title"])}'
        + '</h3>'
        + (f'<p class="paper">論文 {inline(paper, base, hub)}</p>' if paper else "")
        + f'{body}{render_open([(row, state) for row, state in states if is_open(row)], base, hub)}</section>'
    ), sum(bool(state["newer"]) for _, state in found)


CSS = """
:root{color-scheme:light;--page:#fcfbf8;--ink:#262a28;--muted:#626760;--hair:#d6d8d0;--accent:#884b38;
--serif:"Noto Serif CJK JP","Noto Serif JP","Yu Mincho","Hiragino Mincho ProN",serif;
--sans:"Noto Sans CJK JP","Hiragino Sans","Yu Gothic",Meiryo,sans-serif}
*{box-sizing:border-box}
html{scroll-padding-top:2rem}
body{margin:0;background:var(--page);color:var(--ink);font:15px/1.95 var(--sans);-webkit-font-smoothing:antialiased}
a{color:inherit;text-underline-offset:4px;text-decoration-thickness:1px;text-decoration-color:var(--hair)}
a:hover{color:var(--accent);text-decoration-color:currentColor}
:focus-visible{outline:2px solid var(--accent);outline-offset:5px}
button{font:inherit;color:inherit}
code{font:.85em ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;overflow-wrap:anywhere}
p{margin:.7rem 0;max-width:52rem}
h1,h2,h3,h4{line-height:1.6;text-wrap:pretty}
h1{font:500 clamp(2rem,3.8vw,3rem)/1.5 var(--serif);letter-spacing:.06em;margin:2.6rem 0 .8rem}
h2{font-size:1.65rem;font-weight:600;margin:0}
h3{font-size:1.02rem;font-weight:600;margin:0}
h4{font-size:.86rem;font-weight:400;color:var(--muted);margin:0 0 .6rem}
.shell{max-width:82rem;margin:auto;padding:0 3rem}
.masthead{padding:1.7rem 0 2.8rem;border-top:4px solid var(--ink)}
.masthead-line{display:flex;justify-content:space-between;align-items:baseline;gap:1rem;font-size:.78rem;color:var(--muted)}
.brand{color:var(--ink);letter-spacing:.04em;text-decoration:none;font-weight:600}
.brand span{font-weight:400;margin-left:1rem;border-left:1px solid var(--hair);padding-left:1rem}
.intro{color:var(--muted);font-size:.94rem;line-height:1.9}
.layout{display:grid;grid-template-columns:12.5rem minmax(0,1fr);gap:3.5rem;border-top:1px solid var(--ink)}
.contents{align-self:start;position:sticky;top:2rem;padding-top:2rem;font-size:.85rem}
.contents-label{font-size:.72rem;letter-spacing:.08em;color:var(--muted);margin:0 0 1rem}
.contents ol{list-style:none;padding:0;margin:0}
.contents li{border-bottom:1px solid var(--hair)}
.contents a{display:flex;gap:.9rem;text-decoration:none;padding:.9rem 0;line-height:1.75}
.contents .num{color:var(--accent);font:12px/2 ui-monospace,monospace;padding-top:.1rem}
.contents .back-top{font-size:.75rem;color:var(--muted);margin-top:1.4rem}
main{min-width:0}
.project{padding:2rem 0 4rem;scroll-margin-top:1rem}
.project+.project{border-top:1px solid var(--ink)}
.project-id{display:flex;gap:1rem;align-items:baseline;color:var(--muted);margin:0 0 .7rem;font-size:.8rem}
.project-id .num{color:var(--accent);font:13px ui-monospace,monospace}
.project-description{color:var(--muted);font-size:.86rem;margin:.6rem 0 0}
.line{margin-top:2.7rem}
.line>h3{padding:.65rem 0;border-top:1px solid var(--hair);border-bottom:1px solid var(--hair)}
.paper{font-size:.75rem;color:var(--muted);margin:.5rem 0 0}
.finding{margin-top:2.3rem}
.finding+.finding{margin-top:3rem}
.lead{font:600 clamp(1.15rem,1.7vw,1.5rem)/1.85 var(--serif);letter-spacing:.015em;margin:0 0 1rem}
.stale{border-left:2px solid var(--accent);padding-left:.85rem;font-size:.8rem;color:var(--muted)}
figure{margin:1.3rem 0 1.2rem}
figcaption{font-size:.84rem;line-height:1.9;color:var(--muted);max-width:52rem;margin:0 0 .85rem}
img{max-width:100%;height:auto;display:block;background:#fff}
.figure-zoom{display:block;width:100%;padding:0;border:0;background:transparent;text-align:left;cursor:zoom-in}
.figure-zoom img{width:100%}
.zoom-hint{display:block;font-size:.7rem;line-height:1.6;color:var(--muted);text-align:right;margin-top:.4rem}
.more{display:flex;flex-wrap:wrap;gap:.5rem 1.3rem;color:var(--muted);font-size:.75rem;margin-top:1rem}
.asof{font-variant-numeric:tabular-nums}
a.alt{margin-left:.65rem;font-size:.7rem}
.open{margin-top:2.3rem;border-left:2px solid var(--hair);padding:.1rem 0 .1rem 1.2rem}
.open h4{font-size:.8rem;color:var(--ink);font-weight:600;letter-spacing:.04em;margin:.15rem 0 .6rem}
.open ul{list-style:none;padding:0;margin:0;font-size:.87rem}
.open li+li{margin-top:.65rem}
.tag{font-size:.7rem;color:var(--muted);display:inline-block;margin-left:.7rem}
.tag::before{content:"— ";color:var(--hair)}
.sub{font-size:.85rem;color:var(--muted)}
.colophon{border-top:1px solid var(--ink);padding:1.2rem 0 2.5rem;color:var(--muted);font-size:.75rem}
.colophon p{margin:.4rem 0}
.skip-link{position:absolute;top:-5rem;left:1rem;background:var(--page);padding:.5rem 1rem;z-index:5}
.skip-link:focus{top:1rem}
dialog{max-width:96vw;width:96vw;height:94vh;max-height:94vh;border:1px solid var(--hair);padding:0;background:var(--page);color:var(--ink)}
dialog::backdrop{background:rgb(20 24 22 / .7)}
.viewer-bar{display:flex;gap:1rem;align-items:center;justify-content:space-between;padding:.6rem 1rem;border-bottom:1px solid var(--hair)}
.viewer-bar p{font-size:.8rem;margin:0}
.viewer-bar button{border:1px solid var(--hair);background:var(--page);padding:.3rem .8rem;white-space:nowrap;cursor:pointer}
.viewer-scroll{overflow:auto;height:calc(100% - 4rem);background:white;padding:1rem}
.viewer-scroll img{max-width:none;width:auto;margin:auto}
@media (max-width:1000px){.shell{padding:0 2rem}.layout{grid-template-columns:10rem minmax(0,1fr);gap:2rem}}
@media (max-width:760px){
 .shell{padding:0 1.2rem}.masthead{padding:1.2rem 0 2rem}.masthead-line{align-items:start;flex-direction:column;gap:.4rem}
 h1{margin-top:1.7rem}.layout{display:block}.contents{position:static;padding:1.1rem 0 1.5rem;border-bottom:1px solid var(--hair)}
 .contents ol{display:grid;grid-template-columns:1fr 1fr;gap:0 1.2rem}.contents a{padding:.6rem 0;font-size:.8rem;gap:.6rem}
 .contents .back-top{display:none}.contents-label{margin:0 0 .3rem}.project{padding-top:1.7rem}h2{font-size:1.4rem}
 .line{margin-top:2rem}.lead{line-height:1.9}.more{display:block}.asof{display:block;margin-top:.4rem}
}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--page:#1c201e;--ink:#e5e7de;--muted:#b0b6ad;--hair:#454e47;--accent:#d5a68d}}
@media print{
 :root{color-scheme:light;--page:#fff;--ink:#222;--muted:#555;--hair:#ccc;--accent:#555}
 .shell{max-width:none;padding:0}.layout{display:block}.contents,.skip-link,.zoom-hint,dialog{display:none}
 .masthead{padding-top:1rem}h1{margin-top:1rem}.project{break-before:page}.project:first-child{break-before:auto}
 .finding{break-inside:avoid}h2,h3,h4,.lead,figcaption{break-after:avoid}a{text-decoration:none}
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
    viewerTitle.textContent = button.closest('.finding').querySelector('h4').textContent;
    viewer.showModal();
    document.body.style.overflow = 'hidden';
    viewer.querySelector('.viewer-scroll').scrollTo(0, 0);
  });
});
viewer.querySelector('button').addEventListener('click', () => viewer.close());
viewer.addEventListener('click', event => { if (event.target === viewer) viewer.close(); });
viewer.addEventListener('close', () => { document.body.style.overflow = ''; });
"""


def build(hub: Path, now: datetime) -> tuple[str, int]:
    """The page, and how many findings are older than the results."""
    goal = descriptions(hub)
    sections: list[str] = []
    navigation: list[str] = []
    missing: list[str] = []
    stale = 0
    for member in st.members():
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
        body = ""
        for line in lines:
            block, old = render_line(line, runs, f"{name}/analysis", hub)
            body += block
            stale += old
        title = project_title(text, name)
        number = f"{len(sections) + 1:02d}"
        anchor = f"project-{name}"
        navigation.append(f'<li><a href="#{esc(anchor)}"><span class="num">{number}</span><span>{esc(title)}</span></a></li>')
        sections.append(
            f'<section class="project" id="{esc(anchor)}" aria-labelledby="{esc(anchor)}-title">'
            f'<header class="project-heading"><p class="project-id"><span class="num">{number}</span><code>{esc(name)}</code></p>'
            f'<h2 id="{esc(anchor)}-title">{esc(title)}</h2><p class="project-description">{esc(goal.get(name, ""))}</p></header>{body}</section>')
    rest = (f'<h2>まとめのまだ無い project</h2><p>{esc("、".join(missing))}</p>'
            '<p class="sub"><code>analysis/README.md</code> に <code>&lt;!-- board --&gt;</code> の表を作ると、ここに並びます。</p>'
            if missing else "")
    page = f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>分かっていること — runops hub</title><style>{CSS}</style></head><body id="top">
<a class="skip-link" href="#findings">本文へ</a>
<div class="shell"><header class="masthead">
<div class="masthead-line"><a class="brand" href="#top">runops hub<span>研究ノート</span></a>
<span>生成 <time datetime="{esc(now.isoformat(timespec='minutes'))}">{esc(now.strftime("%Y.%m.%d %H:%M"))}</time></span></div>
<h1>分かっていること</h1>
<p class="intro">それぞれの問いに、いまどこまで答えられるか。<br>得られた結果と、それを支える図、残っている問いをまとめています。</p>
</header><div class="layout">
<nav class="contents" aria-label="プロジェクト目次"><p class="contents-label">プロジェクト</p>
<ol>{"".join(navigation)}</ol><a class="back-top" href="#top">ページの先頭へ ↑</a></nav>
<main id="findings">
{"".join(sections)}
{rest}
</main></div><footer class="colophon">
<p>各プロジェクトの解析索引をもとに生成。確認日は、各結論に反映した結果の時点を示します。</p>
<p>計算の進捗は agent または <code>./hub status</code> で確認できます。</p>
</footer></div>
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
