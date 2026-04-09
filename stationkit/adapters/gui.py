"""GUI アダプタ（未実装のプレースホルダ）。"""

from stationkit.core import StationControllerBase


def create_gui_app(_controller: StationControllerBase) -> None:
    """GUI アプリケーションを生成する（予定）。

    現状は未実装。将来 Tkinter や Web UI 等で提供する想定。

    Args:
        _controller: バインドするコントローラ（未使用）。

    Raises:
        NotImplementedError: 常に送出される。
    """
    raise NotImplementedError("GUI adapter is not implemented yet.")
