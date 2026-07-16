# StationController フレームワーク 設計書

## 1. 概要

複数の対象（ステーション）を切り替えながら、各対象に対して操作を実行する装置・プロセスを、Pythonから統一的に制御するためのフレームワーク。

オートサンプラー、マルチポート弁、テストチャンネル切替器など、「接続 → 対象選択 → 実行」のパターンを持つあらゆる装置に適用可能。

### 設計方針

- 具体クラスの実装者は **async メソッド (`_do_xxx`) を1箇所書くだけ** で、sync/async 両方の公開APIが得られる
- 固有機能は **メタデータ駆動** で宣言し、具体クラスがフレームワーク（FastAPI/Typer等）に依存しない状態を保つ
- ファクトリ関数に具体クラスのインスタンスを渡すだけで、HTTP API・CLI・GUI が自動的に立ち上がる

---

## 2. パッケージ構成

```
stationkit/
├── core/
│   ├── __init__.py
│   ├── base.py            # StationControllerBase
│   ├── action.py           # CustomAction 定義
│   ├── state.py            # ControllerState (状態 enum)
│   └── exceptions.py       # 例外階層
├── adapters/
│   ├── __init__.py
│   ├── http.py             # create_http_app() - FastAPI ファクトリ
│   ├── cli.py              # create_cli_app() - service-backed CLI
│   ├── local_cli.py        # create_local_cli_app() - 同一プロセス向け CLI
│   └── gui.py              # create_gui_app() - GUI ファクトリ (将来)
├── testing/
│   ├── __init__.py
│   └── mock.py             # MockStationController
└── __init__.py
```

---

## 3. コアクラス設計

### 3.1 状態管理: `ControllerState`

```python
# stationkit/core/state.py
from enum import Enum, auto

class ControllerState(Enum):
    DISCONNECTED = auto()
    CONNECTED = auto()
    BUSY = auto()
    ERROR = auto()
```

### 3.2 例外階層

```python
# stationkit/core/exceptions.py

class StationError(Exception):
    """フレームワーク共通の基底例外"""

class ConnectionError(StationError):
    """接続・切断に関するエラー"""

class CommandError(StationError):
    """コマンド送信・応答に関するエラー"""

class TimeoutError(StationError):
    """操作のタイムアウト"""

class StateError(StationError):
    """不正な状態遷移（未接続で execute を呼んだ等）"""
```

### 3.3 基底クラス: `StationControllerBase`

実装者は `_do_xxx` メソッド群（async）をオーバーライドする。
公開APIは sync 版 (`connect`, `execute`, ...) と async 版 (`connect_async`, `execute_async`, ...) の二重インターフェースで提供される。
状態ガード等の共通処理は公開API側に集約し、具体クラスに漏れない設計とする。
`_do_execute()` の中では同期的に長時間かかる処理を書いてよく、UX 上それを避けたい呼び出し面だけ別層の `ExecutionManager` で包む。

