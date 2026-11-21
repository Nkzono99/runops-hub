# コマンドと運用のリファレンス

普段の依頼ではエージェントが `./hub` を実行するため、人がコマンドを覚える必要はありません。
run の作成・投入・中止など状態を変える `runo` コマンドは、対象 project に `cd` して実行します。

## `./hub` コマンド一覧

| command | 用途 |
|---|---|
| `./hub attach NAME PATH SIM... --desc TEXT` | 既存の runops project を `projects/` に加える |
| `./hub new NAME SIM... --desc TEXT` | `../project_NAME` に新しい project を作って加える |
| `./hub detach NAME` | hub から外す（symlink と表の行だけを消す。実体は残る） |
| `./hub defer TARGET [--why TEXT] [--until DATE]` | project か論文を「後回し」として一覧から外す（下の「外す・戻す」） |
| `./hub archive TARGET [--why TEXT]` | 完了・停止した project か論文を一覧から外す |
| `./hub restore TARGET` / `./hub shelf` | 外したものを戻す / 外したものを一覧する |
| `./hub sync` / `list [--json]` / `context` / `doctor` / `update` | 全 project で同じ `runo` を実行する（`sync` のたびに `board.html` も再生成） |
| `./hub status [--peek]` | 全 project の件数と active run、前回の確認以降に状態が変わった run、完了後に記録が無い run |
| `./hub board` | `board.html` を hub directory に生成する（試作） |
| `./hub slides [status\|new\|add\|build\|archive]` | 進捗スライドを `slides/` で管理し、Typst で PDF と 1 枚ごとの PNG にする |
| `./hub logs [DIR [--apply]]` | hub・各 project・論文のジョブログを場所別に数え、`--apply` で `logs/` に集める |
| `./hub relink [--check] [-v]` | skill の link と、project・論文の実体 directory に対する Claude Code 設定を揃える |
| `./hub sessions` | この node で各 project に開いている tmux・claude・codex |
| `./hub term NAME` | project の tmux session に入る |
| `./hub auto [status\|start\|pause\|resume\|stop\|tick\|enable\|...]` | 定期実行の agent（下の「定期実行の agent」）。`./hub auto help` で command 一覧 |

## 外す・戻す（`./hub defer` / `archive` / `restore` / `shelf`）

試しに作った project や一旦終えた論文を、消さずに一覧から外します。`defer` は「後回し」（また戻る）、`archive` は「完了・停止」です。TARGET は `NAME`・`projects/NAME`・`papers/NAME` で、project と論文に同じ名前があるときは前に付けて区別します。

- 外したものは `./hub list`・`context`・`sync`・`doctor`・`update`・`status`・`board`・`sessions`・`logs` に出ず、skill の和集合にも数えません。hub auto もその project では tick も投入もしません（`auto/config.toml` は残すので、戻せば再開します）。
- `projects/NAME`・`papers/NAME` の link と実体はそのままなので、LOG やスライドに書いた path は使えます。Claude Code の作業 directory にも残すため、hub から読み書きするときに確認は出ません。
- hub の `AGENTS.md` の表の行は `.hub/shelf.toml` に移り、`restore` で表の末尾に戻ります。`./hub status` と `./hub list` の最後に `shelf: deferred 1 (trial_x until 12-01), archived 2 — ./hub shelf` のような 1 行が出ます。`--until` の日を過ぎた後回しには `due` が付きます。
- 待ち・実行中の job がある project は、status から消えるので `--force` なしでは外せません。外した project を `./hub detach` すると shelf の記録も消えます。

## `./hub status` の 2 つの表示

- **前回の確認以降に状態が変わった run**: 状態は実行のたびに `.hub/status_seen.json` に残ります。`--peek` は残さないため、エージェントが作業の途中で状態を見ても、人への報告の差分が消えません。
- **完了後に記録が無い run**: 完了した日以降の `LOG.md` の項目、`notes/*.md`、解析 topic の `.md`・`.csv`・`.json`、`runo note` のどこにも run id が出てこない run を数えます。見出しに「進捗確認」を含む LOG の項目は記録に数えません。run id がファイルに載っているかだけを見る粗い目安で、結論が書かれたことまでは確かめません。

## `board.html`

各 project の `analysis/README.md` にある `<!-- board -->` ブロックの表から、各 project の「分かっていること」（結論、図、図の読み方）と「まだ分かっていないこと」をまとめた単体 HTML を生成します（`./hub sync` のたびにも再生成されます）。

- 表の書式は [lib/board.py](../lib/board.py) の冒頭を参照してください。
- 表示名は `<!-- board -->` の直後、最初の `###` より前に `- 表示名: 地形上の導体` のように置けます（省略時は directory 名）。
- 各テーマには `- 論文: papers/<name>` で関連論文を紐付けられます。
- 問い → 結論 → 図の読み方 → 図 → 解釈・限界 → 出典の順に表示し、図はクリックで原寸表示できます。目次・図の拡大・配色は単体 HTML 内で完結し、外部のフォントやライブラリには依存しません。

## 進捗スライド（`./hub slides`）

結果 1 つを card（`card.toml` と図または CSV 表）にして project ごとの節に足し、Typst で PDF と 1 枚ごとの PNG にします。発表後は archive し、次の deck は前回からの差分だけで作ります。画像生成 AI による図には `ai_image` で印を付けられます。

- 書式: [lib/slides.py](../lib/slides.py) の冒頭
- 作り方: [skills/hub-slides/SKILL.md](../skills/hub-slides/SKILL.md)

