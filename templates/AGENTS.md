# runops hub

複数の runops project を 1 か所から見て、作業を振り分けるための入口。ここ自体は runops project
ではなく、各 project は symlink で並んでいる。project はそれぞれ自己完結しており、直接開いて作業することもできる。

| dir | simulator | 内容 |
|---|---|---|

## 作業の進め方

- **project を決めてから動く。** run を変える依頼で project が分からなければ、`./hub list` で状況を見せて対象を確かめる。
- **project の中で作業する。** `runo` は cwd から `runops.toml` を探すので、`cd <dir>` してから使う。
  初めての project では `<dir>/AGENTS.md` と `runo context` を読み、その進め方に従う。
- **記録は project に置く。** run の観察、結論、知見はその project の `runo note`・`LOG.md`・`notes/` に書く。
  run id は project ごとの番号なので、別 project の run は path で参照する。
- **全体を見るときは `./hub`。** 「全部の進捗を見て」は `./hub sync` → `./hub status` で答える。

| command | 用途 |
|---|---|
| `./hub sync` / `list [--json]` / `context` | 全 project で同じ `runo` を実行（`list`・`context` は先頭に直接 session も表示） |
| `./hub status [--peek]` | 全 project の件数と active run、前回の確認以降に状態が変わった run、完了後に LOG・note・解析 file に run id が出てこない run。`./hub sync` の後に使う。作業の途中で見るだけなら `--peek` |
| `./hub board` | 人が見る `board.html` を作り直す。各 project の「分かっていること」（結論、図、図の読み方）と「まだ分かっていないこと」だけを並べ、進捗や経緯は載せない。`./hub sync` のたびにも作り直される。元は各 project の `analysis/README.md` の `<!-- board -->` の表。表の答えは、解析を確かめた agent が判断して足す・直し（図を開いて確かめ、LOG か解析の README に記録した結論、限界も書く）、報告で変えた行を示す |
| `./hub doctor` / `update` | 環境と skill の検査 / 更新（`update` の後は skill link も張り直す） |
| `./hub sessions` | この login node で各 project に開いている tmux・claude・codex |
| `./hub relink [--check] [-v]` | hub の skill link を member から作り直し、member と論文の実体 directory を Claude Code の作業 directory（`.claude/settings.json`）に揃える。`new`・`attach`・`detach`・`update` も最後に実行する |
| `./hub new NAME SIM... --desc TEXT` | 新しい project（`../project_NAME`）を作って hub に加える |
| `./hub attach NAME PATH SIM... --desc TEXT` | 既にある runops project を hub に加える |
| `./hub detach NAME` | 終了した project を hub から外す（symlink と表の行を消す。実体は残る） |
| `./hub term NAME` | 人が使う。project の tmux session に入る（無ければ作る） |

`./hub` は読み取りと保守のための道具で、run を変える `runo` は対象 project の中で実行する。

## Skills

`.claude/skills/` と `.agents/skills/` は、各 project に展開された skill への symlink で、
`./hub relink` が全 member の和集合から作る（同名なら version が新しいもの）。skill は対象 project に `cd` してから使う。
