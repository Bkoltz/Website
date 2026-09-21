# WebODM Website

The official website for [WebODM](https://webodm.org), built with [Zola](https://www.getzola.org/), a fast static site generator written in Rust.

## Prerequisites

- [Zola](https://www.getzola.org/documentation/getting-started/installation/) (v0.17+)
- Git

### Installing Zola

See the [official installation docs](https://www.getzola.org/documentation/getting-started/installation/).

## Getting Started

```bash
git clone https://github.com/WebODM/webodm-web.git
cd webodm-web
zola serve
```

**Open your browser** at [http://127.0.0.1:1111](http://127.0.0.1:1111).

The site will live-reload automatically whenever you save a file.

## Project Structure

```
webodm-web/
├── config.toml        # Zola site configuration
├── start.sh           # Helper script to run the dev server
├── content/           # Markdown pages and blog posts
│   └── blog/          # Blog entries
├── data/              # JSON data files (e.g. datasets) + the Discord archive
├── public/            # Pre-built static assets (CSS, images)
├── static/            # Static files copied as-is to the output
├── templates/         # Tera HTML templates
│   ├── base.html      # Base layout
│   ├── index.html     # Homepage
│   ├── page.html      # Generic page template
│   ├── download.html  # Download page
│   └── datasets.html  # Datasets page
└── themes/            # Zola themes (if any)
```

## Discord help archive (`/community/help/`)

Question-and-answer threads from the `#help` channel of the
[WebODM Discord](https://discord.gg/RxHPXCSMBS) are mirrored into a SQLite
archive and published as one static page per thread, so the answers are
findable by search engines.

```
Discord REST API
      │  scripts/sync_discord.py       (weekly, or on demand)
      ▼
data/discord.sql.enc                   committed archive (encrypted)
static/images/help/                    committed mirrored images
      │  scripts/build_help_pages.py   (runs on every build)
      ▼
content/community/help/*.md            generated, gitignored
      │  zola build
      ▼
public/community/help/<slug>/
```

Attachment URLs on Discord's CDN are signed and expire after about 24 hours, so
images are downloaded, resized and re-encoded to WebP at sync time rather than
hot-linked. Avatars are mirrored for the same reason.

The expiring `ex`/`is`/`hm` signature is deliberately not stored in
`data/discord.sql` -- it would be stale noise in every diff -- so
`attachments.discord_url` holds the bare URL. The CDN answers **404** to an
unsigned attachment URL, so the media pass re-signs in batches of 50 through
`POST /attachments/refresh-urls` immediately before downloading. Avatar URLs are
built from the user id and hash and need no signature, which is why a broken
sync shows every avatar succeeding and every attachment failing.

### Running it locally

No Discord token is needed to build the site: `build_help_pages.py` reads only
the committed archive.

```bash
python scripts/build_help_pages.py    # regenerate the pages
zola build                            # or just ./start.sh
```

To exercise the crawler without a token, replay the recorded fixtures:

```bash
python scripts/sync_discord.py --fixtures scripts/fixtures/discord \
    --db /tmp/test.sqlite3 \
    --guild-id 900000000000000001 --channel-id 900000000000000002
```

`--db` moves the text dump with it -- that run writes `/tmp/test.sql`, never the
committed `data/discord.sql` -- so a fixture run cannot disturb the real
archive. `build_help_pages.py --db` and `help_db.py [dump|restore] <db_path>`
follow the same rule.

To run a real sync you need the bot token (see below):

```bash
export DISCORD_BOT_TOKEN=...
python scripts/sync_discord.py --limit 5 --dry-run   # crawl, write nothing
python scripts/sync_discord.py --limit 5             # small real sync
python scripts/sync_discord.py                       # incremental, all threads
python scripts/sync_discord.py --full                # re-read every thread
```

Run the renderer tests with `python -m unittest discover -s scripts -p 'test_*.py'`.

### Search

The help index carries an autocomplete search box backed by
[Pagefind](https://pagefind.app), the same mechanism the Astro documentation
uses: a static index built from the rendered HTML at deploy time and queried
entirely in the browser, with no search backend and no API key.

Only the thread pages are indexed. `data-pagefind-body` on the thread template
is what scopes it -- once that attribute appears anywhere on a site, Pagefind
indexes *only* pages that carry it, so the marketing pages stay out of the
results by construction.

`zola serve` never writes a `public/` directory, so there is no index during
normal authoring and the search box stays hidden rather than silently returning
nothing. To exercise search locally, build the site and index it:

```bash
python scripts/build_help_pages.py
zola build
npx pagefind --site public
python3 -m http.server -d public 2222   # http://127.0.0.1:2222/community/help/
```

In CI this is the `Build search index` step in `deploy.yml`, which has to run
after `zola build` and before the artifact upload.

### One-time Discord setup

1. Create an application at <https://discord.com/developers/applications>.
2. **Bot** tab → copy the token → add it as the repository secret
   **`DISCORD_BOT_TOKEN`**.
3. **Bot** tab → turn **Public Bot** off.
4. **Bot** tab → **Privileged Gateway Intents** → enable **Message Content
   Intent**. This is self-serve below 10,000 users, and it is required: the
   intent gates the REST API too, not only the Gateway. Without it Discord
   returns empty message content with no error. (`sync_discord.py` detects this
   and aborts rather than publishing blank pages.)
5. Invite the bot — a server admin with `MANAGE_GUILD` has to do this:
   `https://discord.com/oauth2/authorize?client_id=<APP_ID>&scope=bot&permissions=66560`
   Permission `66560` is `VIEW_CHANNEL` + `READ_MESSAGE_HISTORY`; the bot cannot
   post anything.
6. Check that the bot's role really has *View Channel* and *Read Message
   History* on `#help` — a channel-level overwrite can deny what the
   server-wide role grants.
7. Enable Developer Mode in Discord, right-click the server and `#help` →
   **Copy ID**, then set the IDs in `data/discord_config.json`. The scheduled
   GitHub Actions workflow reads that file at runtime.

### What gets published

Only threads that earned a page: at least one substantive reply from someone
other than the person who asked, a question of at least 80 characters, and at
least 350 characters of discussion overall. Thin threads are skipped entirely
rather than marked `noindex`, which also keeps them out of the sitemap.

### The committed archive is encrypted

`data/discord.sql.enc` is what git carries. The plaintext `data/discord.sql` is
a local working copy and is gitignored.

The reason is the removal process below. Removal is retroactive for the site and
for the current dump, but git history is permanent: a plaintext archive would
keep every message ever synced readable by anyone who clones the repository,
including messages later taken down on request. Encrypting the committed file
narrows that from "public forever" to "readable by key holders".

Generate a key once and store it as the `DISCORD_ARCHIVE_KEY` repository secret:

```bash
python scripts/archive_crypto.py keygen
```

Export the same value locally to work with the archive. Without it,
`build_help_pages.py` says so and writes an empty help section, so the rest of
the site still builds for contributors who do not hold the key.

The whole file is encrypted as one unit with AES-256-GCM, gzipped first (1.1 MB
-> 193 KB, so a weekly commit costs about 193 KB rather than 1.1 MB). It is
deliberately *not* encrypted line by line: that would preserve git's line diffs,
but only by being deterministic, so every commit would advertise exactly which
rows changed -- which, correlated against the live site, reveals what was
removed and when. See the module docstring in `scripts/archive_crypto.py`.

Losing the key is recoverable but tedious: the archive is a mirror, so
`python scripts/sync_discord.py --full` rebuilds it from Discord.

### Removing someone's content

Add their Discord username to `usernames` in `data/discord_optout.json`, or a
single thread to `thread_ids`, and the next sync redacts their messages and
drops any thread they started, retroactively, along with the mirrored images.
Username matching is case-insensitive and also tests the Discord global name.

```json
{ "usernames": ["someone"], "thread_ids": ["1234567890"] }
```

For an immediate hard delete, which removes the message rows and unlinks the
mirrored image files rather than waiting for the next sync:

```bash
python scripts/sync_discord.py --purge-user <discord_user_id>
```

The purge takes a numeric user id, not a username, since it addresses rows that
are already stored. It is not a substitute for the opt-out list: add the person
to `usernames` as well, or a later sync will mirror them again.

Messages deleted on Discord disappear from the site at the next sync
automatically. The `/community/help/` page explains this to readers and gives a
route for removal requests, as Discord's developer terms require.

### Automation

`.github/workflows/sync-discord.yml` runs the sync every Monday (and on manual
dispatch), commits the archive, then calls `deploy.yml` as a reusable workflow.
The explicit call is deliberate: a push made with the default `GITHUB_TOKEN`
does not trigger another workflow's `on: push`, so a plain commit would never
deploy.

## Building for Production

To generate the static site into the `public/` directory:

```bash
zola build
```

## Contributing

1. Fork the repository and create a feature branch.
2. Run `zola serve` to preview your changes locally.
3. Commit your changes and open a pull request.

## License

See the repository for license details.
