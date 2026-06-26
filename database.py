import sqlite3
from contextlib import contextmanager
from config import DB_PATH


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS image_groups (
                id          TEXT PRIMARY KEY,
                short_code  TEXT UNIQUE NOT NULL,
                created_at  DATETIME NOT NULL DEFAULT (datetime('now')),
                expires_at  DATETIME NOT NULL,
                image_count INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS images (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id      TEXT NOT NULL REFERENCES image_groups(id) ON DELETE CASCADE,
                filename      TEXT NOT NULL,
                original_name TEXT NOT NULL,
                mime_type     TEXT NOT NULL,
                file_size     INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_sessions (
                session_id  TEXT PRIMARY KEY,
                group_id    TEXT REFERENCES image_groups(id) ON DELETE SET NULL,
                last_upload DATETIME
            );

            CREATE INDEX IF NOT EXISTS idx_groups_code       ON image_groups(short_code);
            CREATE INDEX IF NOT EXISTS idx_groups_expires    ON image_groups(expires_at);
            CREATE INDEX IF NOT EXISTS idx_images_group      ON images(group_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_group    ON user_sessions(group_id);
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
