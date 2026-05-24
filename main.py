"""WeChat bot that replies using an LLM (OpenAI-compatible API)."""


import asyncio
import logging
import qrcode
from dotenv import load_dotenv
from wechatbot import WeChatBot
import threading

load_dotenv()

from llm import llm_reply  # noqa: E402 — must import after load_dotenv


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

    @bot.on_message
    async def handle(msg):
        nonlocal count
        count += 1
        print(f"[{count}] {msg.user_id}: {msg.text}")
        await input_queue.put(msg)

    async def process_worker():
        while True:
            msg = await input_queue.get()
            try:
                await bot.send_typing(msg.user_id)

                async def _send_intermediate(notice: str) -> None:
                    await output_queue.put((msg, notice))

                reply_text = await llm_reply(msg.text, on_intermediate=_send_intermediate, bot=bot, msg=msg)
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