```python
# stationkit/core/base.py
from abc import ABC, abstractmethod
from typing import Any, List
import asyncio

from stationkit.core.state import ControllerState
from stationkit.core.action import CustomAction
from stationkit.core.exceptions import StateError


class StationControllerBase(ABC):

    def __init__(self):
        self._state: ControllerState = ControllerState.DISCONNECTED

    @property
    def state(self) -> ControllerState:
        return self._state

    # =========================================================
    # 実装者がオーバーライドする内部メソッド (async)
    # =========================================================

    @abstractmethod
    async def _do_connect(self, address: str) -> None: ...

    @abstractmethod
    async def _do_disconnect(self) -> None: ...

    @abstractmethod
    async def _do_change(self, target: Any) -> None: ...

    @abstractmethod
    async def _do_execute(self) -> Any: ...

    @abstractmethod
    async def _do_status(self) -> dict: ...

    # =========================================================
    # async 公開API (共通処理を含む)
    # =========================================================

    async def connect_async(self, address: str) -> None:
        if self._state != ControllerState.DISCONNECTED:
            raise StateError(
                f"connect requires DISCONNECTED state, "
                f"current: {self._state.name}"
            )
        await self._do_connect(address)
        self._state = ControllerState.CONNECTED

    async def disconnect_async(self) -> None:
        if self._state == ControllerState.DISCONNECTED:
            raise StateError("Already disconnected")
        await self._do_disconnect()
        self._state = ControllerState.DISCONNECTED

    async def change_async(self, target: Any) -> None:
        self._require_connected()
        await self._do_change(target)

    async def execute_async(self) -> Any:
        self._require_connected()
        self._state = ControllerState.BUSY
        try:
            result = await self._do_execute()
            self._state = ControllerState.CONNECTED
            return result
        except Exception:
            self._state = ControllerState.ERROR
            raise

    async def status_async(self) -> dict:
        return {
            "controller_state": self._state.name,
            **(await self._do_status()),
        }

    # =========================================================
    # sync 公開API (async 版のラッパー)
    # =========================================================

    def connect(self, address: str) -> None:
        self._run_sync(self.connect_async(address))

    def disconnect(self) -> None:
        self._run_sync(self.disconnect_async())

    def change(self, target: Any) -> None:
        self._run_sync(self.change_async(target))

    def execute(self) -> Any:
        return self._run_sync(self.execute_async())

    def status(self) -> dict:
        return self._run_sync(self.status_async())

    # =========================================================
    # 固有機能 (メタデータ駆動)
    # =========================================================

    def get_custom_actions(self) -> List[CustomAction]:
        """固有機能のリストを返す。デフォルトは空。"""
        return []

    # =========================================================
    # 内部ユーティリティ
    # =========================================================

    def _require_connected(self) -> None:
        if self._state not in (
            ControllerState.CONNECTED,
            ControllerState.BUSY,
        ):
            raise StateError(
                f"Operation requires CONNECTED state, "
                f"current: {self._state.name}"
            )

    @staticmethod
    def _run_sync(coro):
        try:
            asyncio.get_running_loop()
            raise RuntimeError(
                "sync API をイベントループ内から呼ばないでください。"
                "xxx_async() を使用してください。"
            )
        except RuntimeError as e:
            msg = str(e)
            if "no current event loop" in msg or "no running event loop" in msg:
                return asyncio.run(coro)
            raise
```

### 3.3.1 長時間 execute 用の補助層: `ExecutionManager`

`StationControllerBase` 自体には job API を入れず、HTTP / GUI / service CLI のような UX 重視面だけ **`ExecutionManager`** を併用する。

- `ExecutionManager.start()` は **`controller.execute()` を丸ごと** single-worker thread に submit して即座に `ExecutionHandle` を返す
- `ExecutionManager.get_status()` は in-memory なジョブ状態のみを返し、装置 I/O を行わない
- `ExecutionManager.cancel()` は worker thread を kill せず、controller が任意実装した **同期 hook `cancel_execution()`** を呼ぶ
- `cancel_execution()` が成功した場合、`_do_execute()` は **`ExecutionCancelledError`** を raise して unwind するのを標準ルールにする
- `ExecutionManager` は `ExecutionCancelledError` を `CANCELLED` に、それ以外の例外を `FAILED` に写像する

この構成により、単純な Python 利用者は従来どおり `execute()` / `execute_async()` を使い続けられ、長時間 execute による UI / HTTP ブロッキングだけを別層で吸収できる。

### 3.3.2 稼働していないときの動作: `_do_idle()`

「稼働していないとき（execute を実行していない期間）の装置の disposition」を宣言するための **任意フック**。
たとえばガスバッグ採取装置で、採取中は流路をバッグへ、採取していないときは排気レーンへ向ける、といった要件に対応する。

