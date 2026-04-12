"""デモ用エントリポイント: モックコントローラにバインドした CLI。

この CLI は ``serve`` で常駐 service を起動し、他のサブコマンドは
その service に HTTP 経由でアクセスする。
"""

from stationkit import MockStationController, create_cli_app

app = create_cli_app(MockStationController())


def main() -> None:
    """Typer アプリを起動する（``python -m`` やコンソールスクリプトから呼ばれる）。"""
    app()


if __name__ == "__main__":
    main()
