#!/Library/Frameworks/Python.framework/Versions/3.14/bin/python3
"""Telegram bot: send Instagram/TikTok/YouTube URL → get playable media."""

from __future__ import annotations

import asyncio
import json
import os
import random
import secrets
import shutil
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    InputMediaPhoto,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from instagram_core import (
    TELEGRAM_MAX_UPLOAD_BYTES,
    YOUTUBE_QUALITY_HEIGHTS,
    compress_video_for_telegram,
    download_instagram_media,
    estimate_compress_for_file,
    extract_media_url,
    format_mb,
    inspect_youtube_video,
    is_image,
    is_video,
    is_youtube_url,
    probe_video_metadata,
)

MAX_UPLOAD_BYTES = TELEGRAM_MAX_UPLOAD_BYTES
MEDIA_GROUP_LIMIT = 10
JOB_TTL_SECONDS = 30 * 60
PROGRESS_INTERVAL_SECONDS = 10
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
YOUTUBE_COOKIES_BROWSER = os.getenv("YOUTUBE_COOKIES_FROM_BROWSER", "").strip() or None
YOUTUBE_COOKIES_FILE = os.getenv("YOUTUBE_COOKIES_FILE", "").strip() or None
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
_pending_jobs: dict[str, "PendingJob"] = {}
_heavy_lock = asyncio.Lock()


@dataclass
class PendingJob:
    kind: str
    user_id: int
    url: str
    created: float
    workdir: Path | None = None
    oversized: list[Path] = field(default_factory=list)
    sent_video: bool = False
    partial: bool = False
    height: int | None = None
    busy: bool = False


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
    query = update.callback_query
    message = update.message
    user = update.effective_user
    if not user:
        return False

    if is_authorized(user.id):
        return True

    if query:
        await query.answer("Сначала напиши /start и ответь на вопрос.", show_alert=True)
        return False

    if not message:
        return False

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
            "Пришли ссылку на Instagram, TikTok или YouTube."
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
        "Пришли ссылку на Instagram, TikTok или YouTube.\n"
        "Я скачаю и отправлю медиа как есть:\n"
        "• YouTube — выбор качества (1080p/720p/480p/360p), до 20 минут\n"
        "• видео — в исходном разрешении и соотношении сторон\n"
        "• если файл больше ~50 MB — предложу сжать\n"
        "• фото — одно фото или альбом, если их несколько\n"
        "• карусель — фото листаются вместе (до 10 в сообщении)\n"
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


def _chunks(items: list[Path], size: int) -> list[list[Path]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


async def send_photos(message, file_paths: list[Path]) -> None:
    """Send 2+ photos as swipeable albums of up to 10; a single photo stays a photo."""
    send_as_document = SEND_DOCUMENT and not SEND_PREVIEW
    for chunk in _chunks(file_paths, MEDIA_GROUP_LIMIT):
        if len(chunk) == 1:
            await send_media(message, chunk[0])
            continue

        handles = [path.open("rb") for path in chunk]
        try:
            if send_as_document:
                media = [
                    InputMediaDocument(media=handle, filename=path.name)
                    for handle, path in zip(handles, chunk)
                ]
            else:
                media = [InputMediaPhoto(media=handle) for handle in handles]
            await message.reply_media_group(media=media)
        finally:
            for handle in handles:
                handle.close()


async def send_random_category_b_joke(message) -> None:
    if not CATEGORY_B_JOKES:
        return
    await message.reply_text(random.choice(CATEGORY_B_JOKES))


def cleanup_workdir(path: Path | None) -> None:
    if not path:
        return
    shutil.rmtree(path, ignore_errors=True)


def purge_expired_jobs() -> None:
    # A job being compressed keeps its workdir: ffmpeg still reads from it.
    now = time.monotonic()
    for job_id, job in list(_pending_jobs.items()):
        if not job.busy and now - job.created > JOB_TTL_SECONDS:
            cleanup_workdir(job.workdir)
            _pending_jobs.pop(job_id, None)


def cancel_user_jobs(user_id: int) -> None:
    for job_id, job in list(_pending_jobs.items()):
        if job.user_id == user_id and not job.busy:
            cleanup_workdir(job.workdir)
            _pending_jobs.pop(job_id, None)


def quality_keyboard(job_id: str, qualities: list[int]) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(f"{height}p", callback_data=f"q:{job_id}:{height}")
        for height in qualities
    ]
    rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("Отмена", callback_data=f"x:{job_id}")])
    return InlineKeyboardMarkup(rows)


