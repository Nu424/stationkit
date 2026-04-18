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
| `stationkit.adapters` | `create_http_app`（FastAPI）、`create_cli_app`（service + CLI client）、`create_local_cli_app`（同一プロセス向け）、`create_gui_app`（Gradio） |
| `stationkit.testing` | `MockStationController`（実機なしでの検証用） |

## 開発者がやること（最小）

1. **`StationControllerBase` を継承**する。
2. **`_do_connect` / `_do_disconnect` / `_do_change` / `_do_execute` / `_do_status`** を **すべて async** で実装する。
3. **`_do_change` の第2引数 `target` に具体型のヒントを付ける**（例: `int`）。HTTP/CLI の「切替引数」の型はここから自動解決されます。`Any` のままだと警告のうえ `str` 扱いになります。
4. **`_do_execute` は無引数でも実装できる**。実行ごとの設定が必要なら、`_do_execute(self, params: ExecuteParams)` または `_do_execute(self, params: ExecuteParams | None = None)` のように **Pydantic モデル 1 個**を追加できる。
5. 必要なら **`get_custom_actions()`** で `CustomAction` のリストを返し、固有操作を HTTP/CLI に載せる。
6. 安全な中断に対応したい装置だけ、**同期メソッド `cancel_execution()`** を任意実装する。中断後は `_do_execute()` が `ExecutionCancelledError` を送出して unwind するのを標準ルールにする。

同期メソッド（`connect` など）と `*_async` は基底クラスが提供するため、装置側で二重実装する必要はありません。
`_do_execute()` の中身は同期的に長時間かかる処理でも構いません。HTTP / GUI / service-backed CLI のように UX 上ブロッキングを避けたい面では、後述の `ExecutionManager` が `controller.execute()` 全体を別スレッドで管理します。

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

## execute にパラメータを持たせる

`execute()` 自体に実行時パラメータを持たせたい場合は、`_do_execute` に **Pydantic モデル 1 個**を追加します。これを定義すると、HTTP / CLI / GUI / 直接 API が同じスキーマを共有します。

```python
from typing import Any

from pydantic import BaseModel

from stationkit import StationControllerBase


class ExecuteParams(BaseModel):
    duration_s: int
    rpm: int | None = None


class MyDeviceController(StationControllerBase):
    # ... _do_connect / _do_disconnect / _do_change / _do_status は省略 ...

    async def _do_execute(self, params: ExecuteParams) -> dict[str, Any]:
        return {
            "duration_s": params.duration_s,
            "rpm": params.rpm,
        }
```

- **直接 API**: `controller.execute(ExecuteParams(duration_s=5, rpm=120))` または `controller.execute({"duration_s": 5, "rpm": 120})`
- **HTTP**: `POST /execute` に `{"duration_s": 5, "rpm": 120}` を送る
- **CLI**: `execute '{"duration_s": 5, "rpm": 120}'`
- **GUI**: モデルの各フィールドが単純型ならフォーム入力、複雑型なら JSON 入力になる

`_do_execute(self, params: ExecuteParams | None = None)` のように `None` を許可した場合は、空入力の `execute()` もそのまま使えます。

## スクリプトから直接使う（HTTP を立てない）

ライブラリとして取り込み、**同じコントローラインスタンス**に対して同期 API または `async` API を呼び出します。バッチ処理や、既存の測定スクリプトから装置を叩く用途向けです。

### 同期で一通り使う例

通常のスクリプト（トップレベルが同期）では、次のように書けます。`connect` → `change`（必要なら繰り返し）→ `execute` → `disconnect` の流れが典型です。`_do_execute` に入力モデルを持たせた実装では、`execute(params)` の形で実行時パラメータも渡せます。

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

## 長時間 execute を別層で扱う

`execute()` / `execute_async()` は引き続き「完了まで待って結果を返す」API です。これを変えたくない一方で、HTTP や GUI では長時間の実行を別スレッドで扱いたいので、`stationkit` は **`ExecutionManager`** を別層で用意しています。

```python
from stationkit import ExecutionManager, ExecutionState, MyDeviceController

ctrl = MyDeviceController()
ctrl.connect("COM3")
ctrl.change(1)

manager = ExecutionManager(ctrl)
handle = manager.start()

while True:
    status = manager.get_status(handle.execution_id)
    print(status.state)
    if status.state not in {ExecutionState.RUNNING, ExecutionState.CANCELLING}:
        break

if status.state == ExecutionState.SUCCEEDED:
    print(status.result)
```

