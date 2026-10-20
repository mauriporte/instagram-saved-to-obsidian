"""Ideas outside Obsidian: a Home Assistant to-do list kept in sync with the idea notes, and a plain-Markdown copy of
the vault dashboards (Bases only render inside Obsidian; Home Assistant's notes panel shows Markdown).

- HA list (config "ideas_todo", e.g. todo.ideas): one item per idea note (title; summary, effort/value, link in the
  description). Ticking it marks the note «hecha»; deleting it marks it «descartada». Setting the note's status to
  hecha/descartada in Obsidian ticks the item.
- <notes folder>/../20-dashboards/Dashboards.md: tables of ideas (por revisar, rápidas y útiles, todas) and projects,
  rewritten only when something changed (no Syncthing churn).
Called from links.py on every cycle; `python vault_sync.py` runs it once.
"""

import json, os, re, urllib.parse
import ideas
from ideas import log, requests

STATE = os.path.join(ideas.HERE, "vault_sync_state.json")
TODO = ideas.CFG.get("ideas_todo", "todo.ideas")
IDEAS_DIR = os.path.join(ideas.BASE, ideas.MODES["idea"]["folder"])
DASH = os.path.join(ideas.VAULT, "20-dashboards", "Dashboards.md")
DONE = ("hecha", "descartada")


def frontmatter(path):
    t = open(path, encoding="utf-8").read()
    m = re.match(r"---\n(.*?)\n---\n", t, re.S)
    fm = {}
    for line in (m.group(1).splitlines() if m else []):
        k = re.match(r"^([A-Za-z_]+):\s*(.*)$", line)
        if k:
            v = k.group(2).strip()
            try:
                fm[k.group(1)] = json.loads(v) if v[:1] in '"[' else v
            except ValueError:
                fm[k.group(1)] = v.strip('"')
    title = (re.search(r"^# (.+)$", t, re.M) or [None, os.path.splitext(os.path.basename(path))[0]])[1]
    return fm, title, t


def set_status(path, status):
    fm, _, t = frontmatter(path)
    t = re.sub(r'^status: .*$', f'status: "{status}"', t, count=1, flags=re.M)
    t = re.sub(r'^updated: .*$', f'updated: "{ideas.dt.date.today():%Y-%m-%d}"', t, count=1, flags=re.M)
    open(path, "w", encoding="utf-8").write(t)
    log(f"[vault] {os.path.basename(path)} -> {status}")


def notes():
    out = []
    if os.path.isdir(IDEAS_DIR):
        for f in sorted(os.listdir(IDEAS_DIR)):
            if f.endswith(".md"):
                p = os.path.join(IDEAS_DIR, f); fm, title, _ = frontmatter(p)
                if fm.get("type") == "idea":
                    out.append({"file": f, "path": p, "title": title, **fm})
    return out


def ha(method, path, **kw):
    return requests.request(method, f"{ideas.HA}/api/{path}", headers={"Authorization": "Bearer " + ideas.ha_token()}, timeout=30, **kw)


CLEAR_BTN = ideas.CFG.get("ideas_clear_button", "input_button.vaciar_lista_ideas")


