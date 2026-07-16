"""長時間 execute を別スレッドで管理する補助レイヤ。"""

from __future__ import annotations

import inspect
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from threading import Lock
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel

from stationkit.core.base import StationControllerBase
from stationkit.core.exceptions import StateError
from stationkit.core.execution_context import ExecutionContext
from stationkit.core.state import ControllerState


# -----------------------------------------------------------------------------
# 公開状態モデル
# -----------------------------------------------------------------------------


class ExecutionState(str, Enum):
    """ExecutionManager が管理する実行状態。"""

    RUNNING = "RUNNING"
    CANCELLING = "CANCELLING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ExecutionHandle(BaseModel):
    """開始済み実行を識別するハンドル。"""

    execution_id: str


class ExecutionStatus(BaseModel):
    """1 件の execute ジョブの公開状態。"""

    execution_id: str
    state: ExecutionState
    started_at: datetime
    finished_at: datetime | None = None
    result: Any | None = None
    error_message: str | None = None
    cancel_requested: bool = False


@runtime_checkable
class SupportsExecutionCancellation(Protocol):
    """安全な execute 中断を提供できる controller 用の任意 hook。"""

    def cancel_execution(self) -> None:
        """進行中の execute を中断するよう機器へ要求する。

        Returns:
            なし。
        """


# -----------------------------------------------------------------------------
# 内部データ構造
# -----------------------------------------------------------------------------


@dataclass(slots=True)
class _ExecutionRecord:
    """ExecutionManager 内部で保持する mutable な実行記録。"""

    status: ExecutionStatus
    future: Future[None] | None = None
    error: BaseException | None = None


