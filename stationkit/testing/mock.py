"""テストおよびデモ用のインメモリコントローラ。"""

from __future__ import annotations

from typing import Any

from stationkit.core import StationControllerBase


class MockStationController(StationControllerBase):
    """実装置なしでフレームワークの動作を確認するモック。

    接続・切替・実行の呼び出しをログに残し、状態はメモリ上のみで保持する。
    """

    def __init__(self) -> None:
        """モックの内部状態を初期化する。"""
        super().__init__()
        self._current_target: int | None = None
        self._call_log: list[str] = []

    @property
    def call_log(self) -> list[str]:
        """これまでに呼ばれた操作のログ（コピー）を返す。

        Returns:
            呼び出し記録文字列のリスト。
        """
        return list(self._call_log)

    async def _do_connect(self, address: str) -> None:
        """接続をログに記録する（実際の I/O は行わない）。

        Args:
            address: 接続先識別子。
        """
        self._call_log.append(f"connect({address})")

    async def _do_disconnect(self) -> None:
        """切断をログに記録する。"""
        self._call_log.append("disconnect()")

    async def _do_change(self, target: int) -> None:
        """現在ターゲットを更新しログに記録する。

        Args:
            target: 新しいターゲット番号。
        """
        self._call_log.append(f"change({target})")
        self._current_target = target

    async def _do_execute(self) -> dict[str, Any]:
        """ダミー結果を返しログに記録する。

        Returns:
            ``mock`` フラグと現在ターゲットを含む dict。
        """
        self._call_log.append("execute()")
        return {"mock": True, "target": self._current_target}

    async def _do_status(self) -> dict[str, Any]:
        """モック固有の状態を返す。

        Returns:
            現在ターゲットと呼び出しログを含む dict。
        """
        return {
            "current_target": self._current_target,
            "call_log": self.call_log,
        }