## `./hub relink` と旧形式の互換

- `relink` は member の skill を集め、同名なら version が新しいものを選びます。hub 自身の skill（`skills/` の `hub-slides`）も同じ場所に張ります。
- member と `papers/*` の実体 directory、hub のコードの場所は `.claude/settings.json` の `permissions.additionalDirectories` に加えます。他の設定と手で足した directory は残し、外れた member の分だけ消します。管理する一覧は `.hub/permission_dirs.json` に保存します。
- 旧形式の hub 直下にある project も検出します。同名のリンクが両方にある場合は `projects/` 側を使います。

## ジョブログの整理（`./hub logs`）

`./hub logs` は解析・図・論文の build や hub のレビューを走査し、番号付きの `.out`・`.err`・`.log` と、それ以外の `.log` を数えます。通常は番号付きログが作業ファイルに混在する場所を表示し、`--all-dirs` ですでに整理済みの場所と番号なしログの場所も表示します。run の中は解析ディレクトリと `work/` 直下を数え、シミュレーション出力の下位ディレクトリには入りません。

```bash
./hub logs
./hub logs projects/dust_charging/analysis/zenith-angle-dependence-20261104
./hub logs projects/dust_charging/analysis/zenith-angle-dependence-20261104 --apply
```

指定したディレクトリの直下で、Git 管理外かつ `sacct` で終了を確認できたファイルを、同じ名前で `logs/` へ移します（失敗したジョブも対象）。同名の別ファイル、runops の実行ログ、既存の `logs/` は保持します。外部の文書から直リンクしているファイルは移動するとパスが変わります。

新しいジョブでは投入前に出力先を作り、解析ならその解析の `logs/`、PDF なら論文の `build/logs/` に標準出力・エラーを指定します。hub を VS Code で開くと、ワークスペースの `files.exclude` が解析・図・run の作業場所などにあるジョブログをエクスプローラーから隠します（表示は `.vscode/settings.json` で切り替え可能。初期化時は既存の VS Code 設定を優先します）。

## 定期実行の agent（`./hub auto`）

完了した run の解析と、明らかに必要な追加計算（継続・小さい派生）の投入を、決まった間隔で agent に任せます。

1 回分（tick）の流れ:

1. **前処理（script）**: 有効な project で `runo sync` し、`auto/ledger.jsonl` と manifest を突き合わせて、途中で
   止まった投入を片付け、同じ入力・同じ親の継続の重複を見つけたら auto の方を取り消します。続いて、完了・失敗から
   `quiet_hours` たっても LOG・note・解析の file に run id が出てこない run を集めます（`./hub status` と同じ判定）。
   無ければここで終わり、agent は起動しません。
2. **判断（agent）**: project ごとに run を予約（`auto/claims/`、期限付き）し、`codex exec` を project で起動します。
   sandbox は workspace-write で、書けるのは project と `auto/` だけです。指示は `templates/auto_tick.md`
   （`auto/ticks/ID/PROJECT.prompt.md` に実際の文面が残ります）。
3. **投入（gate）**: agent は `hub auto submit RUN --kind continue|derive` で投入し、script が予算、継続の条件、重複、
   hold、show_my_time の残りを調べてから `runo submit` します。通らないものは `hub auto propose` で提案になり、
   人が `./hub auto approve N` で投入します。tick の間は `auto/bin/sbatch`・`tssrun` が PATH の先頭にあり、
   解析 job の枠（`[analysis]`）を超える直接の投入を拒否します。
4. **後処理（script）**: 予約を外し、agent の最後のメッセージと台帳の出来事を `auto/reports/YYYY-MM.md` に追記します。

| file（`auto/` の下） | 中身 |
|---|---|
| `config.toml` | 間隔、予算、解析 job の枠、継続とみなす key、対象の project（`enable`・`disable` が書き換える） |
| `control.json` | 人の指示（run・paused・stopped、`now`、`kick`）。ループは `POLL`（10 秒）ごとに読む |
| `heartbeat.json` | ループの host・pid・状態。`status` はこれと `loop.lock` の flock から生死を判断する |
| `ledger.jsonl` | 投入（planned → submit / submit-failed）、解析 job、提案、defer、release、取り消し |
| `claims/`、`holds.json`、`proposals/` | 予約（期限付き）、触らせない対象、提案 |
| `reports/`、`ticks/`、`loop.log` | tick ごとの報告、tick の作業 file（`keep_tick_days` 日で消える）、ループのログ |

止める・再開する command は `auto/control.json` を書くだけなので、どの ssh session・login node からでも効きます。
`/LARGE0`・`/LARGE1` の flock は login node をまたぐため、別の node から `start` してもループは 2 つになりません。
ループの process が別の node で固まったとき（`status` が HUNG と出す）だけは、その node で `./hub auto stop --force` が要ります。

## hub 本体の更新

`./hub update` は各 project の runops の更新です。hub 本体の更新は次を使います。

```bash
uvx --refresh --from git+https://github.com/Nkzono99/runops-hub runops-hub init /path/to/hub
```

この再初期化は `uvx` 方式で作った hub が対象です。clone 方式は、その repository で `git pull` します。どちらの方式でも、既存の `AGENTS.md`・`CLAUDE.md`・member 用の規則・追加済み project・論文・記録は上書きしません。新しいテンプレートの規則が必要なときは、エージェントが利用者の規則と照合して反映します。
