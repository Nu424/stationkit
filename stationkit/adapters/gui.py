"""Gradio ベースの GUI アダプタ。

単一の :class:`~stationkit.core.base.StationControllerBase` を全クライアントで共有し、
接続・切替・実行および :class:`~stationkit.core.action.CustomAction` を Gradio から操作する。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from threading import Lock
from time import perf_counter
from typing import Any

import gradio as gr
from pydantic import BaseModel, TypeAdapter

from stationkit._logging import (
    get_adapter_logger,
    log_operation_failure,
    log_operation_start,
    log_operation_success,
)
from stationkit.adapters._form_inputs import (
    input_field_specs_from_model,
    model_uses_scalar_fields,
    supports_scalar_input,
)
from stationkit.adapters._shared import normalize_result, resolve_target_type
from stationkit.core import CustomAction, StateError, StationControllerBase
from stationkit.core.introspection import (
    ExecuteParamsSpec,
    resolve_execute_params_spec,
    unwrap_optional_type,
)
from stationkit.execution import ExecutionManager, ExecutionState, ExecutionStatus

LOGGER = get_adapter_logger("gui")

# -----------------------------------------------------------------------------
# データ構造（UI バインディング）
# -----------------------------------------------------------------------------


@dataclass(slots=True)
class _FieldBinding:
    """CustomAction の入力フィールドと Gradio ウィジェットの対応。

    Attributes:
        name: 入力モデル上のフィールド名。
        annotation: フィールドの型注釈（Gradio コンポーネント選択と :func:`_coerce_value` に使用）。
        label: ウィジェットに表示するラベル。
        default: 初期表示用のデフォルト値。必須フィールドの場合は ``None``。
    """

    name: str
    annotation: Any
    label: str
    default: Any


# -----------------------------------------------------------------------------
# パブリック API
# -----------------------------------------------------------------------------


def create_gui_app(controller: StationControllerBase) -> gr.Blocks:
    """コントローラ用の Gradio GUI（:class:`gradio.Blocks`）を組み立てる。

    ``create_http_app()`` と同様、渡された **1 つの** ``controller`` を GUI 全体で共有する。
    状態の本体は controller 側にあり、各操作後に ``status_async()`` で最新表示を更新する。

    Args:
        controller: バインドするコントローラインスタンス。

    Returns:
        コンポーネントとイベントが登録済みの :class:`gradio.Blocks`。呼び出し側で ``launch()`` する。

    Note:
        共有 controller への同時アクセスを避けるため、操作は内部の ``threading.Lock`` で逐次化する。
    """
    target_type = resolve_target_type(controller)
    execute_params_spec = resolve_execute_params_spec(controller)
    operation_lock = Lock()
    execution_manager = ExecutionManager(controller)
    custom_actions = controller.get_custom_actions()
    execute_inputs: list[gr.Component] = []

    with gr.Blocks(title=f"{type(controller).__name__} GUI") as app:
        # --- ヘッダ・説明 ---
        gr.Markdown(f"# {type(controller).__name__} GUI")
        gr.Markdown(
            "A shared controller instance is bound to this GUI. "
            "All clients operate on the same device state."
        )

        # --- 接続・切断・ステータス更新 ---
        with gr.Row():
            address_input = gr.Textbox(
                label="Address",
                placeholder="COM3 / tcp://host / device-specific address",
            )
            connect_button = gr.Button("Connect", variant="primary")
            disconnect_button = gr.Button("Disconnect")
            refresh_button = gr.Button("Refresh Status")

        # --- コア操作（change / execute）---
        with gr.Group():
            gr.Markdown("## Core Operations")
            with gr.Row():
                target_component = _create_value_component(
                    target_type,
                    label="Target",
                    default=None,
                )
                change_button = gr.Button("Change")
                execute_button = gr.Button("Start Execute", variant="secondary")
                cancel_button = gr.Button("Cancel Execute")
            # ---executeの入力仕様に応じて、Gradioコンポーネントを生成する
            execute_inputs = _render_execute_inputs(execute_params_spec)

        # --- CustomAction セクション見出し ---
        if custom_actions:
            gr.Markdown("## Custom Actions")

        # --- 出力パネル（メッセージ・結果・状態）---
        message_output = gr.Textbox(label="Message", lines=3, interactive=False)
        result_output = gr.JSON(label="Result")
        status_output = gr.JSON(label="Status")

        # --- イベント登録：標準操作 ---
        connect_button.click(
            fn=_build_connect_handler(
                controller,
                operation_lock,
                execution_manager=execution_manager,
            ),
            inputs=[address_input],
            outputs=[message_output, result_output, status_output],
        )
        disconnect_button.click(
            fn=_build_disconnect_handler(
                controller,
                operation_lock,
                execution_manager=execution_manager,
            ),
            outputs=[message_output, result_output, status_output],
        )
        refresh_button.click(
            fn=_build_refresh_handler(
                controller,
                operation_lock,
                execution_manager=execution_manager,
            ),
            outputs=[message_output, result_output, status_output],
        )
        change_button.click(
            fn=_build_change_handler(
                controller,
                operation_lock,
                target_type,
                execution_manager=execution_manager,
            ),
            inputs=[target_component],
            outputs=[message_output, result_output, status_output],
        )
        execute_button.click(
            fn=_build_execute_handler(
                controller,
                operation_lock,
                execute_params_spec,
                execution_manager=execution_manager,
            ),
            inputs=execute_inputs,
            outputs=[message_output, result_output, status_output],
        )
        cancel_button.click(
            fn=_build_cancel_execute_handler(
                controller,
                operation_lock,
                execution_manager,
            ),
            outputs=[message_output, result_output, status_output],
        )

        # --- イベント登録：CustomAction ---
        for action in custom_actions:
            _render_custom_action(
                action=action,
                controller=controller,
                operation_lock=operation_lock,
                execution_manager=execution_manager,
                outputs=[message_output, result_output, status_output],
            )

        # --- 初期表示：ステータス読み込み ---
        app.load(
            fn=_build_refresh_handler(
                controller,
                operation_lock,
                execution_manager=execution_manager,
            ),
            outputs=[message_output, result_output, status_output],
        )

    return app


# -----------------------------------------------------------------------------
# CustomAction 用 UI 構築
# -----------------------------------------------------------------------------


def _render_custom_action(
    action: CustomAction,
    controller: StationControllerBase,
    operation_lock: Lock,
    execution_manager: ExecutionManager | None,
    outputs: list[gr.Component],
) -> None:
    """1 件の CustomAction について、アコーディオン内に入力 UI と実行ボタンを追加する。

    入力スキーマが単純スカラー型のフィールドのみならフィールドごとにウィジェットを生成し、
    それ以外は JSON テキスト 1 本で入力する。

    Args:
        action: 登録する固有操作のメタデータ。
        controller: 共有コントローラ。
        operation_lock: 操作の逐次化に使うロック。
        outputs: クリック時に更新する Gradio 出力コンポーネント（message / result / status）。
    """
    with gr.Accordion(f"{action.name}: {action.description}", open=False):
        # ---入力スキーマがGradio標準の単一ウィジェットで表現できる場合は、それを使用する
        if _input_schema_uses_field_widgets(action.input_schema):
            field_bindings = _build_field_bindings(action.input_schema)
            inputs = [
                _create_value_component(
                    field.annotation,
                    label=field.label,
                    default=field.default,
                )
                for field in field_bindings
            ]
            if not inputs:
                gr.Markdown("No parameters.")
            run_button = gr.Button(f"Run {action.name}")
            run_button.click(
                fn=_build_action_fields_handler(
                    controller=controller,
                    operation_lock=operation_lock,
                    action=action,
                    field_bindings=field_bindings,
                    execution_manager=execution_manager,
                ),
                inputs=inputs,
                outputs=outputs,
            )
            return

        # ---入力スキーマがGradio標準の単一ウィジェットで表現できない場合は、JSONテキスト1本で入力する
        params_input = gr.Textbox(
            label="Parameters (JSON)",
            lines=6,
            placeholder='{"field": "value"}',
        )
        run_button = gr.Button(f"Run {action.name}")
        run_button.click(
            fn=_build_action_json_handler(
                controller=controller,
                operation_lock=operation_lock,
                action=action,
                execution_manager=execution_manager,
            ),
            inputs=[params_input],
            outputs=outputs,
        )


def _render_execute_inputs(params_spec: ExecuteParamsSpec) -> list[gr.Component]:
    """execute 入力モデルに応じて GUI コンポーネントを生成する。"""
    # ---executeのパラメータがない場合は、空リストを返す
    if not params_spec.accepts_params:
        return []

    model_type = params_spec.model_type
    if model_type is None:
        raise TypeError("execute parameter spec is missing model type")

    # ---executeのパラメータがウィジェットで表現できる場合は、ウィジェットを生成する
    if _input_schema_uses_field_widgets(model_type):
        bindings = _build_field_bindings(model_type)
        if not bindings:
            return []
        with gr.Row():
            # ---_FieldBindingをもとに、ウィジェットを生成する
            return [
                _create_value_component(
                    field.annotation,
                    label=field.label,
                    default=field.default,
                )
                for field in bindings
            ]

    # ---executeのパラメータがウィジェットで表現できない場合は、JSONテキスト1本で入力する
    params_input = gr.Textbox(
        label="Execute Parameters (JSON)",
        lines=6,
        placeholder='{"field": "value"}',
    )
    return [params_input]


# -----------------------------------------------------------------------------
# Gradio イベントハンドラの生成（標準操作）
# -----------------------------------------------------------------------------


def _build_connect_handler(
    controller: StationControllerBase,
    operation_lock: Lock,
    execution_manager: ExecutionManager | None = None,
) -> Callable[[str], Awaitable[tuple[str, Any, dict[str, Any]]]]:
    """「接続」ボタン用の非同期ハンドラを返す。

    Args:
        controller: 共有コントローラ。
        operation_lock: 操作の逐次化に使うロック。

    Returns:
        ``address: str`` を受け取り、``(message, result, status)`` のタプルを返すコルーチン。
        ``result`` は接続操作では常に ``None``。
    """

    async def handle_connect(address: str) -> tuple[str, Any, dict[str, Any]]:
        async def connect() -> None:
            await controller.connect_async(address)

        return await _run_controller_action(
            controller=controller,
            operation_lock=operation_lock,
            operation=connect,
            operation_name="connect",
            success_message="Connected.",
            log_context={"address_provided": bool(address)},
            execution_manager=execution_manager,
        )

    return handle_connect


def _build_disconnect_handler(
    controller: StationControllerBase,
    operation_lock: Lock,
    execution_manager: ExecutionManager | None = None,
) -> Callable[[], Awaitable[tuple[str, Any, dict[str, Any]]]]:
    """「切断」ボタン用の非同期ハンドラを返す。

    Args:
        controller: 共有コントローラ。
        operation_lock: 操作の逐次化に使うロック。

    Returns:
        引数なしで ``(message, result, status)`` を返すコルーチン。
        ``result`` は切断操作では常に ``None``。
    """

    async def handle_disconnect() -> tuple[str, Any, dict[str, Any]]:
        return await _run_controller_action(
            controller=controller,
            operation_lock=operation_lock,
            operation=controller.disconnect_async,
            operation_name="disconnect",
            success_message="Disconnected.",
            execution_manager=execution_manager,
        )

    return handle_disconnect


def _build_refresh_handler(
    controller: StationControllerBase,
    operation_lock: Lock,
    execution_manager: ExecutionManager | None = None,
) -> Callable[[], Awaitable[tuple[str, Any, dict[str, Any]]]]:
    """「ステータス更新」および初期 ``load`` 用の非同期ハンドラを返す。

    controller への副作用はなく、最新の ``status_async()`` の結果だけを表示する。

    Args:
        controller: 共有コントローラ。
        operation_lock: 操作の逐次化に使うロック。

    Returns:
        引数なしで ``(message, result, status)`` を返すコルーチン。
        ``result`` は常に ``None``。
    """

    async def handle_refresh() -> tuple[str, Any, dict[str, Any]]:
        return await _run_controller_action(
            controller=controller,
            operation_lock=operation_lock,
            operation=_noop_async,
            operation_name="status",
            success_message="Status refreshed.",
            execution_manager=execution_manager,
            allow_when_running=True,
            include_execution_result=True,
        )

    return handle_refresh


def _build_change_handler(
    controller: StationControllerBase,
    operation_lock: Lock,
    target_type: Any,
    execution_manager: ExecutionManager | None = None,
) -> Callable[[Any], Awaitable[tuple[str, Any, dict[str, Any]]]]:
    """「切替」ボタン用の非同期ハンドラを返す。

    Args:
        controller: 共有コントローラ。
        operation_lock: 操作の逐次化に使うロック。
        target_type: ``resolve_target_type`` で得た ``change`` の引数型。

    Returns:
        生のウィジェット値を受け取り、:func:`_coerce_value` で変換してから
        ``change_async`` を呼び、``(message, result, status)`` を返すコルーチン。
        ``result`` は切替操作では常に ``None``。
    """

    async def handle_change(raw_target: Any) -> tuple[str, Any, dict[str, Any]]:
        async def change() -> None:
            target = _coerce_value(raw_target, target_type)
            await controller.change_async(target)

        return await _run_controller_action(
            controller=controller,
            operation_lock=operation_lock,
            operation=change,
            operation_name="change",
            success_message="Target changed.",
            log_context={"target_type": target_type.__name__},
            execution_manager=execution_manager,
        )

    return handle_change


def _build_execute_handler(
    controller: StationControllerBase,
    operation_lock: Lock,
    params_spec: ExecuteParamsSpec,
    execution_manager: ExecutionManager | None = None,
) -> Callable[..., Awaitable[tuple[str, Any, dict[str, Any]]]]:
    """「実行開始」ボタン用の非同期ハンドラを返す。

    Args:
        controller: 共有コントローラ。
        operation_lock: 操作の逐次化に使うロック。

    Returns:
        execute 入力 UI から得た値を受け取り、開始結果と最新 ``status`` を返す
        コルーチン。``execution_manager`` がある場合は非ブロッキングに開始する。
    """
    async def handle_execute(*raw_values: Any) -> tuple[str, Any, dict[str, Any]]:
        # ---executeの入力仕様に応じて、値をパースする
        params = _parse_execute_input(params_spec, raw_values)
        # ---execution_managerがNoneの場合、直接controller.execute_async()を実行する(従来通り)
        if execution_manager is None:
            async def execute() -> Any:
                return await controller.execute_async(params)

            return await _run_controller_action(
                controller=controller,
                operation_lock=operation_lock,
                operation=execute,
                operation_name="execute",
                success_message="Execution completed.",
                include_result=True,
                log_context={"input_arity": len(raw_values)},
            )

        # ---execution_managerが存在する場合、execution_managerを使ってexecuteを非ブロッキングに開始する
        return await _run_manager_execute_action(
            controller=controller,
            operation_lock=operation_lock,
            execution_manager=execution_manager,
            params=params,
            log_context={"input_arity": len(raw_values)},
        )

    return handle_execute


def _build_cancel_execute_handler(
    controller: StationControllerBase,
    operation_lock: Lock,
    execution_manager: ExecutionManager,
) -> Callable[[], Awaitable[tuple[str, Any, dict[str, Any]]]]:
    """「実行キャンセル」ボタン用ハンドラを返す。"""

    async def handle_cancel() -> tuple[str, Any, dict[str, Any]]:
        return await _run_controller_action(
            controller=controller,
            operation_lock=operation_lock,
            operation=lambda: asyncio.to_thread(execution_manager.cancel),
            operation_name="execute_cancel",
            success_message="Cancellation requested.",
            execution_manager=execution_manager,
            allow_when_running=True,
        )

    return handle_cancel


# -----------------------------------------------------------------------------
# Gradio イベントハンドラの生成（CustomAction）
# -----------------------------------------------------------------------------


def _build_action_fields_handler(
    controller: StationControllerBase,
    operation_lock: Lock,
    action: CustomAction,
    field_bindings: list[_FieldBinding],
    execution_manager: ExecutionManager | None = None,
) -> Callable[..., Awaitable[tuple[str, Any, dict[str, Any]]]]:
    """フィールド分割ウィジェットから CustomAction を実行するハンドラを返す。

    Args:
        controller: 共有コントローラ（ロック共有のため渡す。action 自体は controller 非依存でも可）。
        operation_lock: 操作の逐次化に使うロック。
        action: 実行する CustomAction。
        field_bindings: ウィジェット順とフィールドの対応。

    Returns:
        ウィジェット値を可変長で受け取り、:func:`_parse_action_fields` 後に ``action.func`` を
        await し、``(message, result, status)`` を返すコルーチン。
    """

    async def handle_action(*raw_values: Any) -> tuple[str, Any, dict[str, Any]]:
        # ---共通のハンドラで実行する用のasync関数を定義する
        async def run_action() -> Any:
            # ---ウィジェットから得た値(raw_values)を、入力スキーマに合わせて検証・変換する
            params = _parse_action_fields(
                input_schema=action.input_schema,
                field_bindings=field_bindings,
                raw_values=raw_values,
            )
            return await action.func(params)

        # ---実行は共通のハンドラで
        return await _run_controller_action(
            controller=controller,
            operation_lock=operation_lock,
            operation=run_action,
            operation_name=action.name,
            success_message=f"Action '{action.name}' completed.",
            include_result=True,
            log_context={"has_params": True},
            execution_manager=execution_manager,
        )

    return handle_action


def _build_action_json_handler(
    controller: StationControllerBase,
    operation_lock: Lock,
    action: CustomAction,
    execution_manager: ExecutionManager | None = None,
) -> Callable[[str], Awaitable[tuple[str, Any, dict[str, Any]]]]:
    """JSON テキストから CustomAction を実行するハンドラを返す。

    Args:
        controller: 共有コントローラ（ロック共有のため渡す）。
        operation_lock: 操作の逐次化に使うロック。
        action: 実行する CustomAction。

    Returns:
        JSON 文字列を受け取り、:func:`_parse_action_json` 後に ``action.func`` を await し、
        ``(message, result, status)`` を返すコルーチン。
    """

    async def handle_action(params_json: str) -> tuple[str, Any, dict[str, Any]]:
        async def run_action() -> Any:
            params = _parse_action_json(action.input_schema, params_json)
            return await action.func(params)

        return await _run_controller_action(
            controller=controller,
            operation_lock=operation_lock,
            operation=run_action,
            operation_name=action.name,
            success_message=f"Action '{action.name}' completed.",
            include_result=True,
            log_context={"has_params": True},
            execution_manager=execution_manager,
        )

    return handle_action


# -----------------------------------------------------------------------------
# 共有 controller 上の 1 操作の実行と排他
# -----------------------------------------------------------------------------


async def _run_controller_action(
    controller: StationControllerBase,
    operation_lock: Lock,
    operation: Callable[[], Awaitable[Any]],
    operation_name: str,
    success_message: str,
    include_result: bool = False,
    include_execution_result: bool = False,
    log_context: dict[str, Any] | None = None,
    execution_manager: ExecutionManager | None = None,
    allow_when_running: bool = False,
) -> tuple[str, Any, dict[str, Any]]:
    """ロック下で ``operation`` を 1 回実行し、メッセージ・結果・状態をまとめて返す。

    成功時は ``success_message`` と、必要なら ``normalize_result`` 済みの結果、
    および ``status_async()`` 相当の dict を返す。失敗時はエラー文字列と ``result=None``、
    可能なら取得した状態 dict を返す。

    Args:
        controller: 状態取得に使う共有コントローラ。
        operation_lock: 逐次化用の ``threading.Lock``。
        operation: 引数なしの async 操作（例: ``controller.disconnect_async``）。
        success_message: 成功時に UI に表示するメッセージ。
        include_result: True のとき ``operation`` の戻り値を ``normalize_result`` して返す。
        include_execution_result: True のとき execution status から表示用 result を補う。

    Returns:
        ``(message, result, status)``。``result`` は ``include_result`` が False のとき ``None``。
    """
    context = log_context or {}
    started = perf_counter()
    log_operation_start(
        LOGGER,
        layer="gui",
        operation_name=operation_name,
        controller_name=type(controller).__name__,
        context=context,
    )
    async with _hold_lock(operation_lock):
        try:
            # ---execution_managerが存在する場合、execution_managerの状況を確認する
            if (
                execution_manager is not None
                and not allow_when_running
                and execution_manager.has_active_execution()
            ):
                raise StateError("Execution is already running.")
            result = await operation()
            status = await _safe_status(controller, execution_manager)
        except Exception as exc:
            status = await _safe_status(controller, execution_manager)
            log_operation_failure(
                LOGGER,
                layer="gui",
                operation_name=operation_name,
                controller_name=type(controller).__name__,
                duration_ms=(perf_counter() - started) * 1000,
                context=context,
                exc=exc,
            )
            return f"Error: {exc}", None, status

    log_operation_success(
        LOGGER,
        layer="gui",
        operation_name=operation_name,
        controller_name=type(controller).__name__,
        duration_ms=(perf_counter() - started) * 1000,
        context=context,
    )
    display_result = None
    if include_result: # controller側の実行結果を、display_resultに格納する
        display_result = normalize_result(result)
    elif include_execution_result: # execution_manager側の実行結果を、display_resultに格納する
        display_result = _extract_execution_result(status)
    return success_message, display_result, status


async def _run_manager_execute_action(
    controller: StationControllerBase,
    operation_lock: Lock,
    execution_manager: ExecutionManager,
    params: Any | None,
    log_context: dict[str, Any] | None = None,
) -> tuple[str, Any, dict[str, Any]]:
    """manager を使って execute を開始し、即座に状態を返す。"""
    context = log_context or {}
    started = perf_counter()
    log_operation_start(
        LOGGER,
        layer="gui",
        operation_name="execute_start",
        controller_name=type(controller).__name__,
        context=context,
    )
    try:
        async with _hold_lock(operation_lock):
            if execution_manager.has_active_execution():
                raise StateError("Execution is already running.")
            # ---execution_manager.start()で、executeを開始する
            handle = await asyncio.to_thread(execution_manager.start, params)

        status = await _safe_status(controller, execution_manager)
        log_operation_success(
            LOGGER,
            layer="gui",
            operation_name="execute_start",
            controller_name=type(controller).__name__,
            duration_ms=(perf_counter() - started) * 1000,
            context={**context, "execution_id": handle.execution_id},
        )
        return (
            f"Execution started: {handle.execution_id}",
            None,
            status,
        )
    except Exception as exc:
        status = await _safe_status(controller, execution_manager)
        log_operation_failure(
            LOGGER,
            layer="gui",
            operation_name="execute_start",
            controller_name=type(controller).__name__,
            duration_ms=(perf_counter() - started) * 1000,
            context=context,
            exc=exc,
        )
        return f"Error: {exc}", None, status


# -----------------------------------------------------------------------------
# CustomAction 入力スキーマの解釈
# -----------------------------------------------------------------------------


def _input_schema_uses_field_widgets(input_schema: type[BaseModel]) -> bool:
    """入力スキーマの全フィールドが「単一ウィジェット対応型」かどうかを返す。

    Args:
        input_schema: CustomAction の ``input_schema``。

    Returns:
        すべてのフィールドが :func:`_supports_native_component` を満たすとき True。
    """
    # Optional[bool] は Checkbox で `None` を表現しづらいため GUI 側では除外する。
    return model_uses_scalar_fields(
        input_schema,
        allow_optional_bool=False,
    )


def _build_field_bindings(input_schema: type[BaseModel]) -> list[_FieldBinding]:
    """Pydantic モデルの各フィールドから :class:`_FieldBinding` のリストを作る。
    Pydanticスキーマをforで解析して、_FieldBindingの構造に変換してるだけ。

    Args:
        input_schema: CustomAction の ``input_schema``。

    Returns:
        ``model_fields`` の定義順に並べたバインディング一覧。
    """
    # Pydantic field の列挙や default 解決は共通 helper 側へ寄せる。
    # ---InputFieldSpecを_FieldBindingに変換して、listにする
    bindings: list[_FieldBinding] = []
    for spec in input_field_specs_from_model(input_schema):
        bindings.append(
            _FieldBinding(
                name=spec.name,
                annotation=spec.annotation,
                label=spec.label,
                default=spec.default,
            )
        )
    return bindings


def _parse_action_fields(
    input_schema: type[BaseModel],
    field_bindings: list[_FieldBinding],
    raw_values: tuple[Any, ...],
) -> BaseModel:
    """ウィジェットから得た値をフィールド名付き dict にし、入力モデルを検証する。

    Args:
        input_schema: 検証に使う Pydantic モデル。
        field_bindings: フィールドとウィジェット順の対応。
        raw_values: Gradio から渡る値のタプル（``field_bindings`` と同じ長さであること）。

    Returns:
        ``input_schema`` の検証済みインスタンス。

    Raises:
        ValueError: :func:`_coerce_value` 内で JSON 解析などに失敗した場合。
        pydantic.ValidationError: モデル検証に失敗した場合。
    """
    payload = {
        binding.name: _coerce_value(raw_value, binding.annotation) # 生の値を型に合わせて検証・変換する
        for binding, raw_value in zip(field_bindings, raw_values, strict=True)
    }
    return input_schema.model_validate(payload)


def _parse_action_json(
    input_schema: type[BaseModel],
    params_json: str,
) -> BaseModel:
    """JSON 文字列をパースして入力モデルを検証する。

    空文字（空白のみ）のときは空オブジェクト ``{}`` として扱う。

    Args:
        input_schema: 検証に使う Pydantic モデル。
        params_json: ユーザーが入力した JSON テキスト。

    Returns:
        ``input_schema`` の検証済みインスタンス。

    Raises:
        ValueError: JSON の構文が不正な場合。
        pydantic.ValidationError: モデル検証に失敗した場合。
    """
    text = params_json.strip()
    if not text:
        payload: Any = {}
    else:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON input: {exc}") from exc
    return input_schema.model_validate(payload)


def _parse_execute_input(
    params_spec: ExecuteParamsSpec,
    raw_values: tuple[Any, ...],
) -> BaseModel | None:
    """execute 入力ウィジェットの値を controller に渡す値へ変換する。
    
    Args:
        params_spec: execute 入力仕様。
        raw_values: Gradio から渡る値のタプル（``field_bindings`` と同じ長さであること）。

    Returns:
        ``input_schema`` の検証済みインスタンス。
    """
    if not params_spec.accepts_params:
        return None

    model_type = params_spec.model_type
    if model_type is None:
        raise TypeError("execute parameter spec is missing model type")

    # ---入力のすべてがウィジェットで表現できている場合は、入力フィールドの生値を、Pydanticモデルに変換する
    if _input_schema_uses_field_widgets(model_type):
        field_bindings = _build_field_bindings(model_type)
        if not field_bindings:
            return model_type.model_validate({})
        if not params_spec.required and _all_execute_values_empty(raw_values):
            return None
        # 入力の型(model_type)と、型と入力フィールドの紐づけ(field_bindings)をもとに、フィールドの値(raw_values)をPydanticモデルに変換する
        # フィールドの値だけでは、型にそったデータ構造を復元できない。
        # なので、field_bindingsでひもづけを復元し、model_typeで改めてmodel_validate()する
        return _parse_action_fields(model_type, field_bindings, raw_values)

    # ---入力のすべてがウィジェットで表現できない場合は、JSONテキスト1本で入力する
    params_json = str(raw_values[0]) if raw_values else ""
    if not params_json.strip() and not params_spec.required:
        return None
    return _parse_action_json(model_type, params_json)


def _all_execute_values_empty(raw_values: tuple[Any, ...]) -> bool:
    """すべての execute 入力が未入力相当かどうかを返す。"""
    return all(value in (None, "") for value in raw_values)


# -----------------------------------------------------------------------------
# 型に応じた Gradio コンポーネントと値の変換
# -----------------------------------------------------------------------------


def _create_value_component(
    value_type: Any,
    label: str,
    default: Any,
) -> gr.Component:
    """型に応じて Checkbox / Number / Textbox（または JSON 用 Textbox）を生成する。

    Args:
        value_type: 対象の Python 型（``Optional`` は :func:`unwrap_optional_type` で解釈）。
        label: コンポーネントのラベル。
        default: 初期値。未設定のときは型に応じて空欄や False を使う。

    Returns:
        対応する Gradio 入力コンポーネント。
    """
    base_type, is_optional = unwrap_optional_type(value_type)

    # ---各種型に合わせて、対応するGradioコンポーネントを生成する
    if base_type is bool and not is_optional: # boolはT/Fしか持てず、Noneを表現できないので、not is_optionalにしている
        return gr.Checkbox(label=label, value=bool(default) if default is not None else False)

    if base_type is int:
        value = None if default is None else int(default)
        return gr.Number(label=label, value=value, precision=0)

    if base_type is float:
        value = None if default is None else float(default)
        return gr.Number(label=label, value=value)

    if base_type is str:
        value = "" if default is None else str(default)
        return gr.Textbox(label=label, value=value)

    # ---どれでもない場合は、複雑型用のTextboxを生成する
    placeholder = _json_placeholder(base_type)
    return gr.Textbox(
        label=f"{label} (JSON)",
        lines=6,
        value=_json_default(default),
        placeholder=placeholder,
    )


def _coerce_value(raw_value: Any, value_type: Any) -> Any:
    """Gradio から渡された生の値を ``value_type`` に合わせて検証・変換する。

    単一ウィジェット型は :class:`pydantic.TypeAdapter` でそのまま検証する。
    複雑型で文字列が来た場合は JSON としてパースしてから検証する。

    Args:
        raw_value: ウィジェットの値。
        value_type: 期待する型（``Optional`` 可）。

    Returns:
        ``TypeAdapter(value_type).validate_python(...)`` の結果。

    Raises:
        ValueError: JSON として解釈すべき文字列が不正な場合（メッセージ内に元の型を含む）。
        pydantic.ValidationError: 型検証に失敗した場合。
    """
    # ---TypeAdapterは、オブジェクトが特定の型に合致するかを検証するやつ
    adapter = TypeAdapter(value_type)
    # ---値の型がGradio標準の単一ウィジェットで表現できる型の場合は、そのままvalidateする
    if _supports_native_component(value_type):
        return adapter.validate_python(raw_value)

    # ---表現できず、値の型がstr型でもない場合は、そのままTypeAdapterでvalidateする
    if not isinstance(raw_value, str):
        return adapter.validate_python(raw_value)

    # ---表現できないけどstr型の場合は、JSONとしてパースする
    text = raw_value.strip()
    if not text:
        return adapter.validate_python(None)

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Expected JSON input for {value_type!r}: {exc}") from exc
    return adapter.validate_python(payload)


def _supports_native_component(value_type: Any) -> bool:
    """``str`` / ``int`` / ``float`` / 非 Optional ``bool`` をGradio標準の単一ウィジェットで表現できるか。

    Args:
        value_type: 判定する型注釈。

    Returns:
        上記に該当し、かつ ``Optional[bool]`` でないとき True。
    """
    return supports_scalar_input(value_type, allow_optional_bool=False)


def _json_default(default: Any) -> str:
    """複雑型用 Textbox の初期文字列を JSON で返す。

    Args:
        default: デフォルト値。``None`` なら空文字。

    Returns:
        ``normalize_result`` 後に ``json.dumps`` した UTF-8 文字列。
    """
    if default is None:
        return ""
    return json.dumps(normalize_result(default), ensure_ascii=False, indent=2)


def _json_placeholder(value_type: Any) -> str:
    """複雑型用 Textbox のプレースホルダ文字列を返す。

    Args:
        value_type: 入力対象型。``BaseModel`` サブクラスならスキーマの title を利用する。

    Returns:
        プレースホルダ用の短い文字列。
    """
    if isinstance(value_type, type) and issubclass(value_type, BaseModel):
        return value_type.model_json_schema().get("title", "{}")
    return "Enter JSON here"


# -----------------------------------------------------------------------------
# 状態取得とロック（逐次化）
# -----------------------------------------------------------------------------


async def _safe_status(
    controller: StationControllerBase,
    execution_manager: ExecutionManager | None = None,
) -> dict[str, Any]:
    """``status_async()`` を呼び、失敗時も最低限の dict を返す。

    Args:
        controller: 状態を問い合わせるコントローラ。

    Returns:
        成功時は ``status_async()`` の戻り値。失敗時は ``controller_state`` と
        ``status_error`` キーを含む dict。
    """
    try:
        status = await controller.status_async()
    except Exception as exc:
        status = {
            "controller_state": controller.state.name,
            "status_error": str(exc),
        }
    execution_status = _try_get_execution_status(execution_manager)
    if execution_status is not None:
        status["execution"] = normalize_result(execution_status)
    return status


def _extract_execution_result(status: dict[str, Any]) -> Any:
    """status dict から表示用の execution result を取り出す。"""
    execution = status.get("execution")
    if not isinstance(execution, dict):
        return None
    if execution.get("state") != ExecutionState.SUCCEEDED:
        return None
    return execution.get("result")


def _try_get_execution_status(
    execution_manager: ExecutionManager | None,
) -> ExecutionStatus | None:
    """取得可能なら execution status を返す。"""
    if execution_manager is None:
        return None
    try:
        return execution_manager.get_status()
    except StateError:
        return None


async def _noop_async() -> None:
    """何もしない async コルーチン。

    ステータス更新のみ行う経路で ``operation`` 引数を満たすために使う。
    """
    return None


@asynccontextmanager
async def _hold_lock(operation_lock: Lock):
    """``threading.Lock`` をイベントループをブロックせずに取得・解放するコンテキスト。

    ``asyncio.to_thread`` でロック取得を別スレッドに逃がし、UI スレッドのブロックを避ける。

    Args:
        operation_lock: 共有する ``threading.Lock``。

    Yields:
        ロック取得済みの区間。

    Note:
        解放は ``finally`` で必ず行う。
    """
    await asyncio.to_thread(operation_lock.acquire)
    try:
        yield
    finally:
        operation_lock.release()
