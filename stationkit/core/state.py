"""コントローラのライフサイクル状態を表す列挙型。"""

from enum import Enum, auto


class ControllerState(Enum):
    """装置コントローラの接続・実行に関する状態。

    Attributes:
        DISCONNECTED: 未接続。
        CONNECTED: 接続済みで操作可能。
        BUSY: 実行中（例: execute 処理中）。
        ERROR: 直前の操作でエラーが発生した状態。
    """

    DISCONNECTED = auto()
    CONNECTED = auto()
    BUSY = auto()
    ERROR = auto()
