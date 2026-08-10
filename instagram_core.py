#!/Library/Frameworks/Python.framework/Versions/3.14/bin/python3
"""Shared Instagram download helpers for GUI and Telegram bot."""

from __future__ import annotations

import html
import re
import ssl
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
INSTAGRAM_PAGE_USER_AGENT = "Mozilla/5.0"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

LogFn = Callable[[str], None]


def _noop_log(_: str) -> None:
    return None


class ListLogger:
    def __init__(self, log: LogFn):
        self.log = log

    def debug(self, message):
        if message:
            self.log(str(message))

    def info(self, message):
        if message:
            self.log(str(message))

    def warning(self, message):
        if message:
            self.log("WARNING: " + str(message))

    def error(self, message):
        if message:
            text = str(message)
            if "No video formats found" in text:
                self.log("Фото-элемент карусели не является видео, обработаю его отдельно.")
                return
            self.log("ERROR: " + text)


@dataclass
class DownloadResult:
    files: list[Path] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    error: str | None = None
    partial: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.files) and not self.error


def get_ffmpeg_path() -> str | None:
    if not imageio_ffmpeg:
        return None
    return imageio_ffmpeg.get_ffmpeg_exe()


def get_shortcode_from_url(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]

    for index, part in enumerate(parts):
        if part in {"p", "reel", "tv"} and index + 1 < len(parts):
            return parts[index + 1]

    return "instagram"


def _clean_extracted_url(url: str) -> str:
    return url.rstrip(").,]}>\"'")


def extract_instagram_url(text: str) -> str | None:
    match = re.search(
        r"https?://(?:www\.)?instagram\.com/[^\s<>\"']+",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return _clean_extracted_url(match.group(0))


def extract_tiktok_url(text: str) -> str | None:
    match = re.search(
        r"https?://(?:(?:www|m|vm|vt)\.)?tiktok\.com/[^\s<>\"']+",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return _clean_extracted_url(match.group(0))


def extract_media_url(text: str) -> str | None:
    return extract_instagram_url(text) or extract_tiktok_url(text)


def is_instagram_url(url: str) -> bool:
    return "instagram.com" in url.lower()


def is_tiktok_url(url: str) -> bool:
    return "tiktok.com" in url.lower()


def get_media_files(output_path: Path) -> set[Path]:
    return {
        file_path
        for file_path in output_path.glob("*")
        if file_path.is_file() and file_path.suffix.lower() in MEDIA_EXTENSIONS
    }


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def filter_video_thumbnails(files: list[Path]) -> list[Path]:
    """Drop still images that are covers/thumbnails for already downloaded videos."""
    videos = [path for path in files if is_video(path)]
    images = [path for path in files if is_image(path)]
    if not videos or not images:
        return files

    video_stems = {path.stem.lower() for path in videos}
    # yt-dlp names: <date>_<id>_<title>.mp4 — drop matching <date>_<id>_*.jpg
    video_id_prefixes: set[str] = set()
    for path in videos:
        parts = path.stem.split("_")
        if len(parts) >= 2:
            video_id_prefixes.add("_".join(parts[:2]).lower())

    kept: list[Path] = []
    for path in files:
        if not is_image(path):
            kept.append(path)
            continue

        stem = path.stem.lower()
        if stem in video_stems:
            continue
        if any(stem == prefix or stem.startswith(prefix + "_") for prefix in video_id_prefixes):
            continue
        kept.append(path)

    return kept


def instagram_photo_fallback_mode(
    result_code: int,
    files_after_ydl: list[Path],
) -> str | None:
    """
    Decide whether Instagram photo fallback is needed.

    Returns:
      - None: do not fetch photos (e.g. pure video already downloaded)
      - "gallery_only": mixed carousel — fetch real photos, skip page scrape
      - "full": photo post / total miss — gallery-dl then page scrape
    """
    has_video = any(is_video(path) for path in files_after_ydl)
    has_image = any(is_image(path) for path in files_after_ydl)

    if result_code == 0 and (has_video or has_image):
        return None
    if not files_after_ydl:
        return "full"
    if has_video and not has_image:
        return "gallery_only"
    if not has_video:
        return "full"
    return None


def build_ydl_options(
    output_path: Path,
    *,
    cookies_browser: str | None = None,
    cookies_file: str | Path | None = None,
    log: LogFn = _noop_log,
    progress_hook: Callable[[dict], None] | None = None,
) -> dict:
    ffmpeg_path = get_ffmpeg_path()

    options = {
        "outtmpl": str(output_path / "%(upload_date|unknown)s_%(id)s_%(title).80s.%(ext)s"),
        "format": "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/b[vcodec^=avc1]/bv*+ba/b",
        "merge_output_format": "mp4",
        "noplaylist": False,
        "ignoreerrors": True,
        "retries": 5,
        "fragment_retries": 5,
        "continuedl": True,
        "writeinfojson": False,
        "writethumbnail": False,
        "quiet": False,
        "no_warnings": False,
        "color": "no_color",
        "logger": ListLogger(log),
    }

    if progress_hook:
        options["progress_hooks"] = [progress_hook]

    if ffmpeg_path:
        options["ffmpeg_location"] = ffmpeg_path

    if cookies_file:
        options["cookiefile"] = str(cookies_file)
    elif cookies_browser and cookies_browser != "Без cookies":
        options["cookiesfrombrowser"] = (cookies_browser,)

    return options


def find_instagram_photo_urls(page_html: str) -> list[str]:
    raw_urls = re.findall(
        r"https://scontent[^\"'<> ]+?\.jpg\?[^\"'<> ]+",
        page_html,
    )

    photo_urls = []
    seen_paths = set()

    for raw_url in raw_urls:
        photo_url = html.unescape(raw_url)

        if "cdninstagram.com" not in photo_url:
            continue

        path_key = urlparse(photo_url).path
        if path_key in seen_paths:
            continue

        seen_paths.add(path_key)
        photo_urls.append(photo_url)

    return photo_urls


def download_file(url: str, destination: Path, referer: str) -> None:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": referer,
        },
    )

    with urlopen(request, timeout=30, context=ssl._create_unverified_context()) as response:
        destination.write_bytes(response.read())


