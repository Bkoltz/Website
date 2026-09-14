#!/usr/bin/env python3
"""
Sync WebODM Discord #help threads into the local archive.

Crawls a Discord forum or text channel over the REST API, stores threads,
messages and users in SQLite, mirrors image attachments and avatars into
static/images/help/, and writes the committed text dump data/discord.sql.

Media is mirrored rather than hot-linked because Discord attachment URLs are
signed and expire after roughly 24 hours.

Requirements: pip install Pillow requests
Environment:  DISCORD_BOT_TOKEN (required unless --fixtures)
              DISCORD_GUILD_ID, DISCORD_HELP_CHANNEL_ID (optional overrides)
"""

import argparse
import hashlib
import io
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

try:
    import requests
    from PIL import Image, ImageOps
except ImportError:
    print("Error: Install dependencies first: pip install Pillow requests")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))
import help_db
from discord_markdown import plain_text

ROOT_DIR = Path(__file__).parent.parent
CONFIG_FILE = ROOT_DIR / "data" / "discord_config.json"
OPTOUT_FILE = ROOT_DIR / "data" / "discord_optout.json"
MEDIA_DIR = ROOT_DIR / "static" / "images" / "help"
ATTACH_DIR = MEDIA_DIR / "attachments"
AVATAR_DIR = MEDIA_DIR / "avatars"

API_BASE = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://webodm.org, 1.0)"
MIN_INTERVAL = 0.2          # ~5 req/s, far below the 50 req/s global ceiling
MAX_RETRIES = 5
REQUEST_TIMEOUT = 30
ERROR_BUDGET = 10           # 4xx responses tolerated before aborting

CHANNEL_TEXT = 0
CHANNEL_FORUM = 15
CHANNEL_MEDIA = 16
KEEP_MESSAGE_TYPES = {0, 19}   # DEFAULT, REPLY
DISCORD_EPOCH_MS = 1420070400000

MAX_SOURCE_BYTES = 25 * 1024 * 1024
MAX_IMAGE_EDGE = 1280
WEBP_QUALITY = 80
MAX_OUTPUT_BYTES = 220 * 1024
AVATAR_SIZE = 64
REFRESH_BATCH = 50          # attachment URLs re-signed per API call
MAX_IMAGES_PER_MESSAGE = 4
MAX_IMAGES_PER_THREAD = 12
MEMBER_REFRESH_DAYS = 90
ARCHIVED_GRACE_DAYS = 3
MEDIA_WARN_BYTES = 300 * 1024 * 1024

# Publication thresholds. Thin pages are a site-wide quality signal, not just
# individually weak pages, so the bar is deliberately high.
MIN_QUESTION_CHARS = 80
MIN_TOTAL_CHARS = 350
MIN_ANSWER_CHARS = 25
MASS_DELETE_RATIO = 0.20


class DiscordFatal(Exception):
    """An unrecoverable condition: bad token, missing permission, bad config."""


def now_iso() -> str:
    """Current UTC time as a sortable ISO8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def snowflake_to_iso(snowflake: str) -> str:
    """Derive a creation timestamp from a Discord snowflake id."""
    ms = (int(snowflake) >> 22) + DISCORD_EPOCH_MS
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm_ts(value: str | None) -> str | None:
    """Normalize a Discord ISO8601 timestamp to second precision with Z."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def strip_signature(url: str) -> str:
    """Drop the expiring ex/is/hm query parameters from a CDN URL."""
    return url.split("?", 1)[0]


