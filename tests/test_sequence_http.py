"""sequence HTTP adapter のテスト。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from threading import Event
from time import sleep
from typing import Any

from fastapi.testclient import TestClient
from pydantic import BaseModel

from stationkit import (
    ControllerMetadata,
    ExecutionCancelledError,
    ExecutionContext,
    SequenceMode,
    create_sequence_http_app,
)
from stationkit.testing import MockStationController


class ExecuteInput(BaseModel):
    """execute 用の入力スキーマ。"""

    duration_s: int
    rpm: int | None = None


class OptionalBoolExecuteInput(BaseModel):
    """Optional[bool] を含む execute 入力スキーマ。"""

    enabled: bool | None = None


class ParameterizedExecuteController(MockStationController):
    """execute が必須パラメータを受け取る controller。"""

    async def _do_execute(self, params: ExecuteInput) -> dict[str, Any]:
        self._call_log.append(f"execute({params.duration_s}, {params.rpm})")
        return {
            "mock": True,
            "target": self._current_target,
            "params": params.model_dump(),
        }


class OptionalBoolExecuteController(MockStationController):
    """Optional[bool] を受け取る controller。"""

    async def _do_execute(
        self,
        params: OptionalBoolExecuteInput | None = None,
    ) -> dict[str, Any]:
        payload = None if params is None else params.model_dump()
        return {
            "mock": True,
            "target": self._current_target,
            "params": payload,
        }


class SlowExecuteController(MockStationController):
    """外部イベントまで execute を継続する controller。"""

    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self._release = Event()

    def finish_execution(self) -> None:
        """execute を終端へ進める。"""
        self._release.set()

    async def _do_execute(self) -> dict[str, Any]:
        self._call_log.append("execute()")
        self.started.set()
        while not self._release.is_set():
            await asyncio.sleep(0.01)
        return {"mock": True, "target": self._current_target}


class CancellableExecuteController(SlowExecuteController):
    """cancel hook を持つ controller。"""

    def __init__(self) -> None:
        super().__init__()
        self.cancel_calls = 0

    def cancel_execution(self) -> None:
        """cancel を受けたら execute を終了へ進める。"""
        self.cancel_calls += 1
        self.finish_execution()

    def get_metadata(self) -> ControllerMetadata:
        """終了駆動・時間駆動の両方を宣言する。"""
        return ControllerMetadata(
            sequence_modes=(
                SequenceMode.COMPLETION_DRIVEN,
                SequenceMode.TIME_DRIVEN,
            )
        )

    async def _do_execute(self) -> dict[str, Any]:
        self._call_log.append("execute()")
        self.started.set()
        while not self._release.is_set():
            await asyncio.sleep(0.01)
        raise ExecutionCancelledError("Execution cancelled.")


class ContextAwareExecuteController(MockStationController):
    """ExecutionContext を受け取る controller。"""

    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self._release = Event()
        self.last_context: ExecutionContext | None = None

    def finish_execution(self) -> None:
        """execute を終端へ進める。"""
        self._release.set()

    async def _do_execute(self, *, context: ExecutionContext) -> dict[str, Any]:
        self.last_context = context
        self._call_log.append("execute(context)")
        self.started.set()
        while not self._release.is_set():
            await asyncio.sleep(0.01)
        return {
            "mock": True,
            "target": self._current_target,
            "execution_id": context.execution_id,
        }


class ContextAwareCancellableController(ContextAwareExecuteController):
    """context 対応かつ cancel 可能な controller。"""

    def __init__(self) -> None:
        super().__init__()
        self.cancel_calls = 0

    def cancel_execution(self) -> None:
        """cancel を受けたら execute を終了へ進める。"""
        self.cancel_calls += 1
        self.finish_execution()

    def get_metadata(self) -> ControllerMetadata:
        """終了駆動・時間駆動の両方を宣言する。"""
        return ControllerMetadata(
            sequence_modes=(
                SequenceMode.COMPLETION_DRIVEN,
                SequenceMode.TIME_DRIVEN,
            )
        )

    async def _do_execute(self, *, context: ExecutionContext) -> dict[str, Any]:
        self.last_context = context
        self._call_log.append("execute(context)")
        self.started.set()
        while not self._release.is_set():
            await asyncio.sleep(0.01)
        raise ExecutionCancelledError("Execution cancelled.")


class TimeDrivenOnlyController(CancellableExecuteController):
    """時間駆動だけを公開する controller。"""

    def get_metadata(self) -> ControllerMetadata:
        return ControllerMetadata(sequence_modes=(SequenceMode.TIME_DRIVEN,))


class CompletionDrivenOnlyController(MockStationController):
    """終了駆動だけを公開する controller。"""

    def get_metadata(self) -> ControllerMetadata:
        return ControllerMetadata(sequence_modes=(SequenceMode.COMPLETION_DRIVEN,))


class TimeDrivenWithoutCancelController(MockStationController):
    """時間駆動を宣言するが cancel hook を持たない controller。"""

    def get_metadata(self) -> ControllerMetadata:
        return ControllerMetadata(sequence_modes=(SequenceMode.TIME_DRIVEN,))


def _wait_for_status_condition(
    client: TestClient,
    predicate,
    timeout_s: float = 1.5,
) -> dict[str, Any]:
    """`/api/status` が条件を満たすまで polling する。"""
    remaining = timeout_s
    latest: dict[str, Any] | None = None
    while remaining > 0:
        latest = client.get("/api/status").json()
        if predicate(latest):
            return latest
        sleep(0.02)
        remaining -= 0.02
    raise AssertionError(f"Condition not met. Latest status: {latest}")


def test_sequence_http_meta_and_status_bootstrap() -> None:
    """meta と status の初期化レスポンスを返せること。"""
    client = TestClient(create_sequence_http_app(ParameterizedExecuteController()))

    meta = client.get("/api/meta")
    status = client.get("/api/status")

    assert meta.status_code == 200
    assert meta.json()["controller_name"] == "ParameterizedExecuteController"
    assert meta.json()["target"]["fields"][0]["type"] == "int"
    assert meta.json()["execute"]["kind"] == "fields"
    assert [field["name"] for field in meta.json()["execute"]["fields"]] == [
        "duration_s",
        "rpm",
    ]

    assert status.status_code == 200
    assert status.json()["controller"]["controller_state"] == "DISCONNECTED"
    assert status.json()["manual_execution"] is None
    assert status.json()["sequence"] is None


def test_sequence_http_meta_uses_controller_sequence_modes() -> None:
    """controller の宣言順を API の選択肢として返すこと。"""
    default_meta = TestClient(
        create_sequence_http_app(MockStationController())
    ).get("/api/meta").json()
    completion_meta = TestClient(
        create_sequence_http_app(CompletionDrivenOnlyController())
    ).get("/api/meta").json()
    both_meta = TestClient(
        create_sequence_http_app(CancellableExecuteController())
    ).get("/api/meta").json()
    time_meta = TestClient(
        create_sequence_http_app(TimeDrivenOnlyController())
    ).get("/api/meta").json()

    assert default_meta["sequence_modes"] == [
        "COMPLETION_DRIVEN",
        "TIME_DRIVEN",
    ]
    assert completion_meta["sequence_modes"] == ["COMPLETION_DRIVEN"]
    assert both_meta["sequence_modes"] == [
        "COMPLETION_DRIVEN",
        "TIME_DRIVEN",
    ]
    assert time_meta["sequence_modes"] == ["TIME_DRIVEN"]


def test_sequence_http_meta_keeps_optional_bool_as_nullable_bool_field() -> None:
    """HTTP meta では Optional[bool] を nullable な bool field として公開する。"""
    client = TestClient(create_sequence_http_app(OptionalBoolExecuteController()))

    meta = client.get("/api/meta")

    assert meta.status_code == 200
    assert meta.json()["execute"]["kind"] == "fields"
    assert meta.json()["execute"]["fields"] == [
        {
            "name": "enabled",
            "label": "enabled",
            "type": "bool",
            "required": False,
            "default": None,
            "nullable": True,
        }
    ]


def test_sequence_http_connect_change_and_disconnect() -> None:
    """controller 操作の基本 API が動くこと。"""
    client = TestClient(create_sequence_http_app(MockStationController()))

    assert client.post("/api/controller/connect", json={"address": "COM50"}).json() == {
        "ok": True
    }
    assert client.post("/api/controller/change", json={"target": 4}).json() == {"ok": True}

    status = client.get("/api/status").json()
    assert status["controller"]["controller_state"] == "CONNECTED"
    assert status["controller"]["current_target"] == 4

    assert client.post("/api/controller/disconnect").json() == {"ok": True}
    assert client.get("/api/status").json()["controller"]["controller_state"] == "DISCONNECTED"


def test_sequence_http_manual_idle() -> None:
    """/api/controller/idle で idle 状態へ移せること。"""
    client = TestClient(create_sequence_http_app(MockStationController()))

    client.post("/api/controller/connect", json={"address": "COM51"})
    client.post("/api/controller/change", json={"target": 4})

    assert client.post("/api/controller/idle").json() == {"ok": True}
    assert client.get("/api/status").json()["controller"]["routing"] == "exhaust"

    client.post("/api/controller/disconnect")
    assert client.post("/api/controller/idle").status_code == 409


def test_sequence_http_validate_rejects_unsupported_sequence_mode() -> None:
    """controller が宣言していないモードを validation で拒否すること。"""
    client = TestClient(
        create_sequence_http_app(CompletionDrivenOnlyController())
    )
    now = datetime.now(UTC)
    definition = {
        "name": "timed",
        "mode": SequenceMode.TIME_DRIVEN.value,
        "steps": [
            {
                "target": 1,
                "start_at": (now + timedelta(seconds=1)).isoformat(),
                "end_at": (now + timedelta(seconds=2)).isoformat(),
            }
        ],
    }

    response = client.post("/api/sequence/validate", json={"definition": definition})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert {issue["code"] for issue in body["issues"]} >= {
        "unsupported_sequence_mode"
    }
    assert "cancel_not_supported" not in {
        issue["code"] for issue in body["issues"]
    }

    run_response = client.post(
        "/api/sequence/run", json={"definition": definition}
    )
    assert run_response.status_code == 422
    assert "not supported" in run_response.json()["detail"]


def test_sequence_http_validate_requires_cancel_for_declared_time_mode() -> None:
    """時間駆動を宣言しても cancel hook がなければ拒否すること。"""
    client = TestClient(
        create_sequence_http_app(TimeDrivenWithoutCancelController())
    )
    now = datetime.now(UTC)
    definition = {
        "name": "timed-without-cancel",
        "mode": SequenceMode.TIME_DRIVEN.value,
        "steps": [
            {
                "target": 1,
                "start_at": (now + timedelta(seconds=1)).isoformat(),
                "end_at": (now + timedelta(seconds=2)).isoformat(),
            }
        ],
    }

    body = client.post(
        "/api/sequence/validate", json={"definition": definition}
    ).json()

    assert body["ok"] is False
    assert {issue["code"] for issue in body["issues"]} >= {
        "cancel_not_supported"
    }


def test_sequence_http_run_and_stop_sequence() -> None:
    """sequence の run / stop と snapshot polling が動くこと。"""
    controller = CancellableExecuteController()
    client = TestClient(create_sequence_http_app(controller))
    client.post("/api/controller/connect", json={"address": "COM52"})
    definition = {
        "name": "run-stop",
        "steps": [
            {"label": "first", "target": 2},
            {"label": "second", "target": 5},
        ],
    }

    started = client.post("/api/sequence/run", json={"definition": definition})
    assert started.status_code == 200
    assert started.json()["state"] == "RUNNING"
    assert controller.started.wait(timeout=1.0)

    blocked_check = client.post(
        "/api/sequence/check-step",
        json={"definition": definition, "step_index": 0},
    )
    assert blocked_check.status_code == 409
    assert blocked_check.json() == {"detail": "Sequence is already running."}

    stop = client.post("/api/sequence/stop")
    assert stop.status_code == 200

    terminal = _wait_for_status_condition(
        client,
        lambda payload: payload["sequence"] is not None
        and payload["sequence"]["state"] == "CANCELLED",
    )
    assert terminal["sequence"]["steps"][0]["state"] == "CANCELLED"
    assert terminal["sequence"]["steps"][1]["state"] == "SKIPPED"


def test_sequence_http_check_step_starts_selected_step() -> None:
    """check-step が選択行だけを manual execution として起動できること。"""
    controller = SlowExecuteController()
    client = TestClient(create_sequence_http_app(controller))
    client.post("/api/controller/connect", json={"address": "COM53"})
    definition = {
        "name": "check-step",
        "steps": [
            {"label": "first", "target": 6},
            {"label": "second", "target": 9},
        ],
    }

    started = client.post(
        "/api/sequence/check-step",
        json={"definition": definition, "step_index": 1},
    )

    assert started.status_code == 200
    assert started.json()["execution_id"]
    assert controller.started.wait(timeout=1.0)

    status = client.get("/api/status").json()
    assert status["controller"]["current_target"] == 9
    assert status["manual_execution"]["state"] == "RUNNING"

    controller.finish_execution()
    terminal = _wait_for_status_condition(
        client,
        lambda payload: payload["manual_execution"] is not None
        and payload["manual_execution"]["state"] == "SUCCEEDED",
    )
    assert terminal["manual_execution"]["state"] == "SUCCEEDED"


def test_sequence_http_rejects_sequence_run_during_manual_execution() -> None:
    """manual execute 中は sequence run を拒否すること。"""
    controller = SlowExecuteController()
    client = TestClient(create_sequence_http_app(controller))
    client.post("/api/controller/connect", json={"address": "COM54"})
    client.post(
        "/api/sequence/check-step",
        json={
            "definition": {"name": "manual", "steps": [{"target": 1}]},
            "step_index": 0,
        },
    )
    assert controller.started.wait(timeout=1.0)

    response = client.post(
        "/api/sequence/run",
        json={"definition": {"name": "blocked", "steps": [{"target": 2}]}},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Execution is already running."}

    controller.finish_execution()
    _wait_for_status_condition(
        client,
        lambda payload: payload["manual_execution"] is not None
        and payload["manual_execution"]["state"] == "SUCCEEDED",
    )


def test_sequence_http_meta_does_not_expose_execution_context() -> None:
    """ExecutionContext は meta のユーザー入力 schema に現れないこと。"""
    client = TestClient(create_sequence_http_app(ContextAwareExecuteController()))

    meta = client.get("/api/meta")

    assert meta.status_code == 200
    body = meta.json()
    assert body["execute"]["kind"] == "none"
    assert "context" not in body["execute"]
    assert "fields" not in body["execute"] or all(
        field["name"] != "context" for field in body["execute"].get("fields", [])
    )


def test_sequence_http_check_step_context_has_no_schedule() -> None:
    """manual check-step では予定境界が None であること。"""
    controller = ContextAwareExecuteController()
    client = TestClient(create_sequence_http_app(controller))
    client.post("/api/controller/connect", json={"address": "COM55"})

    started = client.post(
        "/api/sequence/check-step",
        json={
            "definition": {
                "name": "check-context",
                "steps": [{"label": "only", "target": 8}],
            },
            "step_index": 0,
        },
    )
    assert started.status_code == 200
    assert controller.started.wait(timeout=1.0)
    assert controller.last_context is not None
    assert controller.last_context.execution_id == started.json()["execution_id"]
    assert controller.last_context.scheduled_start_at is None
    assert controller.last_context.scheduled_end_at is None
    assert controller.last_context.sequence_run_id is None

    controller.finish_execution()
    _wait_for_status_condition(
        client,
        lambda payload: payload["manual_execution"] is not None
        and payload["manual_execution"]["state"] == "SUCCEEDED",
    )


def test_sequence_http_time_driven_passes_schedule_in_context() -> None:
    """TIME_DRIVEN の予定時刻が controller context に届くこと。"""
    controller = ContextAwareCancellableController()
    client = TestClient(create_sequence_http_app(controller))
    client.post("/api/controller/connect", json={"address": "COM56"})
    now = datetime.now(UTC)
    start_at = now + timedelta(milliseconds=50)
    end_at = start_at + timedelta(milliseconds=200)
    definition = {
        "name": "timed-context",
        "mode": SequenceMode.TIME_DRIVEN.value,
        "steps": [
            {
                "id": "step-1",
                "target": 3,
                "start_at": start_at.isoformat(),
                "end_at": end_at.isoformat(),
            }
        ],
    }

    started = client.post("/api/sequence/run", json={"definition": definition})
    assert started.status_code == 200
    assert controller.started.wait(timeout=1.5)
    assert controller.last_context is not None
    assert controller.last_context.sequence_run_id == started.json()["run_id"]
    assert controller.last_context.sequence_step_id == "step-1"
    assert controller.last_context.sequence_step_index == 0
    assert controller.last_context.scheduled_start_at is not None
    assert controller.last_context.scheduled_end_at is not None
    assert abs(
        (
            controller.last_context.scheduled_start_at - start_at.astimezone(UTC)
        ).total_seconds()
    ) < 0.001
    assert abs(
        (
            controller.last_context.scheduled_end_at - end_at.astimezone(UTC)
        ).total_seconds()
    ) < 0.001

    terminal = _wait_for_status_condition(
        client,
        lambda payload: payload["sequence"] is not None
        and payload["sequence"]["state"] != "RUNNING",
        timeout_s=2.0,
    )
    assert terminal["sequence"]["state"] in {"SUCCEEDED", "CANCELLED"}
    assert controller.cancel_calls >= 1
