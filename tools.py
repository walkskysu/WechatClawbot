"""Tool implementations for the WechatClawbot LLM agent."""

import asyncio
import base64
import json
import os
import stat
from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx

_ROOT_DIR = Path(__file__).resolve().parent
_model: str = os.environ.get("LLM_MODEL", "")

# Logging callback — injected by llm.py so all tools share the same log file.
_log_debug_fn: Callable[[str, str], None] = lambda tag, msg: None


def set_log_debug(fn: Callable[[str, str], None]) -> None:
    """Configure the logging callback (called once from llm.py after startup)."""
    global _log_debug_fn
    _log_debug_fn = fn


def _log_debug(tag: str, message: str) -> None:
    _log_debug_fn(tag, message)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Web search (OpenRouter built-in)
# ---------------------------------------------------------------------------

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
