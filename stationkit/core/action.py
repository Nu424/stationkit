"""アダプタ（HTTP/CLI 等）が参照する固有操作のメタデータ定義。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


@dataclass(slots=True)
class CustomAction:
    """コントローラ固有の操作を宣言するデータクラス。

    具体クラスは ``get_custom_actions()`` で一覧を返し、ファクトリが
    エンドポイントやサブコマンドを自動生成します。

    Attributes:
        name: 操作の識別子（URL パスや CLI サブコマンド名に使われる）。
        description: 人間向けの説明（OpenAPI 等に載る）。
        func: 実行される async 関数。第1引数は ``input_schema`` のインスタンス。
        input_schema: 入力の Pydantic モデル。
        output_schema: 出力の Pydantic モデル。省略時はスキーマ制約なし。
    """

    name: str
    description: str
    func: Callable[[BaseModel], Awaitable[Any]]
    input_schema: type[BaseModel]
    output_schema: type[BaseModel] | None = None