class DiscordClient:
    """Rate-limit-aware REST client, with fixture replay for offline testing."""

    def __init__(self, token: str | None, fixtures: Path | None = None,
                 record: Path | None = None, verbose: bool = False):
        self.token = token
        self.fixtures = fixtures
        self.record = record
        self.verbose = verbose
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        if token:
            self.session.headers["Authorization"] = f"Bot {token}"
        self._last_request = 0.0
        self._buckets: dict[str, tuple[int, float]] = {}
        self._route_bucket: dict[str, str] = {}
        self.requests_made = 0
        self.rate_limit_waits = 0
        self.error_count = 0
        if record:
            record.mkdir(parents=True, exist_ok=True)

    def _fixture_path(self, base: Path, path: str, params: dict | None) -> Path:
        key = path + "?" + json.dumps(params or {}, sort_keys=True)
        return base / (hashlib.sha256(key.encode()).hexdigest()[:16] + ".json")

    def _throttle(self, route: str) -> None:
        """Respect both the client-side floor and the server's bucket state."""
        bucket = self._route_bucket.get(route)
        if bucket:
            remaining, reset_at = self._buckets.get(bucket, (1, 0.0))
            if remaining <= 0:
                delay = reset_at - time.time()
                if delay > 0:
                    if self.verbose:
                        print(f"    rate limit: sleeping {delay:.2f}s ({bucket})")
                    self.rate_limit_waits += 1
                    time.sleep(delay + 0.05)
        gap = time.time() - self._last_request
        if gap < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - gap)

    def request(self, path: str, params: dict | None = None) -> Any:
        """GET a Discord API path, returning decoded JSON (None on 404)."""
        if self.fixtures:
            fp = self._fixture_path(self.fixtures, path, params)
            if not fp.exists():
                return None
            return json.loads(fp.read_text())

        route = re.sub(r"\d{5,25}", "{id}", path)
        for attempt in range(MAX_RETRIES):
            self._throttle(route)
            self._last_request = time.time()
            self.requests_made += 1
            resp = self.session.get(
                API_BASE + path, params=params, timeout=REQUEST_TIMEOUT
            )

            bucket = resp.headers.get("X-RateLimit-Bucket")
            if bucket:
                self._route_bucket[route] = bucket
                try:
                    self._buckets[bucket] = (
                        int(resp.headers.get("X-RateLimit-Remaining", 1)),
                        time.time() + float(resp.headers.get("X-RateLimit-Reset-After", 0)),
                    )
                except ValueError:
                    pass

            if resp.status_code == 200:
                if self.record:
                    fp = self._fixture_path(self.record, path, params)
                    fp.write_text(json.dumps(resp.json(), indent=1))
                return resp.json()

            if resp.status_code == 404:
                return None

            self.error_count += 1
            if self.error_count > ERROR_BUDGET:
                raise DiscordFatal(
                    f"Too many error responses ({self.error_count}); aborting before "
                    "Discord rate-limits this IP at the Cloudflare layer."
                )

            if resp.status_code == 429:
                # Never retry blindly: 10k 4xx in 10 minutes gets the IP banned.
                body = resp.json() if resp.content else {}
                delay = float(body.get("retry_after", 1.0))
                if resp.headers.get("X-RateLimit-Scope") == "global":
                    delay += 1.0
                self.rate_limit_waits += 1
                print(f"  WARNING: rate limited, waiting {delay:.2f}s")
                time.sleep(delay)
                continue

            if resp.status_code == 401:
                raise DiscordFatal(
                    "401 Unauthorized: DISCORD_BOT_TOKEN is missing or invalid."
                )
            if resp.status_code == 403:
                raise DiscordFatal(
                    f"403 Forbidden on {path}. The bot lacks VIEW_CHANNEL or "
                    "READ_MESSAGE_HISTORY here. Re-invite it with "
                    "permissions=66560 and check the channel's permission overwrites."
                )
            if 500 <= resp.status_code < 600:
                delay = 0.5 * (2 ** attempt) + random.random()
                print(f"  WARNING: HTTP {resp.status_code}, retrying in {delay:.1f}s")
                time.sleep(delay)
                continue

            raise DiscordFatal(f"HTTP {resp.status_code} on {path}: {resp.text[:200]}")

        raise DiscordFatal(f"Gave up on {path} after {MAX_RETRIES} attempts")

    def get_channel(self, channel_id: str) -> dict | None:
        """Fetch a channel object."""
        return self.request(f"/channels/{channel_id}")

    def get_active_threads(self, guild_id: str, parent_id: str) -> list[dict]:
        """Active threads in the guild, filtered to one parent channel."""
        data = self.request(f"/guilds/{guild_id}/threads/active") or {}
        return [t for t in data.get("threads", []) if t.get("parent_id") == parent_id]

    def iter_archived_threads(self, channel_id: str) -> Iterator[dict]:
        """Page through public archived threads, newest archive first."""
        before = None
        while True:
            params = {"limit": 100}
            if before:
                params["before"] = before
            data = self.request(
                f"/channels/{channel_id}/threads/archived/public", params
            ) or {}
            threads = data.get("threads", [])
            if not threads:
                return
            for t in threads:
                yield t
            if not data.get("has_more"):
                return
            # This cursor is an ISO8601 archive timestamp, not a snowflake.
            before = threads[-1].get("thread_metadata", {}).get("archive_timestamp")
            if not before:
                return

    def iter_thread_messages(self, thread_id: str) -> Iterator[dict]:
        """Yield every message in a thread, walking backwards from newest."""
        before = None
        while True:
            params = {"limit": 100}
            if before:
                params["before"] = before
            batch = self.request(f"/channels/{thread_id}/messages", params)
            if batch is None:
                return
            if not batch:
                return
            for m in batch:
                yield m
            if len(batch) < 100:
                return
            # The array is newest-first regardless of cursor direction.
            before = batch[-1]["id"]

    def get_message(self, channel_id: str, message_id: str) -> dict | None:
        """Fetch one message by id."""
        return self.request(f"/channels/{channel_id}/messages/{message_id}")

    def get_guild_member(self, guild_id: str, user_id: str) -> dict | None:
        """Fetch a guild member, for the server nickname."""
        return self.request(f"/guilds/{guild_id}/members/{user_id}")

    def refresh_attachment_urls(self, urls: list[str]) -> dict[str, str]:
        """
        Exchange bare CDN attachment URLs for freshly signed ones.

        Discord signs attachment URLs with expiring ex/is/hm query parameters and
        serves a 404 without them. The signature lasts about 24 hours, so it is
        deliberately never persisted (see store_message); this re-signs the stored
        bare URLs at the moment they are needed.

        Returns a {bare url: signed url} map, omitting anything Discord declined.
        """
        if self.fixtures or not urls:
            return {}
        out: dict[str, str] = {}
        for start in range(0, len(urls), REFRESH_BATCH):
            chunk = urls[start:start + REFRESH_BATCH]
            for attempt in range(MAX_RETRIES):
                self._throttle("/attachments/refresh-urls")
                self._last_request = time.time()
                self.requests_made += 1
                resp = self.session.post(
                    API_BASE + "/attachments/refresh-urls",
                    json={"attachment_urls": chunk}, timeout=REQUEST_TIMEOUT,
                )
                if resp.status_code == 429:
                    body = resp.json() if resp.content else {}
                    delay = float(body.get("retry_after", 1.0))
                    self.rate_limit_waits += 1
                    print(f"  WARNING: rate limited, waiting {delay:.2f}s")
                    time.sleep(delay)
                    continue
                if resp.status_code != 200:
                    self.error_count += 1
                    print(f"  WARNING: could not re-sign {len(chunk)} attachment "
                          f"URLs: HTTP {resp.status_code}")
                    break
                for item in resp.json().get("refreshed_urls", []):
                    if item.get("original") and item.get("refreshed"):
                        out[item["original"]] = item["refreshed"]
                break
        return out

    def download(self, url: str) -> bytes | None:
        """Fetch binary content from the CDN (not an API route, so unthrottled)."""
        if self.fixtures:
            return None
        try:
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT, stream=True)
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as e:
            # Say whether the URL carried a signature: an unsigned CDN attachment
            # URL 404s, and that looks identical to a deleted file otherwise.
            signed = "signed" if "hm=" in url else "UNSIGNED"
            print(f"  WARNING: download failed [{signed}] "
                  f"{strip_signature(url)}: {e}")
            return None


# ── configuration and opt-out ───────────────────────────────────────────────


