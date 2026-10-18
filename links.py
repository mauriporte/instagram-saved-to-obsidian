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
import datetime as dt, json, os, re, shutil, sys, tempfile, time, traceback
import ideas  # config, Instagram session, Whisper, Claude/Ollama, note writer and notifications
from ideas import log, requests
import vault_sync  # HA «Ideas» list + Markdown dashboards, refreshed every cycle

CFG = ideas.CFG.get("links", {})
STATE = os.path.join(ideas.HERE, "links_state.json")
LINK_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
COLLECTION = CFG.get("collection", "Enlaces")
PROMPT = ideas.PROMPT.replace(
    "You are filing an Instagram post {owner} saved in their collection «{collection}».",
    "You are filing a link {owner} sent to be saved as an idea («{collection}»). If the page itself is not in the "
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


def analyse(url):
    """Idea dict for one link, or None. Instagram goes through ideas.process (same as the collections)."""
    if re.search(r"instagram\.com/(p|reel|reels|tv)/", url):
        cl = ideas.client()
        it = ideas.item(cl.media_info(cl.media_pk_from_url(url)))
        return ideas.process(cl, COLLECTION, "idea", it, set()), it["pk"]
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
        prompt = PROMPT.format(owner=ideas.OWNER, language=ideas.LANGUAGE, collection=COLLECTION,
                               task=ideas.MODES["idea"]["task"].format(owner=ideas.OWNER), context=ideas.CONTEXT,
                               post_url=url, caption=caption, transcript=transcript[:6000] or "(no speech / not a video)",
                               lang=lang, urls=", ".join(sorted(set(LINK_RE.findall(desc)))) or "none", frames_hint=hint)
        try:
            d, brain = ideas.ask_claude(prompt, tmp), "claude"
        except Exception as e:
            log("[links] Claude unavailable -> Ollama:", e)
            ideas._whisper = None
            d, brain = ideas.ask_ollama(prompt.replace(hint, "Frames are attached as images.") if hint else prompt, imgs[:4]), "ollama"
        d["title"] = d.get("title") or d.get("one_liner") or url
        note = ideas.write_note("idea", COLLECTION, d, url, brain, dt.date.today())  # links are always filed
        log(f"[{COLLECTION}] {d['title']} | {brain} | {note}")
        return d, re.sub(r"\W+", "_", url)[-60:]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def handle(url, st):
    """Process one link once; notify per idea. Returns the idea title or None."""
    if url in st["done"]:
        return None
    try:
        d, nid = analyse(url)
    except Exception:
        log("[links] FAILED", url, traceback.format_exc().splitlines()[-1]); return None
    finally:
        ideas._whisper = None  # the watcher runs all day: do not keep Whisper's VRAM between links
    st["done"].append(url); save_state(st)
    if d:
        ideas.notify_item("idea", d, nid)
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
        urls = LINK_RE.findall(f'{it.get("summary", "")} {it.get("description") or ""}')
        if not urls:
            continue
        titles = [t for t in (handle(u, st) for u in urls) if t]
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
                seen.append(m["id"]); del seen[:-500]; save_state(st)
                for u in LINK_RE.findall(m.get("text") or ""):
                    t = handle(u, st)
                    if t:
                        requests.post(f"{B}/v1/chats/{q(chat['id'])}/messages", headers=H, timeout=20,
                                      json={"text": f"💡 Idea guardada: {t}"})


def main():
    st = load_state()
    if "--url" in sys.argv:
        print(handle(sys.argv[sys.argv.index("--url") + 1], st)); return
    while True:
        for src in (from_ha, from_beeper, lambda st: vault_sync.run()):
            try:
                src(st)
            except Exception:
                log("[links]", src.__name__, "error:", traceback.format_exc().splitlines()[-1])
        if "--once" in sys.argv:
            return
        time.sleep(int(CFG.get("interval", 120)))


if __name__ == "__main__":
    main()
