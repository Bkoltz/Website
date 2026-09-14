#!/usr/bin/env python3
"""
SQLite schema and persistence helpers for the Discord #help archive.

Owns the DDL, connection handling and the deterministic text dump that is the
committed source of truth. Both sync_discord.py and build_help_pages.py import
this module; neither touches sqlite3 directly.

The binary database (data/discord.sqlite3) is gitignored and rebuilt on demand
from the committed text dump (data/discord.sql). A binary file rewritten every
week defeats git delta compression; a sorted text dump diffs row by row.

Requirements: none (standard library only)
"""

import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import archive_crypto

ROOT_DIR = Path(__file__).parent.parent
DB_FILE = ROOT_DIR / "data" / "discord.sqlite3"


def sql_path_for(db_path: Path) -> Path:
    """
    The text dump that belongs to a database file: same directory and stem.

    Keeping the pair together means --db redirects the dump too, so a test or
    fixture run cannot overwrite the committed data/discord.sql.
    """
    return Path(db_path).with_suffix(".sql")


def enc_path_for(db_path: Path) -> Path:
    """
    The encrypted dump beside a database file: discord.sqlite3 -> discord.sql.enc.

    This is the file that gets committed; the plaintext .sql next to it is a
    local working copy and is gitignored. See scripts/archive_crypto.py.
    """
    return Path(db_path).with_suffix(".sql.enc")


DB_SQL = sql_path_for(DB_FILE)
DB_ENC = enc_path_for(DB_FILE)

SCHEMA_VERSION = 1