def load_config(args: argparse.Namespace) -> dict:
    """Resolve guild/channel ids from CLI flags, environment, then config file."""
    cfg = {}
    if CONFIG_FILE.exists():
        cfg = json.loads(CONFIG_FILE.read_text())
    guild = args.guild_id or os.environ.get("DISCORD_GUILD_ID") or cfg.get("guild_id")
    channel = (
        args.channel_id
        or os.environ.get("DISCORD_HELP_CHANNEL_ID")
        or cfg.get("help_channel_id")
    )
    if not channel:
        raise DiscordFatal(
            "No help channel id. Set it in data/discord_config.json, or pass "
            "--channel-id, or set DISCORD_HELP_CHANNEL_ID."
        )
    cfg["guild_id"] = str(guild) if guild else None
    cfg["help_channel_id"] = str(channel)
    return cfg


def load_optout() -> dict:
    """Read the hand-maintained opt-out list."""
    if not OPTOUT_FILE.exists():
        return {"usernames": [], "thread_ids": []}
    data = json.loads(OPTOUT_FILE.read_text())
    return {
        "usernames": {str(x).lower() for x in data.get("usernames", [])},
        "thread_ids": {str(x) for x in data.get("thread_ids", [])},
    }


# ── users ───────────────────────────────────────────────────────────────────


def upsert_user(conn, client: DiscordClient, author: dict, guild_id: str | None,
                optout: dict, fetch_member: bool = True) -> str:
    """
    Store or refresh a user, resolving their display name.

    The REST message object carries no `member` field (that is a Gateway-only
    property), so the server nickname needs a separate guild-member lookup,
    cached here and refreshed only every MEMBER_REFRESH_DAYS.
    """
    uid = str(author["id"])
    username = author.get("username") or "unknown"
    global_name = author.get("global_name")
    is_bot = 1 if author.get("bot") else 0
    opted = 1 if (username.lower() in optout["usernames"]
                  or (global_name or "").lower() in optout["usernames"]) else 0

    row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    nick = row["nick"] if row else None
    fetched_at = row["member_fetched_at"] if row else None

    stale = True
    if fetched_at:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(
                fetched_at.replace("Z", "+00:00")
            )
            stale = age > timedelta(days=MEMBER_REFRESH_DAYS)
        except ValueError:
            stale = True

    if fetch_member and guild_id and stale and not is_bot:
        member = client.get_guild_member(guild_id, uid)
        if member is not None:
            nick = member.get("nick")
            fetched_at = now_iso()

    display = nick or global_name or username

    if row:
        conn.execute(
            "UPDATE users SET username=?, global_name=?, nick=?, display_name=?, "
            "is_bot=?, avatar_hash=?, member_fetched_at=?, opted_out=?, updated_at=? "
            "WHERE id=?",
            (username, global_name, nick, display, is_bot, author.get("avatar"),
             fetched_at, opted, now_iso(), uid),
        )
    else:
        conn.execute(
            "INSERT INTO users (id, username, global_name, nick, display_name, is_bot, "
            "avatar_hash, member_fetched_at, opted_out, first_seen_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (uid, username, global_name, nick, display, is_bot, author.get("avatar"),
             fetched_at, opted, now_iso(), now_iso()),
        )
    return uid


# ── slugs ───────────────────────────────────────────────────────────────────


RESERVED_SLUGS = {
    "page", "topic", "index", "_index", "feed", "atom", "rss", "sitemap",
    "robots", "tag", "tags", "search", "all",
}


def assign_slug(conn, thread_id: str, title: str) -> str:
    """
    Return the thread's permanent slug, minting one on first sight.

    A slug is never recomputed: renaming a thread on Discord changes its title
    but must not change its URL.
    """
    row = conn.execute("SELECT slug FROM threads WHERE id = ?", (thread_id,)).fetchone()
    if row and row["slug"]:
        return row["slug"]

    base = help_db.slugify(title)
    if len(base) < 3 or base in RESERVED_SLUGS or base.startswith("page-"):
        # Emoji-only, CJK, empty or colliding-with-routing titles get a stable
        # opaque slug; the correct title still drives <h1> and <title>.
        suffix = _base36(int(thread_id))[-8:]
        base = f"thread-{suffix}" if len(base) < 3 else f"{base}-thread"

    slug, n = base, 1
    while True:
        clash = conn.execute(
            "SELECT id FROM threads WHERE slug = ? AND id != ?", (slug, thread_id)
        ).fetchone()
        if not clash:
            break
        n += 1
        slug = f"{base}-{n}"

    conn.execute(
        "INSERT OR IGNORE INTO slug_history (slug, thread_id, is_current, created_at) "
        "VALUES (?,?,1,?)",
        (slug, thread_id, now_iso()),
    )
    return slug


def _base36(value: int) -> str:
    """Encode an integer in base36, for compact opaque slugs."""
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    out = []
    while value:
        value, rem = divmod(value, 36)
        out.append(digits[rem])
    return "".join(reversed(out))


# ── media mirroring ─────────────────────────────────────────────────────────


def process_image(raw: bytes) -> tuple[bytes, int, int] | None:
    """
    Normalize an image for the web: downscale, strip metadata, encode WebP.

    Metadata stripping is not only a size win -- EXIF on drone screenshots can
    carry GPS coordinates the poster never meant to publish.
    """
    try:
        with Image.open(io.BytesIO(raw)) as img:
            img = ImageOps.exif_transpose(img)
            if getattr(img, "is_animated", False):
                img.seek(0)
            if img.mode in ("RGBA", "LA", "P"):
                background = Image.new("RGB", img.size, (255, 255, 255))
                converted = img.convert("RGBA")
                background.paste(converted, mask=converted.split()[-1])
                img = background
            elif img.mode != "RGB":
                img = img.convert("RGB")

            img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.LANCZOS)

            for quality in (WEBP_QUALITY, 70, 60, 50):
                buf = io.BytesIO()
                img.save(buf, format="WEBP", quality=quality, method=4)
                if buf.tell() <= MAX_OUTPUT_BYTES:
                    return buf.getvalue(), img.width, img.height

            img.thumbnail((1024, 1024), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="WEBP", quality=50, method=4)
            return buf.getvalue(), img.width, img.height
    except Exception as e:
        print(f"  WARNING: image processing failed: {e}")
        return None


