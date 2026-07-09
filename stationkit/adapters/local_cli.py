"""同一プロセス内でコントローラ状態を保持するローカル CLI。

デバッグやテストのように 1 プロセス内で複数コマンドを呼びたい場合に使う。
通常の単発 CLI/CI では、service-backed な ``create_cli_app()`` を推奨する。
"""

from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any

import typer
from pydantic import BaseModel, ValidationError

from stationkit._logging import (
    get_adapter_logger,
    log_operation_failure,
    log_operation_start,
    log_operation_success,
)
from stationkit.adapters._shared import format_cli_output, resolve_target_type
from stationkit.core import StationControllerBase, StationError
from stationkit.core.introspection import ExecuteParamsSpec, resolve_execute_params_spec

LOGGER = get_adapter_logger("local_cli")


def create_local_cli_app(controller: StationControllerBase) -> typer.Typer:
    """コントローラ用のローカル Typer アプリを生成する。

    標準サブコマンドに加え、``get_custom_actions()`` の各操作を
    JSON 文字列引数のサブコマンドとして登録する。

    Args:
        controller: 装置実装のインスタンス。

    Returns:
        コマンド登録済みの :class:`typer.Typer`。
    """
    app = typer.Typer(name=type(controller).__name__)
    target_type = resolve_target_type(controller)
    execute_params_spec = resolve_execute_params_spec(controller)

    # -------------------------------------------------------------------------
    # エラー表示の共通処理
    # -------------------------------------------------------------------------

    def handle_station_call(
        func: Callable[[], Any],
        *,
        operation_name: str,
        log_context: dict[str, Any] | None = None,
    ) -> Any:
        """StationError / ValidationError をメッセージと終了コードに変換する。

        Args:
            func: 引数なしで呼び出すコールバック。

        Returns:
            ``func()`` の戻り値。

        Raises:
            typer.Exit: エラー時にコード 1 または 2 で終了する。
        """
        started = perf_counter()
        context = log_context or {}
        log_operation_start(
            LOGGER,
            layer="local_cli",
            operation_name=operation_name,
            controller_name=type(controller).__name__,
            context=context,
        )
        try:
            result = func()
        except ValidationError as exc:
            log_operation_failure(
                LOGGER,
                layer="local_cli",
                operation_name=operation_name,
                controller_name=type(controller).__name__,
                duration_ms=(perf_counter() - started) * 1000,
                context=context,
                exc=exc,
            )
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2) from exc
        except StationError as exc:
            log_operation_failure(
                LOGGER,
                layer="local_cli",
                operation_name=operation_name,
                controller_name=type(controller).__name__,
                duration_ms=(perf_counter() - started) * 1000,
                context=context,
                exc=exc,
            )
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
        log_operation_success(
            LOGGER,
            layer="local_cli",
            operation_name=operation_name,
            controller_name=type(controller).__name__,
            duration_ms=(perf_counter() - started) * 1000,
            context=context,
        )
        return result

    # -------------------------------------------------------------------------
    # 標準サブコマンド
    # -------------------------------------------------------------------------

    @app.command()
    def connect(address: str) -> None:
        """装置に接続する。"""
        handle_station_call(
            lambda: controller.connect(address),
            operation_name="connect",
            log_context={"address_provided": bool(address)},
        )
        typer.echo("Connected.")

    @app.command()
    def disconnect() -> None:
        """装置から切断する。"""
        handle_station_call(controller.disconnect, operation_name="disconnect")
        typer.echo("Disconnected.")

    @app.command()
    def idle() -> None:
        """装置を idle 状態へ移す。"""
        handle_station_call(controller.idle, operation_name="idle")
        typer.echo("Idled.")

    @app.command()
    def status() -> None:
        """状態を表示する。"""
        result = handle_station_call(controller.status, operation_name="status")
        typer.echo(format_cli_output(result))

    @app.command()
    def change(target: target_type) -> None:
        """対象を切り替える。"""
        handle_station_call(
            lambda: controller.change(target),
            operation_name="change",
            log_context={"target_type": type(target).__name__},
        )
        typer.echo(f"Changed to {target}.")

    app.command(name="execute")(_build_execute_command(controller, execute_params_spec))

    # -------------------------------------------------------------------------
    # CustomAction から動的にサブコマンドを追加
    # -------------------------------------------------------------------------

    for action in controller.get_custom_actions():
        app.command(name=action.name)(
            _build_action_command(
                controller=controller,
                input_schema=action.input_schema,
                func=action.func,
                action_name=action.name,
                description=action.description,
            )
        )

    return app