def sync_todo(items_notes, st):
    if not ideas.HA:
        return
    r = ha("POST", "services/todo/get_items?return_response", json={"entity_id": TODO})
    if not r.ok:
        log("[vault] todo list unavailable:", r.status_code); return
    items = (r.json().get("service_response") or {}).get(TODO, {}).get("items", [])
    cleared = set(st.get("cleared", []))  # removed with «Vaciar lista»: the notes keep their status, they don't come back
    btn = ha("GET", f"states/{CLEAR_BTN}")
    if btn.ok and pressed_since(btn, "clear_seen", st, lambda s: None):
        for it in items:
            m = re.search(r"nota: (.+\.md)\s*$", it.get("description") or "")
            if m:
                cleared.add(m.group(1))
        ha("POST", "services/todo/remove_item", json={"entity_id": TODO, "item": [it["uid"] for it in items]})
        st["reviewed"] = sorted(set(st.get("reviewed", [])) | {n["file"] for n in all_notes()})  # «Ideas nuevas» back to 0
        log(f"[vault] Ideas list emptied: {len(items)} items (notes unchanged)")
        items = []
    st["cleared"] = sorted(cleared)
    by_note = {}
    for it in items:
        m = re.search(r"nota: (.+\.md)\s*$", it.get("description") or "")
        if m:
            by_note[m.group(1)] = it
    synced = set(st.get("synced", []))
    for n in items_notes:
        it, status = by_note.get(n["file"]), n.get("status", "nueva")
        if it is None:
            if n["file"] in cleared:
                continue
            if n["file"] in synced:  # deleted in HA → discarded
                if status not in DONE:
                    set_status(n["path"], "descartada")
                continue
            if status in DONE:
                continue  # already closed before it reached the list
            desc = "\n".join(x for x in (n.get("summary", ""), f"Esfuerzo: {n.get('effort') or '?'} · Valor: {n.get('value') or '?'}",
                                         n.get("url", ""), f"nota: {n['file']}") if x)
            ha("POST", "services/todo/add_item", json={"entity_id": TODO, "item": n["title"][:200], "description": desc})
            synced.add(n["file"])
        else:
            synced.add(n["file"])
            if it["status"] == "completed" and status not in DONE:
                set_status(n["path"], "hecha")
            elif it["status"] == "needs_action" and status in DONE:
                ha("POST", "services/todo/update_item", json={"entity_id": TODO, "item": it["uid"], "status": "completed"})
    st["synced"] = sorted(synced)


CHECK = re.compile(r"^- \[( |x|X)\] Completada\s*$", re.M)


def sync_checkboxes(st):
    """Every note has a «- [ ] Completada» checkbox (Notes and Obsidian). Ticking or unticking it sets the note's status
    ("hecha" / back to "nueva"); a status changed elsewhere (e.g. ticked in the Ideas list) ticks or unticks the box.
    Notes without the box get it, ticked if already "hecha". st["checked"] remembers the last box state per note."""
    checked = set(st.get("checked", []))
    first = "checked" not in st
    for n in all_notes():
        t = open(n["path"], encoding="utf-8").read()
        m = CHECK.search(t)
        done = n.get("status") == "hecha"
        if not m:  # older note: add the box under the title
            box = "- [x] Completada" if done else "- [ ] Completada"
            t2 = re.sub(r"^(# .+\n)", lambda h: h.group(1) + "\n" + box + "\n", t, count=1, flags=re.M)
            if t2 != t:
                open(n["path"], "w", encoding="utf-8").write(t2)
            (checked.add if done else checked.discard)(n["file"])
            continue
        box = m.group(1).lower() == "x"
        if not first and box != (n["file"] in checked):  # the user ticked/unticked it
            if box != done:
                set_status(n["path"], "hecha" if box else "nueva")
        elif box != done:  # status changed elsewhere: the box follows
            t = t[:m.start()] + ("- [x] Completada" if done else "- [ ] Completada") + t[m.end():]
            open(n["path"], "w", encoding="utf-8").write(t)
            box = done
        (checked.add if box else checked.discard)(n["file"])
    st["checked"] = sorted(checked)


def publish_sensor(st):
    """Counters for the Ideas dashboard, over every Insta-Ideas note (all collections):
    sensor.ideas_nuevas = still open and not reviewed yet («Vaciar lista» marks everything processed so far as reviewed),
    newest first with the path the Notes Vault panel opens; sensor.ideas_total = every idea processed;
    sensor.ideas_completadas = marked complete ("hecha"). Pushed every cycle (REST states do not survive a HA restart)."""
    if not ideas.HA:
        return
    every = all_notes()
    reviewed = set(st.get("reviewed", []))
    pend = sorted((n for n in every if n.get("status", "nueva") not in DONE and n["file"] not in reviewed),
                  key=lambda n: (n.get("created", ""), n["file"]), reverse=True)
    rel = lambda n: os.path.relpath(n["path"], ideas.VAULT).replace(os.sep, "/")
    items = [{"title": n["title"], "summary": n.get("summary", ""), "effort": n.get("effort", ""), "value": n.get("value", ""),
              "collection": n.get("collection", ""), "path": rel(n)} for n in pend]
    ha("POST", "states/sensor.ideas_nuevas", json={"state": len(items), "attributes": {
        "friendly_name": "Ideas nuevas", "icon": "mdi:lightbulb-on", "unit_of_measurement": "ideas",
        "items": items[:30], "dashboard_path": "20-dashboards/Dashboards.md"}})
    ha("POST", "states/sensor.ideas_total", json={"state": len(every), "attributes": {
        "friendly_name": "Ideas procesadas", "icon": "mdi:lightbulb-group", "unit_of_measurement": "ideas"}})
    done = sorted((n for n in every if n.get("status") == "hecha"), key=lambda n: (n.get("updated", ""), n["file"]))  # oldest first
    ha("POST", "states/sensor.ideas_completadas", json={"state": len(done), "attributes": {
        "friendly_name": "Completadas", "icon": "mdi:check-circle", "unit_of_measurement": "ideas",
        "items": [{"t": n["title"], "c": n.get("collection", ""), "d": n.get("updated", ""), "s": (n.get("summary") or "")[:110],
                   "p": rel(n)} for n in done]}})  # oldest completion first, for the «Completadas» page


