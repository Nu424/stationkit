"""stationkit のコア・HTTP/CLI アダプタの結合テスト。"""

from __future__ import annotations

import asyncio
import logging
from threading import Event, Lock
from time import sleep
from typing import Any

import gradio as gr
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field
from typer.testing import CliRunner

from stationkit.adapters._form_inputs import (
    input_field_specs_from_model,
    model_uses_scalar_fields,
    resolve_primitive_input_type,
    supports_scalar_input,
)
import stationkit.adapters.cli as cli_adapter
import stationkit.adapters.gui as gui_adapter
from stationkit import (
    ControllerState,
    CustomAction,
    ExecutionCancelledError,
    ExecutionContext,
    ExecutionManager,
    ExecutionState,
    SequenceDefinition,
    SequenceMode,
    SequenceRunner,
    SequenceStep,
    StateError,
    create_cli_app,
    create_gui_app,
    create_http_app,
    create_local_cli_app,
)
from stationkit.core.introspection import resolve_execute_params_spec
from stationkit.testing import MockStationController

# -----------------------------------------------------------------------------
# テスト用ダブルとスキーマ
# -----------------------------------------------------------------------------


class FailingController(MockStationController):
    """execute 時に失敗させ、ERROR 状態遷移を検証する。"""

    async def _do_execute(self) -> dict[str, bool]:
        raise RuntimeError("boom")


class CalibrateInput(BaseModel):
    """CustomAction 用の入力スキーマ（テスト）。"""

    level: int
    force: bool = False


class CalibrateOutput(BaseModel):
    """CustomAction 用の出力スキーマ（テスト）。"""

    status: str
    level: int


class ComplexActionInput(BaseModel):
    """GUI の JSON フォールバック確認用入力。"""

    config: dict[str, int]


class ExecuteInput(BaseModel):
    """execute 用の必須入力スキーマ。"""

    duration_s: int
    rpm: int | None = None


class OptionalExecuteInput(BaseModel):
    """execute 用のオプショナル入力スキーマ。"""

    duration_s: int | None = None
    rpm: int | None = None


class OptionalBoolInput(BaseModel):
    """Optional[bool] の widget 判定確認用スキーマ。"""

    enabled: bool | None = None


class FormFieldSpecInput(BaseModel):
    """共通 field spec helper の確認用スキーマ。"""

    required_level: int = Field(title="Required Level")
    optional_ratio: float | None = Field(default=None, title="Optional Ratio")
    enabled: bool | None = Field(default=None, title="Enabled")


class AdvancedMockStationController(MockStationController):
    """固有操作 calibrate を公開するモック。"""

    async def _do_calibrate(self, params: CalibrateInput) -> CalibrateOutput:
        """キャリブレーションのダミー実装。

        Args:
            params: レベルと強制フラグ。

        Returns:
            成功を示す出力モデル。
        """
        return CalibrateOutput(status="success", level=params.level)

    def get_custom_actions(self) -> list[CustomAction]:
        """HTTP/CLI に載せる操作一覧。"""
        return [
            CustomAction(
                name="calibrate",
                description="Run calibration",
                func=self._do_calibrate,
                input_schema=CalibrateInput,
                output_schema=CalibrateOutput,
            )
        ]


class ParameterizedExecuteController(MockStationController):
    """execute が必須パラメータを受け取るモック。"""

    async def _do_execute(self, params: ExecuteInput) -> dict[str, Any]:
        self._call_log.append(f"execute({params.duration_s}, {params.rpm})")
        return {
            "mock": True,
            "target": self._current_target,
            "params": params.model_dump(),
        }


class OptionalExecuteController(MockStationController):
    """execute がオプショナルパラメータを受け取るモック。"""

    async def _do_execute(
        self,
        params: OptionalExecuteInput | None = None,
    ) -> dict[str, Any]:
        payload = None if params is None else params.model_dump()
        self._call_log.append(f"execute({payload})")
        return {
            "mock": True,
            "target": self._current_target,
            "params": payload,
        }


class SlowExecuteController(MockStationController):
    """外部イベントまで execute を継続するモック。"""

    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self._release = Event()

    def finish_execution(self) -> None:
        self._release.set()

    async def _do_execute(self) -> dict[str, Any]:
        self._call_log.append("execute()")
        self.started.set()
        while not self._release.is_set():
            await asyncio.sleep(0.01)
        return {"mock": True, "target": self._current_target}


class CancellableExecuteController(SlowExecuteController):
    """cancel hook で execute を中断できるモック。"""

    def __init__(self) -> None:
        super().__init__()
        self.cancel_calls = 0

    def cancel_execution(self) -> None:
        self.cancel_calls += 1
        self.finish_execution()

    async def _do_execute(self) -> dict[str, Any]:
        self._call_log.append("execute()")
        self.started.set()
        while not self._release.is_set():
            await asyncio.sleep(0.01)
        raise ExecutionCancelledError("Execution cancelled.")


class ContextOnlyExecuteController(MockStationController):
    """params 無しで ExecutionContext だけを受け取るモック。"""

    def __init__(self) -> None:
        super().__init__()
        self.last_context: ExecutionContext | None = None

    async def _do_execute(self, *, context: ExecutionContext) -> dict[str, Any]:
        self.last_context = context
        self._call_log.append("execute(context)")
        return {
            "mock": True,
            "target": self._current_target,
            "execution_id": context.execution_id,
            "started_at": context.started_at.isoformat(),
        }


