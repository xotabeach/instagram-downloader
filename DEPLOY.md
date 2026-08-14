# Деплой Telegram-бота на сервер

Бот крутится на том же VPS, что и CrimeaTrip test (`crimeatrip-test` в `~/.ssh/config`).

| Что | Значение |
|---|---|
| SSH host alias | `crimeatrip-test` |
| Каталог на сервере | `/opt/instagram-downloader` |
| Systemd unit | `instagram-telegram-bot.service` |
| Python | 3.12 через `uv` (venv в `.venv`) |
| Конфиг | `instagram_telegram_bot.env` (на сервере, в git не коммитить) |
| Cookies | `instagram_cookies.txt` (на сервере, в git не коммитить) |
| JS-рантайм для YouTube | `deno` в `/usr/local/bin` |

На сервере мало RAM (~480 MB) и там же Docker CrimeaTrip — не гоняй пачку тяжёлых видео подряд.

Бот сам ограничивает нагрузку:

- YouTube длиннее **20 минут** не качает
- одновременно один download/compress
- сжатие до 50 MB — только по кнопке, FFmpeg `veryfast` и 1 поток
- systemd: MemoryHigh 160 MB, MemoryMax 220 MB, 1 CPU

---

## Важно: один токен = один процесс

Telegram разрешает **одно** long-polling подключение на токен.  
Перед стартом на сервере **останови** локальный `Instagram Telegram Bot.command` / `python3 instagram_telegram_bot.py` на Mac.

---

## Первый раз (уже сделано на `crimeatrip-test`)

Если поднимаешь на **новом** хосте с нуля:

```bash
# 1) Скопировать код (без .venv)
rsync -avz \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '*.mp4' \
  ./ crimeatrip-test:/opt/instagram-downloader/

# 2) На сервере: uv + Python 3.12 + зависимости
ssh crimeatrip-test 'bash -s' <<'REMOTE'
set -e
export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

cd /opt/instagram-downloader
# путь cookies на сервере
sed -i 's|^INSTAGRAM_COOKIES_FILE=.*|INSTAGRAM_COOKIES_FILE=/opt/instagram-downloader/instagram_cookies.txt|' \
  instagram_telegram_bot.env

uv venv --python 3.12 .venv
uv pip install -r requirements.txt

cat >/etc/systemd/system/instagram-telegram-bot.service <<'EOF'
[Unit]
Description=Instagram/TikTok Telegram downloader bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/instagram-downloader
EnvironmentFile=/opt/instagram-downloader/instagram_telegram_bot.env
ExecStart=/opt/instagram-downloader/.venv/bin/python /opt/instagram-downloader/instagram_telegram_bot.py
Restart=on-failure
RestartSec=15
Environment=TMPDIR=/tmp

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now instagram-telegram-bot.service
systemctl status instagram-telegram-bot.service --no-pager
REMOTE
```

Системный Python на Ubuntu 20.04 — **3.8**; свежий `yt-dlp` его не ставит. Не пытайся ставить зависимости в system Python — только `uv` + 3.12.

---

## Обычное обновление кода

С Mac, из корня этого репозитория:

```bash
# 1) Залить файлы (env и cookies можно не трогать, если уже на сервере)
rsync -avz \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude 'instagram_telegram_bot.env' \
  --exclude 'instagram_cookies.txt' \
  --exclude 'authorized_users.json' \
  --exclude '*.mp4' \
  ./ crimeatrip-test:/opt/instagram-downloader/

# 2) Если менялся requirements.txt — переустановить пакеты
ssh crimeatrip-test 'export PATH="$HOME/.local/bin:$PATH"; cd /opt/instagram-downloader && uv pip install -r requirements.txt'

# 3) Перезапуск
ssh crimeatrip-test 'systemctl restart instagram-telegram-bot && systemctl status instagram-telegram-bot --no-pager'
```

Короткий вариант «только код, зависимости не трогали»:

```bash
rsync -avz \
  --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
  --exclude 'instagram_telegram_bot.env' --exclude 'instagram_cookies.txt' \
  --exclude 'authorized_users.json' \
  ./ crimeatrip-test:/opt/instagram-downloader/ \
&& ssh crimeatrip-test 'systemctl restart instagram-telegram-bot'
```

---

## Автодеплой через GitHub Actions

Каждый push в `main` запускает `.github/workflows/deploy.yml`. Workflow:

