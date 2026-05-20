"""LLM client — OpenAI-compatible chat completion."""

import configparser
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from openai import AsyncOpenAI

_ROOT_DIR = Path(__file__).resolve().parent
_LOG_DIR = _ROOT_DIR / "log"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_LOG_FILE = _LOG_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
_HISTORY_DIR = _ROOT_DIR / "history"
_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
_LATEST_CHAT_FILE = _HISTORY_DIR / "latest_chat.json"
_SERVER_CONF_FILE = _ROOT_DIR / "server.conf"
_DEFAULT_LATEST_CHAT_LIMIT = 20

_client = AsyncOpenAI(
    api_key=os.environ["LLM_API_KEY"],
    base_url=os.environ["LLM_API_URL"],
)
_model = os.environ["LLM_MODEL"]


def _read_json_array(path: Path) -> list:
    if not path.exists():
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    return data if isinstance(data, list) else []


def _write_json_array(path: Path, items: list) -> None:
    path.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _today_history_file() -> Path:
    return _HISTORY_DIR / f"{datetime.now().strftime('%Y%m%d')}.json"


def _load_latest_chat_limit() -> int:
    if not _SERVER_CONF_FILE.exists():
        return _DEFAULT_LATEST_CHAT_LIMIT

    conf = configparser.ConfigParser()
    try:
        conf.read(_SERVER_CONF_FILE, encoding="utf-8")
    except (configparser.Error, OSError):
        return _DEFAULT_LATEST_CHAT_LIMIT

    try:
        value = conf.getint("server", "latest_chat_limit",
                            fallback=_DEFAULT_LATEST_CHAT_LIMIT)
    except ValueError:
        return _DEFAULT_LATEST_CHAT_LIMIT

    return max(1, value)


def _sanitize_messages(messages: list) -> list[dict]:
    sanitized: list[dict] = []
    for item in messages:
        if not isinstance(item, dict):
            continue

        role = item.get("role")
        content = item.get("content")
        if role in {"system", "user", "assistant"} and isinstance(content, str):
            sanitized.append({"role": role, "content": content})
    return sanitized


def _read_latest_chat() -> list[dict]:
    return _sanitize_messages(_read_json_array(_LATEST_CHAT_FILE))


def _append_daily_history(new_messages: list[dict]) -> None:
    history_file = _today_history_file()
    history_messages = _sanitize_messages(_read_json_array(history_file))
    history_messages.extend(_sanitize_messages(new_messages))
    _write_json_array(history_file, history_messages)


def _update_latest_chat(new_messages: list[dict]) -> None:
    limit = _load_latest_chat_limit()
    latest_messages = _read_latest_chat()
    latest_messages.extend(_sanitize_messages(new_messages))
    _write_json_array(_LATEST_CHAT_FILE, latest_messages[-limit:])


def _append_log(record: dict) -> None:
    """Persist one LLM call record in a readable text block."""
    request_text = json.dumps(record.get(
        "request", {}), ensure_ascii=False, indent=2, default=str)
    response_text = json.dumps(record.get(
        "response", {}), ensure_ascii=False, indent=2, default=str)

    block = (
        f"[{record.get('requested_at', '')}] call_id={record.get('call_id', '')}\n"
        "---------------------- Request ----------------------\n"
        f"{request_text}\n"
        "---------------------- Response ---------------------\n"
        f"{response_text}\n"
        "-----------------------------------------------------\n\n"
    )

    with _LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(block)


async def llm_reply(text: str) -> str:
    call_id = str(uuid4())
    requested_at = datetime.now(timezone.utc).isoformat()
    latest_messages = _read_latest_chat()
    request_payload = {
        "model": _model,
        "messages": latest_messages + [{"role": "user", "content": text}],
    }

    response = await _client.chat.completions.create(**request_payload)
    reply_text = response.choices[0].message.content or ""

    try:
        new_messages = [
            {"role": "user", "content": text},
            {"role": "assistant", "content": reply_text},
        ]
        _append_daily_history(new_messages)
        _update_latest_chat(new_messages)
    except Exception:
        # History persistence is best-effort and should not break reply generation.
        pass

    try:
        _append_log(
            {
                "call_id": call_id,
                "requested_at": requested_at,
                "responded_at": datetime.now(timezone.utc).isoformat(),
                "request": request_payload,
                "response": response.model_dump(),
            }
        )
    except Exception:
        # Logging is best-effort and should not break reply generation.
        pass

    return reply_text