class ContextParameterizedExecuteController(MockStationController):
    """params と ExecutionContext の両方を受け取るモック。"""

    def __init__(self) -> None:
        super().__init__()
        self.last_context: ExecutionContext | None = None
        self.last_params: ExecuteInput | None = None

    async def _do_execute(
        self,
        params: ExecuteInput,
        *,
        context: ExecutionContext,
    ) -> dict[str, Any]:
        self.last_params = params
        self.last_context = context
        self._call_log.append(
            f"execute({params.duration_s}, {params.rpm}, context)"
        )
        return {
            "mock": True,
            "target": self._current_target,
            "params": params.model_dump(),
            "scheduled_end_at": (
                None
                if context.scheduled_end_at is None
                else context.scheduled_end_at.isoformat()
            ),
        }


class ContextAwareCancellableController(MockStationController):
    """context を受け取りつつ cancel 可能なモック。"""

    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self._release = Event()
        self.cancel_calls = 0
        self.last_context: ExecutionContext | None = None

    def finish_execution(self) -> None:
        self._release.set()

    def cancel_execution(self) -> None:
        self.cancel_calls += 1
        self.finish_execution()

    async def _do_execute(self, *, context: ExecutionContext) -> dict[str, Any]:
        self.last_context = context
        self._call_log.append("execute(context)")
        self.started.set()
        while not self._release.is_set():
            await asyncio.sleep(0.01)
        raise ExecutionCancelledError("Execution cancelled.")


def _wait_for_manager_terminal_status(
    manager: ExecutionManager,
    execution_id: str,
    timeout_s: float = 1.0,
) -> Any:
    """manager の実行が終端状態になるまで polling する。"""
    remaining = timeout_s
    while remaining > 0:
        status = manager.get_status(execution_id)
        if status.state not in {ExecutionState.RUNNING, ExecutionState.CANCELLING}:
            return status
        sleep(0.02)
        remaining -= 0.02
    raise AssertionError("Execution did not reach a terminal state in time.")


# -----------------------------------------------------------------------------
# StationControllerBase（同期 API・状態）
# -----------------------------------------------------------------------------


def test_sync_controller_flow() -> None:
    """同期 API で接続から切断まで一通り動作し、ログが期待どおりであること。"""
    controller = MockStationController()

    controller.connect("COM3")
    controller.change(4)
    result = controller.execute()
    status = controller.status()
    controller.disconnect()

    assert result == {"mock": True, "target": 4}
    assert status["controller_state"] == "CONNECTED"
    assert status["current_target"] == 4
    # execute 成功後は基底クラスが _do_idle を呼び、流路が排気へ戻る。
    assert status["routing"] == "exhaust"
    assert controller.state == ControllerState.DISCONNECTED
    # idle は connect 直後 / execute 成功終端 / disconnect 直前に自動発火する。
    assert controller.call_log == [
        "connect(COM3)",
        "idle()",
        "change(4)",
        "execute()",
        "idle()",
        "idle()",
        "disconnect()",
    ]


def test_execute_failure_sets_error_state() -> None:
    """execute 内で例外が出た場合、状態が ERROR になること。"""
    controller = FailingController()
    controller.connect("COM3")

    with pytest.raises(RuntimeError, match="boom"):
        controller.execute()

    assert controller.state == ControllerState.ERROR


def test_execute_supports_required_and_optional_params() -> None:
    """execute が必須/任意パラメータ付きで呼べること。"""
    required = ParameterizedExecuteController()
    required.connect("COM3")
    required.change(2)

    with pytest.raises(TypeError, match="requires execute parameters"):
        required.execute()

    result = required.execute({"duration_s": 5, "rpm": 120})
    assert result == {
        "mock": True,
        "target": 2,
        "params": {"duration_s": 5, "rpm": 120},
    }

    optional = OptionalExecuteController()
    optional.connect("COM4")
    optional.change(7)

    assert optional.execute() == {"mock": True, "target": 7, "params": None}
    assert optional.execute({"duration_s": 3}) == {
        "mock": True,
        "target": 7,
        "params": {"duration_s": 3, "rpm": None},
    }


# -----------------------------------------------------------------------------
# idle（稼働していないときの動作）フック
# -----------------------------------------------------------------------------


def test_idle_fires_on_connect_execute_and_disconnect() -> None:
    """connect 直後 / execute 成功終端 / disconnect 直前に idle が自動発火すること。"""
    controller = MockStationController()

    controller.connect("COM3")
    # connect 直後に idle が確立され、流路は排気レーンへ向く。
    assert controller.status()["routing"] == "exhaust"

    controller.change(2)
    result = controller.execute()
    assert result == {"mock": True, "target": 2}
    # execute 成功後は基底クラスが idle を呼び、流路が排気へ戻る。
    assert controller.status()["routing"] == "exhaust"

    controller.disconnect()
    assert controller.call_log == [
        "connect(COM3)",
        "idle()",
        "change(2)",
        "execute()",
        "idle()",
        "idle()",
        "disconnect()",
    ]


def test_idle_not_fired_after_execute_error() -> None:
    """想定外エラーで ERROR になった execute のあとは idle を呼ばないこと。"""
    controller = FailingController()
    controller.connect("COM3")
    controller.change(2)

    with pytest.raises(RuntimeError, match="boom"):
        controller.execute()

    assert controller.state == ControllerState.ERROR
    # idle は connect 直後の 1 回だけで、execute エラー後には発火しない。
    assert controller.call_log.count("idle()") == 1
    assert controller.call_log[-1] != "idle()"