def _record_media(conn, kind: str, source_key: str, source_url: str,
                  status: str, **fields) -> int:
    """Insert or update a media row, returning its id."""
    row = conn.execute(
        "SELECT id FROM media WHERE source_key = ?", (source_key,)
    ).fetchone()
    values = {
        "kind": kind, "source_key": source_key, "source_url": source_url,
        "status": status, "fetched_at": now_iso(),
        "content_sha256": fields.get("sha"), "local_path": fields.get("path"),
        "mime": fields.get("mime"), "width": fields.get("width"),
        "height": fields.get("height"), "bytes": fields.get("bytes"),
        "error": fields.get("error"),
    }
    if row:
        sets = ", ".join(f"{k}=?" for k in values)
        conn.execute(f"UPDATE media SET {sets} WHERE id=?",
                     (*values.values(), row["id"]))
        return row["id"]
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    cur = conn.execute(f"INSERT INTO media ({cols}) VALUES ({marks})",
                       tuple(values.values()))
    return cur.lastrowid


def attachment_key(attachment_id: str) -> str:
    """Stable media key for an attachment. The signed CDN URL is not stable."""
    return hashlib.sha256(f"attachment|{attachment_id}".encode()).hexdigest()[:32]


def mirror_attachment(conn, client: DiscordClient, att: dict) -> int | None:
    """
    Mirror one image attachment locally, deduping by attachment id and content.

    The signed CDN URL is not a stable key (it changes every fetch), so the
    immutable attachment snowflake is used instead.
    """
    aid = str(att["id"])
    key = attachment_key(aid)
    base_url = strip_signature(att.get("url", ""))
    mime = att.get("content_type") or ""

    existing = conn.execute(
        "SELECT id, status, local_path FROM media WHERE source_key = ?", (key,)
    ).fetchone()
    if existing and existing["status"] == "ok" and existing["local_path"]:
        if (ROOT_DIR / "static" / existing["local_path"].lstrip("/")).exists():
            return existing["id"]

    if not mime.startswith("image/") or "svg" in mime:
        return _record_media(conn, "attachment", key, base_url, "skipped_type", mime=mime)
    if (att.get("size") or 0) > MAX_SOURCE_BYTES:
        return _record_media(conn, "attachment", key, base_url, "skipped_size", mime=mime)

    raw = client.download(att["url"])
    if raw is None:
        return _record_media(conn, "attachment", key, base_url, "failed",
                             mime=mime, error="download failed")

    processed = process_image(raw)
    if processed is None:
        return _record_media(conn, "attachment", key, base_url, "failed",
                             mime=mime, error="decode failed")

    data, width, height = processed
    sha = hashlib.sha256(data).hexdigest()
    rel = f"/images/help/attachments/{sha[:16]}.webp"
    dest = ATTACH_DIR / f"{sha[:16]}.webp"
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    return _record_media(conn, "attachment", key, base_url, "ok", sha=sha, path=rel,
                         mime="image/webp", width=width, height=height,
                         bytes=len(data))


def mirror_avatar(conn, client: DiscordClient, user_id: str,
                  avatar_hash: str | None) -> int | None:
    """Mirror a user avatar. Users without one fall back to a local placeholder."""
    if not avatar_hash:
        return None
    key = hashlib.sha256(f"avatar|{user_id}|{avatar_hash}".encode()).hexdigest()[:32]
    ext = "gif" if avatar_hash.startswith("a_") else "png"
    url = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.{ext}?size=128"

    existing = conn.execute(
        "SELECT id, status, local_path FROM media WHERE source_key = ?", (key,)
    ).fetchone()
    if existing and existing["status"] == "ok" and existing["local_path"]:
        if (ROOT_DIR / "static" / existing["local_path"].lstrip("/")).exists():
            return existing["id"]

    raw = client.download(url)
    if raw is None:
        return _record_media(conn, "avatar", key, strip_signature(url), "failed",
                             error="download failed")
    try:
        with Image.open(io.BytesIO(raw)) as img:
            if getattr(img, "is_animated", False):
                img.seek(0)
            img = img.convert("RGB").resize((AVATAR_SIZE, AVATAR_SIZE), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="WEBP", quality=82, method=4)
            data = buf.getvalue()
    except Exception as e:
        return _record_media(conn, "avatar", key, strip_signature(url), "failed",
                             error=str(e)[:200])

    sha = hashlib.sha256(data).hexdigest()
    rel = f"/images/help/avatars/{sha[:16]}.webp"
    dest = AVATAR_DIR / f"{sha[:16]}.webp"
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    return _record_media(conn, "avatar", key, strip_signature(url), "ok", sha=sha,
                         path=rel, mime="image/webp", width=AVATAR_SIZE,
                         height=AVATAR_SIZE, bytes=len(data))


# ── thread crawling ─────────────────────────────────────────────────────────


class SyncStats:
    """Counters used both for the closing summary and the silent-failure guards."""

    def __init__(self):
        self.threads_seen = 0
        self.threads_updated = 0
        self.messages_upserted = 0
        self.messages_deleted = 0
        self.media_downloaded = 0
        self.message_lists_fetched = 0
        self.message_lists_empty = 0
        self.messages_inspected = 0
        self.messages_with_content = 0


def discover_threads(client: DiscordClient, guild_id: str, channel_id: str,
                     full: bool, since: str | None) -> list[dict]:
    """Enumerate active plus public-archived threads under the help channel."""
    seen: dict[str, dict] = {}

    if guild_id:
        for t in client.get_active_threads(guild_id, channel_id):
            seen[str(t["id"])] = t
        print(f"  Active threads: {len(seen)}")

    archived = 0
    for t in client.iter_archived_threads(channel_id):
        archived += 1
        tid = str(t["id"])
        if tid not in seen:
            seen[tid] = t
        if since:
            stamp = t.get("thread_metadata", {}).get("archive_timestamp") or ""
            if stamp and stamp < since and not full:
                break
    print(f"  Archived threads: {archived}")
    return list(seen.values())


def thread_created_at(thread: dict) -> str:
    """Thread creation time, falling back to the snowflake for pre-2022 threads."""
    meta = thread.get("thread_metadata") or {}
    return norm_ts(meta.get("create_timestamp")) or snowflake_to_iso(str(thread["id"]))