- 具体クラスは async メソッド `_do_idle()` を任意実装する（非 abstract・デフォルト no-op）。
- 基底クラスが次の遷移で **自動的に** 呼び出す:
  - `connect` 成功後（安全な初期状態の確立）
  - `execute` 成功終端（メイン操作後に idle へ戻す）
  - `execute` cancel 終端（`ExecutionCancelledError` による協調的中断後。基底の `execute_async` 内で捕捉→`CONNECTED` 復帰→idle→再送出とし、`ExecutionManager` は無改修）
  - `disconnect` 直前（通信を閉じる前にハードウェアを安全側へ置く。`CONNECTED` のときのみ）
- **想定外エラーで `ERROR` になった execute のあとは呼ばない**（装置状態が不確かなため能動操作を避ける）。
- idle は新しい状態ではなく「`CONNECTED` に入るときに確立する振る舞い」であり、`ControllerState` は変えない。
- 操作者向けに手動 API `idle()` / `idle_async()` を公開する（`CONNECTED` のときのみ許可）。HTTP `POST /idle`、CLI `idle`、GUI「Go Idle」、Sequence App の待機ボタンから利用できる。
- 時間駆動シーケンスの待機ギャップ中は、直前ステップの execute 終端で idle が確立されるため、`sequence.py` は無改修で「待機中は排気」を満たせる。

失敗時の扱い:

- `connect` 直後 / `execute` 成功終端の idle 失敗は送出する（後者は `ERROR` へ遷移させる。装置が安全状態にない可能性を通知するため）。
- `execute` cancel 終端 / `disconnect` 直前の idle 失敗は **best-effort**（ログのみ）とし、cancel の写像や切断そのものを妨げない。

### 3.4 固有機能定義: `CustomAction`

フレームワーク非依存の純粋なデータクラス。
ファクトリがこの情報を読み取り、エンドポイントやコマンドを自動生成する。

```python
# stationkit/core/action.py
from dataclasses import dataclass, field
from typing import Callable, Type, Optional
from pydantic import BaseModel


@dataclass
class CustomAction:
    name: str                                  # 機能名 (例: "calibrate")
    description: str                           # 説明
    func: Callable                             # 実行されるメソッド
    input_schema: Type[BaseModel]              # 引数の型 (Pydantic モデル)
    output_schema: Optional[Type[BaseModel]] = None  # 戻り値の型 (任意)
```

### 3.5 controller capability: `ControllerMetadata`

Sequence App 固有の設定を server factory に渡すのではなく、controller が
`get_metadata()` で対応 capability を宣言する。`sequence_modes` は非空かつ重複なしの
tuple とし、先頭を UI の既定モードとして扱う。

```python
from stationkit import ControllerMetadata, SequenceMode


def get_metadata(self) -> ControllerMetadata:
    return ControllerMetadata(
        sequence_modes=(
            SequenceMode.COMPLETION_DRIVEN,
            SequenceMode.TIME_DRIVEN,
        )
    )
```

- 未指定時: `COMPLETION_DRIVEN` と `TIME_DRIVEN` の両方
- 時間駆動のみ: `(SequenceMode.TIME_DRIVEN,)`
- 両対応: `(SequenceMode.COMPLETION_DRIVEN, SequenceMode.TIME_DRIVEN)`

`GET /api/meta` はこの宣言を frontend へ渡し、`SequenceRunner.validate()` も同じ宣言を
実行可否の正本として使う。時間駆動は終了時刻で実行を中断するため、
`TIME_DRIVEN` を宣言する controller は `cancel_execution()` も実装しなければならない。

---

## 4. 具体クラスの実装例

### 4.1 基本的な装置

```python
class AutoSamplerDriver(StationControllerBase):
    """整数ポジションで対象を切り替えるオートサンプラー"""

    def __init__(self):
        super().__init__()
        self._connection = None
        self._current_position: int | None = None

    async def _do_connect(self, address: str) -> None:
        self._connection = await open_serial(address)

    async def _do_disconnect(self) -> None:
        await self._connection.close()
        self._connection = None

    async def _do_change(self, target: int) -> None:
        await self._connection.send(f"POS {target}")
        self._current_position = target

    async def _do_execute(self) -> dict:
        response = await self._connection.send("SAMPLE")
        return {"position": self._current_position, "result": response}

    async def _do_status(self) -> dict:
        return {"current_position": self._current_position}
```

