"""WeChat bot that replies using an LLM (OpenAI-compatible API)."""


import asyncio
import configparser
import logging
import os
import qrcode
from dotenv import load_dotenv
from wechatbot import WeChatBot
import threading

load_dotenv()

_conf = configparser.ConfigParser()
_conf.read(os.path.join(os.path.dirname(__file__),
           "server.conf"), encoding="utf-8")
REPLY_FILE_RECEIVED: str = _conf.get(
    "replies", "file_received", fallback="收到，请问下一步怎么处理？")
MAX_REPLY_CHARS: int = _conf.getint(
    "replies", "max_text_chunk_chars", fallback=1500)

from llm import llm_reply  # noqa: E402 — must import after load_dotenv
from media import handle_media_message  # noqa: E402
from job_manager import get_manager as _get_job_manager  # noqa: E402
from wechat_logging import call_wechat_bot_async, call_wechat_bot_sync  # noqa: E402


def print_terminal_qr(url: str) -> None:
    print(f"\nScan this URL in WeChat:\n{url}\n")

    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


def _normalize_reply_text(reply: object) -> str:
    if reply is None:
        return ""
    if isinstance(reply, str):
        return reply
    return str(reply)


def _split_reply_text(reply: object, limit: int = MAX_REPLY_CHARS) -> list[str]:
    text = _normalize_reply_text(reply)
    if not text:
        return ["抱歉，这次没有生成可发送的内容。"]

    if limit <= 0 or len(text) <= limit:
        return [text]

    chunks: list[str] = []
    start = 0
    text_len = len(text)
    while start < text_len:
        end = min(start + limit, text_len)
        if end < text_len:
            split_at = text.rfind("\n", start, end)
            if split_at <= start:
                split_at = text.rfind(" ", start, end)
            if split_at > start:
                end = split_at

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = end
        while start < text_len and text[start] in ("\n", " "):
            start += 1

    return chunks or ["抱歉，这次没有生成可发送的内容。"]


async def _safe_reply(bot: WeChatBot, msg, reply: object) -> bool:
    chunks = _split_reply_text(reply)
    for index, chunk in enumerate(chunks, start=1):
        try:
            await call_wechat_bot_async(
                "reply",
                bot.reply,
                msg,
                chunk,
                msg=msg,
                detail=f"chunk={index}/{len(chunks)}, len={len(chunk)}",
            )
        except Exception:
            return False
    return True


async def main():
    logging.basicConfig(level=logging.INFO)

    bot = WeChatBot(
        cred_path="./cred/wechat.json",
        on_qr_url=print_terminal_qr,
        on_scanned=lambda: print("Scanned!"),
        on_expired=lambda: print("Expired..."),
        on_error=lambda err: print(f"Error: {err}"),
    )

    creds = await call_wechat_bot_async("login", bot.login)
    print(f"Logged in: {creds.account_id} ({creds.user_id})")

    _jm = _get_job_manager()
    _jm.set_bot(bot)
    _jm.start_monitor()

    input_queue = asyncio.Queue()
    output_queue = asyncio.Queue()
    count = 0
    # user_id -> absolute path of the file awaiting processing instructions
    pending_files: dict[str, str] = {}

    doc_dir = os.path.join(os.path.dirname(__file__), "doc")
    os.makedirs(doc_dir, exist_ok=True)

    @bot.on_message
    async def handle(msg):
        nonlocal count
        count += 1
        print(f"[{count}] {msg.user_id}: {msg.text} (type={msg.type})")

        # --- File pending: user replies with processing instruction ---
        if msg.user_id in pending_files and msg.type not in ("image", "voice", "file", "video"):
            file_path = pending_files.pop(msg.user_id)
            instruction = (msg.text or "").strip()
            file_arg = os.path.relpath(
                file_path, os.path.dirname(os.path.abspath(__file__)))
            cmd = [
                "codex", "exec", instruction,
                "-i", file_arg,
                "--skip-git-repo-check",
                "--sandbox", "workspace-write",
            ]
            logging.info("Running codex: %s", cmd)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=os.path.dirname(os.path.abspath(__file__)),
                )
                stdout, stderr = await proc.communicate()
                output = stdout.decode("utf-8", errors="replace").strip()
                if not output:
                    output = stderr.decode("utf-8", errors="replace").strip()
                if not output:
                    output = "执行完成"
            except Exception as exc:
                logging.exception("codex exec failed")
                output = f"执行失败：{exc}"
            await _safe_reply(bot, msg, output)
            return

        llm_input_text = msg.text

        if msg.type in ("image", "voice", "file", "video"):
            llm_input_text, saved_path = await handle_media_message(bot, msg, doc_dir, llm_input_text)

            # File upload: pause LLM and ask user for processing instructions
            if msg.type == "file" and saved_path:
                pending_files[msg.user_id] = saved_path
                await _safe_reply(bot, msg, REPLY_FILE_RECEIVED)
                return

        await input_queue.put((msg, llm_input_text))

    async def process_worker():
        while True:
            msg, llm_input_text = await input_queue.get()
            try:
                await call_wechat_bot_async(
                    "send_typing",
                    bot.send_typing,
                    msg.user_id,
                    user_id=msg.user_id,
                )

                async def _send_intermediate(notice: str) -> None:
                    await output_queue.put((msg, notice))

                reply_text = await llm_reply(llm_input_text, on_intermediate=_send_intermediate, bot=bot, msg=msg)
                await output_queue.put((msg, reply_text))
            except Exception as exc:
                logging.exception("LLM call failed for user %s", msg.user_id)
                await output_queue.put((msg, f"抱歉，调用大模型失败（{type(exc).__name__}）。请稍后重试。"))
            finally:
                input_queue.task_done()

    async def reply_worker():
        while True:
            msg, reply = await output_queue.get()
            try:
                await _safe_reply(bot, msg, reply)
            finally:
                output_queue.task_done()

    # 启动后台worker
    asyncio.create_task(process_worker())
    asyncio.create_task(reply_worker())

    print("Listening for messages (Ctrl+C to stop)")
    try:
        await call_wechat_bot_async("start", bot.start)
    except KeyboardInterrupt:
        call_wechat_bot_sync("stop", bot.stop)
    print(f"Stopped. Processed {count} messages.")


if __name__ == "__main__":
    asyncio.run(main())