def sync_thread(conn, client: DiscordClient, thread: dict, cfg: dict,
                optout: dict, stats: SyncStats, full: bool) -> bool:
    """
    Bring one thread's stored messages up to date.

    When anything changed the whole thread is re-crawled rather than appending
    from a high-water mark: a forward-only crawl structurally cannot observe an
    edit or a deletion of an older message, and help threads are short enough
    that a full re-read costs one to three requests.
    """
    tid = str(thread["id"])
    meta = thread.get("thread_metadata") or {}
    title = thread.get("name") or "Untitled"
    row = conn.execute("SELECT * FROM threads WHERE id = ?", (tid,)).fetchone()

    applied = [str(x) for x in (thread.get("applied_tags") or [])]
    stored_tags = (
        {r["tag_id"] for r in conn.execute(
            "SELECT tag_id FROM thread_tags WHERE thread_id = ?", (tid,))}
        if row else set()
    )

    changed = (
        row is None
        or full
        or row["last_message_id"] != (thread.get("last_message_id") or None)
        or row["total_message_sent"] != (thread.get("total_message_sent") or 0)
        or row["title"] != title
        or stored_tags != set(applied)
    )

    slug = assign_slug(conn, tid, title)
    created = thread_created_at(thread)
    guild_id = cfg.get("guild_id") or str(thread.get("guild_id") or "")

    if row is None:
        conn.execute(
            "INSERT INTO threads (id, parent_id, guild_id, title, slug, owner_id, "
            "created_at, first_seen_at) VALUES (?,?,?,?,?,?,?,?)",
            (tid, str(thread.get("parent_id") or cfg["help_channel_id"]), guild_id,
             title, slug, str(thread.get("owner_id") or ""), created, now_iso()),
        )

    conn.execute(
        "UPDATE threads SET title=?, last_message_id=?, message_count=?, "
        "total_message_sent=?, archived=?, locked=?, last_synced_at=?, deleted_at=NULL "
        "WHERE id=?",
        (title, thread.get("last_message_id"), thread.get("message_count") or 0,
         thread.get("total_message_sent") or 0, 1 if meta.get("archived") else 0,
         1 if meta.get("locked") else 0, now_iso(), tid),
    )

    conn.execute("DELETE FROM thread_tags WHERE thread_id = ?", (tid,))
    for tag_id in applied:
        conn.execute(
            "INSERT OR IGNORE INTO thread_tags (thread_id, tag_id) VALUES (?,?)",
            (tid, tag_id),
        )

    if not changed:
        return False

    messages = list(client.iter_thread_messages(tid))
    stats.message_lists_fetched += 1
    if not messages:
        stats.message_lists_empty += 1

    have_starter = any(str(m["id"]) == tid for m in messages)
    if not have_starter:
        # Forum posts keep the starter inside the thread; text-channel threads
        # keep it in the parent. Either may have been deleted.
        starter = client.get_message(tid, tid) or client.get_message(
            str(thread.get("parent_id") or cfg["help_channel_id"]), tid
        )
        if starter:
            messages.append(starter)

    messages = [m for m in messages if int(m.get("type", 0)) in KEEP_MESSAGE_TYPES]
    messages.sort(key=lambda m: int(m["id"]))

    seen_ids = set()
    for seq, msg in enumerate(messages):
        mid = str(msg["id"])
        seen_ids.add(mid)
        author = msg.get("author") or {}
        stats.messages_inspected += 1
        content = msg.get("content") or ""
        if content or msg.get("attachments") or msg.get("embeds"):
            stats.messages_with_content += 1

        author_id = upsert_user(conn, client, author, guild_id, optout) if author else None
        redacted = 0
        if author_id:
            urow = conn.execute(
                "SELECT opted_out FROM users WHERE id = ?", (author_id,)
            ).fetchone()
            redacted = 1 if urow and urow["opted_out"] else 0

        reactions = [
            {"name": r.get("emoji", {}).get("name"),
             "id": r.get("emoji", {}).get("id"),
             "count": r.get("count", 0)}
            for r in (msg.get("reactions") or [])
        ]
        reaction_total = sum(r["count"] for r in reactions)
        embeds = [
            {"title": e.get("title"), "description": e.get("description"),
             "url": e.get("url")}
            for e in (msg.get("embeds") or [])
            if e.get("title") or e.get("description")
        ]
        ref = msg.get("message_reference") or {}

        existing = conn.execute("SELECT id FROM messages WHERE id = ?", (mid,)).fetchone()
        fields = (
            tid, author_id, seq, int(msg.get("type", 0)),
            "" if redacted else content,
            norm_ts(msg.get("timestamp")) or snowflake_to_iso(mid),
            norm_ts(msg.get("edited_timestamp")),
            str(ref.get("message_id")) if ref.get("message_id") else None,
            1 if msg.get("pinned") else 0, reaction_total,
            json.dumps(reactions) if reactions else None,
            json.dumps(embeds) if embeds else None,
            1 if author.get("bot") else 0, redacted, now_iso(),
        )
        if existing:
            conn.execute(
                "UPDATE messages SET thread_id=?, author_id=?, seq=?, type=?, content=?, "
                "created_at=?, edited_at=?, reply_to_id=?, pinned=?, reaction_total=?, "
                "reactions_json=?, embeds_json=?, is_bot=?, redacted=?, last_seen_at=?, "
                "deleted_at=NULL WHERE id=?",
                (*fields, mid),
            )
        else:
            conn.execute(
                "INSERT INTO messages (thread_id, author_id, seq, type, content, "
                "created_at, edited_at, reply_to_id, pinned, reaction_total, "
                "reactions_json, embeds_json, is_bot, redacted, last_seen_at, "
                "first_seen_at, id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*fields, now_iso(), mid),
            )
        stats.messages_upserted += 1

        conn.execute("DELETE FROM attachments WHERE message_id = ?", (mid,))
        for pos, att in enumerate(msg.get("attachments") or []):
            mime = att.get("content_type") or ""
            conn.execute(
                "INSERT OR REPLACE INTO attachments (id, message_id, position, filename, "
                "content_type, size, width, height, is_image, discord_url) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (str(att["id"]), mid, pos, att.get("filename") or "file", mime,
                 att.get("size"), att.get("width"), att.get("height"),
                 1 if mime.startswith("image/") and "svg" not in mime else 0,
                 strip_signature(att.get("url", ""))),
            )

    # Anything we stored before but did not see now was deleted on Discord.
    stored = {
        r["id"] for r in conn.execute(
            "SELECT id FROM messages WHERE thread_id = ? AND deleted_at IS NULL", (tid,))
    }
    for gone in stored - seen_ids:
        conn.execute("UPDATE messages SET deleted_at = ? WHERE id = ?", (now_iso(), gone))
        stats.messages_deleted += 1

    if messages:
        conn.execute(
            "UPDATE threads SET starter_message_id=?, high_water_msg_id=?, "
            "last_activity_at=?, stored_message_count=? WHERE id=?",
            (str(messages[0]["id"]), str(messages[-1]["id"]),
             norm_ts(messages[-1].get("timestamp")), len(messages), tid),
        )
    return True


