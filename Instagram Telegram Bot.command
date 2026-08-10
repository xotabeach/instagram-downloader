#!/bin/zsh
cd "$(dirname "$0")"

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
ENV_FILE="$(dirname "$0")/instagram_telegram_bot.env"

if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi

if [[ -f "$ENV_FILE" ]]; then
  set -a
  source "$ENV_FILE"
  set +a
fi

if [[ -z "$TELEGRAM_BOT_TOKEN" ]]; then
  echo "Создай файл:"
  echo "  $ENV_FILE"
  echo
  echo "Содержимое:"
  echo 'TELEGRAM_BOT_TOKEN=123456:ABC'
  echo '# INSTAGRAM_COOKIES_FILE=/path/to/instagram_cookies.txt'
  echo 'TELEGRAM_AUTH_QUESTION=Как зовут автора бота?'
  echo 'TELEGRAM_AUTH_ANSWER=xotabeach'
  echo
  echo "Токен берётся у @BotFather."
  echo "Cookies из браузера лучше не включать — macOS будет спрашивать доступ к Keychain."
  read -k 1 "?Нажми любую клавишу..."
  exit 1
fi

echo "Python: $PYTHON"
"$PYTHON" -m pip install -q -U "python-telegram-bot>=21" yt-dlp imageio-ffmpeg gallery-dl
exec "$PYTHON" "$(dirname "$0")/instagram_telegram_bot.py"
