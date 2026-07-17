"""ExecutionManager の上でシーケンス実行を管理する補助レイヤ。

このモジュールは、複数の ``execute`` を「1 行ずつ」直列で進めるための
ドメインモデルと :class:`SequenceRunner` を提供する。

処理の流れ（概要）:

1. :class:`SequenceDefinition` でシーケンス全体（モード・ステップ列）を表現する。
2. :meth:`SequenceRunner.validate` で定義を事前検証する（GUI の Validate 相当）。
3. :meth:`SequenceRunner.start` がバックグラウンドスレッドで :meth:`SequenceRunner._run_sequence`
   を起動し、各ステップで ``controller.change`` と ``ExecutionManager.start`` を呼ぶ。
4. :meth:`SequenceRunner.get_snapshot` が UI 向けに現在状態を返す（時間駆動の残り時間表示も含む）。

Note:
    ``ExecutionManager`` は同時に 1 ジョブしか持てないため、本 runner は
    ステップ間で必ず終端状態まで待ってから次の ``start`` に進む。
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from threading import Lock
from time import sleep
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from stationkit.core import (
    ControllerState,
    SequenceMode,
    StateError,
    StationControllerBase,
    TimeoutError,
)
from stationkit.core.execution_context import ExecutionContext
from stationkit.core.introspection import (
    normalize_execute_params,
    resolve_execute_params_spec,
    resolve_target_type,
)
from stationkit.execution import (
    ExecutionManager,
    ExecutionState,
    ExecutionStatus,
    SupportsExecutionCancellation,
)


# -----------------------------------------------------------------------------
# 公開モデル（シーケンス定義・状態・検証結果）
# -----------------------------------------------------------------------------


class SequenceRunState(str, Enum):
    """SequenceRunner が公開するシーケンス全体の状態。

    Attributes:
        IDLE: 未開始、または直近の実行記録が無い状態（スナップショット取得時の扱い）。
        RUNNING: 実行中。
        STOPPING: ユーザーからの停止要求を受け取り、進行中 execute の中断を試みている。
        SUCCEEDED: 正常終了。
        FAILED: 実行エラーなどで打ち切られた。
        CANCELLED: ユーザー停止などで打ち切られた。
    """

    IDLE = "IDLE"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SequenceStepState(str, Enum):
    """各ステップの公開状態。

    Attributes:
        PENDING: まだ開始されていない（有効ステップのみ）。
        WAITING: 時間駆動で ``start_at`` まで待機中。
        RUNNING: execute 実行中（``ExecutionManager`` 上は RUNNING）。
        CANCELLING: cancel 要求済みで終端待ち。
        SUCCEEDED: execute が成功終了。
        FAILED: execute が失敗終了。
        CANCELLED: execute が取消終了（時間駆動の終了時刻 cancel もここに写像される）。
        SKIPPED: 無効化・ユーザー停止・先行失敗などによりスキップされた。
    """

    PENDING = "PENDING"
    WAITING = "WAITING"
    RUNNING = "RUNNING"
    CANCELLING = "CANCELLING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


class SequenceIssueSeverity(str, Enum):
    """検証・実行時 issue の重要度。

    Attributes:
        ERROR: ``start`` を許可しないレベルの問題。
        WARNING: 実行は続行可能だが注意が必要な問題。
    """

    ERROR = "ERROR"
    WARNING = "WARNING"


class SequenceRunHandle(BaseModel):
    """開始済みシーケンスを識別するハンドル。

    Attributes:
        run_id: :meth:`SequenceRunner.start` が採番した実行 ID。
    """

    run_id: str


class SequenceIssue(BaseModel):
    """検証や実行中に見つかった注意点。

    Attributes:
        severity: 重要度（:class:`SequenceIssueSeverity`）。
        code: 機械可読な識別子（例: ``cancel_not_supported``）。
        message: 人間向けメッセージ。
        step_id: 対象ステップの ID。シーケンス全体向けなら ``None``。
        step_index: 対象ステップの定義上のインデックス。シーケンス全体向けなら ``None``。
    """

    severity: SequenceIssueSeverity
    code: str
    message: str
    step_id: str | None = None
    step_index: int | None = None


class SequenceValidationResult(BaseModel):
    """シーケンス定義の検証結果。

    Attributes:
        issues: 検出された注意点の一覧。
    """

    issues: list[SequenceIssue] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """ERROR 相当の issue が無い場合に ``True`` を返す。

        Returns:
            ERROR issue が 1 件も無ければ ``True``、それ以外は ``False``。
        """
        return all(issue.severity != SequenceIssueSeverity.ERROR for issue in self.issues)


class SequenceStep(BaseModel):
    """シーケンス 1 行分の定義。

    Attributes:
        id: ステップの一意 ID（JSON import/export で安定参照に使う）。
        enabled: ``False`` の行は実行対象外として ``SKIPPED`` 扱いになる。
        label: UI 表示用ラベル。
        target: ``controller.change`` に渡す値（型は各 controller の ``_do_change`` に従う）。
        execute_params: ``ExecutionManager.start`` に渡す execute 引数（dict 化された値）。
        start_at: 時間駆動モードでの開始時刻（タイムゾーン無しはローカル解釈）。
        end_at: 時間駆動モードでの終了時刻（到達時に ``cancel`` を要求する）。
        notes: 自由記述メモ。
    """

    id: str = Field(default_factory=lambda: uuid4().hex)
    enabled: bool = True
    label: str = ""
    target: Any = None
    execute_params: dict[str, Any] | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    notes: str = ""


class SequenceDefinition(BaseModel):
    """import / export 可能なシーケンス定義。

    Attributes:
        version: シリアライズ形式のバージョン（将来の互換用）。
        name: シーケンス名。
        mode: 進め方（:class:`SequenceMode`）。
        steps: 上から順に実行されるステップ列。
    """

    version: int = 1
    name: str = "Sequence"
    mode: SequenceMode = SequenceMode.COMPLETION_DRIVEN
    steps: list[SequenceStep] = Field(default_factory=list)


class SequenceStepStatus(BaseModel):
    """各ステップの実行状況（スナップショット用）。

    Attributes:
        id: ステップ ID。
        enabled: 定義上の有効/無効。
        label: 表示ラベル。
        target: 定義上のターゲット値。
        execute_params: 定義上の execute 引数。
        start_at: 定義上の開始時刻。
        end_at: 定義上の終了時刻。
        notes: メモ。
        state: 実行状態（:class:`SequenceStepState`）。
        message: 人間向けの進捗メッセージ。
        countdown_text: 時間駆動向けの残り時間表示（:meth:`SequenceRunner.get_snapshot` で付与）。
        execution_id: 紐づく ``ExecutionManager`` ジョブ ID。
        started_at: ステップ開始に関する時刻（execute 開始を指す）。
        finished_at: ステップ終了に関する時刻（execute 終端を指す）。
        result: execute の戻り値（JSON 化済みの dict 等）。
        error_message: 失敗時のエラーメッセージ。
        cancel_requested: cancel 要求が出ているか。
    """

    id: str
    enabled: bool
    label: str
    target: Any = None
    execute_params: dict[str, Any] | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    notes: str = ""
    state: SequenceStepState = SequenceStepState.PENDING
    message: str | None = None
    countdown_text: str | None = None
    execution_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: Any | None = None
    error_message: str | None = None
    cancel_requested: bool = False


class SequenceSnapshot(BaseModel):
    """SequenceRunner が GUI/HTTP 向けに返すスナップショット。

    Attributes:
        run_id: 実行 ID。未開始で記録が無い場合は ``None`` になり得る。
        sequence_name: シーケンス名。
        mode: 進め方。
        state: シーケンス全体の状態。
        started_at: シーケンス開始時刻。
        finished_at: シーケンス終了時刻。
        current_step_index: 現在処理中のステップインデックス。無ければ ``None``。
        stop_requested: ユーザー停止が要求されたか。
        message: シーケンス全体の要約メッセージ。
        issues: 検証警告や実行時に付与された issue。
        steps: ステップごとの状態列（定義と同じ長さ）。
    """

    run_id: str | None = None
    sequence_name: str = "Sequence"
    mode: SequenceMode = SequenceMode.COMPLETION_DRIVEN
    state: SequenceRunState = SequenceRunState.IDLE
    started_at: datetime | None = None
    finished_at: datetime | None = None
    current_step_index: int | None = None
    stop_requested: bool = False
    message: str | None = None
    issues: list[SequenceIssue] = Field(default_factory=list)
    steps: list[SequenceStepStatus] = Field(default_factory=list)


@dataclass(slots=True)
class _SequenceRecord:
    """SequenceRunner 内部で保持する mutable な実行記録。

    Attributes:
        definition: 実行に使った定義（開始時点のスナップショット）。
        snapshot: UI 向けに更新していくスナップショット。
        future: バックグラウンド実行の ``Future``。
    """

    definition: SequenceDefinition
    snapshot: SequenceSnapshot
    future: Future[None] | None = None


class _SequenceOutcome(str, Enum):
    """内部の制御フローで使う終端理由。

    Attributes:
        SUCCEEDED: ステップとしては成功で次へ進める。
        FAILED: ステップ失敗によりシーケンスを打ち切る。
        STOPPED: ユーザー停止により打ち切る。
        SCHEDULED_END: 時間駆動の終了時刻 cancel により終端へ到達した（シーケンス継続可）。
    """

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"
    SCHEDULED_END = "SCHEDULED_END"

# -----------------------------------------------------------------------------
# SequenceRunner クラス
# -----------------------------------------------------------------------------
class SequenceRunner:
    """ExecutionManager を使って複数 execute を順番に制御する。

    1 つの controller に対して、内部で専用の :class:`ExecutionManager` を 1 つ持ち、
    シーケンス中は常に「高々 1 つの execute ジョブ」だけがアクティブになるように制御する。

    Note:
        ``stop`` は協調的 cancel を前提とする。cancel 非対応 controller では
        時間駆動モードを :meth:`validate` で拒否する。
    """

    def __init__(
        self,
        controller: StationControllerBase,
        *,
        poll_interval_s: float = 0.2,
        cancel_timeout_s: float = 10.0,
    ) -> None:
        """SequenceRunner を初期化する。

        Args:
            controller: シーケンス実行の対象となるコントローラ。
            poll_interval_s: ``ExecutionManager.get_status`` のポーリング間隔（秒）。
            cancel_timeout_s: cancel 要求後に終端状態へ遷移しない場合のタイムアウト（秒）。

        Note:
            cancel 非対応の controller でも完了駆動モードは利用できる。
        """
        self._controller = controller
        self._execution_manager = ExecutionManager(controller)
        self._poll_interval_s = poll_interval_s
        self._cancel_timeout_s = cancel_timeout_s
        self._lock = Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"{type(controller).__name__}Sequence",
        )
        self._record: _SequenceRecord | None = None
        self._stop_requested = False
        # ---_do_change, _do_execute の型を解決する
        self._target_adapter = TypeAdapter(resolve_target_type(controller))
        self._execute_params_spec = resolve_execute_params_spec(controller)

    def validate(self, definition: SequenceDefinition | dict[str, Any]) -> SequenceValidationResult:
        """シーケンス定義を controller の型制約込みで検証する。

        Args:
            definition: 検証対象の定義。dict でも受け付ける。

        Returns:
            検証結果（issue 一覧を含む）。

        Notes:
            定義は :class:`SequenceDefinition` として正規化したうえで、次を検査する。

            全ステップ:
                - ステップ ``id`` の重複（ERROR）。

            有効（``enabled``）な各ステップ:
                - ``target`` が必須であること（ERROR）。
                - ``target`` が当該 controller の target 型に合致すること
                  （:class:`TypeAdapter` による検証、ERROR）。
                - 当該 controller が execute 用パラメータを受け付けない場合、
                  ``execute_params`` は未指定（``None`` または空の dict）に限る（ERROR）。
                - 受け付ける場合は ``normalize_execute_params`` に通ること
                  （TypeError / ValidationError を ERROR に変換）。

            モードとスケジュール:
                - controller metadata が宣言していないモードは使用不可（ERROR）。
                - ``TIME_DRIVEN`` では、上記の有効ステップごとに
                  ``start_at`` / ``end_at`` の双方が必須（ERROR）かつ
                  ``end_at`` を UTC 比較で厳密に ``start_at`` より後にする（ERROR）。
                - それ以外のモードで ``start_at`` または ``end_at`` がある場合は
                  無視される旨の警告（WARNING）。

            定義全体:
                - 有効なステップが少なくとも 1 つ存在すること（ERROR）。

            時間駆動モードのみ、さらに定義全体で:
                - :meth:`cancel_execution` を持つ controller（協調的 cancel 対応）であること
                  （ERROR; :class:`SupportsExecutionCancellation`）。
                - 有効ステップを定義上の **順序** どおりに見たとき、前ステップの
                  ``end_at``（UTC）未満に次ステップの ``start_at`` がある
                  重なり（順序不整合）がないこと（ERROR）。
        """
        normalized = SequenceDefinition.model_validate(definition)
        issues: list[SequenceIssue] = [] # 問題点を格納するリスト
        enabled_steps: list[tuple[int, SequenceStep]] = [] # 有効なステップを格納するリスト
        seen_ids: set[str] = set() # これまでに確認したステップIDを格納する集合
        supported_modes = self._controller.get_metadata().sequence_modes

        if normalized.mode not in supported_modes:
            issues.append(
                SequenceIssue(
                    severity=SequenceIssueSeverity.ERROR,
                    code="unsupported_sequence_mode",
                    message=(
                        f"Mode {normalized.mode.value} is not supported by "
                        f"{type(self._controller).__name__}."
                    ),
                )
            )

        # 各ステップに対して検証を行う
        for index, step in enumerate(normalized.steps):
            # ---ステップIDが重複している場合はエラー
            if step.id in seen_ids:
                issues.append(
                    _issue(
                        SequenceIssueSeverity.ERROR,
                        "duplicate_step_id",
                        "Step ids must be unique.",
                        step,
                        index,
                    )
                )
            seen_ids.add(step.id)

            # ---ステップが無効な場合はスキップ
            if not step.enabled:
                continue
            enabled_steps.append((index, step))

            # ---ターゲット(=対象のサンプル番号)が無い場合はエラー
            if step.target is None:
                issues.append(
                    _issue(
                        SequenceIssueSeverity.ERROR,
                        "missing_target",
                        "Enabled steps require a target.",
                        step,
                        index,
                    )
                )
            else:
                # ---ターゲット(=対象のサンプル番号)が有る場合は、TypeAdapterで検証する
                try:
                    # 型があっていればOK
                    self._target_adapter.validate_python(step.target)
                except ValidationError as exc:
                    # 型があっていない場合はエラー
                    issues.append(
                        _issue(
                            SequenceIssueSeverity.ERROR,
                            "invalid_target",
                            f"Target is invalid: {exc}",
                            step,
                            index,
                        )
                    )

            if not self._execute_params_spec.accepts_params:
                if step.execute_params not in (None, {}):
                    # ---executeのパラメータを受け付けないのに、パラメータがある場合はエラー
                    issues.append(
                        _issue(
                            SequenceIssueSeverity.ERROR,
                            "unexpected_execute_params",
                            "This controller does not accept execute parameters.",
                            step,
                            index,
                        )
                    )
            else:
                # ---executeのパラメータを受け付ける場合は、normalize_execute_paramsで検証する
                try:
                    normalize_execute_params(self._controller, step.execute_params)
                except (TypeError, ValidationError) as exc:
                    # 型があっていない場合はエラー
                    issues.append(
                        _issue(
                            SequenceIssueSeverity.ERROR,
                            "invalid_execute_params",
                            f"Execute parameters are invalid: {exc}",
                            step,
                            index,
                        )
                    )

            # ---時間駆動モードの場合は、開始時刻と終了時刻が必要
            if normalized.mode == SequenceMode.TIME_DRIVEN:
                # ---開始時刻と終了時刻が無い場合はエラー
                if step.start_at is None or step.end_at is None:
                    issues.append(
                        _issue(
                            SequenceIssueSeverity.ERROR,
                            "missing_schedule",
                            "Time-driven steps require both start_at and end_at.",
                            step,
                            index,
                        )
                    )
                elif _to_utc(step.start_at) >= _to_utc(step.end_at):
                    # ---開始時刻が終了時刻より前の場合はエラー
                    issues.append(
                        _issue(
                            SequenceIssueSeverity.ERROR,
                            "invalid_schedule_range",
                            "end_at must be later than start_at.",
                            step,
                            index,
                        )
                    )
            elif step.start_at is not None or step.end_at is not None:
                # ---時間駆動モードではないのに、開始時刻と終了時刻がある場合は警告
                issues.append(
                    _issue(
                        SequenceIssueSeverity.WARNING,
                        "unused_schedule",
                        "start_at/end_at are ignored in completion-driven mode.",
                        step,
                        index,
                    )
                )

        # ...と、ここまで一通りみてきて、
        # ---有効なステップが無い場合はエラー
        if not enabled_steps:
            issues.append(
                SequenceIssue(
                    severity=SequenceIssueSeverity.ERROR,
                    code="no_enabled_steps",
                    message="At least one enabled step is required.",
                )
            )

        # ---時間駆動モードの場合は、cancel_executionをサポートしているかを確認する
        if (
            normalized.mode == SequenceMode.TIME_DRIVEN
            and normalized.mode in supported_modes
        ):
            # ---cancel_executionをサポートしていない場合はエラー
            if not isinstance(self._controller, SupportsExecutionCancellation):
                issues.append(
                    SequenceIssue(
                        severity=SequenceIssueSeverity.ERROR,
                        code="cancel_not_supported",
                        message=(
                            "Time-driven mode requires a controller that supports "
                            "cancel_execution()."
                        ),
                    )
                )
            # ---時間駆動モードの場合は、ステップの開始時刻と終了時刻が重複していないかを確認する
            previous_end: datetime | None = None
            for index, step in enabled_steps:
                if step.start_at is None or step.end_at is None:
                    continue
                start_at = _to_utc(step.start_at)
                end_at = _to_utc(step.end_at)
                if previous_end is not None and start_at < previous_end:
                    issues.append(
                        _issue(
                            SequenceIssueSeverity.ERROR,
                            "overlapping_schedule",
                            "Time-driven steps must be ordered and non-overlapping.",
                            step,
                            index,
                        )
                    )
                previous_end = end_at

        # ---検証結果を返す
        return SequenceValidationResult(issues=issues)

    def start(self, definition: SequenceDefinition | dict[str, Any]) -> SequenceRunHandle:
        """シーケンスを別スレッドで開始し、即座にハンドルを返す。

        Args:
            definition: 実行するシーケンス定義。

        Returns:
            実行 ID を含むハンドル。

        Raises:
            ValueError: 検証に失敗した場合（ERROR issue のメッセージを連結する）。
            StateError: 既に別のシーケンス実行が進行中の場合。
            StateError: controller が ``CONNECTED`` でない場合。
        """
        # ---シーケンス全体を検証する
        normalized = SequenceDefinition.model_validate(definition)
        validation = self.validate(normalized)
        if not validation.ok:
            message = "; ".join(issue.message for issue in validation.issues if issue.severity == SequenceIssueSeverity.ERROR)
            raise ValueError(message)

        with self._lock:
            if self._has_active_run_locked():
                raise StateError("Sequence is already running.")
            # ERROR 残留時などに RUNNING スナップショットを作らないよう、開始前に拒否する。
            # 復帰は disconnect → connect の明示操作に委ねる。
            if self._controller.state != ControllerState.CONNECTED:
                raise StateError(
                    "Operation requires CONNECTED state, "
                    f"current: {self._controller.state.name}"
                )

            # ---SequenceStepから、SequenceStepStatus -> SequenceSnapshotを作成する
            snapshot = SequenceSnapshot(
                run_id=uuid4().hex,
                sequence_name=normalized.name,
                mode=normalized.mode,
                state=SequenceRunState.RUNNING,
                started_at=_utcnow(),
                issues=list(validation.issues),
                steps=[
                    SequenceStepStatus(
                        id=step.id,
                        enabled=step.enabled,
                        label=step.label or f"Step {index + 1}",
                        target=step.target,
                        execute_params=step.execute_params,
                        start_at=step.start_at,
                        end_at=step.end_at,
                        notes=step.notes,
                        state=SequenceStepState.SKIPPED if not step.enabled else SequenceStepState.PENDING,
                        message="Disabled step." if not step.enabled else None,
                    )
                    for index, step in enumerate(normalized.steps)
                ],
            )
            # ---内部用_SequenceRecordを作成する
            record = _SequenceRecord(definition=normalized, snapshot=snapshot)
            self._record = record
            self._stop_requested = False
            # ---バックグラウンドで_run_sequence()を実行する
            record.future = self._executor.submit(self._run_sequence, snapshot.run_id, normalized)
            return SequenceRunHandle(run_id=snapshot.run_id)

    def get_snapshot(self, run_id: str | None = None) -> SequenceSnapshot:
        """現在または最後のシーケンス実行状況を返す。

        Args:
            run_id: 対象実行 ID。省略時は直近の記録を返す。

        Returns:
            スナップショットのディープコピー。時間駆動向けの ``countdown_text`` を付与する。

        Raises:
            StateError: 対象の実行記録が存在しない場合。
        """
        with self._lock:
            # ---_SequenceRecordから、SequenceSnapshotを取得する
            record = self._require_record_locked(run_id)
            snapshot = record.snapshot.model_copy(deep=True)
        now = _utcnow()
        # ---時間駆動モードの場合は、countdown_textが更新されたスナップショットを返す
        for step in snapshot.steps:
            step.countdown_text = _build_countdown_text(snapshot.mode, step, now)
        return snapshot

    def stop(self, run_id: str | None = None) -> None:
        """進行中シーケンスへ停止要求を出す。

        ``stop_requested`` を立て、進行中 execute があれば :meth:`ExecutionManager.cancel` を呼ぶ。
        以降のステップは :meth:`SequenceRunner._mark_remaining_skipped` により ``SKIPPED`` になる。

        Args:
            run_id: 対象実行 ID。省略時は直近の実行を対象にする。

        Raises:
            StateError: 停止可能な実行が無い場合。
        """
        should_cancel = False
        with self._lock:
            record = self._require_record_locked(run_id)
            if record.snapshot.state not in (
                SequenceRunState.RUNNING,
                SequenceRunState.STOPPING,
            ):
                raise StateError("No running sequence to stop.")
            # ---_stop_requestedを立てる
            self._stop_requested = True
            record.snapshot.stop_requested = True
            record.snapshot.state = SequenceRunState.STOPPING
            should_cancel = self._execution_manager.has_active_execution()

        if should_cancel:
            # ---ExecutionManager.cancel()を呼び出す
            try:
                self._execution_manager.cancel()
            except StateError:
                pass

    def has_active_run(self) -> bool:
        """進行中シーケンスがあるかどうかを返す。

        Returns:
            ``RUNNING`` / ``STOPPING`` のいずれかなら ``True``。
        """
        with self._lock:
            return self._has_active_run_locked()

    def export_report_json(self, run_id: str | None = None) -> str:
        """スナップショットを JSON 文字列として書き出す。

        Args:
            run_id: 対象実行 ID。省略時は直近の記録。

        Returns:
            ``SequenceSnapshot`` の JSON（インデント付き）。
        """
        return self.get_snapshot(run_id).model_dump_json(indent=2)

    def close(self) -> None:
        """内部 executor と ExecutionManager を停止する。

        Note:
            シャットダウンは待機しない（``wait=False``）。アプリ終了時向け。
        """
        self._execution_manager.close()
        self._executor.shutdown(wait=False, cancel_futures=False)

    # -------------------------------------------------------------------------
    # 内部: シーケンス本体の実行ループ
    # -------------------------------------------------------------------------

    def _run_sequence(self, run_id: str, definition: SequenceDefinition) -> None:
        """バックグラウンドスレッド上でシーケンス全体を実行する。startにより、別スレッドで呼び出される関数。

        Args:
            run_id: 実行 ID。
            definition: 実行対象の定義。

        Note:
            例外は握りつぶさず、スナップショットへ ``runner_error`` issue として残す。
        """
        outcome = SequenceRunState.SUCCEEDED
        message = "Sequence completed."
        try:
            for index, step in enumerate(definition.steps):
                # ---開始前に確認する
                # enabledじゃない場合はスキップ
                if not step.enabled:
                    continue
                # stop_requestedが立っている場合は、スキップ
                if self._stop_requested:
                    self._mark_remaining_skipped(index)
                    outcome = SequenceRunState.CANCELLED
                    message = "Sequence stopped before starting the next step."
                    break
                # ---ステップを実行する
                step_outcome = self._run_step(run_id, definition.mode, index, step)
                if step_outcome == _SequenceOutcome.SUCCEEDED:
                    continue
                if step_outcome == _SequenceOutcome.SCHEDULED_END:
                    continue
                if step_outcome == _SequenceOutcome.STOPPED:
                    self._mark_remaining_skipped(index + 1)
                    outcome = SequenceRunState.CANCELLED
                    message = "Sequence stop was requested."
                    break
                self._mark_remaining_skipped(index + 1)
                outcome = SequenceRunState.FAILED
                message = "Sequence failed."
                break
        except Exception as exc:
            outcome = SequenceRunState.FAILED
            message = str(exc) or exc.__class__.__name__
            with self._lock:
                record = self._require_matching_record_locked(run_id)
                current_step_index = record.snapshot.current_step_index
                if current_step_index is not None:
                    runtime = record.snapshot.steps[current_step_index]
                    runtime.state = SequenceStepState.FAILED
                    runtime.error_message = message
                    runtime.message = message
                    runtime.finished_at = _utcnow()
                record.snapshot.issues.append(
                    SequenceIssue(
                        severity=SequenceIssueSeverity.ERROR,
                        code="runner_error",
                        message=message,
                        step_index=current_step_index,
                    )
                )

        with self._lock:
            record = self._require_matching_record_locked(run_id)
            record.snapshot.state = outcome
            record.snapshot.message = message
            record.snapshot.finished_at = _utcnow()
            record.snapshot.current_step_index = None

    def _run_step(
        self,
        run_id: str,
        mode: SequenceMode,
        index: int,
        step: SequenceStep,
    ) -> _SequenceOutcome:
        """1 ステップ分の処理（change → execute → 待機）を実行する。

        Args:
            run_id: 実行 ID。
            mode: シーケンスモード。
            index: ステップインデックス。
            step: ステップ定義。

        Returns:
            このステップの結果に応じた内部アウトカム。
        """
        with self._lock:
            # ---実行するステップにあわせて、_SequenceRecordを更新する
            record = self._require_matching_record_locked(run_id)
            record.snapshot.current_step_index = index
            runtime = record.snapshot.steps[index]
            runtime.message = "Preparing step."
            runtime.error_message = None
            runtime.result = None

        # ---時間駆動モードの場合は、実行時間になるまで待機する
        if mode == SequenceMode.TIME_DRIVEN:
            wait_outcome = self._wait_until_start(run_id, index, step)
            if wait_outcome is not None:
                return wait_outcome

        # ----------
        # ---以下、いちステップを実施する
        # ----------
        # ---targetを変更する
        self._controller.change(self._target_adapter.validate_python(step.target))
        # ---execute に渡す実行コンテキストを組み立てる
        # TIME_DRIVEN の予定境界は書き換えず、実開始時刻との差から遅延を判別可能にする。
        scheduled_start_at = (
            _to_utc(step.start_at)
            if mode == SequenceMode.TIME_DRIVEN and step.start_at is not None
            else None
        )
        scheduled_end_at = (
            _to_utc(step.end_at)
            if mode == SequenceMode.TIME_DRIVEN and step.end_at is not None
            else None
        )
        step_context = ExecutionContext(
            started_at=_utcnow(),
            scheduled_start_at=scheduled_start_at,
            scheduled_end_at=scheduled_end_at,
            sequence_run_id=run_id,
            sequence_step_id=step.id,
            sequence_step_index=index,
        )
        # ---executeを開始する
        handle = self._execution_manager.start(
            step.execute_params,
            context=step_context,
        )
        # ---_SequenceRecordを更新する
        with self._lock:
            record = self._require_matching_record_locked(run_id)
            runtime = record.snapshot.steps[index]
            runtime.execution_id = handle.execution_id
            runtime.started_at = _utcnow()
            runtime.state = SequenceStepState.RUNNING
            runtime.message = "Execution running."

        # ---executeの終端までポーリングする(scheduled_endがあるなら、それも考慮する)
        scheduled_end = scheduled_end_at
        return self._wait_for_execution(run_id, index, handle.execution_id, scheduled_end)

    def _wait_until_start(
        self,
        run_id: str,
        index: int,
        step: SequenceStep,
    ) -> _SequenceOutcome | None:
        """始まりを待つ。時間駆動モードで ``start_at`` まで待機する。

        Args:
            run_id: 実行 ID。
            index: ステップインデックス。
            step: ステップ定義。

        Returns:
            待機の結果として打ち切る場合は :class:`_SequenceOutcome`。
            待機不要（``start_at`` 無し）や遅延開始扱いで即開始する場合は ``None``。

        Note:
            遅延開始（現在時刻が ``start_at`` を過ぎている）場合は警告 issue を付与し、
            直ちに execute へ進む。
        """
        if step.start_at is None:
            return None
        start_at = _to_utc(step.start_at)
        now = _utcnow()
        # ---待機開始時点で、現在時刻がstart_atを過ぎている場合は警告を出し、即実行する
        if now > start_at:
            self._add_issue(
                run_id,
                SequenceIssueSeverity.WARNING,
                "late_start",
                f"Step started late by {_format_duration(now - start_at)}.",
                step.id,
                index,
            )
            return None

        # ---開始時刻前は、ひたすら待つ
        while _utcnow() < start_at:
            # ---stop_requestedが立っている場合、待機を終了する
            if self._stop_requested:
                with self._lock:
                    record = self._require_matching_record_locked(run_id)
                    runtime = record.snapshot.steps[index]
                    runtime.state = SequenceStepState.SKIPPED
                    runtime.message = "Skipped because sequence stop was requested."
                return _SequenceOutcome.STOPPED
            # ---WAITING状態にする
            with self._lock:
                record = self._require_matching_record_locked(run_id)
                runtime = record.snapshot.steps[index]
                runtime.state = SequenceStepState.WAITING
                runtime.message = "Waiting for scheduled start time."
            # ---ひたすら待つよ
            sleep(min(self._poll_interval_s, 0.5))
        return None

    def _wait_for_execution(
        self,
        run_id: str,
        index: int,
        execution_id: str,
        scheduled_end: datetime | None,
    ) -> _SequenceOutcome:
        """終わりを待つ。execute の終端までポーリングし、必要なら cancel を発行する。

        Args:
            run_id: 実行 ID。
            index: ステップインデックス。
            execution_id: ``ExecutionManager`` のジョブ ID。
            scheduled_end: 時間駆動の終了時刻（UTC 正規化済みで渡す）。完了駆動では ``None``。

        Returns:
            終端理由に応じた内部アウトカム。

        Raises:
            TimeoutError: cancel 要求後に終端へ到達しなかった場合。
        """
        scheduled_cancel_requested = False
        stop_cancel_requested = False
        cancel_deadline: datetime | None = None

        while True:
            # ---状態をポーリングし続ける
            status = self._execution_manager.get_status(execution_id)
            # ---状態を更新する
            self._sync_step_status(run_id, index, status)
            now = _utcnow()
            # ---終端状態になった場合は、終端理由に応じてアウトカムを返す
            if status.state not in (ExecutionState.RUNNING, ExecutionState.CANCELLING):
                if status.state == ExecutionState.CANCELLED:
                    if stop_cancel_requested or self._stop_requested:
                        return _SequenceOutcome.STOPPED
                    if (
                        scheduled_end is not None
                        and (scheduled_cancel_requested or now >= scheduled_end)
                    ):
                        return _SequenceOutcome.SCHEDULED_END
                if stop_cancel_requested:
                    return _SequenceOutcome.STOPPED
                if scheduled_cancel_requested:
                    return _SequenceOutcome.SCHEDULED_END
                if status.state == ExecutionState.SUCCEEDED:
                    return _SequenceOutcome.SUCCEEDED
                return _SequenceOutcome.FAILED

            # ---stop_requestedが立っている場合は、cancelを要求する
            if self._stop_requested and not stop_cancel_requested:
                stop_cancel_requested = True
                if self._request_cancel(run_id, index, execution_id, "Sequence stop was requested."):
                    cancel_deadline = now + timedelta(seconds=self._cancel_timeout_s)
                else:
                    # ---cancelを要求できない場合は、警告を出す
                    self._add_issue(
                        run_id,
                        SequenceIssueSeverity.WARNING,
                        "stop_waits_for_completion",
                        "Execution cancellation is not supported; waiting for the active step to finish.",
                        self._current_step_id(run_id, index),
                        index,
                    )

            # ---時間駆動モードの場合、終了時刻になったらcancelを要求する
            if (
                scheduled_end is not None
                and not scheduled_cancel_requested
                and now >= scheduled_end
            ):
                scheduled_cancel_requested = True
                if self._request_cancel(run_id, index, execution_id, "Scheduled end time reached."):
                    cancel_deadline = now + timedelta(seconds=self._cancel_timeout_s)

            # ---cancel_deadlineになっても終端状態にならない場合は、エラーを返す
            if cancel_deadline is not None and now >= cancel_deadline:
                raise TimeoutError(
                    "Execution did not reach a terminal state after cancel request."
                )

            # ---ひたすらポーリングするんだね
            sleep(self._poll_interval_s)

    def _request_cancel(
        self,
        run_id: str,
        index: int,
        execution_id: str,
        message: str,
    ) -> bool:
        """ExecutionManager へ cancel を要求し、ステップ表示を更新する。

        Args:
            run_id: 実行 ID。
            index: ステップインデックス。
            execution_id: 対象ジョブ ID。
            message: UI 向けメッセージ。

        Returns:
            cancel 要求が受理できた場合 ``True``。未対応等で拒否されたら ``False``。
        """
        try:
            # ---ExecutionManager へ cancel を要求する
            self._execution_manager.cancel(execution_id)
        except StateError:
            return False
        with self._lock:
            # ---キャンセル要求が受理されたら、_SequenceRecordを更新する
            # (これにより、_wait_for_execution()が ExecutionState.CANCELLING で終了し、アウトカムを返すことになる)
            record = self._require_matching_record_locked(run_id)
            runtime = record.snapshot.steps[index]
            runtime.state = SequenceStepState.CANCELLING
            runtime.cancel_requested = True
            runtime.message = message
        return True

    def _sync_step_status(
        self,
        run_id: str,
        index: int,
        status: ExecutionStatus,
    ) -> None:
        """ExecutionManager の状態をステップのスナップショットへ反映する。

        Args:
            run_id: 実行 ID。
            index: ステップインデックス。
            status: ``ExecutionManager.get_status`` の結果。
        """
        with self._lock:
            record = self._require_matching_record_locked(run_id)
            runtime = record.snapshot.steps[index]
            runtime.execution_id = status.execution_id
            runtime.cancel_requested = status.cancel_requested
            runtime.result = _normalize_value(status.result)
            runtime.error_message = status.error_message
            runtime.started_at = status.started_at
            runtime.finished_at = status.finished_at

            mapping = {
                ExecutionState.RUNNING: SequenceStepState.RUNNING,
                ExecutionState.CANCELLING: SequenceStepState.CANCELLING,
                ExecutionState.SUCCEEDED: SequenceStepState.SUCCEEDED,
                ExecutionState.FAILED: SequenceStepState.FAILED,
                ExecutionState.CANCELLED: SequenceStepState.CANCELLED,
            }
            runtime.state = mapping[status.state]
            runtime.message = _message_from_execution_status(status)

    def _mark_remaining_skipped(self, start_index: int) -> None:
        """指定インデックス以降の未着手ステップを ``SKIPPED`` にする。

        Args:
            start_index: スキップ開始位置（このインデックスを含む）。
        """
        with self._lock:
            if self._record is None:
                return
            for runtime in self._record.snapshot.steps[start_index:]:
                if runtime.enabled and runtime.state == SequenceStepState.PENDING:
                    runtime.state = SequenceStepState.SKIPPED
                    runtime.message = "Skipped."

    def _add_issue(
        self,
        run_id: str,
        severity: SequenceIssueSeverity,
        code: str,
        message: str,
        step_id: str | None,
        step_index: int | None,
    ) -> None:
        """スナップショットへ実行時 issue を追加する。

        Args:
            run_id: 実行 ID。
            severity: 重要度。
            code: issue コード。
            message: メッセージ。
            step_id: 対象ステップ ID。
            step_index: 対象ステップインデックス。
        """
        with self._lock:
            record = self._require_matching_record_locked(run_id)
            record.snapshot.issues.append(
                SequenceIssue(
                    severity=severity,
                    code=code,
                    message=message,
                    step_id=step_id,
                    step_index=step_index,
                )
            )

    def _current_step_id(self, run_id: str, index: int) -> str | None:
        """現在ステップの ID を返す。

        Args:
            run_id: 実行 ID。
            index: ステップインデックス。

        Returns:
            ステップ ID。存在しなければ ``None``。
        """
        with self._lock:
            record = self._require_matching_record_locked(run_id)
            return record.snapshot.steps[index].id

    def _has_active_run_locked(self) -> bool:
        """ロック保持下で進行中シーケンスかどうかを判定する。

        Returns:
            ``RUNNING`` / ``STOPPING`` のいずれかなら ``True``。
        """
        return (
            self._record is not None
            and self._record.snapshot.state
            in (SequenceRunState.RUNNING, SequenceRunState.STOPPING)
        )

    def _require_record_locked(self, run_id: str | None) -> _SequenceRecord:
        """ロック保持下で実行記録を取得する。

        Args:
            run_id: 対象実行 ID。省略時は直近。

        Returns:
            内部記録。

        Raises:
            StateError: 記録が無い、または ID が一致しない場合。
        """
        if self._record is None:
            raise StateError("No sequence has been started.")
        if run_id is None or run_id == self._record.snapshot.run_id:
            return self._record
        raise StateError(f"Unknown sequence run id: {run_id}")

    def _require_matching_record_locked(self, run_id: str) -> _SequenceRecord:
        """ロック保持下で ``run_id`` が一致する実行記録を取得する。

        Args:
            run_id: 対象実行 ID。

        Returns:
            内部記録。

        Raises:
            StateError: 記録が無い、または ID が一致しない場合。
        """
        record = self._require_record_locked(run_id)
        if record.snapshot.run_id != run_id:
            raise StateError(f"Unknown sequence run id: {run_id}")
        return record

# ----------
# ---各種ヘルパー関数
# ----------
def _issue(
    severity: SequenceIssueSeverity,
    code: str,
    message: str,
    step: SequenceStep,
    index: int,
) -> SequenceIssue:
    """ステップに紐づく :class:`SequenceIssue` を生成する。

    Args:
        severity: 重要度。
        code: issue コード。
        message: メッセージ。
        step: 対象ステップ。
        index: ステップインデックス。

    Returns:
        生成した issue。
    """
    return SequenceIssue(
        severity=severity,
        code=code,
        message=message,
        step_id=step.id,
        step_index=index,
    )


def _normalize_value(value: Any) -> Any:
    """スナップショット向けに値を JSON 互換へ正規化する。

    Args:
        value: 任意の値。

    Returns:
        ``BaseModel`` なら ``model_dump(mode=\"json\")``、それ以外はそのまま。
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def _message_from_execution_status(status: ExecutionStatus) -> str:
    """ExecutionManager の状態から UI メッセージを組み立てる。

    Args:
        status: ジョブ状態。

    Returns:
        人間向けメッセージ。
    """
    if status.state == ExecutionState.RUNNING:
        return "Execution running."
    if status.state == ExecutionState.CANCELLING:
        return "Cancellation requested."
    if status.state == ExecutionState.SUCCEEDED:
        return "Execution completed."
    if status.state == ExecutionState.CANCELLED:
        return "Execution cancelled."
    return status.error_message or "Execution failed."


def _build_countdown_text(
    mode: SequenceMode,
    step: SequenceStepStatus,
    now: datetime,
) -> str | None:
    """時間駆動向けの残り時間表示文字列を組み立てる。

    Args:
        mode: シーケンスモード。
        step: ステップ状態。
        now: 現在時刻（UTC）。

    Returns:
        表示文字列。完了駆動モードでは ``None``。
    """
    if mode != SequenceMode.TIME_DRIVEN:
        return None

    if step.state in (
        SequenceStepState.SUCCEEDED,
        SequenceStepState.CANCELLED,
        SequenceStepState.FAILED,
        SequenceStepState.SKIPPED,
    ):
        return {
            SequenceStepState.SUCCEEDED: "Completed",
            SequenceStepState.CANCELLED: "Cancelled",
            SequenceStepState.FAILED: "Failed",
            SequenceStepState.SKIPPED: "Skipped",
        }[step.state]

    if step.state == SequenceStepState.WAITING and step.start_at is not None:
        remaining = _to_utc(step.start_at) - now
        return f"Starts in {_format_duration(remaining)}"

    if step.state in (SequenceStepState.RUNNING, SequenceStepState.CANCELLING) and step.end_at is not None:
        remaining = _to_utc(step.end_at) - now
        if remaining.total_seconds() > 0:
            return f"Ends in {_format_duration(remaining)}"
        return "At scheduled end"

    if step.start_at is not None and _to_utc(step.start_at) > now:
        return f"Starts in {_format_duration(_to_utc(step.start_at) - now)}"
    return None


def _format_duration(delta: timedelta) -> str:
    """``timedelta`` を ``hh:mm:ss`` 形式へ整形する。

    Args:
        delta: 残り時間。

    Returns:
        ゼロ埋めされた時間文字列。
    """
    total_seconds = max(0, int(delta.total_seconds()))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _to_utc(value: datetime) -> datetime:
    """``datetime`` を UTC に正規化する。

    Args:
        value: 入力日時。

    Returns:
        UTC へ変換した日時。

    Note:
        タイムゾーン無し（naive）は「ローカル時刻」として解釈してから UTC へ変換する。
    """
    if value.tzinfo is None:
        return value.astimezone().astimezone(UTC)
    return value.astimezone(UTC)


def _utcnow() -> datetime:
    """UTC の現在時刻を返す。

    Returns:
        タイムゾーン付き UTC 現在時刻。
    """
    return datetime.now(UTC)
