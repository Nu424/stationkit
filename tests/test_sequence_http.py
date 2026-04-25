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
    ExecutionCancelledError,
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

    async def _do_execute(self) -> dict[str, Any]:
        self._call_log.append("execute()")
        self.started.set()
        while not self._release.is_set():
            await asyncio.sleep(0.01)
        raise ExecutionCancelledError("Execution cancelled.")


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


def test_sequence_http_validate_and_time_driven_cancel_unsupported() -> None:
    """time-driven を cancel 未対応 controller が validation で拒否されること。"""
    client = TestClient(create_sequence_http_app(MockStationController()))
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
    assert {issue["code"] for issue in body["issues"]} >= {"cancel_not_supported"}


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
