"""Sequence Web App のサンプル launcher。"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from apps.sequence_app.server.app import create_sequence_app_server
from stationkit import MockStationController


def main() -> None:
    """Mock controller を sequence web app として起動する。"""
    controller = MockStationController()
    dist_dir = Path(__file__).resolve().parent / "apps" / "sequence_app" / "web" / "dist"
    app = create_sequence_app_server(
        controller,
        frontend_dist_dir=dist_dir if dist_dir.is_dir() else None,
        dev_frontend_origin=os.environ.get("STATIONKIT_SEQUENCE_DEV_ORIGIN"),
    )
    uvicorn.run(
        app,
        host=os.environ.get("STATIONKIT_SEQUENCE_HOST", "127.0.0.1"),
        port=int(os.environ.get("STATIONKIT_SEQUENCE_PORT", "8000")),
    )


if __name__ == "__main__":
    main()