- `ExecutionManager` は **`controller.execute()` を丸ごと** single-worker thread で実行します。`_do_execute()` を直接呼びません。
- `get_status()` は in-memory のジョブ状態だけを返し、装置 I/O を行いません。
- `cancel()` は worker thread を kill せず、`cancel_execution()` hook を持つ controller に対して **協調的 cancel** を要求します。
- `cancel_execution()` を実装しない controller では、`ExecutionManager.cancel()` は未対応エラーを返します。

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
| POST | `/execute` | メイン操作（サンプリングや計測開始など）を実行し、結果を返す（**接続済みであること**） | デフォルトは なし（空ボディ）。`_do_execute(self, params: ExecuteParams)` を定義した実装では、その **Pydantic モデルどおりの JSON**。`ExecuteParams | None = None` の場合は空ボディも可。 | **装置の戻り値依存**。`dict` ならそのキー構造の JSON。Pydantic モデルを返す実装はプレーンなオブジェクトに変換される。例: `{"address": "COM3", "target": 2}` やモックなら `{"mock": true, "target": 2}` |
| POST | `/execute/start` | `ExecutionManager` 経由で execute を開始し、すぐ制御を返す | `/execute` と同じ | `{"execution_id": "<id>"}` |
| GET | `/execute/status` | 直近または指定 execute ジョブの状態を取得する | クエリ `execution_id=<id>` は任意 | `{"execution_id": "...", "state": "RUNNING" | "SUCCEEDED" | "FAILED" | "CANCELLED", "result": ..., "error_message": null, "cancel_requested": false, ...}` |
| POST | `/execute/cancel` | 進行中 execute の cancel を要求する | `{"execution_id": "<id>"}` は任意 | `{"ok": true}` |
| POST | `/actions/{name}` | `get_custom_actions()` で登録した**固有操作**を実行する | **`{name}` に対応する `input_schema` どおりの JSON** 例: `calibrate` が `level` と `force` を持つなら `{"level": 7, "force": true}` | **`output_schema` がある場合**はそのフィールド構造の JSON。ない場合は操作の戻り値に応じた JSON（プレーン化ルールは `/execute` と同様） |

**エラー時**（状態不整合や `StationError` 系）は HTTP ステータスが 4xx/5xx になり、本文は次の形です。

```json
{"detail": "エラーメッセージ（例外の文字列）"}
```

`stationkit.core.exceptions` の **`StationError` 系**はステータスにマッピングされます（例: `StateError` → 409、`TimeoutError` → 504、`ConnectionError` / `CommandError` → 502、その他の `StationError` → 500）。

## CLI として動かす

`create_cli_app()` は、**常駐 service が状態を保持し、CLI は毎回その service を呼び出す** 形です。スクリプトや CI から都度コマンドを呼びつつ、`connect` 後の状態を共有したい場合はこちらを使います。

```python
from stationkit import MyDeviceController, create_cli_app

app = create_cli_app(MyDeviceController())

if __name__ == "__main__":
    app()
```

起動例（`serve` は uvicorn が必要です）:

```bash
uv add uvicorn

# terminal 1: service を起動
uv run python mycli.py serve --host 127.0.0.1 --port 8000

# terminal 2: CLI client から service を叩く
uv run python mycli.py --server http://127.0.0.1:8000 connect COM3
uv run python mycli.py --server http://127.0.0.1:8000 change 2
uv run python mycli.py --server http://127.0.0.1:8000 execute-start
uv run python mycli.py --server http://127.0.0.1:8000 execute-status
uv run python mycli.py --server http://127.0.0.1:8000 execute-cancel
uv run python mycli.py --server http://127.0.0.1:8000 execute
uv run python mycli.py --server http://127.0.0.1:8000 execute '{"duration_s": 5, "rpm": 120}'
uv run python mycli.py --server http://127.0.0.1:8000 status
uv run python mycli.py --server http://127.0.0.1:8000 disconnect
```

`--server` の代わりに環境変数 `STATIONKIT_SERVER_URL` でも service URL を渡せます。

`execute-start` / `execute-status` / `execute-cancel` は、それぞれ execution id の発行、実行状態の確認、cancel 要求を明示的に行うためのコマンドです。`execute` は引き続き **完了まで待機する convenience command** として残してあり、内部実装では `POST /execute/start` と `GET /execute/status` の polling を使います。`execute` が入力モデルを持つコントローラでは、CLI でも **JSON 文字列 1 引数**で同じスキーマを渡します。`_do_execute(self, params: ExecuteParams | None = None)` のようなオプショナル入力なら、引数なし `execute` も引き続き使えます。

