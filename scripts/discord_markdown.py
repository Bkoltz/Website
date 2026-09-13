#!/usr/bin/env python3
"""
Convert Discord-flavored markdown into safe HTML for the public help pages.

This module is the security boundary of the whole feature: everything it
receives is untrusted text written by strangers, and everything it emits is
injected into webodm.org. The order of operations below is load-bearing --
code spans are extracted first, then the remainder is HTML-escaped, and only
after that are our own tags introduced. Nothing user-supplied is ever emitted
unescaped.

Requirements: none (standard library only)
"""

import html
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Languages we are willing to echo into a class attribute.
CODE_LANGUAGES = {
    "bash", "sh", "shell", "console", "python", "py", "json", "yaml", "yml",
    "javascript", "js", "typescript", "ts", "html", "xml", "css", "sql", "c",
    "cpp", "csharp", "java", "go", "rust", "ruby", "php", "ini", "toml",
    "diff", "docker", "dockerfile", "makefile", "text", "log",
}

# Links to our own properties are real endorsements; everything else is UGC.
INTERNAL_HOSTS = {
    "webodm.org", "www.webodm.org", "docs.webodm.org", "swag.webodm.org",
    "github.com/WebODM",
}

SAFE_SCHEMES = {"http", "https"}

# Invisible and direction-controlling characters. Bidi overrides can visually
# reverse a URL, so they are stripped rather than escaped.
INVISIBLE_RE = re.compile(
    "[​-‏‪-‮⁦-⁩﻿­]"
)

EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")
CREDENTIAL_RES = [
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"(?i)\b(?:password|passwd|token|secret|api[_-]?key)\s*[=:]\s*\S{6,}"),
]
FOREIGN_INVITE_RE = re.compile(r"(?i)\b(?:https?://)?(?:discord\.gg|discord\.com/invite)/([\w-]+)")
WEBODM_INVITE_CODES = {"RxHPXCSMBS"}

SENTINEL = "\x00"


@dataclass
class RenderContext:
    """Lookup tables used to resolve Discord's id-based inline references."""

    users: dict[str, str] = field(default_factory=dict)
    channels: dict[str, str] = field(default_factory=dict)
    roles: dict[str, str] = field(default_factory=dict)
    emoji: dict[str, str] = field(default_factory=dict)
    message_anchors: dict[str, str] = field(default_factory=dict)


