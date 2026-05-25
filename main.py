"""WeChat bot that replies using an LLM (OpenAI-compatible API)."""


import asyncio
import base64
import logging
import os
import qrcode
from dotenv import load_dotenv
from openai import AsyncOpenAI
from wechatbot import WeChatBot
import threading

load_dotenv()

from llm import llm_reply  # noqa: E402 — must import after load_dotenv


def _extract_voice_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(p for p in parts if p.strip()).strip()
    return ""


def _detect_audio_format(file_name: str, media_bytes: bytes) -> str:
    ext = os.path.splitext(file_name)[1].lower().lstrip(".")
    if ext in {"wav", "mp3", "m4a", "ogg", "webm"}:
        return ext

    # Fallback to magic-bytes detection when filename has no extension.
    head = media_bytes[:16]
    if head.startswith(b"RIFF") and b"WAVE" in head:
        return "wav"
    if head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "mp3"
    if head.startswith(b"OggS"):
        return "ogg"
    if head.startswith(b"\x1A\x45\xDF\xA3"):
        return "webm"
    if b"ftyp" in media_bytes[:64]:
        return "m4a"

    # Conservative default expected by many providers.
    return "wav"


def _candidate_audio_formats(primary: str) -> list[str]:
    # Current OpenRouter provider in use only accepts wav/mp3.
    if primary == "mp3":
        ordered = ["mp3", "wav"]
    else:
        ordered = ["wav", "mp3"]
    seen: set[str] = set()
    result: list[str] = []
    for item in ordered:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _ensure_media_extension(file_name: str, msg_type: str, media_bytes: bytes) -> str:
    # Keep original name when extension already exists.
    ext = os.path.splitext(file_name)[1]
    if ext:
        return file_name

    header = media_bytes[:64]

    if msg_type == "image":
        if header.startswith(b"\xFF\xD8\xFF"):
            suffix = ".jpg"
        elif header.startswith(b"\x89PNG\r\n\x1a\n"):
            suffix = ".png"
        elif header.startswith(b"GIF87a") or header.startswith(b"GIF89a"):
            suffix = ".gif"
        elif header.startswith(b"RIFF") and b"WEBP" in header:
            suffix = ".webp"
        else:
            suffix = ".jpg"
    elif msg_type == "voice":
        if header.startswith(b"#!SILK_V3"):
            suffix = ".silk"
        elif header.startswith(b"RIFF") and b"WAVE" in header:
            suffix = ".wav"
        elif header.startswith(b"ID3") or (len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0):
            suffix = ".mp3"
        elif header.startswith(b"OggS"):
            suffix = ".ogg"
        elif header.startswith(b"#!AMR"):
            suffix = ".amr"
        elif b"ftyp" in header:
            suffix = ".m4a"
        else:
            suffix = ".silk"
    elif msg_type == "video":
        if b"ftyp" in header:
            suffix = ".mp4"
        elif header.startswith(b"\x1A\x45\xDF\xA3"):
            suffix = ".webm"
        else:
            suffix = ".mp4"
    else:
        suffix = ".bin"

    return f"{file_name}{suffix}"


async def _transcribe_voice(media_bytes: bytes, audio_format: str) -> str:
    api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("LLM_API_KEY")
    if not api_key:
        raise RuntimeError("Missing OPENROUTER_API_KEY/LLM_API_KEY for voice transcription")

    base_url = os.getenv("OPENROUTER_API_URL", "https://openrouter.ai/api/v1")
    model = os.getenv("OPENROUTER_AUDIO_MODEL", "openai/gpt-audio-mini")

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    completion = await client.chat.completions.create(
        extra_headers={
            "HTTP-Referer": os.getenv("OPENROUTER_HTTP_REFERER", ""),
            "X-OpenRouter-Title": os.getenv("OPENROUTER_APP_TITLE", "WechatClawbot"),
        },
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "请将这段语音准确转写为文字，仅输出转写文本。",
                    },
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": base64.b64encode(media_bytes).decode("ascii"),
                            "format": audio_format,
                        },
                    },
                ],
            }
        ],
    )
    return _extract_voice_text(completion.choices[0].message.content)


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

    doc_dir = os.path.join(os.path.dirname(__file__), "doc")
    os.makedirs(doc_dir, exist_ok=True)

    @bot.on_message
    async def handle(msg):
        nonlocal count
        count += 1
        print(f"[{count}] {msg.user_id}: {msg.text} (type={msg.type})")

        llm_input_text = msg.text

        if msg.type in ("image", "voice", "file", "video"):
            media = None
            try:
                media = await bot.download(msg)
            except Exception as exc:
                logging.warning("Failed to download media from %s: %s", msg.user_id, exc)

            if media:
                file_name = media.file_name or f"{msg.type}_{int(msg.timestamp.timestamp())}"
                file_name = _ensure_media_extension(file_name, msg.type, media.data)
                save_path = os.path.join(doc_dir, file_name)
                with open(save_path, "wb") as f:
                    f.write(media.data)
                print(f"  Downloaded {msg.type} -> {save_path} ({len(media.data)} bytes)")

                if msg.type == "voice":
                    # WeChat often provides recognized text in msg.text for voice messages.
                    # Prefer it to avoid unnecessary format mismatch errors from audio models.
                    if (msg.text or "").strip():
                        llm_input_text = msg.text.strip()
                    else:
                        transcript = ""
                        primary_format = _detect_audio_format(file_name, media.data)
                        for audio_format in _candidate_audio_formats(primary_format):
                            try:
                                transcript = await _transcribe_voice(media.data, audio_format)
                                if transcript:
                                    break
                            except Exception as exc:
                                # OpenRouter may reject mismatched format; try other formats before falling back.
                                logging.warning(
                                    "Voice transcription failed for %s with format=%s: %s",
                                    msg.user_id,
                                    audio_format,
                                    exc,
                                )

                        if transcript:
                            llm_input_text = transcript
                        else:
                            llm_input_text = ""
                            logging.warning(
                                "Voice transcription unavailable for %s and msg.text is empty",
                                msg.user_id,
                            )

                    if llm_input_text:
                        await bot.reply(msg, f"收到消息如下:{llm_input_text}")
                    else:
                        await bot.reply(msg, "收到消息如下:(语音转写失败，请重试)")

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
