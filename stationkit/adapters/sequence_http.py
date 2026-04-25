"""SequenceRunner を HTTP API 化する FastAPI adapter。

このモジュールは、測定シーケンス用 Web UI から必要になる最小限の HTTP API を
組み立てる。責務はあくまで「controller / ExecutionManager / SequenceRunner を
HTTP 越しに扱えるようにすること」に限定し、static 配信や CORS などの
アプリ固有都合は上位の wrapper に委ねる。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from threading import Lock
from time import perf_counter
from typing import Any, Literal, TypeAlias

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, TypeAdapter

from stationkit.adapters._form_inputs import (
    InputFieldSpec,
    PrimitiveInputType,
    input_field_specs_from_model,
    resolve_primitive_input_type,
)
from stationkit._logging import (
    get_adapter_logger,
    log_operation_failure,
    log_operation_start,
    log_operation_success,
)
from stationkit.adapters._shared import normalize_result
from stationkit.core import (
    CommandError,
    ConnectionError,
    CustomAction,
    StateError,
    StationControllerBase,
    StationError,
    TimeoutError,
)
from stationkit.core.introspection import (
    ExecuteParamsSpec,
    normalize_execute_params,
    resolve_execute_params_spec,
    resolve_target_type,
    unwrap_optional_type,
)
from stationkit.execution import ExecutionHandle, ExecutionManager
from stationkit.sequence import (
    SequenceDefinition,
    SequenceIssue,
    SequenceRunner,
    SequenceSnapshot,
)

PrimitiveFieldType: TypeAlias = PrimitiveInputType
InputFormKind: TypeAlias = Literal["none", "field", "fields", "json"]


# -----------------------------------------------------------------------------
# UI メタデータ / HTTP 入出力モデル
# FieldMetaがあつまって、InputMetaができる
# カスタムアクションは、InputMetaを囲っている
# -----------------------------------------------------------------------------


class FieldMeta(BaseModel):
    """動的フォーム 1 項目ぶんのメタデータ。"""

    name: str
    label: str
    type: PrimitiveFieldType
    required: bool
    default: Any = None
    nullable: bool = False


class InputMeta(BaseModel):
    """target / execute / action 入力のメタデータ。"""

    kind: InputFormKind
    accepts_params: bool = True
    required: bool = False
    fields: list[FieldMeta] = Field(default_factory=list)


class CustomActionMeta(BaseModel):
    """CustomAction の UI 用メタデータ。"""

    name: str
    description: str
    input: InputMeta


class SequenceMetaResponse(BaseModel):
    """画面初期化用のメタデータ。"""

    controller_name: str
    target: InputMeta
    execute: InputMeta
    sequence_modes: list[str]
    custom_actions: list[CustomActionMeta] = Field(default_factory=list)


class SequenceStatusResponse(BaseModel):
    """画面 polling 用の全体状態。"""

    controller: dict[str, Any]
    manual_execution: dict[str, Any] | None = None
    sequence: dict[str, Any] | None = None


class SequenceValidateResponse(BaseModel):
    """validation API の返却値。"""

    ok: bool
    issues: list[SequenceIssue] = Field(default_factory=list)


class ConnectRequest(BaseModel):
    """接続要求。"""

    address: str


class ChangeRequest(BaseModel):
    """target 切替要求。"""

    target: Any


class SequenceDefinitionRequest(BaseModel):
    """シーケンス定義を受け取る要求。"""

    definition: SequenceDefinition


class SequenceCheckStepRequest(BaseModel):
    """単一ステップ実行要求。"""

    definition: SequenceDefinition
    step_index: int


# ----------
# ---本体
# ----------
def create_sequence_http_app(
    controller: StationControllerBase,
    logger: logging.Logger | None = None,
) -> FastAPI:
    """シーケンス管理用の HTTP API を生成する。

    Args:
        controller: 対象コントローラ。
        logger: adapter 用ロガー。省略時は既定ロガーを使う。

    Returns:
        `/api/...` ルート登録済みの FastAPI アプリ。
    """
    # -------------------------------------------------------------------------
    # adapter 内で共有する実行コンテキストを初期化する
    # -------------------------------------------------------------------------
    app_logger = logger or get_adapter_logger("sequence_http")
    operation_lock = Lock()
    manual_execution_manager = ExecutionManager(controller) # 手動実行用のExecutionManager
    sequence_runner = SequenceRunner(controller) # シーケンス実行用のSequenceRunner(こいつはこいつで内部にExecutionManagerを持っている)
    target_adapter = TypeAdapter(resolve_target_type(controller))
    execute_params_spec = resolve_execute_params_spec(controller)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """FastAPI アプリの生存期間に合わせて内部リソースを管理する。

        Args:
            _app: lifespan の対象 FastAPI アプリ。

        Yields:
            アプリ実行期間中の制御を呼び出し側へ渡す。

        Note:
            adapter 内部で生成した `ExecutionManager` と `SequenceRunner` は、
            アプリ終了時にここで確実に close する。
        """
        try:
            yield
        finally:
            # ---終了時、ExecutionManagerとSequenceRunnerをcloseする
            manual_execution_manager.close()
            sequence_runner.close()

    app = FastAPI(
        title=f"{type(controller).__name__} Sequence API",
        lifespan=lifespan,
    )

    # -------------------------------------------------------------------------
    # 例外ハンドリング
    # -------------------------------------------------------------------------

    @app.exception_handler(StationError)
    async def handle_station_error(
        _request: Request, exc: StationError
    ) -> JSONResponse:
        """`StationError` 系を HTTP エラーへ変換する。

        Args:
            _request: 発生元リクエスト。
            exc: 変換対象の例外。

        Returns:
            status code と `detail` を含む JSON レスポンス。
        """
        return JSONResponse(
            status_code=_resolve_status_code(exc),
            content={"detail": str(exc)},
        )

    # -------------------------------------------------------------------------
    # bootstrap / polling 用 API
    # -------------------------------------------------------------------------

    @app.get("/api/meta", response_model=SequenceMetaResponse)
    async def api_meta() -> SequenceMetaResponse:
        """フォーム生成用のメタデータを返す。

        Returns:
            target / execute / custom action の入力仕様をまとめたメタデータ。
        """
        return SequenceMetaResponse(
            controller_name=type(controller).__name__,
            target=_build_target_meta(controller),
            execute=_build_execute_meta(execute_params_spec),
            sequence_modes=["COMPLETION_DRIVEN", "TIME_DRIVEN"],
            custom_actions=[
                _build_custom_action_meta(action)
                for action in controller.get_custom_actions()
            ],
        )

    @app.get("/api/status", response_model=SequenceStatusResponse)
    async def api_status() -> SequenceStatusResponse:
        """controller / manual execute / sequence の状態をまとめて返す。

        Returns:
            画面の bootstrap と polling に必要な現在状態。
        """
        controller_status = await _run_sequence_http_operation(
            logger=app_logger,
            controller=controller,
            operation_name="status",
            operation=controller.status_async,
            context={"path": "/api/status"},
            allow_manual_when_running=True,
            allow_sequence_when_running=True,
            operation_lock=None,
        )
        return SequenceStatusResponse(
            controller=controller_status,
            # ---各種実行の状態を取得して返す
            manual_execution=_try_get_manual_execution_status(manual_execution_manager),
            sequence=_try_get_sequence_snapshot(sequence_runner),
        )

    # ---以下、各種エンドポイント。
    # 基本的には、controller, ExecutionManager, SequenceRunnerで用意した関数を、_run_sequence_http_operationで実行する感じ。
    # -------------------------------------------------------------------------
    # controller 直接操作 API
    # -------------------------------------------------------------------------

    @app.post("/api/controller/connect")
    async def api_connect(req: ConnectRequest) -> dict[str, bool]:
        """装置へ接続する。

        Args:
            req: 接続先アドレスを含むリクエスト。

        Returns:
            成功時の確認レスポンス。
        """
        await _run_sequence_http_operation(
            logger=app_logger,
            controller=controller,
            operation_name="connect",
            operation=lambda: controller.connect_async(req.address),
            context={"path": "/api/controller/connect"},
            operation_lock=operation_lock,
            manual_execution_manager=manual_execution_manager,
            sequence_runner=sequence_runner,
        )
        return {"ok": True}

    @app.post("/api/controller/disconnect")
    async def api_disconnect() -> dict[str, bool]:
        """装置から切断する。

        Returns:
            成功時の確認レスポンス。
        """
        await _run_sequence_http_operation(
            logger=app_logger,
            controller=controller,
            operation_name="disconnect",
            operation=controller.disconnect_async,
            context={"path": "/api/controller/disconnect"},
            operation_lock=operation_lock,
            manual_execution_manager=manual_execution_manager,
            sequence_runner=sequence_runner,
        )
        return {"ok": True}

    @app.post("/api/controller/change")
    async def api_change(req: ChangeRequest) -> dict[str, bool]:
        """target を手動で切り替える。

        Args:
            req: 切替対象 target を含むリクエスト。

        Returns:
            成功時の確認レスポンス。
        """
        # HTTP ボディでは Any で受け、controller の target 型へここで正規化する。
        target = target_adapter.validate_python(req.target)
        await _run_sequence_http_operation(
            logger=app_logger,
            controller=controller,
            operation_name="change",
            operation=lambda: controller.change_async(target),
            context={"path": "/api/controller/change"},
            operation_lock=operation_lock,
            manual_execution_manager=manual_execution_manager,
            sequence_runner=sequence_runner,
        )
        return {"ok": True}

    # -------------------------------------------------------------------------
    # sequence 全体の検証 / 実行 / 停止 API
    # -------------------------------------------------------------------------

    @app.post("/api/sequence/validate", response_model=SequenceValidateResponse)
    async def api_sequence_validate(
        req: SequenceDefinitionRequest,
    ) -> SequenceValidateResponse:
        """シーケンス定義を検証する。

        Args:
            req: 検証対象のシーケンス定義。

        Returns:
            `ok` と issue 一覧を持つ validation 結果。
        """
        result = await _run_sequence_http_operation(
            logger=app_logger,
            controller=controller,
            operation_name="sequence_validate",
            operation=lambda: asyncio.to_thread(sequence_runner.validate, req.definition),
            context={"path": "/api/sequence/validate"},
            allow_manual_when_running=True,
            allow_sequence_when_running=True,
            operation_lock=None,
        )
        return SequenceValidateResponse(ok=result.ok, issues=result.issues)

    @app.post("/api/sequence/run", response_model=SequenceSnapshot)
    async def api_sequence_run(req: SequenceDefinitionRequest) -> SequenceSnapshot:
        """シーケンス全体の実行を開始する。

        Args:
            req: 実行対象のシーケンス定義。

        Returns:
            実行開始直後の snapshot。
        """

        async def start_sequence() -> SequenceSnapshot:
            """別スレッドで sequence を開始し、開始直後の snapshot を返す。

            Returns:
                開始した run の最新 snapshot。
            """
            handle = await asyncio.to_thread(sequence_runner.start, req.definition)
            return await asyncio.to_thread(sequence_runner.get_snapshot, handle.run_id)

        return await _run_sequence_http_operation(
            logger=app_logger,
            controller=controller,
            operation_name="sequence_run",
            operation=start_sequence,
            context={"path": "/api/sequence/run"},
            operation_lock=operation_lock,
            manual_execution_manager=manual_execution_manager,
            sequence_runner=sequence_runner,
            include_result=True,
        )

    @app.post("/api/sequence/stop", response_model=SequenceSnapshot)
    async def api_sequence_stop() -> SequenceSnapshot:
        """シーケンス停止を要求し、最新 snapshot を返す。

        Returns:
            stop 要求直後の最新 snapshot。
        """

        async def stop_sequence() -> SequenceSnapshot:
            """別スレッドで stop を要求し、その直後の snapshot を返す。

            Returns:
                stop 要求後の最新 snapshot。
            """
            await asyncio.to_thread(sequence_runner.stop)
            return await asyncio.to_thread(sequence_runner.get_snapshot)

        return await _run_sequence_http_operation(
            logger=app_logger,
            controller=controller,
            operation_name="sequence_stop",
            operation=stop_sequence,
            context={"path": "/api/sequence/stop"},
            allow_manual_when_running=True,
            allow_sequence_when_running=True,
            operation_lock=None,
            include_result=True,
        )

    @app.post("/api/sequence/check-step", response_model=ExecutionHandle)
    async def api_sequence_check_step(
        req: SequenceCheckStepRequest,
    ) -> ExecutionHandle:
        """選択ステップ 1 行だけを単体検証として起動する。

        Args:
            req: 対象シーケンス定義と step index を含むリクエスト。

        Returns:
            開始した manual execution のハンドル。
        """
        # ---ステップ番号・ターゲット・実行パラメータをバリデーションして取得する
        step = _get_enabled_step(req.definition, req.step_index)
        target = target_adapter.validate_python(step.target)
        params = normalize_execute_params(controller, step.execute_params)

        async def check_step() -> ExecutionHandle:
            """選択ステップの target 適用と execute 開始を順に行う。

            Returns:
                開始した manual execution のハンドル。
            """
            await controller.change_async(target)
            return await asyncio.to_thread(manual_execution_manager.start, params)

        return await _run_sequence_http_operation(
            logger=app_logger,
            controller=controller,
            operation_name="sequence_check_step",
            operation=check_step,
            context={"path": "/api/sequence/check-step", "step_index": req.step_index},
            operation_lock=operation_lock,
            manual_execution_manager=manual_execution_manager,
            sequence_runner=sequence_runner,
        )

    return app


# -----------------------------------------------------------------------------
# HTTP 実行境界の共通 helper
# -----------------------------------------------------------------------------


def _resolve_status_code(exc: StationError) -> int:
    """`StationError` の種類から HTTP ステータスコードを決める。

    Args:
        exc: 変換対象の例外。

    Returns:
        例外種別に対応する HTTP ステータスコード。
    """
    if isinstance(exc, StateError):
        return 409
    if isinstance(exc, TimeoutError):
        return 504
    if isinstance(exc, (ConnectionError, CommandError)):
        return 502
    return 500


async def _run_sequence_http_operation(
    *,
    logger: logging.Logger,
    controller: StationControllerBase,
    operation_name: str,
    operation: Callable[[], Awaitable[Any]],
    context: dict[str, Any] | None = None,
    operation_lock: Lock | None = None,
    manual_execution_manager: ExecutionManager | None = None,
    sequence_runner: SequenceRunner | None = None,
    allow_manual_when_running: bool = False,
    allow_sequence_when_running: bool = False,
    include_result: bool = False,
) -> Any:
    """HTTP 境界でログを出しながら 1 操作を実行する。

    Args:
        logger: adapter 用ロガー。
        controller: 対象コントローラ。
        operation_name: ログに載せる操作名。
        operation: 実行対象の async callable。
        context: ログへ付与する追加コンテキスト。
        operation_lock: controller 操作を直列化したい場合の共有ロック。
        manual_execution_manager: manual execution の排他確認に使う manager。
        sequence_runner: sequence run の排他確認に使う runner。
        allow_manual_when_running: manual execution 実行中でも許可するか。
        allow_sequence_when_running: sequence run 実行中でも許可するか。
        include_result: 戻り値を JSON 化して返すか。

    Returns:
        `operation` の実行結果。`include_result=True` の場合は
        `normalize_result()` 済みの値。

    Raises:
        StateError: manual execution または sequence run の排他条件に違反した場合。
        Exception: `operation` 内で発生した例外をそのまま再送出する。
    """
    base_context = context or {}
    started = perf_counter()
    # ---ログ出す
    log_operation_start(
        logger,
        layer="http",
        operation_name=operation_name,
        controller_name=type(controller).__name__,
        context=base_context,
    )
    try:
        async with _maybe_hold_lock(operation_lock):
            # ---手動実行が実行中の場合、エラーを返す
            if (
                manual_execution_manager is not None
                and not allow_manual_when_running
                and manual_execution_manager.has_active_execution()
            ):
                raise StateError("Execution is already running.")
            # ---シーケンス実行が実行中の場合、エラーを返す
            if (
                sequence_runner is not None
                and not allow_sequence_when_running
                and sequence_runner.has_active_run()
            ):
                raise StateError("Sequence is already running.")
            # ---operationを実行する
            result = await operation()
    except Exception as exc:
        # ---失敗した場合、ログを出す
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

    # ---成功した場合、ログを出す
    log_operation_success(
        logger,
        layer="http",
        operation_name=operation_name,
        controller_name=type(controller).__name__,
        duration_ms=(perf_counter() - started) * 1000,
        context=base_context,
    )
    # ---結果を返す
    return normalize_result(result) if include_result else result


@asynccontextmanager
async def _maybe_hold_lock(operation_lock: Lock | None):
    """必要な場合だけロックを取得する。

    Args:
        operation_lock: 取得対象の共有ロック。`None` の場合は何もしない。

    Yields:
        ロック取得済み、またはロック不要な実行コンテキスト。
    """
    if operation_lock is None:
        yield
        return
    await asyncio.to_thread(operation_lock.acquire)
    try:
        yield
    finally:
        operation_lock.release()


# -----------------------------------------------------------------------------
# `/api/meta` 用のフォームメタデータ構築 helper
# -----------------------------------------------------------------------------


def _build_target_meta(controller: StationControllerBase) -> InputMeta:
    """target 入力用のメタデータを返す。

    Args:
        controller: target 型を解決したいコントローラ。

    Returns:
        target 入力欄 1 つぶんのメタデータ。
    """
    # target は Pydantic model ではないため、型注釈から直接 1 field 分を組み立てる。
    target_type = resolve_target_type(controller)
    field = _field_meta_from_annotation(
        name="target",
        annotation=target_type,
        label="Target",
        required=True,
        default=None,
    )
    return InputMeta(kind="field", accepts_params=True, required=True, fields=[field])


def _build_execute_meta(params_spec: ExecuteParamsSpec) -> InputMeta:
    """execute 入力用のメタデータを返す。

    Args:
        params_spec: execute の入力仕様。

    Returns:
        execute 入力欄全体のメタデータ。

    Raises:
        TypeError: params を受け取るはずなのにモデル型が欠けている場合。
    """
    # ---入力をなにも受け付けない場合
    if not params_spec.accepts_params:
        return InputMeta(kind="none", accepts_params=False, required=False)

    model_type = params_spec.model_type
    if model_type is None:
        raise TypeError("execute parameter spec is missing model type")

    # 単純な scalar field 群で表現できるなら `fields`、複雑なら JSON 入力へ倒す。
    fields = _field_meta_list_from_model(model_type)
    if any(field.type == "json" for field in fields):
        return InputMeta(
            kind="json",
            accepts_params=True,
            required=params_spec.required,
            fields=[],
        )
    return InputMeta(
        kind="fields",
        accepts_params=True,
        required=params_spec.required,
        fields=fields,
    )


def _build_custom_action_meta(action: CustomAction) -> CustomActionMeta:
    """`CustomAction` のフォームメタデータを返す。

    Args:
        action: UI へ公開する custom action 定義。

    Returns:
        custom action 1 件ぶんの入力メタデータ。
    """
    # ---カスタムアクションをlist[FieldMeta]にする
    fields = _field_meta_list_from_model(action.input_schema)
    if any(field.type == "json" for field in fields):
        # jsonなら、json入力へ
        input_meta = InputMeta(kind="json", accepts_params=True, required=True, fields=[])
    else:
        # それ以外は、fields入力へ
        input_meta = InputMeta(kind="fields", accepts_params=True, required=True, fields=fields)
    return CustomActionMeta(
        name=action.name,
        description=action.description,
        input=input_meta,
    )


def _field_meta_list_from_model(model_type: type[BaseModel]) -> list[FieldMeta]:
    """Pydantic モデルの全フィールドを UI 用メタデータへ変換する。

    Args:
        model_type: 変換対象の Pydantic モデル型。

    Returns:
        モデル定義順に並んだ field メタデータ一覧。
    """
    # Pydantic field の列挙・nullable 判定・default 解決は共通 helper 側へ寄せる。
    # ---InputFieldSpecをFieldMetaに変換して、listにする
    return [
        _field_meta_from_spec(spec)
        for spec in input_field_specs_from_model(model_type)
    ]


def _field_meta_from_spec(spec: InputFieldSpec) -> FieldMeta:
    """共通の入力フィールド仕様を HTTP 用 `FieldMeta` に変換する。

    Args:
        spec: adapter 共通の入力フィールド仕様。

    Returns:
        HTTP レスポンスへ載せる `FieldMeta`。
    """
    return FieldMeta(
        name=spec.name,
        label=spec.label,
        type=spec.primitive_type,
        required=spec.required,
        default=normalize_result(spec.default),
        nullable=spec.nullable,
    )


def _field_meta_from_annotation(
    *,
    name: str,
    annotation: Any,
    label: str,
    required: bool,
    default: Any,
) -> FieldMeta:
    """型注釈から 1 項目ぶんの `FieldMeta` を生成する。

    Args:
        name: フィールド名。
        annotation: 元の型注釈。
        label: UI 表示ラベル。
        required: 必須入力かどうか。
        default: 既定値。

    Returns:
        UI 生成に必要な 1 項目ぶんのメタデータ。
    """
    # target のように単独の型注釈しか無い入力も、共通 helper で分類して扱う。
    field_type: PrimitiveFieldType = resolve_primitive_input_type(annotation)
    return FieldMeta(
        name=name,
        label=label,
        type=field_type,
        required=required,
        default=normalize_result(default),
        nullable=_is_nullable_annotation(annotation),
    )


def _is_nullable_annotation(annotation: Any) -> bool:
    """型注釈が `None` を許容するかどうかを返す。

    Args:
        annotation: 判定対象の型注釈。

    Returns:
        `None` を許容する場合は `True`、それ以外は `False`。
    """
    _, nullable = unwrap_optional_type(annotation)
    return nullable


# -----------------------------------------------------------------------------
# status / check-step 用の小さな helper
# -----------------------------------------------------------------------------


def _try_get_manual_execution_status(
    manual_execution_manager: ExecutionManager,
) -> dict[str, Any] | None:
    """取得可能なら manual execution 状態を返す。

    Args:
        manual_execution_manager: 状態取得対象の manager。

    Returns:
        実行履歴があればその状態、未開始なら `None`。
    """
    try:
        return normalize_result(manual_execution_manager.get_status())
    except StateError:
        return None


def _try_get_sequence_snapshot(
    sequence_runner: SequenceRunner,
) -> dict[str, Any] | None:
    """取得可能なら sequence snapshot を返す。

    Args:
        sequence_runner: 状態取得対象の runner。

    Returns:
        実行履歴があればその snapshot、未開始なら `None`。
    """
    try:
        return normalize_result(sequence_runner.get_snapshot())
    except StateError:
        return None


def _get_enabled_step(
    definition: SequenceDefinition,
    step_index: int,
) -> Any:
    """`step_index` が指す有効ステップを返す。範囲チェック・有効チェックをする

    Args:
        definition: 対象のシーケンス定義。
        step_index: 取得したいステップ位置。

    Returns:
        指定位置の有効な `SequenceStep`。

    Raises:
        StateError: index が範囲外、または対象ステップが無効の場合。
    """
    if step_index < 0 or step_index >= len(definition.steps):
        raise StateError("Selected step index is out of range.")
    step = definition.steps[step_index]
    if not step.enabled:
        raise StateError("Selected step is disabled.")
    return step
