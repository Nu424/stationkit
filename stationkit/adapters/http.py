"""FastAPI アプリケーションを組み立てるファクトリ。"""

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, create_model

from stationkit.adapters._shared import normalize_result, resolve_target_type
from stationkit.core import (
    CommandError,
    ConnectionError,
    StateError,
    StationControllerBase,
    StationError,
    TimeoutError,
)


def create_http_app(controller: StationControllerBase) -> FastAPI:
    """コントローラ用の FastAPI アプリを生成する。

    標準エンドポイント（connect / disconnect / status / change / execute）に加え、
    ``get_custom_actions()`` で返した操作を ``/actions/{name}`` に登録する。

    Args:
        controller: 装置実装のインスタンス。

    Returns:
        ルート登録済みの :class:`fastapi.FastAPI` インスタンス。
    """
    app = FastAPI(title=type(controller).__name__)
    target_type = resolve_target_type(controller)

    # リクエストボディ用の動的モデル（change の target 型に追従）
    ConnectRequest = create_model("ConnectRequest", address=(str, ...))
    ChangeRequest = create_model("ChangeRequest", target=(target_type, ...))

    # -------------------------------------------------------------------------
    # 例外ハンドリング
    # -------------------------------------------------------------------------

    @app.exception_handler(StationError)
    async def handle_station_error(
        _request: Request, exc: StationError
    ) -> JSONResponse:
        """StationError 系を HTTP ステータスと JSON 本文に変換する。"""
        status_code = _resolve_status_code(exc)
        return JSONResponse(status_code=status_code, content={"detail": str(exc)})

    # -------------------------------------------------------------------------
    # 標準エンドポイント
    # -------------------------------------------------------------------------

    @app.post("/connect")
    async def connect(req: ConnectRequest) -> dict[str, bool]:
        """装置に接続する。"""
        await controller.connect_async(req.address)
        return {"ok": True}

    @app.post("/disconnect")
    async def disconnect() -> dict[str, bool]:
        """装置から切断する。"""
        await controller.disconnect_async()
        return {"ok": True}

    @app.get("/status")
    async def status() -> dict[str, Any]:
        """コントローラおよび装置の状態を取得する。"""
        return await controller.status_async()

    @app.post("/change")
    async def change(req: ChangeRequest) -> dict[str, bool]:
        """対象ステーション（またはチャネル）を切り替える。"""
        await controller.change_async(req.target)
        return {"ok": True}

    @app.post("/execute")
    async def execute() -> Any:
        """メイン操作を実行し、結果を返す。"""
        return normalize_result(await controller.execute_async())

    # -------------------------------------------------------------------------
    # CustomAction から動的に POST を追加
    # -------------------------------------------------------------------------

    for action in controller.get_custom_actions():
        app.post(
            f"/actions/{action.name}",
            description=action.description,
            response_model=action.output_schema,
        )(_build_action_endpoint(action.func, action.input_schema, action.name))

    return app


def _resolve_status_code(exc: StationError) -> int:
    """例外の種類に応じた HTTP ステータスコードを返す。

    Args:
        exc: 捕捉した :class:`StationError`。

    Returns:
        適切な 4xx/5xx コード。
    """
    if isinstance(exc, StateError):
        return 409
    if isinstance(exc, TimeoutError):
        return 504
    if isinstance(exc, (ConnectionError, CommandError)):
        return 502
    return 500


def _build_action_endpoint(
    func: Callable[[BaseModel], Awaitable[Any]],
    input_schema: type[BaseModel],
    action_name: str,
) -> Callable[[BaseModel], Awaitable[Any]]:
    """CustomAction 用の FastAPI エンドポイント関数を生成する。

    Args:
        func: 入力モデルを受け取る async callable。
        input_schema: リクエストボディの Pydantic モデル。
        action_name: ログ用の操作名。

    Returns:
        FastAPI に渡す async エンドポイント。
    """
    async def endpoint(params: input_schema) -> Any:
        """固有操作を実行する。"""
        return normalize_result(await func(params))

    endpoint.__name__ = f"{action_name}_endpoint"
    return endpoint
