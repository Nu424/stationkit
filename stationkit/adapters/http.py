"""FastAPI アプリケーションを組み立てるファクトリ。"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, create_model

from stationkit.adapters._shared import normalize_result, resolve_target_type
from stationkit._logging import (
    get_adapter_logger,
    log_operation_failure,
    log_operation_start,
    log_operation_success,
)
from stationkit.core import (
    CommandError,
    ConnectionError,
    StateError,
    StationControllerBase,
    StationError,
    TimeoutError,
)
from stationkit.core.introspection import ExecuteParamsSpec, resolve_execute_params_spec
from stationkit.execution import ExecutionHandle, ExecutionManager, ExecutionStatus


def create_http_app(
    controller: StationControllerBase,
    logger: logging.Logger | None = None,
) -> FastAPI:
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
    execute_params_spec = resolve_execute_params_spec(controller) # executeの入力仕様をもとに、エンドポイントの形式を決定する
    app_logger = logger or get_adapter_logger("http") # httpアダプタ用のロガーを取得する
    execution_manager = ExecutionManager(controller)

    # リクエストボディ用の動的モデル（change の target 型に追従）
    ConnectRequest = create_model("ConnectRequest", address=(str, ...))
    ChangeRequest = create_model("ChangeRequest", target=(target_type, ...))
    CancelRequest = create_model("CancelRequest", execution_id=(str | None, None))

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
        await _run_http_operation(
            logger=app_logger,
            controller=controller,
            operation_name="connect",
            operation=lambda: controller.connect_async(req.address),
            context={"path": "/connect"},
            execution_manager=execution_manager,
        )
        return {"ok": True}

    @app.post("/disconnect")
    async def disconnect() -> dict[str, bool]:
        """装置から切断する。"""
        await _run_http_operation(
            logger=app_logger,
            controller=controller,
            operation_name="disconnect",
            operation=controller.disconnect_async,
            context={"path": "/disconnect"},
            execution_manager=execution_manager,
        )
        return {"ok": True}

    @app.post("/idle")
    async def idle() -> dict[str, bool]:
        """装置を idle 状態（稼働していないときの disposition）へ移す。"""
        await _run_http_operation(
            logger=app_logger,
            controller=controller,
            operation_name="idle",
            operation=controller.idle_async,
            context={"path": "/idle"},
            execution_manager=execution_manager,
        )
        return {"ok": True}

    @app.get("/status")
    async def status() -> dict[str, Any]:
        """コントローラおよび装置の状態を取得する。"""
        return await _run_http_operation(
            logger=app_logger,
            controller=controller,
            operation_name="status",
            operation=controller.status_async,
            context={"path": "/status"},
            execution_manager=execution_manager,
            allow_when_running=True,
        )

    @app.post("/change")
    async def change(req: ChangeRequest) -> dict[str, bool]:
        """対象ステーション（またはチャネル）を切り替える。"""
        await _run_http_operation(
            logger=app_logger,
            controller=controller,
            operation_name="change",
            operation=lambda: controller.change_async(req.target),
            context={
                "path": "/change",
                "target_type": type(req.target).__name__,
            },
            execution_manager=execution_manager,
        )
        return {"ok": True}

    """メイン操作を実行し、結果を返す。"""
    app.post("/execute")(
        _build_execute_endpoint(
            controller,
            app_logger,
            execute_params_spec,
            execution_manager,
        )
    )
    app.post("/execute/start", response_model=ExecutionHandle)(
        _build_execute_start_endpoint(
            controller,
            app_logger,
            execute_params_spec,
            execution_manager,
        )
    )

    @app.get("/execute/status", response_model=ExecutionStatus)
    async def execute_status(execution_id: str | None = None) -> ExecutionStatus:
        """直近または指定 execute ジョブの状態を取得する。"""
        return await _run_http_operation(
            logger=app_logger,
            controller=controller,
            operation_name="execute_status",
            operation=lambda: asyncio.to_thread(
                execution_manager.get_status, # execution_manager.get_status()で、実行中のexecuteの状況を取得する
                execution_id,
            ),
            context={
                "path": "/execute/status",
                "has_execution_id": execution_id is not None,
            },
            allow_when_running=True,
        )

    @app.post("/execute/cancel")
    async def execute_cancel(req: CancelRequest | None = None) -> dict[str, bool]:
        """進行中 execute の cancel を要求する。"""
        execution_id = None if req is None else req.execution_id
        await _run_http_operation(
            logger=app_logger,
            controller=controller,
            operation_name="execute_cancel",
            operation=lambda: asyncio.to_thread(
                execution_manager.cancel, # execution_manager.cancel()で、実行中のexecuteを中断する
                execution_id,
            ),
            context={
                "path": "/execute/cancel",
                "has_execution_id": execution_id is not None,
            },
            allow_when_running=True,
        )
        return {"ok": True}

    # -------------------------------------------------------------------------
    # CustomAction から動的に POST を追加
    # -------------------------------------------------------------------------

    for action in controller.get_custom_actions():
        app.post(
            f"/actions/{action.name}",
            description=action.description,
            response_model=action.output_schema,
        )(
            _build_action_endpoint(
                controller=controller,
                logger=app_logger,
                func=action.func,
                input_schema=action.input_schema,
                action_name=action.name,
                execution_manager=execution_manager,
            )
        )

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
    controller: StationControllerBase,
    logger: logging.Logger,
    func: Callable[[BaseModel], Awaitable[Any]],
    input_schema: type[BaseModel],
    action_name: str,
    execution_manager: ExecutionManager,
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
        return await _run_http_operation(
            logger=logger,
            controller=controller,
            operation_name=action_name,
            operation=lambda: func(params),
            context={
                "path": f"/actions/{action_name}",
                "has_params": True,
            },
            include_result=True,
            execution_manager=execution_manager,
        )

    endpoint.__name__ = f"{action_name}_endpoint"
    return endpoint


def _build_execute_endpoint(
    controller: StationControllerBase,
    logger: logging.Logger,
    params_spec: ExecuteParamsSpec,
    execution_manager: ExecutionManager,
) -> Callable[..., Awaitable[Any]]:
    """execute の入力仕様に応じた FastAPI エンドポイントを返す。
    
    Args:
        controller: コントローラインスタンス。
        logger: ロガー。
        params_spec: execute 入力仕様。

    Returns:
        FastAPI に渡す async エンドポイント。
    """
    # ---入力を受け取らない場合は、入力不要として、_run_http_operation()を実行する
    if not params_spec.accepts_params:
        async def endpoint() -> Any:
            return await _run_http_operation(
                logger=logger,
                controller=controller,
                operation_name="execute",
                operation=controller.execute_async,
                context={"path": "/execute", "has_params": False},
                include_result=True,
                execution_manager=execution_manager,
            )

        endpoint.__name__ = "execute_endpoint"
        return endpoint

    # ---入力を受け取る場合は、入力を受け取るasync関数を定義する
    model_type = params_spec.model_type
    if model_type is None:
        raise TypeError("execute parameter spec is missing model type")

    if params_spec.required: # Noneが許容されない場合
        async def endpoint(params: model_type) -> Any:
            return await _run_http_operation(
                logger=logger,
                controller=controller,
                operation_name="execute",
                operation=lambda: controller.execute_async(params),
                context={"path": "/execute", "has_params": True},
                include_result=True,
                execution_manager=execution_manager,
            )
    else: # Noneが許容される場合
        async def endpoint(params: model_type | None = None) -> Any:
            return await _run_http_operation(
                logger=logger,
                controller=controller,
                operation_name="execute",
                operation=lambda: controller.execute_async(params),
                context={"path": "/execute", "has_params": params is not None},
                include_result=True,
                execution_manager=execution_manager,
            )

    endpoint.__name__ = "execute_endpoint"
    return endpoint


def _build_execute_start_endpoint(
    controller: StationControllerBase,
    logger: logging.Logger,
    params_spec: ExecuteParamsSpec,
    execution_manager: ExecutionManager,
) -> Callable[..., Awaitable[ExecutionHandle]]:
    """manager ベースの execute 開始エンドポイントを返す。
    
    Args:
        controller: コントローラインスタンス。
        logger: ロガー。
        params_spec: execute 入力仕様。
        execution_manager: ExecutionManagerインスタンス。

    Returns:
        FastAPI に渡す async エンドポイント。
    """
    # ---入力を受け取らない場合は、入力不要として、_run_http_operation()を実行するエンドポイントを返す
    if not params_spec.accepts_params:
        async def endpoint() -> ExecutionHandle:
            return await _run_http_operation(
                logger=logger,
                controller=controller,
                operation_name="execute_start",
                operation=lambda: asyncio.to_thread(execution_manager.start), # execution_manager.start()で、executeを開始する
                context={"path": "/execute/start", "has_params": False},
                allow_when_running=True,
            )

        endpoint.__name__ = "execute_start_endpoint"
        return endpoint

    # ---以下、入力を受け取る場合:
    model_type = params_spec.model_type
    if model_type is None:
        raise TypeError("execute parameter spec is missing model type")

    # ---Noneが許容されない場合、入力を受け取るasync関数を定義する
    if params_spec.required:
        async def endpoint(params: model_type) -> ExecutionHandle:
            return await _run_http_operation(
                logger=logger,
                controller=controller,
                operation_name="execute_start",
                operation=lambda: asyncio.to_thread(execution_manager.start, params), # 入力を受け取りながら、execution_manager.start()する
                context={"path": "/execute/start", "has_params": True},
                allow_when_running=True,
            )
    else:
        # ---Noneが許容される場合、NoneもOKなasync関数を定義する
        async def endpoint(params: model_type | None = None) -> ExecutionHandle:
            return await _run_http_operation(
                logger=logger,
                controller=controller,
                operation_name="execute_start",
                operation=lambda: asyncio.to_thread(execution_manager.start, params),
                context={
                    "path": "/execute/start",
                    "has_params": params is not None,
                },
                allow_when_running=True,
            )

    endpoint.__name__ = "execute_start_endpoint"
    return endpoint


async def _run_http_operation(
    *,
    logger: logging.Logger,
    controller: StationControllerBase,
    operation_name: str,
    operation: Callable[[], Awaitable[Any]],
    context: dict[str, Any] | None = None,
    include_result: bool = False,
    execution_manager: ExecutionManager | None = None,
    allow_when_running: bool = False,
) -> Any:
    """HTTP 境界でログを出しつつ controller 操作を 1 回実行する。
    
    Args:
        logger: ロガー。
        controller: コントローラインスタンス。
        operation_name: 操作名。
        operation: 操作関数。
        context: コンテキスト。
        include_result: 結果を含めるかどうか。
        execution_manager: ExecutionManagerインスタンス。
        allow_when_running: 実行中の場合に許可するかどうか。

    Returns:
        操作の結果。
    """
    base_context = context or {}
    started = perf_counter()
    # ---操作開始ログを出力する
    log_operation_start(
        logger,
        layer="http",
        operation_name=operation_name,
        controller_name=type(controller).__name__,
        context=base_context,
    )
    try:
        # ---execution_managerの状況を確認してから、操作を実行する
        if (
            execution_manager is not None
            and not allow_when_running
            and execution_manager.has_active_execution()
        ):
            raise StateError("Execution is already running.")
        result = await operation()
    except Exception as exc:
        # ---失敗した場合、操作失敗ログを出力する
        log_operation_failure(
            logger,
            layer="http",
            operation_name=operation_name,
            controller_name=type(controller).__name__,
            duration_ms=(perf_counter() - started) * 1000,
            context=base_context,
            exc=exc,
        )
        raise

    # ---成功した場合、操作成功ログを出力する
    log_operation_success(
        logger,
        layer="http",
        operation_name=operation_name,
        controller_name=type(controller).__name__,
        duration_ms=(perf_counter() - started) * 1000,
        context=base_context,
    )
    return normalize_result(result) if include_result else result
