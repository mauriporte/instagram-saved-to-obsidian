# Daily: read your Instagram saved collections (READ-ONLY, saved session), understand each item
# (caption + Whisper transcript + a few frames) and file it in an Obsidian vault according to the collection's mode:
#   idea      -> implementation idea for your setup (non-tech posts skipped) + Home Assistant notification
#   plan      -> what / where / when / price + Home Assistant notification
#   personal  -> the core message or quote, faithful; Obsidian only
#   music     -> song + artist + link; Obsidian only
#   private   -> one neutral line, separate folder; Obsidian only
# Every run: all NEW saves (since the first run) + up to `backlog_per_run` older items, in collection priority order.
# Brain: Claude Code headless (your subscription); if it fails (e.g. out of usage) -> local Ollama.
# Everything personal lives in config.json (see config.example.json). Run: python ideas.py [--backlog N] [--only NAME] [--newest-first | --oldest-first] [--continuous] [--dry-run]
import base64, datetime as dt, json, os, random, re, shutil, subprocess, sys, tempfile, time, traceback, unicodedata, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
SESSION = os.path.join(HERE, "session.json")
STATE = os.path.join(HERE, "state.json")
LOG = os.path.join(HERE, "ideas.log")
CFG_PATH = os.path.join(HERE, "config.json")
if not os.path.exists(CFG_PATH):
    sys.exit("config.json not found: copy config.example.json to config.json and edit it")
CFG = json.load(open(CFG_PATH, encoding="utf-8"))
arg = lambda k, d: sys.argv[sys.argv.index(k) + 1] if k in sys.argv else d
BACKLOG = int(arg("--backlog", CFG.get("backlog_per_run", 15)))  # older items per run, all collections together
MAX_NEW = int(CFG.get("max_new_per_run", 25))                    # safety cap for new saves per run
LIMIT = int(arg("--limit", 0)) or None                           # --limit N: at most N posts this run (tests)
ONLY = arg("--only", None)
DRY = "--dry-run" in sys.argv
MAX_TRIES = 3  # a post whose download/analysis fails this many times is set aside (state.json "failed")
CONTINUOUS = "--continuous" in sys.argv  # started by links.py's continuous switch: reuse a recent Instagram sync
ORDER_ARG ="newest" if "--newest-first" in sys.argv else "oldest" if "--oldest-first" in sys.argv else None
VAULT = CFG["vault"]
NOTES_DIR = CFG.get("notes_subfolder", "")          # folder inside the vault for the per-mode folders
BASE = os.path.join(VAULT, NOTES_DIR)
HOME_NOTE = CFG.get("home_note")                    # optional: note that gets a link to each index (e.g. "Home.md")
ASSETS = os.path.join(VAULT, CFG.get("assets_subfolder", "90-assets/instagram"))  # saved pictures shown in the notes
PICTURE_MODES = set(CFG.get("picture_modes", ["plan", "personal", "music"]))      # which modes get the picture
HA = CFG.get("home_assistant", {}).get("url")       # optional
HA_TOKEN_ENV = CFG.get("home_assistant", {}).get("token_env", "HA_TOKEN")
OLLAMA = CFG.get("ollama", {}).get("url", "http://localhost:11434")
OLLAMA_MODEL = CFG.get("ollama", {}).get("model", "qwen3:8b")
OWNER = CFG.get("owner", "The user")
LANGUAGE = CFG.get("language", "English")
CONTEXT = CFG.get("profile", "")                    # who you are / your setup, so ideas are judged for fit
# priority order; names matched case/accent/whitespace-insensitive («planes\n», «Música»)
COLLECTIONS = [tuple(c) for c in CFG["collections"]]

