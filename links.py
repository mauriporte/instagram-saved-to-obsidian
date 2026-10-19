"""Links → idea notes: any link you send is analysed and filed like the Instagram «Work» ideas.

Inputs (both optional, set in config.json → "links"):
  - a Home Assistant to-do list (e.g. todo.ideas_enlaces): add an item with a link; it is ticked when processed.
  - Beeper Desktop chats by title (e.g. a Telegram/WhatsApp group «Ideas» with only you): every link posted there,
    by you or anyone, is processed and answered in the chat with the idea's title. Needs a Beeper Desktop API token.
Per link: Instagram posts use the saved Instagram session; anything yt-dlp can download (YouTube, TikTok, X, …) is
transcribed with Whisper plus a few frames; other pages (articles, GitHub) are read by Claude with WebFetch.
Each link gives an idea note (same folder and format as ideas.py) and its own Home Assistant notification.
Run: python links.py [--once] [--url URL]   (default: keep watching every config "interval" seconds)
"""
import datetime as dt, json, os, re, shutil, subprocess, sys, tempfile, time, traceback
import ideas  # config, Instagram session, Whisper, Claude/Ollama, note writer and notifications
from ideas import log, requests
import vault_sync  # HA «Ideas» list + Markdown dashboards, refreshed every cycle

CFG = ideas.CFG.get("links", {})
STATE = os.path.join(ideas.HERE, "links_state.json")
LINK_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
COLLECTION = CFG.get("collection", "Work")  # links go here unless the message names another collection
PROMPT = ideas.PROMPT.replace(
    "You are filing an Instagram post {owner} saved in their collection «{collection}».",
    "You are filing a link {owner} sent to be saved in their collection «{collection}». If the page itself is not in the "
    "material below, open it with WebFetch (and WebSearch if needed) and judge what it is from there.").replace(
    "Instagram post: {post_url}", "Link: {post_url}")


def load_state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except (OSError, ValueError):
        return {"done": [], "beeper_seen": {}}


def save_state(st):
    json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def media(url, folder):
    """Audio (for Whisper) and video (for frames) of the link when it is media, as separate files: YouTube serves
    them apart and merging would need ffmpeg. Returns (audio or None, video or None, title, description)."""
    import yt_dlp
    base = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True, "max_filesize": 400 * 1024 * 1024, "socket_timeout": 30}
    try:
        with yt_dlp.YoutubeDL(base) as y:
            info = y.extract_info(url, download=False)
    except Exception as e:  # not media (articles, repos…) or unsupported site
        log("[links] no media for", url, "->", type(e).__name__)
        return None, None, "", ""
    title, desc = info.get("title") or "", info.get("description") or ""
    if (info.get("duration") or 0) > 3600:  # long videos: metadata only, Claude reads the page
        return None, None, title, desc
    got = {}
    for kind, fmt in (("audio", "ba/b"), ("video", "bv*[height<=720]/b[height<=720]/bv*/b")):
        try:
            with yt_dlp.YoutubeDL(base | {"format": fmt, "outtmpl": os.path.join(folder, f"{kind}.%(ext)s")}) as y:
                y.download([url])
            got[kind] = next((os.path.join(folder, f) for f in os.listdir(folder) if f.startswith(kind + ".")), None)
        except Exception as e:
            log("[links]", kind, "download failed for", url, "->", type(e).__name__)
    return got.get("audio"), got.get("video"), title, desc


class Busy(Exception):
    """A scan is using the Instagram session: try this link again next cycle."""


def pick_collection(text):
    """(collection, mode) named in the message text besides the links ("planes", "quotes"…), else the default one."""
    words = ideas.norm(LINK_RE.sub(" ", text or ""))
    for name, mode in ideas.COLLECTIONS:
        if re.search(rf"(?<!\w){re.escape(ideas.norm(name))}(?!\w)", words):
            return name, mode
    return COLLECTION, next((m for n, m in ideas.COLLECTIONS if ideas.norm(n) == ideas.norm(COLLECTION)), "idea")


