"""LLM client — OpenAI-compatible chat completion with agentic tool loop."""

import asyncio
import configparser
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable
from uuid import uuid4

from openai import AsyncOpenAI

from tools import (
    _call_openrouter_web_search,
    _execute_tool,
    set_log_debug as _set_tools_log_debug,
)

_ROOT_DIR = Path(__file__).resolve().parent
_LOG_DIR = _ROOT_DIR / "log"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_LOG_FILE = _LOG_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
_HISTORY_DIR = _ROOT_DIR / "history"
_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
_LATEST_CHAT_FILE = _HISTORY_DIR / "latest_chat.json"
_SERVER_CONF_FILE = _ROOT_DIR / "server.conf"
_DEFAULT_LATEST_CHAT_LIMIT = 20

# --- Agent configuration (loaded once at startup) ---
_AGENT_MD_FILE = _ROOT_DIR / "agent" / "agent.md"
_TOOLS_MD_FILE = _ROOT_DIR / "agent" / "tools.md"
_TOOLS_JSON_FILE = _ROOT_DIR / "agent" / "tools.json"
_SKILLS_MD_FILE = _ROOT_DIR / "agent" / "skills.md"
_AVAILABLE_SKILLS_XML_FILE = _ROOT_DIR / "agent" / "available_skills.xml"
_SKILLS_DIR = _ROOT_DIR / "skills"

_DEFAULT_MAX_TOOL_CALLS = 20
_DEFAULT_MAX_SKILL_READS = 5
_DEFAULT_TOOL_ACK = "好的，我开始工作了"


def _load_replies() -> dict[str, str]:
    """Return reply strings from the [replies] section of server.conf."""
    conf = configparser.ConfigParser()
    if _SERVER_CONF_FILE.exists():
        try:
            conf.read(_SERVER_CONF_FILE, encoding="utf-8")
        except (configparser.Error, OSError):
            pass
    return {
        "tool_ack": conf.get("replies", "tool_ack", fallback=_DEFAULT_TOOL_ACK),
    }


def _parse_skill_frontmatter(skill_md_path: Path) -> dict[str, str]:
    """Extract name and description from a SKILL.md frontmatter block."""
    try:
        text = skill_md_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    # Find the first --- delimiter (allow garbage before it)
    fm_match = re.search(r"---\n(.*?)\n---", text, re.DOTALL)
    if not fm_match:
        return {}

    fm_block = fm_match.group(1)
    result: dict[str, str] = {}
    for field in ("name", "description"):
        m = re.search(rf"^{field}:\s*(.+)$", fm_block, re.MULTILINE)
        if m:
            result[field] = m.group(1).strip()
    return result