MODES = {
    "idea": {"folder": "Ideas Instagram", "emoji": "💡", "notify": True, "skip_irrelevant": True,
             "about": "ideas para implementar",
             "task": """{owner} saved this because it showed something useful (AI, GitHub, coding-assistant skills,
tools). Figure out exactly WHAT it is and turn it into an implementation idea for their setup. If a repo/tool/skill is
named, identify it precisely (owner/repo, official URL) — you may use WebSearch/WebFetch to confirm. Be honest: if it is
hype or does not fit, say so. relevant=false if it is NOT about AI, software, GitHub, tools or tech they could implement.
Fill: what (2-4 sentences), fit (fit with their setup), steps (concrete), effort, value,
claude (could this be applied to or taught to Claude Code — as a skill, MCP server, CLAUDE.md rule, hook or slash
command? say which and how in 1-3 sentences, or why not), ha (could it be added to their Home Assistant setup —
integration, automation, dashboard, add-on? how, or why not), pros (list), cons (list: costs, risks, maintenance,
overlap with what they already have)."""},
    "plan": {"folder": "Planes Instagram", "emoji": "📍", "notify": True,
             "about": "planes, sitios y cosas que hacer",
             "task": """{owner} saved this as a PLAN (a place, restaurant, trip, event, activity, something to do).
Identify the exact place/event with WebSearch/WebFetch (venue name, full address, official URL). Fill: what, where
(venue name + full address), when (dates or opening hours), price (price range or ticket price), url (official site),
maps (a Google Maps link for that exact place: https://www.google.com/maps/search/?api=1&query=<venue name and address,
URL-encoded>), rating (e.g. "Google 4.6 (2,300 reseñas)" if you can confirm it, else ""), reviews (2-4 items, each
"Source — rating if any — one-sentence summary — URL", ONLY from reputable sources you actually found: Google Maps,
TripAdvisor, Michelin Guide, Guía Repsol, well-known newspapers/magazines; never invent reviews; [] if none found),
steps (practical tips as a list: booking, best time, what to order/see).
kind: "event" if it happens on specific dates (concert, festival, exhibition, market, opening, show, limited-time
thing), "place" for a permanent venue (restaurant, bar, shop, viewpoint), "activity" otherwise. For an EVENT the date is
MANDATORY: `when` must contain the exact date(s) — day, month and year — and the time if known, found on the official
site or ticketing pages (WebSearch/WebFetch). If it already happened, still give its date and add "(ya pasó)"; if it
recurs (yearly festival, weekly market), give the next edition's date. For an EVENT also fill start and end in ISO 8601
local time: "YYYY-MM-DDTHH:MM" when the time is known, else "YYYY-MM-DD" (end = last day for multi-day events; "" if
unknown); for anything else leave start and end empty."""},
    "personal": {"folder": "Personal Instagram", "emoji": "🪞", "notify": False,
                 "about": "reflexiones, frases y cosas personales",
                 "task": """{owner} saved this for personal reasons: a quote, a reflection, something deep or meaningful
to them: a saying, phrase, poem or reflection. TRANSCRIBE it completely and faithfully: quote = the whole text exactly as
it appears on the image(s)/frames or is spoken (every line of a poem, line breaks kept, original language, no
paraphrasing; open the images with the Read tool to read them), plus the author after a final line "— Author" if shown.
what = the core message in 1-3 sentences, fit = in one or two sentences why it may resonate (gentle, no therapy talk)."""},
    "music": {"folder": "Música Instagram", "emoji": "🎵", "notify": False,
              "about": "canciones y artistas guardados",
              "task": """{owner} saved this for the MUSIC. Identify the song being played/referenced and the artist
(caption, on-screen text, lyrics in the transcript; WebSearch lyrics or the track if needed). Fill: artist, song,
what (genre/vibe in one sentence). url = a direct link to that track you actually found: Spotify
(https://open.spotify.com/track/...) first; if it is not on Spotify, Bandcamp or Mixcloud (a DJ set/mix); "" if none.
artist_url = the artist's page: Spotify artist page (https://open.spotify.com/artist/...), else AllMusic; "".
tour = upcoming concerts/festival dates in EUROPE you can confirm on the artist's site, Songkick, Bandsintown or a
ticketing site, one item each as "date (day month year) — city, country — venue — ticket/info URL"; [] if none found;
never invent dates. If the post is itself about a specific EVENT (a concert, festival, club night): kind = "event",
where = venue + full address, when = exact date(s) with year, maps = a Google Maps link for the venue
(https://www.google.com/maps/search/?api=1&query=<venue and address, URL-encoded>); otherwise leave those empty."""},
    "food": {"folder": "Food Instagram", "emoji": "🍳", "notify": False,
             "about": "recetas y sugerencias de comida",
             "task": """{owner} saved this as FOOD: a recipe or a food suggestion. Write the recipe in text (in the
note's language): title = the dish; what = a short description of the dish and its origin (1-2 sentences);
ingredients = every ingredient with its quantity, one item each (from the caption, the on-screen text in the images and
the spoken transcript; open the images with the Read tool); steps = the preparation, one step per item, with times and
temperatures; price = "" unless stated. If the post doesn't give the full recipe, complete it from a reputable recipe
source (WebSearch/WebFetch) and say so in the first step. Cross-check the dish with other versions: variants = other
known versions of the same dish, one item each as "Country/region — how it differs" (e.g. "Italia — sin nata, con
guanciale"); [] if it has no notable variants. url = the source recipe if you used one, else ""."""},
    "learn": {"folder": "Learn Instagram", "emoji": "🎓", "notify": False,
              "about": "cursos y cosas que aprender",
              "task": """{owner} saved this to LEARN: a course, workshop, tutorial series, book or topic to study. Identify
it with WebSearch/WebFetch. Fill: what = a summary of what it teaches and for whom (2-3 sentences); url = the main
official link (course page, site, video or book page); github = its GitHub repository if there is one, else ""; signup =
the sign-up / enrolment / registration link if available, else ""; format = "online" (virtual), "presencial" (you have to
attend in person) or "mixto" (both), "" if unknown; price (free, price or "?"); when = dates, schedule or duration if any;
for presencial or mixto also where (venue + full address) and maps (a Google Maps link for the venue:
https://www.google.com/maps/search/?api=1&query=<venue and address, URL-encoded>); steps = what you would learn, as a short
list of topics. Only links you actually found; never invent them."""},
    "general": {"folder": "Otras Instagram", "emoji": "📌", "notify": False,
                "about": "otras colecciones",
                "task": """{owner} saved this in the collection «{collection}». Fill: what = a short description of what
the post shows or says and why it may be worth keeping (2-3 sentences); steps = its key points as a short list (the
things to remember, do or look at); url = the main link it refers to if there is one you can confirm, else "";
quote = the text of the post if it is short (under about 80 words), else ""."""},
    "home": {"folder": "Home Instagram", "emoji": "🏠", "notify": False,
             "about": "trucos y cosas que hacer en casa",
             "task": """{owner} saved this as a HOME TIP: something practical to do at home, nothing digital (cleaning,
maintenance, small repairs, organising, decor, cooking tricks). Fill: what = a brief description of the tip and why it
helps (1-2 sentences); steps = the tip as short, concrete steps (what to use, how, how often); quote = the full
transcript of the text/speech ONLY if it is short (under about 120 words, open the images with the Read tool to read
on-screen text), else "" (the steps are then the summary); price = rough cost of materials if any ("" if none)."""},
    "private": {"folder": "Privado Instagram", "emoji": "🔒", "notify": False,
                "about": "colección privada",
                "task": """This comes from a private collection. Write ONLY a short, neutral, non-explicit description
(one_liner and title), nothing graphic. Leave the other fields empty."""},
}
for _m, _v in (CFG.get("modes") or {}).items():  # optional per-mode overrides (folder, emoji, notify, about)
    MODES[_m].update(_v)

# CUDA DLLs shipped as pip wheels (nvidia-cublas-cu12 / nvidia-cudnn-cu12)
for sub in ("cublas", "cudnn"):
    d = os.path.join(HERE, "venv", "Lib", "site-packages", "nvidia", sub, "bin")
    if os.path.isdir(d):
        os.add_dll_directory(d); os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]

import requests  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows console is cp1252


