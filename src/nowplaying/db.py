"""SQLite persistence: the single source of truth for track state."""

import asyncio
import sqlite3
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import TypeVar

import aiosqlite

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS artists (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  title      TEXT UNIQUE NOT NULL,
  mbid       TEXT,
  biography  TEXT
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
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  title       TEXT,
  album_id    INTEGER REFERENCES albums(id),
  artist_id   INTEGER NOT NULL REFERENCES artists(id),
  duration    INTEGER,
  mbid        TEXT
);

-- The love/hate verdict: score 1 = loved, -1 = hated, 0 = withdrawn. It is the
-- only verdict store - the thumbs render from it and the auto-skip reads it.
CREATE TABLE IF NOT EXISTS feedback (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  track_id INTEGER NOT NULL REFERENCES tracks(id),
  score    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS history (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp    DATETIME DEFAULT (datetime('now', 'localtime')),
  track_id     INTEGER NOT NULL REFERENCES tracks(id),
  auto_skipped INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS playback (
  id          INTEGER PRIMARY KEY CHECK (id = 1),
  position_ms INTEGER NOT NULL,
  updated_at  DATETIME DEFAULT (datetime('now', 'localtime'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_track_id ON feedback(track_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_albums_title_artist
  ON albums(title, artist_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tracks_title_album_artist
  ON tracks(title, album_id, artist_id);

CREATE INDEX IF NOT EXISTS idx_history_track_id ON history(track_id);
"""

# This is the single place a schema evolution is expressed - there is no
# separate versioned migration list.
_EXTRA_COLUMNS: dict[str, dict[str, str]] = {
    'tracks': {'mbid': 'TEXT'},
    'artists': {'mbid': 'TEXT', 'biography': 'TEXT'},
}

# Columns older builds wrote and nothing reads any more (`tracks.description`).
# SQLite only gained `ALTER TABLE ... DROP COLUMN` in 3.35; on an older runtime
# the leftover column is harmless (nullable, defaulted), so a failure is
# ignored.
_LEGACY_COLUMNS: dict[str, tuple[str, ...]] = {
    'tracks': ('description',),
}

# Tables a build before the single-`feedback`-table change created. Their rows
# are a mirror of the `feedback` verdict, so nothing is lost by dropping them.
_LEGACY_TABLES: tuple[str, ...] = ('favourites', 'skips')


def connect(path: str) -> sqlite3.Connection:
    """Open a WAL-mode connection with FK enforcement; creates the schema."""

    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    _prepare(conn)
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Idempotently ALTER in missing columns and drop retired columns/tables."""

    for table, columns in _EXTRA_COLUMNS.items():
        existing = {
            row[1] for row in conn.execute(f'PRAGMA table_info({table})')
        }
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}')

    for table, columns in _LEGACY_COLUMNS.items():
        existing = {
            row[1] for row in conn.execute(f'PRAGMA table_info({table})')
        }
        for name in columns:
            if name in existing:
                with suppress(sqlite3.OperationalError):
                    conn.execute(f'ALTER TABLE {table} DROP COLUMN {name}')

    for table in _LEGACY_TABLES:
        conn.execute(f'DROP TABLE IF EXISTS {table}')

    _localize_history(conn)


async def _ensure_columns_async(conn: aiosqlite.Connection) -> None:
    """Async twin of `_ensure_columns` for the web server's connections."""

    for table, columns in _EXTRA_COLUMNS.items():
        cur = await conn.execute(f'PRAGMA table_info({table})')
        existing = {row[1] for row in await cur.fetchall()}
        for name, ddl in columns.items():
            if name not in existing:
                await conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}')

    for table, columns in _LEGACY_COLUMNS.items():
        cur = await conn.execute(f'PRAGMA table_info({table})')
        existing = {row[1] for row in await cur.fetchall()}
        for name in columns:
            if name in existing:
                with suppress(sqlite3.OperationalError):
                    await conn.execute(f'ALTER TABLE {table} DROP COLUMN {name}')

    for table in _LEGACY_TABLES:
        await conn.execute(f'DROP TABLE IF EXISTS {table}')

    await _localize_history_async(conn)


_HISTORY_LOCAL_VERSION = 1


def _local_now() -> str:
    """Current local wall-clock time in SQLite's `YYYY-MM-DD HH:MM:SS` format."""

    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _as_local(value: object) -> str:
    """Read a stored timestamp as UTC and return it in local wall-clock time."""

    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone().strftime('%Y-%m-%d %H:%M:%S')


def _localize_history(conn: sqlite3.Connection) -> None:
    """One-shot: rewrite `history.timestamp` values that were stored as UTC."""

    if conn.execute('PRAGMA user_version').fetchone()[0] >= _HISTORY_LOCAL_VERSION:
        return
    conn.execute('BEGIN IMMEDIATE')
    try:
        if conn.execute('PRAGMA user_version').fetchone()[0] >= _HISTORY_LOCAL_VERSION:
            conn.rollback()
            return
        rows = conn.execute(
            'SELECT id, timestamp FROM history WHERE timestamp IS NOT NULL'
        ).fetchall()
        conn.executemany(
            'UPDATE history SET timestamp = ? WHERE id = ?',
            [(_as_local(stamp), row_id) for row_id, stamp in rows],
        )
        conn.execute(f'PRAGMA user_version = {_HISTORY_LOCAL_VERSION}')
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


async def _localize_history_async(conn: aiosqlite.Connection) -> None:
    """Async twin of `_localize_history`."""

    cur = await conn.execute('PRAGMA user_version')
    row = await cur.fetchone()
    if row and row[0] >= _HISTORY_LOCAL_VERSION:
        return
    await conn.execute('BEGIN IMMEDIATE')
    try:
        cur = await conn.execute('PRAGMA user_version')
        row = await cur.fetchone()
        if row and row[0] >= _HISTORY_LOCAL_VERSION:
            await conn.rollback()
            return
        cur = await conn.execute(
            'SELECT id, timestamp FROM history WHERE timestamp IS NOT NULL'
        )
        rows = await cur.fetchall()
        await conn.executemany(
            'UPDATE history SET timestamp = ? WHERE id = ?',
            [(_as_local(stamp), row_id) for row_id, stamp in rows],
        )
        await conn.execute(f'PRAGMA user_version = {_HISTORY_LOCAL_VERSION}')
        await conn.commit()
    except BaseException:
        await conn.rollback()
        raise


def _prepare(conn: sqlite3.Connection, attempts: int = 15) -> None:
    """Apply the baseline schema and ensure all columns exist."""

    for attempt in range(attempts):
        try:
            conn.execute('PRAGMA foreign_keys = ON')
            conn.execute('PRAGMA journal_mode = WAL')
            conn.execute('PRAGMA synchronous = NORMAL')
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
            await conn.execute('PRAGMA synchronous = NORMAL')
            await conn.executescript(SCHEMA)
            await _ensure_columns_async(conn)
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
    mbid: str | None = None,
    artist_mbid: str | None = None,
    biography: str | None = None,
) -> int:
    """Upsert artist/album/track and return the stable `track_id` (no commit)."""

    artist = _normalize(artist)
    title = _normalize(title)

    conn.execute(
        """
        INSERT INTO artists(title, mbid, biography) VALUES (?, ?, ?)
        ON CONFLICT(title) DO UPDATE SET
            mbid      = COALESCE(excluded.mbid, artists.mbid),
            biography = COALESCE(excluded.biography, artists.biography)
        """,
        (artist, artist_mbid, biography),
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
                'UPDATE tracks SET duration = COALESCE(?, duration), '
                'mbid = COALESCE(?, mbid) WHERE id = ?',
                (duration, mbid, existing[0]),
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
                'UPDATE tracks SET album_id = ?, duration = COALESCE(?, duration), '
                'mbid = COALESCE(?, mbid) WHERE id = ?',
                (album_id, duration, mbid, row[0]),
            )
            return row[0]

        conn.execute(
            """
            INSERT INTO tracks(title, album_id, artist_id, duration, mbid)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(title, album_id, artist_id) DO UPDATE SET
                duration = COALESCE(excluded.duration, tracks.duration),
                mbid     = COALESCE(excluded.mbid, tracks.mbid)
            """,
            (title, album_id, artist_id, duration, mbid),
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
                'UPDATE tracks SET duration = COALESCE(?, duration), '
                'mbid = COALESCE(?, mbid) WHERE id = ?',
                (duration, mbid, row[0]),
            )
            return row[0]
        cur = conn.execute(
            'INSERT INTO tracks(title, album_id, artist_id, duration, mbid) '
            'VALUES (?, NULL, ?, ?, ?)',
            (title, artist_id, duration, mbid),
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
        # Keep a MusicBrainz id that only the placeholder managed to resolve.
        conn.execute(
            'UPDATE tracks SET mbid = COALESCE(mbid, '
            '(SELECT mbid FROM tracks WHERE id = ?)) WHERE id = ?',
            (placeholder_id, target_id),
        )
        # The verdict is a state, not a count: keep the target's score and only
        # adopt the placeholder's when the target has none.
        conn.execute(
            'INSERT INTO feedback(track_id, score) '
            'SELECT ?, score FROM feedback WHERE track_id = ? '
            'ON CONFLICT(track_id) DO NOTHING',
            (target_id, placeholder_id),
        )
        conn.execute('DELETE FROM feedback WHERE track_id = ?', (placeholder_id,))
        conn.execute(
            'UPDATE history SET track_id = ? WHERE track_id = ?',
            (target_id, placeholder_id),
        )
        conn.execute('DELETE FROM tracks WHERE id = ?', (placeholder_id,))


def stored_duration(conn: sqlite3.Connection, artist: str, title: str) -> int | None:
    """Return any previously stored duration for a track, or None."""

    row = conn.execute(
        """
        SELECT t.duration
        FROM tracks t JOIN artists a ON a.id = t.artist_id
        WHERE a.title = ? AND t.title = ? AND t.duration IS NOT NULL
        ORDER BY t.id DESC LIMIT 1
        """,
        (_normalize(artist), _normalize(title)),
    ).fetchone()
    return row[0] if row else None


def mbid_for(conn: sqlite3.Connection, artist: str, title: str) -> str | None:
    """Return any previously stored recording MBID for a track, or None."""

    row = conn.execute(
        """
        SELECT t.mbid
        FROM tracks t JOIN artists a ON a.id = t.artist_id
        WHERE a.title = ? AND t.title = ? AND t.mbid IS NOT NULL
        ORDER BY t.id DESC LIMIT 1
        """,
        (_normalize(artist), _normalize(title)),
    ).fetchone()
    return row[0] if row else None


def artist_mbid_for(conn: sqlite3.Connection, artist: str) -> str | None:
    """Return any previously stored MusicBrainz artist MBID for an artist."""

    row = conn.execute(
        'SELECT mbid FROM artists WHERE title = ? AND mbid IS NOT NULL',
        (_normalize(artist),),
    ).fetchone()
    return row[0] if row else None


def biography_for(conn: sqlite3.Connection, artist: str) -> str | None:
    """Return any previously stored artist biography, or `None`."""

    row = conn.execute(
        'SELECT biography FROM artists WHERE title = ? AND biography IS NOT NULL',
        (_normalize(artist),),
    ).fetchone()
    return row[0] if row else None


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


def latest_cover_key(conn: sqlite3.Connection) -> tuple[str | None, str | None, str | None]:
    """Return `(cover_url, artist, album)` of the latest non-auto-skipped history row.

    The monitor seeds its last-written cover identity from this so a restart
    doesn't re-trim the current cover, while an album change (even one sharing
    the same raw thumbnail URL) still triggers a fresh download.
    """

    row = conn.execute(
        """
        SELECT al.cover_url, ar.title, al.title
        FROM history h
        JOIN tracks t ON t.id = h.track_id
        JOIN artists ar ON ar.id = t.artist_id
        JOIN albums al ON al.id = t.album_id
        WHERE h.auto_skipped = 0 AND al.cover_url NOT LIKE '/%'
        ORDER BY h.id DESC
        LIMIT 1
        """,
    ).fetchone()
    return (row[0], row[1], row[2]) if row else (None, None, None)


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
    """Insert a history row and commit; returns the new `history.id`.

    The timestamp is written explicitly in local wall-clock time - the default
    of a pre-existing table is still SQLite's UTC `CURRENT_TIMESTAMP`.
    """

    cur = conn.execute(
        'INSERT INTO history(track_id, auto_skipped, timestamp) VALUES (?, ?, ?)',
        (track_id, int(auto_skipped), _local_now()),
    )
    row_id = cur.lastrowid
    assert row_id is not None
    conn.commit()
    return row_id


def feedback_score(conn: sqlite3.Connection, track_id: int) -> int:
    """Return the track's love/hate verdict (1, -1 or 0 when unrated)."""

    row = conn.execute(
        'SELECT score FROM feedback WHERE track_id = ?', (track_id,)
    ).fetchone()
    return int(row[0]) if row else 0


def set_feedback(conn: sqlite3.Connection, track_id: int, score: int) -> None:
    """Upsert the track's feedback verdict (1 = love, -1 = hate, 0 = clear)."""

    conn.execute(
        """
        INSERT INTO feedback(track_id, score) VALUES (?, ?)
        ON CONFLICT(track_id) DO UPDATE SET score = excluded.score
        """,
        (track_id, score),
    )
    conn.commit()


def set_position(conn: sqlite3.Connection, position_ms: int) -> bool:
    """Upsert the single playback-position row; return True if the value changed."""

    row = conn.execute('SELECT position_ms FROM playback WHERE id = 1').fetchone()
    if row is not None and int(row[0]) == position_ms:
        return False
    conn.execute(
        """
        INSERT INTO playback(id, position_ms, updated_at)
        VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            position_ms = excluded.position_ms,
            updated_at  = excluded.updated_at
        """,
        (position_ms, _local_now()),
    )
    conn.commit()
    return True


def set_duration(conn: sqlite3.Connection, track_id: int, duration_ms: int) -> None:
    """Refine a track's duration in place; no history row is recorded."""

    conn.execute(
        'UPDATE tracks SET duration = ? WHERE id = ?',
        (duration_ms, track_id),
    )
    conn.commit()


def latest_history_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute('SELECT MAX(id) FROM history').fetchone()
    return row[0] if row and row[0] is not None else None


def latest_history(conn: sqlite3.Connection) -> tuple[int, int] | None:
    """Return `(history id, track_id)` of the newest history row, or `None`."""

    row = conn.execute(
        'SELECT id, track_id FROM history ORDER BY id DESC LIMIT 1'
    ).fetchone()
    return (row[0], row[1]) if row else None


def current_position(conn: sqlite3.Connection) -> int:
    """Return the last stored playback position in ms (0 when unset)."""

    row = conn.execute('SELECT position_ms FROM playback WHERE id = 1').fetchone()
    return int(row[0]) if row is not None else 0

# --- Async layer (aiosqlite) for the web server ------------------------------

_conns: dict[str, aiosqlite.Connection] = {}
_conn_lock = asyncio.Lock()

_T = TypeVar('_T')

_CURRENT_SQL = """
SELECT a.title AS Artist, al.title AS Album, t.title AS Title,
       al.id AS AlbumId, al.cover_url AS CoverUrl, t.id AS TrackId,
       t.duration AS Duration,
       al.review AS Review, a.biography AS Biography,
       t.mbid AS Mbid, COALESCE(fb.score, 0) AS Feedback
FROM history h
JOIN tracks t ON t.id = h.track_id
LEFT JOIN albums al ON al.id = t.album_id
JOIN artists a ON a.id = t.artist_id
LEFT JOIN feedback fb ON fb.track_id = t.id
WHERE h.auto_skipped = 0
ORDER BY h.id DESC
LIMIT 1
"""

_TRACK_SQL = """
SELECT a.title AS Artist, al.title AS Album, t.title AS Title, t.mbid AS Mbid
FROM tracks t
LEFT JOIN albums al ON al.id = t.album_id
JOIN artists a ON a.id = t.artist_id
WHERE t.id = ?
"""

async def _adb(db_path: str) -> aiosqlite.Connection:
    """Return the shared long-lived connection for `db_path`, creating it once."""

    conn = _conns.get(db_path)
    if conn is not None:
        return conn
    async with _conn_lock:
        if db_path in _conns:
            return _conns[db_path]
        fresh = await aiosqlite.connect(db_path, timeout=10)
        try:
            await _prepare_async(fresh)
        except BaseException:
            with suppress(Exception):
                await fresh.close()
            raise
        _conns[db_path] = fresh
        return fresh


async def _invalidate(db_path: str) -> None:
    """Drop the cached connection so the next call reconnects."""

    async with _conn_lock:
        conn = _conns.pop(db_path, None)
    if conn is not None:
        with suppress(Exception):
            await conn.close()


async def _acall(
    db_path: str, op: Callable[[aiosqlite.Connection], Awaitable[_T]]
) -> _T:
    """Run `op(conn)` on the shared connection; reconnect after a failure."""

    try:
        return await op(await _adb(db_path))
    except (sqlite3.Error, OSError):
        await _invalidate(db_path)
        raise


async def _afetchone(db_path: str, sql: str, params: tuple = ()) -> tuple | None:
    async def op(conn: aiosqlite.Connection) -> tuple | None:
        cur = await conn.execute(sql, params)
        row = await cur.fetchone()
        return tuple(row) if row is not None else None

    return await _acall(db_path, op)


async def alatest_change_token(
    db_path: str,
) -> tuple[int | None, int | None, int]:
    """SSE change signal: `(latest history id, its track's duration, feedback)`.

    The duration is part of the token so a track whose duration is refined
    after it was first recorded (without a new history row) still pushes an
    update to the browser. The feedback score is part of it for the same
    reason: a love/hate vote writes no history row, but must re-render the
    thumbs on every connected client.
    """

    row = await _afetchone(
        db_path,
        """
        SELECT h.id, t.duration, COALESCE(fb.score, 0)
        FROM history h
        JOIN tracks t ON t.id = h.track_id
        LEFT JOIN feedback fb ON fb.track_id = t.id
        ORDER BY h.id DESC
        LIMIT 1
        """,
    )
    return (row[0], row[1], row[2]) if row else (None, None, 0)


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
        'Duration': track[6],
        'Review': track[7],
        'Biography': track[8],
        'MBID': track[9],
        'Feedback': track[10],
    }


async def aget_track(db_path: str, track_id: int) -> dict | None:
    """Return one track row (`Artist`/`Album`/`Title`/`MBID`), or None."""

    track = await _afetchone(db_path, _TRACK_SQL, (track_id,))
    if track is None:
        return None
    return {
        'Artist': track[0],
        'Album': track[1],
        'Title': track[2],
        'MBID': track[3],
    }


async def aget_position(db_path: str) -> tuple[int, str] | None:
    """Return the latest `(position_ms, updated_at)` playback row, or `None`."""

    row = await _afetchone(
        db_path, 'SELECT position_ms, updated_at FROM playback WHERE id = 1'
    )
    if row is None:
        return None
    return (int(row[0]), str(row[1]))


async def aset_feedback(db_path: str, track_id: int, score: int) -> None:
    """Store the track's love/hate verdict (1, -1 or 0 to clear it)."""

    async def op(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            """
            INSERT INTO feedback(track_id, score) VALUES (?, ?)
            ON CONFLICT(track_id) DO UPDATE SET score = excluded.score
            """,
            (track_id, score),
        )
        await conn.commit()

    await _acall(db_path, op)


async def aclose_all() -> None:
    """Close every cached connection; called at app shutdown."""

    async with _conn_lock:
        conns = list(_conns.values())
        _conns.clear()
    for conn in conns:
        with suppress(Exception):
            await conn.close()
