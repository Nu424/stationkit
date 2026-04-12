"""stationkit のコア・HTTP/CLI アダプタの結合テスト。"""

from __future__ import annotations

import asyncio
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
    execute = gui_adapter._build_execute_handler(controller, operation_lock)

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
