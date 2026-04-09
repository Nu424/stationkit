"""HTTP/CLI アダプタで共有する型解決とシリアライズ補助。"""

from __future__ import annotations

import json
import warnings
from typing import Any, get_type_hints

from pydantic import BaseModel

from stationkit.core.base import StationControllerBase


def resolve_target_type(controller: StationControllerBase) -> type[Any]:
    """``_do_change`` の ``target`` 型を解決する。

    具体クラスは ``_do_change(self, target: int)`` のように型ヒントを付けること。
    ``Any`` のままの場合は警告を出し ``str`` にフォールバックする。

    Args:
        controller: 対象のコントローラインスタンス。

    Returns:
        ``change`` 操作の引数として使う Python 型。

    Warns:
        UserWarning: ``target`` に具体型が付いていない場合。
    """
    # ---指定したStationControllerBaseの、_do_changeメソッドのtarget引数の型を取得する
    hints = get_type_hints(controller._do_change)
    target_type = hints.get("target", Any)
    if target_type is Any:
        warnings.warn(
            (
                f"{type(controller).__name__}._do_change is missing a concrete target "
                "type hint. Falling back to str."
            ),
            stacklevel=2,
        )
        return str
    return target_type


def normalize_result(value: Any) -> Any:
    """HTTP レスポンス用に値をプレーンな構造にそろえる。

    Pydantic モデルは ``model_dump()`` した dict に変換する。

    Args:
        value: 装置やハンドラの戻り値。

    Returns:
        dict / list / スカラーなど JSON 化しやすい値。
    """
    if isinstance(value, BaseModel):
        return value.model_dump()
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