def log(*a):
    line = dt.datetime.now().strftime("%F %T ") + " ".join(str(x) for x in a)
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def norm(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().strip().casefold()


def ha_token():
    t = os.environ.get(HA_TOKEN_ENV)
    if not t and os.name == "nt":  # Windows user-level env var (a scheduled task may not inherit it)
        t = subprocess.run(["powershell", "-NoProfile", "-c", f'[Environment]::GetEnvironmentVariable("{HA_TOKEN_ENV}","User")'],
                           capture_output=True, text=True).stdout.strip()
    return t


def run_collection():
    """--only NAME > Home Assistant selector ("collection_entity": 1st option = all collections, then one per name) > all."""
    if ONLY:
        return ONLY
    ent = CFG.get("collection_entity")
    if HA and ent:
        try:
            s = requests.get(f"{HA}/api/states/{ent}", headers={"Authorization": "Bearer " + ha_token()}, timeout=20).json()
            opts = s["attributes"]["options"]
            name = re.sub(r" · \d+$", "", s["state"])  # options carry the posts left: "Work · 49"
            if s["state"] in opts[1:] and any(norm(name) == norm(n) for n, _ in COLLECTIONS):
                return name
        except Exception as e:
            log(f"collection: {ent} unreadable ({type(e).__name__}), using all")
    return None


def run_order():
    """--oldest-first/--newest-first > Home Assistant selector ("order_entity": 1st option = oldest, 2nd = newest) > "order"."""
    if ORDER_ARG:
        return ORDER_ARG
    ent = CFG.get("order_entity")
    if HA and ent:
        try:
            s = requests.get(f"{HA}/api/states/{ent}", headers={"Authorization": "Bearer " + ha_token()}, timeout=20).json()
            return ("oldest", "newest")[s["attributes"]["options"].index(s["state"])]
        except Exception as e:
            log(f"order: {ent} unreadable ({type(e).__name__}), using config")
    order = CFG.get("order", "oldest")
    assert order in ("oldest", "newest"), f'config "order" must be "oldest" or "newest", not {order!r}'
    return order


SCAN_SENSOR = "sensor.insta_ideas_escaneo"
LAST_SCAN = os.path.join(HERE, "last_scan.json")


def scan_lock():
    """Held for the whole scan; the OS frees it if the process dies. Returns None if another scan holds it."""
    f = open(os.path.join(HERE, "scan.lock"), "a+")
    try:
        if os.name == "nt":
            import msvcrt; f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl; fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        f.close(); return None


def scan_running():
    f = scan_lock()
    if f:
        f.close()  # closing releases the lock
    return f is None


def progress(text, done=0, total=0, running=True):
    """Publishes the scan state as sensor.insta_ideas_escaneo (the Ideas dashboard's scan button)."""
    if DRY or not HA:
        return
    last = {}
    if not running:
        try:
            last = json.load(open(LAST_SCAN, encoding="utf-8"))
        except (OSError, ValueError):
            pass
    try:
        requests.post(f"{HA}/api/states/{SCAN_SENSOR}", headers={"Authorization": "Bearer " + ha_token()}, timeout=20, json={
            "state": text, "attributes": {"friendly_name": "Escaneo Insta-Ideas", "icon": "mdi:radar" if running else "mdi:play-circle",
                                          "running": running, "done": done, "total": total,
                                          "percent": round(100 * done / total) if total else 0, **last}})
    except Exception as e:
        log("scan sensor error:", type(e).__name__)


def notify(title, message, nid):
    if DRY or not HA:
        log("[notify]" if HA else "[no Home Assistant configured]", title, "|", message.replace("\n", " / ")); return
    r = requests.post(f"{HA}/api/services/persistent_notification/create", timeout=20,
                      headers={"Authorization": "Bearer " + ha_token()},
                      json={"title": title, "message": message, "notification_id": nid})
    log("HA notification", r.status_code)


CALENDAR = CFG.get("calendar") or {}  # {"entity": "calendar.x", "collections": ["Attending"]}: events go to that calendar
CALENDAR_LOG = os.path.join(HERE, "calendar_added.json")


def event_times(d):
    """HA calendar.create_event times from the idea's ISO start/end: timed (default 2 h) or all-day (end exclusive)."""
    s, e = (d.get("start") or "").strip(), (d.get("end") or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?", s):
        start = dt.datetime.fromisoformat(s)
        end = dt.datetime.fromisoformat(e) if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?", e) else None
        end = end if end and end > start else start + dt.timedelta(hours=2)
        return {"start_date_time": start.isoformat(timespec="minutes"), "end_date_time": end.isoformat(timespec="minutes")}
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s[:10]) if s else False:
        start = dt.date.fromisoformat(s[:10])
        end = dt.date.fromisoformat(e[:10]) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", e[:10]) else start
        return {"start_date": start.isoformat(), "end_date": (max(end, start) + dt.timedelta(days=1)).isoformat()}
    return None


def add_to_calendar(collection, d, post_url, key):
    """A complete, dated event from a calendar collection (config "calendar") becomes a calendar event, once per post."""
    if DRY or not (HA and CALENDAR.get("entity")) or norm(collection) not in {norm(c) for c in CALENDAR.get("collections", [])}:
        return
    if d.get("kind") != "event" or d.get("incomplete") or not (times := event_times(d)):
        notify("📅 No añadido al calendario", f"«{d.get('title')}» ({collection}) no tiene fecha y hora claras. {post_url}",
               f"insta_cal_{key}")
        return
    added = json.load(open(CALENDAR_LOG, encoding="utf-8")) if os.path.exists(CALENDAR_LOG) else []
    if str(key) in added:
        return
    g = lambda k: (d.get(k) or "").strip()
    desc = "\n".join(x for x in (g("one_liner"), g("when"), g("price") and f"Precio: {g('price')}",
                                 g("maps") and f"Maps: {g('maps')}", g("url") and f"Web: {g('url')}", f"Post: {post_url}") if x)
    event = {"entity_id": CALENDAR["entity"], "summary": d["title"], "description": desc, "location": g("where"), **times}
    if create_event(event):
        json.dump(added + [str(key)], open(CALENDAR_LOG, "w", encoding="utf-8"))
    else:  # calendar unavailable (HA would silently skip the call): retried every watcher cycle
        pending = json.load(open(CALENDAR_PENDING, encoding="utf-8")) if os.path.exists(CALENDAR_PENDING) else {}
        pending[str(key)] = event
        json.dump(pending, open(CALENDAR_PENDING, "w", encoding="utf-8"), ensure_ascii=False)
        log(f"[calendar] {d['title']}: calendar unavailable, will retry")


CALENDAR_PENDING = os.path.join(HERE, "calendar_pending.json")


def create_event(event):
    """calendar.create_event, only when the calendar entity is available (HA answers 200 and does nothing otherwise)."""
    H = {"Authorization": "Bearer " + ha_token()}
    try:
        s = requests.get(f"{HA}/api/states/{event['entity_id']}", headers=H, timeout=20)
        if not s.ok or s.json().get("state") in ("unavailable", "unknown"):
            return False
        r = requests.post(f"{HA}/api/services/calendar/create_event", timeout=30, headers=H, json=event)
    except requests.RequestException:
        return False
    if r.ok:
        log(f"[calendar] {event['summary']} -> {event['entity_id']}")
    return r.ok


def retry_calendar():
    """Events that could not be added (calendar unavailable) are tried again; called every watcher cycle."""
    if not os.path.exists(CALENDAR_PENDING):
        return
    pending = json.load(open(CALENDAR_PENDING, encoding="utf-8"))
    added = json.load(open(CALENDAR_LOG, encoding="utf-8")) if os.path.exists(CALENDAR_LOG) else []
    for key, event in list(pending.items()):
        if key in added or create_event(event):
            pending.pop(key); added = sorted(set(added) | {key})
    json.dump(added, open(CALENDAR_LOG, "w", encoding="utf-8"))
    if pending:
        json.dump(pending, open(CALENDAR_PENDING, "w", encoding="utf-8"), ensure_ascii=False)
    else:
        os.remove(CALENDAR_PENDING)


# ---------- Instagram (read-only) ----------
def ha_on(key):
    """True if the HA input_boolean named by config key `key` is on; off if missing, unreadable or a dry run."""
    ent = CFG.get(key)
    if DRY or not (HA and ent):
        return False
    try:
        return requests.get(f"{HA}/api/states/{ent}", headers={"Authorization": "Bearer " + ha_token()}, timeout=20).json()["state"] == "on"
    except Exception as e:
        log(f"{ent} unreadable ({type(e).__name__}), taking it as off")
        return False


AMIGABLE = None  # «Amigable»: same work, but one request at a time with long human pauses


def amigable():
    global AMIGABLE
    if AMIGABLE is None:
        AMIGABLE = ha_on("friendly_switch")
    return AMIGABLE


def client():
    from instagrapi import Client
    cl = Client()
    cl.delay_range = [10, 20] if amigable() else [3, 8]  # pause before every Instagram request
    cl.load_settings(SESSION)  # saved session only; never logs in with a password
    return cl


def item(m):
    return {"pk": str(m.pk), "code": m.code, "type": m.media_type, "caption": (m.caption_text or "")[:3000]}


def sync(cl, state):
    """Refresh the cached item lists. Full list once a week (or first time), otherwise only the newest page."""
    from instagrapi.exceptions import LoginRequired
    try:
        cols = cl.collections()
        by_name = {norm(c.name): c.id for c in cols}
        state["ig_collections"] = {c.name.strip(): c.media_count for c in cols if c.name.strip() != "All posts"}  # every IG collection, for HA
        week = state.get("full_sync", "") < (dt.date.today() - dt.timedelta(days=7)).isoformat()
        for name, mode in COLLECTIONS:
            pk = by_name.get(norm(name))
            if not pk or (ONLY and norm(ONLY) != norm(name)):
                continue
            c = state["collections"].setdefault(name, {"items": [], "baseline": None})
            known = {i["pk"] for i in c["items"]}
            full = (week and not amigable()) or not c["items"]  # Amigable: no weekly full re-read, newest page only
            medias = cl.collection_medias(pk, amount=0 if full else 21)
            fresh = [item(m) for m in medias if str(m.pk) not in known]
            c["items"] = fresh + c["items"]  # newest first
            if c["baseline"] is None:  # first run: everything that exists now is backlog
                c["baseline"] = [i["pk"] for i in c["items"]]
            log(f"{name}: {len(c['items'])} items ({len(fresh)} new in list)")
        if week and not ONLY:
            state["full_sync"] = dt.date.today().isoformat()
    except LoginRequired:
        notify("⚠️ Instagram guardados", "La sesión de Instagram ha caducado. Hay que renovarla (pídeselo a Claude).", "insta_ideas_session")
        raise SystemExit("session expired")
    cl.dump_settings(SESSION)  # keep refreshed cookies


def download(cl, it, folder, kind=None):
    """Local files (videos/photos) for the item. Read-only GETs."""
    pk, kind = int(it["pk"]), kind or it["type"]
    try:
        if kind == 2:
            return [str(cl.video_download(pk, folder))]
        if kind == 1:
            return [str(cl.photo_download(pk, folder))]
        if kind == 8:
            return [str(p) for p in cl.album_download(pk, folder)]
    except AssertionError:  # instagrapi: the saved list's media type was stale ("Must been photo"); ask the post itself
        real = cl.media_info(pk).media_type
        if real != kind and kind == it["type"]:  # one retry with the real type
            log(f"{it['code']}: type {kind} in the list, {real} in fact; retrying")
            return download(cl, it, folder, real)
        raise
    return []


# ---------- understanding ----------
_whisper = None


def transcribe(path):
    global _whisper
    import av
    with av.open(path) as c:  # silent videos (no audio stream) made faster-whisper raise IndexError
        if not c.streams.audio:
            return "", "?"
    from faster_whisper import WhisperModel
    if _whisper is None:
        try:
            _whisper = WhisperModel("small", device="cuda", compute_type="float16")
        except Exception as e:
            log("Whisper GPU unavailable, using CPU:", e)
            _whisper = WhisperModel("small", device="cpu", compute_type="int8")
    segs, info = _whisper.transcribe(path, vad_filter=True)
    return " ".join(s.text.strip() for s in segs).strip(), info.language


def frames(video, folder, n=4):
    """Grab n frames spread over the video (on-screen text: names, quotes, addresses, URLs)."""
    import av
    out = []
    with av.open(video) as c:
        s = c.streams.video[0]
        dur = float(c.duration / av.time_base) if c.duration else 30.0
        for i in range(n):
            t = dur * (0.12 + 0.76 * i / max(n - 1, 1))
            c.seek(int(t / s.time_base), stream=s)
            for fr in c.decode(s):
                img = fr.to_image(); img.thumbnail((900, 900))
                p = os.path.join(folder, f"frame{i}.jpg"); img.save(p, quality=85); out.append(p)
                break
    return out


URL_RE = re.compile(r"(https?://[^\s)\"'>]+|github\.com/[\w.-]+/[\w.-]+)", re.I)

PROMPT = """You are filing an Instagram post {owner} saved in their collection «{collection}».
{task}

{context}

Instagram post: {post_url}
Caption:
{caption}

Transcript (auto, language {lang}):
{transcript}

URLs found in the caption: {urls}
{frames_hint}

Answer in {language} (keep quotes/lyrics in their original language) as a JSON object with exactly these keys
(use "" or [] for keys that do not apply): relevant (bool), title (max 8 words), one_liner (max 15 words), url,
what, fit, steps (list), effort (bajo|medio|alto|""), value (bajo|medio|alto|""), claude, ha, pros (list),
cons (list), where, when, kind (event|place|activity|""), price, maps, rating, reviews (list), quote, artist, song,
tags (one-word list)."""

SCHEMA = {"type": "object",
          "required": ["relevant", "title", "one_liner", "url", "what", "fit", "steps", "effort", "value",
                       "claude", "ha", "pros", "cons", "where", "when", "kind", "price", "maps", "rating", "reviews",
                       "quote", "artist", "song", "artist_url", "tour", "start", "end", "github", "signup", "format", "ingredients", "variants", "tags"],
          "properties": {k: {"type": "string"} for k in ("title", "one_liner", "url", "what", "fit", "claude", "ha",
                                                         "where", "when", "price", "maps", "rating", "quote", "artist", "song",
                                                         "artist_url", "start", "end", "github", "signup", "format")}
          | {k: {"type": "array", "items": {"type": "string"}} for k in ("pros", "cons", "reviews", "tour", "ingredients", "variants")}
          | {"relevant": {"type": "boolean"}, "steps": {"type": "array", "items": {"type": "string"}},
             "tags": {"type": "array", "items": {"type": "string"}},
             "kind": {"type": "string", "enum": ["event", "place", "activity", ""]},
             "effort": {"type": "string", "enum": ["bajo", "medio", "alto", ""]},
             "value": {"type": "string", "enum": ["bajo", "medio", "alto", ""]}}}


def ask_claude(prompt, workdir):
    cmd = [shutil.which("claude") or "claude", "-p", "--output-format", "json", "--max-turns", "20",
           "--json-schema", json.dumps(SCHEMA), "--allowedTools", "Read", "WebSearch", "WebFetch",
           "--strict-mcp-config", "--setting-sources", "project"]  # no user hooks/MCP servers in this headless run
    # prompt via stdin and no shell: cmd.exe mangled the long prompt / JSON schema quoting
    r = subprocess.run(cmd, cwd=workdir, input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=900)
    out = json.loads(r.stdout or "{}") if r.stdout.strip().startswith("{") else {}
    if out.get("structured_output"):
        return out["structured_output"]
    if r.returncode != 0 or out.get("is_error") or not out.get("result"):
        raise RuntimeError(f"claude failed rc={r.returncode} {out.get('subtype')}: {(out.get('result') or r.stderr or r.stdout)[:300]}")
    return parse_json(out["result"])


def ask_ollama(prompt, images):
    body = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "think": False, "format": "json",
            "options": {"num_ctx": 8192}}
    if images:
        body["images"] = [base64.b64encode(open(p, "rb").read()).decode() for p in images]
    r = requests.post(f"{OLLAMA}/api/generate", json=body, timeout=900)
    if r.status_code != 200 and images:  # model without vision: retry text-only
        body.pop("images"); r = requests.post(f"{OLLAMA}/api/generate", json=body, timeout=900)
    r.raise_for_status()
    return parse_json(r.json()["response"])


