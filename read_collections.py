"""Read every collection's post list from Instagram (no processing, no unsaving), so HA shows real pending counts.

Same sync as a scan, but for all configured collections regardless of the «Todas» switches; Amigable pace, one session.
Usage: python read_collections.py
"""
import json, sys
import ideas


def main():
    lock = ideas.scan_lock()
    if not lock:
        ideas.log("[read] a scan is using the Instagram session; try later"); return 2
    try:
        ideas.AMIGABLE = True
        ideas.CFG["todas"] = None  # every collection, whatever the switches say
        ideas.progress("Leyendo colecciones…")
        state = json.load(open(ideas.STATE, encoding="utf-8"))
        state.setdefault("collections", {})
        cl = ideas.client()
        ideas.sync(cl, state)
        json.dump(state, open(ideas.STATE, "w", encoding="utf-8"), ensure_ascii=False)
        ideas.log("[read] all collections read")
        return 0
    finally:
        ideas.progress("Inactivo", running=False)
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