class ExecutionManager:
    """controller.execute() を single-worker thread でジョブ管理する。"""

    def __init__(self, controller: StationControllerBase) -> None:
        """ExecutionManager を初期化する。

        Args:
            controller: execute を委譲する対象コントローラ。
        """
        self._controller = controller
        self._lock = Lock()
        # ExecutionManagerが、ThreadPoolExecutorを管理する
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"{type(controller).__name__}Execution",
        )
        self._record: _ExecutionRecord | None = None

    def start(
        self,
        params: Any | None = None,
        *,
        context: ExecutionContext | None = None,
    ) -> ExecutionHandle:
        """execute を別スレッドで開始し、即座にハンドルを返す。

        Args:
            params: controller.execute() にそのまま渡す execute パラメータ。
            context: controller へ渡す実行コンテキスト。省略時は
                ``execution_id`` と ``started_at`` だけを持つ既定値を生成する。
                呼び出し側が渡した値がある場合は、それらを上書きして共有する。

        Returns:
            開始したジョブを識別するハンドル。

        Raises:
            StateError: すでに進行中の execute が存在する場合。
        """
        with self._lock:
            if self._has_active_execution_locked():
                raise StateError("Execution is already running.")

            # 実行開始前に公開状態を作り、worker と共有する。
            execution_id = uuid4().hex
            started_at = _utcnow()
            status = ExecutionStatus(
                execution_id=execution_id,
                state=ExecutionState.RUNNING,
                started_at=started_at,
            )
            # status と controller が見る識別子・実開始時刻を一致させる。
            if context is None:
                resolved_context = ExecutionContext(
                    started_at=started_at,
                    execution_id=execution_id,
                )
            else:
                resolved_context = context.model_copy(
                    update={
                        "started_at": started_at,
                        "execution_id": execution_id,
                    }
                )
            record = _ExecutionRecord(status=status)
            self._record = record

            # 実処理は controller.execute() 全体を worker thread へ委譲する。
            record.future = self._executor.submit(
                self._run_execution, # _run_execution()で、controller.execute()をラップしている
                status.execution_id,
                params,
                resolved_context,
            )
            return ExecutionHandle(execution_id=status.execution_id)

    def get_status(self, execution_id: str | None = None) -> ExecutionStatus:
        """現在または最後の execute ジョブ状態を返す。  ExecutionManagerによる実行状況のみを管理する(各機器の具体的な状況は、controller.status_async()等で取得する)

        Args:
            execution_id: 取得対象の execution id。省略時は直近ジョブを返す。

        Returns:
            外部公開用にコピーした execution status。

        Raises:
            StateError: 対象ジョブが存在しない場合。
        """
        with self._lock:
            record = self._require_record_locked(execution_id)
            return record.status.model_copy(deep=True)

    def cancel(self, execution_id: str | None = None) -> None:
        """進行中 execute に cancel を要求する。

        worker thread を強制停止せず、controller 側の任意 hook に協調的な
        中断要求を委ねる。

        Args:
            execution_id: cancel 対象の execution id。省略時は直近ジョブを使う。

        Returns:
            なし。

        Raises:
            StateError: 実行中ジョブがない場合、または cancel 未対応の場合。
            TypeError: `cancel_execution()` が同期メソッドでない場合。
        """
        controller = self._controller
        with self._lock:
            record = self._require_record_locked(execution_id)
            if record.status.state not in (
                ExecutionState.RUNNING,
                ExecutionState.CANCELLING,
            ):
                raise StateError("No running execution to cancel.")
            if record.status.cancel_requested:
                return
            if not isinstance(controller, SupportsExecutionCancellation):
                raise StateError(
                    "Execution cancellation is not supported by this controller."
                )
            # _ExecutionRecordのstatusを更新し、cancel_requestedをTrueにする
            record.status.cancel_requested = True
            record.status.state = ExecutionState.CANCELLING

        # cancel hook は controller 実装側に委ねる。thread 自体は kill しない。
        result = controller.cancel_execution()
        if inspect.isawaitable(result): # awaitableなら、close()を呼び出す
            close = getattr(result, "close", None)
            if callable(close):
                close()
            raise TypeError(
                "cancel_execution() must be synchronous because "
                "ExecutionManager.cancel() is synchronous."
            )

    def has_active_execution(self) -> bool:
        """進行中 execute があるかどうかを返す。

        Returns:
            状態が `RUNNING` または `CANCELLING` のジョブがある場合は `True`。
        """
        with self._lock:
            return self._has_active_execution_locked()

    def close(self) -> None:
        """内部 executor を停止する。

        Returns:
            なし。
        """
        self._executor.shutdown(wait=False, cancel_futures=False)

    # ----------
    # ---以下、ヘルパー関数
    # ----------
    def _run_execution(
        self,
        execution_id: str,
        params: Any | None,
        context: ExecutionContext,
    ) -> None:
        """worker thread 上で controller.execute() を実行する。

        Args:
            execution_id: 実行対象ジョブの識別子。
            params: controller.execute() に渡す execute パラメータ。
            context: controller.execute() に渡す実行コンテキスト。

        Returns:
            なし。
        """
        try:
            result = self._controller.execute(params, context=context)
        except Exception as exc:
            self._finish_with_exception(execution_id, exc)
            return
        self._finish_with_success(execution_id, result)

    def _finish_with_success(self, execution_id: str, result: Any) -> None:
        """成功終了したジョブの状態を確定する。

        Args:
            execution_id: 更新対象ジョブの識別子。
            result: execute の戻り値。

        Returns:
            なし。

        Raises:
            StateError: 対象ジョブが見つからない場合。
        """
        with self._lock:
            # 対象ジョブrecordを取得し、SUCCEEDEDに設定する
            record = self._require_matching_record_locked(execution_id)
            record.error = None
            record.status.state = ExecutionState.SUCCEEDED
            record.status.finished_at = _utcnow()
            record.status.result = result
            record.status.error_message = None

    def _finish_with_exception(self, execution_id: str, exc: Exception) -> None:
        """例外終了したジョブの状態を確定する。

        Args:
            execution_id: 更新対象ジョブの識別子。
            exc: execute 中に発生した例外。

        Returns:
            なし。

        Raises:
            StateError: 対象ジョブが見つからない場合。
        """
        from stationkit.core.exceptions import ExecutionCancelledError

        with self._lock:
            # 対象ジョブrecordを取得し、errorを設定する
            record = self._require_matching_record_locked(execution_id)
            record.error = exc
            record.status.finished_at = _utcnow()
            record.status.result = None
            record.status.error_message = str(exc) or exc.__class__.__name__

            # cancel だけは FAILED ではなく CANCELLED に写像する。
            if isinstance(exc, ExecutionCancelledError):
                self._restore_controller_state_after_cancel()
                record.status.state = ExecutionState.CANCELLED
                return

            record.status.state = ExecutionState.FAILED

    def _restore_controller_state_after_cancel(self) -> None:
        """cancel 後に controller 状態を接続済みに戻す。

        Returns:
            なし。
        """
        if self._controller.state in (ControllerState.BUSY, ControllerState.ERROR):
            self._controller._state = ControllerState.CONNECTED

    # ----------
    # ---ロックしながら、各種値を取得する
    # ----------
    def _has_active_execution_locked(self) -> bool:
        """ロック保持下で進行中 execute の有無を判定する。

        Returns:
            状態が `RUNNING` または `CANCELLING` のジョブがある場合は `True`。
        """
        return self._record is not None and self._record.status.state in (
            ExecutionState.RUNNING,
            ExecutionState.CANCELLING,
        )

    def _require_record_locked(
        self,
        execution_id: str | None,
    ) -> _ExecutionRecord:
        """ロック保持下で対象ジョブを取得する。

        Args:
            execution_id: 取得対象の execution id。省略時は直近ジョブを使う。

        Returns:
            内部管理用の execution record。

        Raises:
            StateError: ジョブが存在しない場合、または id が一致しない場合。
        """
        if self._record is None:
            raise StateError("No execution has been started.")
        if execution_id is None or execution_id == self._record.status.execution_id:
            return self._record
        raise StateError(f"Unknown execution id: {execution_id}")

    def _require_matching_record_locked(self, execution_id: str) -> _ExecutionRecord:
        """完了処理対象の execution id と内部 record の一致を確認する。

        Args:
            execution_id: worker thread から渡された execution id。

        Returns:
            一致確認済みの execution record。

        Raises:
            StateError: 対象ジョブが存在しない場合、または id が一致しない場合。
        """
        record = self._require_record_locked(execution_id)
        if record.status.execution_id != execution_id:
            raise StateError(f"Unknown execution id: {execution_id}")
        return record


# -----------------------------------------------------------------------------
# 小さなユーティリティ
# -----------------------------------------------------------------------------


def _utcnow() -> datetime:
    """UTC の現在時刻を返す。

    Returns:
        タイムゾーン付きの UTC 現在時刻。
    """
    return datetime.now(timezone.utc)
