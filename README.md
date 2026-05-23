# CC ボートレース予想 AI - セットアップ手順

## フォルダ構成
```
boatrace/
├── server.py   ← Pythonサーバー
├── index.html  ← ブラウザUI
└── README.md   ← この手順書
```

---

## ステップ1：必要なライブラリをインストール

ターミナルを開いて以下を実行：

```bash
pip3 install beautifulsoup4
```

（標準ライブラリのみで動作しますが、スクレイピング精度向上のため推奨）

---

## ステップ2：Anthropic APIキーを取得

1. https://console.anthropic.com にアクセス
2. アカウント作成（Googleアカウント可）
3. 左メニュー「API Keys」→「Create Key」
4. 表示された `sk-ant-...` のキーをコピーして保存

---

## ステップ3：サーバーを起動

### 方法A：APIキーを環境変数で設定する（推奨）

```bash
# boatraceフォルダに移動
cd ~/Downloads/boatrace

# APIキーを設定してサーバー起動（1行で）
ANTHROPIC_API_KEY=sk-ant-xxxxxxxx python3 server.py
```

### 方法B：ブラウザUIから毎回入力する

```bash
cd ~/Downloads/boatrace
python3 server.py
```

起動後、ブラウザのAPIキー欄に入力・保存できます。

---

## ステップ4：ブラウザで開く

サーバー起動後、ブラウザで以下を開く：

```
http://localhost:8765
```

---

## 使い方

### URLから自動取得（スクレイピング）
1. [ボートレース公式](https://www.boatrace.jp) でレースを開く
2. URLをコピーしてアプリに貼り付け
3. 「取得」ボタンで出走表を自動入力

### テキストを手動入力
1. 出走表テキストをコピペ
2. 会場・水面状況を入力
3. 「AI予想を生成する」で完了

---

## サーバーの停止

ターミナルで `Ctrl + C` を押す

---

## トラブルシューティング

| 症状 | 対処法 |
|------|--------|
| `python3: command not found` | `python3 --version` で確認 |
| APIエラー (401) | APIキーが正しいか確認 |
| データ取得エラー | 公式サイトのURL形式を確認。手動入力で代用可 |
| ポート使用中エラー | server.py の PORT = 8765 を別の数字に変更 |

---

## 毎回の起動を楽にする

`start.sh` というファイルを作ると、ダブルクリックで起動できます：

```bash
#!/bin/bash
cd "$(dirname "$0")"
ANTHROPIC_API_KEY=sk-ant-xxxxxxxx python3 server.py
```

作成後: `chmod +x start.sh` で実行権限を付与
