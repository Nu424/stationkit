"""ステーション制御の基底クラス。

実装者は ``_do_*`` の async メソッドのみをオーバーライドし、
公開 API は同期版と ``*_async`` 版の両方が利用できます。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any

from stationkit.core.action import CustomAction
from stationkit.core.exceptions import StateError
from stationkit.core.state import ControllerState


class StationControllerBase(ABC):
    """複数ステーション切替・実行パターンを統一する抽象コントローラ。

    状態ガードと sync/async の橋渡しは本クラス側に集約し、
    サブクラスは装置固有の ``_do_*`` 実装に集中できます。
    """

    # -------------------------------------------------------------------------
    # 初期化・状態
    # -------------------------------------------------------------------------

    def __init__(self) -> None:
        """コントローラを ``DISCONNECTED`` で初期化する。"""
        self._state = ControllerState.DISCONNECTED

    @property
    def state(self) -> ControllerState:
        """現在のコントローラ状態を返す。

        Returns:
            現在の :class:`ControllerState`。
        """
        return self._state

    # -------------------------------------------------------------------------
    # サブクラスが実装する内部 API（async）
    # -------------------------------------------------------------------------

    @abstractmethod
    async def _do_connect(self, address: str) -> None:
        """装置への接続を行う（実装者用）。

        Args:
            address: 接続先の識別子（COM ポート、ホスト名など装置依存）。
        """

    @abstractmethod
    async def _do_disconnect(self) -> None:
        """装置から切断する（実装者用）。"""

    @abstractmethod
    async def _do_change(self, target: Any) -> None:
        """対象ステーションまたはチャネルを切り替える（実装者用）。

        Args:
            target: 切替先。具体型はサブクラスで型ヒントを付けること
                （HTTP/CLI の引数型解決に使用される）。
        """

    @abstractmethod
    async def _do_execute(self) -> Any:
        """メイン操作（サンプリング等）を実行する（実装者用）。

        Returns:
            装置から得た結果。dict や Pydantic モデルなど任意。
        """

    @abstractmethod
    async def _do_status(self) -> dict[str, Any]:
        """装置固有の状態フィールドを dict で返す（実装者用）。

        Returns:
            ``controller_state`` 以外のキーを含めてよいステータス辞書。
        """

    # -------------------------------------------------------------------------
    # 公開 API（async）
    # -------------------------------------------------------------------------

    async def connect_async(self, address: str) -> None:
        """非同期で装置に接続する。

        Args:
            address: 接続先の識別子。

        Raises:
            StateError: 既に ``DISCONNECTED`` でない場合。
        """
        if self._state != ControllerState.DISCONNECTED:
            raise StateError(
                f"connect requires DISCONNECTED state, current: {self._state.name}"
            )
        await self._do_connect(address)
        self._state = ControllerState.CONNECTED

    async def disconnect_async(self) -> None:
        """非同期で装置から切断する。

        Raises:
            StateError: すでに ``DISCONNECTED`` の場合。
        """
        if self._state == ControllerState.DISCONNECTED:
            raise StateError("Already disconnected")
        await self._do_disconnect()
        self._state = ControllerState.DISCONNECTED

    async def change_async(self, target: Any) -> None:
        """非同期で対象を切り替える。

        Args:
            target: 切替先（型はサブクラスの ``_do_change`` に合わせる）。

        Raises:
            StateError: 未接続など、接続済みでない場合。
        """
        self._require_connected()
        await self._do_change(target)

    async def execute_async(self) -> Any:
        """非同期でメイン操作を実行する。

        実行中は状態が ``BUSY`` になり、成功時は ``CONNECTED`` に戻る。
        例外発生時は ``ERROR`` になる。

        Returns:
            ``_do_execute`` の戻り値。

        Raises:
            StateError: 未接続の場合。
            Exception: ``_do_execute`` 内で発生した例外（状態は ``ERROR``）。
        """
        self._require_connected()
        self._state = ControllerState.BUSY
        try:
            result = await self._do_execute()
        except Exception:
            self._state = ControllerState.ERROR
            raise
        self._state = ControllerState.CONNECTED
        return result

    async def status_async(self) -> dict[str, Any]:
        """非同期で状態を取得する。

        Returns:
            ``controller_state`` キーと ``_do_status`` の結果をマージした dict。
        """
        return {
            "controller_state": self._state.name,
            **(await self._do_status()),
        }

    # -------------------------------------------------------------------------
    # 公開 API（同期ラッパー）
    # -------------------------------------------------------------------------

    def connect(self, address: str) -> None:
        """同期で装置に接続する。

        イベントループ実行中に呼ぶと :class:`RuntimeError` になる。
        その場合は ``connect_async`` を使うこと。

        Args:
            address: 接続先の識別子。
        """
        self._run_sync(self.connect_async(address))

    def disconnect(self) -> None:
        """同期で装置から切断する。

        Raises:
            RuntimeError: イベントループ内から呼ばれた場合。
        """
        self._run_sync(self.disconnect_async())

    def change(self, target: Any) -> None:
        """同期で対象を切り替える。

        Args:
            target: 切替先。

        Raises:
            RuntimeError: イベントループ内から呼ばれた場合。
        """
        self._run_sync(self.change_async(target))

    def execute(self) -> Any:
        """同期でメイン操作を実行する。

        Returns:
            ``_do_execute`` の戻り値。

        Raises:
            RuntimeError: イベントループ内から呼ばれた場合。
        """
        return self._run_sync(self.execute_async())

    def status(self) -> dict[str, Any]:
        """同期で状態を取得する。

        Returns:
            ``status_async`` と同じ構造の dict。

        Raises:
            RuntimeError: イベントループ内から呼ばれた場合。
        """
        return self._run_sync(self.status_async())

    # -------------------------------------------------------------------------
    # 拡張: 固有操作
    # -------------------------------------------------------------------------

    def get_custom_actions(self) -> list[CustomAction]:
        """アダプタに公開する固有操作の一覧を返す。

        デフォルトは空。サブクラスで ``CustomAction`` を組み立てて返す。

        Returns:
            :class:`CustomAction` のリスト。
        """
        return []

    # -------------------------------------------------------------------------
    # 内部ユーティリティ
    # -------------------------------------------------------------------------

    def _require_connected(self) -> None:
        """接続済み（または実行中）であることを要求する。

        Raises:
            StateError: ``CONNECTED`` でも ``BUSY`` でもない場合。
        """
        if self._state not in (ControllerState.CONNECTED, ControllerState.BUSY):
            raise StateError(
                f"Operation requires CONNECTED state, current: {self._state.name}"
            )

    @staticmethod
    def _run_sync(coro: Any) -> Any:
        """コルーチンを同期的に完了させる。

        実行中のイベントループがない場合は ``asyncio.run`` を使用する。
        ループ内から呼ばれた場合は、未 await のコルーチンを閉じてから
        :class:`RuntimeError` を送出する。

        Args:
            coro: 実行するコルーチン。

        Returns:
            コルーチンの結果。

        Raises:
            RuntimeError: 既にイベントループが動いている場合。
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError as exc:
            if "no running event loop" in str(exc).lower():
                return asyncio.run(coro) # ループが無いなら、新しくasyncioループを作って実行する
            raise # ループがすでにある場合、二重に作ることはできないので、エラーを返す

        close = getattr(coro, "close", None)
        if callable(close):
            close()
        raise RuntimeError(
            "sync API cannot be called from inside an event loop. "
            "Use the corresponding *_async() method instead."
        )
