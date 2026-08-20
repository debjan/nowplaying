"""SQLite persistence: the single source of truth for track state."""

import asyncio
import sqlite3
import time

import aiosqlite

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS artists (
  id    INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS albums (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  title             TEXT,
  artist_id         INTEGER NOT NULL REFERENCES artists(id),
  cover_url         TEXT,
  release_group_id  TEXT,
  review            TEXT
);

CREATE TABLE IF NOT EXISTS tracks (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  title     TEXT,
  album_id  INTEGER REFERENCES albums(id),
  artist_id INTEGER NOT NULL REFERENCES artists(id),
  duration  INTEGER
);

CREATE TABLE IF NOT EXISTS favourites (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  track_id INTEGER NOT NULL REFERENCES tracks(id),
  count    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS skips (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  track_id INTEGER NOT NULL REFERENCES tracks(id),
  count    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS history (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp    DATETIME DEFAULT CURRENT_TIMESTAMP,
  track_id     INTEGER NOT NULL REFERENCES tracks(id),
  auto_skipped INTEGER DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_skips_track_id ON skips(track_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_favourites_track_id ON favourites(track_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_albums_title_artist
  ON albums(title, artist_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tracks_title_album_artist
  ON tracks(title, album_id, artist_id);
"""

# This is the single place a schema evolution is expressed — there is no
# separate versioned migration list.
_EXTRA_COLUMNS: dict[str, dict[str, str]] = {}


def connect(path: str) -> sqlite3.Connection:
    """Open a WAL-mode connection with FK enforcement; creates the schema."""

    conn = sqlite3.connect(path, timeout=10)
    _prepare(conn)
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Idempotently ALTER in any columns missing from pre-existing tables."""

    for table, columns in _EXTRA_COLUMNS.items():
        existing = {
            row[1] for row in conn.execute(f'PRAGMA table_info({table})')
        }
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}')


def _prepare(conn: sqlite3.Connection, attempts: int = 15) -> None:
    """Apply the baseline schema and ensure all columns exist."""

    for attempt in range(attempts):
        try:
            conn.execute('PRAGMA foreign_keys = ON')
            conn.execute('PRAGMA journal_mode = WAL')
            conn.executescript(SCHEMA)
            _ensure_columns(conn)
            return
        except sqlite3.OperationalError as exc:
            if 'locked' not in str(exc).lower() or attempt == attempts - 1:
                raise
            time.sleep(0.1 * (attempt + 1))


async def _prepare_async(
    conn: aiosqlite.Connection, attempts: int = 15
) -> None:
    """Async twin of `_prepare` for the web server's connections."""

    for attempt in range(attempts):
        try:
            await conn.execute('PRAGMA journal_mode = WAL')
            await conn.executescript(SCHEMA)
            return
        except sqlite3.OperationalError as exc:
            if 'locked' not in str(exc).lower() or attempt == attempts - 1:
                raise
            await asyncio.sleep(0.1 * (attempt + 1))


def _normalize(s: str) -> str:
    """Strip and collapse internal whitespace so near-identical names match."""

    return ' '.join(s.split())


def resolve_track(
    conn: sqlite3.Connection,
    artist: str,
    album: str | None,
    title: str,
    cover_url: str | None = None,
    duration: int | None = None,
    release_group_id: str | None = None,
    review: str | None = None,
) -> int:
    """Upsert artist/album/track and return the stable `track_id` (no commit)."""

    artist = _normalize(artist)
    title = _normalize(title)

    conn.execute(
        'INSERT INTO artists(title) VALUES (?) ON CONFLICT(title) DO NOTHING',
        (artist,),
    )
    artist_id = conn.execute(
        'SELECT id FROM artists WHERE title = ?', (artist,)
    ).fetchone()[0]

    if album:
        album = _normalize(album)
        conn.execute(
            """
            INSERT INTO albums(title, artist_id, cover_url, release_group_id, review)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(title, artist_id) DO UPDATE SET
                cover_url        = COALESCE(excluded.cover_url, albums.cover_url),
                release_group_id = COALESCE(excluded.release_group_id, albums.release_group_id),
                review           = COALESCE(excluded.review, albums.review)
            """,
            (album, artist_id, cover_url, release_group_id, review),
        )
        album_id = conn.execute(
            'SELECT id FROM albums WHERE title = ? AND artist_id = ?',
            (album, artist_id),
        ).fetchone()[0]

        existing = conn.execute(
            'SELECT id FROM tracks WHERE title = ? AND album_id = ? AND artist_id = ?',
            (title, album_id, artist_id),
        ).fetchone()
        if existing:
            conn.execute(
                'UPDATE tracks SET duration = ? WHERE id = ?',
                (duration, existing[0]),
            )
            _fold_placeholder(conn, title, artist_id, existing[0])
            return existing[0]

        row = conn.execute(
            'SELECT id FROM tracks WHERE title = ? AND artist_id = ? '
            'AND album_id IS NULL',
            (title, artist_id),
        ).fetchone()
        if row:
            conn.execute(
                'UPDATE tracks SET album_id = ?, duration = ? WHERE id = ?',
                (album_id, duration, row[0]),
            )
            return row[0]

        conn.execute(
            """
            INSERT INTO tracks(title, album_id, artist_id, duration)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(title, album_id, artist_id) DO UPDATE SET
                duration = excluded.duration
            """,
            (title, album_id, artist_id, duration),
        )
        track_id = conn.execute(
            'SELECT id FROM tracks WHERE title = ? AND album_id = ? AND artist_id = ?',
            (title, album_id, artist_id),
        ).fetchone()[0]
    else:
        # Unresolved: reuse or insert a track with a NULL album_id (no album row).
        row = conn.execute(
            'SELECT id FROM tracks WHERE title = ? AND artist_id = ? '
            'AND album_id IS NULL',
            (title, artist_id),
        ).fetchone()
        if row:
            conn.execute(
                'UPDATE tracks SET duration = ? WHERE id = ?',
                (duration, row[0]),
            )
            return row[0]
        cur = conn.execute(
            'INSERT INTO tracks(title, album_id, artist_id, duration) '
            'VALUES (?, NULL, ?, ?)',
            (title, artist_id, duration),
        )
        track_id = cur.lastrowid
    assert track_id is not None
    return track_id


def _fold_placeholder(
    conn: sqlite3.Connection, title: str, artist_id: int, target_id: int
) -> None:
    """Merge placeholder NULL-album tracks into an existing resolved track."""

    placeholders = [
        r[0]
        for r in conn.execute(
            'SELECT id FROM tracks WHERE title = ? AND artist_id = ? '
            'AND album_id IS NULL',
            (title, artist_id),
        )
    ]
    for placeholder_id in placeholders:
        for table in ('skips', 'favourites'):
            conn.execute(
                f'INSERT INTO {table}(track_id, count) '
                f'SELECT ?, count FROM {table} WHERE track_id = ? '
                f'ON CONFLICT(track_id) DO UPDATE SET count = count + excluded.count',
                (target_id, placeholder_id),
            )
            # Drop the placeholder's own row so it no longer holds a dangling FK.
            conn.execute(f'DELETE FROM {table} WHERE track_id = ?', (placeholder_id,))
        conn.execute(
            'UPDATE history SET track_id = ? WHERE track_id = ?',
            (target_id, placeholder_id),
        )
        conn.execute('DELETE FROM tracks WHERE id = ?', (placeholder_id,))


def release_for(
    conn: sqlite3.Connection, artist: str, album: str
) -> str | None:
    """Return a previously stored `release_group_id`, or `None`."""

    row = conn.execute(
        """
        SELECT al.release_group_id
        FROM albums al JOIN artists ar ON ar.id = al.artist_id
        WHERE ar.title = ? AND al.title = ?
        ORDER BY al.id DESC LIMIT 1
        """,
        (artist, album),
    ).fetchone()
    if not row or row[0] is None:
        return None
    return row[0]


def latest_cover_url(conn: sqlite3.Connection) -> str | None:
    """Return the raw cover URL of the latest non-auto-skipped history row."""

    row = conn.execute(
        """
        SELECT al.cover_url
        FROM history h
        JOIN tracks t ON t.id = h.track_id
        JOIN albums al ON al.id = t.album_id
        WHERE h.auto_skipped = 0 AND al.cover_url NOT LIKE '/%'
        ORDER BY h.id DESC
        LIMIT 1
        """,
    ).fetchone()
    return row[0] if row else None


def record_history(
    conn: sqlite3.Connection, track_id: int, auto_skipped: bool = False
) -> int:
    """Insert a history row and commit; returns the new `history.id`."""

    conn.execute(
        'INSERT INTO history(track_id, auto_skipped) VALUES (?, ?)',
        (track_id, int(auto_skipped)),
    )
    conn.commit()
    return conn.execute('SELECT last_insert_rowid()').fetchone()[0]


def skip_count(conn: sqlite3.Connection, track_id: int) -> int:
    row = conn.execute('SELECT count FROM skips WHERE track_id = ?', (track_id,)).fetchone()
    return row[0] if row else 0


def increment_skip(conn: sqlite3.Connection, track_id: int) -> None:
    conn.execute(
        """
        INSERT INTO skips(track_id, count) VALUES (?, 1)
        ON CONFLICT(track_id) DO UPDATE SET count = count + 1
        """,
        (track_id,),
    )
    conn.commit()


def increment_favourite(conn: sqlite3.Connection, track_id: int) -> None:
    conn.execute(
        """
        INSERT INTO favourites(track_id, count) VALUES (?, 1)
        ON CONFLICT(track_id) DO UPDATE SET count = count + 1
        """,
        (track_id,),
    )
    conn.commit()


def latest_history_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute('SELECT MAX(id) FROM history').fetchone()
    return row[0] if row and row[0] is not None else None

# --- Async layer (aiosqlite) for the web server ------------------------------

_ensured_paths: set[str] = set()
_ensure_lock = asyncio.Lock()

_CURRENT_SQL = """
SELECT a.title AS Artist, al.title AS Album, t.title AS Title,
       al.id AS AlbumId, al.cover_url AS CoverUrl, t.id AS TrackId,
       COALESCE(f.count, 0) AS FavouriteCount, t.duration AS Duration,
       al.review AS Review
FROM history h
JOIN tracks t ON t.id = h.track_id
LEFT JOIN albums al ON al.id = t.album_id
JOIN artists a ON a.id = t.artist_id
LEFT JOIN favourites f ON f.track_id = t.id
WHERE h.auto_skipped = 0
ORDER BY h.id DESC
LIMIT 1
"""

async def _ensure_schema(db_path: str) -> None:
    """Create/migrate the DB once per process so the web can run standalone."""

    if db_path in _ensured_paths:
        return
    async with _ensure_lock:
        if db_path in _ensured_paths:
            return
        async with aiosqlite.connect(db_path) as conn:
            await _prepare_async(conn)
        _ensured_paths.add(db_path)


async def _aioconnect(db_path: str) -> aiosqlite.Connection:
    await _ensure_schema(db_path)
    return aiosqlite.connect(db_path)


async def _afetchone(db_path: str, sql: str, params: tuple = ()) -> tuple | None:
    conn = await _aioconnect(db_path)
    async with conn:
        cur = await conn.execute(sql, params)
        row = await cur.fetchone()
        return tuple(row) if row is not None else None


async def alatest_history_id(db_path: str) -> int | None:
    row = await _afetchone(db_path, 'SELECT MAX(id) FROM history')
    return row[0] if row and row[0] is not None else None


async def aget_current(db_path: str) -> dict | None:
    track = await _afetchone(db_path, _CURRENT_SQL)
    if track is None:
        return None
    return {
        'Artist': track[0],
        'Album': track[1],
        'Title': track[2],
        'AlbumId': track[3],
        'CoverUrl': track[4],
        'TrackId': track[5],
        'FavouriteCount': track[6],
        'Duration': track[7],
        'Review': track[8],
    }


async def aincrement_skip(db_path: str, track_id: int) -> None:
    conn = await _aioconnect(db_path)
    async with conn:
        await conn.execute(
            """
            INSERT INTO skips(track_id, count) VALUES (?, 1)
            ON CONFLICT(track_id) DO UPDATE SET count = count + 1
            """,
            (track_id,),
        )
        await conn.commit()


async def aincrement_favourite(db_path: str, track_id: int) -> None:
    conn = await _aioconnect(db_path)
    async with conn:
        await conn.execute(
            """
            INSERT INTO favourites(track_id, count) VALUES (?, 1)
            ON CONFLICT(track_id) DO UPDATE SET count = count + 1
            """,
            (track_id,),
        )
        await conn.commit()
