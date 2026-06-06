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

from llm import llm_reply  # noqa: E402 — must import after load_dotenv
from media import handle_media_message  # noqa: E402


def print_terminal_qr(url: str) -> None:
    print(f"\nScan this URL in WeChat:\n{url}\n")

    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


async def main():
    logging.basicConfig(level=logging.INFO)

    bot = WeChatBot(
        cred_path="./cred/wechat.json",
        on_qr_url=print_terminal_qr,
        on_scanned=lambda: print("Scanned!"),
        on_expired=lambda: print("Expired..."),
        on_error=lambda err: print(f"Error: {err}"),
    )

    creds = await bot.login()
    print(f"Logged in: {creds.account_id} ({creds.user_id})")

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
            await bot.reply(msg, output)
            return

        llm_input_text = msg.text

        if msg.type in ("image", "voice", "file", "video"):
            llm_input_text, saved_path = await handle_media_message(bot, msg, doc_dir, llm_input_text)

            # File upload: pause LLM and ask user for processing instructions
            if msg.type == "file" and saved_path:
                pending_files[msg.user_id] = saved_path
                await bot.reply(msg, REPLY_FILE_RECEIVED)
                return

        await input_queue.put((msg, llm_input_text))

    async def process_worker():
        while True:
            msg, llm_input_text = await input_queue.get()
            try:
                await bot.send_typing(msg.user_id)

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
            await bot.reply(msg, reply)
            output_queue.task_done()

    # 启动后台worker
    asyncio.create_task(process_worker())
    asyncio.create_task(reply_worker())

    print("Listening for messages (Ctrl+C to stop)")
    try:
        await bot.start()
    except KeyboardInterrupt:
        bot.stop()
    print(f"Stopped. Processed {count} messages.")


if __name__ == "__main__":
    asyncio.run(main())
