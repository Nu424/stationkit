"""stationkit: ステーション制御フレームワークのパッケージルート。

主要シンボルはここからインポートできる（例: ``StationControllerBase``,
``create_http_app``, ``create_cli_app``）。
"""

from stationkit.adapters import (
    create_cli_app,
    create_gui_app,
    create_http_app,
    create_local_cli_app,
)
from stationkit.core import (
    CommandError,
    ConnectionError,
    ControllerState,
    CustomAction,
    ExecutionCancelledError,
    StateError,
    StationControllerBase,
    StationError,
    TimeoutError,
)
from stationkit.execution import (
    ExecutionHandle,
    ExecutionManager,
    ExecutionState,
    ExecutionStatus,
    SupportsExecutionCancellation,
)
from stationkit.sequence import (
    SequenceDefinition,
    SequenceIssue,
    SequenceIssueSeverity,
    SequenceMode,
    SequenceRunHandle,
    SequenceRunner,
    SequenceRunState,
    SequenceSnapshot,
    SequenceStep,
    SequenceStepState,
    SequenceValidationResult,
)
from stationkit.testing import MockStationController

__all__ = [
    "CommandError",
    "ConnectionError",
    "ControllerState",
    "create_cli_app",
    "create_gui_app",
    "create_http_app",
    "create_local_cli_app",
    "CustomAction",
    "ExecutionCancelledError",
    "ExecutionHandle",
    "ExecutionManager",
    "ExecutionState",
    "ExecutionStatus",
    "MockStationController",
    "SequenceDefinition",
    "SequenceIssue",
    "SequenceIssueSeverity",
    "SequenceMode",
    "SequenceRunHandle",
    "SequenceRunner",
    "SequenceRunState",
    "SequenceSnapshot",
    "SequenceStep",
    "SequenceStepState",
    "SequenceValidationResult",
    "StateError",
    "StationControllerBase",
    "StationError",
    "SupportsExecutionCancellation",
    "TimeoutError",
]
