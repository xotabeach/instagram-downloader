#!/Library/Frameworks/Python.framework/Versions/3.14/bin/python3
"""Telegram bot: send Instagram URL → get preview media + original file."""

from __future__ import annotations

import asyncio
import os
import tempfile
import traceback
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from instagram_core import (
    download_instagram_media,
    extract_instagram_url,
    is_image,
    is_video,
)

# Telegram Bot API limit for regular bots is 50 MB.
MAX_UPLOAD_BYTES = 49 * 1024 * 1024


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_allowed_user_ids() -> set[int] | None:
    raw = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    if not raw:
        return None
    return {int(part.strip()) for part in raw.split(",") if part.strip()}


ALLOWED_USER_IDS = load_allowed_user_ids()
COOKIES_BROWSER = os.getenv("INSTAGRAM_COOKIES_FROM_BROWSER", "").strip() or None
COOKIES_FILE = os.getenv("INSTAGRAM_COOKIES_FILE", "").strip() or None
SEND_PREVIEW = env_bool("TELEGRAM_SEND_PREVIEW", True)
SEND_DOCUMENT = env_bool("TELEGRAM_SEND_DOCUMENT", True)


def user_allowed(user_id: int | None) -> bool:
    if ALLOWED_USER_IDS is None:
        return True
    return user_id is not None and user_id in ALLOWED_USER_IDS


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not user_allowed(update.effective_user.id):
        if update.message:
            await update.message.reply_text("Доступ запрещён.")
        return

    await update.message.reply_text(
        "Пришли ссылку на Instagram (post / reel / video / photo).\n"
        "Я скачаю и отправлю:\n"
        "• обычный вариант (фото/видео в ленте)\n"
        "• оригинал файлом без сжатия Telegram"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def send_media_pair(message, file_path: Path) -> None:
    size = file_path.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        await message.reply_text(
            f"Файл слишком большой для Telegram Bot API ({size / (1024 * 1024):.1f} MB): "
            f"{file_path.name}\nЛимит ~50 MB."
        )
        return

    caption = file_path.name

    with file_path.open("rb") as media_file:
        if SEND_PREVIEW:
            if is_image(file_path):
                await message.reply_photo(photo=media_file, caption=f"Превью: {caption}")
            elif is_video(file_path):
                await message.reply_video(video=media_file, caption=f"Превью: {caption}")
            else:
                await message.reply_document(document=media_file, caption=caption)
                return

    if SEND_DOCUMENT:
        with file_path.open("rb") as document_file:
            await message.reply_document(
                document=document_file,
                filename=file_path.name,
                caption=f"Оригинал (без сжатия): {caption}",
            )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text:
        return

    if not update.effective_user or not user_allowed(update.effective_user.id):
        await message.reply_text("Доступ запрещён.")
        return

    url = extract_instagram_url(message.text)
    if not url:
        await message.reply_text("Не вижу ссылку Instagram. Пришли URL вида https://www.instagram.com/...")
        return

    status = await message.reply_text("Скачиваю...")
    await message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)

    workdir = Path(tempfile.mkdtemp(prefix="ig_tg_"))

    try:
        result = await asyncio.to_thread(
            download_instagram_media,
            url,
            workdir,
            cookies_browser=COOKIES_BROWSER,
            cookies_file=COOKIES_FILE,
        )

        if not result.files:
            details = "\n".join(result.messages[-8:]) if result.messages else (result.error or "unknown")
            await status.edit_text(f"Не удалось скачать.\n\n{details}")
            return

        await status.edit_text(f"Скачано файлов: {len(result.files)}. Отправляю...")

        for file_path in result.files:
            try:
                await send_media_pair(message, file_path)
            except TelegramError as send_error:
                await message.reply_text(f"Не отправил {file_path.name}: {send_error}")

        note = "Готово."
        if result.partial:
            note = "Готово частично: часть элементов могла не скачаться."
        await status.edit_text(note)

    except Exception as e:
        await status.edit_text(f"Ошибка: {e}")
        traceback.print_exc()

    finally:
        for file_path in workdir.glob("*"):
            try:
                file_path.unlink()
            except OSError:
                pass
        try:
            workdir.rmdir()
        except OSError:
            pass


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "Не задан TELEGRAM_BOT_TOKEN.\n"
            "Пример:\n"
            'export TELEGRAM_BOT_TOKEN="123:ABC"\n'
            'export INSTAGRAM_COOKIES_FROM_BROWSER="chrome"\n'
            'export TELEGRAM_ALLOWED_USER_IDS="123456789"\n'
            "python3 instagram_telegram_bot.py"
        )

    if COOKIES_FILE and not Path(COOKIES_FILE).expanduser().exists():
        raise SystemExit(f"Файл cookies не найден: {COOKIES_FILE}")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("Instagram Telegram bot started.")
    print(f"Cookies browser: {COOKIES_BROWSER or 'нет'}")
    print(f"Cookies file: {COOKIES_FILE or 'нет'}")
    print(f"Allowed users: {sorted(ALLOWED_USER_IDS) if ALLOWED_USER_IDS else 'все'}")
    print(f"Send preview: {SEND_PREVIEW}, send document: {SEND_DOCUMENT}")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
