"""ステーション制御の基底クラス。

実装者は ``_do_*`` の async メソッドのみをオーバーライドし、
公開 API は同期版と ``*_async`` 版の両方が利用できます。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from stationkit._logging import (
    get_controller_logger,
    log_operation_failure,
    log_operation_start,
    log_operation_success,
)
from stationkit.core.action import CustomAction
from stationkit.core.exceptions import StateError
from stationkit.core.execution_context import ExecutionContext
from stationkit.core.introspection import (
    normalize_execute_params,
    resolve_execute_params_spec,
)
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
        self._logger = get_controller_logger(type(self).__name__)

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

        サブクラスは後方互換のため無引数のまま実装してよい。必要なら
        ``_do_execute(self, params: MyParams)`` または
        ``_do_execute(self, params: MyParams | None = None)`` のように
        Pydantic モデル 1 個を追加で受け取れる。

        実行コンテキストが必要な場合は、予約済み keyword-only 引数として
        ``*, context: ExecutionContext`` を追加できる。例:

        - ``_do_execute(self, *, context: ExecutionContext)``
        - ``_do_execute(self, params: MyParams, *, context: ExecutionContext)``

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
        async def operation() -> None:
            if self._state != ControllerState.DISCONNECTED:
                raise StateError(
                    f"connect requires DISCONNECTED state, current: {self._state.name}"
                )
            await self._do_connect(address)
            self._state = ControllerState.CONNECTED

        await self._run_logged_operation(
            "connect",
            operation,
            log_context={"address_provided": bool(address)},
        )

    async def disconnect_async(self) -> None:
        """非同期で装置から切断する。

        Raises:
            StateError: すでに ``DISCONNECTED`` の場合。
        """
        async def operation() -> None:
            if self._state == ControllerState.DISCONNECTED:
                raise StateError("Already disconnected")
            await self._do_disconnect()
            self._state = ControllerState.DISCONNECTED

        await self._run_logged_operation("disconnect", operation)

    async def change_async(self, target: Any) -> None:
        """非同期で対象を切り替える。

        Args:
            target: 切替先（型はサブクラスの ``_do_change`` に合わせる）。

        Raises:
            StateError: 未接続など、接続済みでない場合。
        """
        async def operation() -> None:
            self._require_connected()
            await self._do_change(target)

        await self._run_logged_operation(
            "change",
            operation,
            log_context={"target_type": type(target).__name__},
        )

    async def execute_async(
        self,
        params: Any | None = None,
        *,
        context: ExecutionContext | None = None,
    ) -> Any:
        """非同期でメイン操作を実行する。

        実行中は状態が ``BUSY`` になり、成功時は ``CONNECTED`` に戻る。
        例外発生時は ``ERROR`` になる。

        Args:
            params: execute 入力。Pydantic モデルインスタンス、または
                ``dict`` などモデルに変換できる値を渡す。``_do_execute`` が
                引数を持たない場合は ``None`` を渡すか省略する。
            context: 実行コンテキスト。省略時は実開始時刻だけを持つ既定値を生成する。
                ``_do_execute`` が ``context`` を受け取る場合のみ渡される。

        Returns:
            ``_do_execute`` の戻り値。

        Raises:
            StateError: 未接続の場合。
            Exception: ``_do_execute`` 内で発生した例外（状態は ``ERROR``）。
        """
        # ---引数として渡されたパラメータを、Pydanticモデルに変換する
        normalized_params = normalize_execute_params(self, params)
        accepts_context = resolve_execute_params_spec(self).accepts_context
        resolved_context = context or ExecutionContext(started_at=_utcnow())

        async def operation() -> Any:
            self._require_connected()
            self._state = ControllerState.BUSY
            try:
                # ---引数をつけたりつけなかったりしながら、_do_execute()を実行する
                if normalized_params is None and not accepts_context:
                    result = await self._do_execute()
                elif normalized_params is None:
                    result = await self._do_execute(context=resolved_context)
                elif not accepts_context:
                    result = await self._do_execute(normalized_params)
                else:
                    result = await self._do_execute(
                        normalized_params,
                        context=resolved_context,
                    )
            except Exception:
                self._state = ControllerState.ERROR
                raise
            self._state = ControllerState.CONNECTED
            return result

        return await self._run_logged_operation(
            "execute",
            operation,
            log_context={
                "has_params": normalized_params is not None,
                "has_context": accepts_context,
            },
        )

    async def status_async(self) -> dict[str, Any]:
        """非同期で状態を取得する。

        Returns:
            ``controller_state`` キーと ``_do_status`` の結果をマージした dict。
        """
        async def operation() -> dict[str, Any]:
            return {
                "controller_state": self._state.name,
                **(await self._do_status()),
            }

        return await self._run_logged_operation("status", operation)

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

    def execute(
        self,
        params: Any | None = None,
        *,
        context: ExecutionContext | None = None,
    ) -> Any:
        """同期でメイン操作を実行する。

        Args:
            params: ``execute_async`` と同じ execute 入力。
            context: ``execute_async`` と同じ実行コンテキスト。

        Returns:
            ``_do_execute`` の戻り値。

        Raises:
            RuntimeError: イベントループ内から呼ばれた場合。
        """
        return self._run_sync(self.execute_async(params, context=context))

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

    async def _run_logged_operation(
        self,
        operation_name: str,
        operation: Callable[[], Awaitable[Any]],
        log_context: dict[str, Any] | None = None,
    ) -> Any:
        """操作の開始・終了・失敗ログを共通化する。
        
        Args:
            operation_name: 操作名。
            operation: 操作関数。
            log_context: ログコンテキスト。

        Returns:
            操作の結果。

        Raises:
            Exception: 操作中に発生した例外。
        """
        context = {"state_before": self._state.name, **(log_context or {})}
        started = perf_counter()
        # ---操作開始ログを出力する
        log_operation_start(
            self._logger,
            layer="controller",
            operation_name=operation_name,
            controller_name=type(self).__name__,
            context=context,
        )
        try:
            # ---操作を実行する
            result = await operation()
        except Exception as exc:
            # ---失敗した場合、操作失敗ログを出力する
            log_operation_failure(
                self._logger,
                layer="controller",
                operation_name=operation_name,
                controller_name=type(self).__name__,
                duration_ms=(perf_counter() - started) * 1000,
                context={**context, "state_after": self._state.name},
                exc=exc,
            )
            raise

        # ---成功した場合、操作成功ログを出力する
        log_operation_success(
            self._logger,
            layer="controller",
            operation_name=operation_name,
            controller_name=type(self).__name__,
            duration_ms=(perf_counter() - started) * 1000,
            context={**context, "state_after": self._state.name},
        )
        return result

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


def _utcnow() -> datetime:
    """UTC の現在時刻を返す。

    Returns:
        タイムゾーン付きの UTC 現在時刻。
    """
    return datetime.now(timezone.utc)