def parse_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON in answer: " + text[:200])
    return json.loads(m.group(0))


# ---------- output ----------
def slug(s):
    return re.sub(r'[\\/:*?"<>|#\[\]^]', "", s).strip()[:70] or "nota"


def lst(items):
    clean = [re.sub(r"^\s*\d+[.)]\s*", "", s) for s in items or [] if s]  # models number steps themselves
    return "\n".join(f"{i}. {s}" for i, s in enumerate(clean, 1)) or "—"


def body_for(mode, d, post_url, pics=()):
    g = lambda k: (d.get(k) or "").strip()
    warn = f"> [!warning] Idea incompleta: {d['incomplete']}\n\n" if d.get("incomplete") else ""
    head = f"# {d['title']}\n\n{warn}> {g('one_liner')}\n\n" + "".join(f"![[{p}|500]]\n" for p in pics) + ("\n" if pics else "")
    if mode == "idea":
        return head + (f"- **Enlace:** {g('url') or '—'}\n- **{'Reel' if 'instagram.com' in post_url else 'Fuente'}:** {post_url}\n"
                       f"- **Esfuerzo:** {g('effort') or '?'} · **Valor:** {g('value') or '?'}\n\n"
                       f"## Qué es\n{g('what')}\n\n## Encaje en mi setup\n{g('fit')}\n\n"
                       f"## ¿Aplicable a Claude?\n{g('claude') or '—'}\n\n## ¿En Home Assistant?\n{g('ha') or '—'}\n\n"
                       f"## Pros\n{lst(d.get('pros')) or '—'}\n\n## Contras\n{lst(d.get('cons')) or '—'}\n\n"
                       f"## Cómo implementarlo\n{lst(d.get('steps'))}\n")
    if mode == "plan":
        place = " ".join(x for x in (d.get("title", ""), g("where")) if x)
        maps = g("maps") if g("maps").startswith("https://") else (
            f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(place)}" if g("where") else "")
        reviews = "\n".join(f"- {r}" for r in d.get("reviews") or [] if r) or "_No se encontraron reseñas de fuentes fiables._"
        return head + (f"- **Dónde:** {g('where') or '—'}" + (f" · [Google Maps]({maps})" if maps else "") +
                       f"\n- **Cuándo / horario:** {g('when') or '—'}\n- **Precio:** {g('price') or '—'}\n"
                       f"- **Valoración:** {g('rating') or '—'}\n- **Web:** {g('url') or '—'}\n- **Post:** {post_url}\n\n"
                       f"## Qué es\n{g('what')}\n\n## Reseñas\n{reviews}\n\n## Consejos\n{lst(d.get('steps'))}\n")
    if mode == "personal":
        quote = "\n".join("> " + l if l.strip() else ">" for l in g("quote").splitlines()) if g("quote") else ""
        return head + (f"## Texto\n{quote}\n\n" if quote else "") + f"## Mensaje\n{g('what')}\n\n## Por qué resuena\n{g('fit')}\n\n- **Post:** {post_url}\n"
    if mode == "music":
        q = urllib.parse.quote(f"{g('artist')} {g('song')}".strip())
        artist = f"[{g('artist') or '?'}]({g('artist_url')})" if g("artist_url").startswith("https://") else (g("artist") or "?")
        event = ""
        if d.get("kind") == "event":
            maps = g("maps") if g("maps").startswith("https://") else ""
            event = (f"\n## Evento\n- **Dónde:** {g('where') or '—'}" + (f" · [Google Maps]({maps})" if maps else "") +
                     f"\n- **Cuándo:** {g('when') or '—'}\n")
        tour = lst(d.get("tour")) or "_No se encontraron fechas próximas en Europa._"
        return head + (f"- **Artista:** {artist}\n- **Canción:** {g('song') or '?'}\n"
                       f"- **Escuchar:** {g('url') or f'https://open.spotify.com/search/{q}'}\n- **Post:** {post_url}\n\n{g('what')}\n"
                       f"{event}\n## Próximos conciertos en Europa\n{tour}\n")
    if mode == "food":
        ing = "\n".join(f"- {x}" for x in d.get("ingredients") or [] if x) or "—"
        var = "\n".join(f"- {x}" for x in d.get("variants") or [] if x) or "_No se conocen otras versiones destacadas._"
        return head + (f"{g('what')}\n\n## Ingredientes\n{ing}\n\n## Preparación\n{lst(d.get('steps')) or '—'}\n\n"
                       f"## Otras versiones\n{var}\n\n" + (f"- **Receta de referencia:** {g('url')}\n" if g("url") else "")
                       + f"- **Post:** {post_url}\n")
    if mode == "learn":
        fmt = {"online": "Online (virtual)", "presencial": "Presencial", "mixto": "Mixto (online y presencial)"}.get(g("format"), g("format") or "?")
        maps = g("maps") if g("maps").startswith("https://") else ""
        rows = [f"- **Formato:** {fmt}", f"- **Enlace:** {g('url') or '—'}", f"- **GitHub:** {g('github')}" * bool(g("github")),
                f"- **Inscripción:** {g('signup')}" * bool(g("signup")), f"- **Precio:** {g('price')}" * bool(g("price")),
                f"- **Cuándo:** {g('when')}" * bool(g("when")),
                (f"- **Dónde:** {g('where')}" + (f" · [Google Maps]({maps})" if maps else "")) * bool(g("where")),
                f"- **Post:** {post_url}"]
        return head + "\n".join(r for r in rows if r) + f"\n\n## Qué es\n{g('what')}\n\n## Qué aprenderías\n{lst(d.get('steps')) or '—'}\n"
    if mode == "general":
        text = "\n".join("> " + l if l.strip() else ">" for l in g("quote").splitlines()) if g("quote") else ""
        return head + (f"{g('what')}\n\n## Puntos clave\n{lst(d.get('steps')) or '—'}\n\n" + (f"## Texto\n{text}\n\n" if text else "")
                       + (f"- **Enlace:** {g('url')}\n" if g("url") else "") + f"- **Post:** {post_url}\n")
    if mode == "home":
        text = "\n".join("> " + l if l.strip() else ">" for l in g("quote").splitlines()) if g("quote") else ""
        return head + (f"{g('what')}\n\n## Cómo\n{lst(d.get('steps')) or '—'}\n\n" + (f"## Texto\n{text}\n\n" if text else "") +
                       (f"- **Coste:** {g('price')}\n" if g("price") else "") + f"- **Post:** {post_url}\n")
    return head + f"- **Post:** {post_url}\n"  # private


