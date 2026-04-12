"""デモ用エントリポイント: モックコントローラにバインドした CLI。

この CLI は ``serve`` で常駐 service を起動し、他のサブコマンドは
その service に HTTP 経由でアクセスする。
"""

from stationkit import MockStationController
# from stationkit import create_cli_app
# from stationkit import create_http_app
from stationkit import create_gui_app

controller = MockStationController()

# cli_app = create_cli_app(controller)
# http_app = create_http_app(controller)
gui_app = create_gui_app(controller)


if __name__ == "__main__":
    # cli_app()
    # http_app.run()
    gui_app.launch()