# ── publication filter ──────────────────────────────────────────────────────


def recompute_publishable(conn, optout: dict) -> tuple[int, int]:
    """
    Decide which threads earn a public page.

    Thin pages are a site-wide quality signal rather than merely weak
    individual pages, so an unanswered or one-word thread gets no page at all.
    Not generating the file also keeps it out of the sitemap, which a
    `noindex` page would not.
    """
    published = 0
    total = 0
    for t in conn.execute("SELECT * FROM threads"):
        total += 1
        tid = t["id"]
        reason = None

        msgs = list(conn.execute(
            "SELECT m.*, u.opted_out FROM messages m "
            "LEFT JOIN users u ON u.id = m.author_id "
            "WHERE m.thread_id = ? AND m.deleted_at IS NULL ORDER BY m.seq", (tid,)
        ))
        question = msgs[0] if msgs else None
        replies = msgs[1:]

        owner_opted = False
        if t["owner_id"]:
            row = conn.execute(
                "SELECT opted_out FROM users WHERE id = ?", (t["owner_id"],)
            ).fetchone()
            owner_opted = bool(row and row["opted_out"])
        if question and question["redacted"]:
            owner_opted = True

        q_text = plain_text(question["content"]) if question else ""
        answer_texts = [
            plain_text(r["content"]) for r in replies
            if not r["redacted"] and not r["is_bot"]
            and r["author_id"] != (question["author_id"] if question else None)
        ]
        good_answers = [a for a in answer_texts if len(a) >= MIN_ANSWER_CHARS]
        total_chars = len(q_text) + sum(len(a) for a in answer_texts)

        if t["deleted_at"]:
            reason = "deleted"
        elif tid in optout["thread_ids"]:
            reason = "optout"
        elif owner_opted:
            reason = "optout"
        elif not question:
            reason = "no-question"
        elif len(q_text) < MIN_QUESTION_CHARS:
            reason = "thin-question"
        elif not good_answers:
            reason = "no-answer"
        elif total_chars < MIN_TOTAL_CHARS:
            reason = "thin"

        ok = reason is None
        score = len(q_text) + total_chars // 2 + 20 * len(good_answers) + \
            sum(r["reaction_total"] for r in replies) * 5

        # Best answer: most-reacted substantial reply from someone other than
        # the asker; length is the tie-break.
        accepted = None
        if ok:
            candidates = [
                r for r in replies
                if not r["redacted"] and not r["is_bot"]
                and r["author_id"] != question["author_id"]
                and len(plain_text(r["content"])) >= MIN_ANSWER_CHARS
            ]
            if candidates:
                best = max(candidates,
                           key=lambda r: (r["reaction_total"], len(r["content"])))
                if best["reaction_total"] > 0 or len(candidates) == 1:
                    accepted = best["id"]

        conn.execute(
            "UPDATE threads SET publishable=?, exclude_reason=?, quality_score=?, "
            "reply_count=?, accepted_message_id=? WHERE id=?",
            (1 if ok else 0, reason, score, len(good_answers), accepted, tid),
        )
        if ok:
            published += 1
    return published, total


def assert_guards(stats: SyncStats, app_hint: str = "") -> None:
    """
    Abort on the shapes of data that mean a permission or intent is missing.

    Both failure modes are silent at the API level: Discord returns an empty
    array for a missing READ_MESSAGE_HISTORY and empty strings for a missing
    MESSAGE_CONTENT intent. Without these checks the pipeline would happily
    publish thousands of blank pages.
    """
    if stats.threads_seen == 0:
        raise DiscordFatal(
            "No threads found. Check the channel id, that the bot is in the guild, "
            "and that it has VIEW_CHANNEL on the help channel."
        )
    if stats.message_lists_fetched > 0 and \
            stats.message_lists_empty == stats.message_lists_fetched:
        raise DiscordFatal(
            "Every message list came back empty. The bot is almost certainly "
            "missing READ_MESSAGE_HISTORY on the help channel (Discord returns an "
            "empty array rather than a 403 for this)."
        )
    if stats.messages_inspected >= 5 and stats.messages_with_content == 0:
        raise DiscordFatal(
            "Fetched %d messages and every one had empty content, attachments and "
            "embeds. The MESSAGE_CONTENT privileged intent is OFF. Enable it in the "
            "Developer Portal (Bot -> Privileged Gateway Intents); it gates the REST "
            "API too, not just the Gateway.%s" % (stats.messages_inspected, app_hint)
        )


def attachment_on_disk(conn, attachment_id: str) -> bool:
    """True when this attachment is already mirrored and the file is still there."""
    row = conn.execute(
        "SELECT local_path FROM media WHERE source_key = ? AND status = 'ok'",
        (attachment_key(str(attachment_id)),)
    ).fetchone()
    return bool(row and row["local_path"]
                and (ROOT_DIR / "static" / row["local_path"].lstrip("/")).exists())


