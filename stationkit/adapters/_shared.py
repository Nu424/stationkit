"""HTTP/CLI アダプタで共有する型解決とシリアライズ補助。"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from stationkit.core.introspection import resolve_target_type


def normalize_result(value: Any) -> Any:
    """HTTP レスポンス用に値をプレーンな構造にそろえる。

    Pydantic モデルは ``model_dump()`` した dict に変換する。

    Args:
        value: 装置やハンドラの戻り値。

    Returns:
        dict / list / スカラーなど JSON 化しやすい値。
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def format_cli_output(value: Any) -> str:
    """CLI 向けに結果を文字列化する。

    dict または list は UTF-8 を維持した JSON 文字列にする。

    Args:
        value: 表示したい値。

    Returns:
        ターミナルに出力する文字列。
    """
    normalized = normalize_result(value)
    if isinstance(normalized, (dict, list)):
        return json.dumps(normalized, ensure_ascii=False)
    return str(normalized)
