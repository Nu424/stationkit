"""service-backed な Typer CLI を組み立てるファクトリ。"""

import json
from collections.abc import Callable
from importlib import import_module
from time import perf_counter
from typing import Any

import httpx
import typer
from pydantic import BaseModel, ValidationError

from stationkit._logging import (
    get_adapter_logger,
    log_operation_failure,
    log_operation_start,
    log_operation_success,
)
from stationkit.adapters._shared import format_cli_output, normalize_result, resolve_target_type
from stationkit.adapters.http import create_http_app
from stationkit.core import StationControllerBase
from stationkit.core.introspection import ExecuteParamsSpec, resolve_execute_params_spec

DEFAULT_SERVER_URL = "http://127.0.0.1:8000"
LOGGER = get_adapter_logger("cli")


class ServiceClientError(Exception):
    """常駐 service との通信エラー。"""


def create_cli_app(controller: StationControllerBase) -> typer.Typer:
    """コントローラ用の service-backed CLI を生成する。

    ``serve`` サブコマンドで HTTP service を起動し、その他のコマンドは
    指定した server URL の service に対して HTTP リクエストを送る。

    Args:
        controller: service 起動時にバインドする装置実装インスタンス。

    Returns:
        コマンド登録済みの :class:`typer.Typer`。
    """
    app = typer.Typer(name=type(controller).__name__)
    target_type = resolve_target_type(controller)
    execute_params_spec = resolve_execute_params_spec(controller)

    @app.callback()
    def callback(
        ctx: typer.Context,
        server: str = typer.Option(
            DEFAULT_SERVER_URL,
            "--server",
            envvar="STATIONKIT_SERVER_URL",
            help="stationkit service の URL。",
        ),
    ) -> None:
        """ローカル service またはそのクライアントとして動作する。"""
        ctx.obj = {"server_url": _normalize_server_url(server)}

    # -------------------------------------------------------------------------
    # service 起動
    # -------------------------------------------------------------------------

    @app.command()
    def serve(
        host: str = typer.Option("127.0.0.1", help="待ち受けホスト。"),
        port: int = typer.Option(8000, min=1, max=65535, help="待ち受けポート。"),
    ) -> None:
        """HTTP service を起動する。"""
        try:
            uvicorn = import_module("uvicorn")
        except ImportError as exc:
            typer.secho(
                "uvicorn is required to run the stationkit service.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1) from exc

        uvicorn.run(create_http_app(controller), host=host, port=port)

    # -------------------------------------------------------------------------
    # 標準サブコマンド
    # -------------------------------------------------------------------------

    @app.command()
    def connect(ctx: typer.Context, address: str) -> None:
        """装置に接続する。"""
        _handle_service_call(
            lambda: _request_service(
                "POST",
                _get_server_url(ctx),
                "/connect",
                {"address": address},
            ),
            operation_name="connect",
            controller_name=type(controller).__name__,
            log_context={"path": "/connect", "address_provided": bool(address)},
        )
        typer.echo("Connected.")

    @app.command()
    def disconnect(ctx: typer.Context) -> None:
        """装置から切断する。"""
        _handle_service_call(
            lambda: _request_service("POST", _get_server_url(ctx), "/disconnect"),
            operation_name="disconnect",
            controller_name=type(controller).__name__,
            log_context={"path": "/disconnect"},
        )
        typer.echo("Disconnected.")

    @app.command()
    def status(ctx: typer.Context) -> None:
        """状態を表示する。"""
        result = _handle_service_call(
            lambda: _request_service("GET", _get_server_url(ctx), "/status"),
            operation_name="status",
            controller_name=type(controller).__name__,
            log_context={"path": "/status"},
        )
        typer.echo(format_cli_output(result))

    @app.command()
    def change(ctx: typer.Context, target: target_type) -> None:
        """対象を切り替える。"""
        _handle_service_call(
            lambda: _request_service(
                "POST",
                _get_server_url(ctx),
                "/change",
                {"target": target},
            ),
            operation_name="change",
            controller_name=type(controller).__name__,
            log_context={
                "path": "/change",
                "target_type": type(target).__name__,
            },
        )
        typer.echo(f"Changed to {target}.")

    app.command(name="execute")(
        _build_execute_command(
            params_spec=execute_params_spec,
            controller_name=type(controller).__name__,
        )
    )

    # -------------------------------------------------------------------------
    # CustomAction から動的にサブコマンドを追加
    # -------------------------------------------------------------------------

    for action in controller.get_custom_actions():
        app.command(name=action.name)(
            _build_action_command(
                controller_name=type(controller).__name__,
                input_schema=action.input_schema,
                action_name=action.name,
                description=action.description,
            )
        )

    return app


def _build_action_command(
    controller_name: str,
    input_schema: type[BaseModel],
    action_name: str,
    description: str,
) -> Callable[[typer.Context, str], None]:
    """CustomAction 用の CLI コールバックを生成する。

    Args:
        input_schema: JSON 文字列からパースする Pydantic モデル。
        action_name: コマンド関数名に使う識別子。
        description: ヘルプ用の説明（``__doc__`` に設定）。

    Returns:
        ``ctx`` と ``params_json`` を受け取る Typer コマンド関数。
    """

    def command(ctx: typer.Context, params_json: str) -> None:
        result = _handle_service_call(
            lambda: _request_service(
                "POST",
                _get_server_url(ctx),
                f"/actions/{action_name}",
                input_schema.model_validate_json(params_json).model_dump(),
            ),
            operation_name=action_name,
            controller_name=controller_name,
            log_context={"path": f"/actions/{action_name}", "has_params": True},
        )
        typer.echo(format_cli_output(result))

    command.__name__ = f"{action_name}_command"
    command.__doc__ = description
    return command


def _get_server_url(ctx: typer.Context) -> str:
    """Typer のコンテキストから server URL を取得する。"""
    return str(ctx.obj["server_url"])


def _handle_service_call(
    func: Callable[[], Any],
    *,
    operation_name: str,
    controller_name: str,
    log_context: dict[str, Any] | None = None,
) -> Any:
    """通信・入力検証エラーを CLI 向けに整形する。"""
    started = perf_counter()
    context = log_context or {}
    log_operation_start(
        LOGGER,
        layer="cli",
        operation_name=operation_name,
        controller_name=controller_name,
        context=context,
    )
    try:
        result = func()
    except ValidationError as exc:
        log_operation_failure(
            LOGGER,
            layer="cli",
            operation_name=operation_name,
            controller_name=controller_name,
            duration_ms=(perf_counter() - started) * 1000,
            context=context,
            exc=exc,
        )
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    except ServiceClientError as exc:
        log_operation_failure(
            LOGGER,
            layer="cli",
            operation_name=operation_name,
            controller_name=controller_name,
            duration_ms=(perf_counter() - started) * 1000,
            context=context,
            exc=exc,
        )
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    log_operation_success(
        LOGGER,
        layer="cli",
        operation_name=operation_name,
        controller_name=controller_name,
        duration_ms=(perf_counter() - started) * 1000,
        context=context,
    )
    return result


def _normalize_server_url(server_url: str) -> str:
    """末尾スラッシュを除いた server URL を返す。"""
    return server_url.rstrip("/")


def _request_service(
    method: str,
    server_url: str,
    path: str,
    payload: Any | None = None,
) -> Any:
    """service に HTTP リクエストを送り、JSON をパースして返す。"""
    url = f"{_normalize_server_url(server_url)}{path}"
    m = method.upper()

    try:
        with httpx.Client(timeout=30.0) as client:
            if payload is not None:
                response = client.request(
                    m,
                    url,
                    json=normalize_result(payload),
                )
            else:
                response = client.request(m, url)
    except httpx.ConnectError as exc:
        raise ServiceClientError(
            f"Could not reach stationkit service at {server_url}: {exc}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise ServiceClientError(f"Request timed out: {exc}") from exc
    except httpx.RequestError as exc:
        raise ServiceClientError(f"Request failed: {exc}") from exc

    if response.is_error:
        raise ServiceClientError(_format_error_response(response)) from None

    if not response.content:
        return None

    try:
        return response.json()
    except json.JSONDecodeError:
        return response.text


def _format_error_response(response: httpx.Response) -> str:
    """4xx/5xx レスポンスから人間向けメッセージを取り出す。"""
    if not response.content:
        return f"Service returned HTTP {response.status_code}."

    try:
        parsed = response.json()
    except json.JSONDecodeError:
        return response.text

    if isinstance(parsed, dict) and "detail" in parsed:
        return str(parsed["detail"])
    return response.text


def _build_execute_command(
    params_spec: ExecuteParamsSpec,
    controller_name: str,
) -> Callable[..., None]:
    """execute 用 CLI コマンドを入力仕様に応じて生成する。"""
    # ---executeが、パラメータ不要の場合
    if not params_spec.accepts_params:
        def command(ctx: typer.Context) -> None:
            """メイン操作を実行し、結果を表示する。"""
            result = _handle_service_call(
                lambda: _request_service("POST", _get_server_url(ctx), "/execute"),
                operation_name="execute",
                controller_name=controller_name,
                log_context={"path": "/execute", "has_params": False},
            )
            typer.echo(format_cli_output(result))

        command.__name__ = "execute_command"
        return command

    # ---executeが、パラメータ必須の場合
    if params_spec.required:
        def command(ctx: typer.Context, params_json: str) -> None:
            """メイン操作を実行し、結果を表示する。"""
            result = _handle_service_call(
                lambda: _request_service(
                    "POST",
                    _get_server_url(ctx),
                    "/execute",
                    _parse_execute_payload(params_spec, params_json),
                ),
                operation_name="execute",
                controller_name=controller_name,
                log_context={"path": "/execute", "has_params": True},
            )
            typer.echo(format_cli_output(result))
    else: # ---executeが、パラメータ任意の場合
        def command(ctx: typer.Context, params_json: str = "") -> None:
            """メイン操作を実行し、結果を表示する。"""
            result = _handle_service_call(
                lambda: _request_service(
                    "POST",
                    _get_server_url(ctx),
                    "/execute",
                    _parse_execute_payload(params_spec, params_json),
                ),
                operation_name="execute",
                controller_name=controller_name,
                log_context={
                    "path": "/execute",
                    "has_params": bool(params_json.strip()), # ここが違うだけ？
                },
            )
            typer.echo(format_cli_output(result))

    command.__name__ = "execute_command"
    return command


def _parse_execute_payload(
    params_spec: ExecuteParamsSpec,
    params_json: str,
) -> dict[str, Any] | None:
    """CLI の execute 入力 JSON を検証して payload 化する。"""
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

    # ---入力テキストをmodel_validate_json()して、Pydanticモデルに変換する
    return model_type.model_validate_json(text).model_dump()