def compress_keyboard(job_id: str, lower_qualities: list[int]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("Сжать до 50 MB", callback_data=f"c:{job_id}")]]
    if lower_qualities:
        rows.append(
            [
                InlineKeyboardButton(f"Скачать {height}p", callback_data=f"q:{job_id}:{height}")
                for height in lower_qualities
            ]
        )
    rows.append([InlineKeyboardButton("Отмена", callback_data=f"x:{job_id}")])
    return InlineKeyboardMarkup(rows)


def lower_qualities_for(job: "PendingJob") -> list[int]:
    """Re-downloading YouTube in a smaller size beats re-encoding on this server."""
    if not is_youtube_url(job.url):
        return []
    ceiling = job.height or max(YOUTUBE_QUALITY_HEIGHTS)
    return [height for height in YOUTUBE_QUALITY_HEIGHTS if height < ceiling][:2]


async def offer_compress(status, job_id: str, job: "PendingJob") -> None:
    file_path = job.oversized[0]
    lower = lower_qualities_for(job)
    estimate = await asyncio.to_thread(estimate_compress_for_file, file_path)
    hint = f"Сжатие займёт ~{max(estimate // 60, 1)} мин"
    hint += " — перекачать в меньшем качестве быстрее." if lower else "."
    await status.edit_text(
        f"Файл слишком большой для Telegram ({format_mb(file_path.stat().st_size)}).\n"
        f"Лимит ~50 MB. {hint}",
        reply_markup=compress_keyboard(job_id, lower),
    )


async def compress_with_progress(status, source: Path) -> Path:
    """Compress in a worker thread while the status message shows live percent."""
    state = {"percent": 0.0}
    task = asyncio.create_task(
        asyncio.to_thread(
            compress_video_for_telegram,
            source,
            progress=lambda percent: state.update(percent=percent),
        )
    )

    shown = -1
    while not task.done():
        done, _ = await asyncio.wait({task}, timeout=PROGRESS_INTERVAL_SECONDS)
        if done:
            break
        percent = int(state["percent"])
        if percent != shown:
            shown = percent
            try:
                await status.edit_text(f"Сжимаю до ~50 MB... {percent}%")
            except TelegramError:
                pass

    return await task


async def finish_delivery(reply_target, status, sent_video: bool, partial: bool, workdir: Path | None) -> None:
    if sent_video:
        try:
            await send_random_category_b_joke(reply_target)
        except TelegramError:
            pass
    note = "Готово частично: часть элементов могла не скачаться." if partial else "Готово."
    await status.edit_text(note)
    cleanup_workdir(workdir)


