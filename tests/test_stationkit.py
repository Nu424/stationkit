"""stationkit のコア・HTTP/CLI アダプタの結合テスト。"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from typer.testing import CliRunner

from stationkit import (
    ControllerState,
    CustomAction,
    create_cli_app,
    create_http_app,
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


def test_cli_adapter_supports_core_and_custom_commands() -> None:
    """CLI で標準サブコマンドと JSON 引数の固有コマンドが動くこと。"""
    controller = AdvancedMockStationController()
    app = create_cli_app(controller)
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
