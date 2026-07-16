"""コントローラ実装の型ヒントを解釈する helper。

_do_change()のtargetや、_do_execute()の入力は、それぞれの具体的な実装ごとに異なる。
それぞれの実装においても型を適切に扱うため、introspection.pyでは、これらの型解決のための関数を定義している。
"""

from __future__ import annotations

import inspect
import warnings
from dataclasses import dataclass
from types import NoneType, UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from pydantic import BaseModel

from stationkit.core.execution_context import ExecutionContext


# -----------------------------------------------------------------------------
# execute 入力仕様の表現
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExecuteParamsSpec:
    """`_do_execute` の入力仕様を表す。

    Attributes:
        model_type: execute が受け取る Pydantic モデル型。入力を取らない場合は ``None``。
        required: execute 入力が必須である場合は ``True``。
        accepts_context: keyword-only の ``context`` を受け取る場合は ``True``。
    """

    model_type: type[BaseModel] | None
    required: bool
    accepts_context: bool = False

    @property
    def accepts_params(self) -> bool:
        """入力モデルを受け取る実装かどうかを返す。

        Returns:
            execute が入力モデルを受け取る場合は ``True``。
        """
        return self.model_type is not None


# -----------------------------------------------------------------------------
# change / execute シグネチャの公開 helper
# -----------------------------------------------------------------------------


def resolve_target_type(controller: Any) -> type[Any]:
    """``_do_change`` の ``target`` 型を解決する。

    Args:
        controller: 対象のコントローラインスタンス。

    Returns:
        ``change`` の入力として扱う Python 型。具体型が取れない場合は ``str``。

    Warns:
        UserWarning: ``_do_change`` の ``target`` に具体型ヒントが無い場合。
    """
    # `change` の入力型は `_do_change` の型ヒントを唯一の情報源にする。
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


def unwrap_optional_type(annotation: Any) -> tuple[Any, bool]:
    """`Optional[T]` または `T | None` を分解する。

    Args:
        annotation: 判定対象の型注釈。

    Returns:
        `(実体型, None を許容するか)` のタプルを返す。
        `Optional[T]` / `T | None` と解釈できない場合は
        `(annotation, False)` を返す。
    """
    # Optional 判定は core / adapter 双方で使うため、ここで一元化する。
    origin = get_origin(annotation)
    if origin not in {Union, UnionType}:
        return annotation, False

    args = get_args(annotation)
    non_none_args = [arg for arg in args if arg is not NoneType]
    if len(non_none_args) == 1 and len(non_none_args) != len(args):
        # 全args数とnon_none_args数が一致しない場合は、一部がNoneTypeであり、optionalであることを示す
        return non_none_args[0], True
    return annotation, False


def resolve_execute_params_spec(controller: Any) -> ExecuteParamsSpec:
    """``_do_execute`` の execute 入力仕様を解決する。

    Args:
        controller: 対象のコントローラインスタンス。

    Returns:
        execute が入力を受け取るかどうか、および必須かどうかを表す仕様。

    Raises:
        TypeError: ``_do_execute`` の引数個数や型ヒントが想定外の場合。
    """
    signature = inspect.signature(controller._do_execute)
    hints = get_type_hints(controller._do_execute)
    positional_params: list[inspect.Parameter] = []
    accepts_context = False

    for parameter in signature.parameters.values():
        # キーワード専用パラメータの場合、"context"を期待する
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY:
            if parameter.name != "context":
                raise TypeError(
                    f"{type(controller).__name__}._do_execute only supports "
                    "keyword-only parameter named 'context'."
                )
            # contextの型注釈を取得し、ExecutionContext型であることを検証する
            _validate_context_annotation(
                type(controller).__name__,
                hints.get(parameter.name, Any),
            )
            accepts_context = True
            continue
        
        # 可変位置引数や可変キーワード引数は使用できない
        if parameter.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            raise TypeError(
                f"{type(controller).__name__}._do_execute must not use "
                "*args or **kwargs."
            )
        positional_params.append(parameter)

    if not positional_params:
        # 位置引数がない場合、入力を受け取らない実装であることを示す
        return ExecuteParamsSpec(
            model_type=None,
            required=False,
            accepts_context=accepts_context,
        )
    # 位置引数が1つの場合、その引数を入力として受け取る実装であることを示す
    if len(positional_params) != 1:
        raise TypeError(
            f"{type(controller).__name__}._do_execute must accept zero or one "
            "positional parameter (plus optional keyword-only context)."
        )

    parameter = positional_params[0]
    annotation = hints.get(parameter.name, Any)

    # execute で許容するのは「Pydantic モデル 1 個」またはその optional だけに絞る。
    model_type, accepts_none = _resolve_execute_model_type(annotation)
    if model_type is None:
        raise TypeError(
            f"{type(controller).__name__}._do_execute parameter must be a "
            "Pydantic BaseModel type or an optional BaseModel type."
        )

    if parameter.default not in (inspect.Signature.empty, None):
        raise TypeError(
            f"{type(controller).__name__}._do_execute default parameter value must "
            "be None when provided."
        )

    # デフォルト値と `None` 許容有無から、公開 API 上の必須性を決める。
    required = parameter.default is inspect.Signature.empty and not accepts_none
    return ExecuteParamsSpec(
        model_type=model_type,
        required=required,
        accepts_context=accepts_context,
    )


