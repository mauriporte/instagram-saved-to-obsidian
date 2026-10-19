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
import base64, datetime as dt, json, os, re, shutil, subprocess, sys, tempfile, traceback, unicodedata, urllib.parse

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
steps (practical tips as a list: booking, best time, what to order/see)."""},
    "personal": {"folder": "Personal Instagram", "emoji": "🪞", "notify": False,
                 "about": "reflexiones, frases y cosas personales",
                 "task": """{owner} saved this for personal reasons: a quote, a reflection, something deep or meaningful
to them: a saying, phrase, poem or reflection. TRANSCRIBE it completely and faithfully: quote = the whole text exactly as
it appears on the image(s)/frames or is spoken (every line of a poem, line breaks kept, original language, no
paraphrasing; open the images with the Read tool to read them), plus the author after a final line "— Author" if shown.
what = the core message in 1-3 sentences, fit = in one or two sentences why it may resonate (gentle, no therapy talk)."""},
    "music": {"folder": "Música Instagram", "emoji": "🎵", "notify": False,
              "about": "canciones y artistas guardados",
              "task": """{owner} saved this for the MUSIC. Identify the song and artist (caption, on-screen text,
lyrics in the transcript; you may WebSearch lyrics). Fill: artist, song, what (genre/vibe in one sentence), url
(official link if found, else empty)."""},
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
            if s["state"] in opts[1:] and any(norm(s["state"]) == norm(n) for n, _ in COLLECTIONS):
                return s["state"]
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


# ---------- Instagram (read-only) ----------
def client():
    from instagrapi import Client
    cl = Client()
    cl.delay_range = [3, 8]  # human-like pauses between requests
    cl.load_settings(SESSION)  # saved session only; never logs in with a password
    return cl


def item(m):
    return {"pk": str(m.pk), "code": m.code, "type": m.media_type, "caption": (m.caption_text or "")[:3000]}


def sync(cl, state):
    """Refresh the cached item lists. Full list once a week (or first time), otherwise only the newest page."""
    from instagrapi.exceptions import LoginRequired
    try:
        by_name = {norm(c.name): c.id for c in cl.collections()}
        week = state.get("full_sync", "") < (dt.date.today() - dt.timedelta(days=7)).isoformat()
        for name, mode in COLLECTIONS:
            pk = by_name.get(norm(name))
            if not pk or (ONLY and norm(ONLY) != norm(name)):
                continue
            c = state["collections"].setdefault(name, {"items": [], "baseline": None})
            known = {i["pk"] for i in c["items"]}
            medias = cl.collection_medias(pk, amount=0 if (week or not c["items"]) else 21)
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


def download(cl, it, folder):
    """Local files (videos/photos) for the item. Read-only GETs."""
    pk = int(it["pk"])
    if it["type"] == 2:
        return [str(cl.video_download(pk, folder))]
    if it["type"] == 1:
        return [str(cl.photo_download(pk, folder))]
    if it["type"] == 8:
        return [str(p) for p in cl.album_download(pk, folder)]
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
cons (list), where, when, price, maps, rating, reviews (list), quote, artist, song, tags (one-word list)."""

SCHEMA = {"type": "object",
          "required": ["relevant", "title", "one_liner", "url", "what", "fit", "steps", "effort", "value",
                       "claude", "ha", "pros", "cons", "where", "when", "price", "maps", "rating", "reviews",
                       "quote", "artist", "song", "tags"],
          "properties": {k: {"type": "string"} for k in ("title", "one_liner", "url", "what", "fit", "claude", "ha",
                                                         "where", "when", "price", "maps", "rating", "quote", "artist", "song")}
          | {k: {"type": "array", "items": {"type": "string"}} for k in ("pros", "cons", "reviews")}
          | {"relevant": {"type": "boolean"}, "steps": {"type": "array", "items": {"type": "string"}},
             "tags": {"type": "array", "items": {"type": "string"}},
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
    head = f"# {d['title']}\n\n> {g('one_liner')}\n\n" + "".join(f"![[{p}|500]]\n" for p in pics) + ("\n" if pics else "")
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
        return head + (f"- **Artista:** {g('artist') or '?'}\n- **Canción:** {g('song') or '?'}\n"
                       f"- **Escuchar:** {g('url') or f'https://open.spotify.com/search/{q}'}\n- **Post:** {post_url}\n\n{g('what')}\n")
    return head + f"- **Post:** {post_url}\n"  # private


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
          "url": d.get("url", ""), "source": post_url, "status": "nueva", "brain": brain}
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
        done.add(it["pk"])
        if MODES[mode].get("skip_irrelevant") and not d.get("relevant", True):
            log(f"[{collection}] not relevant, skipped: {post_url} | {d.get('one_liner')}")
            return None
        if not d.get("title"):
            d["title"] = d.get("one_liner") or it["code"]
        pics = save_pictures(it["code"], imgs) if mode in PICTURE_MODES else []
        note = write_note(mode, collection, d, post_url, brain, dt.date.today(), pics)
        log(f"[{collection}] {d['title']} | {brain} | {note}")
        return d
    except Exception:
        log(f"[{collection}] FAILED {post_url}", traceback.format_exc().splitlines()[-1])
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
    queue = queue_new[:MAX_NEW] + queue_old[:BACKLOG]
    log(f"queue ({'newest' if newest_first else 'oldest'} first): {len(queue_new)} new, {len(queue_old)} backlog -> processing {len(queue)}")
    results = []
    for name, mode in dict.fromkeys((n, m) for n, m, _ in queue):
        ensure_index(mode)
    ok = 0
    for n, (name, mode, it) in enumerate(queue):
        progress(f"{round(100 * n / len(queue))} % ({n}/{len(queue)})", n, len(queue))
        d = process(cl, name, mode, it, done)
        if not d and it["pk"] not in done:  # failed (not just "not relevant"): count it
            failed[it["pk"]] = failed.get(it["pk"], 0) + 1
        save()
        ok += bool(d)
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
    notify(f"{MODES[mode]['emoji']} {d['title']}", "\n\n".join(p for p in parts if p), f"insta_{mode}_{pk}")


if __name__ == "__main__":
    main()