def download_instagram_photos(url: str, output_path: Path, log: LogFn = _noop_log) -> int:
    request = Request(
        url,
        headers={
            "User-Agent": INSTAGRAM_PAGE_USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.instagram.com/",
        },
    )

    with urlopen(request, timeout=30, context=ssl._create_unverified_context()) as response:
        page_html = response.read().decode("utf-8", "replace")

    photo_urls = find_instagram_photo_urls(page_html)
    if not photo_urls:
        return 0

    shortcode = get_shortcode_from_url(url)
    output_path.mkdir(parents=True, exist_ok=True)
    downloaded_count = 0

    for index, photo_url in enumerate(photo_urls, start=1):
        destination = output_path / f"{shortcode}_{index:02d}.jpg"

        if destination.exists():
            log(f"Фото уже есть: {destination}")
            continue

        download_file(photo_url, destination, url)
        downloaded_count += 1
        log(f"Фото скачано: {destination}")

    return downloaded_count


def download_photos_with_gallery_dl(
    url: str,
    output_path: Path,
    *,
    cookies_browser: str | None = None,
    cookies_file: str | Path | None = None,
    log: LogFn = _noop_log,
) -> int:
    if not cookies_file and (not cookies_browser or cookies_browser == "Без cookies"):
        log("Для скачивания всех фото из карусели нужны cookies.")
        return 1

    command = [
        sys.executable,
        "-m",
        "gallery_dl",
        "--no-colors",
        "--no-check-certificate",
        "--filter",
        (
            "extension in ('jpg', 'jpeg', 'png', 'webp') and "
            "typename == 'GraphImage'"
        ),
        "-D",
        str(output_path),
        "-f",
        "{date:%Y%m%d}_{media_id}.{extension}",
        url,
    ]

    if cookies_file:
        command[4:4] = ["--cookies", str(cookies_file)]
    else:
        command[4:4] = ["--cookies-from-browser", cookies_browser]

    log("Скачивание фото из Instagram-карусели...")

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    if process.stdout:
        for line in process.stdout:
            line = line.strip()
            if line:
                log(line)

    return process.wait()


