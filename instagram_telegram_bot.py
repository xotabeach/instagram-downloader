#!/Library/Frameworks/Python.framework/Versions/3.14/bin/python3
"""Telegram bot: send Instagram URL → get preview media + original file."""

from __future__ import annotations

import asyncio
import json
import os
import random
import tempfile
import traceback
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from instagram_core import (
    download_instagram_media,
    extract_media_url,
    is_image,
    is_video,
    probe_video_metadata,
)

# Telegram Bot API limit for regular bots is 50 MB.
MAX_UPLOAD_BYTES = 49 * 1024 * 1024
AUTH_STORE_PATH = Path(__file__).with_name("authorized_users.json")
JOKES_PATH = Path(__file__).with_name("category_b_jokes.json")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_category_b_jokes() -> list[str]:
    if not JOKES_PATH.exists():
        return []
    try:
        data = json.loads(JOKES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [str(item).strip() for item in data if str(item).strip()]


COOKIES_BROWSER = os.getenv("INSTAGRAM_COOKIES_FROM_BROWSER", "").strip() or None
COOKIES_FILE = os.getenv("INSTAGRAM_COOKIES_FILE", "").strip() or None
SEND_PREVIEW = env_bool("TELEGRAM_SEND_PREVIEW", True)
SEND_DOCUMENT = env_bool("TELEGRAM_SEND_DOCUMENT", False)
AUTH_QUESTION = (
    os.getenv("TELEGRAM_AUTH_QUESTION", "").strip()
    or "Как зовут автора бота?"
)
AUTH_ANSWER = (
    os.getenv("TELEGRAM_AUTH_ANSWER", "").strip().lower().lstrip("@")
    or "xotabeach"
)
CATEGORY_B_JOKES = load_category_b_jokes()

# Users who already saw the question and are waiting for the answer (in-memory).
_pending_auth: set[int] = set()


def load_authorized_user_ids() -> set[int]:
    if not AUTH_STORE_PATH.exists():
        return set()
    try:
        data = json.loads(AUTH_STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(data, list):
        return set()
    return {int(item) for item in data}


def save_authorized_user_ids(user_ids: set[int]) -> None:
    AUTH_STORE_PATH.write_text(
        json.dumps(sorted(user_ids), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


AUTHORIZED_USER_IDS = load_authorized_user_ids()


def normalize_answer(text: str) -> str:
    return text.strip().lower().lstrip("@")


def is_authorized(user_id: int | None) -> bool:
    return user_id is not None and user_id in AUTHORIZED_USER_IDS


def authorize_user(user_id: int) -> None:
    AUTHORIZED_USER_IDS.add(user_id)
    _pending_auth.discard(user_id)
    save_authorized_user_ids(AUTHORIZED_USER_IDS)


async def ask_auth_question(message) -> None:
    await message.reply_text(
        "Чтобы пользоваться ботом, ответь на вопрос:\n"
        f"{AUTH_QUESTION}"
    )


async def ensure_authorized(update: Update) -> bool:
    """Return True if user may use the bot. Ask the secret question only once."""
    message = update.message
    user = update.effective_user
    if not message or not user:
        return False

    if is_authorized(user.id):
        return True

    text = (message.text or "").strip()
    if text.startswith("/"):
        # /start and /help should only show the question, not treat as answer.
        if user.id not in _pending_auth:
            _pending_auth.add(user.id)
            await ask_auth_question(message)
        else:
            await message.reply_text(f"Жду ответ на вопрос:\n{AUTH_QUESTION}")
        return False

    if normalize_answer(text) == AUTH_ANSWER:
        authorize_user(user.id)
        await message.reply_text(
            "Ок, доступ открыт.\n"
            "Пришли ссылку на Instagram или TikTok."
        )
        return False

    if user.id not in _pending_auth:
        _pending_auth.add(user.id)
        await ask_auth_question(message)
        return False

    await message.reply_text("Неверный ответ. Попробуй ещё раз.")
    return False


def welcome_text() -> str:
    return (
        "Пришли ссылку на Instagram или TikTok.\n"
        "Я скачаю и отправлю медиа как есть:\n"
        "• видео — в исходном разрешении и соотношении сторон\n"
        "• фото — фото\n"
        "• карусель — каждый элемент отдельно\n"
        "После видео — случайный анекдот категории Б."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not await ensure_authorized(update):
        return
    await update.message.reply_text(welcome_text())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def send_media(message, file_path: Path) -> None:
    size = file_path.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        await message.reply_text(
            f"Файл слишком большой для Telegram Bot API ({size / (1024 * 1024):.1f} MB): "
            f"{file_path.name}\nЛимит ~50 MB."
        )
        return

    # Video plays right in the chat; explicit display size keeps the original
    # aspect ratio. The extra document copy is opt-in because it uploads twice.
    # Photo: just a photo.
    if is_video(file_path):
        if SEND_PREVIEW or not SEND_DOCUMENT:
            metadata = await asyncio.to_thread(probe_video_metadata, file_path)
            with file_path.open("rb") as media_file:
                await message.reply_video(
                    video=media_file,
                    width=metadata.width,
                    height=metadata.height,
                    duration=metadata.duration,
                    supports_streaming=True,
                )

        if SEND_DOCUMENT:
            with file_path.open("rb") as document_file:
                await message.reply_document(
                    document=document_file,
                    filename=file_path.name,
                    disable_content_type_detection=True,
                )
        return

    if is_image(file_path):
        with file_path.open("rb") as media_file:
            if SEND_PREVIEW or not SEND_DOCUMENT:
                await message.reply_photo(photo=media_file)
            else:
                await message.reply_document(
                    document=media_file,
                    filename=file_path.name,
                    disable_content_type_detection=True,
                )
        return

    with file_path.open("rb") as media_file:
        await message.reply_document(
            document=media_file,
            filename=file_path.name,
            disable_content_type_detection=True,
        )


async def send_random_category_b_joke(message) -> None:
    if not CATEGORY_B_JOKES:
        return
    await message.reply_text(random.choice(CATEGORY_B_JOKES))


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text:
        return

    if not await ensure_authorized(update):
        return

    url = extract_media_url(message.text)
    if not url:
        await message.reply_text(
            "Не вижу ссылку Instagram/TikTok.\n"
            "Пришли URL вида:\n"
            "• https://www.instagram.com/...\n"
            "• https://www.tiktok.com/... или https://vm.tiktok.com/..."
        )
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

        sent_video = False
        for file_path in result.files:
            try:
                await send_media(message, file_path)
                if is_video(file_path):
                    sent_video = True
            except TelegramError as send_error:
                await message.reply_text(f"Не отправил {file_path.name}: {send_error}")

        if sent_video:
            try:
                await send_random_category_b_joke(message)
            except TelegramError:
                pass

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
            'export TELEGRAM_AUTH_ANSWER="xotabeach"\n'
            "python3 instagram_telegram_bot.py"
        )

    if COOKIES_FILE and not Path(COOKIES_FILE).expanduser().exists():
        raise SystemExit(
            f"Файл cookies не найден: {COOKIES_FILE}\n\n"
            "Для фото Instagram нужен cookies.txt:\n"
            "1) Зайди в Instagram в Chrome\n"
            "2) Расширением «Get cookies.txt LOCALLY» экспортируй cookies\n"
            f"3) Сохрани файл сюда: {COOKIES_FILE}\n"
            "4) Запусти бота снова\n\n"
            "Либо временно убери INSTAGRAM_COOKIES_FILE из .env "
            "(тогда сработают в основном только видео/TikTok)."
        )

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("Instagram Telegram bot started.")
    print(f"Cookies browser: {COOKIES_BROWSER or 'нет'}")
    print(f"Cookies file: {COOKIES_FILE or 'нет'}")
    print(f"Auth question: {AUTH_QUESTION}")
    print(f"Authorized users: {len(AUTHORIZED_USER_IDS)}")
    print(f"Category B jokes: {len(CATEGORY_B_JOKES)}")
    print(f"Send preview: {SEND_PREVIEW}, send document: {SEND_DOCUMENT}")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