def _update_available_skills_xml() -> None:
    """Scan skills/ subdirectories and rebuild available_skills.xml."""
    if not _SKILLS_DIR.exists():
        return

    entries: list[str] = []
    for skill_dir in sorted(_SKILLS_DIR.iterdir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_dir.is_dir() or not skill_md.exists():
            continue

        meta = _parse_skill_frontmatter(skill_md)
        name = meta.get("name") or skill_dir.name
        description = meta.get("description") or ""
        location = str(skill_md.resolve())

        entries.append(
            f"  <skill>\n"
            f"    <name>{name}</name>\n"
            f"    <description>{description}</description>\n"
            f"    <location>{location}</location>\n"
            f"  </skill>"
        )

    xml = "<available_skills>\n" + \
        "\n".join(entries) + "\n</available_skills>\n"
    try:
        _AVAILABLE_SKILLS_XML_FILE.write_text(xml, encoding="utf-8")
    except OSError:
        pass


def _load_agent_limits() -> tuple[int, int]:
    """Return (max_tool_calls, max_skill_reads) from server.conf."""
    if not _SERVER_CONF_FILE.exists():
        return _DEFAULT_MAX_TOOL_CALLS, _DEFAULT_MAX_SKILL_READS

    conf = configparser.ConfigParser()
    try:
        conf.read(_SERVER_CONF_FILE, encoding="utf-8")
    except (configparser.Error, OSError):
        return _DEFAULT_MAX_TOOL_CALLS, _DEFAULT_MAX_SKILL_READS

    max_tools = conf.getint("agent", "max_tool_calls",
                            fallback=_DEFAULT_MAX_TOOL_CALLS)
    max_skills = conf.getint("agent", "max_skill_reads",
                             fallback=_DEFAULT_MAX_SKILL_READS)
    return max(1, max_tools), max(1, max_skills)


# Rebuild available_skills.xml from local skills directories on startup
_update_available_skills_xml()

_SYSTEM_CONTENT: str = "\n\n".join(
    part
    for part in [
        _AGENT_MD_FILE.read_text(
            encoding="utf-8") if _AGENT_MD_FILE.exists() else "",
        _TOOLS_MD_FILE.read_text(
            encoding="utf-8") if _TOOLS_MD_FILE.exists() else "",
        _SKILLS_MD_FILE.read_text(
            encoding="utf-8") if _SKILLS_MD_FILE.exists() else "",
        _AVAILABLE_SKILLS_XML_FILE.read_text(
            encoding="utf-8") if _AVAILABLE_SKILLS_XML_FILE.exists() else "",
    ]
    if part.strip()
)

_TOOLS: list = (
    json.loads(_TOOLS_JSON_FILE.read_text(encoding="utf-8"))
    if _TOOLS_JSON_FILE.exists()
    else []
)

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


def _log_debug(tag: str, message: str) -> None:
    """Append a lightweight debug line to the current log file."""
    ts = datetime.now(timezone.utc).isoformat()
    line = f"[{ts}] [{tag}] {message}\n"
    try:
        with _LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


# Share the log-debug callback with the tools module so they write to the same file.
_set_tools_log_debug(_log_debug)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def llm_reply(
    text: str,
    on_intermediate: Callable[[str], Awaitable[None]] | None = None,
    bot=None,
    msg=None,
) -> str:
    call_id = str(uuid4())
    requested_at = datetime.now(timezone.utc).isoformat()

    # Build the message list: system prompt + history + new user message
    messages: list[dict] = [{"role": "system", "content": _SYSTEM_CONTENT}]
    messages.extend(_read_latest_chat())
    messages.append({"role": "user", "content": text})

    last_response = None
    max_tool_calls, max_skill_reads = _load_agent_limits()
    tool_ack_sent = False

    # Agentic loop — keep calling the model until it stops issuing tool calls
    while True:
        request_payload = {
            "model": _model,
            "messages": messages,
            "tools": _TOOLS,
        }

        response = await _client.chat.completions.create(**request_payload)
        last_response = response
        choice = response.choices[0]
        finish_reason = choice.finish_reason

        if finish_reason == "tool_calls":
            tool_calls = choice.message.tool_calls or []

            # Notify user once before executing the first tool-call batch.
            if not tool_ack_sent and on_intermediate:
                await on_intermediate(_load_replies()["tool_ack"])
                tool_ack_sent = True

            # Append the assistant's tool-call message to maintain context
            assistant_msg = choice.message.model_dump(exclude_none=True)
            # Ensure tool_calls are serialisable (convert objects → dicts)
            if "tool_calls" in assistant_msg:
                assistant_msg["tool_calls"] = [
                    tc if isinstance(tc, dict) else tc.model_dump(
                        exclude_none=True)
                    for tc in (choice.message.tool_calls or [])
                ]
            messages.append(assistant_msg)

            # Per-cycle counters — reset at the start of each tool-call batch
            cycle_tool_count = 0
            cycle_skill_count = 0

            for tc in tool_calls:
                fn_name = tc.function.name
                try:
                    fn_args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    fn_args = {}

                if fn_name == "web_search":
                    if on_intermediate:
                        await on_intermediate("正在调用 web search 工具...")
                    result = await _call_openrouter_web_search(messages)
                    return result

                # Enforce tool call limit for this cycle
                if cycle_tool_count >= max_tool_calls:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": f"Error: tool call limit ({max_tool_calls}) reached for this turn.",
                    })
                    continue

                # Enforce skill read limit (reads targeting a SKILL.md file)
                if fn_name == "read":
                    read_path = fn_args.get("path", "")
                    if read_path.upper().endswith("SKILL.MD"):
                        if cycle_skill_count >= max_skill_reads:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": f"Error: skill read limit ({max_skill_reads}) reached for this turn.",
                            })
                            cycle_tool_count += 1
                            continue
                        cycle_skill_count += 1

                tool_result = await _execute_tool(fn_name, fn_args, bot=bot, msg=msg)
                cycle_tool_count += 1
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": tool_result,
                    }
                )
            # Cycle complete — counters will reset on the next iteration
        else:
            # finish_reason == "stop" (or any non-tool terminal reason)
            reply_text = choice.message.content or ""
            break

    try:
        new_messages = [
            {"role": "user", "content": text},
            {"role": "assistant", "content": reply_text},
        ]
        _append_daily_history(new_messages)
        _update_latest_chat(new_messages)
    except Exception:
        pass

    try:
        _append_log(
            {
                "call_id": call_id,
                "requested_at": requested_at,
                "responded_at": datetime.now(timezone.utc).isoformat(),
                "request": request_payload,
                "response": last_response.model_dump(),
            }
        )
    except Exception:
        pass

    return reply_text
