"""LLM client — OpenAI-compatible chat completion with agentic tool loop."""

import asyncio
import base64
import configparser
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable
from uuid import uuid4

import httpx
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

# --- Agent configuration (loaded once at startup) ---
_AGENT_MD_FILE = _ROOT_DIR / "agent" / "agent.md"
_TOOLS_MD_FILE = _ROOT_DIR / "agent" / "tools.md"
_TOOLS_JSON_FILE = _ROOT_DIR / "agent" / "tools.json"
_SKILLS_MD_FILE = _ROOT_DIR / "agent" / "skills.md"
_AVAILABLE_SKILLS_XML_FILE = _ROOT_DIR / "agent" / "available_skills.xml"
_SKILLS_DIR = _ROOT_DIR / "skills"

_DEFAULT_MAX_TOOL_CALLS = 20
_DEFAULT_MAX_SKILL_READS = 5


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


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def _tool_read(path: str, offset: int | None = None, limit: int | None = None, **_: object) -> str:
    p = Path(path)
    if not p.exists():
        return f"Error: file not found: {path}"
    if p.is_dir():
        return f"Error: {path} is a directory — use 'list' instead"

    if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        return f"[Image file: {path} — binary content not inlined in tool results]"

    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"Error reading file: {exc}"

    lines = text.splitlines()
    start = max((offset or 1) - 1, 0)
    selected = lines[start: start + limit] if limit else lines[start:]
    result = "\n".join(selected)

    encoded = result.encode("utf-8")
    if len(encoded) > 50 * 1024:
        result = encoded[: 50 *
                         1024].decode("utf-8", errors="replace") + "\n... [truncated]"
    return result


async def _tool_list(path: str | None = None, all: bool = True, long: bool = True, **_: object) -> str:
    p = Path(path) if path else Path.cwd()
    if not p.exists():
        return f"Error: path not found: {path}"
    try:
        entries = sorted(p.iterdir(), key=lambda e: (
            e.is_file(), e.name.lower()))
    except OSError as exc:
        return f"Error listing directory: {exc}"

    visible = [e for e in entries if all or not e.name.startswith(".")]

    if not long:
        return "\n".join(e.name + ("/" if e.is_dir() else "") for e in visible)

    lines = [f"total {len(visible)}"]
    for e in visible:
        try:
            st = e.stat()
            mode = stat.filemode(st.st_mode)
            size = st.st_size
            mtime = datetime.fromtimestamp(st.st_mtime).strftime("%b %d %H:%M")
            lines.append(
                f"{mode} {size:>10} {mtime} {e.name}{'/' if e.is_dir() else ''}")
        except OSError:
            lines.append(f"??????????   0 ??? ?? ??:?? {e.name}")
    return "\n".join(lines)


async def _tool_write(path: str, content: str, **_: object) -> str:
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Written {len(content.encode('utf-8'))} bytes to {path}"
    except OSError as exc:
        return f"Error writing file: {exc}"


async def _tool_edit(path: str, edits: list, **_: object) -> str:
    p = Path(path)
    if not p.exists():
        return f"Error: file not found: {path}"
    try:
        content = p.read_text(encoding="utf-8")
    except OSError as exc:
        return f"Error reading file: {exc}"

    for i, edit in enumerate(edits):
        old_text = edit.get("oldText", "")
        new_text = edit.get("newText", "")
        if old_text not in content:
            return f"Error: edit #{i + 1} oldText not found in file: {repr(old_text[:80])}"
        content = content.replace(old_text, new_text, 1)

    try:
        p.write_text(content, encoding="utf-8")
        return f"Edited {path}: applied {len(edits)} replacement(s)"
    except OSError as exc:
        return f"Error writing file: {exc}"


async def _tool_exec(
    command: str,
    workdir: str | None = None,
    env: dict | None = None,
    timeout: int | float | None = None,
    **_: object,
) -> str:
    merged_env = {**os.environ, **(env or {})}
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=workdir,
            env=merged_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=float(timeout) if timeout else None,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return "Error: command timed out"
        return stdout.decode(errors="replace")
    except Exception as exc:
        return f"Error executing command: {exc}"