def normalize_execute_params(controller: Any, params: Any | None) -> BaseModel | None:
    """公開 `execute` API に渡された値を `_do_execute` 用に正規化する。

    Args:
        controller: 対象のコントローラインスタンス。
        params: 公開 API に渡された execute 入力。Pydantic モデル、dict、
            または ``None`` を想定する。

    Returns:
        ``_do_execute`` に渡せる Pydantic モデル、または入力不要時の ``None``。

    Raises:
        TypeError: 入力不要の controller に対して params を渡した場合、または
            必須入力が不足している場合。
        pydantic.ValidationError: Pydantic モデル検証に失敗した場合。
    """
    spec = resolve_execute_params_spec(controller)

    # 入力を受け取らない実装では、明示的な params 指定をエラーにする。
    if not spec.accepts_params:
        if params is not None:
            raise TypeError(
                f"{type(controller).__name__}.execute does not accept parameters."
            )
        return None

    if params is None:
        if spec.required:
            raise TypeError(
                f"{type(controller).__name__}.execute requires execute parameters."
            )
        return None

    # 既にモデルならそのまま、別モデルなら dict 化して期待モデルへ再検証する。
    model_type = spec.model_type
    if model_type is None:
        return None
    if isinstance(params, model_type):
        return params
    if isinstance(params, BaseModel):
        params = params.model_dump()
    return model_type.model_validate(params)


# -----------------------------------------------------------------------------
# execute 型注釈の内部解決 helper
# -----------------------------------------------------------------------------


def _validate_context_annotation(controller_name: str, annotation: Any) -> None:
    """keyword-only ``context`` の型注釈を検証する。

    Args:
        controller_name: エラーメッセージ用のコントローラ名。
        annotation: ``context`` 引数の型注釈。

    Raises:
        TypeError: ``ExecutionContext`` 以外の型が指定された場合。
    """
    if annotation is ExecutionContext:
        return
    raise TypeError(
        f"{controller_name}._do_execute keyword-only parameter 'context' "
        "must be annotated as ExecutionContext."
    )


def _resolve_execute_model_type(
    annotation: Any,
) -> tuple[type[BaseModel] | None, bool]:
    """execute 型注釈からモデル型と ``None`` 許容有無を抽出する。

    Args:
        annotation: `_do_execute` 引数に付いた型注釈。

    Returns:
        `(モデル型, None を許容するか)` のタプル。解釈できない場合は
        ``(None, False)``。
    """
    # annotation には `_do_execute` の引数注釈としてさまざまな値が入りうる。
    # 例:
    # 1. BaseModel のサブクラス
    # 2. BaseModel のサブクラス | None
    # 3. 型注釈なし時の Any
    # 4. int / str / dict[...] / NoneType など、今回の execute では受け入れない型
    # この関数が受け入れたいのは 1 と 2 だけ。

    # 型注釈が無い場合は Any として見えるので、この時点で不受理にする。
    if annotation is Any:
        return None, False
    # ここでは「単体の Pydantic モデル型」だけを受け入れる。
    if _is_pydantic_model(annotation):
        return annotation, False

    # Optional[T] / T | None をほどき、`モデル型 1 個 + None` だけを受け入れる。
    unwrapped, accepts_none = unwrap_optional_type(annotation)
    if accepts_none and _is_pydantic_model(unwrapped):
        return unwrapped, True

    # ここに来るのは、Pydantic モデルでも Optional/Union でもない型。
    # 例: int, str, bool, list, dict, NoneType など。
    return None, False


def _is_pydantic_model(annotation: Any) -> bool:
    """型注釈が Pydantic モデル型かどうかを返す。

    Args:
        annotation: 判定対象の型注釈。

    Returns:
        ``BaseModel`` のサブクラスであれば ``True``。
    """
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)