MONTHS = ("ene|feb|mar|abr|may|jun|jul|ago|sep|oct|nov|dic|jan|apr|aug|dec|january|february|march|april|june|july|"
          "august|september|october|november|december|enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|"
          "noviembre|diciembre")


def has_date(text):
    """True if text holds a concrete date: '29 mayo 2026', '29/05/2026', '2026-05-29', 'May 29'…"""
    t = (text or "").lower()
    return bool(re.search(r"(?<!\d)\d{1,2}/\d{1,2}(/\d{2,4})?(?!\d)|\b\d{4}-\d{2}-\d{2}\b", t) or
                re.search(rf"(?<!\d)\d{{1,2}}(º|°)?\s*(de\s+)?({MONTHS})\b|\b({MONTHS})\.?\s+\d{{1,2}}(?!\d)", t))


EVENT_RETRY = ("\n\nIMPORTANT: this is an EVENT and `when` has no concrete date yet. Search the official site and ticketing "
               "pages and fill `when` with the exact date(s) (day month year, time if known); repeat every other field as before.")


def ensure_event_date(d, prompt, workdir, brain):
    """An event without a date is not a complete idea: ask once more for the date only; flag it if still missing."""
    if brain == "claude":
        try:
            r = ask_claude(prompt + EVENT_RETRY, workdir)
            if has_date(r.get("when")):
                d["when"] = r["when"]
                return d
        except Exception as e:
            log("event date retry failed:", type(e).__name__)
    d["incomplete"] = "falta la fecha del evento"
    log("event without a date:", d.get("title"))
    return d