def mirror_media_for_publishable(conn, client: DiscordClient, stats: SyncStats,
                                 dry_run: bool) -> None:
    """
    Download media only for threads that will actually be published.

    Roughly half of all threads are filtered as thin, so deferring the media
    pass until after recompute_publishable halves the committed image set.
    """
    threads = list(conn.execute(
        "SELECT id FROM threads WHERE publishable = 1 AND deleted_at IS NULL"
    ))

    # Choose the attachments first, so the re-signing calls below cover only
    # images that actually survive the per-message and per-thread budgets.
    selected = []
    for t in threads:
        budget = MAX_IMAGES_PER_THREAD
        per_message: dict[str, int] = {}
        for att in conn.execute(
            "SELECT a.*, m.id AS mid FROM attachments a "
            "JOIN messages m ON m.id = a.message_id "
            "WHERE m.thread_id = ? AND m.deleted_at IS NULL AND m.redacted = 0 "
            "AND a.is_image = 1 ORDER BY m.seq, a.position", (t["id"],)
        ):
            if budget <= 0:
                break
            used = per_message.get(att["mid"], 0)
            if used >= MAX_IMAGES_PER_MESSAGE:
                continue
            selected.append(att)
            per_message[att["mid"]] = used + 1
            budget -= 1

    if not dry_run:
        # discord_url is stored without its signature, and the CDN 404s on an
        # unsigned attachment URL, so every one still to fetch needs re-signing.
        pending = [a for a in selected if not attachment_on_disk(conn, a["id"])]
        signed = client.refresh_attachment_urls(
            sorted({a["discord_url"] for a in pending if a["discord_url"]}))
        if pending and not signed:
            print(f"  WARNING: could not re-sign any of {len(pending)} attachment "
                  "URLs; images will be skipped this run.")

        for i, att in enumerate(selected, 1):
            url = att["discord_url"]
            media_id = mirror_attachment(conn, client, {
                "id": att["id"], "url": signed.get(url, url),
                "content_type": att["content_type"], "size": att["size"],
            })
            if media_id:
                conn.execute("UPDATE attachments SET media_id=? WHERE id=?",
                             (media_id, att["id"]))
                ok = conn.execute("SELECT status FROM media WHERE id=?",
                                  (media_id,)).fetchone()
                if ok and ok["status"] == "ok":
                    stats.media_downloaded += 1
            if i % 25 == 0:
                print(f"  [{i}/{len(selected)}] media pass")

    users = list(conn.execute(
        "SELECT DISTINCT u.id, u.avatar_hash FROM users u "
        "JOIN messages m ON m.author_id = u.id "
        "JOIN threads t ON t.id = m.thread_id "
        "WHERE t.publishable = 1 AND u.avatar_hash IS NOT NULL AND u.opted_out = 0"
    ))
    for u in users:
        if dry_run:
            continue
        media_id = mirror_avatar(conn, client, u["id"], u["avatar_hash"])
        if media_id:
            conn.execute("UPDATE users SET avatar_media_id=? WHERE id=?",
                         (media_id, u["id"]))


def media_size_bytes() -> int:
    """Total size of the mirrored media directory."""
    if not MEDIA_DIR.exists():
        return 0
    return sum(f.stat().st_size for f in MEDIA_DIR.rglob("*") if f.is_file())


# ── entry point ─────────────────────────────────────────────────────────────


def purge_user(conn, user_id: str) -> None:
    """Hard-delete one user's content, for an explicit removal request."""
    rows = list(conn.execute("SELECT id FROM messages WHERE author_id = ?", (user_id,)))
    for r in rows:
        for att in conn.execute(
            "SELECT media_id FROM attachments WHERE message_id = ?", (r["id"],)
        ):
            if att["media_id"]:
                m = conn.execute("SELECT local_path FROM media WHERE id = ?",
                                 (att["media_id"],)).fetchone()
                if m and m["local_path"]:
                    path = ROOT_DIR / "static" / m["local_path"].lstrip("/")
                    path.unlink(missing_ok=True)
                conn.execute("UPDATE media SET status='gone', local_path=NULL "
                             "WHERE id=?", (att["media_id"],))
        conn.execute("DELETE FROM attachments WHERE message_id = ?", (r["id"],))
    conn.execute("DELETE FROM messages WHERE author_id = ?", (user_id,))
    conn.execute("UPDATE threads SET excluded=1, exclude_reason='optout', "
                 "publishable=0 WHERE owner_id=?", (user_id,))
    avatar = conn.execute("SELECT avatar_media_id FROM users WHERE id=?",
                          (user_id,)).fetchone()
    if avatar and avatar["avatar_media_id"]:
        m = conn.execute("SELECT local_path FROM media WHERE id=?",
                         (avatar["avatar_media_id"],)).fetchone()
        if m and m["local_path"]:
            (ROOT_DIR / "static" / m["local_path"].lstrip("/")).unlink(missing_ok=True)
    conn.execute("UPDATE users SET opted_out=1, avatar_media_id=NULL, avatar_hash=NULL "
                 "WHERE id=?", (user_id,))
    print(f"Purged {len(rows)} messages by user {user_id}.")