def test_idle_fires_after_execution_cancel() -> None:
    """cancel 終端後も idle が発火し、CONNECTED へ戻ること。"""
    controller = CancellableExecuteController()
    controller.connect("COM3")
    controller.change(5)
    manager = ExecutionManager(controller)
    handle = manager.start()
    assert controller.started.wait(timeout=1.0)

    manager.cancel(handle.execution_id)
    status = _wait_for_manager_terminal_status(manager, handle.execution_id)

    assert status.state == ExecutionState.CANCELLED
    assert controller.state == ControllerState.CONNECTED
    # cancel 後も idle が確立され、流路は排気へ戻る。
    assert controller.status()["routing"] == "exhaust"
    # connect 直後と cancel 終端の少なくとも 2 回 idle が発火している。
    assert controller.call_log.count("idle()") >= 2


def test_manual_idle_requires_connected_state() -> None:
    """手動 idle は CONNECTED のときだけ許可されること。"""
    controller = MockStationController()

    with pytest.raises(StateError, match="idle requires CONNECTED"):
        controller.idle()

    controller.connect("COM3")
    controller.change(1)
    controller.idle()
    assert controller.state == ControllerState.CONNECTED
    assert controller.status()["routing"] == "exhaust"


def test_sync_api_rejects_active_event_loop() -> None:
    """イベントループ実行中に同期 API を呼ぶと RuntimeError になること。"""
    controller = MockStationController()

    async def call_sync_api() -> None:
        with pytest.raises(RuntimeError, match=r"inside an event loop"):
            controller.connect("COM3")

    asyncio.run(call_sync_api())


def test_execution_manager_rejects_double_start_and_returns_terminal_result() -> None:
    """manager が即時に handle を返し、二重 start を reject すること。"""
    controller = SlowExecuteController()
    controller.connect("COM20")
    controller.change(4)
    manager = ExecutionManager(controller)

    handle = manager.start()
    assert controller.started.wait(timeout=1.0)
    assert handle.execution_id
    assert manager.get_status(handle.execution_id).state == ExecutionState.RUNNING

    with pytest.raises(StateError, match="already running"):
        manager.start()

    controller.finish_execution()
    status = _wait_for_manager_terminal_status(manager, handle.execution_id)
    assert status.state == ExecutionState.SUCCEEDED
    assert status.result == {"mock": True, "target": 4}


def test_execution_manager_maps_failures_and_cancellations() -> None:
    """manager が FAILED / CANCELLED を区別して保持すること。"""
    failing = FailingController()
    failing.connect("COM21")
    failing.change(2)
    failing_manager = ExecutionManager(failing)
    failed = failing_manager.start()
    failed_status = _wait_for_manager_terminal_status(
        failing_manager,
        failed.execution_id,
    )
    assert failed_status.state == ExecutionState.FAILED
    assert failed_status.error_message == "boom"

    cancellable = CancellableExecuteController()
    cancellable.connect("COM22")
    cancellable.change(5)
    cancel_manager = ExecutionManager(cancellable)
    handle = cancel_manager.start()
    assert cancellable.started.wait(timeout=1.0)

    cancel_manager.cancel(handle.execution_id)
    status = _wait_for_manager_terminal_status(cancel_manager, handle.execution_id)
    assert status.state == ExecutionState.CANCELLED
    assert status.cancel_requested is True
    assert cancellable.cancel_calls == 1
    assert cancellable.state == ControllerState.CONNECTED


def test_execution_manager_reports_unsupported_cancel() -> None:
    """cancel hook を持たない controller では cancel を reject すること。"""
    controller = SlowExecuteController()
    controller.connect("COM23")
    controller.change(6)
    manager = ExecutionManager(controller)
    handle = manager.start()
    assert controller.started.wait(timeout=1.0)

    with pytest.raises(StateError, match="not supported"):
        manager.cancel(handle.execution_id)

    controller.finish_execution()
    status = _wait_for_manager_terminal_status(manager, handle.execution_id)
    assert status.state == ExecutionState.SUCCEEDED


def test_execute_context_opt_in_without_params() -> None:
    """params 無しの context opt-in が直接 execute で動くこと。"""
    controller = ContextOnlyExecuteController()
    controller.connect("COM30")
    controller.change(1)

    result = controller.execute()

    assert controller.last_context is not None
    assert controller.last_context.execution_id is None
    assert controller.last_context.scheduled_start_at is None
    assert controller.last_context.scheduled_end_at is None
    assert result["execution_id"] is None
    assert resolve_execute_params_spec(controller).accepts_params is False
    assert resolve_execute_params_spec(controller).accepts_context is True


def test_execute_context_opt_in_with_params() -> None:
    """params 付きの context opt-in が直接 execute で動くこと。"""
    controller = ContextParameterizedExecuteController()
    controller.connect("COM31")
    controller.change(2)

    result = controller.execute({"duration_s": 4, "rpm": 80})

    assert controller.last_params is not None
    assert controller.last_params.duration_s == 4
    assert controller.last_context is not None
    assert controller.last_context.execution_id is None
    assert result["params"] == {"duration_s": 4, "rpm": 80}
    assert resolve_execute_params_spec(controller).accepts_params is True
    assert resolve_execute_params_spec(controller).accepts_context is True


def test_execution_manager_propagates_context_identity() -> None:
    """ExecutionManager が execution_id / started_at を context と共有すること。"""
    controller = ContextOnlyExecuteController()
    controller.connect("COM32")
    controller.change(3)
    manager = ExecutionManager(controller)

    handle = manager.start()
    status = _wait_for_manager_terminal_status(manager, handle.execution_id)

    assert status.state == ExecutionState.SUCCEEDED
    assert controller.last_context is not None
    assert controller.last_context.execution_id == handle.execution_id
    assert controller.last_context.started_at == status.started_at
    assert controller.last_context.scheduled_start_at is None
    assert controller.last_context.scheduled_end_at is None


