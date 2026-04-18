"""stationkit 共通の例外階層。

装置やフレームワークから送出するエラーは、可能な限りこのモジュールの型に
マッピングすることを推奨します（HTTP/CLI で一括変換しやすくするため）。
"""


class StationError(Exception):
    """フレームワークおよび装置制御に関する例外の基底クラス。"""


class ConnectionError(StationError):
    """接続または切断に失敗したときに送出する。"""


class CommandError(StationError):
    """コマンド送信や応答処理に失敗したときに送出する。"""


class TimeoutError(StationError):
    """操作が許容時間内に完了しなかったときに送出する。"""


class StateError(StationError):
    """現在の状態では許可されない操作が呼ばれたときに送出する。

    例: 未接続のまま change を呼び出した場合など。
    """


class ExecutionCancelledError(StationError):
    """execute が安全に中断されたことを表す。"""
