"""stationkit のログ規約をまとめる helper。"""

from __future__ import annotations

import logging
from typing import Any

# -----------------------------------------------------------------------------
# ルート logger の初期化
# -----------------------------------------------------------------------------


LOGGER_ROOT_NAME = "stationkit"

_root_logger = logging.getLogger(LOGGER_ROOT_NAME)
# 呼び出し元へ、ライブラリ側から勝手にログを出さないようにするため、NullHandlerを追加する
if not any(isinstance(handler, logging.NullHandler) for handler in _root_logger.handlers):
    _root_logger.addHandler(logging.NullHandler())


# -----------------------------------------------------------------------------
# logger 取得 helper
# -----------------------------------------------------------------------------


def get_controller_logger(controller_name: str) -> logging.Logger:
    """コントローラ層用の logger を返す。

    Args:
        controller_name: コントローラクラス名など、logger 名に埋め込む識別子。

    Returns:
        ``stationkit.controller.<controller_name>`` 形式の logger。
    """
    return logging.getLogger(f"{LOGGER_ROOT_NAME}.controller.{controller_name}")


def get_adapter_logger(adapter_name: str) -> logging.Logger:
    """アダプタ層用の logger を返す。

    Args:
        adapter_name: HTTP / CLI / GUI などのアダプタ識別子。

    Returns:
        ``stationkit.<adapter_name>`` 形式の logger。
    """
    return logging.getLogger(f"{LOGGER_ROOT_NAME}.{adapter_name}")


# -----------------------------------------------------------------------------
# 操作ライフサイクルのログ出力
# -----------------------------------------------------------------------------


def log_operation_start(
    logger: logging.Logger,
    *,
    layer: str,
    operation_name: str,
    controller_name: str,
    context: dict[str, Any] | None = None,
) -> None:
    """操作開始ログを出力する。

    Args:
        logger: 出力先 logger。
        layer: ログを出した層。例: ``controller``、``http``、``cli``。
        operation_name: 操作名。例: ``connect``、``execute``。
        controller_name: 対象コントローラ名。
        context: 追加で付与したい補足情報。
    """
    logger.info(
        "stationkit operation started",
        extra=_build_log_extra(
            layer=layer,
            operation_name=operation_name,
            controller_name=controller_name,
            event="start",
            context=context,
        ),
    )


def log_operation_success(
    logger: logging.Logger,
    *,
    layer: str,
    operation_name: str,
    controller_name: str,
    duration_ms: float,
    context: dict[str, Any] | None = None,
) -> None:
    """操作成功ログを出力する。

    Args:
        logger: 出力先 logger。
        layer: ログを出した層。例: ``controller``、``http``、``cli``。
        operation_name: 操作名。例: ``connect``、``execute``。
        controller_name: 対象コントローラ名。
        duration_ms: 操作完了までの経過時間（ミリ秒）。
        context: 追加で付与したい補足情報。
    """
    logger.info(
        "stationkit operation completed",
        extra=_build_log_extra(
            layer=layer,
            operation_name=operation_name,
            controller_name=controller_name,
            event="success",
            success=True,
            duration_ms=duration_ms,
            context=context,
        ),
    )


def log_operation_failure(
    logger: logging.Logger,
    *,
    layer: str,
    operation_name: str,
    controller_name: str,
    duration_ms: float,
    context: dict[str, Any] | None = None,
    exc: BaseException,
) -> None:
    """操作失敗ログを出力する。

    Args:
        logger: 出力先 logger。
        layer: ログを出した層。例: ``controller``、``http``、``cli``。
        operation_name: 操作名。例: ``connect``、``execute``。
        controller_name: 対象コントローラ名。
        duration_ms: 失敗までの経過時間（ミリ秒）。
        context: 追加で付与したい補足情報。
        exc: ログへ添付する例外オブジェクト。
    """
    logger.exception(
        "stationkit operation failed",
        exc_info=exc,
        extra=_build_log_extra(
            layer=layer,
            operation_name=operation_name,
            controller_name=controller_name,
            event="failure",
            success=False,
            duration_ms=duration_ms,
            context=context,
        ),
    )


def _build_log_extra(
    *,
    layer: str,
    operation_name: str,
    controller_name: str,
    event: str,
    success: bool | None = None,
    duration_ms: float | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """共通 `extra` 辞書を構築する。

    Args:
        layer: ログを出した層。例: ``controller``、``http``、``cli``。
        operation_name: 操作名。
        controller_name: 対象コントローラ名。
        event: 操作ライフサイクル上のイベント種別。例: ``start``、``success``、
            ``failure``。
        success: 成否が確定している場合の真偽値。
        duration_ms: 経過時間（ミリ秒）。
        context: 呼び出し側が付けたい追加コンテキスト。

    Returns:
        `logging` の ``extra`` に渡せる辞書。
    """
    # まず全ログで必ず揃えたい基礎フィールドを積む。
    extra: dict[str, Any] = {
        "stationkit_layer": layer,
        "stationkit_op": operation_name,
        "stationkit_controller": controller_name,
        "stationkit_event": event,
    }

    # 成否と時間は、開始ログでは未確定なので必要なときだけ付与する。
    if success is not None:
        extra["stationkit_success"] = success
    if duration_ms is not None:
        extra["stationkit_duration_ms"] = round(duration_ms, 3)

    # 補足情報は `stationkit_` 接頭辞で namespaced して衝突を避ける。
    if context:
        for key, value in context.items():
            if value is not None:
                extra[f"stationkit_{key}"] = value
    return extra