def analyse(url, collection=COLLECTION, mode="idea"):
    """Idea dict for one link, or None. Instagram goes through ideas.process (same as the collections)."""
    if re.search(r"instagram\.com/(p|reel|reels|tv)/", url):
        lock = ideas.scan_lock()  # one user of the Instagram session at a time (scans hold the same lock)
        if not lock:
            raise Busy(url)
        try:
            ideas.AMIGABLE = None  # the watcher runs all day: read the «Amigable» switch afresh for each link
            cl = ideas.client()
            it = ideas.item(cl.media_info(cl.media_pk_from_url(url)))
            return ideas.process(cl, collection, mode, it, set()), it["pk"]
        finally:
            lock.close()
    tmp = tempfile.mkdtemp(prefix="link_", dir=ideas.HERE)
    try:
        audio, video, title, desc = media(url, tmp)
        transcript, lang, imgs = "", "?", []
        if audio:
            transcript, lang = ideas.transcribe(audio)
        if video:
            try:
                imgs = ideas.frames(video, tmp)
            except Exception as e:  # audio-only "video" or a codec PyAV cannot seek
                log("[links] no frames:", type(e).__name__)
        hint = ("Frames are in the current folder: " + ", ".join(os.path.basename(i) for i in imgs) +
                " — open them with the Read tool to read on-screen text.") if imgs else ""
        caption = "\n".join(x for x in (title, desc[:2500]) if x) or "(none: open the link)"
        prompt = PROMPT.format(owner=ideas.OWNER, language=ideas.LANGUAGE, collection=collection,
                               task=ideas.MODES[mode]["task"].format(owner=ideas.OWNER), context=ideas.CONTEXT,
                               post_url=url, caption=caption, transcript=transcript[:6000] or "(no speech / not a video)",
                               lang=lang, urls=", ".join(sorted(set(LINK_RE.findall(desc)))) or "none", frames_hint=hint)
        try:
            d, brain = ideas.ask_claude(prompt, tmp), "claude"
        except Exception as e:
            log("[links] Claude unavailable -> Ollama:", e)
            ideas._whisper = None
            d, brain = ideas.ask_ollama(prompt.replace(hint, "Frames are attached as images.") if hint else prompt, imgs[:4]), "ollama"
        if mode in ("plan", "music") and d.get("kind") == "event" and not ideas.has_date(d.get("when")):
            d = ideas.ensure_event_date(d, prompt, tmp, brain)  # an event without a date is incomplete
        d["title"] = d.get("title") or d.get("one_liner") or url
        note = ideas.write_note(mode, collection, d, url, brain, dt.date.today())  # links are always filed
        log(f"[{collection}] {d['title']} | {brain} | {note}")
        return d, re.sub(r"\W+", "_", url)[-60:]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def handle(url, st, text=""):
    """Process one link once, in the collection its message names (default COLLECTION); notify per idea. Returns the title or None."""
    if url in st["done"]:
        return None
    try:
        collection, mode = pick_collection(text)
        d, nid = analyse(url, collection, mode)
    except Busy:
        raise
    except Exception:
        log("[links] FAILED", url, traceback.format_exc().splitlines()[-1]); return None
    finally:
        ideas._whisper = None  # the watcher runs all day: do not keep Whisper's VRAM between links
    st["done"].append(url); save_state(st)
    if d:
        ideas.notify_item(mode, d, nid)
        return d["title"]
    return None


# ---------- inputs ----------
def ha(method, path, **kw):
    return requests.request(method, f"{ideas.HA}/api/{path}", headers={"Authorization": "Bearer " + ideas.ha_token()}, timeout=30, **kw)


def from_ha(st):
    ent = CFG.get("todo")
    if not ent or not ideas.HA:
        return
    r = ha("POST", "services/todo/get_items?return_response", json={"entity_id": ent, "status": "needs_action"})
    items = (r.json().get("service_response") or {}).get(ent, {}).get("items", []) if r.ok else []
    for it in items:
        text = f'{it.get("summary", "")} {it.get("description") or ""}'
        urls = LINK_RE.findall(text)
        if not urls:
            continue
        try:
            titles = [t for t in (handle(u, st, text) for u in urls) if t]
        except Busy:
            log("[links] Instagram busy (scan running): link postponed"); continue
        ha("POST", "services/todo/update_item", json={"entity_id": ent, "item": it["uid"], "status": "completed",
            "description": ((it.get("description") or "") + "\n" + " · ".join(f"💡 {t}" for t in titles)).strip()})


