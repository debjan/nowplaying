"""Configuration: environment variables with sensible defaults.

Values are read in precedence order (highest wins):

  1. real process environment (systemd Environment=, shell exports)
  2. `.env` in the project root (python-dotenv; see .env.example)
  3. hardcoded defaults below

All knobs use the NOWPLAYING_ prefix, so systemd units and shells can
override them without touching code:

    NOWPLAYING_DB      SQLite database path (default ~/.local/share/nowplaying/nowplaying.db)
    NOWPLAYING_COVER   trimmed album-art file (default ~/.local/share/nowplaying/cover.png)
    NOWPLAYING_PORT    port for `python -m nowplaying.web` (default 4940)
    NOWPLAYING_DEBUG   log debug messages to DEBUG_LOG (default ~/.local/share/nowplaying/nowplaying.log)
    PLAYERCTL_PLAYER   MPRIS player name to control (default empty = playerctl focus)
    YT_DLP             allow the yt-dlp album-art fallback (1/true/yes/on)
    SKIP_FOREVER       auto-skip tracks skipped >= 3 times (1/true/yes/on)
    PREVIOUS_LOVE      show a heart for tracks Previous-ed >= 3 times while playing
"""

import hashlib
import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the repo root (independent of the caller's cwd).
# dotenv does NOT override vars already in the environment — real env wins.
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')


def get_env(name: str, default: str = '') -> str:
    """Return an env var, falling back to `default` when unset."""
    return os.environ.get(name, default)


def _expand(path: str) -> str:
    """Resolve `~` in a configured path so sqlite/PIL receive an absolute path."""
    return str(Path(path).expanduser())


def ensure_dirs() -> None:
    """Create parent directories for DB_PATH, COVER_FILE, and DEBUG_LOG."""
    for path in (DB_PATH, COVER_FILE, DEBUG_LOG):
        parent = Path(path).parent
        parent.mkdir(parents=True, exist_ok=True)


def cover_url(url: str) -> str:
    """Served URL for a cover thumbnail: COVER_URL with a version query param."""
    key = hashlib.sha1(url.encode('utf-8')).hexdigest()[:8]
    return f'{COVER_URL}?v={key}'


DB_PATH = get_env('NOWPLAYING_DB', '~/.local/share/nowplaying/nowplaying.db')
COVER_FILE = get_env('NOWPLAYING_COVER', '~/.local/share/nowplaying/cover.png')
DEBUG_LOG = get_env('NOWPLAYING_DEBUG_LOG', '~/.local/share/nowplaying/nowplaying.log')
COVER_URL = '/cover.png'  # path the web server serves the trimmed cover at

DB_PATH = _expand(DB_PATH)
COVER_FILE = _expand(COVER_FILE)
DEBUG_LOG = _expand(DEBUG_LOG)

HOST = get_env('NOWPLAYING_HOST', '0.0.0.0')
SKIP_FOREVER = get_env('SKIP_FOREVER', '').strip().lower() in ('1', 'true', 'yes', 'on')
PREVIOUS_LOVE = get_env('PREVIOUS_LOVE', '').strip().lower() in ('1', 'true', 'yes', 'on')
DEBUG = get_env('NOWPLAYING_DEBUG', '').strip().lower() in ('1', 'true', 'yes', 'on')
PORT = int(get_env('NOWPLAYING_PORT', '4940'))
PLAYERCTL_PLAYER = get_env('PLAYERCTL_PLAYER')
YT_DLP = get_env('YT_DLP', '').strip().lower() in ('1', 'true', 'yes', 'on')
FANART_PROJECT = 'b3ef17e6708a180bc9830f9b6e67d43f'
FANART_API_KEY = get_env('FANART_API_KEY')

ensure_dirs()
