# runops-hub

複数の [runops](https://github.com/Nkzono99/runops) project を、1 つの directory で agent と一緒に扱うための入口。
人が hub を初期化し、その後の project の追加・作成・進捗確認・解析は agent に自然文で頼む。
各 project は symlink で並び、それぞれ単独でも使える。

## はじめる

Linux 上で Git と [uv](https://docs.astral.sh/uv/getting-started/installation/) が使えること。
次を実行して、作成した directory で Codex または Claude Code を起動する。

```bash
uvx --from git+https://github.com/Nkzono99/runops-hub runops-hub init ~/work/hub
cd ~/work/hub
codex  # Claude Code なら claude
```

`init` の path を省略すると、現在の directory に作る。
repository の clone や runops の事前インストールは不要。
`AGENTS.md` に agent 用の手順が入り、必要な runops の準備も agent が行う。

あとは、例えば次のように依頼する。人が `attach` の書式や simulator の名前を調べる必要はない。

> `/data/project_crater` の計算を、この hub から扱えるようにして。

> いま動いている計算の進捗を、まとめて教えて。

> 平坦面とクレーターの結果を比べて、図とレポートにまとめて。

> 新しく月面のダスト帯電を調べたい。既存 project で扱えるか見て、必要なら project を作って。

agent は依頼先の設定と規則を読み、hub への追加と skill の設定を行う。
対象や目的が不足するときはその点を確認し、計算の投入は各 project の規則に従う。

## 作成される directory

```text
hub/
├── AGENTS.md           # agent の入口。project の一覧と作業の規則
├── CLAUDE.md           # Claude Code からも同じ規則を読む
├── hub                 # 以後、agent が使う ./hub
└── .hub/
    ├── runtime/        # hub のコードとテンプレート
    ├── python          # hub 用 Python の実体 path
    └── member_section.md
```

hub のコードはこの directory に保存する。`uvx` のキャッシュを削除しても `./hub` は使える。
Python は uvx の仮想環境への symlink を解決して記録し、通常の Python 3.11 以上も利用できる。
project の一覧・規則・記録はここに置き、run や解析の結果は各 project に置く。
`board.html` は、agent が確認した結論・図・残っている問いをまとめるページで、`sync` の際に生成される。

hub 本体の更新も「hub を最新版にして」と agent に頼めばよい。
初期化を再実行しても、編集した規則や追加済み project、論文、記録は保持する。
既存の clone 方式で作った hub も、そのまま使える。

## agent 向けの操作

普段の依頼では、この表のコマンドを人が実行する必要はない。
生成した hub の `AGENTS.md` が、runops の準備から project 追加後の確認までを案内する。

| command | 用途 |
|---|---|
| `./hub attach NAME PATH SIM... --desc TEXT` | 既存の runops project を加える |
| `./hub new NAME SIM... --desc TEXT` | `../project_NAME` に新しい project を作る |
| `./hub sync` / `list [--json]` / `context` / `doctor` / `update` | 全 project で同じ `runo` を実行する |
| `./hub status [--peek]` | 全 project の件数と active run、前回の確認以降に状態が変わった run、完了後に記録が無い run |
| `./hub board` | 人が見る `board.html` を hub directory に作る（試作）。各 project の「分かっていること」（結論、図、図の読み方）と「まだ分かっていないこと」。`./hub sync` のたびにも作り直す。元になる表の書式は `lib/board.py` の冒頭 |
| `./hub sessions` | この node で各 project に開いている tmux・claude・codex |
| `./hub relink [--check] [-v]` | skill の link と、project・論文の実体 directory に対する Claude Code の設定を揃える |
| `./hub detach NAME` | hub から外す（symlink と表の行だけを消す） |
| `./hub term NAME` | project の tmux session に入る |

run を変える `runo`（`new`、`submit`、`cancel` など）は対象の project に `cd` して実行する。
`./hub update` は各 project の runops の更新であり、hub 本体の更新は次を使う。

```bash
uvx --refresh --from git+https://github.com/Nkzono99/runops-hub runops-hub init /path/to/hub
```

この再初期化は uvx 方式で作った hub が対象。clone 方式は、その repository で `git pull` する。
どちらの方式でも、既存の `AGENTS.md`・`CLAUDE.md`・member 用の規則は上書きしない。
新しいテンプレートの規則が必要なときは、agent が利用者の規則と照合して反映する。

`relink` は member の skill を集め、同名なら version が新しいものを選ぶ。
member と `papers/*` の実体 directory、hub のコードの場所は `.claude/settings.json` の
`permissions.additionalDirectories` に加える。他の設定と手で足した directory は残し、
外れた member の分だけ消す。管理する一覧は `.hub/permission_dirs.json` に保存する。

### `./hub status` の 2 つの表示

- **前回の確認以降に状態が変わった run。** 状態は実行のたびに `.hub/status_seen.json` に残る。
  `--peek` は残さないので、agent が作業の途中で状態を見ても、人への報告の差分が消えない。
- **完了後に記録が無い run。** 完了した日以降の `LOG.md` の項目、`notes/*.md`、解析 topic の
  `.md`・`.csv`・`.json`、`runo note` のどこにも run id が出てこない run を数える。
  見出しに「進捗確認」を含む LOG の項目は記録に数えない。run id が file に載っているかだけを見る
  粗い目安で、結論が書かれたことまでは確かめない。

## 開発

従来の repository への symlink 方式も使える。こちらはコードの編集をすぐ hub へ反映するための手順。

```bash
git clone https://github.com/Nkzono99/runops-hub.git
cd runops-hub
./hub init ~/work/dev-hub
```

board の表示名は、各 `analysis/README.md` の `<!-- board -->` の直後、最初の `###` より前に
`- 表示名: 地形上の導体` のように置けます。省略時は directory 名を使います。
問い → 結論 → 図の読み方 → 図 → 解釈・限界 → 出典の順に表示し、図はクリックで原寸表示できます。
目次・図の拡大・配色は単体HTMLの中で動き、外部のフォントやライブラリには依存しません。

```bash
python3 -m unittest discover -s tests -t tests
uv build
python3 tests/check_distribution.py dist
```

単体テストは標準ライブラリだけで動く（Python 3.11 以上）。HPC では環境の規則に従って計算ノードで実行する。
最後の検査は wheel と sdist をそれぞれ uvx で実行し、キャッシュ削除後の `./hub`、project の
attach・detach、編集した規則を保持する再初期化を確かめる。uv とネットワーク接続を使う。
`src/runops_hub/` は初期化の入口で、既存の `hub`・`lib/`・`templates/` を配布する。
`lib/*.py` は `RUNOPS_HUB_DIR`（`./hub` が設定する）を hub directory として扱う。

## License

Apache License 2.0. [LICENSE](LICENSE) を参照。
