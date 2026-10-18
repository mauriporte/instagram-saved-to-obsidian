"""Ideas outside Obsidian: a Home Assistant to-do list kept in sync with the idea notes, and a plain-Markdown copy of
the vault dashboards (Bases only render inside Obsidian; Home Assistant's notes panel shows Markdown).

- HA list (config "ideas_todo", e.g. todo.ideas): one item per idea note (title; summary, effort/value, link in the
  description). Ticking it marks the note «hecha»; deleting it marks it «descartada». Setting the note's status to
  hecha/descartada in Obsidian ticks the item.
- <notes folder>/../20-dashboards/Dashboards.md: tables of ideas (por revisar, rápidas y útiles, todas) and projects,
  rewritten only when something changed (no Syncthing churn).
Called from links.py on every cycle; `python vault_sync.py` runs it once.
"""
import json, os, re
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


def sync_todo(items_notes, st):
    if not ideas.HA:
        return
    r = ha("POST", "services/todo/get_items?return_response", json={"entity_id": TODO})
    if not r.ok:
        log("[vault] todo list unavailable:", r.status_code); return
    items = (r.json().get("service_response") or {}).get(TODO, {}).get("items", [])
    by_note = {}
    for it in items:
        m = re.search(r"nota: (.+\.md)\s*$", it.get("description") or "")
        if m:
            by_note[m.group(1)] = it
    synced = set(st.get("synced", []))
    for n in items_notes:
        it, status = by_note.get(n["file"]), n.get("status", "nueva")
        if it is None:
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


def publish_sensor(items_notes):
    """sensor.ideas_nuevas: count of ideas not yet hecha/descartada, newest first, each with the path Home Assistant's
    Notes Vault panel opens (/notes-vault?path=…). Pushed every cycle (REST states do not survive a HA restart)."""
    if not ideas.HA:
        return
    pend = sorted((n for n in items_notes if n.get("status", "nueva") not in DONE), key=lambda n: (n.get("created", ""), n["file"]), reverse=True)
    rel = os.path.relpath(IDEAS_DIR, ideas.VAULT).replace(os.sep, "/")
    items = [{"title": n["title"], "summary": n.get("summary", ""), "effort": n.get("effort", ""), "value": n.get("value", ""),
              "collection": n.get("collection", ""), "path": f"{rel}/{n['file']}"} for n in pend]
    ha("POST", "states/sensor.ideas_nuevas", json={"state": len(items), "attributes": {
        "friendly_name": "Ideas nuevas", "icon": "mdi:lightbulb-on", "unit_of_measurement": "ideas",
        "items": items[:30], "dashboard_path": "20-dashboards/Dashboards.md"}})


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


def run():
    st = {}
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
        publish_sensor(ns)
    except Exception as e:
        log("[vault] sensor error:", type(e).__name__, e)
    text = render(ns)
    old = open(DASH, encoding="utf-8").read() if os.path.exists(DASH) else ""
    if text != old:
        os.makedirs(os.path.dirname(DASH), exist_ok=True)
        open(DASH, "w", encoding="utf-8").write(text)
        log("[vault] Dashboards.md updated")
    json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False)


if __name__ == "__main__":
    run()