def save_pictures(code, imgs, n=4):
    """Copy up to n of the post's pictures (photos, or one video frame) into the vault; returns their file names."""
    if DRY or not imgs:
        return []
    from PIL import Image
    os.makedirs(ASSETS, exist_ok=True)
    frames_only = all(os.path.basename(p).startswith("frame") for p in imgs)
    pick = [imgs[min(1, len(imgs) - 1)]] if frames_only else imgs[:n]   # a video: one frame from early in it
    out = []
    for i, src in enumerate(pick, 1):
        name = f"{code}_{i}.jpg"
        with Image.open(src) as im:
            im = im.convert("RGB"); im.thumbnail((1200, 1200))
            im.save(os.path.join(ASSETS, name), quality=85)
        out.append(name)
    return out


def write_note(mode, collection, d, post_url, brain, when, pics=()):
    folder = os.path.join(BASE, MODES[mode]["folder"])
    os.makedirs(folder, exist_ok=True)
    name = f"{when:%Y-%m-%d} {slug(d['title'])}.md"
    tags = ["instagram" if "instagram.com" in post_url else "enlace", mode] + [re.sub(r"\W", "", t.lower()) for t in d.get("tags") or [] if t]
    fm = {"type": mode, "tags": sorted(set(t for t in tags if t)), "created": f"{when:%Y-%m-%d}",
          "updated": f"{when:%Y-%m-%d}", "summary": d.get("one_liner", ""), "collection": collection,
          "url": d.get("url", ""), "source": post_url, "status": "incompleta" if d.get("incomplete") else "nueva", "brain": brain}
    if d.get("incomplete"):
        fm["missing"] = d["incomplete"]
    if mode == "idea":
        fm |= {"effort": d.get("effort", ""), "value": d.get("value", "")}
    if mode == "plan":
        fm |= {"where": d.get("where", ""), "when": d.get("when", ""), "price": d.get("price", ""), "rating": d.get("rating", "")}
    if pics:
        fm["image"] = pics[0]
    front = "---\n" + "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fm.items()) + "\n---\n"
    text = front + body_for(mode, d, post_url, pics) + f"\n---\nVolver a [[{MODES[mode]['folder']}]] · [[Home]]\n"
    if DRY:
        log("[dry-run] note:", os.path.join(folder, name)); print(text); return name
    open(os.path.join(folder, name), "w", encoding="utf-8").write(text)
    return name


