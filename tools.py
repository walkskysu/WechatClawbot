"""Tool implementations for the WechatClawbot LLM agent."""

import asyncio
import json
import os
import stat
from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx

from job_manager import get_manager as _get_job_manager

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
    msg: object = None,
    **_: object,
) -> str:
    return await _get_job_manager().run_job(
        command=command,
        name=f"exec: {command[:80]}",
        cwd=workdir,
        env=env,
        timeout=float(timeout) if timeout else None,
        user_id=getattr(msg, "user_id", None),
        msg=msg,
    )


async def _tool_generate_image(
    prompt: str,
    output_path: str | None = None,
    msg: object = None,
    **_: object,
) -> str:
    """Run codex locally to generate an image using $imagegen."""
    if not output_path:
        out_dir = _ROOT_DIR / "outputs"
        out_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = str(out_dir / f"image_{timestamp}.png")

    command = (
        f'codex exec "使用 $imagegen {prompt}, 输出文件到 {output_path}"'
        f' --skip-git-repo-check --sandbox workspace-write'
    )
    _log_debug("generate_image", f"EXEC command={command!r}")

    output = await _get_job_manager().run_job(
        command=command,
        name=f"generate_image: {prompt[:60]}",
        user_id=getattr(msg, "user_id", None),
        msg=msg,
        check_returncode=True,
    )
    _log_debug("generate_image", f"EXEC output={output!r}")
    if output.startswith("Error:"):
        return output
    return f"Image saved to {output_path}\n{output}".strip()


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
        return await handler(**args, bot=bot, msg=msg)

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