def test_sequence_runner_passes_time_driven_schedule_in_context() -> None:
    """TIME_DRIVEN の予定開始・終了時刻が UTC 正規化されて context に届くこと。"""
    from datetime import UTC, datetime, timedelta

    controller = ContextAwareCancellableController()
    controller.connect("COM33")
    runner = SequenceRunner(controller, poll_interval_s=0.02, cancel_timeout_s=1.0)
    start_at = datetime.now(UTC) + timedelta(milliseconds=50)
    end_at = start_at + timedelta(milliseconds=200)
    definition = SequenceDefinition(
        name="timed-context",
        mode=SequenceMode.TIME_DRIVEN,
        steps=[
            SequenceStep(
                id="step-1",
                label="collect",
                target=4,
                start_at=start_at,
                end_at=end_at,
            )
        ],
    )

    handle = runner.start(definition)
    assert controller.started.wait(timeout=1.0)
    assert controller.last_context is not None
    assert controller.last_context.execution_id is not None
    assert controller.last_context.sequence_run_id == handle.run_id
    assert controller.last_context.sequence_step_id == "step-1"
    assert controller.last_context.sequence_step_index == 0
    assert controller.last_context.scheduled_start_at == start_at.astimezone(UTC)
    assert controller.last_context.scheduled_end_at == end_at.astimezone(UTC)

    # end_at 到達による cancel を待つ
    remaining = 1.5
    snapshot = None
    while remaining > 0:
        snapshot = runner.get_snapshot(handle.run_id)
        if snapshot.state.value != "RUNNING":
            break
        sleep(0.02)
        remaining -= 0.02
    assert snapshot is not None
    assert snapshot.state.value in {"SUCCEEDED", "CANCELLED"}
    assert controller.cancel_calls >= 1


def test_sequence_runner_completion_driven_has_no_schedule_in_context() -> None:
    """COMPLETION_DRIVEN では予定境界が None のまま context に渡ること。"""
    controller = ContextOnlyExecuteController()
    controller.connect("COM34")
    runner = SequenceRunner(controller, poll_interval_s=0.02)
    definition = SequenceDefinition(
        name="completion-context",
        mode=SequenceMode.COMPLETION_DRIVEN,
        steps=[SequenceStep(id="step-a", target=5)],
    )

    handle = runner.start(definition)
    remaining = 1.0
    snapshot = None
    while remaining > 0:
        snapshot = runner.get_snapshot(handle.run_id)
        if snapshot.state.value != "RUNNING":
            break
        sleep(0.02)
        remaining -= 0.02

    assert snapshot is not None
    assert snapshot.state.value == "SUCCEEDED"
    assert controller.last_context is not None
    assert controller.last_context.sequence_run_id == handle.run_id
    assert controller.last_context.sequence_step_id == "step-a"
    assert controller.last_context.sequence_step_index == 0
    assert controller.last_context.scheduled_start_at is None
    assert controller.last_context.scheduled_end_at is None


def test_legacy_controllers_remain_compatible_with_context_plumbing() -> None:
    """既存の 0/1 引数 controller が context 伝播経路でも動くこと。"""
    no_params = MockStationController()
    no_params.connect("COM35")
    no_params.change(1)
    assert no_params.execute() == {"mock": True, "target": 1}

    with_params = ParameterizedExecuteController()
    with_params.connect("COM36")
    with_params.change(2)
    manager = ExecutionManager(with_params)
    handle = manager.start({"duration_s": 1, "rpm": 10})
    status = _wait_for_manager_terminal_status(manager, handle.execution_id)
    assert status.state == ExecutionState.SUCCEEDED
    assert status.result == {
        "mock": True,
        "target": 2,
        "params": {"duration_s": 1, "rpm": 10},
    }


# -----------------------------------------------------------------------------
# アダプタ
# -----------------------------------------------------------------------------


def test_http_adapter_exposes_core_and_custom_actions() -> None:
    """HTTP で標準操作と CustomAction が利用でき、StateError が 409 になること。"""
    controller = AdvancedMockStationController()
    client = TestClient(create_http_app(controller))

    invalid_change = client.post("/change", json={"target": 1})
    connect = client.post("/connect", json={"address": "COM9"})
    change = client.post("/change", json={"target": 2})
    execute = client.post("/execute")
    action = client.post("/actions/calibrate", json={"level": 7, "force": True})
    status = client.get("/status")

    assert invalid_change.status_code == 409
    assert invalid_change.json() == {
        "detail": "Operation requires CONNECTED state, current: DISCONNECTED"
    }
    assert connect.json() == {"ok": True}
    assert change.json() == {"ok": True}
    assert execute.json() == {"mock": True, "target": 2}
    assert action.json() == {"status": "success", "level": 7}
    assert status.json()["controller_state"] == "CONNECTED"


def test_http_adapter_exposes_idle() -> None:
    """HTTP の POST /idle で idle 状態へ移せ、未接続では 409 になること。"""
    controller = MockStationController()
    client = TestClient(create_http_app(controller))

    client.post("/connect", json={"address": "COM3"})
    client.post("/change", json={"target": 3})

    assert client.post("/idle").json() == {"ok": True}
    assert client.get("/status").json()["routing"] == "exhaust"

    client.post("/disconnect")
    unconnected_idle = client.post("/idle")
    assert unconnected_idle.status_code == 409