def ensure_index(mode):
    m = MODES[mode]
    p = os.path.join(BASE, m["folder"] + ".md")
    if os.path.exists(p) or DRY:
        return
    today = dt.date.today().isoformat()
    cols = {"idea": "summary, effort, value, status, url", "plan": "summary, where, when, price, status",
            "personal": "summary, collection", "music": "summary, url", "private": "summary"}[mode]
    open(p, "w", encoding="utf-8").write(f"""---
type: project
tags: [project, instagram, {mode}]
created: {today}
updated: {today}
summary: {m['about'].capitalize()}; generado a diario desde Instagram (solo lectura).
related: ["[[Home]]"]
---
# {m['folder']}

{m['emoji']} {m['about'].capitalize()}. Cada día se procesan los guardados nuevos y unos cuantos antiguos.

```base
filters:
  and:
    - file.inFolder("{(NOTES_DIR + '/' if NOTES_DIR else '') + m['folder']}")
views:
  - type: table
    name: {m['folder']}
    order: [file.name, {cols}]
    sort:
      - property: created
        direction: DESC
```
""")
    if not HOME_NOTE:
        return
    home = os.path.join(VAULT, HOME_NOTE)
    h = open(home, encoding="utf-8").read()
    if f"[[{m['folder']}]]" not in h:
        h = h.replace("## Proyectos\n", f"## Proyectos\n- [[{m['folder']}]] — {m['about']}\n", 1)
        open(home, "w", encoding="utf-8").write(h)


def process(cl, collection, mode, it, done):
    post_url = f"https://www.instagram.com/{'reel' if it['type'] == 2 else 'p'}/{it['code']}/"
    tmp = tempfile.mkdtemp(prefix="insta_", dir=HERE)
    try:
        files = download(cl, it, tmp)
        transcript, lang, imgs = "", "?", []
        for f in files:
            if f.lower().endswith((".mp4", ".mov")):
                t, lang = transcribe(f); transcript += " " + t
                imgs += frames(f, tmp)
            else:
                imgs.append(f)
        urls = sorted(set(URL_RE.findall(it["caption"])))
        hint = ("Frames/photos are in the current folder: " + ", ".join(os.path.basename(i) for i in imgs[:6]) +
                " — open them with the Read tool to read on-screen text.") if imgs else ""
        prompt = PROMPT.format(owner=OWNER, language=LANGUAGE, collection=collection,
                               task=MODES[mode]["task"].format(owner=OWNER), context=CONTEXT, post_url=post_url,
                               caption=it["caption"] or "(none)", transcript=transcript.strip()[:6000] or "(no speech)",
                               lang=lang, urls=", ".join(urls) or "none", frames_hint=hint)
        try:
            d, brain = ask_claude(prompt, tmp), "claude"
        except Exception as e:
            log("Claude unavailable -> Ollama:", e)
            global _whisper
            _whisper = None  # free VRAM for the LLM
            d, brain = ask_ollama(prompt.replace(hint, "Frames are attached as images.") if hint else prompt, imgs[:4]), "ollama"
        if mode in ("plan", "music") and d.get("kind") == "event" and not has_date(d.get("when")):
            d = ensure_event_date(d, prompt, tmp, brain)
        done.add(it["pk"])
        if MODES[mode].get("skip_irrelevant") and not d.get("relevant", True):
            log(f"[{collection}] not relevant, skipped: {post_url} | {d.get('one_liner')}")
            return None
        if not d.get("title"):
            d["title"] = d.get("one_liner") or it["code"]
        pics = save_pictures(it["code"], imgs) if mode in PICTURE_MODES else []
        note = write_note(mode, collection, d, post_url, brain, dt.date.today(), pics)
        log(f"[{collection}] {d['title']} | {brain} | {note}")
        add_to_calendar(collection, d, post_url, it["pk"])
        return d
    except Exception as e:
        if type(e).__name__ in ("LoginRequired", "ChallengeRequired"):  # session expired: stop the scan, don't blame the post
            notify("⚠️ Instagram guardados", "La sesión de Instagram ha caducado. Hay que renovarla (pídeselo a Claude).", "insta_ideas_session")
            raise SystemExit("session expired")
        log(f"[{collection}] FAILED {post_url}", traceback.format_exc().splitlines()[-1])
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)



def unsave_on():
    """HA switch "unsave_switch" («Quitar de guardados»); off (keep saved) if it is missing or unreadable."""
    return ha_on("unsave_switch")


