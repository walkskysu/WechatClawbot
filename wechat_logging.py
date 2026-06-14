"""Logging helpers for WeChatBot operations."""

import logging
from typing import Any, Callable


def _format_bot_context(
    *,
    user_id: str | None = None,
    msg: Any = None,
    detail: str | None = None,
) -> str:
    parts: list[str] = []

    resolved_user_id = user_id or getattr(msg, "user_id", None)
    if resolved_user_id:
        parts.append(f"user_id={resolved_user_id}")

    msg_type = getattr(msg, "type", None)
    if msg_type:
        parts.append(f"msg_type={msg_type}")

    if detail:
        parts.append(detail)

    if not parts:
        return ""
    return " [" + ", ".join(parts) + "]"


async def call_wechat_bot_async(
    operation: str,
    func: Callable[..., Any],
    *args: Any,
    user_id: str | None = None,
    msg: Any = None,
    detail: str | None = None,
    **kwargs: Any,
) -> Any:
    context = _format_bot_context(user_id=user_id, msg=msg, detail=detail)
    try:
        result = func(*args, **kwargs)
        if hasattr(result, "__await__"):
            result = await result
    except Exception:
        logging.exception("WeChatBot %s failed%s", operation, context)
        raise

    logging.info("WeChatBot %s succeeded%s", operation, context)
    return result


def call_wechat_bot_sync(
    operation: str,
    func: Callable[..., Any],
    *args: Any,
    user_id: str | None = None,
    msg: Any = None,
    detail: str | None = None,
    **kwargs: Any,
) -> Any:
    context = _format_bot_context(user_id=user_id, msg=msg, detail=detail)
    try:
        result = func(*args, **kwargs)
    except Exception:
        logging.exception("WeChatBot %s failed%s", operation, context)
        raise

    logging.info("WeChatBot %s succeeded%s", operation, context)
    return result
