"""Sequence Web App 用の FastAPI サーバラッパ。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from stationkit import StationControllerBase, create_sequence_http_app

_DEFAULT_FRONTEND_DIST_DIR = Path(__file__).resolve().parent / "static"


def create_sequence_app_server(
    controller: StationControllerBase,
    *,
    frontend_dist_dir: str | Path | None = None,
    dev_frontend_origin: str | None = None,
) -> FastAPI:
    """シーケンス Web アプリ用の FastAPI サーバを生成する。

    create_sequence_http_appに、
    - CORS
    - static配信(Reactアプリのビルド出力を配信)
    を追加したものを返す。

    Args:
        controller: 対象コントローラ。
        frontend_dist_dir: production 用 frontend build 出力ディレクトリ。
            未指定時は同梱 static を使う。存在する場合は静的配信と
            SPA fallback を有効化する。
        dev_frontend_origin: 開発時 React dev server の origin。
            指定時は CORS を有効化する。

    Returns:
        `/api/...` を持つ FastAPI アプリ。必要に応じて SPA 配信設定を追加したもの。
    """
    app = create_sequence_http_app(controller)

    if dev_frontend_origin:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[dev_frontend_origin],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    dist_dir = _resolve_frontend_dist_dir(frontend_dist_dir)
    if dist_dir is None and frontend_dist_dir is None:
        dist_dir = _resolve_frontend_dist_dir(_DEFAULT_FRONTEND_DIST_DIR)
    if dist_dir is None:
        return app

    index_file = dist_dir / "index.html"

    @app.get("/", include_in_schema=False)
    async def serve_frontend_index() -> FileResponse:
        """frontend の index.html を返す。"""
        return FileResponse(index_file)

    @app.get("/{asset_path:path}", include_in_schema=False)
    async def serve_frontend_assets(asset_path: str) -> FileResponse:
        """frontend asset または SPA fallback を返す。"""
        requested = (dist_dir / asset_path).resolve()
        if requested.is_file() and requested.is_relative_to(dist_dir):
            return FileResponse(requested)
        return FileResponse(index_file)

    return app


def _resolve_frontend_dist_dir(
    frontend_dist_dir: str | Path | None,
) -> Path | None:
    """静的配信対象ディレクトリを検証して返す。"""
    if frontend_dist_dir is None:
        return None

    dist_dir = Path(frontend_dist_dir).resolve()
    index_file = dist_dir / "index.html"
    if not dist_dir.is_dir() or not index_file.is_file():
        return None
    return dist_dir