def test_http_adapter_supports_required_and_optional_execute_params() -> None:
    """HTTP execute が required / optional body を扱えること。"""
    required_controller = ParameterizedExecuteController()
    required_client = TestClient(create_http_app(required_controller))
    required_client.post("/connect", json={"address": "COM9"})
    required_client.post("/change", json={"target": 2})

    missing = required_client.post("/execute")
    provided = required_client.post("/execute", json={"duration_s": 6, "rpm": 90})

    assert missing.status_code == 422
    assert provided.status_code == 200
    assert provided.json() == {
        "mock": True,
        "target": 2,
        "params": {"duration_s": 6, "rpm": 90},
    }

    optional_controller = OptionalExecuteController()
    optional_client = TestClient(create_http_app(optional_controller))
    optional_client.post("/connect", json={"address": "COM10"})
    optional_client.post("/change", json={"target": 4})

    empty = optional_client.post("/execute")
    payload = optional_client.post("/execute", json={"duration_s": 9})

    assert empty.status_code == 200
    assert empty.json() == {"mock": True, "target": 4, "params": None}
    assert payload.status_code == 200
    assert payload.json() == {
        "mock": True,
        "target": 4,
        "params": {"duration_s": 9, "rpm": None},
    }


def test_http_adapter_exposes_execute_start_status_and_cancel() -> None:
    """HTTP が manager-backed な start/status/cancel を公開すること。"""
    controller = CancellableExecuteController()
    client = TestClient(create_http_app(controller))
    client.post("/connect", json={"address": "COM13"})
    client.post("/change", json={"target": 3})

    started = client.post("/execute/start")
    assert started.status_code == 200
    execution_id = started.json()["execution_id"]
    assert controller.started.wait(timeout=1.0)

    running = client.get("/execute/status", params={"execution_id": execution_id})
    assert running.status_code == 200
    assert running.json()["state"] == "RUNNING"

    blocked_change = client.post("/change", json={"target": 5})
    assert blocked_change.status_code == 409
    assert blocked_change.json() == {"detail": "Execution is already running."}

    cancel = client.post("/execute/cancel", json={"execution_id": execution_id})
    assert cancel.status_code == 200
    assert cancel.json() == {"ok": True}

    remaining = 1.0
    terminal: dict[str, Any] | None = None
    while remaining > 0:
        status = client.get("/execute/status", params={"execution_id": execution_id})
        terminal = status.json()
        if terminal["state"] == "CANCELLED":
            break
        sleep(0.02)
        remaining -= 0.02

    assert terminal is not None
    assert terminal["state"] == "CANCELLED"
    assert terminal["cancel_requested"] is True


def test_gui_adapter_builds_blocks() -> None:
    """GUI アダプタが Gradio Blocks を返せること。"""
    app = create_gui_app(AdvancedMockStationController())

    assert isinstance(app, gr.Blocks)


def test_gui_handlers_share_controller_state_and_refresh_status() -> None:
    """GUI helper が同一 controller を共有し、最新状態を返すこと。"""
    controller = MockStationController()
    operation_lock = Lock()

    connect = gui_adapter._build_connect_handler(controller, operation_lock)
    change = gui_adapter._build_change_handler(controller, operation_lock, int)
    execute = gui_adapter._build_execute_handler(
        controller,
        operation_lock,
        resolve_execute_params_spec(controller),
    )

    connect_message, connect_result, connect_status = asyncio.run(connect("COM7"))
    change_message, change_result, change_status = asyncio.run(change(3))
    execute_message, execute_result, execute_status = asyncio.run(execute())

    assert connect_message == "Connected."
    assert connect_result is None
    assert connect_status["controller_state"] == "CONNECTED"

    assert change_message == "Target changed."
    assert change_result is None
    assert change_status["current_target"] == 3

    assert execute_message == "Execution completed."
    assert execute_result == {"mock": True, "target": 3}
    assert execute_status["controller_state"] == "CONNECTED"
    # connect / execute 成功後に idle が自動発火する。
    assert controller.call_log == [
        "connect(COM7)",
        "idle()",
        "change(3)",
        "execute()",
        "idle()",
    ]


def test_gui_execute_handler_supports_required_and_optional_params() -> None:
    """GUI execute helper が params あり/なしを扱えること。"""
    required = ParameterizedExecuteController()
    required.connect("COM8")
    required.change(5)
    required_execute = gui_adapter._build_execute_handler(
        required,
        Lock(),
        resolve_execute_params_spec(required),
    )

    message, result, status = asyncio.run(required_execute(4.0, 150.0))
    assert message == "Execution completed."
    assert result == {
        "mock": True,
        "target": 5,
        "params": {"duration_s": 4, "rpm": 150},
    }
    assert status["controller_state"] == "CONNECTED"

    optional = OptionalExecuteController()
    optional.connect("COM11")
    optional.change(8)
    optional_execute = gui_adapter._build_execute_handler(
        optional,
        Lock(),
        resolve_execute_params_spec(optional),
    )

    message, result, status = asyncio.run(optional_execute(None, None))
    assert message == "Execution completed."
    assert result == {"mock": True, "target": 8, "params": None}
    assert status["controller_state"] == "CONNECTED"