1. Копирует репозиторий во временный каталог пользователя `instagram-deploy`.
2. Запускает фиксированный root-owned скрипт `/usr/local/sbin/deploy-instagram-bot`.
3. Обновляет зависимости только при изменении `requirements.txt`.
4. Перезапускает и проверяет systemd-сервис.

Секреты репозитория:

- `DEPLOY_HOST`
- `DEPLOY_PORT`
- `DEPLOY_USER`
- `DEPLOY_SSH_PRIVATE_KEY`
- `DEPLOY_SSH_KNOWN_HOSTS`

Деплой сериализован через `concurrency`, поэтому два обновления одновременно не запустятся.
Файлы `instagram_telegram_bot.env`, `instagram_cookies.txt` и
`authorized_users.json` при обновлении не удаляются и не перезаписываются.

Для бота задан лимит памяти и CPU через systemd override. Это не останавливает
Docker-контейнеры и не перезапускает Docker Compose.

---

## JS-рантайм для YouTube (deno)

Свежий `yt-dlp` решает JS-челленджи YouTube через внешний рантайм. Без него
скачивание падает с `HTTP Error 403: Forbidden`, а в логе видно
`No supported JavaScript runtime could be found`.

Нужны две вещи:

1. `deno` в `PATH` (ставим в `/usr/local/bin`)
2. пакет EJS-скриптов — он приходит из `yt-dlp[default]` в `requirements.txt`

Установка/обновление deno на сервере:

```bash
ssh crimeatrip-test 'bash -s' <<'REMOTE'
set -e
cd /tmp
curl -fsSL -o deno.zip https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip
unzip -oq deno.zip -d /usr/local/bin
chmod 755 /usr/local/bin/deno
rm -f deno.zip
install -d -m 755 /var/cache/deno
deno --version
REMOTE
```

Кэш deno задан в systemd override: `Environment=DENO_DIR=/var/cache/deno`.

Проверка, что yt-dlp видит рантайм:

```bash
ssh crimeatrip-test 'cd /opt/instagram-downloader && .venv/bin/python -m yt_dlp -v --simulate "https://youtu.be/UQ9KKH9YdJ8" 2>&1 | grep -i "JS runtimes"'
# ожидаем: [debug] JS runtimes: deno-2.9.5
```

---

## Обновить cookies Instagram

Cookies протухают. Экспортируй свежий `cookies.txt` локально, затем:

```bash
scp ./instagram_cookies.txt crimeatrip-test:/opt/instagram-downloader/instagram_cookies.txt
ssh crimeatrip-test 'systemctl restart instagram-telegram-bot'
```

В `.env` на сервере должно быть:

```env
INSTAGRAM_COOKIES_FILE=/opt/instagram-downloader/instagram_cookies.txt
```

---

## Обновить `.env` (токен, вопрос/ответ)

Отредактируй локально или на сервере:

```bash
ssh crimeatrip-test 'nano /opt/instagram-downloader/instagram_telegram_bot.env'
ssh crimeatrip-test 'systemctl restart instagram-telegram-bot'
```

Или залей файл целиком (осторожно: перезапишет серверный):

```bash
scp ./instagram_telegram_bot.env crimeatrip-test:/opt/instagram-downloader/
# поправь путь cookies на серверный, если в файле всё ещё macOS-путь:
ssh crimeatrip-test "sed -i 's|^INSTAGRAM_COOKIES_FILE=.*|INSTAGRAM_COOKIES_FILE=/opt/instagram-downloader/instagram_cookies.txt|' /opt/instagram-downloader/instagram_telegram_bot.env && systemctl restart instagram-telegram-bot"
```

---

## Логи и диагностика

```bash
# статус
ssh crimeatrip-test 'systemctl status instagram-telegram-bot --no-pager'

# хвост логов
ssh crimeatrip-test 'journalctl -u instagram-telegram-bot -n 50 --no-pager'

# live
ssh crimeatrip-test 'journalctl -u instagram-telegram-bot -f'

# стоп / старт
ssh crimeatrip-test 'systemctl stop instagram-telegram-bot'
ssh crimeatrip-test 'systemctl start instagram-telegram-bot'
```

Если бот молчит, а на Mac он ещё запущен — убей локальный процесс и `systemctl restart` на сервере.

Если Instagram фото/карусель не качаются — почти всегда протухшие cookies (см. выше).

---

## Что не коммитить

Уже в `.gitignore`:

- `instagram_telegram_bot.env`
- `*cookies*.txt`
- `authorized_users.json`

На сервер они лежат рядом с кодом, но в git их не пушим.