VIEW_SELECT = ideas.CFG.get("collection_view_entity", "input_select.ideas_ver_coleccion")


def col_slug(name):
    """Same as Home Assistant's slugify for these names: 'Música' -> 'musica', 'What2Do' -> 'what2do'."""
    return re.sub(r"[^a-z0-9]+", "_", ideas.norm(name)).strip("_")


MOVE_IDEA = ideas.CFG.get("move_idea_entity", "input_select.ideas_mover_idea")
MOVE_TO = ideas.CFG.get("move_to_entity", "input_select.ideas_mover_destino")
MOVE_BTN = ideas.CFG.get("move_button", "input_button.ideas_mover")
LINKS_COLLECTION = ideas.CFG.get("links", {}).get("collection", "Work")


def mode_of(collection):
    """Folder/mode a collection's notes belong in: its configured mode; None for a collection that is not configured."""
    return next((m for n, m in ideas.COLLECTIONS if ideas.norm(n) == ideas.norm(collection)), None)  # None: not configured (any more)


def all_notes():
    """Every Insta-Ideas note in every mode folder, with its frontmatter."""
    out = []
    for mode, m in ideas.MODES.items():
        d = os.path.join(ideas.BASE, m["folder"])
        for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if f.endswith(".md"):
                fm, title, _ = frontmatter(os.path.join(d, f))
                if fm.get("collection"):
                    out.append({"file": f, "path": os.path.join(d, f), "title": title, "mode": mode, **fm})
    return out


def set_field(path, key, value):
    t = open(path, encoding="utf-8").read()
    t = re.sub(rf"^{key}: .*$", f"{key}: {json.dumps(value, ensure_ascii=False)}", t, count=1, flags=re.M)
    open(path, "w", encoding="utf-8").write(t)


def relocate(notes_):
    """A note whose `collection` was changed (in Obsidian or by the HA mover) goes to that collection's folder."""
    moved = 0
    for n in notes_:
        mode = mode_of(n["collection"])
        if mode is None or mode == n["mode"]:  # unknown collections (e.g. deleted ones) stay where they are
            continue
        dest_dir = os.path.join(ideas.BASE, ideas.MODES[mode]["folder"])
        os.makedirs(dest_dir, exist_ok=True)
        dest, i = os.path.join(dest_dir, n["file"]), 2
        while os.path.exists(dest):  # never overwrite a note with the same name
            dest = os.path.join(dest_dir, f"{n['file'][:-3]} ({i}).md"); i += 1
        set_field(n["path"], "type", mode)
        os.replace(n["path"], dest)
        log(f"[vault] moved {n['file']} -> {ideas.MODES[mode]['folder']} ({n['collection']})")
        moved += 1
    return moved


def label(n):
    return f"{n['title']} · {n.get('created', '')}"[:250]  # name first: the dropdown is narrow