def unsave(cl, it):
    """Removes the post from Instagram's saved posts entirely (every collection and «All saved posts»)."""
    time.sleep(random.uniform(4, 10))  # gentle pace: write actions are what Instagram watches for automation
    try:
        cl.media_unsave(cl.media_id(it["pk"]))
        log(f"unsaved from Instagram: {it['code']}")
        return True
    except Exception as e:
        if type(e).__name__ in ("LoginRequired", "ChallengeRequired"):
            notify("⚠️ Instagram guardados", "La sesión de Instagram ha caducado. Hay que renovarla (pídeselo a Claude).", "insta_ideas_session")
            raise SystemExit("session expired")
        log(f"unsave FAILED {it['code']}: {type(e).__name__}")  # stays saved; the note is already written
        return False


# ---------- main ----------
def main():
    lock = scan_lock()
    if not lock:
        log("another scan is already running; exiting"); return
    try:
        scan()
    except Exception:
        log("scan FAILED", traceback.format_exc().splitlines()[-1])
        json.dump({"last_scan": dt.datetime.now().isoformat(timespec="minutes"), "last_result": "error"},
                  open(LAST_SCAN, "w", encoding="utf-8"))
        raise
    finally:
        progress("Inactivo", running=False)
        lock.close()


def scan():
    global ONLY
    ONLY = run_collection()   # sync() and the queue below then handle only that collection
    if ONLY:
        log("collection:", ONLY)
    progress("Sincronizando Instagram…")
    state = json.load(open(STATE, encoding="utf-8")) if os.path.exists(STATE) else {}
    state.setdefault("collections", {}); state.setdefault("done", [])
    done = set(state["done"])
    save = lambda: DRY or json.dump(state | {"done": sorted(done)}, open(STATE, "w", encoding="utf-8"), ensure_ascii=False)
    cl = client()
    last = dt.datetime.fromisoformat(state["last_sync"]) if state.get("last_sync") else None
    if CONTINUOUS and last and dt.datetime.now() - last < dt.timedelta(hours=6):
        log("continuous: Instagram list synced at", state["last_sync"], "- reusing it")  # back-to-back scans don't re-sync
    else:
        sync(cl, state)
        state["last_sync"] = dt.datetime.now().isoformat(timespec="seconds")
    save()
    failed = state.setdefault("failed", {})  # pk -> failed attempts; after MAX_TRIES the post is skipped (kept, not done)
    newest_first = run_order() == "newest"
    queue_new, queue_old = [], []
    for name, mode in COLLECTIONS:
        c = state["collections"].get(name)
        if not c or (ONLY and norm(ONLY) != norm(name)):  # --only NAME: sync and process just that collection
            continue
        base = set(c["baseline"] or [])
        for it in (c["items"] if newest_first else reversed(c["items"])):  # items are stored newest first
            if it["pk"] in done or failed.get(it["pk"], 0) >= MAX_TRIES:  # set aside after repeated failures
                continue
            (queue_old if it["pk"] in base else queue_new).append((name, mode, it))
    queue = (queue_new[:MAX_NEW] + queue_old[:BACKLOG])[:LIMIT]
    if amigable():
        log("Amigable: one request at a time with long pauses")
    log(f"queue ({'newest' if newest_first else 'oldest'} first): {len(queue_new)} new, {len(queue_old)} backlog -> processing {len(queue)}")
    results = []
    for name, mode in dict.fromkeys((n, m) for n, m, _ in queue):
        ensure_index(mode)
    ok = 0
    unsave_ = unsave_on()
    if unsave_:
        log("unsave: on - posts with a complete note are removed from Instagram's saved posts")
    for n, (name, mode, it) in enumerate(queue):
        if n and amigable():
            time.sleep(random.uniform(10, 20))  # one post at a time, with a human-sized break between them
        progress(f"{round(100 * n / len(queue))} % ({n}/{len(queue)})", n, len(queue))
        d = process(cl, name, mode, it, done)
        if not d and it["pk"] not in done:  # failed (not just "not relevant"): count it
            failed[it["pk"]] = failed.get(it["pk"], 0) + 1
        if unsave_ and d and not d.get("incomplete") and unsave(cl, it):  # only once the note is complete
            state.setdefault("unsaved", []).append(it["pk"])
        save()
        ok += it["pk"] in done  # handled, including "not relevant" skips (only failures don't count)
        if d and MODES[mode]["notify"]:
            results.append((mode, d, it["pk"]))
    for mode, d, pk in results:  # one notification per idea/plan
        notify_item(mode, d, pk)
    json.dump({"last_scan": dt.datetime.now().isoformat(timespec="minutes"), "last_result": f"{ok}/{len(queue)} procesados"},
              open(LAST_SCAN, "w", encoding="utf-8"), ensure_ascii=False)
    if CONTINUOUS and not ok:  # backlog done, or only posts that keep failing: stop instead of looping all night
        stop_continuous("nada pendiente" if not queue else f"{len(queue)} pendientes fallan siempre")


def stop_continuous(why):
    ent = CFG.get("continuous_switch")
    if DRY or not (HA and ent):
        return
    requests.post(f"{HA}/api/services/input_boolean/turn_off", headers={"Authorization": "Bearer " + ha_token()},
                  json={"entity_id": ent}, timeout=20)
    notify("💡 Insta-Ideas: modo continuo apagado", f"Se apagó solo: {why}.", "insta_continuous_off")


def notify_item(mode, d, pk):
    g = lambda k: (d.get(k) or "").strip()
    parts = [g("one_liner")]
    if mode == "idea":
        parts += [f"**Claude:** {g('claude')}" if g("claude") else "", f"**HA:** {g('ha')}" if g("ha") else "",
                  f"**Esfuerzo:** {g('effort') or '?'} · **Valor:** {g('value') or '?'}"]
    if mode == "plan":
        parts += [f"📍 {g('where')}" if g("where") else "", f"⭐ {g('rating')}" if g("rating") else "",
                  f"[Google Maps]({g('maps')})" if g("maps").startswith("https://") else ""]
    parts += [g("url"), f"Nota en Obsidian: {MODES[mode]['folder']}"]
    if d.get("incomplete"):
        parts.insert(0, f"⚠️ Idea incompleta: {d['incomplete']}")
    notify(f"{'⚠️ ' if d.get('incomplete') else ''}{MODES[mode]['emoji']} {d['title']}", "\n\n".join(p for p in parts if p), f"insta_{mode}_{pk}")


if __name__ == "__main__":
    main()
