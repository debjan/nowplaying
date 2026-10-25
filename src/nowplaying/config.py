"""Configuration: environment variables with sensible defaults.

Values are read in precedence order (highest wins):

  1. real process environment (systemd Environment=, shell exports)
  2. `.env` in the project root (python-dotenv; see .env.example)
  3. hardcoded defaults below

All knobs use the NOWPLAYING_ prefix, so systemd units and shells can
override them without touching code. File defaults live under the platform
data dir (%LOCALAPPDATA%\\nowplaying on Windows, ~/.local/share/nowplaying
elsewhere); an existing file at the old ~/.local/share default is copied
over on first run so history isn't orphaned:

    NOWPLAYING_DB      SQLite database path (default <data-dir>/nowplaying.db)
    NOWPLAYING_COVER   trimmed album-art file (default <data-dir>/cover.png)
    NOWPLAYING_DEBUG_LOG  debug log file (default <data-dir>/nowplaying.log)
    NOWPLAYING_PORT    port for `nowplaying-web` / `python -m nowplaying.web` (default 4940)
    NOWPLAYING_DEBUG   log debug messages to DEBUG_LOG (off by default)
    PLAYERCTL_PLAYER   MPRIS player name to control (default empty = playerctl focus)
    YT_DLP             allow the yt-dlp album-art fallback (1/true/yes/on)
    SKIP_FOREVER       auto-skip tracks skipped >= 3 times (1/true/yes/on)
    PREVIOUS_LOVE      show a heart for tracks Previous-ed >= 3 times while playing
"""

import hashlib
import os
import platform
import shutil
from contextlib import suppress
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the repo root (independent of the caller's cwd).
# dotenv does NOT override vars already in the environment - real env wins.
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')


def _data_dir() -> str:
    """Platform data dir: %LOCALAPPDATA%/nowplaying on Windows, ~/.local/share/nowplaying elsewhere."""
    if platform.system() == 'Windows':
        if local := os.environ.get('LOCALAPPDATA'):
            return f'{local}/nowplaying'
        return '~/AppData/Local/nowplaying'
    return '~/.local/share/nowplaying'


_DATA_DIR = _data_dir()
_LEGACY_DIR = '~/.local/share/nowplaying'


def get_env(name: str, default: str = '') -> str:
    """Return an env var, falling back to `default` when unset."""
    return os.environ.get(name, default)


def _expand(path: str) -> str:
    """Resolve `~` in a configured path so sqlite/PIL receive an absolute path."""
    return str(Path(path).expanduser())


def _default_path(env: str, filename: str) -> str:
    """Env override, else the platform data-dir default.

    On first run with defaults, an existing file at the old
    ~/.local/share default is copied to the new location so
    history/cover art isn't orphaned. Explicitly configured paths are
    returned untouched. Failures are swallowed: worst case the app
    starts with a fresh file.
    """

    if env in os.environ:
        return _expand(os.environ[env])
    new = _expand(f'{_DATA_DIR}/{filename}')
    legacy = _expand(f'{_LEGACY_DIR}/{filename}')
    if legacy != new and not os.path.exists(new) and os.path.exists(legacy):
        with suppress(OSError):
            Path(new).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy, new)
    return f'{_DATA_DIR}/{filename}'


def ensure_dirs() -> None:
    """Create parent directories for DB_PATH, COVER_FILE, and DEBUG_LOG."""
    for path in (DB_PATH, COVER_FILE, DEBUG_LOG):
        parent = Path(path).parent
        parent.mkdir(parents=True, exist_ok=True)


def cover_url(url: str, extra: str = '') -> str:
    """Served URL for a cover thumbnail: COVER_URL with a version query param.

    `extra` (e.g. artist|album) is hashed into the version so two different
    albums sharing the same raw thumbnail URL still get distinct URLs -
    otherwise the browser's immutable cache would serve the first album's art
    for the second one.
    """
    key = hashlib.sha1(f'{extra}|{url}'.encode()).hexdigest()[:8]
    return f'{COVER_URL}?v={key}'


DB_PATH = _default_path('NOWPLAYING_DB', 'nowplaying.db')
COVER_FILE = _default_path('NOWPLAYING_COVER', 'cover.png')
DEBUG_LOG = _default_path('NOWPLAYING_DEBUG_LOG', 'nowplaying.log')
COVER_URL = '/cover.png'  # path the web server serves the trimmed cover at
SOURCE = get_env(
    'NOWPLAYING_SOURCE',
    'windows' if platform.system() == 'Windows' else 'dbus',
)

DB_PATH = _expand(DB_PATH)
COVER_FILE = _expand(COVER_FILE)
DEBUG_LOG = _expand(DEBUG_LOG)

def _port() -> int:
    """Parse NOWPLAYING_PORT; fall back to 4940 on bad values."""
    try:
        return int(get_env('NOWPLAYING_PORT', '4940'))
    except ValueError:
        return 4940


HOST = get_env('NOWPLAYING_HOST', '0.0.0.0')
SKIP_FOREVER = get_env('SKIP_FOREVER', '').strip().lower() in ('1', 'true', 'yes', 'on')
PREVIOUS_LOVE = get_env('PREVIOUS_LOVE', '').strip().lower() in ('1', 'true', 'yes', 'on')
DEBUG = get_env('NOWPLAYING_DEBUG', '').strip().lower() in ('1', 'true', 'yes', 'on')
PORT = _port()
PLAYERCTL_PLAYER = get_env('PLAYERCTL_PLAYER')
YT_DLP = get_env('YT_DLP', '').strip().lower() in ('1', 'true', 'yes', 'on')
FANART_PROJECT = 'b3ef17e6708a180bc9830f9b6e67d43f'
FANART_API_KEY = get_env('FANART_API_KEY')
GOOGLE_API_KEY = get_env('GOOGLE_API_KEY')

ensure_dirs()
