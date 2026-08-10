#!/Library/Frameworks/Python.framework/Versions/3.14/bin/python3
import sys
import threading
import queue
import html
import re
import ssl
import subprocess
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except ImportError as e:
    print(
        "Не удалось импортировать tkinter.\n"
        f"Текущий Python: {sys.executable}\n"
        f"Ошибка: {e}",
        file=sys.stderr,
    )
    raise SystemExit(1)

try:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError
except ImportError:
    print(
        "Не установлен yt-dlp для этого Python.\n"
        f"Текущий Python: {sys.executable}\n\n"
        "Запусти в Терминале:\n"
        f'"{sys.executable}" -m pip install -U yt-dlp imageio-ffmpeg gallery-dl\n\n'
        "Или открывай скрипт через файл Instagram Downloader.command в этой папке",
        file=sys.stderr,
    )
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Нет зависимостей",
            "Не установлен yt-dlp для этого Python.\n\n"
            f"Python: {sys.executable}\n\n"
            "Установи пакеты: pip install -r requirements.txt\n"
            "или запускай через Instagram Downloader.command",
        )
        root.destroy()
    except Exception:
        pass
    raise SystemExit(1)

try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
INSTAGRAM_PAGE_USER_AGENT = "Mozilla/5.0"


def get_system_downloads_folder() -> Path:
    home = Path.home()

    if sys.platform == "darwin":
        return home / "Downloads"

    if sys.platform.startswith("win"):
        return home / "Downloads"

    if sys.platform.startswith("linux"):
        return home / "Downloads"

    return home


class GuiLogger:
    def __init__(self, log_queue: queue.Queue):
        self.log_queue = log_queue

    def debug(self, message):
        if message:
            self.log_queue.put(str(message))

    def info(self, message):
        if message:
            self.log_queue.put(str(message))

    def warning(self, message):
        if message:
            self.log_queue.put("WARNING: " + str(message))

    def error(self, message):
        if message:
            text = str(message)

            if "No video formats found" in text:
                self.log_queue.put("Фото-элемент карусели не является видео, обработаю его отдельно.")
                return

            self.log_queue.put("ERROR: " + text)


class InstagramDownloaderApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Instagram Downloader")
        self.root.geometry("760x520")
        self.root.minsize(680, 460)

        self.log_queue = queue.Queue()
        self.is_downloading = False

        self.url_var = tk.StringVar()
        self.output_dir_var = tk.StringVar(value=str(get_system_downloads_folder()))
        self.cookies_browser_var = tk.StringVar(value="Без cookies")
        self.print_direct_url_var = tk.BooleanVar(value=False)

        self.build_ui()
        self.process_log_queue()

    def build_ui(self):
        main = ttk.Frame(self.root, padding=16)
        main.pack(fill=tk.BOTH, expand=True)

        title = ttk.Label(
            main,
            text="Скачивание Instagram post / reels / video / photo",
            font=("Arial", 16, "bold")
        )
        title.pack(anchor="w", pady=(0, 12))

        url_label = ttk.Label(main, text="Ссылка на Instagram / TikTok:")
        url_label.pack(anchor="w")

        self.url_entry = ttk.Entry(main, textvariable=self.url_var, font=("Arial", 12))
        self.url_entry.pack(fill=tk.X, pady=(4, 12))
        self.url_entry.focus()
        self.add_text_edit_shortcuts(self.url_entry)

        folder_row = ttk.Frame(main)
        folder_row.pack(fill=tk.X, pady=(0, 12))

        folder_left = ttk.Frame(folder_row)
        folder_left.pack(side=tk.LEFT, fill=tk.X, expand=True)

        folder_label = ttk.Label(folder_left, text="Папка для скачивания:")
        folder_label.pack(anchor="w")

        folder_entry = ttk.Entry(folder_left, textvariable=self.output_dir_var)
        folder_entry.pack(fill=tk.X, pady=(4, 0))
        self.add_text_edit_shortcuts(folder_entry)

        browse_button = ttk.Button(folder_row, text="Выбрать папку", command=self.choose_folder)
        browse_button.pack(side=tk.RIGHT, padx=(12, 0), pady=(20, 0))

        options_row = ttk.Frame(main)
        options_row.pack(fill=tk.X, pady=(0, 12))

        cookies_label = ttk.Label(options_row, text="Cookies из браузера:")
        cookies_label.pack(side=tk.LEFT)

        cookies_combo = ttk.Combobox(
            options_row,
            textvariable=self.cookies_browser_var,
            values=[
                "Без cookies",
                "chrome",
                "firefox",
                "safari",
                "brave",
                "edge",
                "opera",
                "vivaldi",
            ],
            state="readonly",
            width=18
        )
        cookies_combo.pack(side=tk.LEFT, padx=(8, 18))

        direct_check = ttk.Checkbutton(
            options_row,
            text="Только показать прямую CDN-ссылку",
            variable=self.print_direct_url_var
        )
        direct_check.pack(side=tk.LEFT)

        buttons_row = ttk.Frame(main)
        buttons_row.pack(fill=tk.X, pady=(0, 12))

        self.download_button = ttk.Button(buttons_row, text="Скачать", command=self.start_download)
        self.download_button.pack(side=tk.LEFT)

        clear_button = ttk.Button(buttons_row, text="Очистить лог", command=self.clear_log)
        clear_button.pack(side=tk.LEFT, padx=(8, 0))

        self.status_label = ttk.Label(main, text="Готово к работе")
        self.status_label.pack(anchor="w", pady=(0, 6))

        log_label = ttk.Label(main, text="Лог:")
        log_label.pack(anchor="w")

        log_frame = ttk.Frame(main)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = tk.Text(log_frame, height=14, wrap=tk.WORD)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.add_text_edit_shortcuts(self.log_text)

        scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.log_text.configure(yscrollcommand=scrollbar.set)

    def add_text_edit_shortcuts(self, widget):
        widget.bind("<Command-v>", lambda event: self.widget_event(widget, "<<Paste>>"))
        widget.bind("<Command-c>", lambda event: self.widget_event(widget, "<<Copy>>"))
        widget.bind("<Command-x>", lambda event: self.widget_event(widget, "<<Cut>>"))
        widget.bind("<Command-a>", lambda event: self.select_all(widget))
        widget.bind("<Control-v>", lambda event: self.widget_event(widget, "<<Paste>>"))
        widget.bind("<Control-c>", lambda event: self.widget_event(widget, "<<Copy>>"))
        widget.bind("<Control-x>", lambda event: self.widget_event(widget, "<<Cut>>"))
        widget.bind("<Control-a>", lambda event: self.select_all(widget))

    def widget_event(self, widget, event_name: str):
        widget.event_generate(event_name)
        return "break"

    def select_all(self, widget):
        if isinstance(widget, tk.Text):
            widget.tag_add(tk.SEL, "1.0", tk.END)
            widget.mark_set(tk.INSERT, "1.0")
            widget.see(tk.INSERT)
        else:
            widget.selection_range(0, tk.END)
            widget.icursor(tk.END)

        return "break"

    def choose_folder(self):
        folder = filedialog.askdirectory(initialdir=self.output_dir_var.get())
        if folder:
            self.output_dir_var.set(folder)

    def clear_log(self):
        self.log_text.delete("1.0", tk.END)

    def append_log(self, message: str):
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)

    def process_log_queue(self):
        try:
            while True:
                message = self.log_queue.get_nowait()
                self.append_log(message)
        except queue.Empty:
            pass

        self.root.after(150, self.process_log_queue)

    def set_downloading_state(self, downloading: bool):
        self.is_downloading = downloading

        if downloading:
            self.download_button.configure(state=tk.DISABLED)
            self.status_label.configure(text="Скачивание...")
        else:
            self.download_button.configure(state=tk.NORMAL)
            self.status_label.configure(text="Готово к работе")

    def start_download(self):
        if self.is_downloading:
            return

        url = self.url_var.get().strip()
        output_dir = self.output_dir_var.get().strip()

        if not url:
            messagebox.showerror("Ошибка", "Вставь ссылку на Instagram или TikTok.")
            return

        if "instagram.com" not in url and "tiktok.com" not in url:
            messagebox.showwarning(
                "Проверка ссылки",
                "Ссылка не похожа на Instagram/TikTok URL, но я всё равно попробую скачать."
            )

        if not output_dir:
            messagebox.showerror("Ошибка", "Выбери папку для скачивания.")
            return

        output_path = Path(output_dir).expanduser().resolve()
        output_path.mkdir(parents=True, exist_ok=True)

        self.set_downloading_state(True)

        thread = threading.Thread(
            target=self.download_worker,
            args=(url, output_path),
            daemon=True
        )
        thread.start()

    def build_ydl_options(self, output_path: Path) -> dict:
        selected_browser = self.cookies_browser_var.get()
        ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe() if imageio_ffmpeg else None

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
            "logger": GuiLogger(self.log_queue),
            "progress_hooks": [self.progress_hook],
        }

        if ffmpeg_path:
            options["ffmpeg_location"] = ffmpeg_path

        if selected_browser != "Без cookies":
            options["cookiesfrombrowser"] = (selected_browser,)

        return options

    def progress_hook(self, data: dict):
        status = data.get("status")

        if status == "downloading":
            filename = data.get("filename", "")
            percent = data.get("_percent_str", "").strip()
            speed = data.get("_speed_str", "").strip()
            eta = data.get("_eta_str", "").strip()

            line = f"Скачивание: {percent}"

            if speed:
                line += f" | скорость: {speed}"

            if eta:
                line += f" | осталось: {eta}"

            if filename:
                line += f" | файл: {Path(filename).name}"

            self.log_queue.put(line)

        elif status == "finished":
            filename = data.get("filename", "")
            if filename:
                self.log_queue.put(f"Файл скачан: {filename}")
            self.log_queue.put("Обработка файла...")

    def get_shortcode_from_url(self, url: str) -> str:
        parts = [part for part in urlparse(url).path.split("/") if part]

        for index, part in enumerate(parts):
            if part in {"p", "reel", "tv"} and index + 1 < len(parts):
                return parts[index + 1]

        return "instagram"

    def find_instagram_photo_urls(self, page_html: str) -> list[str]:
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

            parsed = urlparse(photo_url)
            path_key = parsed.path

            if path_key in seen_paths:
                continue

            seen_paths.add(path_key)
            photo_urls.append(photo_url)

        return photo_urls

    def download_file(self, url: str, destination: Path, referer: str):
        request = Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Referer": referer,
            },
        )

        with urlopen(request, timeout=30, context=ssl._create_unverified_context()) as response:
            destination.write_bytes(response.read())

    def download_instagram_photos(self, url: str, output_path: Path) -> int:
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

        photo_urls = self.find_instagram_photo_urls(page_html)

        if not photo_urls:
            return 0

        shortcode = self.get_shortcode_from_url(url)
        output_path.mkdir(parents=True, exist_ok=True)

        downloaded_count = 0

        for index, photo_url in enumerate(photo_urls, start=1):
            destination = output_path / f"{shortcode}_{index:02d}.jpg"

            if destination.exists():
                self.log_queue.put(f"Фото уже есть: {destination}")
                continue

            self.download_file(photo_url, destination, url)
            downloaded_count += 1
            self.log_queue.put(f"Фото скачано: {destination}")

        return downloaded_count

    def download_photos_with_gallery_dl(self, url: str, output_path: Path) -> int:
        selected_browser = self.cookies_browser_var.get()

        if selected_browser == "Без cookies":
            self.log_queue.put("Для скачивания всех фото из этой карусели выбери cookies из браузера, где открыт Instagram.")
            return 1

        command = [
            sys.executable,
            "-m",
            "gallery_dl",
            "--no-colors",
            "--no-check-certificate",
            "--cookies-from-browser",
            selected_browser,
            "--filter",
            "extension in ('jpg', 'jpeg', 'png', 'webp')",
            "-D",
            str(output_path),
            "-f",
            "{date:%Y%m%d}_{media_id}.{extension}",
            url,
        ]

        self.log_queue.put("Скачивание фото из Instagram-карусели...")

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
                    self.log_queue.put(line)

        return process.wait()

    def get_ffmpeg_path(self) -> str | None:
        if not imageio_ffmpeg:
            return None

        return imageio_ffmpeg.get_ffmpeg_exe()

    def is_quicktime_friendly_mp4(self, file_path: Path) -> bool:
        ffmpeg_path = self.get_ffmpeg_path()

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

    def make_mp4_quicktime_friendly(self, file_path: Path):
        ffmpeg_path = self.get_ffmpeg_path()

        if not ffmpeg_path:
            self.log_queue.put("ffmpeg не найден, не могу перекодировать MP4 для QuickTime.")
            return

        if self.is_quicktime_friendly_mp4(file_path):
            return

        temp_path = file_path.with_name(file_path.stem + ".quicktime.mp4")
        self.log_queue.put(f"Перекодирую для QuickTime: {file_path.name}")

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
            self.log_queue.put(f"Не удалось перекодировать {file_path.name}: {result.stdout[-1000:]}")
            if temp_path.exists():
                temp_path.unlink()
            return

        temp_path.replace(file_path)
        self.log_queue.put(f"MP4 готов для QuickTime: {file_path}")

    def make_new_mp4_files_quicktime_friendly(self, output_path: Path, existing_files: set[Path]):
        for file_path in sorted(output_path.glob("*.mp4")):
            if file_path not in existing_files:
                self.make_mp4_quicktime_friendly(file_path)

    def get_media_files(self, output_path: Path) -> set[Path]:
        extensions = {".jpg", ".jpeg", ".png", ".webp", ".mp4"}
        return {
            file_path
            for file_path in output_path.glob("*")
            if file_path.is_file() and file_path.suffix.lower() in extensions
        }

    def download_worker(self, url: str, output_path: Path):
        try:
            self.log_queue.put(f"Ссылка: {url}")
            self.log_queue.put(f"Папка: {output_path}")
            self.log_queue.put(f"Cookies: {self.cookies_browser_var.get()}")
            existing_mp4_files = set(output_path.glob("*.mp4"))
            existing_media_files = self.get_media_files(output_path)

            options = self.build_ydl_options(output_path)

            if self.print_direct_url_var.get():
                options["skip_download"] = True
                options["quiet"] = True

                with YoutubeDL(options) as ydl:
                    info = ydl.extract_info(url, download=False)

                    if not info:
                        self.log_queue.put("Не удалось получить данные.")
                        return

                    self.log_queue.put("Прямые временные ссылки:")

                    if "entries" in info and info["entries"]:
                        for index, entry in enumerate(info["entries"], start=1):
                            if not entry:
                                continue

                            title = entry.get("title") or "Без названия"
                            direct_url = entry.get("url") or "URL не найден"
                            ext = entry.get("ext") or "unknown"

                            self.log_queue.put(f"\nМедиа #{index}")
                            self.log_queue.put(f"Название: {title}")
                            self.log_queue.put(f"Тип: {ext}")
                            self.log_queue.put(f"URL: {direct_url}")
                    else:
                        title = info.get("title") or "Без названия"
                        direct_url = info.get("url") or "URL не найден"
                        ext = info.get("ext") or "unknown"

                        self.log_queue.put(f"Название: {title}")
                        self.log_queue.put(f"Тип: {ext}")
                        self.log_queue.put(f"URL: {direct_url}")

            else:
                selected_browser = self.cookies_browser_var.get()

                with YoutubeDL(options) as ydl:
                    result_code = ydl.download([url])

                self.make_new_mp4_files_quicktime_friendly(output_path, existing_mp4_files)

                photo_count = 0
                gallery_result_code = 1

                if "instagram.com" in url:
                    if selected_browser != "Без cookies":
                        gallery_result_code = self.download_photos_with_gallery_dl(url, output_path)
                        if gallery_result_code == 0:
                            self.log_queue.put("Фото из карусели обработаны.")
                        else:
                            self.log_queue.put("gallery-dl не смог скачать фото, пробую запасной публичный способ.")

                    if selected_browser == "Без cookies" or gallery_result_code != 0:
                        try:
                            photo_count = self.download_instagram_photos(url, output_path)
                        except Exception as photo_error:
                            self.log_queue.put(f"Не удалось скачать фото из страницы Instagram: {photo_error}")

                new_mp4_count = len([file_path for file_path in output_path.glob("*.mp4") if file_path not in existing_mp4_files])
                new_media_count = len(self.get_media_files(output_path) - existing_media_files)

                if "instagram.com" in url and not new_media_count and selected_browser == "Без cookies":
                    self.log_queue.put("Instagram не отдал фото без авторизации. Выбери cookies из браузера и запусти ещё раз.")

                if result_code and not new_media_count:
                    self.log_queue.put("Завершено частично: часть элементов не удалось скачать.")
                else:
                    self.log_queue.put("Готово.")

                if photo_count:
                    self.log_queue.put(f"Фото сохранено: {photo_count}")

        except DownloadError as e:
            error_text = str(e)
            self.log_queue.put(f"Ошибка yt-dlp: {error_text}")
            self.log_queue.put("")
            self.log_queue.put("Что проверить:")

            if "Operation not permitted" in error_text or "Errno 1" in error_text:
                self.log_queue.put("1. macOS блокирует чтение cookies браузера.")
                self.log_queue.put("2. Системные настройки → Конфиденциальность и безопасность → Полный доступ к диску")
                self.log_queue.put("3. Добавь туда Терминал (или Python / Cursor), перезапусти приложение и попробуй снова.")
                self.log_queue.put("4. Либо экспортируй cookies.txt из расширения браузера и используй браузер, где Instagram уже открыт.")
            elif "could not be decrypted" in error_text or "no key found" in error_text or "find-generic-password failed" in error_text:
                self.log_queue.put("1. Не удалось расшифровать cookies Chrome/Chromium (нет доступа к Keychain).")
                self.log_queue.put("2. Запусти скрипт из обычного Терминала.app и разреши доступ к связке ключей.")
                self.log_queue.put("3. Либо выбери Safari (нужен Полный доступ к диску) или Firefox.")
            elif "empty media response" in error_text or "not granting access" in error_text:
                self.log_queue.put("1. Instagram требует авторизацию для этой ссылки.")
                self.log_queue.put("2. Выбери cookies из браузера, где ты залогинен в Instagram.")
                self.log_queue.put("3. Убедись, что пост открывается в этом браузере.")
                self.log_queue.put("4. Если cookies не читаются — дай Терминалу Полный доступ к диску / доступ к Keychain.")
            elif "No video formats found" in error_text:
                self.log_queue.put("1. Если это фото-пост или карусель без видео, yt-dlp может не скачать его как видео.")
                self.log_queue.put("2. Если пост приватный, 18+, удалённый или требует входа, выбери cookies из браузера, где ты залогинен в Instagram.")
                self.log_queue.put("3. Для Safari/Chrome сначала открой Instagram в браузере и убедись, что этот пост там реально открывается.")
                self.log_queue.put("4. Если ошибка остаётся только на одной ссылке, вероятно Instagram не отдаёт медиа для этого поста через yt-dlp.")
            else:
                self.log_queue.put("1. Выбери cookies из браузера, где открыт Instagram.")
                self.log_queue.put("2. Проверь, что ссылка открывается в браузере.")
                self.log_queue.put("3. Для Safari нужен Полный доступ к диску; для Chrome — доступ к Keychain.")

        except Exception as e:
            self.log_queue.put(f"Неожиданная ошибка: {e}")

        finally:
            self.root.after(0, lambda: self.set_downloading_state(False))


def main():
    root = tk.Tk()
    app = InstagramDownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