async def deliver_or_offer(
    reply_target,
    status,
    *,
    user_id: int,
    url: str,
    workdir: Path,
    files: list[Path],
    partial: bool,
    sent_video: bool = False,
    max_height: int | None = None,
) -> None:
    oversized: list[Path] = []
    photo_batch: list[Path] = []

    async def flush_photos() -> None:
        nonlocal photo_batch
        if not photo_batch:
            return
        try:
            await send_photos(reply_target, photo_batch)
        except TelegramError as send_error:
            await reply_target.reply_text(f"Не отправил фото: {send_error}")
        photo_batch = []

    for file_path in files:
        if is_image(file_path):
            photo_batch.append(file_path)
            if len(photo_batch) >= MEDIA_GROUP_LIMIT:
                await flush_photos()
            continue

        await flush_photos()
        if is_video(file_path) and file_path.stat().st_size > MAX_UPLOAD_BYTES:
            oversized.append(file_path)
            continue
        try:
            await send_media(reply_target, file_path)
            if is_video(file_path):
                sent_video = True
        except TelegramError as send_error:
            await reply_target.reply_text(f"Не отправил {file_path.name}: {send_error}")

    await flush_photos()

    if oversized:
        job_id = secrets.token_hex(8)
        job = PendingJob(
            kind="compress",
            user_id=user_id,
            url=url,
            created=time.monotonic(),
            workdir=workdir,
            oversized=oversized,
            sent_video=sent_video,
            partial=partial,
            height=max_height,
        )
        _pending_jobs[job_id] = job
        await offer_compress(status, job_id, job)
        return

    await finish_delivery(reply_target, status, sent_video, partial, workdir)


async def run_download(
    reply_target,
    status,
    *,
    user_id: int,
    url: str,
    max_height: int | None = None,
) -> None:
    workdir = Path(tempfile.mkdtemp(prefix="ig_tg_"))
    try:
        if _heavy_lock.locked():
            await status.edit_text("Подожди, обрабатывается другое видео...")
        async with _heavy_lock:
            await reply_target.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
            result = await asyncio.to_thread(
                download_instagram_media,
                url,
                workdir,
                cookies_browser=COOKIES_BROWSER,
                cookies_file=COOKIES_FILE,
                youtube_cookies_browser=YOUTUBE_COOKIES_BROWSER,
                youtube_cookies_file=YOUTUBE_COOKIES_FILE,
                max_height=max_height,
            )

        if not result.files:
            details = "\n".join(result.messages[-8:]) if result.messages else (result.error or "unknown")
            await status.edit_text(f"Не удалось скачать.\n\n{details}")
            cleanup_workdir(workdir)
            return

        await status.edit_text(f"Скачано файлов: {len(result.files)}. Отправляю...")
        await deliver_or_offer(
            reply_target,
            status,
            user_id=user_id,
            url=url,
            workdir=workdir,
            files=result.files,
            partial=result.partial,
            max_height=max_height,
        )
    except Exception:
        cleanup_workdir(workdir)
        raise


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if not message or not message.text or not user:
        return

    if not await ensure_authorized(update):
        return

    purge_expired_jobs()
    url = extract_media_url(message.text)
    if not url:
        await message.reply_text(
            "Не вижу ссылку Instagram/TikTok/YouTube.\n"
            "Пришли URL вида:\n"
            "• https://www.instagram.com/...\n"
            "• https://www.tiktok.com/... или https://vm.tiktok.com/...\n"
            "• https://youtu.be/... или https://www.youtube.com/watch?v=..."
        )
        return

    cancel_user_jobs(user.id)

    if is_youtube_url(url):
        status = await message.reply_text("Смотрю ролик...")
        info = await asyncio.to_thread(
            inspect_youtube_video,
            url,
            cookies_browser=YOUTUBE_COOKIES_BROWSER,
            cookies_file=YOUTUBE_COOKIES_FILE,
        )
        if info.error:
            await status.edit_text(info.error)
            return
        job_id = secrets.token_hex(8)
        _pending_jobs[job_id] = PendingJob(
            kind="quality",
            user_id=user.id,
            url=url,
            created=time.monotonic(),
        )
        title = (info.title or "YouTube").strip()[:80]
        duration = ""
        if info.duration:
            duration = f" · {info.duration // 60} мин {info.duration % 60:02d} с"
        await status.edit_text(
            f"{title}{duration}\nВыбери качество:",
            reply_markup=quality_keyboard(job_id, info.qualities),
        )
        return

    status = await message.reply_text("Скачиваю...")
    try:
        await run_download(message, status, user_id=user.id, url=url)
    except Exception as error:
        await status.edit_text(f"Ошибка: {error}")
        traceback.print_exc()


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return

    if not await ensure_authorized(update):
        return

    await query.answer()
    purge_expired_jobs()

    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 2:
        await query.edit_message_text("Эта кнопка уже не действует.")
        return

    action, job_id = parts[0], parts[1]
    job = _pending_jobs.get(job_id)
    if not job or job.user_id != user.id:
        await query.edit_message_text("Эта кнопка уже не действует.")
        return

    status = query.message
    if not status:
        return

    if action == "x":
        cleanup_workdir(job.workdir)
        _pending_jobs.pop(job_id, None)
        await status.edit_text("Отменено.")
        return

    if action == "q" and job.kind in {"quality", "compress"}:
        if len(parts) < 3 or not parts[2].isdigit():
            await status.edit_text("Неизвестное качество.")
            return
        height = int(parts[2])
        cleanup_workdir(job.workdir)
        _pending_jobs.pop(job_id, None)
        await status.edit_text(f"Скачиваю {height}p...")
        try:
            await run_download(
                status,
                status,
                user_id=user.id,
                url=job.url,
                max_height=height,
            )
        except Exception as error:
            await status.edit_text(f"Ошибка: {error}")
            traceback.print_exc()
        return

    if action == "c" and job.kind == "compress":
        if not job.oversized:
            _pending_jobs.pop(job_id, None)
            await status.edit_text("Файл уже обработан.")
            return
        source = job.oversized[0]
        await status.edit_text("Сжимаю до ~50 MB...")
        job.busy = True
        try:
            if _heavy_lock.locked():
                await status.edit_text("Подожди, обрабатывается другое видео...")
            async with _heavy_lock:
                compressed = await compress_with_progress(status, source)
        except Exception as error:
            await status.edit_text(f"Не удалось сжать: {error}")
            cleanup_workdir(job.workdir)
            _pending_jobs.pop(job_id, None)
            traceback.print_exc()
            return
        finally:
            job.busy = False

        job.oversized.pop(0)
        try:
            await send_media(status, compressed)
            job.sent_video = True
        except TelegramError as send_error:
            await status.reply_text(f"Не отправил {compressed.name}: {send_error}")

        if job.oversized:
            await offer_compress(status, job_id, job)
            return

        _pending_jobs.pop(job_id, None)
        await finish_delivery(status, status, job.sent_video, job.partial, job.workdir)
        return

    await status.edit_text("Эта кнопка уже не действует.")