def _build_media_content(
    media_type: str,
    data: bytes,
    file_name: str | None,
    path_name: str,
    caption: str | None,
) -> dict | str:
    """Return the content dict for bot.reply_media / bot.send_media, or an error string."""
    if media_type == "image":
        return {"image": data}
    if media_type == "file":
        return {"file": data, "file_name": file_name or path_name}
    if media_type == "video":
        content: dict = {"video": data}
        if caption:
            content["caption"] = caption
        return content
    return f"Error: unknown media_type '{media_type}'"


async def _tool_generate_image(
    prompt: str,
    model: str = "openai/gpt-5.4-image-2",
    quality: str = "high",
    output_path: str | None = None,
    **_: object,
) -> str:
    """Call OpenRouter /responses with openrouter:image_generation tool and save the result."""
    base_url = os.environ.get("LLM_API_URL", "https://openrouter.ai/api/v1").rstrip("/")
    url = f"{base_url}/responses"
    payload = {
        "model": _model,
        "input": prompt,
        "tools": [
            {
                "type": "openrouter:image_generation",
                "parameters": {
                    "model": model,
                    "quality": quality,
                },
            }
        ],
    }

    _log_debug("generate_image", f"REQUEST url={url} text_model={_model} image_model={model} quality={quality} prompt={prompt!r}")
    _log_debug("generate_image", f"REQUEST payload={json.dumps(payload, ensure_ascii=False)}")

    try:
        async with httpx.AsyncClient(timeout=120.0) as http:
            resp = await http.post(
                url,
                headers={
                    "Authorization": f"Bearer {os.environ['LLM_API_KEY']}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            _log_debug("generate_image", f"RESPONSE status={resp.status_code}")
            _log_debug("generate_image", f"RESPONSE body={resp.text}")
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        _log_debug("generate_image", f"HTTP_ERROR status={exc.response.status_code} body={exc.response.text}")
        return f"Error: image generation API returned {exc.response.status_code}: {exc.response.text[:300]}"
    except Exception as exc:
        _log_debug("generate_image", f"REQUEST_EXCEPTION {type(exc).__name__}: {exc}")
        return f"Error calling image generation API: {exc}"

    # Extract base64 image from response output array
    output_items = data.get("output", [])
    _log_debug("generate_image", f"PARSE output item count={len(output_items)} types={[i.get('type') for i in output_items if isinstance(i, dict)]}")

    image_b64: str | None = None
    image_status: str | None = None
    image_url: str | None = None
    for item in output_items:
        item_type = item.get("type") if isinstance(item, dict) else None
        _log_debug("generate_image", f"PARSE item type={item_type!r} keys={list(item.keys()) if isinstance(item, dict) else '?'}")
        if item_type == "openrouter:image_generation":
            image_status = item.get("status")
            image_b64 = item.get("result")
            image_url = item.get("imageUrl")
            _log_debug(
                "generate_image",
                f"PARSE found openrouter:image_generation status={image_status!r} result present={image_b64 is not None} result_len={len(image_b64) if image_b64 else 0} image_url_present={bool(image_url)}",
            )
            break

    if not image_b64 and not image_url:
        _log_debug("generate_image", f"PARSE image result empty; status={image_status!r} full response={json.dumps(data, ensure_ascii=False)}")
        if image_status and image_status != "completed":
            return f"Error: image generation status={image_status}; the model could not produce an image"
        if image_status == "completed" and image_b64 is None:
            return "Error: image generation completed but result is null — the model declined or quota was exceeded"
        return f"Error: no image in response: {json.dumps(data)[:500]}"

    image_bytes: bytes | None = None
    decode_error: str | None = None

    if image_b64:
        try:
            raw = image_b64.strip()
            if raw.startswith("data:") and "," in raw:
                raw = raw.split(",", 1)[1]
            raw = "".join(raw.split())
            if raw:
                raw += "=" * ((4 - len(raw) % 4) % 4)

            try:
                image_bytes = base64.b64decode(raw, validate=False)
            except Exception:
                image_bytes = base64.urlsafe_b64decode(raw)

            _log_debug("generate_image", f"DECODE success bytes={len(image_bytes)}")
        except Exception as exc:
            decode_error = f"{type(exc).__name__}: {exc}"
            _log_debug("generate_image", f"DECODE_ERROR {decode_error}")

    download_error: str | None = None
    if image_bytes is None and image_url:
        try:
            _log_debug("generate_image", f"DOWNLOAD start url={image_url}")
            async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as http:
                dl = await http.get(image_url)
                dl.raise_for_status()
                image_bytes = dl.content
            _log_debug("generate_image", f"DOWNLOAD success bytes={len(image_bytes)}")
        except Exception as exc:
            download_error = f"{type(exc).__name__}: {exc}"
            _log_debug("generate_image", f"DOWNLOAD_ERROR {download_error}")

    if image_bytes is None:
        if decode_error and download_error:
            return f"Error: failed to decode image result ({decode_error}) and failed to download imageUrl ({download_error})"
        if decode_error:
            return f"Error decoding image data: {decode_error}"
        if download_error:
            return f"Error downloading image from imageUrl: {download_error}"
        return "Error: image bytes are empty after parsing response"

    if not output_path:
        out_dir = _ROOT_DIR / "outputs"
        out_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = str(out_dir / f"image_{timestamp}.png")

    p = Path(output_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(image_bytes)
        _log_debug("generate_image", f"SAVE success path={output_path}")
    except OSError as exc:
        _log_debug("generate_image", f"SAVE_ERROR {type(exc).__name__}: {exc}")
        return f"Error saving image: {exc}"

    return f"Image saved to {output_path} ({len(image_bytes)} bytes)"


async def _tool_wechat_reply_media(
    media_type: str,
    path: str,
    file_name: str | None = None,
    caption: str | None = None,
    *,
    bot,
    msg,
    **_: object,
) -> str:
    p = Path(path)
    if not p.exists():
        return f"Error: file not found: {path}"
    try:
        data = p.read_bytes()
    except OSError as exc:
        return f"Error reading file: {exc}"

    content = _build_media_content(
        media_type, data, file_name, p.name, caption)
    if isinstance(content, str):
        return content

    try:
        await bot.reply_media(msg, content)
        return f"Sent {media_type} ({len(data)} bytes) to conversation"
    except Exception as exc:
        return f"Error sending media: {exc}"


async def _tool_wechat_send_media(
    user_id: str,
    media_type: str,
    path: str,
    file_name: str | None = None,
    caption: str | None = None,
    *,
    bot,
    **_: object,
) -> str:
    p = Path(path)
    if not p.exists():
        return f"Error: file not found: {path}"
    try:
        data = p.read_bytes()
    except OSError as exc:
        return f"Error reading file: {exc}"

    content = _build_media_content(
        media_type, data, file_name, p.name, caption)
    if isinstance(content, str):
        return content

    try:
        await bot.send_media(user_id, content)
        return f"Sent {media_type} ({len(data)} bytes) to user {user_id}"
    except Exception as exc:
        return f"Error sending media: {exc}"


async def _execute_tool(name: str, args: dict, bot=None, msg=None) -> str:
    dispatch = {
        "read": _tool_read,
        "list": _tool_list,
        "write": _tool_write,
        "edit": _tool_edit,
        "exec": _tool_exec,
        "generate_image": _tool_generate_image,
    }
    handler = dispatch.get(name)
    if handler is not None:
        return await handler(**args)

    if name == "wechat_reply_media":
        if bot is None or msg is None:
            return "Error: wechat_reply_media requires active bot/message context"
        return await _tool_wechat_reply_media(**args, bot=bot, msg=msg)

    if name == "wechat_send_media":
        if bot is None:
            return "Error: wechat_send_media requires active bot context"
        return await _tool_wechat_send_media(**args, bot=bot)

    return f"Error: unknown tool '{name}'"


async def _call_openrouter_web_search(messages: list) -> str:
    """Re-submit the conversation to OpenRouter with the built-in web_search tool."""
    base_url = os.environ.get(
        "LLM_API_URL", "https://openrouter.ai/api/v1").rstrip("/")
    url = f"{base_url}/chat/completions"
    payload = {
        "model": _model,
        "messages": messages,
        "tools": [{"type": "openrouter:web_search"}],
    }
    async with httpx.AsyncClient(timeout=60.0) as http:
        resp = await http.post(
            url,
            headers={
                "Authorization": f"Bearer {os.environ['LLM_API_KEY']}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
    return data["choices"][0]["message"]["content"] or ""


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
                await on_intermediate("好的，我开始工作了")
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