def check_move(st, save):
    """HA mover (called every ~10 s by links.py): keeps «Idea a mover» listing the viewed collection, and on the Mover
    button sets the chosen note's collection to «Mover a» and moves it."""
    if not ideas.HA:
        return
    get = lambda e: ha("GET", f"states/{e}")
    view, idea_sel, to_sel, btn = (get(e) for e in (VIEW_SELECT, MOVE_IDEA, MOVE_TO, MOVE_BTN))
    if not all(r.ok for r in (view, idea_sel, to_sel, btn)):
        return
    viewed = view.json()["state"]
    notes_ = [n for n in all_notes() if n["collection"] == viewed]
    notes_.sort(key=lambda n: (n.get("created", ""), n["file"]), reverse=True)
    labels = [label(n) for n in notes_] or ["(sin notas)"]
    if idea_sel.json()["attributes"].get("options") != labels:
        ha("POST", "services/input_select/set_options", json={"entity_id": MOVE_IDEA, "options": labels})
    names = view.json()["attributes"].get("options") or []
    targets = sorted(set(names) | {n for n, _ in ideas.COLLECTIONS}, key=ideas.norm)  # alphabetical
    if to_sel.json()["attributes"].get("options") != targets:
        ha("POST", "services/input_select/set_options", json={"entity_id": MOVE_TO, "options": targets})
    chosen, target = idea_sel.json()["state"], to_sel.json()["state"]
    n = next((x for x in notes_ if label(x) == chosen), None)
    if pressed_since(btn, "move_seen", st, save) and n and target != n["collection"]:
        set_field(n["path"], "collection", target)
        n["collection"] = target
        relocate([n])
        ideas.notify("📂 Idea movida", f"«{n['title']}»: {viewed} → {target}", "ideas_movida")
        publish_collections()
    dbtn = get(DELETE_BTN)
    if dbtn.ok and pressed_since(dbtn, "delete_seen", st, save) and n:
        trash_note(n)
        publish_collections()
    note_mover(st, save)


def trash_note(n):
    trash = os.path.join(ideas.VAULT, ".trash")  # Obsidian's own trash: the note can be restored from there
    os.makedirs(trash, exist_ok=True)
    dest, i = os.path.join(trash, n["file"]), 2
    while os.path.exists(dest):
        dest = os.path.join(trash, f"{n['file'][:-3]} ({i}).md"); i += 1
    os.replace(n["path"], dest)
    log(f"[vault] deleted {n['file']} ({n['collection']}) -> .trash")
    ideas.notify("🗑️ Idea eliminada", f"«{n['title']}» ({n['collection']}) está en la papelera de Obsidian (.trash).", "ideas_eliminada")


MOVE_NOTE = ideas.CFG.get("move_note_entity", "input_text.ideas_mover_nota")


def note_mover(st, save):
    """«Mover» in a note's bottom bar in Notes (patch 09) writes "<ms>|<collection>|<note path>" to MOVE_NOTE: move it."""
    r = ha("GET", f"states/{MOVE_NOTE}")
    if not r.ok:
        return
    val = r.json()["state"]
    if val == st.get("move_note_seen"):
        return
    first = "move_note_seen" not in st
    st["move_note_seen"] = val; save(st)
    parts = val.split("|", 2)
    if first or len(parts) != 3:
        return
    target, path = parts[1], parts[2]
    names = {c for c, _ in ideas.COLLECTIONS}
    n = next((x for x in all_notes() if os.path.relpath(x["path"], ideas.VAULT).replace(os.sep, "/") == path), None)
    if n and target in names and target != n["collection"]:
        old = n["collection"]
        set_field(n["path"], "collection", target)
        n["collection"] = target
        relocate([n])
        ideas.notify("📂 Idea movida", f"«{n['title']}»: {old} → {target}", "ideas_movida")
        publish_collections()


DELETE_BTN = ideas.CFG.get("delete_button", "input_button.eliminar_idea")


def pressed_since(btn, key, st, save):
    """True once per new press of an HA input_button (its state is the last press time); the first look only remembers."""
    pressed = btn.json()["state"]
    if pressed == st.get(key):
        return False
    first = key not in st
    st[key] = pressed; save(st)
    return not first


