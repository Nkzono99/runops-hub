# runops hub

複数の runops project を 1 か所から見て、作業を振り分けるための入口。ここ自体は runops project
ではなく、各 project は `projects/<名前>/` に symlink で並んでいる。project はそれぞれ自己完結しており、直接開いて作業することもできる。

| dir | simulator | 内容 |
|---|---|---|

## 最初の依頼を受けた agent へ

人が行う準備は hub の `init` と、この directory で agent を起動するところまで。
project の追加・作成、runops の準備、以後の操作は agent が行う。人に `attach` などの
コマンドを実行してもらう前提にせず、依頼の path と目的を使って作業する。

1. `runo` が使えるか確認する。無ければ agent が次を実行し、tool の bin directory を
   この session の PATH に加える。既に使える runops は、この準備のために更新しない。

   ```bash
   uv tool install --python 3.11 --from git+https://github.com/Nkzono99/runops runops
   export PATH="$(uv tool dir --bin):$PATH"
   ```

2. 既存 project を加える依頼なら、指定された directory の `runops.toml` と `AGENTS.md` を読む。
   simulator と目的を確かめ、`./hub attach NAME PATH SIM... --desc "目的"` で加える。
   名前や説明は内容から決められる範囲で決める。path や対象が不明なら、その不足だけを聞く。
3. 新しい計算を始める依頼なら、まず既存 project の続きで扱えるか調べる。
   新規なら `./hub new NAME SIM... --desc "目的"` を使い、作成先の `AGENTS.md` と `runo context` に従う。
   simulator の設定・計算資源・投入の確認は project 側の規則に従う。
4. 追加後は `./hub context` と `./hub relink --check` で入口と skill を確かめ、
   何を加えたかと次にできることを短く返す。以後も自然文の依頼を hub / project の操作へ移す。

## 作業の進め方

- **project を決めてから動く。** run を変える依頼で project が分からなければ、`./hub list` で状況を見せて対象を確かめる。
- **project の中で作業する。** `runo` は cwd から `runops.toml` を探すので、`cd <dir>` してから使う。
  初めての project では `<dir>/AGENTS.md` と `runo context` を読み、その進め方に従う。
- **記録は project に置く。** run の観察、結論、知見はその project の `runo note`・`LOG.md`・`notes/` に書く。
  run id は project ごとの番号なので、別 project の run は path で参照する。
- **全体を見るときは `./hub`。** 「全部の進捗を見て」は `./hub sync` → `./hub status` で答える。
- **外したもの（`./hub shelf`）は名指しされたときだけ扱う。** path は変わらないので読むのはそのままでよい。
  run を投入・継続するなら先に `./hub restore` する（外したままだと sync・status・auto に出ない）。

| command | 用途 |
|---|---|
| `./hub sync` / `list [--json]` / `context` | 全 project で同じ `runo` を実行（`list`・`context` は先頭に直接 session も表示） |
| `./hub status [--peek]` | 全 project の件数と active run、前回の確認以降に状態が変わった run、完了後に LOG・note・解析 file に run id が出てこない run。`./hub sync` の後に使う。作業の途中で見るだけなら `--peek` |
| `./hub board` | 人が見る `board.html` を作り直す。各 project の「分かっていること」（結論、図、図の読み方）と「まだ分かっていないこと」だけを並べ、進捗や経緯は載せない。`./hub sync` のたびにも作り直される。元は各 project の `analysis/README.md` の `<!-- board -->` の表。表の答えは、解析を確かめた agent が判断して足す・直し（図を開いて確かめ、LOG か解析の README に記録した結論、限界も書く）、報告で変えた行を示す |
| `./hub doctor` / `update` | 環境と skill の検査 / 更新（`update` の後は skill link も張り直す） |
| `./hub sessions` | この login node で各 project に開いている tmux・claude・codex |
| `./hub logs [DIR [--apply]]` | 各 project・論文・hub のジョブログを場所別に一覧する。`DIR` 指定で直下を確認し、`--apply` で終了したジョブのログを `DIR/logs/` にまとめる。解析・図・PDF の新しいジョブは投入前に作業場所の `logs/`（PDF は `build/logs/`）を作ってそこに出す。 |
| `./hub slides [status\|new\|add\|build\|archive]` | 進捗スライド（`slides/`）。結果 1 つを card 1 つとして project ごとの節に足し、発表ごとの deck を PDF にする。発表後に archive し、次は差分だけで作る。作り方は skill `hub-slides` に従う |
| `./hub relink [--check] [-v]` | hub の skill link を member から作り直し、member と論文の実体 directory を Claude Code の作業 directory（`.claude/settings.json`）に揃える。`new`・`attach`・`detach`・`update` も最後に実行する |
| `./hub new NAME SIM... --desc TEXT` | 新しい project（`../project_NAME`）を作って hub に加える |
| `./hub attach NAME PATH SIM... --desc TEXT` | 既にある runops project を hub に加える |
| `./hub detach NAME` | 終了した project を hub から外す（symlink と表の行を消す。実体は残る） |
| `./hub defer TARGET [--why TEXT] [--until DATE]` / `archive TARGET [--why TEXT]` / `restore TARGET` / `shelf` | 後回しにする project・論文（`defer`）と、完了・停止したもの（`archive`）を一覧と各 command から外す。link と実体と path はそのままで、表の行は `.hub/shelf.toml` に移る。`restore` で戻し、`shelf` で外したものを見る。TARGET は `NAME`・`projects/NAME`・`papers/NAME`。待ち・実行中の job がある project は `--force` が要る |
| `./hub term NAME` | 人が使う。project の tmux session に入る（無ければ作る） |
| `./hub auto [status\|start\|pause\|resume\|stop\|tick\|enable\|...]` | 定期実行の agent（下の「自動運用」）。command は `./hub auto help` |

`./hub` は読み取りと保守のための道具で、run を変える `runo` は対象 project の中で実行する。

## 自動運用（hub auto）

`./hub auto` のループは、`auto/config.toml` で有効にした project を一定間隔で `runo sync` し、完了・失敗から
しばらくたっても記録の無い run があれば、project ごとに `codex exec` を起動して解析させ、明らかに必要な継続と
小さい派生を週の予算内で投入させる。仕事が無い回は agent を起動しない。設定・状態・記録はすべて `auto/` にある。

- **有効にする project は人が決める。** `./hub auto enable NAME [--no-continue] [--no-derive]` / `disable NAME`。
- **auto の枠内の投入は承認済み。** 有効な project で `hub auto submit` の検査（`auto/config.toml` の予算、継続の条件、
  重複）を通った投入は、事前確認の規則の例外とする。枠を超えるものは提案に積まれ、`./hub auto approve N` で投入する。
- **作業 agent は auto の予約を避ける。** `./hub status` と `./hub auto status` に出る claim の run には触らない。
  急ぐなら `./hub auto release PROJECT/RUN`、触らせたくない run・project は `./hub auto hold TARGET`。
- **止める・再開する**: `./hub auto pause [--now]`、`resume`、`stop [--now]`。どの ssh session・login node からでも効く。

## Skills

`.claude/skills/` と `.agents/skills/` は、各 project に展開された skill への symlink で、
`./hub relink` が全 member の和集合から作る（同名なら version が新しいもの）。skill は対象 project に `cd` してから使う。