def is_quicktime_friendly_mp4(file_path: Path) -> bool:
    ffmpeg_path = get_ffmpeg_path()
    if not ffmpeg_path:
        return True

    result = subprocess.run(
        [ffmpeg_path, "-hide_banner", "-i", str(file_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output = result.stdout.lower()

    has_h264_video = "video: h264" in output
    has_audio = "audio:" in output
    has_aac_audio = "audio: aac" in output

    return has_h264_video and (not has_audio or has_aac_audio)


def make_mp4_quicktime_friendly(file_path: Path, log: LogFn = _noop_log) -> None:
    ffmpeg_path = get_ffmpeg_path()
    if not ffmpeg_path:
        log("ffmpeg не найден, не могу перекодировать MP4 для QuickTime.")
        return

    if is_quicktime_friendly_mp4(file_path):
        return

    temp_path = file_path.with_name(file_path.stem + ".quicktime.mp4")
    log(f"Перекодирую для QuickTime: {file_path.name}")

    command = [
        ffmpeg_path,
        "-y",
        "-i",
        str(file_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        str(temp_path),
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    if result.returncode:
        log(f"Не удалось перекодировать {file_path.name}: {result.stdout[-1000:]}")
        if temp_path.exists():
            temp_path.unlink()
        return

    temp_path.replace(file_path)
    log(f"MP4 готов для QuickTime: {file_path}")


def make_new_mp4_files_quicktime_friendly(
    output_path: Path,
    existing_files: set[Path],
    log: LogFn = _noop_log,
) -> None:
    for file_path in sorted(output_path.glob("*.mp4")):
        if file_path not in existing_files:
            make_mp4_quicktime_friendly(file_path, log)


def format_download_error(error_text: str) -> list[str]:
    tips = ["Что проверить:"]

    if "Operation not permitted" in error_text or "Errno 1" in error_text:
        tips.extend(
            [
                "1. macOS блокирует чтение cookies браузера.",
                "2. Системные настройки → Конфиденциальность и безопасность → Полный доступ к диску",
                "3. Добавь туда Терминал/Python и перезапусти бота.",
            ]
        )
    elif (
        "could not be decrypted" in error_text
        or "no key found" in error_text
        or "find-generic-password failed" in error_text
    ):
        tips.extend(
            [
                "1. Не удалось расшифровать cookies Chrome (нет доступа к Keychain).",
                "2. Запусти бота из Терминала.app и разреши доступ к связке ключей.",
                "3. Или положи cookies.txt и укажи INSTAGRAM_COOKIES_FILE.",
            ]
        )
    elif "empty media response" in error_text or "not granting access" in error_text:
        tips.extend(
            [
                "1. Instagram требует авторизацию для этой ссылки.",
                "2. Задай INSTAGRAM_COOKIES_FROM_BROWSER или INSTAGRAM_COOKIES_FILE.",
                "3. Убедись, что пост открывается в браузере под этим аккаунтом.",
            ]
        )
    else:
        tips.extend(
            [
                "1. Проверь ссылку и доступность поста.",
                "2. Для Instagram обычно нужны cookies.",
                "3. Для TikTok обычно cookies не нужны — проверь, что видео публичное.",
            ]
        )

    return tips


def download_instagram_media(
    url: str,
    output_path: Path,
    *,
    cookies_browser: str | None = None,
    cookies_file: str | Path | None = None,
    log: LogFn | None = None,
) -> DownloadResult:
    messages: list[str] = []

    def emit(message: str) -> None:
        messages.append(message)
        if log:
            log(message)

    output_path = Path(output_path).expanduser().resolve()
    output_path.mkdir(parents=True, exist_ok=True)

    # Instagram cookies are only useful for Instagram; for TikTok they often
    # just trigger browser/keychain errors and are not needed.
    use_cookies = is_instagram_url(url)
    effective_cookies_browser = cookies_browser if use_cookies else None
    effective_cookies_file = cookies_file if use_cookies else None

    emit(f"Ссылка: {url}")
    emit(f"Папка: {output_path}")
    emit(f"Cookies browser: {effective_cookies_browser or 'нет'}")
    emit(f"Cookies file: {effective_cookies_file or 'нет'}")

    existing_mp4_files = set(output_path.glob("*.mp4"))
    existing_media_files = get_media_files(output_path)

    try:
        options = build_ydl_options(
            output_path,
            cookies_browser=effective_cookies_browser,
            cookies_file=effective_cookies_file,
            log=emit,
        )

        with YoutubeDL(options) as ydl:
            result_code = ydl.download([url])

        make_new_mp4_files_quicktime_friendly(output_path, existing_mp4_files, emit)

        files_after_ydl = sorted(get_media_files(output_path) - existing_media_files)

        if is_instagram_url(url):
            photo_mode = instagram_photo_fallback_mode(result_code, files_after_ydl)
            if photo_mode:
                gallery_result_code = 1
                if effective_cookies_file or (
                    effective_cookies_browser and effective_cookies_browser != "Без cookies"
                ):
                    gallery_result_code = download_photos_with_gallery_dl(
                        url,
                        output_path,
                        cookies_browser=effective_cookies_browser,
                        cookies_file=effective_cookies_file,
                        log=emit,
                    )
                    if gallery_result_code == 0:
                        emit("Фото из карусели обработаны.")
                    else:
                        emit("gallery-dl не смог скачать фото.")

                # Page scrape pulls video posters/covers — only for pure photo posts.
                if photo_mode == "full" and gallery_result_code != 0:
                    try:
                        photo_count = download_instagram_photos(url, output_path, emit)
                        if photo_count:
                            emit(f"Фото сохранено: {photo_count}")
                    except Exception as photo_error:
                        emit(f"Не удалось скачать фото из страницы Instagram: {photo_error}")
            else:
                emit("Фото-fallback пропущен: видео уже скачано без пропусков.")

        new_files = filter_video_thumbnails(
            sorted(get_media_files(output_path) - existing_media_files)
        )
        partial = bool(result_code) and bool(new_files)

        if not new_files:
            if is_instagram_url(url) and not effective_cookies_file and (
                not effective_cookies_browser or effective_cookies_browser == "Без cookies"
            ):
                emit("Instagram не отдал медиа без авторизации. Нужны cookies.")
            return DownloadResult(
                files=[],
                messages=messages,
                error="Не удалось скачать медиа.",
                partial=False,
            )

        if partial:
            emit("Завершено частично: часть элементов не удалось скачать.")
        else:
            emit("Готово.")

        return DownloadResult(files=new_files, messages=messages, partial=partial)

    except DownloadError as e:
        error_text = str(e)
        emit(f"Ошибка yt-dlp: {error_text}")
        for tip in format_download_error(error_text):
            emit(tip)
        return DownloadResult(files=[], messages=messages, error=error_text)

    except Exception as e:
        emit(f"Неожиданная ошибка: {e}")
        return DownloadResult(files=[], messages=messages, error=str(e))