def test_gui_handlers_surface_execution_manager_state() -> None:
    """GUI helper が manager 実行中の状態と cancel を扱えること。"""
    controller = CancellableExecuteController()
    controller.connect("COM14")
    controller.change(2)
    manager = ExecutionManager(controller)
    handle = manager.start()
    assert controller.started.wait(timeout=1.0)

    refresh = gui_adapter._build_refresh_handler(
        controller,
        Lock(),
        execution_manager=manager,
    )
    change = gui_adapter._build_change_handler(
        controller,
        Lock(),
        int,
        execution_manager=manager,
    )
    cancel = gui_adapter._build_cancel_execute_handler(controller, Lock(), manager)

    refresh_message, _, refresh_status = asyncio.run(refresh())
    assert refresh_message == "Status refreshed."
    assert refresh_status["execution"]["state"] == "RUNNING"

    change_message, _, _ = asyncio.run(change(9))
    assert change_message == "Error: Execution is already running."

    cancel_message, _, cancel_status = asyncio.run(cancel())
    assert cancel_message == "Cancellation requested."
    assert cancel_status["execution"]["state"] in {"CANCELLING", "CANCELLED"}

    terminal = _wait_for_manager_terminal_status(manager, handle.execution_id)
    assert terminal.state == ExecutionState.CANCELLED


def test_gui_execute_handler_starts_without_waiting_when_manager_present() -> None:
    """GUI execute helper が manager ありでは non-blocking に開始すること。"""
    controller = SlowExecuteController()
    controller.connect("COM15")
    controller.change(4)
    manager = ExecutionManager(controller)
    execute = gui_adapter._build_execute_handler(
        controller,
        Lock(),
        resolve_execute_params_spec(controller),
        execution_manager=manager,
    )
    refresh = gui_adapter._build_refresh_handler(
        controller,
        Lock(),
        execution_manager=manager,
    )

    message, result, status = asyncio.run(execute())

    assert message.startswith("Execution started: ")
    assert result is None
    assert status["execution"]["state"] == "RUNNING"
    assert controller.started.wait(timeout=1.0)

    controller.finish_execution()
    terminal = _wait_for_manager_terminal_status(
        manager,
        status["execution"]["execution_id"],
    )
    assert terminal.state == ExecutionState.SUCCEEDED

    refresh_message, refresh_result, refresh_status = asyncio.run(refresh())
    assert refresh_message == "Status refreshed."
    assert refresh_result == {"mock": True, "target": 4}
    assert refresh_status["execution"]["state"] == "SUCCEEDED"


def test_gui_value_coercion_supports_scalar_and_json_fallback() -> None:
    """GUI helper が単純型と JSON フォールバックを扱えること。"""
    assert gui_adapter._coerce_value(5.0, int) == 5
    assert gui_adapter._coerce_value("hello", str) == "hello"
    assert gui_adapter._coerce_value('{"config": {"slot": 2}}', ComplexActionInput) == (
        ComplexActionInput(config={"slot": 2})
    )


def test_gui_custom_action_helpers_support_field_and_json_inputs() -> None:
    """CustomAction 入力 helper が field widget と JSON の両方を扱えること。"""
    assert gui_adapter._input_schema_uses_field_widgets(CalibrateInput) is True
    bindings = gui_adapter._build_field_bindings(CalibrateInput)
    parsed_fields = gui_adapter._parse_action_fields(
        input_schema=CalibrateInput,
        field_bindings=bindings,
        raw_values=(5.0, True),
    )
    assert parsed_fields == CalibrateInput(level=5, force=True)

    assert gui_adapter._input_schema_uses_field_widgets(ComplexActionInput) is False
    parsed_json = gui_adapter._parse_action_json(
        ComplexActionInput,
        '{"config": {"slot": 4}}',
    )
    assert parsed_json == ComplexActionInput(config={"slot": 4})


def test_form_input_helpers_build_specs_and_scalar_classification() -> None:
    """共通 field spec helper が型分類と field 属性を保持できること。"""
    specs = input_field_specs_from_model(FormFieldSpecInput)

    assert [spec.name for spec in specs] == [
        "required_level",
        "optional_ratio",
        "enabled",
    ]
    assert [spec.label for spec in specs] == [
        "Required Level",
        "Optional Ratio",
        "Enabled",
    ]
    assert specs[0].required is True
    assert specs[0].nullable is False
    assert specs[0].primitive_type == "int"
    assert specs[1].required is False
    assert specs[1].nullable is True
    assert specs[1].primitive_type == "float"
    assert specs[2].nullable is True
    assert specs[2].primitive_type == "bool"

    assert resolve_primitive_input_type(str) == "str"
    assert resolve_primitive_input_type(dict[str, int]) == "json"
    assert supports_scalar_input(int | None) is True
    assert supports_scalar_input(bool | None) is True
    assert supports_scalar_input(bool | None, allow_optional_bool=False) is False
    assert model_uses_scalar_fields(CalibrateInput) is True
    assert model_uses_scalar_fields(OptionalBoolInput) is True
    assert model_uses_scalar_fields(
        OptionalBoolInput,
        allow_optional_bool=False,
    ) is False


def test_local_cli_adapter_supports_core_and_custom_commands() -> None:
    """ローカル CLI で標準サブコマンドと固有コマンドが動くこと。"""
    controller = AdvancedMockStationController()
    app = create_local_cli_app(controller)
    runner = CliRunner()

    connect = runner.invoke(app, ["connect", "COM4"])
    change = runner.invoke(app, ["change", "6"])
    execute = runner.invoke(app, ["execute"])
    action = runner.invoke(app, ["calibrate", '{"level": 5, "force": true}'])

    assert connect.exit_code == 0
    assert connect.stdout == "Connected.\n"
    assert change.exit_code == 0
    assert change.stdout == "Changed to 6.\n"
    assert execute.exit_code == 0
    assert execute.stdout == '{"mock": true, "target": 6}\n'
    assert action.exit_code == 0
    assert action.stdout == '{"status": "success", "level": 5}\n'


