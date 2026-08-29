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
from datetime import datetime, timezone
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
    cookies_file_has_sessionid,
    detect_instagram_auth_failure,
    download_instagram_media,
    estimate_compress_for_file,
    extract_media_url,
    format_mb,
    get_media_files,
    inspect_youtube_video,
    is_audio_only,
    is_image,
    is_instagram_url,
    is_tiktok_url,
    is_video,
    is_youtube_url,
    probe_video_metadata,
)

MAX_UPLOAD_BYTES = TELEGRAM_MAX_UPLOAD_BYTES
MEDIA_GROUP_LIMIT = 10
JOB_TTL_SECONDS = 30 * 60
PROGRESS_INTERVAL_SECONDS = 10
ACTIVE_DAYS = (7, 30)
PLATFORMS = ("instagram", "tiktok", "youtube")
AUTH_STORE_PATH = Path(__file__).with_name("authorized_users.json")
STATS_STORE_PATH = Path(__file__).with_name("bot_stats.json")
COOKIES_STATE_PATH = Path(__file__).with_name("cookies_state.json")
JOKES_PATH = Path(__file__).with_name("category_b_jokes.json")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not str(value).strip():
        return default
    try:
        return int(str(value).strip())
    except ValueError:
        return default


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
COOKIES_DIR = Path(
    os.getenv("INSTAGRAM_COOKIES_DIR", "").strip()
    or str(Path(__file__).with_name("instagram_cookies"))
).expanduser()
COOKIE_SLOTS = max(1, min(env_int("INSTAGRAM_COOKIE_SLOTS", 5), 10))
IG_MAX_PER_HOUR = max(1, env_int("INSTAGRAM_MAX_DOWNLOADS_PER_HOUR", 20))
IG_MIN_INTERVAL_SECONDS = max(0, env_int("INSTAGRAM_MIN_INTERVAL_SECONDS", 25))
# Dead slots stay skipped this long unless admin re-uploads them.
COOKIE_DEAD_COOLDOWN_SECONDS = max(60, env_int("INSTAGRAM_COOKIE_DEAD_COOLDOWN_SECONDS", 6 * 3600))
YOUTUBE_COOKIES_BROWSER = os.getenv("YOUTUBE_COOKIES_FROM_BROWSER", "").strip() or None
YOUTUBE_COOKIES_FILE = os.getenv("YOUTUBE_COOKIES_FILE", "").strip() or None
SEND_PREVIEW = env_bool("TELEGRAM_SEND_PREVIEW", True)
SEND_DOCUMENT = env_bool("TELEGRAM_SEND_DOCUMENT", False)
# Telegram compresses reply_photo; set TELEGRAM_PHOTO_PREVIEW=false to send
# originals as documents instead (full resolution / aspect ratio).
PHOTO_PREVIEW = env_bool("TELEGRAM_PHOTO_PREVIEW", True)
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
_ig_rate_lock = asyncio.Lock()
_ig_download_times: list[float] = []
_ig_cookie_rr = 0


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


