# Instagram saved → Obsidian

Turn the posts you save on Instagram into useful notes, every day, automatically.

You save a reel because it shows a GitHub repo, a tool, a place to visit, a quote that hit you or a song.
Then it sits in «Saved» forever. This script reads your **saved collections** (read-only), works out
what each post is actually about and files it in your **Obsidian** vault, with an optional
**Home Assistant** notification for the ones you want to act on.

| Mode | What you get in the note |
| --- | --- |
| `idea` | What the tool/repo is (exact URL), how it fits **your** setup, concrete implementation steps, effort/value. Non-tech posts are skipped. |
| `plan` | What, where (with a map link), when, price, practical tips. |
| `personal` | The exact quote / key sentence and the core message. |
| `music` | Artist, song and a link to listen. |
| `private` | One neutral, non-explicit line. |

Each collection is mapped to a mode in `config.json`, in priority order.

## How it works

1. **Instagram, read-only** — [instagrapi](https://github.com/subzeroid/instagrapi) with a saved session
   (no password stored, no likes/follows/comments, human-like pauses). The full list of each collection is
   read once and cached; daily runs only fetch the newest page.
2. **Understanding the post** — caption + transcript of the audio with
   [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (GPU if available) + a few frames, so
   on-screen text such as repo names or addresses is read too.
3. **Brain** — [Claude Code](https://docs.anthropic.com/en/docs/claude-code) in headless mode
   (`claude -p` with a JSON schema, can web-search to confirm a repo or a place). If it fails, for example
   because you are out of usage, it falls back to a local model in [Ollama](https://ollama.com).
4. **Output** — one Markdown note per post with front-matter, one index note per mode (an Obsidian `base`
   table), and one daily Home Assistant notification for `idea`/`plan` items.

Every run processes all **new** saves plus up to `backlog_per_run` older ones, so a big backlog is
worked through gradually instead of hammering Instagram (or your Claude usage) in one go.

## Setup

```bash
git clone https://github.com/mauriporte/instagram-saved-to-obsidian
cd instagram-saved-to-obsidian
python -m venv venv && venv/Scripts/pip install -r requirements.txt    # Linux/macOS: venv/bin/pip
# NVIDIA GPU (optional): venv/Scripts/pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12>=9,<10"
cp config.example.json config.json   # then edit it
```

- **Instagram session**: log into instagram.com in your browser, copy the `sessionid` cookie and run
  `python import_session.py`. (`login.py` does a username/password/2FA login instead; the password is not saved.)
- **Claude Code** must be installed and logged in (`claude` on PATH). Ollama is optional.
- **Home Assistant** (optional): a long-lived token in the environment variable named in
  `home_assistant.token_env` (default `HA_TOKEN`).

Try it without writing anything:

```bash
python ideas.py --dry-run --backlog 2
```

Then schedule it daily (Windows Task Scheduler, cron, …): `python ideas.py`.

## Configuration (`config.json`)

| Key | Meaning |
| --- | --- |
| `owner`, `language` | Who saved the posts and the language of the notes. |
| `profile` | A few lines about you and your setup, so ideas are judged for fit. |
| `vault`, `notes_subfolder`, `home_note` | Where the notes go; `home_note` (optional) gets a link to each index. |
| `assets_subfolder`, `picture_modes` | Where saved pictures go (default `90-assets/instagram`) and which modes show them (default plan, personal, music; videos give one frame, carousels up to 4). |
| `collections` | `[collection name, mode]` pairs in priority order (names match ignoring case/accents). |
| `modes` | Optional overrides per mode: `folder`, `emoji`, `notify`, `about`. |
| `backlog_per_run`, `max_new_per_run` | How many older / new items per run. |
| `order` | `"oldest"` (default) or `"newest"`: which saves go first. `--oldest-first` / `--newest-first` override it for one run. |
| `scan_button` | Optional Home Assistant `input_button`: pressing it makes `links.py` start a scan (if none is running). Progress is published as `sensor.insta_ideas_escaneo`. |
| `continuous_switch` | Optional Home Assistant `input_boolean`: while on, `links.py` starts a new scan as soon as the last one ends (reusing an Instagram sync less than 6 h old); it turns itself off when a scan completes nothing. |
| `collection_entity` | Optional Home Assistant `input_select`: 1st option = all collections (in priority order), the others = one collection name each; scans then sync and process only that collection. `--only NAME` overrides it. |
| `order_entity` | Optional Home Assistant `input_select` that overrides `order`: its 1st option means oldest first, its 2nd newest first. |
| `home_assistant`, `ollama` | URLs, token variable and fallback model. |

## Notes

- Uses Instagram's private API through instagrapi; keep the frequency low (once a day) and read-only.
  Use at your own risk with your own account.
- Note headings are in Spanish for now (`Qué es`, `Cómo implementarlo`, …); PRs welcome.

## License

MIT

## Links → ideas (`links.py`)

Send any link and it is filed as an idea, with the same analysis, note format and per-idea notification:

- **Inputs** (`config.json` → `links`):
  - `todo`: a Home Assistant to-do list (e.g. a Local To-do called "Ideas: enlaces"). Add an item with a link from
    the app or the web; it is ticked when processed, with the idea's title added to it.
  - `beeper_chats` + `beeper_token`: chat titles in Beeper Desktop, e.g. Beeper's own "Note to self" (on the phone
    too). Every new link posted there is processed and answered with "💡 Idea guardada: …". Covers every network
    Beeper bridges, no bots needed. Get the token with `python beeper_auth.py` (OAuth with Beeper Desktop: approve
    the request it opens; the token lasts 30 days, and links.py sends a Home Assistant notification when it expires).
- **Per link**: Instagram posts use the saved session; anything yt-dlp downloads (YouTube, TikTok, X…) is transcribed
  (audio) and sampled (frames), downloaded as separate audio and video files so no ffmpeg is needed; other pages
  (articles, GitHub) are read by Claude with WebFetch. Links are always filed, even if judged not relevant.
- **Run**: `python links.py` keeps watching (every `interval` seconds, default 120; Whisper is released between
  links); `--once` for a single pass; `--url <link>` for one link. `links-watch.vbs` starts it hidden at logon (put a
  copy in the Startup folder).

```json
"links": {"todo": "todo.ideas_enlaces", "collection": "Enlaces", "interval": 120,
          "beeper_url": "http://localhost:23373", "beeper_token": "", "beeper_chats": ["Note to self"]}
```

## Ideas outside Obsidian (`vault_sync.py`)

Run by `links.py` on every cycle (or once with `python vault_sync.py`):
- **Home Assistant list** (`config.json` → `ideas_todo`, e.g. a Local To-do "Ideas"): one item per idea note, with
  summary, effort/value and link. Ticking it marks the note `hecha`; deleting it marks it `descartada`; closing the note
  in Obsidian (status hecha/descartada) ticks the item.
- **`20-dashboards/Dashboards.md`**: the ideas and projects as plain Markdown tables (ideas to review, quick wins, all,
  projects), for viewers that do not run Obsidian Bases, such as Home Assistant's notes panel. Rewritten only when it
  changes.
- **`sensor.ideas_nuevas`** (pushed to Home Assistant every cycle): the number of open ideas, with each one's title,
  summary, effort/value and its vault path. A Markdown card can list them as links that open the note in the
  [Notes Vault](https://github.com/FezVrasta/ha-notes-vault) panel: `[{{ i.title }}](/notes-vault?path={{ i.path | urlencode }})`.
