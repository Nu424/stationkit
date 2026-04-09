"""stationkit: ステーション制御フレームワークのパッケージルート。

主要シンボルはここからインポートできる（例: ``StationControllerBase``,
``create_http_app``, ``create_cli_app``）。
"""

from stationkit.adapters import create_cli_app, create_gui_app, create_http_app
from stationkit.core import (
    CommandError,
    ConnectionError,
    ControllerState,
    CustomAction,
    StateError,
    StationControllerBase,
    StationError,
    TimeoutError,
)
from stationkit.testing import MockStationController

__all__ = [
    "CommandError",
    "ConnectionError",
    "ControllerState",
    "create_cli_app",
    "create_gui_app",
    "create_http_app",
    "CustomAction",
    "MockStationController",
    "StateError",
    "StationControllerBase",
    "StationError",
    "TimeoutError",
]
