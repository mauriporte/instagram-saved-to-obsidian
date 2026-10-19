"""Unsave, in daily batches, the saved posts that were already processed into a complete note before unsaving existed.

Only posts with a complete note in the vault are unsaved; posts skipped as "not relevant" (no note) or with an incomplete
note stay saved. Uses the same single Instagram session as the scans (scan lock) and the Amigable pace. At the end it
notifies how many were unsaved, how many are left, and how many unprocessed posts each collection still has saved.

Usage: python unsave_processed.py [--limit 100] [--dry-run]
"""
import json, re, sys
import ideas
import vault_sync

LIMIT = int(ideas.arg("--limit", ideas.CFG.get("unsave_backlog_per_day", 100)))
MARK = ideas.os.path.join(ideas.HERE, "unsave_backlog.json")  # {"day", "left"}: the watcher starts one batch a day while left > 0


def complete_codes():
    """Instagram codes of posts that have a complete note in the vault."""
    have = {}
    for n in vault_sync.all_notes():
        t = open(n["path"], encoding="utf-8").read()
        for code in re.findall(r"instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", t):
            have[code] = have.get(code, True) and n.get("status") != "incompleta"
    return {c for c, ok in have.items() if ok}


def backlog(st):
    """Processed, still saved posts with a complete note, in collection order."""
    done, unsaved, ok = set(st.get("done", [])), set(st.get("unsaved", [])), complete_codes()
    return [it for name, _ in ideas.COLLECTIONS for it in (st["collections"].get(name) or {}).get("items") or []
            if it["pk"] in done and it["pk"] not in unsaved and it["code"] in ok]


def unprocessed(st):
    """Per collection: posts still saved that have no complete note (pending, or skipped as not relevant)."""
    unsaved, ok = set(st.get("unsaved", [])), complete_codes()
    out = {}
    for name, _ in ideas.COLLECTIONS:
        n = sum(it["pk"] not in unsaved and it["code"] not in ok for it in (st["collections"].get(name) or {}).get("items") or [])
        if n:
            out[name] = n
    return out


def main():
    lock = ideas.scan_lock()
    if not lock:
        ideas.log("[unsave-backlog] a scan is using the Instagram session; trying again later"); return 2
    try:
        st = json.load(open(ideas.STATE, encoding="utf-8"))
        todo = backlog(st)
        batch = todo[:LIMIT]
        ideas.log(f"[unsave-backlog] {len(todo)} processed posts still saved; this batch: {len(batch)}")
        if ideas.DRY:
            print([it["code"] for it in batch[:10]], "..."); return 0
        ideas.AMIGABLE = True  # always the gentle pace for this
        cl = ideas.client() if batch else None
        done = 0
        for it in batch:
            if ideas.unsave(cl, it):
                st.setdefault("unsaved", []).append(it["pk"]); done += 1
                json.dump(st, open(ideas.STATE, "w", encoding="utf-8"), ensure_ascii=False)
        if cl:
            cl.dump_settings(ideas.SESSION)
        left = len(todo) - done
        rest = unprocessed(st)
        lines = [f"Quitados de guardados: {done}. Procesados que siguen guardados: {left}"
                 + (" (sigue mañana)." if left else ".")]
        if rest:
            lines.append("Sin procesar (se quedan en su colección): " + ", ".join(f"{k} {v}" for k, v in rest.items())
                         + f" · total {sum(rest.values())}.")
        ideas.notify("🔖 Insta-Ideas: guardados", "\n".join(lines), "insta_unsave_backlog")
        json.dump({"day": ideas.dt.date.today().isoformat(), "left": left}, open(MARK, "w", encoding="utf-8"))
        return 0
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
