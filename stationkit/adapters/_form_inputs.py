"""adapter 間で共有する入力フォーム解釈 helper。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from stationkit.core.introspection import unwrap_optional_type

PrimitiveInputType: TypeAlias = Literal["str", "int", "float", "bool", "json"]


# -----------------------------------------------------------------------------
# 入力フォームの中間表現
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InputFieldSpec:
    """adapter 共通で使う入力フィールド仕様を表す。

    それぞれの実装では、これを中間にしながら、各自の入力フォームに変換していく。

    Args:
        name: フィールド名。
        annotation: 元の型注釈。
        base_type: Optional をほどいた後の実体型。
        label: UI 表示ラベル。
        required: 必須入力かどうか。
        default: 生の default 値。
        nullable: `None` を許容するかどうか。
        primitive_type: 単純入力で扱える型か、JSON フォールバックかを表す分類。
    """

    name: str
    annotation: Any
    base_type: Any
    label: str
    required: bool
    default: Any
    nullable: bool
    primitive_type: PrimitiveInputType


# -----------------------------------------------------------------------------
# Pydantic field / 型注釈の共通解釈
# -----------------------------------------------------------------------------


def field_default(field: FieldInfo) -> Any:
    """Pydantic field の default 値を取り出す。

    Args:
        field: `model_fields` の field 定義。

    Returns:
        必須 field の場合は `None`、それ以外は解決済み default 値を返す。
    """
    if field.is_required():
        return None
    return field.get_default(call_default_factory=True)


def resolve_primitive_input_type(annotation: Any) -> PrimitiveInputType:
    """型注釈を単純入力型か JSON フォールバックかへ分類する。

    Args:
        annotation: 判定対象の型注釈。Optional の場合も受け付ける。

    Returns:
        `str` / `int` / `float` / `bool` なら対応する型名、
        それ以外は `"json"` を返す。
    """
    base_type, _ = unwrap_optional_type(annotation)
    if base_type is str:
        return "str"
    if base_type is int:
        return "int"
    if base_type is float:
        return "float"
    if base_type is bool:
        return "bool"
    return "json"


def supports_scalar_input(
    value_type: Any,
    *,
    allow_optional_bool: bool = True,
) -> bool:
    """単一フィールド入力で扱える型かどうかを返す。

    Args:
        value_type: 判定対象の型注釈。
        allow_optional_bool: `bool | None` を単一入力として扱うかどうか。

    Returns:
        単一の scalar 入力で扱える場合は `True`、複雑型や
        `allow_optional_bool=False` 時の `Optional[bool]` は `False` を返す。
    """
    base_type, nullable = unwrap_optional_type(value_type)
    if nullable and base_type is bool and not allow_optional_bool:
        return False
    return resolve_primitive_input_type(base_type) != "json"


def input_field_specs_from_model(model_type: type[BaseModel]) -> list[InputFieldSpec]:
    """Pydantic モデルから入力フィールド仕様一覧(=list[InputFieldSpec])を生成する。

    Args:
        model_type: 解釈対象の Pydantic モデル型。

    Returns:
        モデル定義順の `InputFieldSpec` 一覧を返す。
    """
    result: list[InputFieldSpec] = []
    for name, field in model_type.model_fields.items():
        annotation = field.annotation
        # ---Optionalをほどいて、base_typeとnullableを取得する
        base_type, nullable = unwrap_optional_type(annotation)
        # ---InputFieldSpecを作成して、resultに追加する
        result.append(
            InputFieldSpec(
                name=name,
                annotation=annotation,
                base_type=base_type,
                label=field.title or name,
                required=field.is_required(),
                default=field_default(field),
                nullable=nullable,
                primitive_type=resolve_primitive_input_type(annotation),
            )
        )
    return result


def model_uses_scalar_fields(
    model_type: type[BaseModel],
    *,
    allow_optional_bool: bool = True,
) -> bool:
    """モデル全体を scalar field 群で表現できるかどうかを返す。

    Args:
        model_type: 判定対象の Pydantic モデル型。
        allow_optional_bool: `bool | None` を scalar field とみなすかどうか。

    Returns:
        すべての field が単一入力で扱える場合は `True` を返す。
    """
    # `all([])` は True なので、空モデルは field 群として扱える。
    return all(
        supports_scalar_input(
            spec.annotation,
            allow_optional_bool=allow_optional_bool,
        )
        for spec in input_field_specs_from_model(model_type)
    )