安全な中断に対応したい装置だけ、次のような任意 hook を追加する。

```python
from stationkit import ExecutionCancelledError


class AutoSamplerDriver(StationControllerBase):
    # ... _do_connect / _do_disconnect / _do_change は省略 ...

    def cancel_execution(self) -> None:
        self._connection.send_nowait("ABORT")

    async def _do_execute(self) -> dict:
        while True:
            response = await self._connection.recv()
            if response == "CANCELLED":
                raise ExecutionCancelledError("sampling aborted")
            if response.startswith("DONE"):
                return {"result": response}
```

### 4.2 固有機能を持つ装置

```python
class CalibrateInput(BaseModel):
    level: int
    force: bool = False

class CalibrateOutput(BaseModel):
    status: str
    level: int

class AdvancedSamplerDriver(StationControllerBase):

    async def _do_connect(self, address: str) -> None: ...
    async def _do_disconnect(self) -> None: ...
    async def _do_change(self, target: int) -> None: ...
    async def _do_execute(self) -> dict: ...
    async def _do_status(self) -> dict: ...

    # --- 固有機能 ---

    async def _do_calibrate(self, params: CalibrateInput) -> dict:
        # キャリブレーション処理
        return {"status": "success", "level": params.level}

    def get_custom_actions(self) -> list[CustomAction]:
        return [
            CustomAction(
                name="calibrate",
                description="機器のキャリブレーションを実行します",
                func=self._do_calibrate,
                input_schema=CalibrateInput,
                output_schema=CalibrateOutput,
            ),
        ]
```

---

## 5. ファクトリ（アダプタ）

### 5.1 `change` の型解決

ファクトリは具体クラスの `_do_change` の型ヒントを実行時に取得し、エンドポイント/コマンドの引数型を決定する。

```python
from typing import get_type_hints, Any

def _resolve_target_type(controller: StationControllerBase) -> type:
    hints = get_type_hints(controller._do_change)
    target_type = hints.get("target", Any)
    if target_type is Any:
        import warnings
        warnings.warn(
            f"{type(controller).__name__}._do_change の target に"
            f"型ヒントがありません。str として扱います。",
            stacklevel=2,
        )
        return str
    return target_type
```

### 5.2 FastAPI ファクトリ: `create_http_app()`

```python
# stationkit/adapters/http.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import get_type_hints

def create_http_app(controller: StationControllerBase) -> FastAPI:
    app = FastAPI(title=type(controller).__name__)
    target_type = _resolve_target_type(controller)

    class ConnectRequest(BaseModel):
        address: str

    class ChangeRequest(BaseModel):
        target: target_type  # 動的に型が決まる

    @app.post("/connect")
    async def connect(req: ConnectRequest):
        await controller.connect_async(req.address)
        return {"ok": True}

    @app.post("/disconnect")
    async def disconnect():
        await controller.disconnect_async()
        return {"ok": True}

    @app.get("/status")
    async def status():
        return await controller.status_async()

    @app.post("/change")
    async def change(req: ChangeRequest):
        await controller.change_async(req.target)
        return {"ok": True}

    @app.post("/execute")
    async def execute():
        return await controller.execute_async()

    # --- CustomAction の自動登録 ---
    for action in controller.get_custom_actions():
        def _make_endpoint(act):
            async def endpoint(params: act.input_schema):
                return await act.func(params)
            endpoint.__name__ = act.name
            return endpoint

        app.post(
            f"/actions/{action.name}",
            description=action.description,
            response_model=action.output_schema,
        )(_make_endpoint(action))

    return app
```

### 5.3 Typer ファクトリ: `create_cli_app()`