def main() -> None:
    global YOUTUBE_COOKIES_FILE

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

    # Only age-restricted videos need this file, so a missing one is a warning:
    # dying here would take the whole bot down over an optional extra.
    if YOUTUBE_COOKIES_FILE and not Path(YOUTUBE_COOKIES_FILE).expanduser().exists():
        print(
            f"ВНИМАНИЕ: файл YouTube cookies не найден: {YOUTUBE_COOKIES_FILE}\n"
            "Ролики 18+ скачиваться не будут, остальные — как обычно.\n"
            "Экспортируй cookies с youtube.com и положи файл по этому пути."
        )
        YOUTUBE_COOKIES_FILE = None

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("Instagram Telegram bot started.")
    print(f"Instagram cookies browser: {COOKIES_BROWSER or 'нет'}")
    print(f"Instagram cookies file: {COOKIES_FILE or 'нет'}")
    print(f"YouTube cookies browser: {YOUTUBE_COOKIES_BROWSER or 'нет'}")
    print(f"YouTube cookies file: {YOUTUBE_COOKIES_FILE or 'нет'}")
    print(f"Auth question: {AUTH_QUESTION}")
    print(f"Authorized users: {len(AUTHORIZED_USER_IDS)}")
    print(f"Category B jokes: {len(CATEGORY_B_JOKES)}")
    print(f"Send preview: {SEND_PREVIEW}, send document: {SEND_DOCUMENT}")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
