"""stationkit のコア・HTTP/CLI アダプタの結合テスト。"""

from __future__ import annotations

import asyncio
import logging
from threading import Lock
from typing import Any

import gradio as gr
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from typer.testing import CliRunner

import stationkit.adapters.cli as cli_adapter
import stationkit.adapters.gui as gui_adapter
from stationkit import (
    ControllerState,
    CustomAction,
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
    assert controller.state == ControllerState.DISCONNECTED
    assert controller.call_log == [
        "connect(COM3)",
        "change(4)",
        "execute()",
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


def test_sync_api_rejects_active_event_loop() -> None:
    """イベントループ実行中に同期 API を呼ぶと RuntimeError になること。"""
    controller = MockStationController()

    async def call_sync_api() -> None:
        with pytest.raises(RuntimeError, match=r"inside an event loop"):
            controller.connect("COM3")

    asyncio.run(call_sync_api())


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
    assert controller.call_log == [
        "connect(COM7)",
        "change(3)",
        "execute()",
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
    calls: list[tuple[str, str, str, Any | None]] = []
    service_state = {
        "controller_state": "DISCONNECTED",
        "current_target": None,
        "call_log": [],
    }

    def fake_request(
        method: str,
        server_url: str,
        path: str,
        payload: Any | None = None,
    ) -> Any:
        calls.append((method, server_url, path, payload))

        if path == "/connect":
            service_state["controller_state"] = "CONNECTED"
            service_state["call_log"].append(f"connect({payload['address']})")
            return {"ok": True}

        if path == "/change":
            service_state["current_target"] = payload["target"]
            service_state["call_log"].append(f"change({payload['target']})")
            return {"ok": True}

        if path == "/execute":
            service_state["call_log"].append("execute()")
            return {"mock": True, "target": service_state["current_target"]}

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
        ("POST", "http://svc.test", "/connect", {"address": "COM4"}),
        ("POST", "http://svc.test", "/change", {"target": 6}),
        ("POST", "http://svc.test", "/execute", None),
        (
            "POST",
            "http://svc.test",
            "/actions/calibrate",
            {"level": 5, "force": True},
        ),
        ("GET", "http://svc.test", "/status", None),
    ]


def test_cli_execute_supports_required_and_optional_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """service-backed CLI の execute が required / optional payload を送れること。"""
    calls: list[tuple[str, str, str, Any | None]] = []

    def fake_request(
        method: str,
        server_url: str,
        path: str,
        payload: Any | None = None,
    ) -> Any:
        calls.append((method, server_url, path, payload))
        if path == "/execute":
            return {"payload": payload}
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
    assert ("POST", "http://svc.test", "/execute", {"duration_s": 5, "rpm": 80}) in calls
    assert ("POST", "http://svc.test", "/execute", None) in calls


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
