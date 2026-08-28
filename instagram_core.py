#!/Library/Frameworks/Python.framework/Versions/3.14/bin/python3
"""Shared Instagram download helpers for GUI and Telegram bot."""

from __future__ import annotations

import html
import json
import re
import ssl
import subprocess
import sys
import tempfile
import time
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
AUDIO_EXTENSIONS = {".m4a", ".mp3", ".aac", ".opus", ".ogg", ".wav"}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | AUDIO_EXTENSIONS

GALLERY_DL_INSTAGRAM_FILTER = (
    "((extension in ('jpg', 'jpeg', 'png', 'webp', 'heic', 'heif') and not video_url) "
    "or audio_url)"
)

YOUTUBE_QUALITY_HEIGHTS = (1080, 720, 480, 360)
YOUTUBE_MAX_DURATION_SECONDS = 20 * 60
TELEGRAM_MAX_UPLOAD_BYTES = 49 * 1024 * 1024
COMPRESS_TARGET_BYTES = 45 * 1024 * 1024
COMPRESS_TIMEOUT_SECONDS = 10 * 60
COMPRESS_TIMEOUT_CAP_SECONDS = 25 * 60
# Throughput measured on the deploy VPS (one shared Broadwell core, ~50% steal):
# decoding 1080p60 runs at 0.8x realtime, encoding 480p30 at 1.6x.
COMPRESS_DECODE_PIXELS_PER_SECOND = 124_000_000 / 0.8
COMPRESS_ENCODE_PIXELS_PER_SECOND = 12_400_000 / 1.6
# Bitrate below which a resolution stops looking good. Re-encoding 1080p at
# 1.5 Mbps wastes a lot of CPU on pixels the bitrate cannot carry anyway, and on
# a one-core VPS that difference is tens of minutes.
COMPRESS_HEIGHT_BITRATE = ((1080, 3_500_000), (720, 2_000_000), (480, 1_000_000), (360, 0))
# 60 fps doubles the encoding work and buys nothing at a compressed bitrate.
COMPRESS_MAX_FPS = 30
# android_vr has the full quality ladder but YouTube sometimes 403s its
# media URLs. web_embedded is more stable; tv is last-resort (often 360p).
YOUTUBE_PLAYER_CLIENT_ATTEMPTS = (
    ("android_vr",),
    ("web_embedded",),
    ("tv", "tv_embedded"),
)

LogFn = Callable[[str], None]
ProgressFn = Callable[[float], None]


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


