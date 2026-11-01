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
    AUTOSKIP           auto-skip hated tracks (default on; AUTOSKIP=0 disables)
    THEME              UI theme file from web/themes, e.g. `tokio-night` or `tokio-night.css`
                       (default empty = adaptive dark/light following the OS scheme)
    LISTENBRAINZ_USER_AGENT  contact User-Agent for the ListenBrainz API
    LISTENBRAINZ_TOKEN  ListenBrainz user token; enables listen submission
    LISTENBRAINZ_SUBMIT  set 0 to disable submission even with a token
    LISTENBRAINZ_MIN_PLAY  seconds a track must play to count as a listen
                           (default 30)
"""

import hashlib
import os
import platform
import shutil
from contextlib import suppress
from pathlib import Path

from dotenv import load_dotenv

# Real process env (captured before dotenv loads the file) always wins over the
# `.env` file; after startup the file values live in os.environ too, so the
# pre-load snapshot is what keeps the real env authoritative on re-reads.
_REAL_ENV_KEYS: frozenset[str] = frozenset(os.environ)
ENV_PATH = Path(__file__).resolve().parent.parent.parent / '.env'

# Load .env from the repo root (independent of the caller's cwd).
# dotenv does NOT override vars already in the environment - real env wins.
load_dotenv(ENV_PATH)


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


def _flag(name: str, default: bool = False) -> bool:
    """Parse an on/off environment flag; `default` when the var is unset."""
    if not (value := get_env(name, '').strip().lower()):
        return default
    return value in ('1', 'true', 'yes', 'on')


def _seconds(name: str, default: float) -> float:
    """Parse a duration in seconds from the environment; `default` on bad values."""
    try:
        return float(get_env(name, str(default)))
    except ValueError:
        return default


def _resolve(name: str, file_values: dict[str, str], default: str) -> str:
    """Real env wins; else the `.env` file; else `default` (mirrors the startup parse)."""
    if name in _REAL_ENV_KEYS:
        return os.environ[name]
    return file_values.get(name) or default


def _on(raw: object, default: bool = False) -> bool:
    """Parse an on/off value (`1`/`true`/`yes`/`on`); `default` when unset/empty."""
    if not (text := str(raw or '').strip().lower()):
        return default
    return text in ('1', 'true', 'yes', 'on')


def _file_env(path: Path = ENV_PATH) -> dict[str, str]:
    """Parse simple `KEY=value` lines from the project-root `.env` file.

    `utf-8-sig` tolerates a BOM (Windows editors like to add one), and an
    unreadable file degrades to "no overrides" instead of breaking a reload.
    """
    values: dict[str, str] = {}
    try:
        if not path.is_file():
            return values
        text = path.read_text(encoding='utf-8-sig')
    except (OSError, UnicodeDecodeError):
        return values
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        if key.startswith('export '):
            key = key[len('export ') :].strip()
        values[key] = value.strip()
    return values


def quick_settings_from_disk() -> dict[str, bool]:
    """Current quick-setting flag values, re-read from the `.env` file."""
    file_values = _file_env()
    return {
        'AUTOSKIP': _on(
            _resolve('AUTOSKIP', file_values, _resolve('SKIP_FOREVER', file_values, '1')),
            True,
        ),
        'LISTENBRAINZ_SUBMIT': bool(_resolve('LISTENBRAINZ_TOKEN', file_values, '').strip())
        and _on(_resolve('LISTENBRAINZ_SUBMIT', file_values, '1')),
    }


def reload_quick_settings() -> None:
    """Re-apply the quick-setting flags from the latest `.env` read (module state)."""
    global AUTOSKIP, LISTENBRAINZ_SUBMIT
    flags = quick_settings_from_disk()
    AUTOSKIP = flags['AUTOSKIP']
    LISTENBRAINZ_SUBMIT = flags['LISTENBRAINZ_SUBMIT']


HOST = get_env('NOWPLAYING_HOST', '0.0.0.0')
# Auto-skip hated tracks (thumbs down) the next time they play.
# On by default; an old .env's SKIP_FOREVER still controls it when set.
AUTOSKIP = _flag('AUTOSKIP', default=_flag('SKIP_FOREVER', default=True))
THEME = get_env('THEME', '').strip().lower().removesuffix('.css')
DEBUG = get_env('NOWPLAYING_DEBUG', '').strip().lower() in ('1', 'true', 'yes', 'on')
PORT = _port()
PLAYERCTL_PLAYER = get_env('PLAYERCTL_PLAYER')
YT_DLP = get_env('YT_DLP', '').strip().lower() in ('1', 'true', 'yes', 'on')
FANART_PROJECT = 'b3ef17e6708a180bc9830f9b6e67d43f'
FANART_API_KEY = get_env('FANART_API_KEY')
# ListenBrainz asks for a descriptive UA that can be traced back to you.
LISTENBRAINZ_USER_AGENT = get_env(
    'LISTENBRAINZ_USER_AGENT',
    'nowplaying/1.0.0 (https://github.com/debjan/nowplaying)',
)
# Optional listen submission: a user token (listenbrainz.org/settings) turns it
# on; an explicit 0 disables it again for users who only want the token-free
# similarity dialogs (or a dev machine that shares .env with the Pi).
LISTENBRAINZ_TOKEN = get_env('LISTENBRAINZ_TOKEN').strip()
LISTENBRAINZ_SUBMIT = bool(LISTENBRAINZ_TOKEN) and (
    get_env('LISTENBRAINZ_SUBMIT', '1').strip().lower() in ('1', 'true', 'yes', 'on')
)
# Played time a track needs before it counts as a listen (ListenBrainz asks for
# "half the track or 4 minutes, whichever is lower"; the user asked for 30s).
LISTENBRAINZ_MIN_PLAY_MS = int(_seconds('LISTENBRAINZ_MIN_PLAY', 30.0) * 1000)

ensure_dirs()