def publish_pending():
    """sensor.insta_ideas_pendientes: posts left to process per collection (every configured collection), from the
    last Instagram sync in state.json; set-aside posts (failed MAX_TRIES times) are counted apart."""
    if not ideas.HA or not os.path.exists(ideas.STATE):
        return
    st = json.load(open(ideas.STATE, encoding="utf-8"))
    done, failed, unsaved = set(st.get("done", [])), st.get("failed", {}), set(st.get("unsaved", []))
    rows = []
    for name, _ in ideas.COLLECTIONS:
        items = (st.get("collections", {}).get(name) or {}).get("items") or []
        left = [i for i in items if i["pk"] not in done]
        aside = sum(failed.get(i["pk"], 0) >= ideas.MAX_TRIES for i in left)
        rows.append({"c": name, "p": len(left) - aside, "f": aside, "t": sum(i["pk"] not in unsaved for i in items)})  # still saved on Instagram
    ig = st.get("ig_collections") or {}  # every Instagram collection with its saved count, from the last sync
    for r in rows:  # one sensor per collection, for the clickable list (a row selects that collection in the viewer)
        # "de N" = our own still-saved count: Instagram's media_count lags behind unsaves
        ha("POST", f"states/sensor.insta_ideas_pendientes_{col_slug(r['c'])}", json={"state": r["p"], "attributes": {
            "friendly_name": r["c"], "icon": "mdi:folder-clock-outline", "unit_of_measurement": f"de {r['t']}",
            "collection": r["c"], "set_aside": r["f"], "processed": True}})
    configured = {ideas.norm(r["c"]) for r in rows}
    for name, count in ig.items():  # Instagram collections Insta-Ideas doesn't process: just how many are saved
        if ideas.norm(name) not in configured:
            ha("POST", f"states/sensor.insta_ideas_pendientes_{col_slug(name)}", json={"state": count, "attributes": {
                "friendly_name": name, "icon": "mdi:folder-off-outline", "unit_of_measurement": "guardados · no se procesa",
                "collection": name, "processed": False}})
    names = sorted({r["c"] for r in rows} | {n for n in ig if ideas.norm(n) not in configured}, key=ideas.norm)
    by = {ideas.norm(r["c"]): r for r in rows}
    labels = {}
    for n in names:
        r = by.get(ideas.norm(n))
        labels[n] = f"{n} · {r['p']} de {r['t']}" if r else f"{n} · {ig.get(n, 0)} guardados"
    sync_switches(names, labels, set(by))  # first: a flipped switch updates the «Todas» list
    publish_totals(st, rows)


def publish_totals(st, rows):
    """The Pendientes total and the scan's «Colección» dropdown: only collections switched on («Todas» list)."""
    on = [r for r in rows if in_todas(r["c"])]
    ha("POST", "states/sensor.insta_ideas_pendientes", json={"state": sum(r["p"] for r in on), "attributes": {
        "friendly_name": "Ideas: pendientes", "icon": "mdi:progress-clock",
        "last_sync": st.get("last_sync"), "rows": rows}})
    ent = ideas.CFG.get("collection_entity")  # options carry the posts left: "Work · 49"
    sel = ha("GET", f"states/{ent}") if ent else None
    if sel is not None and sel.ok:
        opts = sel.json()["attributes"]["options"]
        base = lambda o: re.sub(r" · \d+$", "", o)
        new = [f"Todas · {sum(r['p'] for r in on)}"] + [f"{r['c']} · {r['p']}" for r in sorted(on, key=lambda r: ideas.norm(r["c"]))]
        if opts != new:
            current = base(sel.json()["state"])
            ha("POST", "services/input_select/set_options", json={"entity_id": ent, "options": new})
            keep = next((o for o in new if base(o) == current), new[0])
            ha("POST", "services/input_select/select_option", json={"entity_id": ent, "option": keep})


PENDING_CARD = "Pendientes por colección"


def ws_session():
    """An authenticated Home Assistant WebSocket (helpers and dashboards are only editable there)."""
    import websocket  # websocket-client
    ws = websocket.create_connection(ideas.HA.replace("http", "ws", 1) + "/api/websocket", timeout=20)
    ws.recv(); ws.send(json.dumps({"type": "auth", "access_token": ideas.ha_token()})); ws.recv()
    n = [0]

    def call(msg):
        n[0] += 1
        ws.send(json.dumps({"id": n[0], **msg}))
        while True:
            r = json.loads(ws.recv())
            if r.get("id") == n[0]:
                return r
    return ws, call


