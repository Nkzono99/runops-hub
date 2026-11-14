# runops-hub

複数の [runops](https://github.com/Nkzono99/runops) project を 1 つの directory から見るための道具。
hub directory に project を symlink で並べ、`./hub` が全 project の状態をまとめて見せる。
project はそれぞれ自己完結したままで、hub はそこから導出したビューを出すだけである。

この repository にあるのは機能（`hub` と `lib/`）だけ。project の一覧、作業の規則、記録は
hub directory 側に置く。

## 使い始める

runops（`runo`）が入っていること。`lib/` は Python 3.11 以上で動き、`runo` と同じ interpreter を使う。

```bash
git clone https://github.com/Nkzono99/runops-hub.git ~/src/runops-hub
~/src/runops-hub/hub init ~/work/hub      # hub directory を作る
cd ~/work/hub
./hub attach flat ../project_flat emses --desc "平坦面の帯電"   # 既存の project を加える
./hub new crater emses --desc "クレーター地形"                  # 新しい project を作って加える
./hub sync && ./hub status
```

`init` が hub directory に作るもの:

| file | 内容 |
|---|---|
| `hub` | この repository の `hub` への symlink。更新は `git pull` だけで届く |
| `AGENTS.md`、`CLAUDE.md` | agent 向けの入口。project の表は `new`・`attach`・`detach` が保つ。規則は自分の運用に合わせて書き足す |
| `.hub/member_section.md` | `new`・`attach` が各 project の `AGENTS.md` に足す節 |
| `.hub/status_seen.json` | `./hub status` が最後に見た run の状態（自動で作られる） |
| `board.html` | `./hub board` と `./hub sync` が作り直す、人が見る 1 枚（自動で作られる） |

## command

| command | 用途 |
|---|---|
| `./hub sync` / `list [--json]` / `context` / `doctor` / `update` | 全 project で同じ `runo` を実行する |
| `./hub status [--peek]` | 全 project の件数と active run、前回の確認以降に状態が変わった run、完了後に記録が無い run |
| `./hub board` | 人が見る `board.html` を hub directory に作る（試作）。各 project の「分かっていること」（結論、図、図の読み方）と「まだ分かっていないこと」。`./hub sync` のたびにも作り直す。元になる表の書式は `lib/board.py` の冒頭 |
| `./hub sessions` | この node で各 project に開いている tmux・claude・codex |
| `./hub relink [--check] [-v]` | hub の skill link を全 project の和集合から作り直す（同名は version が新しいもの）。あわせて、member と `papers/*` の実体 directory とこの repository を `.claude/settings.json` の `permissions.additionalDirectories` に揃える（symlink 先は Claude Code の作業 directory の外なので、そのままでは auto mode でも書き込みのたびに確認が出る）。手で足した directory と他の設定は残し、外れた member の分だけ消す（管理する一覧は `.hub/permission_dirs.json`） |
| `./hub new NAME SIM... --desc TEXT` | `../project_NAME` に `runo init` して加える |
| `./hub attach NAME PATH SIM... --desc TEXT` | 既にある runops project を加える |
| `./hub detach NAME` | hub から外す（symlink と表の行だけを消す） |
| `./hub term NAME` | project の tmux session に入る |

run を変える `runo`（`new`、`submit`、`cancel` など）は hub からは実行しない。対象の project に `cd` して行う。

### `./hub status` の 2 つの表示

- **前回の確認以降に状態が変わった run。** 状態は実行のたびに `.hub/status_seen.json` に残る。
  `--peek` は残さないので、agent が作業の途中で状態を見ても、人への報告の差分が消えない。
- **完了後に記録が無い run。** 完了した日以降の `LOG.md` の項目、`notes/*.md`、解析 topic の
  `.md`・`.csv`・`.json`、`runo note` のどこにも run id が出てこない run を数える。
  見出しに「進捗確認」を含む LOG の項目は記録に数えない。run id が file に載っているかだけを見る
  粗い目安で、結論が書かれたことまでは確かめない。

## 開発

board の表示名は、各 `analysis/README.md` の `<!-- board -->` の直後、最初の `###` より前に
`- 表示名: 地形上の導体` のように置けます。省略時は directory 名を使います。
問い → 結論 → 図の読み方 → 図 → 解釈・限界 → 出典の順に表示し、図はクリックで原寸表示できます。
目次・図の拡大・配色は単体HTMLの中で動き、外部のフォントやライブラリには依存しません。

```bash
"$(dirname "$(readlink -f "$(command -v runo)")")/python" -m unittest discover -s tests -t tests
```

テストは標準ライブラリだけで動く。`lib/*.py` は `RUNOPS_HUB_DIR`（`./hub` が設定する）を hub directory として扱う。

## License

Apache License 2.0. [LICENSE](LICENSE) を参照。
