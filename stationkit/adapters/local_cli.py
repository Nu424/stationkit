"""同一プロセス内でコントローラ状態を保持するローカル CLI。

デバッグやテストのように 1 プロセス内で複数コマンドを呼びたい場合に使う。
通常の単発 CLI/CI では、service-backed な ``create_cli_app()`` を推奨する。
"""

from collections.abc import Awaitable, Callable

import typer
from pydantic import BaseModel, ValidationError

from stationkit.adapters._shared import format_cli_output, resolve_target_type
from stationkit.core import StationControllerBase, StationError


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

    # -------------------------------------------------------------------------
    # エラー表示の共通処理
    # -------------------------------------------------------------------------

    def handle_station_call(func):
        """StationError / ValidationError をメッセージと終了コードに変換する。

        Args:
            func: 引数なしで呼び出すコールバック。

        Returns:
            ``func()`` の戻り値。

        Raises:
            typer.Exit: エラー時にコード 1 または 2 で終了する。
        """
        try:
            return func()
        except ValidationError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2) from exc
        except StationError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc

    # -------------------------------------------------------------------------
    # 標準サブコマンド
    # -------------------------------------------------------------------------

    @app.command()
    def connect(address: str) -> None:
        """装置に接続する。"""
        handle_station_call(lambda: controller.connect(address))
        typer.echo("Connected.")

    @app.command()
    def disconnect() -> None:
        """装置から切断する。"""
        handle_station_call(controller.disconnect)
        typer.echo("Disconnected.")

    @app.command()
    def status() -> None:
        """状態を表示する。"""
        result = handle_station_call(controller.status)
        typer.echo(format_cli_output(result))

    @app.command()
    def change(target: target_type) -> None:
        """対象を切り替える。"""
        handle_station_call(lambda: controller.change(target))
        typer.echo(f"Changed to {target}.")

    @app.command()
    def execute() -> None:
        """メイン操作を実行し、結果を表示する。"""
        result = handle_station_call(controller.execute)
        typer.echo(format_cli_output(result))

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
            parsed = input_schema.model_validate_json(params_json)
            result = controller._run_sync(func(parsed))
        except ValidationError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2) from exc
        except StationError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(format_cli_output(result))

    command.__name__ = f"{action_name}_command"
    command.__doc__ = description
    return command