def parse_args() -> argparse.Namespace:
    """Build the command line."""
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--channel-id", help="help channel id (overrides config)")
    p.add_argument("--guild-id", help="guild id (overrides config)")
    p.add_argument("--full", action="store_true",
                   help="re-crawl every thread and reconcile thread deletions")
    p.add_argument("--limit", type=int, help="stop after N threads (smoke tests)")
    p.add_argument("--since", help="ignore threads archived before this ISO8601 date")
    p.add_argument("--dry-run", action="store_true", help="crawl but write nothing")
    p.add_argument("--no-media", action="store_true", help="skip all downloads")
    p.add_argument("--db", type=Path, default=help_db.DB_FILE, help="database path")
    p.add_argument("--fixtures", type=Path, help="replay recorded responses, no token")
    p.add_argument("--record", type=Path, help="record live responses into a directory")
    p.add_argument("--allow-mass-delete", action="store_true",
                   help="permit removing more than 20%% of published threads")
    p.add_argument("--purge-user", help="hard-delete one user's content and exit")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    # The dump follows --db, so a fixture or test run writes its own .sql file
    # instead of clobbering the committed archive.
    db_sql = help_db.sql_path_for(args.db)
    db_enc = help_db.enc_path_for(args.db)
    conn = help_db.ensure_db(args.db)
    optout = load_optout()

    if args.purge_user:
        purge_user(conn, args.purge_user)
        recompute_publishable(conn, optout)
        conn.commit()
        help_db.dump_sql(conn, db_sql)
        help_db.seal_dump(db_sql, db_enc)
        return

    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token and not args.fixtures:
        print("Error: DISCORD_BOT_TOKEN is not set (or pass --fixtures for offline use)")
        sys.exit(1)

    try:
        cfg = load_config(args)
    except DiscordFatal as e:
        print(f"Error: {e}")
        sys.exit(1)

    client = DiscordClient(token, args.fixtures, args.record, args.verbose)
    stats = SyncStats()
    started = now_iso()
    mode = "dry-run" if args.dry_run else ("full" if args.full else "incremental")
    print(f"Syncing Discord #help ({mode})...")

    try:
        channel = client.get_channel(cfg["help_channel_id"])
        if not channel:
            raise DiscordFatal(
                f"Channel {cfg['help_channel_id']} not found or not visible to the bot."
            )
        ctype = int(channel.get("type", -1))
        if ctype not in (CHANNEL_TEXT, CHANNEL_FORUM, CHANNEL_MEDIA):
            raise DiscordFatal(
                f"Channel type {ctype} is not a text ({CHANNEL_TEXT}) or forum "
                f"({CHANNEL_FORUM}) channel."
            )
        if not cfg.get("guild_id"):
            cfg["guild_id"] = str(channel.get("guild_id") or "")
        print(f"  Channel: #{channel.get('name')} (type {ctype})")

        for tag in channel.get("available_tags") or []:
            emoji = tag.get("emoji_name")
            conn.execute(
                "INSERT INTO tags (id, name, slug, emoji, moderated, updated_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "name=excluded.name, slug=excluded.slug, emoji=excluded.emoji, "
                "moderated=excluded.moderated, updated_at=excluded.updated_at",
                (str(tag["id"]), tag.get("name") or "tag",
                 help_db.slugify(tag.get("name") or "tag") or str(tag["id"]),
                 emoji, 1 if tag.get("moderated") else 0, now_iso()),
            )

        help_db.set_meta(conn, "guild_id", cfg["guild_id"])
        help_db.set_meta(conn, "channel_id", cfg["help_channel_id"])
        help_db.set_meta(conn, "channel_type", str(ctype))
        help_db.set_meta(conn, "channel_name", channel.get("name") or "help")

        # Incremental runs stop paging archived threads once they are older
        # than the previous sync. The grace period covers clock skew and a
        # missed run; a thread that gets new activity is un-archived and so
        # shows up in the active list regardless.
        since = args.since
        if not since and not args.full:
            last = help_db.get_meta(conn, "last_sync_at")
            if last:
                try:
                    cutoff = datetime.fromisoformat(last.replace("Z", "+00:00")) - \
                        timedelta(days=ARCHIVED_GRACE_DAYS)
                    since = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
                except ValueError:
                    since = None

        threads = discover_threads(
            client, cfg["guild_id"], cfg["help_channel_id"], args.full, since
        )
        if args.limit:
            threads = threads[: args.limit]
        stats.threads_seen = len(threads)

        for i, thread in enumerate(threads, 1):
            updated = sync_thread(conn, client, thread, cfg, optout, stats, args.full)
            if updated:
                stats.threads_updated += 1
            if args.verbose or i % 25 == 0 or updated:
                state = "updated" if updated else "unchanged"
                print(f"  [{i}/{len(threads)}] {thread.get('name', '')[:60]} - {state}")

        assert_guards(stats)

        if args.full and not args.limit:
            live = {str(t["id"]) for t in threads}
            stored = {r["id"] for r in conn.execute(
                "SELECT id FROM threads WHERE deleted_at IS NULL")}
            vanished = stored - live
            published_now = conn.execute(
                "SELECT COUNT(*) c FROM threads WHERE publishable = 1"
            ).fetchone()["c"]
            if published_now and len(vanished) > published_now * MASS_DELETE_RATIO \
                    and not args.allow_mass_delete:
                raise DiscordFatal(
                    f"{len(vanished)} threads disappeared ({published_now} published). "
                    "This usually means a permission regression, not real deletions. "
                    "Re-run with --allow-mass-delete if it is genuine."
                )
            for tid in vanished:
                conn.execute("UPDATE threads SET deleted_at=? WHERE id=?",
                             (now_iso(), tid))

        published_before = conn.execute(
            "SELECT COUNT(*) c FROM threads WHERE publishable = 1"
        ).fetchone()["c"]
        published, total = recompute_publishable(conn, optout)
        if published_before > 0 and published == 0 and not args.allow_mass_delete:
            raise DiscordFatal(
                f"This sync would unpublish all {published_before} pages. That is "
                "almost always a permission or intent regression rather than real "
                "content loss. Re-run with --allow-mass-delete if it is genuine."
            )

        if not args.no_media and not args.dry_run:
            print("  Mirroring media for publishable threads...")
            mirror_media_for_publishable(conn, client, stats, args.dry_run)

        help_db.set_meta(conn, "last_sync_at", started)
        conn.execute(
            "INSERT INTO sync_runs (started_at, finished_at, mode, threads_seen, "
            "threads_updated, messages_upserted, messages_deleted, media_downloaded, "
            "api_requests, rate_limit_waits, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (started, now_iso(), mode, stats.threads_seen, stats.threads_updated,
             stats.messages_upserted, stats.messages_deleted, stats.media_downloaded,
             client.requests_made, client.rate_limit_waits, "ok"),
        )

        if args.dry_run:
            conn.rollback()
            print("\nDry run: nothing written.")
        else:
            conn.commit()
            conn.execute("VACUUM")
            help_db.dump_sql(conn, db_sql)
            help_db.seal_dump(db_sql, db_enc)

        print(
            f"\nDone: {stats.threads_seen} threads seen, {stats.threads_updated} updated, "
            f"{stats.messages_upserted} messages, {stats.messages_deleted} deleted, "
            f"{stats.media_downloaded} images "
            f"({client.requests_made} API requests, {client.rate_limit_waits} waits)."
        )
        print(f"Publishable: {published}/{total} threads.")
        size = media_size_bytes()
        print(f"Mirrored media: {size / 1024 / 1024:.1f} MB")
        if size > MEDIA_WARN_BYTES:
            print("  WARNING: mirrored media is large; consider moving it out of git.")

    except DiscordFatal as e:
        conn.rollback()
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        conn.rollback()
        print("\nInterrupted; nothing written.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