def _build_action_command(
    controller: StationControllerBase,
    input_schema: type[BaseModel],
    func: Callable[[BaseModel], Awaitable[object]],
    action_name: str,
    description: str,
) -> Callable[[str], None]:
    """CustomAction 用の CLI コールバックを生成する。

    Args:
        controller: 同期ラッパー ``_run_sync`` を呼ぶ対象。
        input_schema: JSON 文字列からパースする Pydantic モデル。
        func: 入力モデルを受け取る async 関数。
        action_name: コマンド関数名に使う識別子。
        description: ヘルプ用の説明（``__doc__`` に設定）。

    Returns:
        ``params_json: str`` を受け取る Typer コマンド関数。
    """

    def command(params_json: str) -> None:
        try:
            result = _handle_logged_local_call(
                controller=controller,
                operation_name=action_name,
                func=lambda: controller._run_sync(
                    func(input_schema.model_validate_json(params_json))
                ),
                log_context={"has_params": True},
            )
        except typer.Exit:
            raise
        typer.echo(format_cli_output(result))

    command.__name__ = f"{action_name}_command"
    command.__doc__ = description
    return command


def _build_execute_command(
    controller: StationControllerBase,
    params_spec: ExecuteParamsSpec,
) -> Callable[..., None]:
    """execute 用ローカル CLI コマンドを生成する。
    
    Args:
        controller: コントローラインスタンス。
        params_spec: execute 入力仕様。

    Returns:
        execute 用ローカル CLI コマンド。
    """
    # ---executeが、パラメータ不要の場合
    if not params_spec.accepts_params:
        # ---executeが、パラメータ不要の場合のコマンドを生成する
        def command() -> None:
            """メイン操作を実行し、結果を表示する。"""
            result = _handle_logged_local_call(
                # ---操作を実行する
                controller=controller,
                operation_name="execute",
                func=controller.execute,
                log_context={"has_params": False},
            )
            typer.echo(format_cli_output(result))

        command.__name__ = "execute_command"
        return command

    # ---executeが、パラメータ必須の場合
    if params_spec.required:
        # ---executeが、パラメータ必須の場合のコマンドを生成する
        def command(params_json: str) -> None:
            """メイン操作を実行し、結果を表示する。"""
            result = _handle_logged_local_call(
                controller=controller,
                operation_name="execute",
                func=lambda: controller.execute(
                    _parse_execute_params(params_spec, params_json)
                ),
                log_context={"has_params": True},
            )
            typer.echo(format_cli_output(result))
    else: # ---executeが、パラメータ任意の場合
        # ---executeが、パラメータ任意の場合のコマンドを生成する
        def command(params_json: str = "") -> None:
            """メイン操作を実行し、結果を表示する。"""
            result = _handle_logged_local_call(
                controller=controller,
                operation_name="execute",
                func=lambda: controller.execute(
                    _parse_execute_params(params_spec, params_json)
                ),
                log_context={"has_params": bool(params_json.strip())},
            )
            typer.echo(format_cli_output(result))

    command.__name__ = "execute_command"
    return command


def _handle_logged_local_call(
    *,
    controller: StationControllerBase,
    operation_name: str,
    func: Callable[[], Any],
    log_context: dict[str, Any] | None = None,
) -> Any:
    """ネストした helper からも使える local CLI 用ログ付き実行ラッパー。"""
    started = perf_counter()
    context = log_context or {}
    # ---操作開始ログを出力する
    log_operation_start(
        LOGGER,
        layer="local_cli",
        operation_name=operation_name,
        controller_name=type(controller).__name__,
        context=context,
    )
    try:
        # ---操作を実行する
        result = func()
    except ValidationError as exc:
        # ---失敗した場合、操作失敗ログを出力する
        log_operation_failure(
            LOGGER,
            layer="local_cli",
            operation_name=operation_name,
            controller_name=type(controller).__name__,
            duration_ms=(perf_counter() - started) * 1000,
            context=context,
            exc=exc,
        )
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    except StationError as exc:
        # ---失敗した場合、操作失敗ログを出力する
        log_operation_failure(
            LOGGER,
            layer="local_cli",
            operation_name=operation_name,
            controller_name=type(controller).__name__,
            duration_ms=(perf_counter() - started) * 1000,
            context=context,
            exc=exc,
        )
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    # ---成功した場合、操作成功ログを出力する
    log_operation_success(
        LOGGER,
        layer="local_cli",
        operation_name=operation_name,
        controller_name=type(controller).__name__,
        duration_ms=(perf_counter() - started) * 1000,
        context=context,
    )
    return result


def _parse_execute_params(
    params_spec: ExecuteParamsSpec,
    params_json: str,
) -> BaseModel | None:
    """execute の JSON 文字列を controller 入力に変換する。"""
    model_type = params_spec.model_type
    if model_type is None:
        return None

    text = params_json.strip()
    if not text:
        if params_spec.required:
            raise ValidationError.from_exception_data(
                title=model_type.__name__,
                line_errors=[
                    {
                        "type": "missing",
                        "loc": ("params",),
                        "msg": "Field required",
                        "input": None,
                    }
                ],
            )
        return None

    return model_type.model_validate_json(text)
