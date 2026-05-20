"""WeChat bot that replies using an LLM (OpenAI-compatible API)."""

import asyncio

from dotenv import load_dotenv
from wechatbot import WeChatBot

load_dotenv()

from llm import llm_reply  # noqa: E402 — must import after load_dotenv


async def main():
    bot = WeChatBot(
        on_qr_url=lambda url: print(f"\nScan this URL in WeChat:\n{url}\n"),
        on_error=lambda err: print(f"Error: {err}"),
    )

    creds = await bot.login()
    print(f"Logged in: {creds.account_id} ({creds.user_id})")

    count = 0

    @bot.on_message
    async def handle(msg):
        nonlocal count
        count += 1
        print(f"[{count}] {msg.user_id}: {msg.text}")

        await bot.send_typing(msg.user_id)
        reply_text = await llm_reply(msg.text)
        await bot.reply(msg, reply_text)

    print("Listening for messages (Ctrl+C to stop)")
    try:
        await bot.start()
    except KeyboardInterrupt:
        bot.stop()
    print(f"Stopped. Processed {count} messages.")


if __name__ == "__main__":
    asyncio.run(main())