def extract_youtube_url(text: str) -> str | None:
    match = re.search(
        r"https?://(?:(?:www|m|music)\.)?(?:youtube\.com|youtu\.be)/[^\s<>\"']+",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return _clean_extracted_url(match.group(0))


def extract_media_url(text: str) -> str | None:
    return (
        extract_instagram_url(text)
        or extract_tiktok_url(text)
        or extract_youtube_url(text)
    )


def is_instagram_url(url: str) -> bool:
    return "instagram.com" in urlparse(url).netloc.lower()


def is_tiktok_url(url: str) -> bool:
    return "tiktok.com" in urlparse(url).netloc.lower()


def is_youtube_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host == "youtu.be" or host.endswith(".youtu.be") or "youtube.com" in host


def get_media_files(output_path: Path) -> set[Path]:
    return {
        file_path
        for file_path in output_path.rglob("*")
        if file_path.is_file() and file_path.suffix.lower() in MEDIA_EXTENSIONS
    }


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS


def is_audio_only(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in AUDIO_EXTENSIONS:
        return True
    if suffix not in VIDEO_EXTENSIONS:
        return False
    metadata = probe_video_metadata(path)
    return metadata.has_audio and not metadata.vcodec


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS and not is_audio_only(path)


def sort_files_for_delivery(files: list[Path]) -> list[Path]:
    """Photos first, then attached music, then videos."""

    def sort_key(path: Path) -> tuple[int, str]:
        if is_image(path):
            bucket = 0
        elif is_audio_only(path):
            bucket = 1
        else:
            bucket = 2
        return (bucket, path.name)

    return sorted(files, key=sort_key)


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


def format_selector_for_height(
    max_height: int | None,
    *,
    prefer_avc: bool = False,
    prefer_progressive: bool = False,
) -> str:
    if max_height is None:
        if prefer_progressive:
            # Instagram DASH "best" is often mute VP9. Progressive slots 2/1/0 are
            # usually H.264 with audio when cookies include a live sessionid.
            # Never prefer `best[ext=mp4]` alone: it ranks mute DASH above them.
            return (
                "2/1/0/"
                "best[format_note!*=DASH]/"
                "bv*[vcodec^=avc1]+ba/"
                "bestvideo+bestaudio/"
                "best"
            )
        if prefer_avc:
            return "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/bestvideo+bestaudio/best"
        return "bestvideo+bestaudio/best"
    height = f"height<={max_height}"
    if prefer_avc:
        return (
            f"bv*[{height}][vcodec^=avc1]+ba[acodec^=mp4a]/"
            f"b[{height}][vcodec^=avc1]/"
            f"bv*[{height}]+ba/"
            f"b[{height}]/"
            "bv*+ba/b"
        )
    return (
        f"bv*[{height}]+ba/"
        f"b[{height}]/"
        "bv*+ba/b"
    )


def youtube_extractor_args(clients: tuple[str, ...] | list[str]) -> dict:
    return {"youtube": {"player_client": list(clients)}}


def youtube_qualities_from_formats(formats: list[dict]) -> list[int]:
    max_height = 0
    for item in formats:
        height = item.get("height")
        if isinstance(height, int) and height > max_height:
            max_height = height
    if max_height <= 0:
        return list(YOUTUBE_QUALITY_HEIGHTS)
    qualities = [height for height in YOUTUBE_QUALITY_HEIGHTS if height <= max_height]
    if qualities:
        return qualities
    return [YOUTUBE_QUALITY_HEIGHTS[-1]]


def format_mb(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


def build_ydl_options(
    output_path: Path,
    *,
    cookies_browser: str | None = None,
    cookies_file: str | Path | None = None,
    log: LogFn = _noop_log,
    progress_hook: Callable[[dict], None] | None = None,
    max_height: int | None = None,
    noplaylist: bool = False,
    prefer_avc: bool = False,
    prefer_progressive: bool = False,
    extractor_args: dict | None = None,
) -> dict:
    ffmpeg_path = get_ffmpeg_path()

    options = {
        "outtmpl": str(output_path / "%(upload_date|unknown)s_%(id)s_%(title).80s.%(ext)s"),
        # Prefer the best source streams. ffmpeg only remuxes them into MP4;
        # it must not rescale or re-encode the video unless the user asked.
        "format": format_selector_for_height(
            max_height,
            prefer_avc=prefer_avc,
            prefer_progressive=prefer_progressive,
        ),
        "merge_output_format": "mp4",
        "noplaylist": noplaylist,
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

    if extractor_args:
        options["extractor_args"] = extractor_args

    apply_cookie_options(
        options,
        cookies_browser=cookies_browser,
        cookies_file=cookies_file,
    )

    return options


def cookie_header_from_file(cookies_file: str | Path | None) -> str | None:
    if not cookies_file:
        return None
    path = Path(cookies_file).expanduser()
    if not path.exists():
        return None
    pairs: list[str] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 7 and parts[5] and parts[6]:
                pairs.append(f"{parts[5]}={parts[6]}")
    except OSError:
        return None
    return "; ".join(pairs) if pairs else None


def _instagram_json_unescape(text: str) -> str:
    return html.unescape(text.replace("\\/", "/").replace("\\u0026", "&"))


def instagram_image_url_score(url: str) -> int:
    """Higher is better. Penalize obvious profile-grid crops and tiny thumbs."""
    lowered = url.lower()
    best = 0
    for match in re.finditer(r"s(\d+)x(\d+)", lowered):
        best = max(best, int(match.group(1)) * int(match.group(2)))
    if best:
        return best
    if any(token in lowered for token in ("s150x150", "s320x320", "s640x640")):
        return 640 * 640
    if "stp=" in lowered:
        return 720 * 720
    return 50_000_000


def _parse_instagram_candidate_arrays(page_html: str) -> list[list[dict]]:
    arrays: list[list[dict]] = []
    marker = '"candidates"'
    start = 0
    while True:
        index = page_html.find(marker, start)
        if index == -1:
            break
        start = index + len(marker)
        bracket = page_html.find("[", index)
        if bracket == -1:
            continue
        depth = 0
        for offset, char in enumerate(page_html[bracket:], start=bracket):
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    blob = page_html[bracket : offset + 1]
                    try:
                        parsed = json.loads(_instagram_json_unescape(blob))
                    except json.JSONDecodeError:
                        parsed = None
                    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                        if all("url" in item for item in parsed[:3]):
                            arrays.append(parsed)
                    break
    return arrays


def find_instagram_photo_urls(page_html: str) -> list[str]:
    best_by_path: dict[str, tuple[int, str]] = {}

    def consider(raw_url: str) -> None:
        photo_url = _instagram_json_unescape(raw_url)
        if "cdninstagram.com" not in photo_url:
            return
        path_key = urlparse(photo_url).path
        score = instagram_image_url_score(photo_url)
        current = best_by_path.get(path_key)
        if current is None or score > current[0]:
            best_by_path[path_key] = (score, photo_url)

    for candidates in _parse_instagram_candidate_arrays(page_html):
        best = max(
            candidates,
            key=lambda item: int(item.get("width", 0) or 0) * int(item.get("height", 0) or 0),
        )
        url = best.get("url")
        if isinstance(url, str) and url:
            consider(url)

    for raw_url in re.findall(
        r"https://scontent[^\"'<> ]+?\.(?:jpg|jpeg|png|webp|heic|heif)\?[^\"'<> ]+",
        page_html,
        flags=re.IGNORECASE,
    ):
        consider(raw_url)

    if not best_by_path:
        return []

    return [
        url
        for _, url in sorted(
            best_by_path.values(),
            key=lambda item: item[0],
            reverse=True,
        )
    ]


def extension_for_instagram_photo_url(url: str) -> str:
    path = urlparse(url).path.lower()
    for ext in (".heic", ".heif", ".jpeg", ".jpg", ".png", ".webp"):
        if path.endswith(ext):
            return ext.lstrip(".")
    if "webp" in url.lower():
        return "webp"
    return "jpg"


def download_file(
    url: str,
    destination: Path,
    referer: str,
    *,
    cookie_header: str | None = None,
) -> None:
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": referer,
    }
    if cookie_header:
        headers["Cookie"] = cookie_header

    request = Request(url, headers=headers)

    with urlopen(request, timeout=30, context=ssl._create_unverified_context()) as response:
        destination.write_bytes(response.read())


def download_instagram_photos(
    url: str,
    output_path: Path,
    log: LogFn = _noop_log,
    *,
    cookies_file: str | Path | None = None,
) -> int:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.instagram.com/",
    }
    cookie_header = cookie_header_from_file(cookies_file)
    if cookie_header:
        headers["Cookie"] = cookie_header

    request = Request(url, headers=headers)

    with urlopen(request, timeout=30, context=ssl._create_unverified_context()) as response:
        page_html = response.read().decode("utf-8", "replace")

    photo_urls = find_instagram_photo_urls(page_html)
    if not photo_urls:
        return 0

    shortcode = get_shortcode_from_url(url)
    output_path.mkdir(parents=True, exist_ok=True)
    downloaded_count = 0

    for index, photo_url in enumerate(photo_urls, start=1):
        extension = extension_for_instagram_photo_url(photo_url)
        destination = output_path / f"{shortcode}_{index:02d}.{extension}"

        if destination.exists():
            log(f"Фото уже есть: {destination}")
            continue

        download_file(photo_url, destination, url, cookie_header=cookie_header)
        downloaded_count += 1
        log(f"Фото скачано: {destination}")

    return downloaded_count


def instagram_shortcode_to_media_id(shortcode: str) -> str | None:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    media_id = 0
    try:
        for char in shortcode:
            media_id = media_id * 64 + alphabet.index(char)
    except ValueError:
        return None
    return str(media_id) if media_id else None


def _instagram_audio_candidate_urls(asset: dict) -> list[str]:
    urls: list[str] = []
    for key in (
        "progressive_download_url",
        "fast_start_progressive_download_url",
        "web_30s_preview_download_url",
        "reactive_audio_download_url",
    ):
        value = asset.get(key)
        if isinstance(value, str) and value.startswith("http"):
            urls.append(value)

    manifest = asset.get("dash_manifest")
    if isinstance(manifest, str) and manifest.strip():
        for match in re.finditer(r"<BaseURL>([^<]+)</BaseURL>", manifest):
            url = html.unescape(match.group(1).strip())
            if url.startswith("http"):
                urls.append(url)
    return urls


def _instagram_music_assets_from_item(item: dict) -> list[dict]:
    assets: list[dict] = []

    music_metadata = item.get("music_metadata") or {}
    music_info = music_metadata.get("music_info") or {}
    if isinstance(music_info.get("music_asset_info"), dict):
        assets.append(music_info["music_asset_info"])
    if isinstance(music_metadata.get("original_sound_info"), dict):
        assets.append(music_metadata["original_sound_info"])

    clips = item.get("clips_metadata") or {}
    if isinstance(clips.get("music_info"), dict) and isinstance(
        clips["music_info"].get("music_asset_info"), dict
    ):
        assets.append(clips["music_info"]["music_asset_info"])
    if isinstance(clips.get("original_sound_info"), dict):
        assets.append(clips["original_sound_info"])

    return assets


def fetch_instagram_media_info(
    url: str,
    *,
    cookies_file: str | Path | None = None,
) -> dict | None:
    shortcode = get_shortcode_from_url(url)
    media_id = instagram_shortcode_to_media_id(shortcode)
    if not media_id:
        return None

    cookie_header = cookie_header_from_file(cookies_file)
    if not cookie_header:
        return None

    csrf = ""
    for part in cookie_header.split("; "):
        if part.startswith("csrftoken="):
            csrf = part.split("=", 1)[1]
            break

    request = Request(
        f"https://www.instagram.com/api/v1/media/{media_id}/info/",
        headers={
            "User-Agent": USER_AGENT,
            "Cookie": cookie_header,
            "Accept": "*/*",
            "X-CSRFToken": csrf,
            "X-IG-App-ID": "936619743392459",
            "X-ASBD-ID": "129477",
            "X-IG-WWW-Claim": "0",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": url if url.startswith("http") else f"https://www.instagram.com/p/{shortcode}/",
        },
    )
    try:
        with urlopen(request, timeout=30, context=ssl._create_unverified_context()) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except Exception:
        return None

    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        return None
    item = items[0]
    return item if isinstance(item, dict) else None


def download_instagram_attached_audio(
    url: str,
    output_path: Path,
    *,
    cookies_file: str | Path | None = None,
    log: LogFn = _noop_log,
) -> list[Path]:
    """Download music attached to a photo post / carousel when Instagram exposes a URL."""
    item = fetch_instagram_media_info(url, cookies_file=cookies_file)
    if not item:
        return []

    assets = _instagram_music_assets_from_item(item)
    if not assets:
        return []

    cookie_header = cookie_header_from_file(cookies_file)
    shortcode = get_shortcode_from_url(url)
    output_path.mkdir(parents=True, exist_ok=True)
    downloaded: list[Path] = []

    for index, asset in enumerate(assets, start=1):
        urls = _instagram_audio_candidate_urls(asset)
        title = (
            asset.get("title")
            or asset.get("original_audio_title")
            or asset.get("display_artist")
            or ""
        )
        if not urls:
            # Instagram often redacts licensed-music URLs for brand-new accounts
            # even though music_metadata is present on the post.
            log(
                "У поста есть прикреплённая музыка"
                + (f" ({title})" if title else "")
                + ", но Instagram не отдал ссылку на аудиофайл. "
                "На новых аккаунтах так бывает часто — нужен более «прогретый» аккаунт."
            )
            continue

        audio_id = asset.get("id") or asset.get("audio_asset_id") or index
        destination = output_path / f"{shortcode}_audio_{audio_id}.m4a"
        if destination.exists():
            downloaded.append(destination)
            continue

        last_error = None
        for audio_url in urls:
            temp = destination.with_suffix(".download")
            try:
                download_file(audio_url, temp, url, cookie_header=cookie_header)
                # Instagram often serves music as audio-only MP4; remux to .m4a.
                if temp.suffix.lower() != ".m4a":
                    prepared = prepare_telegram_audio(temp, log)
                    if prepared != temp and prepared.exists():
                        prepared.replace(destination)
                        temp.unlink(missing_ok=True)
                    elif temp.exists():
                        temp.replace(destination)
                else:
                    temp.replace(destination)

                if destination.exists() and destination.stat().st_size > 0:
                    downloaded.append(destination)
                    log(
                        f"Аудио скачано: {destination.name}"
                        + (f" — {title}" if title else "")
                    )
                    break
            except Exception as exc:
                last_error = exc
                temp.unlink(missing_ok=True)
                destination.unlink(missing_ok=True)
        else:
            log(f"Не удалось скачать аудио поста: {last_error or 'нет рабочего URL'}")

    return downloaded


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
        "-o",
        "extractor.instagram.audio=true",
        "--filter",
        GALLERY_DL_INSTAGRAM_FILTER,
        "-D",
        str(output_path),
        "-f",
        "{date:%Y%m%d}_{media_id}_{num}.{extension}",
        url,
    ]

    if cookies_file:
        command[4:4] = ["--cookies", str(cookies_file)]
    else:
        command[4:4] = ["--cookies-from-browser", cookies_browser]

    log("Скачивание фото и аудио из Instagram-карусели...")

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


@dataclass
class VideoMetadata:
    width: int | None = None
    height: int | None = None
    duration: int | None = None
    fps: float | None = None
    vcodec: str | None = None
    acodec: str | None = None
    audio_profile: str | None = None
    has_audio: bool = False


_VIDEO_STREAM_RE = re.compile(r"Video:.*?,\s(\d{2,5})x(\d{2,5})")
_VIDEO_CODEC_RE = re.compile(r"Video:\s*([A-Za-z0-9]+)")
_AUDIO_CODEC_RE = re.compile(r"Audio:\s*([A-Za-z0-9]+)")
_AUDIO_PROFILE_RE = re.compile(r"Audio:\s*[^(]+\(([^)]+)\)")
_FPS_RE = re.compile(r",\s(\d+(?:\.\d+)?)\sfps")
_DISPLAY_ASPECT_RE = re.compile(r"DAR (\d+):(\d+)")
_DURATION_RE = re.compile(r"Duration: (\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_DISPLAY_MATRIX_ROTATION_RE = re.compile(r"rotation of (-?\d+(?:\.\d+)?) degrees")
_ROTATE_TAG_RE = re.compile(r"rotate\s*:\s*(-?\d+)")


def probe_video_metadata(file_path: Path) -> VideoMetadata:
    """Read display size and duration so players keep the original aspect ratio."""
    ffmpeg_path = get_ffmpeg_path()
    if not ffmpeg_path:
        return VideoMetadata()

    result = subprocess.run(
        [ffmpeg_path, "-hide_banner", "-i", str(file_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output = result.stdout
    vcodec_match = _VIDEO_CODEC_RE.search(output)
    acodec_match = _AUDIO_CODEC_RE.search(output)
    profile_match = _AUDIO_PROFILE_RE.search(output)
    vcodec = vcodec_match.group(1).lower() if vcodec_match else None
    acodec = acodec_match.group(1).lower() if acodec_match else None
    audio_profile = profile_match.group(1) if profile_match else None
    has_audio = acodec is not None

    duration = None
    duration_match = _DURATION_RE.search(output)
    if duration_match:
        hours = int(duration_match.group(1))
        minutes = int(duration_match.group(2))
        seconds = float(duration_match.group(3))
        duration = round(hours * 3600 + minutes * 60 + seconds)

    size_match = _VIDEO_STREAM_RE.search(output)
    if not size_match:
        return VideoMetadata(
            duration=duration,
            vcodec=vcodec,
            acodec=acodec,
            audio_profile=audio_profile,
            has_audio=has_audio,
        )

    width = int(size_match.group(1))
    height = int(size_match.group(2))

    # Anamorphic sources (non-square pixels) are stored squeezed; without the
    # display size Telegram renders them with the wrong aspect ratio.
    aspect_match = _DISPLAY_ASPECT_RE.search(output)
    if aspect_match:
        aspect_width = int(aspect_match.group(1))
        aspect_height = int(aspect_match.group(2))
        if aspect_width > 0 and aspect_height > 0:
            width = round(height * aspect_width / aspect_height)

    rotation_match = _DISPLAY_MATRIX_ROTATION_RE.search(output) or _ROTATE_TAG_RE.search(output)
    if rotation_match and round(abs(float(rotation_match.group(1)))) % 180 == 90:
        width, height = height, width

    fps = None
    fps_match = _FPS_RE.search(output)
    if fps_match:
        fps = float(fps_match.group(1))

    return VideoMetadata(
        width=width,
        height=height,
        duration=duration,
        fps=fps,
        vcodec=vcodec,
        acodec=acodec,
        audio_profile=audio_profile,
        has_audio=has_audio,
    )


def _is_h264(metadata: VideoMetadata) -> bool:
    codec = metadata.vcodec or ""
    return codec in {"h264", "avc", "avc1"} or codec.startswith("avc")


def _is_he_aac(metadata: VideoMetadata) -> bool:
    profile = (metadata.audio_profile or "").lower()
    return "he-aac" in profile or "he aac" in profile or "aac_he" in profile


def is_telegram_compatible_video(metadata: VideoMetadata) -> bool:
    """Telegram's in-chat player wants H.264 + AAC LC. VP9/HE-AAC often plays mute."""
    if not metadata.has_audio or not _is_h264(metadata):
        return False
    acodec = metadata.acodec or ""
    if acodec not in {"aac", "mp4a"}:
        return False
    return not _is_he_aac(metadata)


def ensure_telegram_compatible_video(file_path: Path, log: LogFn = _noop_log) -> None:
    """Fix HE-AAC / VP9 for Telegram without burning the VPS on mute files."""
    ffmpeg_path = get_ffmpeg_path()
    if not ffmpeg_path:
        return

    metadata = probe_video_metadata(file_path)
    if is_telegram_compatible_video(metadata):
        return

    if not metadata.has_audio:
        # Full H.264 re-encode cannot invent a soundtrack and used to stall the
        # bot for minutes on mute Instagram DASH. Skip and keep the download fast.
        log(
            f"В {file_path.name} нет аудиодорожки — пропускаю перекодирование. "
            "Обычно помогает свежий instagram_cookies.txt с sessionid."
        )
        return

    temp_path = file_path.with_name(file_path.stem + ".tgplay.mp4")
    if _is_h264(metadata):
        log(f"Перепаковываю звук в AAC LC для Telegram: {file_path.name}")
        command = [
            ffmpeg_path,
            "-y",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-threads",
            "1",
            "-i",
            str(file_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(temp_path),
        ]
    else:
        # Short reels only end up here; still keep the preset cheap for the VPS.
        log(f"Перекодирую в H.264/AAC, чтобы в Telegram был звук: {file_path.name}")
        command = [
            ffmpeg_path,
            "-y",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-threads",
            "1",
            "-i",
            str(file_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ac",
            "2",
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
    if result.returncode or not temp_path.exists():
        log(f"Не удалось починить звук {file_path.name}: {(result.stdout or '')[-400:]}")
        temp_path.unlink(missing_ok=True)
        return

    temp_path.replace(file_path)
    log(f"Файл готов для Telegram: {file_path.name}")


def prepare_telegram_audio(file_path: Path, log: LogFn = _noop_log) -> Path:
    """Remux Instagram music tracks to .m4a so Telegram sends them as audio."""
    if file_path.suffix.lower() == ".m4a":
        return file_path

    ffmpeg_path = get_ffmpeg_path()
    if not ffmpeg_path:
        return file_path

    dest = file_path.with_suffix(".m4a")
    if dest.exists():
        return dest

    result = subprocess.run(
        [
            ffmpeg_path,
            "-y",
            "-i",
            str(file_path),
            "-vn",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ac",
            "2",
            str(dest),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if result.returncode or not dest.exists():
        log(f"Не удалось подготовить аудио {file_path.name}: {(result.stdout or '')[-300:]}")
        return file_path

    file_path.unlink(missing_ok=True)
    log(f"Аудио готово для Telegram: {dest.name}")
    return dest


def cookies_file_has_sessionid(cookies_file: str | Path | None) -> bool:
    if not cookies_file:
        return False
    path = Path(cookies_file).expanduser()
    if not path.exists():
        return False
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 6 and parts[5] == "sessionid" and parts[-1].strip():
                return True
    except OSError:
        return False
    return False


def check_instagram_cookies_status(cookies_file: str | Path | None) -> str | None:
    """Return None when cookies look usable, else a short status code.

    Uses a public media info call rather than current_user: Instagram often
    answers current_user with `useragent mismatch` even for a healthy session.
    """
    if not cookies_file_has_sessionid(cookies_file):
        return "missing_sessionid"

    cookie_header = cookie_header_from_file(cookies_file)
    if not cookie_header:
        return "missing_sessionid"

    csrf = ""
    for part in cookie_header.split("; "):
        if part.startswith("csrftoken="):
            csrf = part.split("=", 1)[1]
            break

    # Well-known public post — only used to probe whether the session is accepted.
    request = Request(
        "https://www.instagram.com/api/v1/media/3968676798661474844/info/",
        headers={
            "User-Agent": USER_AGENT,
            "Cookie": cookie_header,
            "X-CSRFToken": csrf,
            "X-IG-App-ID": "936619743392459",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.instagram.com/",
        },
    )
    try:
        with urlopen(request, timeout=20, context=ssl._create_unverified_context()) as response:
            if response.status == 200:
                return None
            return "invalid_session"
    except Exception as exc:
        body = b""
        if hasattr(exc, "read"):
            try:
                body = exc.read()
            except Exception:
                body = b""
        text = body.decode("utf-8", errors="replace")
        if "checkpoint_required" in text:
            return "checkpoint_required"
        if "login_required" in text or "logged_out" in text:
            return "invalid_session"
        # Transient / UA quirks should not block downloads — gallery-dl/yt-dlp
        # will surface a real failure if the session is actually dead.
        return None


def detect_instagram_auth_failure(messages: list[str] | str) -> str | None:
    """Classify cookie/session failures from already collected download logs."""
    text = messages if isinstance(messages, str) else "\n".join(messages)
    lowered = text.lower()
    if "checkpoint_required" in lowered or (
        "checkpoint" in lowered and "подтвержд" in lowered
    ):
        return "checkpoint_required"
    if (
        "redirect to login" in lowered
        or "accounts/login" in lowered
        or "login_required" in lowered
        or "logged_out" in lowered
    ):
        return "invalid_session"
    if "нет sessionid" in lowered or "missing_sessionid" in lowered:
        return "missing_sessionid"
    return None


def format_instagram_auth_error(status: str | None) -> str:
    if status == "checkpoint_required":
        return (
            "Instagram требует подтверждение входа (checkpoint).\n"
            "Cookies на месте, но сессия заблокирована, пока не подтвердишь в браузере:\n"
            "1) Зайди в Instagram в Chrome под тем же аккаунтом\n"
            "2) Пройди проверку («Это был ты?», SMS, captcha — что попросит)\n"
            "3) Экспортируй новый instagram_cookies.txt и залей на сервер\n\n"
            "Часто так бывает, когда cookies с домашнего IP, а бот работает с VPS."
        )
    if status == "missing_sessionid":
        return (
            "В instagram_cookies.txt нет sessionid.\n"
            "Экспортируй cookies из браузера, где ты залогинен в Instagram."
        )
    return (
        "Instagram cookies не принял — сессия недействительна.\n"
        "Зайди в Instagram в браузере и экспортируй свежий cookies.txt."
    )


@dataclass
class YoutubeInfo:
    title: str = ""
    duration: int | None = None
    qualities: list[int] = field(default_factory=lambda: list(YOUTUBE_QUALITY_HEIGHTS))
    error: str | None = None


def apply_cookie_options(
    options: dict,
    *,
    cookies_browser: str | None = None,
    cookies_file: str | Path | None = None,
) -> None:
    if cookies_file:
        options["cookiefile"] = str(cookies_file)
    elif cookies_browser and cookies_browser != "Без cookies":
        options["cookiesfrombrowser"] = (cookies_browser,)


def format_age_gate_error(has_cookies: bool) -> str:
    if has_cookies:
        return (
            "YouTube просит подтвердить возраст, и даже с cookies не пускает.\n"
            "Проверь, что youtube_cookies.txt свежий и аккаунт открывает это видео в браузере."
        )
    return (
        "YouTube просит подтвердить возраст — без cookies такие ролики не отдаёт.\n"
        "Сделай youtube_cookies.txt:\n"
        "1) В Chrome зайди на youtube.com под аккаунтом 18+\n"
        "2) Расширение «Get cookies.txt LOCALLY» → Export для youtube.com\n"
        "3) Положи как youtube_cookies.txt рядом с ботом\n"
        "4) В .env укажи YOUTUBE_COOKIES_FILE=.../youtube_cookies.txt и перезапусти бота"
    )


def is_youtube_age_gate_error(text: str) -> bool:
    lowered = text.lower()
    return (
        "sign in to confirm your age" in lowered
        or "confirm your age" in lowered
        or "age-restricted" in lowered
        or "may be inappropriate" in lowered
    )


def inspect_youtube_video(
    url: str,
    *,
    cookies_browser: str | None = None,
    cookies_file: str | Path | None = None,
    log: LogFn = _noop_log,
) -> YoutubeInfo:
    ffmpeg_path = get_ffmpeg_path()
    last_error = None
    has_cookies = bool(cookies_file) or bool(
        cookies_browser and cookies_browser != "Без cookies"
    )

    for clients in YOUTUBE_PLAYER_CLIENT_ATTEMPTS:
        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "extract_flat": False,
            "logger": ListLogger(log),
            "extractor_args": youtube_extractor_args(clients),
        }
        if ffmpeg_path:
            options["ffmpeg_location"] = ffmpeg_path
        apply_cookie_options(
            options,
            cookies_browser=cookies_browser,
            cookies_file=cookies_file,
        )

        try:
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
        except DownloadError as error:
            last_error = str(error)
            if is_youtube_age_gate_error(last_error):
                return YoutubeInfo(error=format_age_gate_error(has_cookies))
            continue
        except Exception as error:
            last_error = str(error)
            continue

        if not info:
            last_error = "Не удалось получить данные YouTube."
            continue

        if info.get("_type") == "playlist":
            entries = [entry for entry in (info.get("entries") or []) if entry]
            if not entries:
                last_error = "Плейлист пуст."
                continue
            info = entries[0]

        duration = info.get("duration")
        duration_seconds = int(duration) if isinstance(duration, (int, float)) else None
        if duration_seconds and duration_seconds > YOUTUBE_MAX_DURATION_SECONDS:
            minutes = duration_seconds // 60
            return YoutubeInfo(
                title=str(info.get("title") or ""),
                duration=duration_seconds,
                error=f"Ролик слишком длинный ({minutes} мин). Максимум 20 минут.",
            )

        qualities = youtube_qualities_from_formats(info.get("formats") or [])
        if qualities:
            return YoutubeInfo(
                title=str(info.get("title") or ""),
                duration=duration_seconds,
                qualities=qualities,
            )
        last_error = "YouTube не отдал список качеств."

    if last_error and is_youtube_age_gate_error(last_error):
        return YoutubeInfo(error=format_age_gate_error(has_cookies))
    return YoutubeInfo(error=last_error or "Не удалось получить данные YouTube.")


def compress_height_ladder(original_height: int | None, video_bps: int) -> list[int | None]:
    """Resolutions to try, best affordable first. None means "keep the original"."""
    cap = COMPRESS_HEIGHT_BITRATE[-1][0]
    for height, min_bps in COMPRESS_HEIGHT_BITRATE:
        if video_bps >= min_bps:
            cap = height
            break

    ladder: list[int | None] = []
    if original_height is None or original_height <= cap:
        ladder.append(None)
    for height in (720, 480, 360):
        if height <= cap and (original_height is None or height < original_height):
            ladder.append(height)
    return ladder or [None]


def compress_video_bitrate(duration: int, max_bytes: int) -> int:
    target_bytes = min(max_bytes, COMPRESS_TARGET_BYTES)
    return max(int(target_bytes * 8 * 0.90 / max(duration, 1)) - 96_000, 120_000)


def estimate_compress_seconds(metadata: VideoMetadata, height: int | None) -> int:
    """Rough wall clock for one encode pass, used for the ETA and the timeout."""
    duration = max(metadata.duration or 1, 1)
    source_fps = min(metadata.fps or 30, 120)
    source_pps = (metadata.width or 1280) * (metadata.height or 720) * source_fps

    output_height = height or metadata.height or 720
    output_pps = output_height * output_height * 16 / 9 * min(source_fps, COMPRESS_MAX_FPS)

    decode = source_pps / COMPRESS_DECODE_PIXELS_PER_SECOND
    encode = output_pps / COMPRESS_ENCODE_PIXELS_PER_SECOND
    return int(duration * (decode + encode))


def estimate_compress_for_file(
    file_path: Path,
    *,
    max_bytes: int = TELEGRAM_MAX_UPLOAD_BYTES,
) -> int:
    metadata = probe_video_metadata(file_path)
    video_bps = compress_video_bitrate(metadata.duration or 1, max_bytes)
    height = compress_height_ladder(metadata.height, video_bps)[0]
    return estimate_compress_seconds(metadata, height)


class CompressTimeout(RuntimeError):
    pass


def _run_ffmpeg_with_progress(
    command: list[str],
    *,
    duration: int,
    deadline: float,
    progress: ProgressFn | None = None,
) -> tuple[int, str]:
    """Run ffmpeg, report percent done, and kill it if it runs past the deadline."""
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as error_log:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=error_log,
            text=True,
        )
        timed_out = False
        try:
            for line in process.stdout or []:
                if progress and line.startswith("out_time_us="):
                    value = line.split("=", 1)[1].strip()
                    if value.isdigit():
                        done = int(value) / 1_000_000 / max(duration, 1)
                        progress(min(max(done, 0.0), 1.0) * 100)
                if time.monotonic() > deadline:
                    timed_out = True
                    break
        finally:
            if timed_out:
                process.kill()
            returncode = process.wait()

        error_log.seek(0)
        output = error_log.read()[-1000:]

    if timed_out:
        raise CompressTimeout(output)
    return returncode, output


def compress_video_for_telegram(
    file_path: Path,
    *,
    max_bytes: int = TELEGRAM_MAX_UPLOAD_BYTES,
    timeout_seconds: int = COMPRESS_TIMEOUT_SECONDS,
    log: LogFn = _noop_log,
    progress: ProgressFn | None = None,
) -> Path:
    """Re-encode to fit Telegram's upload limit, keeping the original aspect ratio."""
    ffmpeg_path = get_ffmpeg_path()
    if not ffmpeg_path:
        raise RuntimeError("ffmpeg не найден, не могу сжать видео.")

    metadata = probe_video_metadata(file_path)
    duration = max(metadata.duration or 1, 1)
    video_bps = compress_video_bitrate(duration, max_bytes)
    ladder = compress_height_ladder(metadata.height, video_bps)

    # A slow box needs more than the default budget; the cap still stops runaways.
    budget = min(
        max(timeout_seconds, estimate_compress_seconds(metadata, ladder[0]) * 2),
        COMPRESS_TIMEOUT_CAP_SECONDS,
    )
    temp_path = file_path.with_name(file_path.stem + ".tg.mp4")
    deadline = time.monotonic() + budget
    last_error = "неизвестная ошибка"

    for height in ladder:
        command = [
            ffmpeg_path,
            "-y",
            "-nostdin",
            "-loglevel",
            "error",
            "-nostats",
            "-progress",
            "pipe:1",
            "-threads",
            "1",
            "-i",
            str(file_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "superfast",
            # A short lookahead keeps x264 well under the service memory limit;
            # going over it makes the cgroup throttle the encode to a crawl.
            "-x264-params",
            "rc-lookahead=20:sync-lookahead=0",
            "-b:v",
            str(video_bps),
            "-maxrate",
            str(video_bps),
            "-bufsize",
            str(video_bps * 2),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
        ]
        filters = []
        if height is not None:
            filters.append(f"scale=-2:{height}")
        if metadata.fps and metadata.fps > COMPRESS_MAX_FPS:
            filters.append(f"fps={COMPRESS_MAX_FPS}")
        if filters:
            command.extend(["-vf", ",".join(filters)])

        target = f"{height}p" if height is not None else "исходное разрешение"
        log(f"Сжимаю: {target}, {video_bps // 1000} kbps...")

        command.append(str(temp_path))
        try:
            returncode, output = _run_ffmpeg_with_progress(
                command,
                duration=duration,
                deadline=deadline,
                progress=progress,
            )
        except CompressTimeout:
            if temp_path.exists():
                temp_path.unlink()
            minutes = budget // 60
            raise RuntimeError(
                f"Сжатие не уложилось в {minutes} мин — ролик слишком тяжёлый "
                "для этого сервера. Скачай его в меньшем качестве."
            ) from None

        if returncode:
            last_error = output.strip() or f"код {returncode}"
            log(f"ffmpeg не смог сжать: {last_error}")
            if temp_path.exists():
                temp_path.unlink()
            continue

        size = temp_path.stat().st_size
        log(f"Сжатый файл: {format_mb(size)}")
        if size <= max_bytes:
            file_path.unlink()
            temp_path.replace(file_path)
            return file_path

        last_error = f"после сжатия всё ещё {format_mb(size)}"
        video_bps = max(int(video_bps * 0.75), 80_000)

    if temp_path.exists():
        temp_path.unlink()
    raise RuntimeError(f"Не удалось ужать видео до {format_mb(max_bytes)}: {last_error}")


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
    elif is_youtube_age_gate_error(error_text):
        tips.extend(
            [
                "1. Ролик с возрастным ограничением — нужен youtube_cookies.txt.",
                "2. Экспортируй cookies с youtube.com (аккаунт 18+) и задай YOUTUBE_COOKIES_FILE.",
                "3. Подробности: https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies",
            ]
        )
    elif "HTTP Error 403" in error_text or "unable to download video data" in error_text:
        tips.extend(
            [
                "1. YouTube часто отвечает 403 на один из клиентов плеера.",
                "2. Бот сам перебирает android_vr → web_embedded → tv.",
                "3. Если все три не сработали — подожди и попробуй снова, IP мог временно ограничиться.",
            ]
        )
    else:
        tips.extend(
            [
                "1. Проверь ссылку и доступность поста.",
                "2. Для Instagram обычно нужны cookies (INSTAGRAM_COOKIES_FILE).",
                "3. Для age-restricted YouTube — YOUTUBE_COOKIES_FILE.",
                "4. Обычные TikTok/YouTube cookies не требуют.",
            ]
        )

    return tips


def download_instagram_media(
    url: str,
    output_path: Path,
    *,
    cookies_browser: str | None = None,
    cookies_file: str | Path | None = None,
    youtube_cookies_browser: str | None = None,
    youtube_cookies_file: str | Path | None = None,
    log: LogFn | None = None,
    max_height: int | None = None,
) -> DownloadResult:
    messages: list[str] = []

    def emit(message: str) -> None:
        messages.append(message)
        if log:
            log(message)

    output_path = Path(output_path).expanduser().resolve()
    output_path.mkdir(parents=True, exist_ok=True)

    # Keep cookies scoped to the platform that needs them. Instagram cookies on
    # TikTok/YouTube are useless and sometimes trigger browser/keychain errors.
    cookie_scratch: Path | None = None
    if is_instagram_url(url):
        effective_cookies_browser = cookies_browser
        effective_cookies_file = cookies_file
        if effective_cookies_file and not cookies_file_has_sessionid(effective_cookies_file):
            emit(
                "ВНИМАНИЕ: в Instagram cookies нет sessionid — "
                "часто приходит видео без звука. Обнови cookies.txt из браузера."
            )
    elif is_youtube_url(url):
        effective_cookies_browser = youtube_cookies_browser
        effective_cookies_file = youtube_cookies_file
    else:
        effective_cookies_browser = None
        effective_cookies_file = None

    # yt-dlp rewrites cookiefile in place; if the session dies it can strip
    # sessionid from our persistent file. Feed it a throwaway copy instead.
    if effective_cookies_file:
        source = Path(effective_cookies_file).expanduser()
        if source.exists():
            scratch = Path(tempfile.mkstemp(prefix="cookies_", suffix=".txt")[1])
            scratch.write_bytes(source.read_bytes())
            cookie_scratch = scratch
            effective_cookies_file = scratch

    emit(f"Ссылка: {url}")
    emit(f"Папка: {output_path}")
    emit(f"Cookies browser: {effective_cookies_browser or 'нет'}")
    emit(f"Cookies file: {cookies_file or youtube_cookies_file or 'нет'}")

    # Only warn about missing sessionid up front. Hitting Instagram's API for a
    # "session probe" burns fresh accounts and can trigger checkpoint before
    # the real download even starts.
    if is_instagram_url(url) and cookies_file and not cookies_file_has_sessionid(cookies_file):
        emit(
            "ВНИМАНИЕ: в Instagram cookies нет sessionid — "
            "часто приходит видео без звука / фото не качаются."
        )

    existing_media_files = get_media_files(output_path)

    try:
        youtube = is_youtube_url(url)
        client_attempts = YOUTUBE_PLAYER_CLIENT_ATTEMPTS if youtube else (None,)
        result_code = 1
        files_after_ydl: list[Path] = []

        for index, clients in enumerate(client_attempts):
            if youtube and clients:
                emit(f"YouTube-клиент: {', '.join(clients)}")
            options = build_ydl_options(
                output_path,
                cookies_browser=effective_cookies_browser,
                cookies_file=effective_cookies_file,
                log=emit,
                max_height=max_height,
                noplaylist=youtube,
                prefer_avc=youtube,
                prefer_progressive=is_instagram_url(url),
                extractor_args=youtube_extractor_args(clients) if clients else None,
            )
            with YoutubeDL(options) as ydl:
                result_code = ydl.download([url])
            files_after_ydl = sorted(get_media_files(output_path) - existing_media_files)
            if files_after_ydl or not youtube:
                break
            if index + 1 < len(client_attempts):
                emit("YouTube отклонил поток (часто 403). Пробую другой клиент...")
                for leftover in get_media_files(output_path) - existing_media_files:
                    leftover.unlink(missing_ok=True)

        if is_instagram_url(url):
            photo_mode = instagram_photo_fallback_mode(result_code, files_after_ydl)
            if photo_mode:
                gallery_result_code = 1
                files_before_gallery = set(get_media_files(output_path))
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
                    gallery_images = [
                        path
                        for path in (get_media_files(output_path) - files_before_gallery)
                        if is_image(path)
                    ]
                    gallery_audio = [
                        path
                        for path in (get_media_files(output_path) - files_before_gallery)
                        if is_audio_only(path)
                    ]
                    if gallery_result_code == 0 and (gallery_images or gallery_audio):
                        parts = []
                        if gallery_images:
                            parts.append(f"фото: {len(gallery_images)}")
                        if gallery_audio:
                            parts.append(f"аудио: {len(gallery_audio)}")
                        emit(f"Карусель обработана ({', '.join(parts)})")
                    else:
                        gallery_result_code = 1
                        emit("gallery-dl не смог скачать фото и аудио.")

                # gallery-dl often skips licensed music when Instagram redacts the URL.
                # Try the media info API ourselves as a second chance.
                if not any(
                    is_audio_only(path)
                    for path in (get_media_files(output_path) - files_before_gallery)
                ):
                    try:
                        audio_files = download_instagram_attached_audio(
                            url,
                            output_path,
                            cookies_file=effective_cookies_file or cookies_file,
                            log=emit,
                        )
                        if audio_files:
                            emit(f"Аудио к посту: {len(audio_files)}")
                    except Exception as audio_error:
                        emit(f"Не удалось отдельно скачать аудио: {audio_error}")

                # Page scrape pulls video posters/covers — only for pure photo posts.
                if photo_mode == "full" and gallery_result_code != 0:
                    try:
                        photo_count = download_instagram_photos(
                            url,
                            output_path,
                            emit,
                            cookies_file=effective_cookies_file or cookies_file,
                        )
                        if photo_count:
                            emit(f"Фото сохранено: {photo_count}")
                    except Exception as photo_error:
                        emit(f"Не удалось скачать фото из страницы Instagram: {photo_error}")
            else:
                emit("Фото-fallback пропущен: видео уже скачано без пропусков.")

        new_files = filter_video_thumbnails(
            sorted(get_media_files(output_path) - existing_media_files)
        )
        prepared_files: list[Path] = []
        for file_path in new_files:
            if is_video(file_path):
                ensure_telegram_compatible_video(file_path, emit)
                prepared_files.append(file_path)
            elif is_audio_only(file_path):
                prepared_files.append(prepare_telegram_audio(file_path, emit))
            else:
                prepared_files.append(file_path)
        new_files = sort_files_for_delivery(prepared_files)
        partial = bool(result_code) and bool(new_files)
        # yt-dlp exits non-zero on photo-only posts even when photos downloaded fine.
        if partial and is_instagram_url(url) and new_files and all(
            is_image(path) or is_audio_only(path) for path in new_files
        ):
            partial = False

        if not new_files:
            joined = "\n".join(messages)
            if is_instagram_url(url) and not cookies_file and (
                not cookies_browser or cookies_browser == "Без cookies"
            ):
                emit(
                    "Это фото или карусель Instagram — без cookies их почти не отдают.\n"
                    "Сделай один раз cookies.txt (без чтения браузера каждый раз):\n"
                    "1) В Chrome зайди в Instagram под своим аккаунтом\n"
                    "2) Расширение «Get cookies.txt LOCALLY» → Export → сохранить файл\n"
                    "3) Положи как instagram_cookies.txt рядом с ботом\n"
                    "4) В .env укажи INSTAGRAM_COOKIES_FILE=.../instagram_cookies.txt и перезапусти бота"
                )
                return DownloadResult(
                    files=[],
                    messages=messages,
                    error="Не удалось скачать медиа.",
                    partial=False,
                )
            if is_instagram_url(url):
                auth_status = detect_instagram_auth_failure(joined) or check_instagram_cookies_status(
                    cookies_file
                )
                emit(format_instagram_auth_error(auth_status))
                return DownloadResult(
                    files=[],
                    messages=messages,
                    error=f"instagram_auth:{auth_status or 'invalid_session'}",
                    partial=False,
                )
            if youtube and is_youtube_age_gate_error(joined):
                emit(
                    format_age_gate_error(
                        bool(effective_cookies_file)
                        or bool(
                            effective_cookies_browser
                            and effective_cookies_browser != "Без cookies"
                        )
                    )
                )
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
        if is_youtube_url(url) and is_youtube_age_gate_error(error_text):
            emit(
                format_age_gate_error(
                    bool(youtube_cookies_file)
                    or bool(
                        youtube_cookies_browser and youtube_cookies_browser != "Без cookies"
                    )
                )
            )
        for tip in format_download_error(error_text):
            emit(tip)
        return DownloadResult(files=[], messages=messages, error=error_text, partial=False)

    except Exception as e:
        emit(f"Неожиданная ошибка: {e}")
        return DownloadResult(files=[], messages=messages, error=str(e), partial=False)

    finally:
        if cookie_scratch is not None:
            cookie_scratch.unlink(missing_ok=True)