def set_processing(name, on):
    """A «Pendientes por colección» switch: whether the collection is included in «Todas» runs (config "todas").
    Every collection stays configured (so it's in the dropdowns); one chosen alone in «Colección» is always processed."""
    p = os.path.join(ideas.HERE, "config.json")
    c = json.load(open(p, encoding="utf-8"))
    todas = c.get("todas") or [n for n, _ in c["collections"]]
    has = any(ideas.norm(t) == ideas.norm(name) for t in todas)
    if on == has:
        return
    todas = todas + [name] if on else [t for t in todas if ideas.norm(t) != ideas.norm(name)]
    c["todas"] = todas
    json.dump(c, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    ideas.CFG["todas"] = todas
    log(f"[vault] Todas {'+' if on else '-'} {name}")


def in_todas(name):
    todas = ideas.CFG.get("todas")
    return todas is None or any(ideas.norm(t) == ideas.norm(name) for t in todas)


def sync_switches(names, labels, processed):
    """«Pendientes por colección»: one on/off switch per Instagram collection (input_boolean.insta_proc_<slug>), named
    with its counts. A switch the user flips turns processing on/off in config.json; new collections get a switch set to
    their current state, and the dashboard list is rebuilt (alphabetical) when the set of collections changes."""
    ent = lambda n: f"input_boolean.insta_proc_{col_slug(n)}"
    ws, call = ws_session()
    try:
        helpers = {h["id"]: h for h in call({"type": "input_boolean/list"})["result"]}
        for n in names:
            hid = ent(n).split(".", 1)[1]
            want = ideas.norm(n) in processed and in_todas(n)
            if hid not in helpers:  # created with the id as its name (HA derives the id from the name), then renamed
                call({"type": "input_boolean/create", "name": hid, "icon": "mdi:folder-sync-outline", "initial": want})
                call({"type": "input_boolean/update", "input_boolean_id": hid, "name": labels[n], "icon": "mdi:folder-sync-outline",
                      "initial": want})
                ha("POST", f"services/input_boolean/turn_{'on' if want else 'off'}", json={"entity_id": ent(n)})
                continue
            if helpers[hid].get("name") != labels[n]:
                call({"type": "input_boolean/update", "input_boolean_id": hid, "name": labels[n], "icon": "mdi:folder-sync-outline"})
            s = ha("GET", f"states/{ent(n)}")
            if s.ok and s.json()["state"] in ("on", "off") and (s.json()["state"] == "on") != want:
                if getattr(sync_switches, "ready", False):
                    set_processing(n, s.json()["state"] == "on")  # the user flipped it
                else:  # first cycle after a start: the config is the truth, the switch follows it
                    ha("POST", f"services/input_boolean/turn_{'on' if want else 'off'}", json={"entity_id": ent(n)})
        sync_switches.ready = True
        dash = ideas.CFG.get("ideas_dashboard", "dashboard-ideas")
        key = "|".join(names)
        if getattr(sync_switches, "last", None) == key:
            return
        cfg = call({"type": "lovelace/config", "url_path": dash})["result"]
        card = next((c for v in cfg["views"] for s in v.get("sections", []) for c in s.get("cards", [])
                     if c.get("type") == "entities" and c.get("title") == PENDING_CARD), None)
        if card is None:
            return
        rows = [{"entity": ent(n), "tap_action": {"action": "perform-action", "perform_action": "input_select.select_option",
                 "target": {"entity_id": VIEW_SELECT}, "data": {"option": n}}} for n in names]
        if card.get("entities") != rows:
            card["entities"] = rows
            log(f"[vault] {PENDING_CARD}: {len(rows)} collections",
                call({"type": "lovelace/config/save", "url_path": dash, "config": cfg}).get("success"))
        sync_switches.last = key
    finally:
        ws.close()


def publish_collections():
    """sensor.ideas_coleccion_<slug>: every recognised note of one collection (all modes), newest first, for the
    «Por colección» viewer in Home Assistant; keeps the viewer's selector options in step with the collections."""
    if not ideas.HA:
        return
    relocate(all_notes())  # collection edited in Obsidian: the file goes to that collection's folder first
    groups = {}
    for n in all_notes():
        rel = os.path.relpath(os.path.dirname(n["path"]), ideas.VAULT).replace(os.sep, "/")
        groups.setdefault(n["collection"], []).append({
            "t": n["title"], "s": (n.get("summary") or "")[:110], "d": n.get("created", ""),
            "st": n.get("status", ""), "p": f"{rel}/{n['file']}"})
    order = [n for n, _ in ideas.COLLECTIONS] + sorted(set(groups) - {n for n, _ in ideas.COLLECTIONS})
    configured = {n for n, _ in ideas.COLLECTIONS}
    names = sorted((n for n in order if n in groups or n in configured), key=ideas.norm)  # every collection, alphabetical
    for name in names:
        items = sorted(groups.get(name, []), key=lambda x: (x["d"], x["p"]), reverse=True)
        ha("POST", f"states/sensor.ideas_coleccion_{col_slug(name)}", json={"state": len(items), "attributes": {
            "friendly_name": f"Ideas: {name}", "icon": "mdi:folder-multiple-image", "unit_of_measurement": "notas",
            "collection": name, "items": items}})
    live = {f"sensor.ideas_coleccion_{col_slug(n)}" for n in names}
    for e in [x["entity_id"] for x in ha("GET", "states").json() if x["entity_id"].startswith("sensor.ideas_coleccion_")]:
        if e not in live:  # a collection that was emptied (its notes moved elsewhere)
            ha("DELETE", f"states/{e}")
    sel = ha("GET", f"states/{VIEW_SELECT}")
    if sel.ok and sel.json()["attributes"].get("options") != names and names:
        ha("POST", "services/input_select/set_options", json={"entity_id": VIEW_SELECT, "options": names})
        if sel.json()["state"] in names:
            ha("POST", "services/input_select/select_option", json={"entity_id": VIEW_SELECT, "option": sel.json()["state"]})


def cell(s):
    return str(s or "—").replace("|", "/").replace("\n", " ")


def render(items_notes):
    link = lambda n: f"[[{n['file'][:-3]}\\|{cell(n['title'])}]]"  # escaped pipe: wikilink alias inside a table
    rows = lambda ns, cols: "\n".join("| " + " | ".join(link(n) if c == "title" else cell(n.get(c)) for c in cols) + " |" for n in ns) or "| — |"
    head = lambda names: "| " + " | ".join(names) + " |\n|" + "---|" * len(names)
    new = sorted(items_notes, key=lambda n: n.get("created", ""), reverse=True)
    pending = [n for n in new if n.get("status", "nueva") not in DONE]
    quick = [n for n in pending if n.get("effort") == "bajo" and n.get("value") in ("alto", "medio")]
    projects = []
    for d, _, fs in os.walk(os.path.join(ideas.VAULT, "30-projects")):
        if d != os.path.join(ideas.VAULT, "30-projects"):
            continue
        for f in sorted(fs):
            if f.endswith(".md"):
                fm, title, _ = frontmatter(os.path.join(d, f))
                if fm.get("type") in ("project", "task"):
                    projects.append({"file": f, "title": title, **fm})
    projects.sort(key=lambda n: n.get("updated", ""), reverse=True)
    return f"""---
type: dashboard
tags: [dashboard]
summary: Copia en Markdown de los dashboards (Bases) para verla fuera de Obsidian, p. ej. en Home Assistant. Se regenera sola.
related: ["[[Home]]"]
---
# Dashboards

Copia automática de [[Ideas.base|Ideas]] y [[Proyectos.base|Proyectos]] (en Obsidian, mejor abrir esos: son editables).

## Ideas por revisar ({len(pending)})
{head(["Idea", "Resumen", "Esfuerzo", "Valor", "Colección"])}
{rows(pending, ["title", "summary", "effort", "value", "collection"])}

## Rápidas y útiles ({len(quick)})
{head(["Idea", "Resumen", "Valor"])}
{rows(quick, ["title", "summary", "value"])}

## Todas las ideas ({len(new)})
{head(["Idea", "Estado", "Esfuerzo", "Valor", "Fecha"])}
{rows(new, ["title", "status", "effort", "value", "created"])}

## Proyectos ({len(projects)})
{head(["Proyecto", "Tipo", "Estado", "Actualizado"])}
{rows(projects, ["title", "type", "status", "updated"])}

---
Volver a [[Home]]
"""


HOME_INFO = ideas.CFG.get("home_info")  # {"folder": "20-dashboards/Home Info", "note": "20-dashboards/Home Info.md"}


def home_info():
    """Home Info history: the newest report stays in `folder`, older ones go to `folder`/Historial. The note shows
    every report of the newest day (newest on top, PDF linked) and lists earlier days; each earlier day gets its own
    note in Historial with all that day's reports, newest on top. Newest = last written."""
    if not HOME_INFO:
        return
    d = os.path.join(ideas.VAULT, HOME_INFO["folder"]); hist = os.path.join(d, "Historial")
    found = [os.path.join(x, f) for x in (d, hist) if os.path.isdir(x) for f in os.listdir(x)
             if f.startswith("home-info-") and f.endswith(".html")]
    if not found:
        return
    found.sort(key=os.path.getmtime, reverse=True)
    stem = lambda p: os.path.splitext(os.path.basename(p))[0]
    newest = stem(found[0])
    os.makedirs(hist, exist_ok=True)
    for f in os.listdir(d):  # everything that isn't the newest report moves to the history
        if f.startswith("home-info-") and os.path.splitext(f)[0] != newest and os.path.isfile(os.path.join(d, f)):
            os.replace(os.path.join(d, f), os.path.join(hist, f))
            log(f"[vault] Home Info: {f} -> Historial")
    if os.path.isfile(os.path.join(hist, newest + ".html")):  # the newest came back from the history
        for ext in (".html", ".pdf"):
            if os.path.isfile(os.path.join(hist, newest + ext)):
                os.replace(os.path.join(hist, newest + ext), os.path.join(d, newest + ext))
    folder = lambda s: d if s == newest else hist
    pdf = lambda s: f" · [[{s}.pdf|PDF]]" if os.path.isfile(os.path.join(folder(s), s + ".pdf")) else ""
    name = lambda s: s.removeprefix("home-info-")  # "2026-10-18" or "2026-10-18-tarde"
    by_day = {}  # day -> its reports, newest first (found is newest first)
    for p in found:
        by_day.setdefault(name(stem(p))[:10], []).append(stem(p))

    def page(day):
        """Every report of one day in one view, newest on top."""
        if len(by_day[day]) == 1:
            parts = [f"## {name(s)}{pdf(s)}\n\n![[{s}.html]]\n" for s in by_day[day]]
        else:  # several the same day: each one folds, the newest open (Notes Vault patch 08)
            parts = [f"PDF: [[{s}.pdf|{name(s)}]]\n" * bool(pdf(s)) + f"![[{s}.html|{'abierto' if i == 0 else 'plegado'}: {name(s)}]]\n"
                     for i, s in enumerate(by_day[day])]
        return f'---\ntype: report\nupdated: "{day}"\n---\n' + "\n".join(parts)  # the panel already shows the note name

    def write(path, text):
        if not os.path.exists(path) or open(path, encoding="utf-8").read() != text:
            open(path, "w", encoding="utf-8").write(text)
            log(f"[vault] Home Info: {os.path.relpath(path, ideas.VAULT)} updated")

    # the main note: the newest day; Historial: one note per earlier day ("2026-10-18 - Home Info")
    days = list(by_day)
    pages = {f"{day} - Home Info.md": day for day in days[1:]}
    for f in os.listdir(hist):
        if re.match(r"\d{4}-\d{2}-\d{2} (- )?Home Info.*\.md$", f) and f not in pages:
            os.remove(os.path.join(hist, f))  # a day that is now the newest, or whose reports were removed
    for f, day in pages.items():
        write(os.path.join(hist, f), page(day))
    rows = "\n".join(f"- [[{f[:-3]}|{day}]] ({len(by_day[day])})" for f, day in pages.items())
    write(os.path.join(ideas.VAULT, HOME_INFO["note"]),
          page(days[0]) + f"\n## Historial\n{rows or '_Todavía no hay días anteriores._'}\n")


def run():
    st = {}
    try:
        home_info()
    except Exception as e:
        log("[vault] Home Info error:", type(e).__name__, e)
    try:
        st = json.load(open(STATE, encoding="utf-8"))
    except (OSError, ValueError):
        pass
    ns = notes()
    try:
        sync_todo(ns, st)
    except Exception as e:
        log("[vault] todo sync error:", type(e).__name__, e)
    ns = notes()  # statuses may have changed
    try:
        sync_checkboxes(st)
        publish_sensor(st)
    except Exception as e:
        log("[vault] sensor error:", type(e).__name__, e)
    try:
        publish_collections()
        publish_pending()
        ideas.retry_calendar()  # Attending events that could not be added yet
    except Exception as e:
        log("[vault] collections error:", type(e).__name__, e)
    text = render(ns)
    old = open(DASH, encoding="utf-8").read() if os.path.exists(DASH) else ""
    if text != old:
        os.makedirs(os.path.dirname(DASH), exist_ok=True)
        open(DASH, "w", encoding="utf-8").write(text)
        log("[vault] Dashboards.md updated")
    json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False)


if __name__ == "__main__":
    run()
