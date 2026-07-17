# Sequence Web App

`apps/sequence_app/` は、`SequenceRunner` を FastAPI + React で包んだ monorepo 内アプリ領域です。
library 側は `stationkit.adapters.sequence_http` まで、frontend 配信都合は `apps/sequence_app/server` と `apps/sequence_app/web` に切り分けています。

## 構成

- `server/app.py`: `create_sequence_app_server(controller, ...)`
- `web/`: Vite + React + Tailwind + Zustand の single-page UI

サンプル用の uvicorn 起動は、リポジトリルートの [`main.py`](../../main.py)（`MockStationController` を包む最小 launcher）を使います。

## 開発手順

### 1. Python 依存を入れる

```bash
uv sync
```

### 2. frontend 依存を入れる

```bash
cd apps/sequence_app/web
npm install
```

### 3. backend を起動する

リポジトリルートで、PowerShell:

```powershell
$env:STATIONKIT_SEQUENCE_DEV_ORIGIN = "http://127.0.0.1:5173"
uv run python main.py
```

ルートの `main.py` は `MockStationController` を `create_sequence_app_server()` で包み、`uvicorn` で起動します。
自分の controller を使いたい場合は、このパターンを真似した launcher script を別途作成してください。

### 4. frontend dev server を起動する

別ターミナルで:

```powershell
cd apps/sequence_app/web
$env:VITE_API_BASE_URL = "http://127.0.0.1:8000"
npm run dev
```

ブラウザで `http://127.0.0.1:5173` を開くと、backend の `/api/...` を CORS 経由で利用できます。

## production 相当の起動

frontend を build すると、`server/static` に配布用ファイルが生成されます。
`create_sequence_app_server()` はこの同梱 static を自動検出して同一 origin 配信します。

```powershell
cd apps/sequence_app/web
npm run build
cd ../../..
uv run python main.py
```

## API-only 利用

static 配信が不要なら、library 側の `create_sequence_http_app(controller)` を直接使えます。
この場合は React build や CORS 設定を含まず、純粋な `/api/...` だけを公開します。

## 手動確認チェックリスト

- connect → add step → validate → run → stop
- connect 後に「待機状態へ（Go Idle）」ボタンで idle へ移せること（status の `routing` が更新される）
- controller の `get_metadata().sequence_modes` に応じて実行モードの選択肢が絞られること
- 単一モードの controller では実行モードを変更できないこと
- time-driven step の start/end 表示と countdown 表示
- import/export roundtrip と、未対応モードを含む JSON の import 拒否
- single-step check の load / start
- 実行中の disable 制御
- execute 失敗で controller が `ERROR` になったとき、切断ボタンが有効で接続/実行ボタンが無効になること
- `ERROR` 表示の案内どおり **切断 → 再接続** すると `CONNECTED` に戻り、run / single-step を再開できること
- `ERROR` 中に Connect や Run を押しても再開できず、切断が必要なこと
