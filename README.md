# stationkit

**stationkit** は、「接続 → 対象選択 → 実行」という操作パターンを共有する装置を、
統一された Python インタフェースで扱うための小さなフレームワークです。
オートサンプラー、マルチポート弁、テスト用チャネル切替器など、
複数の *ステーション* を切り替えながら操作する機器全般を想定しています。

## 特徴

- **1 つのコントローラ実装から、5 つの使い方へ展開できる**
  ライブラリ呼び出し / HTTP サーバー / CLI / Gradio GUI / Sequence Web App を、同じ `StationControllerBase` 実装から組み立てられます。
- **sync / async の二重実装が不要**
  装置固有のロジックは `_do_*` を **async** で 1 度書けば、同期 API（`connect` など）と非同期 API（`connect_async` など）の両方が自動で提供されます。
- **型ヒントから入出力スキーマを自動解決**
  `_do_change` の `target` 引数や `_do_execute` の Pydantic パラメータモデルから、HTTP / CLI / GUI の入力型が自動で決まります。
- **長時間実行は `ExecutionManager` が別スレッドで管理**
  `execute()` の API は「完了まで待つ」まま維持しつつ、HTTP / GUI からは非ブロッキングに開始・状況取得・協調的 cancel ができます。
- **固有操作は `CustomAction` で拡張**
  装置独自のコマンドを、Pydantic スキーマ付きで HTTP / CLI / GUI に一括公開できます。

## 目次