def from_beeper(st):
    token, chats = CFG.get("beeper_token"), CFG.get("beeper_chats") or []
    if not token or not chats:
        return
    B, H = CFG.get("beeper_url", "http://localhost:23373"), {"Authorization": f"Bearer {token}"}
    q = lambda cid: requests.utils.quote(cid, safe="")
    for title in chats:
        r = requests.get(f"{B}/v1/chats/search", headers=H, params={"query": title, "limit": 10}, timeout=20)
        st["beeper_ok"] = r.status_code != 401
        if r.status_code == 401:  # OAuth tokens last 30 days: warn once a day, run beeper_auth.py again
            if st.get("beeper_warned") != dt.date.today().isoformat():
                ideas.notify("⚠️ Ideas por Beeper", "El token de Beeper Desktop ha caducado: ejecuta beeper_auth.py en insta-ideas.", "links_beeper_token")
                st["beeper_warned"] = dt.date.today().isoformat(); save_state(st)
            return
        for chat in [c for c in r.json().get("items", []) if ideas.norm(c.get("title") or "") == ideas.norm(title)]:
            seen = st["beeper_seen"].setdefault(chat["id"], [])
            msgs = requests.get(f"{B}/v1/chats/{q(chat['id'])}/messages", headers=H, timeout=20).json().get("items", [])
            if not seen:  # first run on this chat: start from now, do not reprocess history
                st["beeper_seen"][chat["id"]] = [m["id"] for m in msgs] or ["-"]; save_state(st); continue
            for m in reversed(msgs):  # oldest first
                if m["id"] in seen:
                    continue
                for u in LINK_RE.findall(m.get("text") or ""):
                    try:
                        t = handle(u, st, m.get("text"))
                    except Busy:  # a scan has the Instagram session: leave this message for the next cycle
                        log("[links] Instagram busy (scan running): message postponed"); return
                    if t:
                        requests.post(f"{B}/v1/chats/{q(chat['id'])}/messages", headers=H, timeout=20,
                                      json={"text": f"💡 Idea guardada: {t}"})
                seen.append(m["id"]); del seen[:-500]; save_state(st)


def heartbeat(st):
    """sensor.ideas_watcher: when the watcher last ran, so Home Assistant can tell if the PC side is alive."""
    try:
        ha("POST", "states/sensor.ideas_watcher", json={"state": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "attributes": {"friendly_name": "Ideas: último ciclo", "device_class": "timestamp", "icon": "mdi:robot-happy-outline",
                           "beeper_token_ok": st.get("beeper_ok"), "links_processed": len(st.get("done", [])),
                           "interval_s": int(CFG.get("interval", 120))}})
        img = ideas.CFG.get("images_server")
        if img:  # for the Notes Vault patch: the key is readable only with a Home Assistant login
            ha("POST", "states/sensor.vault_images", json={"state": "on", "attributes": {
                "friendly_name": "Ideas: imágenes del vault", "icon": "mdi:image-multiple", "key": img["key"],
                "lan": img.get("lan", "")}})  # e.g. http://<pc-ip>:8796, for viewers at home
    except Exception as e:
        log("[links] heartbeat error:", type(e).__name__)


LAST_START = [0.0]  # the new scan takes a few seconds to take its lock; don't start another meanwhile


