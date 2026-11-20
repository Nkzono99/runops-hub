---
name: hub-slides
description: Add a result to the hub's progress slides, or build and archive a deck (slides/ in the hub, ./hub slides). Use when the user says 「進捗スライドに追加しておいて」, asks for slides for a meeting or a progress report, wants a slide fixed, or has presented a deck. 進捗スライドへの結果の追加、作成、発表後の archive。
---

# 進捗スライド

hub の `slides/` に、結果 1 つを card 1 つとして溜めていき、発表ごとの deck にまとめる。
deck は PDF（と 1 枚ごとの PNG）になる。発表したら archive し、次の deck は前回からの差分だけで作る。

```
slides/
├── <発表日>/INDEX.md          索引 = 並び順。project ごとの節に card を並べた表
├── <発表日>/cards/<slug>/     card.toml、図の写し、描いた図の script と data
├── <発表日>/out/              deck.pdf、png/NN.png、check.json
├── _library/<project>/<slug>/ 使い回す card（設定、模式図、モデルの説明）
└── _archive/<発表日>/         発表済みの deck
```

`./hub slides` は hub directory で実行する。build を login node で走らせてよいか、Typst の場所などは
その hub の `AGENTS.md` に従う。

## 結果を追加する

1. **deck を決める。** `./hub slides status` で作業中の deck を見る。無ければ
   `./hub slides new YYYYMMDD --title "題"`（名前は発表予定日）。
2. **載せる結論を確かめる。** 図を開いて確かめ、LOG か解析の README に記録された結論を載せる。
   記録前の面白い結果も載せてよいが、`limits` に暫定であることと理由（単一 seed、定常前など）を書く。
3. **card を作る。** board にある結果なら問いの文で指す。図と出どころが写され、答えは下書きとして
   comment に入る。
   ```bash
   ./hub slides add 20261127 offset-uv --section conductor_with_terrain --board "離れた地表を照らしても"
   ./hub slides add 20261127 packing --section grain_packing --title "…" --figure projects/grain_packing/analysis/x/out/fig2.png
   ```
   card は追加日に関係なく、その project の節の末尾に入る。節の中の順番は INDEX.md の行を入れ替えて直す。
4. **`card.toml` を書く。** 下の「1 枚の作り方」に従う。
5. **build して、足した枚の PNG を開く。** `./hub slides build 20261127`。直したら build し直す。
6. **報告する。** 足した card、何枚目か、開いて確かめた枚、job を使ったなら id と資源。

## 1 枚の作り方

図に語らせる。文で説明すると、聞き手は文と図を往復して読み解くことになる。

- **図が主役。** 解析の図をそのまま、タイトルの下いっぱいに置く。配置は build が決める（横長の図は文が下、
  正方形や縦長は文が右）。多パネルの図は分割しない。
- **文は主張だけ。** `found` に分かったことを 1 文、必要なら `open` に疑問か次の一手を 1 文。
  `look` には図に書けない見方だけを書く。合わせて 80 字、数値は 2 個までが目安。
- **どれが何かは図の上に書く。** 段、曲線、基準線の名前を文で「上から順に…」と説明しない。
  `marks` の label で図に重ねる。
- **数値は図に載せる。** 主張の根拠になる差は、図の上の矢印と label（「+15 V」）で示す。文に細かい数値を並べない。
- **主張を直接示す図が無ければ、描く。** 分布図から差を読み取らせない。比べたい量だけのプロットを card の中に
  描き（下の「スライド用の図」）、元の図も残す。順番は、元の図 → 主張を示すプロット を基本にする。
- **用語やモデルの説明が要るなら、前に 1 枚置く。** 聞き手が知らないモデル名や条件が図の凡例に出てくるなら、
  その説明の card を結果の前に入れる。使い回せるので `--library` で作る。
- **タイトルは 1 行の短い語句。** 結果が言えるなら結果の形にする（「計算速度に影響なし」）。
- **スライドに出さないもの。** 出どころの path、run id、確認日は `[source]` に書く。build はそれを表示しない。

描き直す範囲: 主張がある新規の結果は、主張を示す図を描く。おさらいや分布図は、解析の図に label を重ねるのを
基本にする。ただし、英語や細かい文字のままで読みにくいなど、描き直した方がだいぶ良くなるなら描き直してよい。

## card.toml

