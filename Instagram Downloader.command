#!/bin/zsh
cd "$(dirname "$0")"

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"

if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi

echo "Python: $PYTHON"
"$PYTHON" -c "import yt_dlp" 2>/dev/null || {
  echo "Устанавливаю зависимости..."
  "$PYTHON" -m pip install -U yt-dlp imageio-ffmpeg gallery-dl
}

exec "$PYTHON" "$(dirname "$0")/instagram_downloader.py"