def load_authorized_user_ids() -> list[int]:
    """Preserve file order: the first id is the owner/admin."""
    if not AUTH_STORE_PATH.exists():
        return []
    try:
        data = json.loads(AUTH_STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    seen: set[int] = set()
    ordered: list[int] = []
    for item in data:
        try:
            user_id = int(item)
        except (TypeError, ValueError):
            continue
        if user_id in seen:
            continue
        seen.add(user_id)
        ordered.append(user_id)
    return ordered


def save_authorized_user_ids(user_ids: list[int]) -> None:
    AUTH_STORE_PATH.write_text(
        json.dumps(user_ids, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


AUTHORIZED_USER_IDS = load_authorized_user_ids()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def platform_for_url(url: str) -> str:
    if is_instagram_url(url):
        return "instagram"
    if is_tiktok_url(url):
        return "tiktok"
    if is_youtube_url(url):
        return "youtube"
    return "other"


def empty_platform_stats() -> dict[str, int]:
    return {"ok": 0, "fail": 0, "videos": 0, "photos": 0}


def load_stats() -> dict:
    if not STATS_STORE_PATH.exists():
        return {"users": {}}
    try:
        data = json.loads(STATS_STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"users": {}}
    if not isinstance(data, dict):
        return {"users": {}}
    users = data.get("users")
    if not isinstance(users, dict):
        data["users"] = {}
    return data


def save_stats(data: dict) -> None:
    STATS_STORE_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def ensure_user_stats(data: dict, user_id: int, username: str | None = None) -> dict:
    users = data.setdefault("users", {})
    key = str(user_id)
    entry = users.get(key)
    if not isinstance(entry, dict):
        now = utc_now_iso()
        entry = {
            "username": username or "",
            "first_seen": now,
            "last_active": now,
            "platforms": {name: empty_platform_stats() for name in PLATFORMS},
        }
        users[key] = entry
    if username:
        entry["username"] = username
    platforms = entry.setdefault("platforms", {})
    for name in PLATFORMS:
        bucket = platforms.get(name)
        if not isinstance(bucket, dict):
            platforms[name] = empty_platform_stats()
            continue
        for field_name, default in empty_platform_stats().items():
            bucket.setdefault(field_name, default)
    return entry


def touch_user_activity(user_id: int, username: str | None = None) -> None:
    data = load_stats()
    entry = ensure_user_stats(data, user_id, username)
    entry["last_active"] = utc_now_iso()
    save_stats(data)


def record_download(
    user_id: int,
    url: str,
    *,
    success: bool,
    files: list[Path] | None = None,
    username: str | None = None,
) -> None:
    platform = platform_for_url(url)
    if platform not in PLATFORMS:
        return

    data = load_stats()
    entry = ensure_user_stats(data, user_id, username)
    entry["last_active"] = utc_now_iso()
    bucket = entry["platforms"][platform]
    if success:
        bucket["ok"] += 1
        for path in files or []:
            if is_video(path):
                bucket["videos"] += 1
            elif is_image(path):
                bucket["photos"] += 1
    else:
        bucket["fail"] += 1
    save_stats(data)


def parse_iso(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def format_user_label(user_id: str, entry: dict) -> str:
    username = (entry.get("username") or "").strip().lstrip("@")
    if username:
        return f"@{username}"
    return f"id:{user_id}"


def format_stats_report() -> str:
    data = load_stats()
    users = data.get("users") or {}
    now = datetime.now(timezone.utc)

    totals = {name: empty_platform_stats() for name in PLATFORMS}
    active_counts = {days: 0 for days in ACTIVE_DAYS}
    ranked: list[tuple[int, str, dict]] = []

    for user_id, entry in users.items():
        if not isinstance(entry, dict):
            continue
        platforms = entry.get("platforms") or {}
        user_total_ok = 0
        for name in PLATFORMS:
            bucket = platforms.get(name) or {}
            for field_name in empty_platform_stats():
                totals[name][field_name] += int(bucket.get(field_name) or 0)
            user_total_ok += int(bucket.get("ok") or 0)
        ranked.append((user_total_ok, user_id, entry))

        last_active = parse_iso(entry.get("last_active"))
        if last_active is not None:
            if last_active.tzinfo is None:
                last_active = last_active.replace(tzinfo=timezone.utc)
            age_days = (now - last_active).total_seconds() / 86400
            for days in ACTIVE_DAYS:
                if age_days <= days:
                    active_counts[days] += 1

    total_ok = sum(totals[name]["ok"] for name in PLATFORMS)
    total_fail = sum(totals[name]["fail"] for name in PLATFORMS)
    total_videos = sum(totals[name]["videos"] for name in PLATFORMS)
    total_photos = sum(totals[name]["photos"] for name in PLATFORMS)

    lines = [
        "Статистика бота",
        "",
        f"Авторизованных: {len(AUTHORIZED_USER_IDS)}",
        f"С активностью в статистике: {len(users)}",
        f"Активных за 7 дней: {active_counts[7]}",
        f"Активных за 30 дней: {active_counts[30]}",
        "",
        f"Скачиваний успешно: {total_ok}",
        f"Ошибок: {total_fail}",
        f"Видео: {total_videos} · фото: {total_photos}",
        "",
        "По платформам:",
    ]
    labels = {"instagram": "Instagram", "tiktok": "TikTok", "youtube": "YouTube"}
    for name in PLATFORMS:
        bucket = totals[name]
        lines.append(
            f"• {labels[name]}: {bucket['ok']} ок / {bucket['fail']} ошибок "
            f"({bucket['videos']} видео, {bucket['photos']} фото)"
        )

    ranked.sort(key=lambda item: (-item[0], item[1]))
    lines.extend(["", "Топ пользователей:"])
    if not ranked:
        lines.append("пока пусто — счёт идёт с момента включения статистики")
    else:
        for ok_count, user_id, entry in ranked[:15]:
            platforms = entry.get("platforms") or {}
            parts = []
            for name in PLATFORMS:
                bucket = platforms.get(name) or {}
                ok = int(bucket.get("ok") or 0)
                if ok:
                    short = {"instagram": "IG", "tiktok": "TT", "youtube": "YT"}[name]
                    parts.append(f"{short}:{ok}")
            breakdown = ", ".join(parts) if parts else "0"
            lines.append(f"• {format_user_label(user_id, entry)} — {ok_count} ({breakdown})")

    return "\n".join(lines)


def is_admin(user_id: int | None) -> bool:
    """Only the first authorized user (owner) can see /stats."""
    return bool(AUTHORIZED_USER_IDS) and user_id == AUTHORIZED_USER_IDS[0]


def load_cookies_state() -> dict:
    if not COOKIES_STATE_PATH.exists():
        return {"slots": {}, "rr": 0}
    try:
        data = json.loads(COOKIES_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"slots": {}, "rr": 0}
    if not isinstance(data, dict):
        return {"slots": {}, "rr": 0}
    data.setdefault("slots", {})
    data.setdefault("rr", 0)
    return data


def save_cookies_state(data: dict) -> None:
    COOKIES_STATE_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def cookies_mtime_iso(path: str | Path | None) -> str | None:
    if not path:
        return None
    file_path = Path(path).expanduser()
    if not file_path.exists():
        return None
    return datetime.fromtimestamp(file_path.stat().st_mtime, timezone.utc).replace(
        microsecond=0
    ).isoformat()


def ensure_cookie_pool() -> None:
    """Create pool dir and migrate legacy INSTAGRAM_COOKIES_FILE into slot 1."""
    COOKIES_DIR.mkdir(parents=True, exist_ok=True)
    slot1 = COOKIES_DIR / "1.txt"
    if slot1.exists():
        return
    if COOKIES_FILE:
        legacy = Path(COOKIES_FILE).expanduser()
        if legacy.exists() and cookies_file_has_sessionid(legacy):
            slot1.write_bytes(legacy.read_bytes())


def cookie_slot_paths() -> list[Path]:
    ensure_cookie_pool()
    paths = [COOKIES_DIR / f"{index}.txt" for index in range(1, COOKIE_SLOTS + 1)]
    # Legacy single-file path only if the pool itself is still empty.
    if COOKIES_FILE and not any(path.exists() for path in paths):
        legacy = Path(COOKIES_FILE).expanduser()
        if legacy.exists():
            paths.append(legacy)
    return paths


def cookie_slot_key(path: Path) -> str:
    return str(path.resolve())


def cookie_slot_label(path: Path) -> str:
    try:
        if path.parent.resolve() == COOKIES_DIR.resolve():
            return path.stem
    except OSError:
        pass
    return path.name


def is_cookie_slot_dead(path: Path, state: dict | None = None) -> bool:
    state = state or load_cookies_state()
    entry = (state.get("slots") or {}).get(cookie_slot_key(path)) or {}
    dead_until = entry.get("dead_until")
    if not dead_until:
        return False
    try:
        until = datetime.fromisoformat(dead_until)
    except ValueError:
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) < until


def mark_cookie_slot_dead(path: Path, reason: str) -> None:
    state = load_cookies_state()
    slots = state.setdefault("slots", {})
    key = cookie_slot_key(path)
    dead_until = datetime.now(timezone.utc).timestamp() + COOKIE_DEAD_COOLDOWN_SECONDS
    slots[key] = {
        "dead_until": datetime.fromtimestamp(dead_until, timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "last_error": reason,
        "marked_at": utc_now_iso(),
    }
    save_cookies_state(state)


def revive_cookie_slot(path: Path) -> None:
    state = load_cookies_state()
    slots = state.setdefault("slots", {})
    key = cookie_slot_key(path)
    if key in slots:
        slots[key] = {
            "dead_until": None,
            "last_error": None,
            "marked_at": None,
            "revived_at": utc_now_iso(),
        }
        save_cookies_state(state)


def list_alive_cookie_slots() -> list[Path]:
    state = load_cookies_state()
    alive: list[Path] = []
    for path in cookie_slot_paths():
        if not path.exists():
            continue
        if not cookies_file_has_sessionid(path):
            continue
        if is_cookie_slot_dead(path, state):
            continue
        alive.append(path)
    return alive


def pick_cookie_slot(exclude: set[Path] | None = None) -> Path | None:
    global _ig_cookie_rr
    alive = list_alive_cookie_slots()
    if exclude:
        alive = [path for path in alive if path.resolve() not in {p.resolve() for p in exclude}]
    if not alive:
        return None
    state = load_cookies_state()
    start = int(state.get("rr") or _ig_cookie_rr or 0) % len(alive)
    path = alive[start]
    state["rr"] = (start + 1) % len(alive)
    _ig_cookie_rr = state["rr"]
    save_cookies_state(state)
    return path


def prune_ig_rate_times(now: float | None = None) -> list[float]:
    global _ig_download_times
    now = time.monotonic() if now is None else now
    cutoff = now - 3600
    _ig_download_times = [stamp for stamp in _ig_download_times if stamp >= cutoff]
    return _ig_download_times


def ig_rate_limit_wait_seconds() -> int:
    """Return seconds to wait, or 0 if a download is allowed now."""
    now = time.monotonic()
    stamps = prune_ig_rate_times(now)
    if IG_MIN_INTERVAL_SECONDS and stamps:
        since_last = now - stamps[-1]
        if since_last < IG_MIN_INTERVAL_SECONDS:
            return int(IG_MIN_INTERVAL_SECONDS - since_last) + 1
    if len(stamps) >= IG_MAX_PER_HOUR:
        return int(stamps[0] + 3600 - now) + 1
    return 0


def record_ig_download_attempt() -> None:
    now = time.monotonic()
    prune_ig_rate_times(now)
    _ig_download_times.append(now)


def netscape_cookie_names(text: str, domain_needle: str) -> set[str]:
    names: set[str] = set()
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        if domain_needle not in parts[0].lower():
            continue
        if parts[5].strip():
            names.add(parts[5].strip())
    return names


def detect_cookies_platform(text: str) -> str | None:
    ig = netscape_cookie_names(text, "instagram.com")
    yt = netscape_cookie_names(text, "youtube.com") | netscape_cookie_names(text, "google.com")
    if "sessionid" in ig:
        return "instagram"
    if yt & {"SID", "HSID", "SSID", "LOGIN_INFO", "__Secure-1PSID", "SAPISID"}:
        return "youtube"
    if ig:
        return "instagram"
    if yt:
        return "youtube"
    return None


def validate_cookies_text(text: str, platform: str) -> str | None:
    """Return error message or None if cookies look usable."""
    if "\t" not in text and "# Netscape" not in text:
        return "Это не Netscape cookies.txt (нужен экспорт из «Get cookies.txt LOCALLY»)."
    if platform == "instagram":
        names = netscape_cookie_names(text, "instagram.com")
        if "sessionid" not in names:
            return "В файле нет sessionid для Instagram."
        return None
    if platform == "youtube":
        names = netscape_cookie_names(text, "youtube.com") | netscape_cookie_names(
            text, "google.com"
        )
        if not (names & {"SID", "HSID", "SSID", "LOGIN_INFO", "__Secure-1PSID", "SAPISID"}):
            return "В файле нет типичных YouTube/Google login cookies."
        return None
    return "Неизвестный тип cookies."


def install_cookies_file(target: str | Path, content: bytes) -> Path:
    path = Path(target).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(content)
    tmp.replace(path)
    revive_cookie_slot(path)
    state = load_cookies_state()
    state["last_upload_at"] = utc_now_iso()
    state["last_alert_at"] = None
    state["last_alert_reason"] = None
    save_cookies_state(state)
    return path


def install_instagram_cookies_to_pool(content: bytes) -> Path:
    """Put cookies into an empty/dead pool slot, else overwrite next round-robin slot."""
    ensure_cookie_pool()
    state = load_cookies_state()
    pool_paths = [COOKIES_DIR / f"{index}.txt" for index in range(1, COOKIE_SLOTS + 1)]

    for path in pool_paths:
        if not path.exists():
            return install_cookies_file(path, content)

    for path in pool_paths:
        if is_cookie_slot_dead(path, state) or not cookies_file_has_sessionid(path):
            return install_cookies_file(path, content)

    index = int(state.get("rr") or 0) % len(pool_paths)
    return install_cookies_file(pool_paths[index], content)


def format_cookies_status() -> str:
    ensure_cookie_pool()
    state = load_cookies_state()
    lines = [
        "Instagram cookie pool",
        f"Лимит: ≤{IG_MAX_PER_HOUR}/час, пауза ≥{IG_MIN_INTERVAL_SECONDS}с (на весь бот)",
    ]
    alive = 0
    for path in [COOKIES_DIR / f"{index}.txt" for index in range(1, COOKIE_SLOTS + 1)]:
        label = cookie_slot_label(path)
        if not path.exists():
            lines.append(f"• слот {label}: пусто")
            continue
        has_sid = cookies_file_has_sessionid(path)
        dead = is_cookie_slot_dead(path, state)
        entry = (state.get("slots") or {}).get(cookie_slot_key(path)) or {}
        if has_sid and not dead:
            alive += 1
            status = "живой"
        elif dead:
            status = f"мёртвый до {entry.get('dead_until', '?')}"
        else:
            status = "нет sessionid"
        lines.append(
            f"• слот {label}: {status}, обновлён {cookies_mtime_iso(path) or '—'}"
        )
    lines.append(f"Живых слотов: {alive}/{COOKIE_SLOTS}")

    yt_path = Path(YOUTUBE_COOKIES_FILE).expanduser() if YOUTUBE_COOKIES_FILE else None
    if yt_path:
        lines.append(
            f"YouTube: {'есть' if yt_path.exists() else 'нет файла'}"
            + (f", обновлён {cookies_mtime_iso(yt_path)}" if yt_path.exists() else "")
        )

    if state.get("last_alert_at"):
        lines.append(
            f"Последний алерт: {state['last_alert_at']}"
            + (f" ({state.get('last_alert_reason')})" if state.get("last_alert_reason") else "")
        )
    lines.append("")
    lines.append(
        "Пришли cookies.txt файлом — попадёт в пул (пустой/мёртвый слот, иначе next).\n"
        "Пул общий для всех пользователей бота. Не разлогинивайся в браузере после экспорта."
    )
    return "\n".join(lines)


async def notify_admin_cookies_dead(bot, reason: str) -> None:
    if not AUTHORIZED_USER_IDS:
        return
    state = load_cookies_state()
    alive = list_alive_cookie_slots()
    # Don't spam while the pool fingerprint is unchanged.
    fingerprint = "|".join(
        f"{cookie_slot_label(path)}:{cookies_mtime_iso(path)}"
        for path in cookie_slot_paths()
        if path.exists()
    )
    if (
        state.get("last_alert_at")
        and state.get("pool_fingerprint") == fingerprint
        and state.get("last_alert_reason") == reason
        and not alive
    ):
        return

    text = (
        "Instagram cookies: проблема с пулом.\n"
        f"Причина: {reason}\n"
        f"Живых слотов: {len(alive)}/{COOKIE_SLOTS}\n\n"
        "Пришли новый cookies.txt сюда файлом "
        "(Get cookies.txt LOCALLY → Export).\n"
        "/cookies — статус пула."
    )
    try:
        await bot.send_message(chat_id=AUTHORIZED_USER_IDS[0], text=text)
    except TelegramError:
        return

    state["last_alert_at"] = utc_now_iso()
    state["last_alert_reason"] = reason
    state["pool_fingerprint"] = fingerprint
    save_cookies_state(state)


def normalize_answer(text: str) -> str:
    return text.strip().lower().lstrip("@")


def is_authorized(user_id: int | None) -> bool:
    return user_id is not None and user_id in AUTHORIZED_USER_IDS


def authorize_user(user_id: int, username: str | None = None) -> None:
    if user_id not in AUTHORIZED_USER_IDS:
        AUTHORIZED_USER_IDS.append(user_id)
    _pending_auth.discard(user_id)
    save_authorized_user_ids(AUTHORIZED_USER_IDS)
    touch_user_activity(user_id, username)


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
        authorize_user(user.id, user.username)
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
        "• фото — превью в чате (для оригинала без сжатия: TELEGRAM_PHOTO_PREVIEW=false)\n"
        "• карусель — фото листаются вместе (до 10 в сообщении)\n"
        "После видео — случайный анекдот категории Б.\n"
        "Админ: /stats, /cookies (пул cookies.txt + лимит Instagram)."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not await ensure_authorized(update):
        return
    user = update.effective_user
    if user:
        touch_user_activity(user.id, user.username)
    await update.message.reply_text(welcome_text())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if not message or not user:
        return
    if not await ensure_authorized(update):
        return
    if not is_admin(user.id):
        await message.reply_text("Команда только для админа.")
        return
    await message.reply_text(format_stats_report())


async def cookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if not message or not user:
        return
    if not await ensure_authorized(update):
        return
    if not is_admin(user.id):
        await message.reply_text("Команда только для админа.")
        return
    await message.reply_text(format_cookies_status())


async def handle_cookies_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if not message or not user or not message.document:
        return
    if not await ensure_authorized(update):
        return
    if not is_admin(user.id):
        return

    document = message.document
    filename = (document.file_name or "").lower()
    if document.file_size and document.file_size > 512 * 1024:
        await message.reply_text("Файл слишком большой для cookies.txt.")
        return

    tg_file = await document.get_file()
    raw = bytes(await tg_file.download_as_bytearray())
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        await message.reply_text("Не удалось прочитать файл как текст.")
        return

    platform = detect_cookies_platform(text)
    if not platform:
        # Allow explicit filenames as a hint when auto-detect fails.
        if "youtube" in filename:
            platform = "youtube"
        elif "instagram" in filename or "cookies" in filename:
            platform = "instagram"
        else:
            await message.reply_text(
                "Не похоже на Instagram/YouTube cookies.txt.\n"
                "Экспортируй через «Get cookies.txt LOCALLY» и пришли ещё раз."
            )
            return

    error = validate_cookies_text(text, platform)
    if error:
        await message.reply_text(error)
        return

    if platform == "instagram":
        path = install_instagram_cookies_to_pool(raw)
        alive = len(list_alive_cookie_slots())
        await message.reply_text(
            f"Instagram cookies добавлены в пул.\n"
            f"Слот: {cookie_slot_label(path)}\n"
            f"sessionid: ок\n"
            f"Живых слотов: {alive}/{COOKIE_SLOTS}\n"
            "Рестарт бота не нужен — пул общий для всех пользователей."
        )
        return

    if not YOUTUBE_COOKIES_FILE:
        await message.reply_text("YOUTUBE_COOKIES_FILE не задан в .env на сервере.")
        return
    path = install_cookies_file(YOUTUBE_COOKIES_FILE, raw)
    await message.reply_text(
        f"YouTube cookies обновлены.\nФайл: {path}\nРестарт бота не нужен."
    )


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
            if PHOTO_PREVIEW and (SEND_PREVIEW or not SEND_DOCUMENT):
                await message.reply_photo(photo=media_file)
            else:
                await message.reply_document(
                    document=media_file,
                    filename=file_path.name,
                    disable_content_type_detection=True,
                )
        return

    if is_audio_only(file_path):
        metadata = await asyncio.to_thread(probe_video_metadata, file_path)
        with file_path.open("rb") as media_file:
            await message.reply_audio(
                audio=media_file,
                duration=metadata.duration,
                filename=file_path.name,
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
    send_as_document = not PHOTO_PREVIEW or (SEND_DOCUMENT and not SEND_PREVIEW)
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
        if is_audio_only(file_path):
            try:
                await send_media(reply_target, file_path)
            except TelegramError as send_error:
                await reply_target.reply_text(f"Не отправил аудио: {send_error}")
            continue

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
    username: str | None = None,
) -> None:
    workdir = Path(tempfile.mkdtemp(prefix="ig_tg_"))
    try:
        if is_instagram_url(url):
            async with _ig_rate_lock:
                wait_for = ig_rate_limit_wait_seconds()
                if wait_for > 0:
                    cleanup_workdir(workdir)
                    await status.edit_text(
                        f"Слишком часто качаем Instagram (лимит на весь бот).\n"
                        f"Подожди ~{max(wait_for // 60, 1)} мин и попробуй снова.\n"
                        f"Сейчас: ≤{IG_MAX_PER_HOUR}/час, пауза ≥{IG_MIN_INTERVAL_SECONDS}с."
                    )
                    return
                record_ig_download_attempt()

        if _heavy_lock.locked():
            await status.edit_text("Подожди, обрабатывается другое видео...")
        async with _heavy_lock:
            await reply_target.chat.send_action(ChatAction.UPLOAD_DOCUMENT)

            result = None
            tried: set[Path] = set()
            cookies_file = COOKIES_FILE
            if is_instagram_url(url):
                while True:
                    slot = pick_cookie_slot(exclude=tried)
                    if slot is None:
                        break
                    tried.add(slot.resolve())
                    await status.edit_text(
                        f"Скачиваю (cookies слот {cookie_slot_label(slot)})..."
                    )
                    result = await asyncio.to_thread(
                        download_instagram_media,
                        url,
                        workdir,
                        cookies_browser=COOKIES_BROWSER,
                        cookies_file=str(slot),
                        youtube_cookies_browser=YOUTUBE_COOKIES_BROWSER,
                        youtube_cookies_file=YOUTUBE_COOKIES_FILE,
                        max_height=max_height,
                    )
                    if result.files:
                        break
                    auth_error = None
                    if result.error and str(result.error).startswith("instagram_auth:"):
                        auth_error = str(result.error).split(":", 1)[1]
                    else:
                        auth_error = detect_instagram_auth_failure(result.messages)
                    if not auth_error:
                        break
                    mark_cookie_slot_dead(slot, auth_error)
                    # Clear partial junk before trying the next account cookies.
                    for leftover in get_media_files(workdir):
                        leftover.unlink(missing_ok=True)
                    if not list_alive_cookie_slots():
                        break
                    await status.edit_text(
                        f"Слот {cookie_slot_label(slot)} отклонён Instagram, пробую другой..."
                    )
                if result is None:
                    result = await asyncio.to_thread(
                        download_instagram_media,
                        url,
                        workdir,
                        cookies_browser=COOKIES_BROWSER,
                        cookies_file=cookies_file,
                        youtube_cookies_browser=YOUTUBE_COOKIES_BROWSER,
                        youtube_cookies_file=YOUTUBE_COOKIES_FILE,
                        max_height=max_height,
                    )
            else:
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
            record_download(user_id, url, success=False, username=username)

            auth_error = None
            if is_instagram_url(url):
                if result.error and str(result.error).startswith("instagram_auth:"):
                    auth_error = str(result.error).split(":", 1)[1]
                else:
                    auth_error = detect_instagram_auth_failure(result.messages)
                if auth_error in {"", "None", None}:
                    auth_error = None

            if auth_error:
                reason = {
                    "checkpoint_required": "checkpoint / подтверждение входа",
                    "invalid_session": "сессия недействительна (login redirect)",
                    "missing_sessionid": "нет sessionid в cookies",
                }.get(auth_error, auth_error)
                await notify_admin_cookies_dead(reply_target.get_bot(), reason)
                alive = len(list_alive_cookie_slots())
                if is_admin(user_id):
                    await status.edit_text(
                        "Instagram cookies: пул не смог скачать.\n"
                        f"Причина: {reason}\n"
                        f"Живых слотов: {alive}/{COOKIE_SLOTS}\n\n"
                        "Пришли новый cookies.txt сюда файлом.\n"
                        "/cookies — статус."
                    )
                else:
                    await status.edit_text(
                        "Сейчас Instagram не пускает (сессии бота).\n"
                        "Админ уже уведомлён — попробуй позже."
                    )
            else:
                await status.edit_text(f"Не удалось скачать.\n\n{details}")
            cleanup_workdir(workdir)
            return

        record_download(
            user_id,
            url,
            success=True,
            files=result.files,
            username=username,
        )
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
        record_download(user_id, url, success=False, username=username)
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
            record_download(user.id, url, success=False, username=user.username)
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
        await run_download(
            message,
            status,
            user_id=user.id,
            url=url,
            username=user.username,
        )
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
                username=user.username,
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

    ensure_cookie_pool()
    alive_slots = list_alive_cookie_slots()
    if not alive_slots:
        print(
            "ВНИМАНИЕ: в пуле Instagram cookies нет живых слотов.\n"
            f"Папка пула: {COOKIES_DIR}\n"
            "Пришли cookies.txt боту (админ) или положи файлы "
            f"{COOKIES_DIR}/1.txt … {COOKIE_SLOTS}.txt"
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
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("cookies", cookies_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_cookies_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("Instagram Telegram bot started.")
    print(f"Instagram cookies browser: {COOKIES_BROWSER or 'нет'}")
    print(f"Instagram cookies pool: {COOKIES_DIR} ({len(alive_slots)}/{COOKIE_SLOTS} alive)")
    print(
        f"Instagram rate limit: ≤{IG_MAX_PER_HOUR}/hour, "
        f"min interval {IG_MIN_INTERVAL_SECONDS}s"
    )
    print(f"YouTube cookies browser: {YOUTUBE_COOKIES_BROWSER or 'нет'}")
    print(f"YouTube cookies file: {YOUTUBE_COOKIES_FILE or 'нет'}")
    print(f"Auth question: {AUTH_QUESTION}")
    print(f"Authorized users: {len(AUTHORIZED_USER_IDS)}")
    if AUTHORIZED_USER_IDS:
        print(f"Stats admin (first authorized): {AUTHORIZED_USER_IDS[0]}")
    print(f"Category B jokes: {len(CATEGORY_B_JOKES)}")
    print(
        f"Send preview: {SEND_PREVIEW}, send document: {SEND_DOCUMENT}, "
        f"photo preview: {PHOTO_PREVIEW}"
    )

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
