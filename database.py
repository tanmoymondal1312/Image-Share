import sqlite3
from contextlib import contextmanager
from config import DB_PATH


def init_db():
    with get_db() as conn:
        # Create tables (without indexes — indexes created after migration)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS image_groups (
                id          TEXT PRIMARY KEY,
                short_code  TEXT UNIQUE NOT NULL,
                created_at  DATETIME NOT NULL DEFAULT (datetime('now')),
                expires_at  DATETIME NOT NULL,
                image_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS images (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id      TEXT NOT NULL REFERENCES image_groups(id) ON DELETE CASCADE,
                filename      TEXT NOT NULL,
                original_name TEXT NOT NULL,
                mime_type     TEXT NOT NULL,
                file_size     INTEGER NOT NULL,
                uploaded_at   DATETIME NOT NULL DEFAULT (datetime('now')),
                expires_at    DATETIME NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_sessions (
                session_id TEXT PRIMARY KEY,
                last_seen  DATETIME NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS session_groups (
                session_id TEXT NOT NULL,
                group_id   TEXT NOT NULL REFERENCES image_groups(id) ON DELETE CASCADE,
                added_at   DATETIME NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (session_id, group_id)
            );
        """)

        # Migrate old schema (adds expires_at column if missing, etc.)
        _migrate(conn)

        # Create indexes after migration so columns are guaranteed to exist
        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_groups_code    ON image_groups(short_code);
            CREATE INDEX IF NOT EXISTS idx_groups_exp     ON image_groups(expires_at);
            CREATE INDEX IF NOT EXISTS idx_images_group   ON images(group_id);
            CREATE INDEX IF NOT EXISTS idx_images_exp     ON images(expires_at);
            CREATE INDEX IF NOT EXISTS idx_sg_session     ON session_groups(session_id);
        """)


def _migrate(conn):
    """Handle schema upgrades from earlier versions."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(images)").fetchall()}

    if "uploaded_at" not in cols:
        conn.execute("ALTER TABLE images ADD COLUMN uploaded_at DATETIME")
        conn.execute("""
            UPDATE images SET uploaded_at = COALESCE(
                (SELECT created_at FROM image_groups WHERE id = images.group_id),
                datetime('now')
            )
        """)

    if "expires_at" not in cols:
        conn.execute("ALTER TABLE images ADD COLUMN expires_at DATETIME")
        conn.execute("""
            UPDATE images SET expires_at = COALESCE(
                (SELECT ig.expires_at FROM image_groups ig WHERE ig.id = images.group_id),
                datetime('now', '+1 hour')
            )
        """)

    # Handle user_sessions schema upgrades
    us_cols = {r[1] for r in conn.execute("PRAGMA table_info(user_sessions)").fetchall()}

    # Add last_seen if table was created with old schema (had last_upload)
    if "last_seen" not in us_cols:
        conn.execute("ALTER TABLE user_sessions ADD COLUMN last_seen DATETIME")
        if "last_upload" in us_cols:
            conn.execute("UPDATE user_sessions SET last_seen = COALESCE(last_upload, datetime('now'))")
        else:
            conn.execute("UPDATE user_sessions SET last_seen = datetime('now')")

    # Migrate old group_id references → session_groups
    if "group_id" in us_cols:
        conn.execute("""
            INSERT OR IGNORE INTO session_groups (session_id, group_id, added_at)
            SELECT session_id, group_id, COALESCE(last_upload, datetime('now'))
            FROM user_sessions WHERE group_id IS NOT NULL
        """)


@contextmanager
def get_db():
    conn = sqlite3.connect(str(DB_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