def test_local_cli_execute_supports_required_and_optional_params() -> None:
    """ローカル CLI の execute が required / optional params を扱えること。"""
    required_app = create_local_cli_app(ParameterizedExecuteController())
    runner = CliRunner()

    assert runner.invoke(required_app, ["connect", "COM4"]).exit_code == 0
    assert runner.invoke(required_app, ["change", "6"]).exit_code == 0
    required_execute = runner.invoke(
        required_app,
        ["execute", '{"duration_s": 5, "rpm": 140}'],
    )

    assert required_execute.exit_code == 0
    assert (
        required_execute.stdout
        == '{"mock": true, "target": 6, "params": {"duration_s": 5, "rpm": 140}}\n'
    )

    optional_app = create_local_cli_app(OptionalExecuteController())
    assert runner.invoke(optional_app, ["connect", "COM5"]).exit_code == 0
    assert runner.invoke(optional_app, ["change", "2"]).exit_code == 0
    optional_execute = runner.invoke(optional_app, ["execute"])

    assert optional_execute.exit_code == 0
    assert optional_execute.stdout == '{"mock": true, "target": 2, "params": null}\n'


def test_cli_adapter_supports_service_backed_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """service-backed CLI が HTTP client として標準操作を委譲できること。"""
    calls: list[tuple[str, str, str, Any | None, Any | None]] = []
    service_state = {
        "controller_state": "DISCONNECTED",
        "current_target": None,
        "call_log": [],
    }
    execution_states: dict[str, dict[str, Any]] = {}

    def fake_request(
        method: str,
        server_url: str,
        path: str,
        payload: Any | None = None,
        query: dict[str, Any] | None = None,
    ) -> Any:
        calls.append((method, server_url, path, payload, query))

        if path == "/connect":
            service_state["controller_state"] = "CONNECTED"
            service_state["call_log"].append(f"connect({payload['address']})")
            return {"ok": True}

        if path == "/change":
            service_state["current_target"] = payload["target"]
            service_state["call_log"].append(f"change({payload['target']})")
            return {"ok": True}

        if path == "/execute/start":
            service_state["call_log"].append("execute()")
            execution_id = "exec-1"
            execution_states[execution_id] = {
                "state": "SUCCEEDED",
                "result": {"mock": True, "target": service_state["current_target"]},
            }
            return {"execution_id": execution_id}

        if path == "/execute/status":
            execution_id = str(query["execution_id"])
            current = execution_states[execution_id]
            return {
                "execution_id": execution_id,
                "state": current["state"],
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:00:01Z",
                "result": current["result"],
                "error_message": None,
                "cancel_requested": False,
            }

        if path == "/status":
            return dict(service_state)

        if path == "/actions/calibrate":
            return {"status": "success", "level": payload["level"]}

        raise AssertionError(f"Unexpected service path: {path}")

    monkeypatch.setattr(cli_adapter, "_request_service", fake_request)

    app = create_cli_app(AdvancedMockStationController())
    runner = CliRunner()

    connect = runner.invoke(app, ["--server", "http://svc.test", "connect", "COM4"])
    change = runner.invoke(app, ["--server", "http://svc.test", "change", "6"])
    execute = runner.invoke(app, ["--server", "http://svc.test", "execute"])
    action = runner.invoke(
        app,
        [
            "--server",
            "http://svc.test",
            "calibrate",
            '{"level": 5, "force": true}',
        ],
    )
    status = runner.invoke(app, ["--server", "http://svc.test", "status"])

    assert connect.exit_code == 0
    assert connect.stdout == "Connected.\n"
    assert change.exit_code == 0
    assert change.stdout == "Changed to 6.\n"
    assert execute.exit_code == 0
    assert execute.stdout == '{"mock": true, "target": 6}\n'
    assert action.exit_code == 0
    assert action.stdout == '{"status": "success", "level": 5}\n'
    assert status.exit_code == 0
    assert (
        status.stdout
        == '{"controller_state": "CONNECTED", "current_target": 6, '
        '"call_log": ["connect(COM4)", "change(6)", "execute()"]}\n'
    )
    assert calls == [
        ("POST", "http://svc.test", "/connect", {"address": "COM4"}, None),
        ("POST", "http://svc.test", "/change", {"target": 6}, None),
        ("POST", "http://svc.test", "/execute/start", None, None),
        (
            "GET",
            "http://svc.test",
            "/execute/status",
            None,
            {"execution_id": "exec-1"},
        ),
        (
            "POST",
            "http://svc.test",
            "/actions/calibrate",
            {"level": 5, "force": True},
            None,
        ),
        ("GET", "http://svc.test", "/status", None, None),
    ]


