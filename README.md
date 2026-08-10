# Instagram & TikTok Downloader

Кинул ссылку — получил медиа. Без вкладок «сохранить как», без танцев с cookies в голове и без загадки, *куда* делся оригинал.

Два лица одной утилиты:

- **GUI на macOS** — вставил ссылку, нажал «Скачать», файл лежит в `Downloads`
- **Telegram-бот** — прислал Instagram или TikTok, получил превью + файл без сжатия Telegram

Под капотом: `yt-dlp`, при необходимости `gallery-dl` и cookies браузера для капризных постов Instagram.

---

## Что умеет

| | Instagram | TikTok |
|---|---|---|
| Видео / Reels | да | да |
| Фото | да | — |
| Карусель (фото + видео) | да, по элементам | — |
| Превью в чате + оригинал файлом | в боте | в боте |

Бот после видео ещё кидает случайный **анекдот категории Б** — потому что скачивать в тишине скучно.

Доступ в бота закрыт **секретным вопросом** (один раз на пользователя), а не списком числовых id.

---

## Быстрый старт (macOS)

### 1. Зависимости

```bash
cd /path/to/instagram-downloader
python3 -m pip install -U -r requirements.txt
```

Нужен Python 3.10+ (в проекте удобно запускать через системный `python3`).

### 2. GUI

Двойной клик по `Instagram Downloader.command`  
или:

```bash
python3 Instagram\ Downloader.command
# или напрямую:
python3 instagram_downloader.py
```

Для Instagram часто нужны cookies из браузера (Chrome / Safari и т.д.) — иначе часть постов молча откажется отдавать медиа.

### 3. Telegram-бот

1. Создай бота у [@BotFather](https://t.me/BotFather), скопируй токен.
2. Скопируй пример конфига:

```bash
cp instagram_telegram_bot.env.example instagram_telegram_bot.env
```

3. Заполни `instagram_telegram_bot.env`:

```env
TELEGRAM_BOT_TOKEN=123456:ABC
TELEGRAM_AUTH_QUESTION=Как зовут автора бота?
TELEGRAM_AUTH_ANSWER=xotabeach
TELEGRAM_SEND_PREVIEW=true
TELEGRAM_SEND_DOCUMENT=true
# Если Instagram требует логин — только файл cookies, без чтения браузера:
# INSTAGRAM_COOKIES_FILE=/path/to/instagram_cookies.txt
```

4. Запуск:

- двойной клик по `Instagram Telegram Bot.command`, или  
- `python3 instagram_telegram_bot.py` (с подхватом env через `.command` / `source`)

Напиши боту `/start` → ответь на секретный вопрос → кидай ссылки.

---

## Как бот отдаёт медиа

**Видео**

1. Превью в ленте (удобно смотреть)
2. Тот же ролик **документом** — без повторного сжатия Telegram
3. Случайный анекдот категории Б

**Фото** — просто фото, без лишних обложек и постеров.

**Карусель** — каждый элемент своим типом: где видео — видео, где фото — фото.

Лимит Bot API ≈ **50 MB** на файл. Больше — бот честно скажет, что не пролезет.

---

## Конфиг бота

| Переменная | Зачем |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен от BotFather |
| `INSTAGRAM_COOKIES_FROM_BROWSER` | лучше **не использовать** на macOS (Keychain-попапы) |
| `INSTAGRAM_COOKIES_FILE` | безопасный вариант: путь к `cookies.txt` |
| `TELEGRAM_AUTH_QUESTION` | вопрос при первом входе |
| `TELEGRAM_AUTH_ANSWER` | ответ (регистр и `@` не важны) |
| `TELEGRAM_SEND_PREVIEW` | слать превью видео/фото |
| `TELEGRAM_SEND_DOCUMENT` | слать оригинал видео файлом |

Авторизованные пользователи пишутся в локальный `authorized_users.json` (в git не попадает).  
Список анекдотов — `category_b_jokes.json`.

---

## Структура

```
instagram_core.py              # общее скачивание (IG + TikTok)
instagram_downloader.py        # GUI
instagram_telegram_bot.py      # Telegram-бот
category_b_jokes.json          # анекдоты категории Б
instagram_telegram_bot.env.example
Instagram Downloader.command
Instagram Telegram Bot.command
```

---

## Cookies и macOS

По умолчанию бот **не** читает cookies из браузера — иначе macOS на каждое скачивание спрашивает доступ к Keychain / диску.

Если Instagram не отдаёт пост без логина:

1. Экспортируй `cookies.txt` расширением вроде «Get cookies.txt LOCALLY»
2. Укажи путь в `INSTAGRAM_COOKIES_FILE`
3. Не включай `INSTAGRAM_COOKIES_FROM_BROWSER`

TikTok обычно **cookies не требует**.

---

## Важно

- Скачивай только то, на что у тебя есть право. Авторы и площадки имеют свои правила.
- Это pet-проект на `yt-dlp` / `gallery-dl`: сайты меняются, иногда что-то ломается — обновляй зависимости.
- Не коммить `.env` и cookies. В `.gitignore` они уже закрыты.

---

## Лицензия / вайб

Сделано, чтобы ссылка из чата превращалась в файл, а не в квест.  
Если после ролика прилетел анекдот категории Б — значит, всё работает как задумано.