def _normalize(text: str) -> str:
    """Unicode-normalize and strip invisible/bidi characters."""
    text = unicodedata.normalize("NFC", text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return INVISIBLE_RE.sub("", text)


def _redact(text: str) -> str:
    """Remove personal contact details and credential-shaped strings."""
    text = EMAIL_RE.sub("[email removed]", text)
    for pattern in CREDENTIAL_RES:
        text = pattern.sub("[redacted]", text)

    def _invite(match: re.Match) -> str:
        return match.group(0) if match.group(1) in WEBODM_INVITE_CODES else "[invite link removed]"

    return FOREIGN_INVITE_RE.sub(_invite, text)


def _extract_code(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Replace code spans with sentinels so later passes cannot reach inside."""
    blocks: list[tuple[str, str]] = []

    def take_fenced(match: re.Match) -> str:
        lang = (match.group(1) or "").strip().lower()
        blocks.append((lang if lang in CODE_LANGUAGES else "", match.group(2)))
        return f"{SENTINEL}B{len(blocks) - 1}{SENTINEL}"

    def take_inline(match: re.Match) -> str:
        blocks.append((None, match.group(1)))
        return f"{SENTINEL}B{len(blocks) - 1}{SENTINEL}"

    text = re.sub(r"```([A-Za-z0-9+#_-]*)\n?(.*?)```", take_fenced, text, flags=re.S)
    text = re.sub(r"``(.+?)``", take_inline, text, flags=re.S)
    text = re.sub(r"`([^`\n]+?)`", take_inline, text)
    return text, blocks


def _restore_code(text: str, blocks: list[tuple[str, str]]) -> str:
    """Re-insert extracted code spans, escaped, as <pre>/<code> elements."""

    def put(match: re.Match) -> str:
        lang, body = blocks[int(match.group(1))]
        escaped = html.escape(body, quote=True)
        if lang is None:
            return f"<code>{escaped}</code>"
        cls = f' class="language-{lang}"' if lang else ""
        return f"<pre><code{cls}>{escaped.strip(chr(10))}</code></pre>"

    return re.sub(rf"{SENTINEL}B(\d+){SENTINEL}", put, text)


def _is_internal(url: str) -> bool:
    """True when a URL points at a WebODM property."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    host = parts.netloc.lower()
    return host in INTERNAL_HOSTS or f"{host}{parts.path}".lower().startswith("github.com/webodm")


def _anchor(url: str, label: str) -> str:
    """Build an <a> for an already-escaped label, or plain text if unsafe."""
    from urllib.parse import urlsplit

    try:
        scheme = urlsplit(html.unescape(url)).scheme.lower()
    except ValueError:
        return label
    if scheme not in SAFE_SCHEMES:
        # javascript:, data:, vbscript: and friends never become links.
        return label
    href = html.escape(html.unescape(url), quote=True)
    if _is_internal(html.unescape(url)):
        return f'<a href="{href}">{label}</a>'
    return f'<a href="{href}" rel="nofollow ugc noopener" target="_blank">{label}</a>'


def _inline(text: str, ctx: RenderContext) -> str:
    """Apply inline formatting to already-escaped text."""
    # Markdown links first, so their URLs are not also caught as bare URLs.
    def md_link(match: re.Match) -> str:
        return _anchor(match.group(2), match.group(1))

    text = re.sub(r"\[([^\]\n]{1,200})\]\(([^)\s]{1,2000})\)", md_link, text)
    text = re.sub(
        r"&lt;(https?://[^\s&]{1,2000})&gt;",
        lambda m: _anchor(m.group(1), m.group(1)),
        text,
    )
    text = re.sub(
        r'(?<!href=")(?<!>)\bhttps?://[^\s<>"\']{1,2000}',
        lambda m: _anchor(m.group(0).rstrip(".,;:!?)"), m.group(0).rstrip(".,;:!?)"))
        + m.group(0)[len(m.group(0).rstrip(".,;:!?)")):],
        text,
    )

    # Discord entities. Angle brackets are already escaped at this point.
    text = re.sub(
        r"&lt;@!?(\d{1,25})&gt;",
        lambda m: '<span class="d-mention">@'
        + html.escape(ctx.users.get(m.group(1), "user"))
        + "</span>",
        text,
    )
    text = re.sub(
        r"&lt;@&amp;(\d{1,25})&gt;",
        lambda m: '<span class="d-mention">@'
        + html.escape(ctx.roles.get(m.group(1), "role"))
        + "</span>",
        text,
    )
    text = re.sub(
        r"&lt;#(\d{1,25})&gt;",
        lambda m: '<span class="d-mention">#'
        + html.escape(ctx.channels.get(m.group(1), "channel"))
        + "</span>",
        text,
    )

    def emoji(match: re.Match) -> str:
        name, eid = match.group(1), match.group(2)
        path = ctx.emoji.get(eid)
        alt = html.escape(f":{name}:", quote=True)
        if not path:
            return alt
        return (
            f'<img class="d-emoji" src="{html.escape(path, quote=True)}" alt="{alt}" '
            'width="20" height="20" loading="lazy">'
        )

    text = re.sub(r"&lt;a?:([A-Za-z0-9_]{1,32}):(\d{1,25})&gt;", emoji, text)

    def timestamp(match: re.Match) -> str:
        try:
            dt = datetime.fromtimestamp(int(match.group(1)), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return match.group(0)
        return f'<time datetime="{dt.isoformat()}">{dt.strftime("%B %-d, %Y %H:%M UTC")}</time>'

    text = re.sub(r"&lt;t:(-?\d{1,12})(?::[tTdDfFR])?&gt;", timestamp, text)

    # Mass pings must not survive into a public mirror.
    text = re.sub(r"@(everyone|here)\b", r"\1", text)

    # Emphasis, longest delimiter first so *** is not eaten by *.
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"<strong><em>\1</em></strong>", text, flags=re.S)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text, flags=re.S)
    text = re.sub(r"__(.+?)__", r"<u>\1</u>", text, flags=re.S)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", text, flags=re.S)
    text = re.sub(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", r"<em>\1</em>", text, flags=re.S)
    text = re.sub(r"~~(.+?)~~", r"<del>\1</del>", text, flags=re.S)
    text = re.sub(
        r"\|\|(.+?)\|\|",
        r'<span class="d-spoiler" tabindex="0">\1</span>',
        text,
        flags=re.S,
    )
    return text


def _blocks(text: str) -> str:
    """Turn line-oriented markdown into block elements."""
    out: list[str] = []
    para: list[str] = []
    list_tag: str | None = None
    in_quote = False

    def flush_para() -> None:
        if para:
            out.append("<p>" + "<br>".join(para) + "</p>")
            para.clear()

    def flush_list() -> None:
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    def flush_quote() -> None:
        nonlocal in_quote
        if in_quote:
            out.append("</blockquote>")
            in_quote = False

    for raw in text.split("\n"):
        line = raw.rstrip()
        stripped = line.strip()

        if not stripped:
            flush_para(); flush_list(); flush_quote()
            continue

        # A fenced block standing alone must not end up inside a <p>.
        if re.fullmatch(rf"{SENTINEL}B\d+{SENTINEL}", stripped):
            flush_para(); flush_list(); flush_quote()
            out.append(stripped)
            continue

        quote = re.match(r"^&gt;(?:&gt;&gt;)?\s?(.*)$", stripped)
        if quote:
            flush_para(); flush_list()
            if not in_quote:
                out.append("<blockquote>")
                in_quote = True
            out.append("<p>" + quote.group(1) + "</p>")
            continue
        flush_quote()

        heading = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if heading:
            flush_para(); flush_list()
            # User content never emits h1/h2: those belong to the page outline.
            level = 2 + len(heading.group(1))
            out.append(f"<h{level}>{heading.group(2)}</h{level}>")
            continue

        item = re.match(r"^(?:[-*]|\d{1,3}\.)\s+(.*)$", stripped)
        if item:
            flush_para()
            want = "ol" if re.match(r"^\d", stripped) else "ul"
            if list_tag != want:
                flush_list()
                out.append(f"<{want}>")
                list_tag = want
            out.append(f"<li>{item.group(1)}</li>")
            continue
        flush_list()

        para.append(stripped)

    flush_para(); flush_list(); flush_quote()
    return "\n".join(out)


def render(content: str, ctx: RenderContext | None = None) -> str:
    """Render Discord markdown to safe HTML."""
    ctx = ctx or RenderContext()
    text = _redact(_normalize(content))
    text, code = _extract_code(text)
    text = html.escape(text, quote=True)
    text = _inline(text, ctx)
    text = _blocks(text)
    text = _restore_code(text, code)
    return text.strip()


def plain_text(content: str, ctx: RenderContext | None = None, limit: int | None = None) -> str:
    """
    Render to plain text, for meta descriptions and JSON-LD.

    Runs the same normalization and redaction as render() so nothing leaks
    through a channel that skips HTML escaping.
    """
    ctx = ctx or RenderContext()
    text = _redact(_normalize(content))
    text = re.sub(r"```[A-Za-z0-9+#_-]*\n?.*?```", " ", text, flags=re.S)
    text = re.sub(r"`([^`\n]+?)`", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"<@!?(\d{1,25})>", lambda m: "@" + ctx.users.get(m.group(1), "user"), text)
    text = re.sub(r"<@&(\d{1,25})>", lambda m: "@" + ctx.roles.get(m.group(1), "role"), text)
    text = re.sub(r"<#(\d{1,25})>", lambda m: "#" + ctx.channels.get(m.group(1), "channel"), text)
    text = re.sub(r"<a?:([A-Za-z0-9_]{1,32}):\d{1,25}>", r"\1", text)
    text = re.sub(r"<t:-?\d{1,12}(?::[tTdDfFR])?>", " ", text)
    text = re.sub(r"<[^>]{0,200}>", " ", text)
    text = text.replace("<", " ").replace(">", " ")
    text = re.sub(r"[*~|`#]+", "", text)
    text = re.sub(r"(?<!\w)_+|_+(?!\w)", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        cut = text[:limit]
        if " " in cut:
            cut = cut[: cut.rindex(" ")]
        text = cut.rstrip(".,;:!?-") + "…"
    return text