def test_cli_execute_supports_required_and_optional_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """service-backed CLI の execute が required / optional payload を送れること。"""
    calls: list[tuple[str, str, str, Any | None, Any | None]] = []
    payloads_by_execution_id: dict[str, Any | None] = {}
    next_execution_id = 1

    def fake_request(
        method: str,
        server_url: str,
        path: str,
        payload: Any | None = None,
        query: dict[str, Any] | None = None,
    ) -> Any:
        nonlocal next_execution_id
        calls.append((method, server_url, path, payload, query))
        if path == "/execute/start":
            execution_id = f"exec-{next_execution_id}"
            next_execution_id += 1
            payloads_by_execution_id[execution_id] = payload
            return {"execution_id": execution_id}
        if path == "/execute/status":
            execution_id = str(query["execution_id"])
            return {
                "execution_id": execution_id,
                "state": "SUCCEEDED",
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:00:01Z",
                "result": {"payload": payloads_by_execution_id[execution_id]},
                "error_message": None,
                "cancel_requested": False,
            }
        if path == "/connect":
            return {"ok": True}
        if path == "/change":
            return {"ok": True}
        raise AssertionError(f"Unexpected service path: {path}")

    monkeypatch.setattr(cli_adapter, "_request_service", fake_request)
    runner = CliRunner()

    required_app = create_cli_app(ParameterizedExecuteController())
    runner.invoke(required_app, ["--server", "http://svc.test", "connect", "COM4"])
    runner.invoke(required_app, ["--server", "http://svc.test", "change", "6"])
    required_execute = runner.invoke(
        required_app,
        ["--server", "http://svc.test", "execute", '{"duration_s": 5, "rpm": 80}'],
    )

    assert required_execute.exit_code == 0
    assert required_execute.stdout == '{"payload": {"duration_s": 5, "rpm": 80}}\n'

    optional_app = create_cli_app(OptionalExecuteController())
    runner.invoke(optional_app, ["--server", "http://svc.test", "connect", "COM7"])
    runner.invoke(optional_app, ["--server", "http://svc.test", "change", "9"])
    optional_execute = runner.invoke(
        optional_app,
        ["--server", "http://svc.test", "execute"],
    )

    assert optional_execute.exit_code == 0
    assert optional_execute.stdout == '{"payload": null}\n'
    assert (
        "POST",
        "http://svc.test",
        "/execute/start",
        {"duration_s": 5, "rpm": 80},
        None,
    ) in calls
    assert ("POST", "http://svc.test", "/execute/start", None, None) in calls


def test_cli_adapter_exposes_explicit_execution_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """service-backed CLI が explicit な execute commands を公開すること。"""
    calls: list[tuple[str, str, str, Any | None, Any | None]] = []

    def fake_request(
        method: str,
        server_url: str,
        path: str,
        payload: Any | None = None,
        query: dict[str, Any] | None = None,
    ) -> Any:
        calls.append((method, server_url, path, payload, query))
        if path == "/execute/start":
            return {"execution_id": "exec-42"}
        if path == "/execute/status":
            return {
                "execution_id": "exec-42",
                "state": "RUNNING",
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": None,
                "result": None,
                "error_message": None,
                "cancel_requested": False,
            }
        if path == "/execute/cancel":
            return {"ok": True}
        raise AssertionError(f"Unexpected service path: {path}")

    monkeypatch.setattr(cli_adapter, "_request_service", fake_request)
    app = create_cli_app(AdvancedMockStationController())
    runner = CliRunner()

    started = runner.invoke(
        app,
        ["--server", "http://svc.test", "execute-start"],
    )
    status = runner.invoke(
        app,
        [
            "--server",
            "http://svc.test",
            "execute-status",
            "--execution-id",
            "exec-42",
        ],
    )
    cancel = runner.invoke(
        app,
        [
            "--server",
            "http://svc.test",
            "execute-cancel",
            "--execution-id",
            "exec-42",
        ],
    )

    assert started.exit_code == 0
    assert started.stdout == '{"execution_id": "exec-42"}\n'
    assert status.exit_code == 0
    assert '"state": "RUNNING"' in status.stdout
    assert cancel.exit_code == 0
    assert cancel.stdout == "Cancellation requested.\n"
    assert calls == [
        ("POST", "http://svc.test", "/execute/start", None, None),
        (
            "GET",
            "http://svc.test",
            "/execute/status",
            None,
            {"execution_id": "exec-42"},
        ),
        (
            "POST",
            "http://svc.test",
            "/execute/cancel",
            {"execution_id": "exec-42"},
            None,
        ),
    ]


def test_stationkit_logging_emits_core_and_http_records(caplog: pytest.LogCaptureFixture) -> None:
    """controller と HTTP 境界が共通 extra フィールドでログを出すこと。"""
    caplog.set_level(logging.INFO, logger="stationkit")
    controller = ParameterizedExecuteController()
    app_logger = logging.getLogger("stationkit.http.test")
    client = TestClient(create_http_app(controller, logger=app_logger))

    client.post("/connect", json={"address": "COM12"})
    client.post("/change", json={"target": 3})
    execute = client.post("/execute", json={"duration_s": 4, "rpm": 70})

    assert execute.status_code == 200
    execute_records = [
        record
        for record in caplog.records
        if getattr(record, "stationkit_op", None) == "execute"
        and getattr(record, "stationkit_event", None) == "success"
    ]
    assert any(record.stationkit_layer == "controller" for record in execute_records)
    assert any(record.stationkit_layer == "http" for record in execute_records)


def test_cli_adapter_supports_serve_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """serve が uvicorn を使って HTTP service を起動すること。"""
    captured: dict[str, Any] = {}

    class FakeUvicorn:
        """uvicorn.run を模した最小ダブル。"""

        @staticmethod
        def run(app: Any, host: str, port: int) -> None:
            captured["app"] = app
            captured["host"] = host
            captured["port"] = port

    monkeypatch.setattr(cli_adapter, "import_module", lambda _name: FakeUvicorn)

    app = create_cli_app(AdvancedMockStationController())
    result = CliRunner().invoke(app, ["serve", "--host", "127.0.0.1", "--port", "9001"])

    assert result.exit_code == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9001
    assert captured["app"].title == "AdvancedMockStationController"