- [動作環境](#動作環境)
- [インストール](#インストール)
- [パッケージ構成](#パッケージ構成)
- [最小実装](#最小実装)
- [同期 API と非同期 API](#同期-api-と非同期-api)
- [スクリプトから直接使う](#スクリプトから直接使う)
- [execute にパラメータを持たせる](#execute-にパラメータを持たせる)
- [固有操作（CustomAction）](#固有操作customaction)
- [長時間 execute を別層で扱う（ExecutionManager）](#長時間-execute-を別層で扱うexecutionmanager)
- [HTTP サーバーとして動かす](#http-サーバーとして動かす)
- [CLI として動かす](#cli-として動かす)
- [Sequence Web App として動かす](#sequence-web-app-として動かす)
- [Gradio GUI として動かす](#gradio-gui-として動かす)
- [ロギング](#ロギング)
- [テスト](#テスト)
- [エラー設計の目安](#エラー設計の目安)
- [さらに読む](#さらに読む)

## 動作環境

- Python 3.12 以上
- 依存関係の管理は [uv](https://github.com/astral-sh/uv) を前提にしています（`pyproject.toml` と整合）

## インストール

```bash
uv sync
```

pytest などの開発用依存も含める場合:

```bash
uv sync --group dev
```

## パッケージ構成

| モジュール | 役割 |
|------|------|
| `stationkit.core` | `StationControllerBase`、状態 (`ControllerState`)、例外階層、`CustomAction` |
| `stationkit.execution` | `ExecutionManager`（長時間 execute の別スレッド管理）と関連モデル |
| `stationkit.adapters` | `create_http_app` (FastAPI) / `create_sequence_http_app` (Sequence API) / `create_cli_app` (service-backed CLI) / `create_local_cli_app` (同一プロセス) / `create_gui_app` (Gradio) |
| `stationkit.testing` | `MockStationController`（実機なしでの動作確認用） |
| `apps/sequence_app` | FastAPI + React の sequence editor / runner アプリ |

主要シンボルはトップレベル（`from stationkit import ...`）から直接インポートできます。

## 最小実装

開発者が書くのは、**`StationControllerBase` のサブクラスで `_do_*` を async 実装する**ことだけです。

1. `StationControllerBase` を継承する。
2. 次の 5 つを **すべて async** で実装する。
   - `_do_connect(self, address: str)`
   - `_do_disconnect(self)`
   - `_do_change(self, target: ...)`
   - `_do_execute(self)` もしくは `_do_execute(self, params: SomeModel)`
   - `_do_status(self)`
3. `_do_change` の `target` には **具体的な型ヒント** を付ける（例: `int`）。
   この型が HTTP / CLI / GUI の切替引数の型として使われます。
   `Any` のまま残すと警告のうえ `str` として扱われます。
4. `_do_execute` に実行時パラメータが必要なら **Pydantic モデル 1 個**を追加する。
   `_do_execute(self, params: MyParams)` または
   `_do_execute(self, params: MyParams | None = None)` の 2 形式に対応します。
5. 装置固有の操作を追加したい場合は `get_custom_actions()` で `CustomAction` のリストを返す。
6. 実行中の安全な中断に対応したい場合は、同期メソッド `cancel_execution()` を任意実装する。
   中断された `_do_execute` は `ExecutionCancelledError` を送出する運用を推奨します。

同期版 (`connect` 等) および `*_async` 版は基底クラスが提供するため、サブクラス側で二重実装する必要はありません。
`_do_execute` の中身が同期的に長時間かかる処理でも問題ありません。HTTP / GUI / service-backed CLI のようにブロッキングを避けたい面では、後述の `ExecutionManager` が `controller.execute()` 全体を別スレッドで管理します。

### コード例

```python
from typing import Any

from stationkit import StationControllerBase


class MyDeviceController(StationControllerBase):
    """実運用ではシリアルやソケットで装置と通信する想定。"""

    def __init__(self) -> None:
        super().__init__()
        self._address: str | None = None
        self._target: int | None = None

    async def _do_connect(self, address: str) -> None:
        self._address = address
        # 例: await open_serial(address)

    async def _do_disconnect(self) -> None:
        self._address = None

    async def _do_change(self, target: int) -> None:
        # target の型ヒント (int) が CLI/HTTP/GUI の引数型になる
        self._target = target

    async def _do_execute(self) -> dict[str, Any]:
        return {"address": self._address, "target": self._target}

    async def _do_status(self) -> dict[str, Any]:
        return {"target": self._target}
```

## 同期 API と非同期 API

- 通常のスクリプト（トップレベルが同期）では `connect` / `change` / `execute` / `status` / `disconnect` を使います。
- **既に asyncio のイベントループが動いている**コンテキスト（FastAPI のハンドラ内、別ライブラリの async コールバック内など）では同期 API は使えず、`RuntimeError` になります。その場合は対応する `*_async` を `await` してください。

## スクリプトから直接使う

HTTP を立てずに、**ライブラリとして取り込み**、同じコントローラインスタンスを再利用する使い方です。
バッチ処理や既存の測定スクリプトからの利用に向いています。

### 同期で一通り使う例

典型的な流れは `connect` → `change`（必要なら繰り返し） → `execute` → `disconnect` です。

```python
from mymodule import MyDeviceController

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

`MockStationController` に差し替えれば、実機なしで API の流れだけを試せます。

```python
from stationkit import MockStationController

c = MockStationController()
c.connect("DEMO")
c.change(2)
print(c.execute())
c.disconnect()
```

### asyncio の中から使う例

イベントループが既に動いているコンテキストでは、同期版は使えないので `*_async` を `await` します。

```python
import asyncio

from mymodule import MyDeviceController


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

失敗時は stationkit の例外（未接続で `change` したときの `StateError` など）がそのまま伝播します。
装置側のエラーは、可能なら `ConnectionError` / `CommandError` / `TimeoutError` などフレームワークの型にマッピングしておくと、HTTP 経由の挙動と揃います（[エラー設計の目安](#エラー設計の目安) を参照）。

## execute にパラメータを持たせる

実行時パラメータが必要な場合は、`_do_execute` に **Pydantic モデル 1 個** を追加します。
1 度定義すれば、直接 API / HTTP / CLI / GUI の全てで同じスキーマが共有されます。

```python
from typing import Any

from pydantic import BaseModel

from stationkit import StationControllerBase


class ExecuteParams(BaseModel):
    duration_s: int
    rpm: int | None = None


class MyDeviceController(StationControllerBase):
    # _do_connect / _do_disconnect / _do_change / _do_status は省略

    async def _do_execute(self, params: ExecuteParams) -> dict[str, Any]:
        return {"duration_s": params.duration_s, "rpm": params.rpm}
```

各経路での渡し方:

- **直接 API**: `controller.execute(ExecuteParams(duration_s=5, rpm=120))` または `controller.execute({"duration_s": 5, "rpm": 120})`
- **HTTP**: `POST /execute` に `{"duration_s": 5, "rpm": 120}` を送る
- **CLI**: `execute '{"duration_s": 5, "rpm": 120}'`
- **GUI**: モデルのフィールドが単純型ならフォーム、複雑型なら JSON テキスト入力

`_do_execute(self, params: ExecuteParams | None = None)` のように `None` を許容した場合は、引数なしの `execute()` もそのまま使えます。

## 固有操作（CustomAction）

装置独自のコマンドを、Pydantic の入出力スキーマ付きで公開できます。`get_custom_actions()` から `CustomAction` のリストを返すと、HTTP / CLI / GUI に自動で反映されます。

```python
from pydantic import BaseModel

from stationkit import CustomAction, StationControllerBase


class ZeroInput(BaseModel):
    """引数なしに相当するモデル（フィールドなしで可）。"""


class ZeroOutput(BaseModel):
    message: str


class MyController(StationControllerBase):
    # _do_* は省略

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

- **HTTP**: `POST /actions/ping`、ボディは入力スキーマに従う JSON（フィールド無しなら `{}`）。
- **CLI**: `ping '{}'` のように **JSON 文字列 1 引数** で渡す。

## 長時間 execute を別層で扱う（ExecutionManager）

`execute()` / `execute_async()` は引き続き「完了まで待って結果を返す」API です。
この同期的なセマンティクスを保ったまま、HTTP / GUI のような UI 面でブロッキングを避けるために、`stationkit` は **`ExecutionManager`** を別層として用意しています。

```python
from stationkit import ExecutionManager, ExecutionState

from mymodule import MyDeviceController

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

- `ExecutionManager` は **`controller.execute()` 全体** を single-worker thread で実行します（`_do_execute()` を直接呼ぶわけではありません）。
- `get_status()` は in-memory のジョブ状態のみを返し、装置 I/O は行いません。装置固有の状態は `controller.status()` 系で取得してください。
- `cancel()` は worker thread を kill せず、controller 側の `cancel_execution()` フックに **協調的な中断要求** を委ねます。
- `cancel_execution()` を実装していない controller に対して `ExecutionManager.cancel()` を呼ぶと、未対応エラーが返ります。

## HTTP サーバーとして動かす

`create_http_app()` でコントローラから FastAPI アプリを生成します。

```python
from stationkit import create_http_app

from mymodule import MyDeviceController

controller = MyDeviceController()
app = create_http_app(controller)
```

起動例（`uvicorn` は別途インストール）:

```bash
uv add uvicorn
uv run uvicorn mymodule:app --reload
```

### 主なエンドポイント

ボディを送る POST は JSON です。`Content-Type: application/json` を付けてください。GET はボディなし、レスポンスはすべて JSON です。

| メソッド | パス | 説明 | リクエスト（ボディ） | レスポンス例 |
|----------|------|------|----------------------|--------------|
| POST | `/connect` | 装置に接続する | `{"address": "<接続先>"}` 例: `{"address": "COM3"}` | `{"ok": true}` |
| POST | `/disconnect` | 装置から切断する | なし（空ボディ） | `{"ok": true}` |
| GET | `/status` | コントローラ状態と装置固有ステータスを取得 | なし | `{"controller_state": "CONNECTED", ...}` （`...` は `_do_status()` のキー） |
| POST | `/change` | 対象ステーション / チャネルを切り替える（要接続） | `{"target": <値>}` 型は `_do_change` の型ヒントに従う（整数なら `{"target": 2}`） | `{"ok": true}` |
| POST | `/execute` | メイン操作を実行して結果を返す（要接続） | 既定は空ボディ。`_do_execute(params: ExecuteParams)` を定義している場合はそのスキーマに従う JSON。`ExecuteParams \| None = None` なら空ボディ可 | 装置の戻り値依存の JSON（例: `{"mock": true, "target": 2}`） |
| POST | `/execute/start` | `ExecutionManager` 経由で execute を開始し、即時にハンドルを返す | `/execute` と同じ | `{"execution_id": "<id>"}` |
| GET | `/execute/status` | 直近または指定 execute ジョブの状態を取得 | クエリ `execution_id=<id>`（任意） | `{"execution_id": "...", "state": "RUNNING \| SUCCEEDED \| FAILED \| CANCELLED", "result": ..., "error_message": null, "cancel_requested": false, ...}` |
| POST | `/execute/cancel` | 進行中 execute の cancel を要求 | `{"execution_id": "<id>"}`（任意） | `{"ok": true}` |
| POST | `/actions/{name}` | `get_custom_actions()` で登録した固有操作を実行 | `{name}` の `input_schema` に従う JSON | `output_schema` がある場合はその構造、なければ戻り値に応じた JSON |

### エラーレスポンス

状態不整合や `StationError` 系は、HTTP ステータス 4xx / 5xx と共に次の形式で返ります。

```json
{"detail": "エラーメッセージ（例外の文字列）"}
```

`stationkit.core.exceptions` の `StationError` 系は HTTP ステータスに次のように写像されます。

| 例外 | HTTP ステータス |
|------|-----------------|
| `StateError` | 409 |
| `TimeoutError` | 504 |
| `ConnectionError` / `CommandError` | 502 |
| その他の `StationError` | 500 |

## CLI として動かす

`create_cli_app()` は **常駐 service が状態を保持し、CLI クライアントは毎回その service を呼び出す** 構成です。
スクリプトや CI から都度コマンドを呼びつつ、`connect` 後の状態をプロセス間で共有したい用途に向いています。

```python
from stationkit import create_cli_app

from mymodule import MyDeviceController

app = create_cli_app(MyDeviceController())

if __name__ == "__main__":
    app()
```

起動例（`serve` には uvicorn が必要です）:

```bash
uv add uvicorn

# ターミナル 1: service を起動
uv run python mycli.py serve --host 127.0.0.1 --port 8000

# ターミナル 2: CLI クライアントから service を叩く
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

`--server` は環境変数 `STATIONKIT_SERVER_URL` でも指定できます。

### execute 関連のサブコマンド

- `execute-start` / `execute-status` / `execute-cancel` は、`ExecutionManager` の開始・状態取得・cancel 要求を明示的に行うコマンドです。
- `execute` は **完了まで待機する convenience コマンド**として残しており、内部実装では `POST /execute/start` と `GET /execute/status` のポーリングを使います。
- `_do_execute` が入力モデルを持つ実装では、CLI でも **JSON 文字列 1 引数** で同じスキーマを渡します。
- `_do_execute(self, params: MyParams | None = None)` のようにオプショナル入力なら、引数なしの `execute` もそのまま使えます。

### 同一プロセスのローカル CLI

デバッグやテストのように、**同じ Python プロセス内で同じコントローラインスタンスを使い回したい**場合は `create_local_cli_app()` を使います。`CliRunner` や独自の対話ループに埋め込む用途向けで、通常の「コマンドを 1 回ずつ起動する CLI」としてはプロセス間で状態を共有しません。

> 単発の CLI が向くのは、**状態を service 側に持つ**場合、または 1 コマンドで `connect → change → execute → disconnect` まで自己完結する場合です。
> 接続状態を次の OS プロセスへ引き継ぎたい用途には、単発 CLI 単体は向きません。

```python
from stationkit import create_local_cli_app

from mymodule import MyDeviceController

app = create_local_cli_app(MyDeviceController())
```

service-backed CLI のサンプルを作りたい場合は、この節の `create_cli_app()` / `create_local_cli_app()` の例をそのまま launcher script に写して使ってください。

## Sequence Web App として動かす

シーケンス編集と実行をブラウザ UI で扱いたい場合は、`apps/sequence_app/server/app.py` の
`create_sequence_app_server(controller, ...)` を使います。
library 側は `create_sequence_http_app(controller)` で `/api/...` だけを提供し、
frontend の static 配信や開発時 CORS は app wrapper 側に分離されています。

```python
import uvicorn

from apps.sequence_app.server.app import create_sequence_app_server
from mymodule import MyDeviceController

controller = MyDeviceController()
app = create_sequence_app_server(
    controller,
    frontend_dist_dir="apps/sequence_app/web/dist",
)
uvicorn.run(app, host="127.0.0.1", port=8000)
```

このリポジトリの `main.py` は `MockStationController` をこの形で包んだ最小サンプルです。
開発手順と手動確認項目は `apps/sequence_app/README.md` を参照してください。

## Gradio GUI として動かす

Gradio ベースの GUI は `create_gui_app()` で生成します。返り値は `gr.Blocks` なので、呼び出し側で `launch()` してください。

```python
from stationkit import create_gui_app

from mymodule import MyDeviceController

controller = MyDeviceController()
app = create_gui_app(controller)
app.launch()
```

`create_http_app()` と同様に **1 つの controller インスタンスを共有** するので、複数のブラウザクライアントが開いていても、見ている相手は同じ装置・同じ接続状態です。状態の本体は Gradio 側ではなく **controller 側** にあります。

### 入力フォームの自動生成ルール

- `_do_change` の `target` が `str` / `int` / `float` / `bool` のいずれかなら、対応する GUI 入力ウィジェットが自動で選ばれます。
- `_do_execute` の入力モデルも同様で、単純なフィールド群ならフォーム、複雑なスキーマなら JSON テキスト入力になります。
- `Enum`、`list` / `dict`、`Union`、`BaseModel` などの複雑な型は、**JSON テキスト入力**にフォールバックします。
- `CustomAction` も同じルールで、入力スキーマが単純なフィールド群ならフォーム、複雑なら JSON テキスト入力で実行できます。

### 実行フロー

- `Start Execute` は内部で `ExecutionManager` を使って **非ブロッキングに開始** します。ハンドラ自体は完了まで待ちません。
- `Refresh Status` で実行状況を更新し、完了済み execute の結果は Result パネルに反映されます。
- `Cancel Execute` で協調的 cancel を要求できます。
- Status パネルには controller / device のステータスに加え、取得可能な場合は `execution` キーでジョブ状態も表示されます。

GUI からの操作は共有 controller に対して逐次実行され、各操作のあとに最新の `status()` 相当の情報が画面へ再反映されます。

## ロギング

`stationkit` は Python 標準の `logging` を使い、コア層と各アダプタ層で共通の `extra` キーを付与します。
既定では `NullHandler` が付いているため、利用側がハンドラを設定しない限り出力は増えません。

```python
import logging

logging.basicConfig(level=logging.INFO)
logging.getLogger("stationkit").setLevel(logging.INFO)
```

主な `extra` キー:

| キー | 内容 |
|------|------|
| `stationkit_layer` | `controller` / `http` / `cli` など、ログを発行した層 |
| `stationkit_op` | 操作名（例: `connect`, `execute`） |
| `stationkit_controller` | コントローラのクラス名 |
| `stationkit_event` | `start` / `success` / `failure` など |
| `stationkit_duration_ms` | 操作の所要時間（ミリ秒） |
| `stationkit_success` | 成否のブール |

`create_http_app(controller, logger=...)` で、HTTP 境界のログを任意の logger に流せます。

運用上、ログには **操作名や状態遷移など最小限の情報のみ** を載せ、接続先アドレス・認証情報・装置固有の秘密値などは出力しない前提で運用してください。

## テスト

```bash
uv run pytest
```

`MockStationController` を使うと、実機なしでアダプタや状態遷移を検証できます。

## エラー設計の目安

- 装置側の例外は、可能なら `ConnectionError` / `CommandError` / `TimeoutError` などフレームワークの型に寄せると、HTTP / CLI での取り扱いが揃います。
- **状態不整合**（未接続で `change` など）は基底クラスが `StateError` を送出します。
- **安全に中断できた execute** は `ExecutionCancelledError` を送出すると、`ExecutionManager` が `CANCELLED` として扱います。

## さらに読む

- 詳細な設計意図は `documents/station_controller_design.md` を参照してください。
