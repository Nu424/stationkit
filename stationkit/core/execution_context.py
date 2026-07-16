"""execute 実行時に controller へ渡す実行コンテキスト。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ExecutionContext(BaseModel):
    """1 回の execute に付随する実行コンテキスト。

    Attributes:
        started_at: controller 実行を開始した実時刻。
        scheduled_start_at: Sequence の予定開始時刻。非時間駆動では ``None``。
        scheduled_end_at: Sequence の予定終了時刻。非時間駆動では ``None``。
        execution_id: ``ExecutionManager`` が採番した ID。直接実行では ``None``。
        sequence_run_id: シーケンス実行 ID。シーケンス外では ``None``。
        sequence_step_id: シーケンスステップ ID。シーケンス外では ``None``。
        sequence_step_index: シーケンスステップインデックス。シーケンス外では ``None``。
    """

    model_config = ConfigDict(frozen=True)

    started_at: datetime
    scheduled_start_at: datetime | None = None
    scheduled_end_at: datetime | None = None
    execution_id: str | None = None
    sequence_run_id: str | None = None
    sequence_step_id: str | None = None
    sequence_step_index: int | None = None
