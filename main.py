"""Sequence Web App のサンプル launcher。"""

from __future__ import annotations

import os

import uvicorn

from apps.sequence_app.server import create_sequence_app_server
from stationkit import MockStationController


def main() -> None:
    """Mock controller を sequence web app として起動する。"""
    controller = MockStationController()
    app = create_sequence_app_server(
        controller,
        dev_frontend_origin=os.environ.get("STATIONKIT_SEQUENCE_DEV_ORIGIN"),
    )
    uvicorn.run(
        app,
        host=os.environ.get("STATIONKIT_SEQUENCE_HOST", "127.0.0.1"),
        port=int(os.environ.get("STATIONKIT_SEQUENCE_PORT", "8000")),
    )


if __name__ == "__main__":
    main()
