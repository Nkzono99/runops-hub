# 開発・テスト

## clone 方式での初期化

コードの編集をすぐ hub へ反映したい場合は、repository への symlink 方式で初期化できます。

```bash
git clone https://github.com/Nkzono99/runops-hub.git
cd runops-hub
./hub init ~/work/dev-hub
```

## 構成

- `src/runops_hub/` が `runops-hub init` の入口で、`hub`・`lib/`・`templates/`・`skills/` を配布します。
- `lib/*.py` は `RUNOPS_HUB_DIR`（`./hub` が設定する）を hub directory として扱います。
- Python は `uvx` 仮想環境への symlink を解決して `.hub/python` に記録し、通常の Python 3.11 以上も利用できます。

## テストと配布物チェック

```bash
python3 -m unittest discover -s tests -t tests
uv build
python3 tests/check_distribution.py dist
```

- 単体テストは標準ライブラリだけで動きます（Python 3.11 以上）。HPC では環境の規則に従って計算ノードで実行してください。
- `tests/check_distribution.py` は wheel と sdist をそれぞれ `uvx` で実行し、キャッシュ削除後の `./hub`、project の attach・detach、編集した規則を保持する再初期化を確かめます（`uv` とネットワーク接続を使います）。