```python
# stationkit/adapters/cli.py
import typer

def create_cli_app(controller: StationControllerBase) -> typer.Typer:
    app = typer.Typer(name=type(controller).__name__)

    @app.callback()
    def callback(ctx: typer.Context, server: str = "http://127.0.0.1:8000"):
        ctx.obj = {"server_url": server}

    @app.command()
    def serve(host: str = "127.0.0.1", port: int = 8000):
        uvicorn.run(create_http_app(controller), host=host, port=port)

    @app.command()
    def connect(ctx: typer.Context, address: str):
        _request_service(
            "POST",
            ctx.obj["server_url"],
            "/connect",
            {"address": address},
        )
        typer.echo("Connected.")

    # change / execute / status / custom actions も同様に
    # service へ HTTP で委譲する

    return app
```

`create_cli_app()` は **単発 CLI を積み重ねても state を共有できるように、常駐 service に責務を寄せる** のが目的である。コントローラのインメモリ状態は `serve` で起動した service 側にあり、CLI はその薄い client になる。

同一プロセス内でコントローラインスタンスを保持したいデバッグ/テスト用途には、`create_local_cli_app()` を別に用意する。

---

## 6. テスト支援: `MockStationController`

実機器なしでファクトリやアプリケーションの動作を検証するためのモック。

```python
# stationkit/testing/mock.py

class MockStationController(StationControllerBase):
    """テスト・デモ用のモックコントローラ"""

    def __init__(self):
        super().__init__()
        self._current_target = None
        self._call_log: list[str] = []

    async def _do_connect(self, address: str) -> None:
        self._call_log.append(f"connect({address})")

    async def _do_disconnect(self) -> None:
        self._call_log.append("disconnect()")

    async def _do_change(self, target: int) -> None:
        self._call_log.append(f"change({target})")
        self._current_target = target

    async def _do_execute(self) -> dict:
        self._call_log.append("execute()")
        return {"mock": True, "target": self._current_target}

    async def _do_status(self) -> dict:
        return {"current_target": self._current_target}
```

---

## 7. 利用フロー

```python
# --- 具体クラスのインスタンスを作る ---
sampler = AutoSamplerDriver()

# --- HTTP サーバーとして起動 ---
app = create_http_app(sampler)
# uvicorn で起動: uvicorn main:app

# --- service + CLI client として起動 ---
cli = create_cli_app(sampler)
# terminal 1: python main.py serve
# terminal 2: python main.py --server http://127.0.0.1:8000 connect COM3

# --- 同一プロセス向けローカル CLI (テスト/デバッグ) ---
local_cli = create_local_cli_app(sampler)

# --- スクリプトから直接使用 (sync) ---
sampler.connect("COM3")
sampler.change(1)
result = sampler.execute()
sampler.disconnect()

# --- async コードから使用 ---
async def main():
    await sampler.connect_async("COM3")
    await sampler.change_async(1)
    result = await sampler.execute_async()
    await sampler.disconnect_async()
```

---

## 8. 設計上の補足

### エラーハンドリング方針

- 具体クラスの `_do_xxx` 内で発生した例外は `stationkit.core.exceptions` の型に変換して送出することを推奨する
- ファクトリ（HTTP/CLI）側で `StationError` 系を一括キャッチし、適切なレスポンス（HTTP 4xx/5xx、CLI エラー表示）に変換する
- `StateError` は基底クラスが自動送出するため、具体クラスでのガード実装は不要

### `target: Any` の型ヒント運用ルール

- 基底クラスでは `target: Any` として柔軟性を確保する
- 具体クラスは **必ず** `_do_change(self, target: int)` のように具体型を付与する
- ファクトリは `get_type_hints()` で具体型を取得する。`Any` のまま残っている場合は警告を出し `str` にフォールバックする

### 今後の拡張候補

- **イベント / フック機構**: `on_connect`, `on_execute_complete` 等のコールバック
  （「稼働していないときの動作」は `_do_idle()`（3.3.2）として実装済み）
- **バッチ実行**: 複数ステーションへの連続操作を宣言的に記述する機能
- **GUI アダプタ**: `create_gui_app()` の実装（Tkinter / web UI 等）
- **ロギング**: 基底クラスの公開APIに統一的なログ出力を組み込む
