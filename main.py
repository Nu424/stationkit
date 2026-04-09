"""デモ用エントリポイント: モックコントローラにバインドした CLI。

本番では自前の ``StationControllerBase`` サブクラスを用意し、
同様に ``create_cli_app`` または ``create_http_app`` に渡す。
"""

from stationkit import MockStationController, create_cli_app

app = create_cli_app(MockStationController())


def main() -> None:
    """Typer アプリを起動する（``python -m`` やコンソールスクリプトから呼ばれる）。"""
    app()


if __name__ == "__main__":
    main()
