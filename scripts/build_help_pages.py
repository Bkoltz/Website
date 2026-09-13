#!/usr/bin/env python3
"""
Generate the /community/help/ pages from the Discord archive.

Reads data/discord.sql (rebuilding data/discord.sqlite3 from it when needed)
and writes one Zola page per publishable thread, plus the section index.
Needs no network and no Discord token, so `zola serve`
works offline for anyone who has cloned the repo.

Rendered HTML is carried in TOML front matter rather than the page body: Zola
interpolates {{ ... }} and {% ... %} inside markdown bodies, which would let a
Discord message inject template syntax into the build. Front matter is not
shortcode-processed.

Requirements: none (standard library only)
Output:       content/community/help/ (generated; gitignored)
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import help_db
from discord_markdown import RenderContext, plain_text, render

ROOT_DIR = Path(__file__).parent.parent
OUT_DIR = ROOT_DIR / "content" / "community" / "help"
OPTOUT_FILE = ROOT_DIR / "data" / "discord_optout.json"
BASE_URL = "https://webodm.org"

# Discord returns reaction emoji by name; render the character instead.
EMOJI_NAMES = {
    "+1": "\U0001F44D", "thumbsup": "\U0001F44D",
    "-1": "\U0001F44E", "thumbsdown": "\U0001F44E",
    "white_check_mark": "\u2705", "heavy_check_mark": "\u2714\ufe0f",
    "ballot_box_with_check": "\u2611\ufe0f", "tada": "\U0001F389",
    "heart": "\u2764\ufe0f", "fire": "\U0001F525", "rocket": "\U0001F680",
    "eyes": "\U0001F440", "pray": "\U0001F64F", "clap": "\U0001F44F",
    "star": "\u2B50", "bulb": "\U0001F4A1", "100": "\U0001F4AF",
    "smile": "\U0001F604", "joy": "\U0001F602", "thinking": "\U0001F914",
    "sob": "\U0001F62D", "pensive": "\U0001F614", "wave": "\U0001F44B",
}

MAX_DESCRIPTION = 155
MAX_ANSWER_TEXT = 1200
RELATED_COUNT = 5
MAX_EXCERPT = 150

STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "have", "has", "not",
    "are", "but", "you", "your", "can", "how", "why", "what", "when", "where",
    "does", "did", "was", "were", "will", "would", "could", "should", "any",
    "all", "get", "got", "its", "it's", "there", "then", "than", "out", "use",
    "using", "used", "into", "about", "after", "before", "some", "only", "very",
    "just", "like", "also", "help", "issue", "issues", "problem", "problems",
    "error", "errors", "question", "webodm", "odm", "please", "thanks", "need",
}


def toml_str(value: str) -> str:
    """
    Serialize a Python string as a single-line TOML basic string.

    Multi-line forms are deliberately never used: user content can contain the
    ''' or \"\"\" delimiter and break out of the front matter.
    """
    out = ['"']
    for ch in value or "":
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append("\\u%04X" % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def toml_list(values: list[str]) -> str:
    """Serialize a list of strings as a TOML array."""
    return "[" + ", ".join(toml_str(v) for v in values) + "]"


def json_ld(obj: dict) -> str:
    """
    Serialize JSON-LD for safe embedding in a <script> element.

    Escaping '<' is mandatory: a message containing </script> would otherwise
    terminate the element early and everything after it becomes live markup.
    """
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def iso_date(value: str | None) -> str:
    """Normalize a stored timestamp to an RFC3339 value Zola accepts."""
    if not value:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return value


def human_date(value: str | None) -> str:
    """Render a timestamp the way the page shows it."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return dt.strftime("%B %-d, %Y")


def load_optout() -> dict:
    """Read the opt-out list, applied again at build time as a second gate."""
    if not OPTOUT_FILE.exists():
        return {"user_ids": set(), "usernames": set(), "thread_ids": set()}
    data = json.loads(OPTOUT_FILE.read_text())
    return {
        "user_ids": {str(x) for x in data.get("user_ids", [])},
        "usernames": {str(x).lower() for x in data.get("usernames", [])},
        "thread_ids": {str(x) for x in data.get("thread_ids", [])},
    }


# ── loading ─────────────────────────────────────────────────────────────────


def build_context(conn) -> RenderContext:
    """Lookup tables so mentions resolve to names instead of raw snowflakes."""
    users = {
        r["id"]: r["display_name"]
        for r in conn.execute("SELECT id, display_name FROM users")
    }
    channels = {}
    channel_id = help_db.get_meta(conn, "channel_id")
    if channel_id:
        channels[channel_id] = help_db.get_meta(conn, "channel_name") or "help"
    return RenderContext(users=users, channels=channels)


def media_for(conn, media_id) -> dict | None:
    """Return a usable mirrored-media row, or None when it never landed."""
    if not media_id:
        return None
    row = conn.execute(
        "SELECT local_path, width, height, status FROM media WHERE id = ?", (media_id,)
    ).fetchone()
    if not row or row["status"] != "ok" or not row["local_path"]:
        return None
    return {"src": row["local_path"], "w": row["width"], "h": row["height"]}


def author_of(conn, user_id, optout: dict) -> dict:
    """Resolve a message author to a display name plus mirrored avatar."""
    if not user_id:
        return {"name": "Community member", "avatar": ""}
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        return {"name": "Community member", "avatar": ""}
    if row["opted_out"] or row["id"] in optout["user_ids"] \
            or (row["username"] or "").lower() in optout["usernames"]:
        return {"name": "Community member", "avatar": ""}
    avatar = media_for(conn, row["avatar_media_id"])
    return {"name": row["display_name"], "avatar": avatar["src"] if avatar else ""}


def attachments_for(conn, message_id: str, author_name: str) -> tuple[list, list]:
    """Split a message's attachments into mirrored images and plain links."""
    images, files = [], []
    for att in conn.execute(
        "SELECT * FROM attachments WHERE message_id = ? ORDER BY position", (message_id,)
    ):
        media = media_for(conn, att["media_id"])
        if media:
            images.append({
                "src": media["src"], "w": media["w"] or 0, "h": media["h"] or 0,
                "alt": f"Screenshot posted by {author_name}",
            })
        else:
            files.append({"name": att["filename"] or "attachment"})
    return images, files


def load_threads(conn, optout: dict) -> list[dict]:
    """Assemble every publishable thread into a render-ready structure."""
    ctx = build_context(conn)
    guild_id = help_db.get_meta(conn, "guild_id") or ""

    out = []
    rows = conn.execute(
        "SELECT * FROM threads WHERE publishable = 1 AND deleted_at IS NULL "
        "ORDER BY created_at DESC"
    )
    for t in rows:
        tid = t["id"]
        if tid in optout["thread_ids"]:
            continue

        msgs = list(conn.execute(
            "SELECT * FROM messages WHERE thread_id = ? AND deleted_at IS NULL "
            "ORDER BY seq", (tid,)
        ))
        if not msgs:
            continue
        question, replies = msgs[0], msgs[1:]
        q_author = author_of(conn, question["author_id"], optout)
        if question["redacted"]:
            continue

        q_images, q_files = attachments_for(conn, question["id"], q_author["name"])
        q_html = render(question["content"], ctx)
        q_text = plain_text(question["content"], ctx)

        answers = []
        seq_by_id = {m["id"]: i for i, m in enumerate(msgs)}
        for reply in replies:
            if reply["is_bot"]:
                continue
            author = author_of(conn, reply["author_id"], optout)
            if reply["redacted"]:
                answers.append({
                    "idx": seq_by_id[reply["id"]], "author": author,
                    "created_iso": iso_date(reply["created_at"]),
                    "created_human": human_date(reply["created_at"]),
                    "html": "<p class=\"help-redacted\">[removed at the author's "
                            "request]</p>",
                    "text": "", "reply_to": -1, "reactions": [], "images": [],
                    "files": [], "accepted": False, "is_op": False,
                    "discord_url": "",
                })
                continue
            images, files = attachments_for(conn, reply["id"], author["name"])
            reactions = []
            if reply["reactions_json"]:
                for r in json.loads(reply["reactions_json"]):
                    name = r.get("name")
                    if not name or r.get("id"):
                        continue
                    glyph = EMOJI_NAMES.get(name)
                    if not glyph and len(name) <= 3 and not name.isascii():
                        glyph = name          # already a unicode emoji
                    reactions.append({
                        "emoji": glyph or f":{name}:", "count": r["count"],
                    })
            answers.append({
                "idx": seq_by_id[reply["id"]],
                "author": author,
                "created_iso": iso_date(reply["created_at"]),
                "created_human": human_date(reply["created_at"]),
                "html": render(reply["content"], ctx),
                "text": plain_text(reply["content"], ctx, MAX_ANSWER_TEXT),
                "reply_to": seq_by_id.get(reply["reply_to_id"], -1),
                "reactions": reactions,
                "images": images,
                "files": files,
                "accepted": reply["id"] == t["accepted_message_id"],
                "is_op": reply["author_id"] == question["author_id"],
                "discord_url": f"https://discord.com/channels/{guild_id}/{tid}/{reply['id']}",
            })

        description = plain_text(question["content"], ctx, MAX_DESCRIPTION)
        if len(description) < 50:
            description = (
                f"{t['title']} - answered by the WebODM community on Discord. "
                f"{len(answers)} replies."
            )

        out.append({
            "id": tid,
            "title": t["title"],
            "slug": t["slug"],
            "description": description,
            "created": iso_date(t["created_at"]),
            "updated": iso_date(t["last_activity_at"] or t["created_at"]),
            "created_human": human_date(t["created_at"]),
            "asked_by": q_author,
            "question_html": q_html,
            "question_text": q_text,
            "question_images": q_images,
            "question_files": q_files,
            "answers": answers,
            "answer_count": len(answers),
            "discord_url": f"https://discord.com/channels/{guild_id}/{tid}",
            "quality_score": t["quality_score"],
        })
    return out


# ── related threads ─────────────────────────────────────────────────────────


def tokenize(text: str) -> set[str]:
    """Content words of a title or question, for the related-threads score."""
    words = re.findall(r"[a-z0-9][a-z0-9_-]{2,}", (text or "").lower())
    return {w for w in words if w not in STOPWORDS}


def compute_related(threads: list[dict]) -> dict[str, list[dict]]:
    """
    Link every page to a few neighbours.

    Scores inverse-document-frequency weighted token overlap, so every
    generated page has internal inbound links rather than sitting in a
    dead-end silo.
    """
    tokens = {
        t["id"]: tokenize(t["title"]) | tokenize(t["question_text"][:400])
        for t in threads
    }
    doc_freq = Counter()
    for toks in tokens.values():
        doc_freq.update(toks)
    total = max(len(threads), 1)

    by_token = defaultdict(list)
    for tid, toks in tokens.items():
        for tok in toks:
            if doc_freq[tok] < total * 0.5:
                by_token[tok].append(tid)

    index = {t["id"]: t for t in threads}
    related: dict[str, list[dict]] = {}
    for t in threads:
        scores = Counter()
        for tok in tokens[t["id"]]:
            weight = total / (1 + doc_freq[tok])
            for other in by_token.get(tok, ()):
                if other != t["id"]:
                    scores[other] += weight
        best = [
            {"title": index[oid]["title"], "url": f"/community/help/{index[oid]['slug']}/"}
            for oid, _ in scores.most_common(RELATED_COUNT)
        ]
        related[t["id"]] = best
    return related


# ── structured data ─────────────────────────────────────────────────────────


def build_jsonld(thread: dict) -> str:
    """QAPage plus BreadcrumbList markup for one thread."""
    url = f"{BASE_URL}/community/help/{thread['slug']}/"

    def answer_node(a: dict) -> dict:
        node = {
            "@type": "Answer",
            "text": a["text"] or a["author"]["name"],
            "url": f"{url}#a-{a['idx']}",
            "dateCreated": a["created_iso"],
            "author": {"@type": "Person", "name": a["author"]["name"]},
        }
        votes = sum(r["count"] for r in a["reactions"])
        if votes:
            node["upvoteCount"] = votes
        return node

    real = [a for a in thread["answers"] if a["text"]]
    accepted = next((a for a in real if a["accepted"]), None)
    suggested = [a for a in real if a is not accepted]

    question = {
        "@type": "Question",
        "name": thread["title"],
        "text": thread["question_text"] or thread["title"],
        "answerCount": len(real),
        "dateCreated": thread["created"],
        "author": {"@type": "Person", "name": thread["asked_by"]["name"]},
    }
    if accepted:
        question["acceptedAnswer"] = answer_node(accepted)
    if suggested:
        question["suggestedAnswer"] = [answer_node(a) for a in suggested]

    graph = [
        {
            "@type": "QAPage",
            "@id": url,
            "url": url,
            "name": thread["title"],
            "mainEntity": question,
        },
        {
            "@type": "BreadcrumbList",
            "itemListElement": [
                {"@type": "ListItem", "position": 1, "name": "Home", "item": BASE_URL + "/"},
                {"@type": "ListItem", "position": 2, "name": "Community",
                 "item": BASE_URL + "/community/"},
                {"@type": "ListItem", "position": 3, "name": "Help",
                 "item": BASE_URL + "/community/help/"},
                {"@type": "ListItem", "position": 4, "name": thread["title"], "item": url},
            ],
        },
    ]
    return json_ld({"@context": "https://schema.org", "@graph": graph})


# ── page writing ────────────────────────────────────────────────────────────


def render_thread_page(thread: dict, related: list[dict]) -> str:
    """Build the TOML front matter for one thread page. The body stays empty."""
    lines = [
        "+++",
        f"title = {toml_str(thread['title'])}",
        f"description = {toml_str(thread['description'])}",
        f"date = {thread['created']}",
        f"updated = {thread['updated']}",
        'template = "help_thread.html"',
        "",
        "[extra]",
        f"thread_id = {toml_str(thread['id'])}",
        f"discord_url = {toml_str(thread['discord_url'])}",
        f"asked_at_human = {toml_str(thread['created_human'])}",
        f"answer_count = {thread['answer_count']}",
        f"question_html = {toml_str(thread['question_html'])}",
        f"jsonld = {toml_str(build_jsonld(thread))}",
        f"author_name = {toml_str(thread['asked_by']['name'])}",
        f"author_avatar = {toml_str(thread['asked_by']['avatar'])}",
    ]

    for img in thread["question_images"]:
        lines += [
            "",
            "[[extra.question_images]]",
            f"src = {toml_str(img['src'])}",
            f"w = {img['w']}",
            f"h = {img['h']}",
            f"alt = {toml_str(img['alt'])}",
        ]
    for f in thread["question_files"]:
        lines += ["", "[[extra.question_files]]", f"name = {toml_str(f['name'])}"]

    for rel in related:
        lines += [
            "",
            "[[extra.related]]",
            f"title = {toml_str(rel['title'])}",
            f"url = {toml_str(rel['url'])}",
        ]

    for a in thread["answers"]:
        lines += [
            "",
            "[[extra.answers]]",
            f"idx = {a['idx']}",
            f"author_name = {toml_str(a['author']['name'])}",
            f"author_avatar = {toml_str(a['author']['avatar'])}",
            f"created_iso = {toml_str(a['created_iso'])}",
            f"created_human = {toml_str(a['created_human'])}",
            f"html = {toml_str(a['html'])}",
            f"reply_to = {a['reply_to']}",
            f"accepted = {'true' if a['accepted'] else 'false'}",
            f"is_op = {'true' if a['is_op'] else 'false'}",
            f"discord_url = {toml_str(a['discord_url'])}",
        ]
        if a["reactions"]:
            pairs = ", ".join(
                "{ emoji = %s, count = %d }" % (toml_str(r["emoji"]), r["count"])
                for r in a["reactions"]
            )
            lines.append(f"reactions = [{pairs}]")
        else:
            lines.append("reactions = []")
        if a["images"]:
            imgs = ", ".join(
                "{ src = %s, w = %d, h = %d, alt = %s }"
                % (toml_str(i["src"]), i["w"], i["h"], toml_str(i["alt"]))
                for i in a["images"]
            )
            lines.append(f"images = [{imgs}]")
        else:
            lines.append("images = []")

    lines += ["+++", ""]
    return "\n".join(lines)


def render_index_page(threads: list[dict]) -> str:
    """Build the section index, which carries the listing data."""
    lines = [
        "+++",
        'title = "WebODM Help & Answers"',
        'description = "Answers to WebODM questions from the community '
        'Discord: processing errors, GCPs, point clouds, orthophotos, '
        'installation and more."',
        'template = "help_index.html"',
        'page_template = "help_thread.html"',
        'sort_by = "date"',
        "paginate_by = 50",
        'paginate_path = "page"',
        "",
        "[extra]",
        f"thread_count = {len(threads)}",
    ]
    lines += ["+++", ""]
    return "\n".join(lines)


def prune_stale(keep: set[Path]) -> int:
    """
    Delete generated pages that are no longer backed by a publishable thread.

    This is how a Discord-side deletion or a new opt-out entry actually removes
    a page from the site and from the sitemap.
    """
    removed = 0
    for path in list(OUT_DIR.rglob("*.md")):
        if path not in keep:
            path.unlink()
            removed += 1
    for path in sorted(OUT_DIR.rglob("*"), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    return removed


def main() -> None:
    global OUT_DIR

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db", type=Path, default=help_db.DB_FILE)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    parser.add_argument("--check", action="store_true",
                        help="generate and report, then remove the output again")
    args = parser.parse_args()

    OUT_DIR = args.out

    if not args.db.exists() and not help_db.sql_path_for(args.db).exists():
        print("No Discord archive yet; writing an empty help section.")
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "_index.md").write_text(render_index_page([]), encoding="utf-8")
        return

    conn = help_db.ensure_db(args.db)
    optout = load_optout()

    threads = load_threads(conn, optout)
    related = compute_related(threads)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    keep: set[Path] = set()

    for i, thread in enumerate(threads, 1):
        path = OUT_DIR / f"{thread['slug']}.md"
        path.write_text(render_thread_page(thread, related[thread["id"]]),
                        encoding="utf-8")
        keep.add(path)
        if i % 100 == 0:
            print(f"  [{i}/{len(threads)}] pages written")

    index = OUT_DIR / "_index.md"
    index.write_text(render_index_page(threads), encoding="utf-8")
    keep.add(index)

    removed = prune_stale(keep)
    print(f"Done: {len(threads)} thread pages, {removed} stale pages removed.")

    if args.check:
        prune_stale(set())
        print("Check mode: generated output removed again.")


if __name__ == "__main__":
    main()