def start_scan(*args):
    LAST_START[0] = time.time()
    subprocess.Popen([sys.executable, os.path.join(ideas.HERE, "ideas.py"), *args], cwd=ideas.HERE,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def scan_button(st):
    """Starts ideas.py when the Home Assistant scan button ("scan_button") was pressed and no scan is running."""
    ent = ideas.CFG.get("scan_button")
    if not ent or not ideas.HA:
        return
    r = ha("GET", f"states/{ent}")
    pressed = r.json().get("state") if r.ok else None
    if pressed and pressed != st.get("scan_button_seen"):
        first = "scan_button_seen" not in st  # first look: remember the last press, don't act on it
        st["scan_button_seen"] = pressed; save_state(st)
        if not first and not ideas.scan_running():
            log("[links] scan button pressed: starting ideas.py")
            start_scan()
            return
    sw = ideas.CFG.get("continuous_switch")  # "Continuo": keep starting scans until it is turned off
    if sw and time.time() - LAST_START[0] > 90 and not ideas.scan_running():
        c = ha("GET", f"states/{sw}")
        if c.ok and c.json().get("state") == "on":
            log("[links] continuous mode: starting the next scan")
            start_scan("--continuous")
            return
    s = ha("GET", f"states/{ideas.SCAN_SENSOR}")  # missing after an HA restart, or stale after a crashed scan
    if (s.status_code == 404 or (s.ok and s.json()["attributes"].get("running"))) and not ideas.scan_running():
        ideas.progress("Inactivo", running=False)


IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
               ".html": "text/html; charset=utf-8", ".pdf": "application/pdf"}


def serve_images():
    """Read-only server for vault files (images_server: {"port", "folders", "key"}), so Home Assistant's Notes Vault
    can show them without copying them anywhere: GET /vault-img/<key>/<path in the vault or bare file name>; only
    pictures, HTML and PDF under `folders`."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading, urllib.parse
    cfg = ideas.CFG.get("images_server")
    if not cfg:
        return
    roots = [os.path.realpath(os.path.join(ideas.VAULT, d)) for d in cfg.get("folders", ["90-assets"])]
    prefix = f"/vault-img/{cfg['key']}/"  # the key in the path: the tunnel route is reachable from the internet
    names = {}

    def by_name(name):
        """Notes embed files by bare file name, as Obsidian resolves them: find it under `folders`."""
        if name not in names:
            names.clear()
            for root in roots:
                for d, _, fs in os.walk(root):
                    for x in fs:
                        names.setdefault(x, os.path.join(d, x))
        return names.get(name, "")

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            rel = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
            f = os.path.realpath(os.path.join(ideas.VAULT, rel.removeprefix(prefix)))
            if "/" not in rel.removeprefix(prefix):
                f = os.path.realpath(by_name(rel.removeprefix(prefix)) or f)
            kind = IMAGE_TYPES.get(os.path.splitext(f)[1].lower())
            if (not rel.startswith(prefix) or not kind or not any(f.startswith(r + os.sep) for r in roots)
                    or not os.path.isfile(f)):
                self.send_error(404); return
            body = open(f, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", kind); self.send_header("Content-Length", str(len(body)))
            if kind.startswith("text/html"):  # served from HA's own address: no scripts, no access to its storage
                self.send_header("Content-Security-Policy", "sandbox allow-popups allow-popups-to-escape-sandbox")
            self.send_header("Cache-Control", "private, max-age=86400" if kind.startswith("image/") else "no-cache")  # reports change in place
            self.end_headers(); self.wfile.write(body)

        def log_message(self, *a):  # quiet: the watcher log is for ideas
            pass

    srv = ThreadingHTTPServer(("0.0.0.0", int(cfg.get("port", 8796))), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"[links] vault pictures served on port {srv.server_address[1]}")


def main():
    st = load_state()
    if "--url" not in sys.argv and "--once" not in sys.argv:
        try:
            serve_images()
        except OSError as e:  # port taken (another watcher running): pictures are already being served
            log("[links] picture server not started:", e)
    if "--url" in sys.argv:
        print(handle(sys.argv[sys.argv.index("--url") + 1], st)); return
    while True:
        for src in (from_ha, from_beeper, lambda st: vault_sync.run()):
            try:
                src(st)
            except Exception:
                log("[links]", src.__name__, "error:", traceback.format_exc().splitlines()[-1])
        heartbeat(st)
        if "--once" in sys.argv:
            return
        for _ in range(max(1, int(CFG.get("interval", 120)) // 10)):  # the scan button is checked every 10 s
            try:
                scan_button(st)
                vault_sync.check_move(st, save_state)
            except Exception:
                log("[links] scan_button/move error:", traceback.format_exc().splitlines()[-1])
            time.sleep(10)


if __name__ == "__main__":
    main()
