# stationkit

複数の対象（ステーション）を切り替えながら操作する装置を、**同じパターン**で扱うための Python フレームワークです。オートサンプラー、マルチポート弁、テスト用チャネル切替器など、「接続 → 対象選択 → 実行」という流れが共通する機器を想定しています。

## 前提

- Python 3.12 以上
- 依存関係の管理に [uv](https://github.com/astral-sh/uv) を利用する想定（このリポジトリの `pyproject.toml` と整合）

セットアップ例:

```bash
uv sync
```

開発用依存（pytest 等）も入れる場合:

```bash
uv sync --group dev
```

## パッケージ構成（概要）

| 領域 | 役割 |
|------|------|
| `stationkit.core` | 状態・例外、`StationControllerBase`、`CustomAction` |
| `stationkit.adapters` | `create_http_app`（FastAPI）、`create_cli_app`（Typer） |
| `stationkit.testing` | `MockStationController`（実機なしでの検証用） |

## 開発者がやること（最小）

1. **`StationControllerBase` を継承**する。
2. **`_do_connect` / `_do_disconnect` / `_do_change` / `_do_execute` / `_do_status`** を **すべて async** で実装する。
3. **`_do_change` の第2引数 `target` に具体型のヒントを付ける**（例: `int`）。HTTP/CLI の「切替引数」の型はここから自動解決されます。`Any` のままだと警告のうえ `str` 扱いになります。
4. 必要なら **`get_custom_actions()`** で `CustomAction` のリストを返し、固有操作を HTTP/CLI に載せる。

同期メソッド（`connect` など）と `*_async` は基底クラスが提供するため、装置側で二重実装する必要はありません。

## 同期 API と非同期 API

- **スクリプトや同期コンテキスト**では `connect` / `change` / `execute` / `status` / `disconnect` を使います。
- **すでに asyncio のイベントループが動いている**コードでは、同期 API は使えません（`RuntimeError`）。その場合は **`connect_async` 等の `*_async`** を使ってください。

## コード例: 具体コントローラ

```python
from typing import Any

from stationkit import StationControllerBase


class MyDeviceController(StationControllerBase):
    """例: 実際にはシリアルやソケットで装置と通信する。"""

    def __init__(self) -> None:
        super().__init__()
        self._address: str | None = None
        self._target: int | None = None

    async def _do_connect(self, address: str) -> None:
        self._address = address
        # await open_serial(address) など

    async def _do_disconnect(self) -> None:
        self._address = None

    async def _do_change(self, target: int) -> None:
        # target の型ヒントが CLI/HTTP の引数型になる
        self._target = target

    async def _do_execute(self) -> dict[str, Any]:
        return {"address": self._address, "target": self._target}

    async def _do_status(self) -> dict[str, Any]:
        return {"target": self._target}
```

## 固有操作（CustomAction）

Pydantic モデルで入出力を宣言し、async 関数に渡します。

```python
from pydantic import BaseModel

from stationkit import CustomAction, StationControllerBase


class ZeroInput(BaseModel):
    """引数なしに相当するダミー（フィールドがなくてよい）。"""


class ZeroOutput(BaseModel):
    message: str


class MyController(StationControllerBase):
    # ... _do_* は省略 ...

    async def _do_ping(self, _params: ZeroInput) -> ZeroOutput:
        return ZeroOutput(message="pong")

    def get_custom_actions(self):
        return [
            CustomAction(
                name="ping",
                description="疎通確認",
                func=self._do_ping,
                input_schema=ZeroInput,
                output_schema=ZeroOutput,
            )
        ]
```

- **HTTP**: `POST /actions/ping`、ボディは `{}` のような JSON（スキーマに従う）。
- **CLI**: `ping '{}'` のように **1 引数で JSON 文字列**を渡す（スキーマにフィールドが無い場合は空オブジェクトでよい）。

## スクリプトから直接使う（HTTP を立てない）

ライブラリとして取り込み、**同じコントローラインスタンス**に対して同期 API または `async` API を呼び出します。バッチ処理や、既存の測定スクリプトから装置を叩く用途向けです。

### 同期で一通り使う例

通常のスクリプト（トップレベルが同期）では、次のように書けます。`connect` → `change`（必要なら繰り返し）→ `execute` → `disconnect` の流れが典型です。

```python
from stationkit import MyDeviceController

ctrl = MyDeviceController()
ctrl.connect("COM3")
try:
    ctrl.change(1)
    result = ctrl.execute()
    print(result)
    print(ctrl.status())
finally:
    ctrl.disconnect()
```

`MockStationController` に差し替えれば、実機なしで API の流れだけ試せます。

```python
from stationkit import MockStationController

c = MockStationController()
c.connect("DEMO")
c.change(2)
print(c.execute())
c.disconnect()
```

### asyncio 内から使う例

**イベントループがすでに動いている**コンテキスト（FastAPI のハンドラ内、他ライブラリの `async` コールバック内など）では、同期メソッド（`connect` など）は **`RuntimeError` になります**。その場合は `*_async` を `await` してください。

```python
import asyncio

from stationkit import MyDeviceController


async def run_sequence() -> None:
    ctrl = MyDeviceController()
    await ctrl.connect_async("COM3")
    try:
        await ctrl.change_async(1)
        result = await ctrl.execute_async()
        print(result)
        print(await ctrl.status_async())
    finally:
        await ctrl.disconnect_async()


asyncio.run(run_sequence())
```

失敗時は `stationkit` の例外（例: 未接続で `change` したときの `StateError`）がそのまま伝播します。装置側のエラーは、可能なら `ConnectionError` / `CommandError` などにマッピングしておくと、HTTP 経由のときとメッセージの扱いが揃いやすくなります。

## HTTP サーバーとして動かす

```python
from stationkit import MyDeviceController, create_http_app

controller = MyDeviceController()
app = create_http_app(controller)
```

起動例（uvicorn は別途インストールが必要です）:

```bash
uv add uvicorn
uv run uvicorn mymodule:app --reload
```

主なエンドポイント（**ボディを送る POST** は JSON。`Content-Type: application/json` を付与してください。GET はボディなしで、レスポンスは JSON です）:

| メソッド | パス | 何をするか | リクエスト（ボディ） | レスポンス（成功時の本文の例） |
|----------|------|------------|----------------------|--------------------------------|
| POST | `/connect` | 装置に接続し、内部状態を接続済みにする | `{"address": "<接続先>"}` 例: `{"address": "COM3"}` や URL 文字列 | `{"ok": true}` |
| POST | `/disconnect` | 装置から切断し、未接続状態に戻す | なし（空ボディ） | `{"ok": true}` |
| GET | `/status` | コントローラの状態名と、装置固有のステータスをまとめて取得する | なし | `{"controller_state": "CONNECTED", ...}` …`...` は `_do_status()` が返すキー（例: `current_target`） |
| POST | `/change` | 対象ステーションやチャネルなどを切り替える（**接続済みであること**） | `{"target": <値>}` …`<値>` の型は各コントローラの `_do_change(self, target: ...)` の型ヒントに従う（例: 整数なら `{"target": 2}`） | `{"ok": true}` |
| POST | `/execute` | メイン操作（サンプリングや計測開始など）を実行し、結果を返す（**接続済みであること**） | なし（空ボディ） | **装置の戻り値依存**。`dict` ならそのキー構造の JSON。Pydantic モデルを返す実装はプレーンなオブジェクトに変換される。例: `{"address": "COM3", "target": 2}` やモックなら `{"mock": true, "target": 2}` |
| POST | `/actions/{name}` | `get_custom_actions()` で登録した**固有操作**を実行する | **`{name}` に対応する `input_schema` どおりの JSON** 例: `calibrate` が `level` と `force` を持つなら `{"level": 7, "force": true}` | **`output_schema` がある場合**はそのフィールド構造の JSON。ない場合は操作の戻り値に応じた JSON（プレーン化ルールは `/execute` と同様） |

**エラー時**（状態不整合や `StationError` 系）は HTTP ステータスが 4xx/5xx になり、本文は次の形です。

```json
{"detail": "エラーメッセージ（例外の文字列）"}
```

`stationkit.core.exceptions` の **`StationError` 系**はステータスにマッピングされます（例: `StateError` → 409、`TimeoutError` → 504、`ConnectionError` / `CommandError` → 502、その他の `StationError` → 500）。

## CLI として動かす

```python
from stationkit import MyDeviceController, create_cli_app

app = create_cli_app(MyDeviceController())

if __name__ == "__main__":
    app()
```

実行例:

```bash
uv run python mycli.py connect COM3
uv run python mycli.py change 2
uv run python mycli.py execute
uv run python mycli.py status
uv run python mycli.py disconnect
```

このリポジトリ付属のデモは `main.py` にあり、`MockStationController` をバインドしています。

```bash
uv run python main.py --help
```

`pyproject.toml` の `[project.scripts]` で `stationkit` コマンドも定義しているため、パッケージをインストールした環境では `stationkit` で同様に起動できます。

## テスト

```bash
uv run pytest
```

`MockStationController` を使えば、実装置なしでアダプタや状態遷移を検証しやすくなります。

## エラー設計の目安

- 装置側の例外は、可能なら **`ConnectionError` / `CommandError` / `TimeoutError`** などフレームワークの型に寄せると、HTTP/CLI で扱いやすくなります。
- **状態不整合**（未接続で `change` など）は基底クラスが **`StateError`** を送出します。

## 今後の拡張

- `create_gui_app` は現状プレースホルダです（呼び出すと `NotImplementedError`）。
- 詳細な設計意図は `documents/station_controller_design.md` を参照してください。
