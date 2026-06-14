"""Manual WeChat reply_media test for large file uploads.

Run this script, scan the QR code, then send a trigger message from the target
conversation. The bot will reply to that message with the configured PPTX and
PNG files using WeChatBot.reply_media.
"""

import argparse
import asyncio
import logging
from pathlib import Path

import qrcode
from wechatbot import WeChatBot

from wechat_logging import call_wechat_bot_async, call_wechat_bot_sync


ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_PPTX = ROOT_DIR / "doc" / "SpaceX_IPO_2026_CN.pptx"
DEFAULT_PNG = ROOT_DIR / "doc" / "superdog_bank_comic.png"


def print_terminal_qr(url: str) -> None:
    print(f"\nScan this URL in WeChat:\n{url}\n")

    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Test WeChatBot.reply_media with large PPTX and PNG files.",
    )
    parser.add_argument(
        "--pptx",
        default=str(DEFAULT_PPTX),
        help="Path to the PPTX file to upload.",
    )
    parser.add_argument(
        "--png",
        default=str(DEFAULT_PNG),
        help="Path to the PNG file to upload.",
    )
    parser.add_argument(
        "--trigger",
        default="test reply_media",
        help="Only send files after receiving this exact text. Use '*' to accept any text message.",
    )
    parser.add_argument(
        "--user-id",
        default="",
        help="Optional WeChat user_id filter. When set, only messages from that user trigger the test.",
    )
    parser.add_argument(
        "--keep-running",
        action="store_true",
        help="Keep listening after a successful upload instead of stopping the bot.",
    )
    return parser


def _validate_file(path_str: str, expected_suffix: str) -> Path:
    path = Path(path_str).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if not path.is_file():
        raise ValueError(f"Path is not a file: {path}")
    if path.suffix.lower() != expected_suffix:
        raise ValueError(f"Expected a {expected_suffix} file, got: {path.name}")
    return path


async def _reply_with_file(bot: WeChatBot, msg, media_type: str, path: Path) -> None:
    payload = {"image": path.read_bytes()} if media_type == "image" else {
        "file": path.read_bytes(),
        "file_name": path.name,
    }
    await call_wechat_bot_async(
        "reply_media",
        bot.reply_media,
        msg,
        payload,
        msg=msg,
        detail=f"media_type={media_type}, path={path.name}, bytes={path.stat().st_size}",
    )


async def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    pptx_path = _validate_file(args.pptx, ".pptx")
    png_path = _validate_file(args.png, ".png")
    trigger_text = args.trigger.strip()
    target_user_id = args.user_id.strip()

    logging.basicConfig(level=logging.INFO)
    logging.info("PPTX ready: %s (%d bytes)", pptx_path, pptx_path.stat().st_size)
    logging.info("PNG ready: %s (%d bytes)", png_path, png_path.stat().st_size)
    if trigger_text == "*":
        logging.info("Trigger mode: any text message")
    else:
        logging.info("Trigger text: %s", trigger_text)
    if target_user_id:
        logging.info("User filter: %s", target_user_id)

    bot = WeChatBot(
        cred_path="./cred/wechat.json",
        on_qr_url=print_terminal_qr,
        on_scanned=lambda: print("Scanned!"),
        on_expired=lambda: print("Expired..."),
        on_error=lambda err: print(f"Error: {err}"),
    )

    creds = await call_wechat_bot_async("login", bot.login)
    print(f"Logged in: {creds.account_id} ({creds.user_id})")
    print("Listening for trigger message. Press Ctrl+C to stop.")

    send_completed = False

    @bot.on_message
    async def handle(msg):
        nonlocal send_completed
        if send_completed and not args.keep_running:
            return

        msg_text = (msg.text or "").strip()
        if msg.type != "text":
            logging.info("Ignoring non-text message from %s (type=%s)", msg.user_id, msg.type)
            return
        if target_user_id and msg.user_id != target_user_id:
            logging.info("Ignoring message from %s; waiting for %s", msg.user_id, target_user_id)
            return
        if trigger_text != "*" and msg_text != trigger_text:
            logging.info("Ignoring text '%s'; waiting for '%s'", msg_text, trigger_text)
            return

        logging.info("Trigger accepted from %s; sending PPTX then PNG", msg.user_id)
        await call_wechat_bot_async(
            "reply",
            bot.reply,
            msg,
            f"开始测试 reply_media：先发送 {pptx_path.name}，再发送 {png_path.name}",
            msg=msg,
            detail="reply_media_test_start",
        )

        await _reply_with_file(bot, msg, "file", pptx_path)
        await _reply_with_file(bot, msg, "image", png_path)

        await call_wechat_bot_async(
            "reply",
            bot.reply,
            msg,
            "reply_media 测试发送完成。",
            msg=msg,
            detail="reply_media_test_done",
        )

        send_completed = True
        if not args.keep_running:
            await call_wechat_bot_async("stop", bot.stop)

    try:
        await call_wechat_bot_async("start", bot.start)
    except KeyboardInterrupt:
        call_wechat_bot_sync("stop", bot.stop)


if __name__ == "__main__":
    asyncio.run(main())