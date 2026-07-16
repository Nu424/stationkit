"""core 層と sequence 層で共有する controller capability。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SequenceMode(str, Enum):
    """シーケンス全体の進め方。"""

    COMPLETION_DRIVEN = "COMPLETION_DRIVEN"
    TIME_DRIVEN = "TIME_DRIVEN"


@dataclass(frozen=True, slots=True)
class ControllerMetadata:
    """station controller が宣言する capability。

    先頭のシーケンスモードは、メタデータ駆動クライアントの既定値として使われる。
    """

    sequence_modes: tuple[SequenceMode, ...] = (SequenceMode.COMPLETION_DRIVEN, SequenceMode.TIME_DRIVEN)

    def __post_init__(self) -> None:
        """UI の既定値を一意に決められないメタデータを拒否する。"""
        if not self.sequence_modes:
            raise ValueError("sequence_modes must contain at least one mode.")
        if any(not isinstance(mode, SequenceMode) for mode in self.sequence_modes):
            raise TypeError("sequence_modes must contain only SequenceMode values.")
        if len(set(self.sequence_modes)) != len(self.sequence_modes):
            raise ValueError("sequence_modes must not contain duplicates.")