### 同一プロセスのローカル CLI を使う場合

デバッグやテストのように、**同じ Python プロセス内で同じコントローラインスタンスを使い回す**なら `create_local_cli_app()` も使えます。こちらは `CliRunner` や独自の対話ループに埋め込む用途向けで、通常の「コマンドを 1 回ずつ起動する CLI」としては状態を共有しません。

> 補足: 単発 CLI が向くのは、状態を service 側に持つ場合や、1 コマンドが `connect -> change -> execute -> disconnect` まで自己完結する場合です。逆に、接続状態をそのまま次の OS プロセスへ引き継ぎたい用途には、単発 CLI 単体は向きません。

```python
from stationkit import MyDeviceController, create_local_cli_app

app = create_local_cli_app(MyDeviceController())
```

このリポジトリ付属のデモは `main.py` にあり、`MockStationController` をバインドした service-backed CLI になっています。

```bash
uv run python main.py --help
```

`pyproject.toml` の `[project.scripts]` で `stationkit` コマンドも定義しているため、パッケージをインストールした環境では `stationkit` で同様に起動できます。

## Gradio GUI として動かす

Gradio ベースの GUI は `create_gui_app()` で生成できます。返り値は **`gr.Blocks`** なので、呼び出し側で `launch()` してください。

```python
from stationkit import MyDeviceController, create_gui_app

controller = MyDeviceController()
app = create_gui_app(controller)
app.launch()
```

この GUI は **`create_http_app()` と同じく、1 つの controller インスタンスを共有** します。つまり、複数のブラウザクライアントが開いていても、見ている相手は同じ装置・同じ接続状態です。状態の本体は Gradio 側ではなく **controller 側** にあります。

初版の入力方針は次のとおりです。

- `change()` の `target` が `str` / `int` / `float` / `bool` の場合は、対応する GUI 入力ウィジェットが自動で使われます。
- `execute()` も同様で、`_do_execute` の入力モデルが単純なフィールド群なら個別フォーム、複雑なスキーマなら JSON テキスト入力になります。
- `Enum`、`list` / `dict`、`Union`、`BaseModel` などの複雑な型は、**JSON テキスト入力**にフォールバックします。
- `CustomAction` も同様で、入力スキーマが単純なフィールド群なら個別フォーム、複雑なスキーマなら JSON テキスト入力で実行できます。
- GUI の `Start Execute` は内部で `ExecutionManager` を使って **非ブロッキングに開始** され、handler 自体は完了まで待機しません。
- 実行状況や最終 result は `Refresh Status` で確認します。完了済み execution があれば Result パネルにも反映されます。
- `Cancel Execute` で協調的 cancel を要求できます。
- GUI の Status パネルには controller/device status に加えて、取得可能なら `execution` キーでジョブ状態も表示されます。

GUI からの操作は共有 controller に対して逐次実行され、各操作のあとに最新の `status()` 相当の情報が画面へ再反映されます。

## ロギング

`stationkit` は Python 標準の `logging` を使い、コア層と各アダプタ層で同じ `extra` キーを付与します。既定では `NullHandler` が付いているため、利用側がハンドラを設定しない限り出力は増えません。

```python
import logging


logging.basicConfig(level=logging.INFO)
logging.getLogger("stationkit").setLevel(logging.INFO)
```

主な `extra` は `stationkit_layer`, `stationkit_op`, `stationkit_controller`, `stationkit_event`, `stationkit_duration_ms`, `stationkit_success` です。`create_http_app(controller, logger=...)` を使うと、HTTP 境界ログを任意の logger に流せます。

ログには操作名や状態遷移のような**運用上必要な最小情報**のみを載せ、接続先アドレス・認証情報・装置固有の秘密値などは載せない前提で使ってください。

## テスト

```bash
uv run pytest
```

`MockStationController` を使えば、実装置なしでアダプタや状態遷移を検証しやすくなります。

## エラー設計の目安

- 装置側の例外は、可能なら **`ConnectionError` / `CommandError` / `TimeoutError`** などフレームワークの型に寄せると、HTTP/CLI で扱いやすくなります。
- **状態不整合**（未接続で `change` など）は基底クラスが **`StateError`** を送出します。
- 安全に中断できた execute は **`ExecutionCancelledError`** を使うと、`ExecutionManager` が `CANCELLED` として扱えます。

## 今後の拡張

- 詳細な設計意図は `documents/station_controller_design.md` を参照してください。