```toml
section = "dust_charging"        # INDEX.md の節（project 名）
kind = "new"                     # new（新規）| recap（おさらい。「今回行ったこと」に出ない）
title = "45°と60°で必要な付着力が基準を超える"
added = "2026-11-20"

[text]
look = "図の見方のうち、図に書けないことだけ"
found = "分かったこと。**鍵になる語句** は色が付く"
open = "疑問（？で終える）、または次にやること"
limits = "単一 seed など、結果の限界（小さく出る）"

[[figures]]                      # 複数並べると、枚数分のスライドになる
file = "fig.png"
from = "projects/dust_charging/analysis/…/fig.png"   # 元の図。変わると build が知らせる
sha256 = "…"
label = "電位"                   # タイトルの後ろに付く。title = "…" なら、その枚だけ別のタイトル
inset = "_library/conductor_with_terrain/geometry/fig.png"   # 右上隅の小さな図

[figures.text]                   # その枚だけの文。無ければ [text] が最初の 1 枚に出る
found = "…"

[[figures.marks]]                # 図への書き込み。位置は図の左上が 0、右下が 1
type = "label"                   # label: x, y, text（size, color, bg = false も可）
x = 0.40                         # rect, ellipse: x, y, w, h    arrow: x1, y1 → x2, y2（先が矢）
y = 0.13
text = "電位"

[source]                         # 出どころ。スライドには出ない
board = "board の問い"
read = "projects/dust_charging/analysis/…/README.md"
runs = "R20261111-0002 R20261111-0003"
asof = "2026-11-14"
```

使い回す card を deck に入れるときは `./hub slides add DECK _library/<project>/<slug>`。
`_library` の card を直すと、作業中の deck すべてに反映される（archive 済みの deck は写しを持つので変わらない）。

## スライド用の図

card の directory に `make_fig.py` と `data.csv` を置き、PNG を同じ場所に出す。

- **data は解析の出力から写す。** 新しい解析はしない。`data.csv` の先頭の comment に、どの file のどの行かを書く。
  解析が要るなら、その project で解析として行い、結果を記録してから載せる。
- **大きさ。** `figsize=(8.8, 3.5)`、文字 14 pt で描くと、スライド上で本文とほぼ同じ大きさになる。文を右に置く
  正方形の図なら `figsize=(4.2, 4.2)` 程度。`dpi=220`。
- **見せるのは 1 つの比較。** 軸は 1 つ（二軸にしない）。系列は必要なものだけ。対照は灰色の破線にして直接名前を付ける。
- **直接 label。** 凡例の代わりに、線や点のそばに名前を書く。主張の根拠の点にだけ、差を矢印と数値で書く。
  すべての点に数値を付けない。
- **色。** 文字と数値は黒か灰色。色は系列の識別だけに使う。格子と軸は薄くする。
- 実行は、図の library がある Python で、site の規則に従う。描いた図の `[[figures]]` には `from` を書かず、
  `[source]` の `data` に元の file を書く（`from` は写した図が元と同じかを確かめるためのもの）。

## build の検査と、PNG で見ること

`./hub slides build DECK` は最後に 1 枚ごとの配置、図の面積、文の字数を出す。

- `!!` は直すまで終了 status が 1 になる: 文がどの配置でも収まらない（削るか card を分ける。文字は縮めない）、
  タイトルが空か 1 行に収まらない、card や図が無い。
- `※` は目安を外れたときの注意: 文が 80 字超、文中の数値が 3 個以上、文のせいで図が小さい、
  元の図が写しの後に更新された（図を写し直すか、古い図のまま出す理由を `limits` に書く）。

検査は見た目を見ない。足した枚・直した枚は PNG を開いて、次を 1 つずつ確かめる。

- label や矢印が、曲線・凡例・軸の文字・隣のパネルに重なっていないか。指している場所は合っているか。
- 文と label が、図と食い違っていないか（色、向き、段の順、単位、符号）。図を見ながら 1 語ずつ照合する。
  作った本人には、書いたつもりのものが見えてしまう。
- 図の中の文字が潰れていないか。隅の小さな図は目印として読めるか。
- 余白が片側に偏っていないか。

位置合わせは 2〜3 回の往復になる。開いていない枚は、開いていないと報告する。
正式な発表の前に依頼があれば、元の資料を見ていない別の agent に PNG だけを渡して講評を受ける。
講評は「通すのに必要」と「改善案」の 2 つに分けて返してもらう。

## 発表のあと

`./hub slides archive DECK` で `_archive/` へ移す。使っていた `_library` の card は写しが入り、当時の deck を
再現できる。次の deck は `./hub slides new` で空から始め、おさらいは `_library` から足す。
発表した card のうち、次も使う説明（設定、模式図、モデルの説明）は `_library/<project>/` へ移すことを提案する。

`./hub slides status` は、前回の発表以降に確認日が進んだ board の答えのうち、まだ card になっていないものを
project ごとに出す。次の deck の候補として使う。

## この作り方の由来

- 図の大きさ、文の量、図への書き込みは、利用者の過去の進捗スライドと、試作への指摘（2026-11-20）に合わせた。
  一般的なスライドの規則（1 枚に図 1 パネル、枚数の上限、タイトルは必ず文）は採っていない。枚数に上限は無く、
  発表者が飛ばして重みを調整する。
- 既存の slide 作成 skill の調査から採ったもの: 描画した画像を見て直す（作者は期待したものを見てしまう）、
  あふれたら文字を縮めず分ける、見た目の講評には欠陥の名前を渡す、機械的に検査できることは検査に任せる。