# Order matters: parents before children, so a restore never trips a foreign key.
DUMP_TABLES = [
    ("meta", "key"),
    ("users", "id"),
    ("tags", "id"),
    ("threads", "id"),
    ("slug_history", "slug"),
    ("thread_tags", "thread_id, tag_id"),
    ("media", "source_key"),
    ("messages", "id"),
    ("attachments", "id"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
  id                TEXT PRIMARY KEY,
  username          TEXT NOT NULL,
  global_name       TEXT,
  nick              TEXT,
  display_name      TEXT NOT NULL,
  is_bot            INTEGER NOT NULL DEFAULT 0,
  avatar_hash       TEXT,
  avatar_media_id   INTEGER,
  member_fetched_at TEXT,
  opted_out         INTEGER NOT NULL DEFAULT 0,
  first_seen_at     TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_optout ON users(opted_out);

CREATE TABLE IF NOT EXISTS tags (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  slug       TEXT NOT NULL,
  emoji      TEXT,
  moderated  INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tags_slug ON tags(slug);

CREATE TABLE IF NOT EXISTS threads (
  id                   TEXT PRIMARY KEY,
  parent_id            TEXT NOT NULL,
  guild_id             TEXT NOT NULL,
  title                TEXT NOT NULL,
  slug                 TEXT NOT NULL,
  owner_id             TEXT,
  created_at           TEXT NOT NULL,
  last_activity_at     TEXT,
  last_message_id      TEXT,
  message_count        INTEGER NOT NULL DEFAULT 0,
  total_message_sent   INTEGER NOT NULL DEFAULT 0,
  stored_message_count INTEGER NOT NULL DEFAULT 0,
  reply_count          INTEGER NOT NULL DEFAULT 0,
  archived             INTEGER NOT NULL DEFAULT 0,
  locked               INTEGER NOT NULL DEFAULT 0,
  starter_message_id   TEXT,
  accepted_message_id  TEXT,
  excluded             INTEGER NOT NULL DEFAULT 0,
  exclude_reason       TEXT,
  publishable          INTEGER NOT NULL DEFAULT 0,
  quality_score        INTEGER NOT NULL DEFAULT 0,
  deleted_at           TEXT,
  first_seen_at        TEXT NOT NULL,
  last_synced_at       TEXT,
  high_water_msg_id    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_threads_slug ON threads(slug);
CREATE INDEX IF NOT EXISTS idx_threads_parent ON threads(parent_id);
CREATE INDEX IF NOT EXISTS idx_threads_pub
  ON threads(publishable, excluded, deleted_at, last_activity_at DESC);

CREATE TABLE IF NOT EXISTS slug_history (
  slug       TEXT PRIMARY KEY,
  thread_id  TEXT NOT NULL,
  is_current INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_slug_history_thread ON slug_history(thread_id);

CREATE TABLE IF NOT EXISTS thread_tags (
  thread_id TEXT NOT NULL,
  tag_id    TEXT NOT NULL,
  PRIMARY KEY (thread_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_thread_tags_tag ON thread_tags(tag_id);

CREATE TABLE IF NOT EXISTS media (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  kind           TEXT NOT NULL,
  source_key     TEXT NOT NULL,
  source_url     TEXT NOT NULL,
  content_sha256 TEXT,
  local_path     TEXT,
  mime           TEXT,
  width          INTEGER,
  height         INTEGER,
  bytes          INTEGER,
  status         TEXT NOT NULL,
  error          TEXT,
  fetched_at     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_media_key ON media(source_key);
CREATE INDEX IF NOT EXISTS idx_media_sha    ON media(content_sha256);
CREATE INDEX IF NOT EXISTS idx_media_status ON media(status);

CREATE TABLE IF NOT EXISTS messages (
  id             TEXT PRIMARY KEY,
  thread_id      TEXT NOT NULL,
  author_id      TEXT,
  seq            INTEGER NOT NULL,
  type           INTEGER NOT NULL,
  content        TEXT NOT NULL DEFAULT '',
  created_at     TEXT NOT NULL,
  edited_at      TEXT,
  reply_to_id    TEXT,
  pinned         INTEGER NOT NULL DEFAULT 0,
  reaction_total INTEGER NOT NULL DEFAULT 0,
  reactions_json TEXT,
  embeds_json    TEXT,
  is_bot         INTEGER NOT NULL DEFAULT 0,
  redacted       INTEGER NOT NULL DEFAULT 0,
  deleted_at     TEXT,
  first_seen_at  TEXT NOT NULL,
  last_seen_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_thread_seq ON messages(thread_id, seq);
CREATE INDEX IF NOT EXISTS idx_messages_author     ON messages(author_id);
CREATE INDEX IF NOT EXISTS idx_messages_live       ON messages(thread_id, deleted_at);

CREATE TABLE IF NOT EXISTS attachments (
  id           TEXT PRIMARY KEY,
  message_id   TEXT NOT NULL,
  position     INTEGER NOT NULL DEFAULT 0,
  filename     TEXT NOT NULL,
  content_type TEXT,
  size         INTEGER,
  width        INTEGER,
  height       INTEGER,
  is_image     INTEGER NOT NULL DEFAULT 0,
  discord_url  TEXT NOT NULL,
  media_id     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_attachments_message ON attachments(message_id);

CREATE TABLE IF NOT EXISTS sync_runs (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at        TEXT NOT NULL,
  finished_at       TEXT,
  mode              TEXT NOT NULL,
  threads_seen      INTEGER DEFAULT 0,
  threads_updated   INTEGER DEFAULT 0,
  messages_upserted INTEGER DEFAULT 0,
  messages_deleted  INTEGER DEFAULT 0,
  media_downloaded  INTEGER DEFAULT 0,
  api_requests      INTEGER DEFAULT 0,
  rate_limit_waits  INTEGER DEFAULT 0,
  status            TEXT,
  error             TEXT
);
"""


def connect(path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the archive database with the right pragmas."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    # DELETE rather than WAL: WAL leaves -wal/-shm side files that would need
    # gitignoring and can outlive an interrupted run.
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Create missing tables and record the schema version."""
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    """Read a value from the meta key/value table."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Write a value into the meta key/value table."""
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def _sql_literal(value: object) -> str:
    """Render a Python value as a SQLite literal."""
    if value is None:
        return "NULL"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, bytes):
        return "X'" + value.hex() + "'"
    return "'" + str(value).replace("'", "''") + "'"


def dump_sql(conn: sqlite3.Connection, dest: Path) -> None:
    """
    Write a deterministic text dump of the archive.

    Rows are ordered by primary key rather than rowid, so the output is stable
    across VACUUM and produces reviewable line-level diffs in git. sync_runs is
    deliberately excluded: it is local run telemetry, not archive content.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "-- WebODM Discord #help archive.",
        "-- Generated by scripts/sync_discord.py. Do not edit by hand.",
        "-- Rebuild the binary database with: python scripts/help_db.py restore",
        "BEGIN TRANSACTION;",
    ]
    for table, order_by in DUMP_TABLES:
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
        if not cols:
            continue
        collist = ", ".join(cols)
        lines.append(f"-- {table}")
        for row in conn.execute(f"SELECT {collist} FROM {table} ORDER BY {order_by}"):
            values = ", ".join(_sql_literal(row[c]) for c in cols)
            lines.append(
                f"INSERT OR REPLACE INTO {table} ({collist}) VALUES ({values});"
            )
    lines.append("COMMIT;")
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")


def restore_sql(src: Path, dest_db: Path) -> sqlite3.Connection:
    """Rebuild the binary database from a text dump, replacing any existing file."""
    if dest_db.exists():
        dest_db.unlink()
    conn = connect(dest_db)
    migrate(conn)
    if src.exists():
        # Foreign keys are off during restore so table order can't break a load.
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.executescript(src.read_text(encoding="utf-8"))
        conn.execute("PRAGMA foreign_keys=ON")
        conn.commit()
    return conn


def seal_dump(sql_path: Path, enc_path: Path | None = None) -> Path | None:
    """
    Encrypt a plaintext dump alongside itself, when a key is configured.

    Returns the path written, or None when no key is set -- in which case the
    plaintext dump stands alone and the caller is running unencrypted.
    """
    key = archive_crypto.load_key(required=False)
    if key is None:
        return None
    enc_path = enc_path or Path(str(sql_path) + ".enc")
    archive_crypto.encrypt_file(sql_path, enc_path, key)
    return enc_path


def unseal_dump(enc_path: Path, sql_path: Path) -> bool:
    """
    Decrypt a committed archive into its plaintext working copy.

    Returns False when no key is configured, so a clone without the key builds
    an empty help section rather than failing outright.
    """
    key = archive_crypto.load_key(required=False)
    if key is None:
        return False
    archive_crypto.decrypt_file(enc_path, sql_path, key)
    return True


def ensure_db(db_path: Path = DB_FILE,
              sql_path: Path | None = None) -> sqlite3.Connection:
    """
    Return a ready connection, rebuilding from the committed dump when needed.

    The dump is authoritative: if it is newer than the binary (a fresh clone, or
    a git pull that brought in someone else's sync), the binary is regenerated.

    sql_path defaults to the dump beside db_path, so passing only a redirected
    db_path never reads or writes the committed archive.
    """
    sql_path = sql_path_for(db_path) if sql_path is None else sql_path

    # The encrypted file is what git carries, so it is the one that can be
    # newer than the local plaintext after a pull. Decrypt before comparing.
    enc_path = enc_path_for(db_path)
    if enc_path.exists() and (
        not sql_path.exists() or enc_path.stat().st_mtime > sql_path.stat().st_mtime
    ):
        unseal_dump(enc_path, sql_path)

    if sql_path.exists() and (
        not db_path.exists() or sql_path.stat().st_mtime > db_path.stat().st_mtime
    ):
        return restore_sql(sql_path, db_path)
    conn = connect(db_path)
    migrate(conn)
    return conn


def slugify(text: str, max_length: int = 80) -> str:
    """Lowercase ASCII slug, truncated on a word boundary. May return ''."""
    import unicodedata

    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"\[(solved|closed|help|question|answered)\]", " ", s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    if len(s) <= max_length:
        return s
    cut = s[:max_length]
    if "-" in cut:
        cut = cut[: cut.rindex("-")]
    return cut.strip("-")


def main() -> None:
    """CLI: `dump` writes data/discord.sql, `restore` rebuilds the binary."""
    import sys

    action = sys.argv[1] if len(sys.argv) > 1 else "help"
    db_file = Path(sys.argv[2]) if len(sys.argv) > 2 else DB_FILE
    sql_file = sql_path_for(db_file)
    if action == "dump":
        conn = connect(db_file)
        migrate(conn)
        dump_sql(conn, sql_file)
        print(f"Wrote {sql_file}")
        sealed = seal_dump(sql_file, enc_path_for(db_file))
        if sealed:
            print(f"Wrote {sealed}")
    elif action == "restore":
        restore_sql(sql_file, db_file)
        print(f"Rebuilt {db_file} from {sql_file.name}")
    else:
        print(__doc__)
        print("Usage: python scripts/help_db.py [dump|restore] [db_path]")


if __name__ == "__main__":
    main()
