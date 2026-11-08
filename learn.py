"""Ollama self-learning hooks for the note writers (one folder per job, shared with the nightly learner).

<learner_dir>/<job>/lessons.md     "- (YYYY-MM-DD) lesson" lines, written by the nightly learner; added to every prompt
<learner_dir>/<job>/signals.jsonl  {"date", "kind", "detail"} per line: what the owner did to the notes (written here)
<learner_dir>/<job>/eval.jsonl     {"input": {collection, mode, caption, url}, "must": [...], "must_not": [...]}
<learner_dir>/<job>/notes.json     notes written by this job (vault-relative path -> sha, source, date), to spot edits

    python learn.py eval <job> [--no-lessons]   -> prints {"job", "score", "passed", "n"} (Ollama only)
    python learn.py signals                     -> scans the vault for edited / moved / deleted notes now
"""
import datetime as dt, difflib, hashlib, json, os, re, sys

DIR = None    # learner_dir from config.json; None = learning off
VAULT = None
KEEP_DAYS = 90  # a note is watched for edits this long after it was written
CHECKBOX = re.compile(r"^- \[[ xX]\] ")


def configure(learner_dir, vault):
    global DIR, VAULT
    DIR, VAULT = learner_dir, vault


def _path(job, name):
    return os.path.join(DIR, job, name)


def lessons(job):
    """The job's lessons as a prompt block ("" when there are none or learning is off)."""
    if not DIR:
        return ""
    try:
        lines = [l.strip() for l in open(_path(job, "lessons.md"), encoding="utf-8") if l.strip().startswith("- ")]
    except OSError:
        return ""
    return "\n\nLessons learned from earlier notes (follow them):\n" + "\n".join(lines) if lines else ""


def _load(job):
    try:
        return json.load(open(_path(job, "notes.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return {"notes": {}}


def _save(job, index):
    os.makedirs(os.path.join(DIR, job), exist_ok=True)
    tmp = _path(job, "notes.json.tmp")
    json.dump(index, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(tmp, _path(job, "notes.json"))


def _sha(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def record(job, note_path, source):
    """Remember a note this job just wrote, so later edits/moves/deletes by the owner become signals."""
    if not DIR:
        return
    rel = os.path.relpath(note_path, VAULT).replace("\\", "/")
    index = _load(job)
    text = open(note_path, encoding="utf-8").read()
    index["notes"][rel] = {"sha": _sha(text), "source": source, "date": dt.date.today().isoformat(), "text": text}
    _save(job, index)


def _signal(job, kind, detail):
    with open(_path(job, "signals.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"date": dt.date.today().isoformat(), "kind": kind, "detail": detail}, ensure_ascii=False) + "\n")


def _changes(old, new):
    """What the owner changed: ticked boxes are a status, anything else a real edit (compact +/- lines)."""
    diff = [l for l in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
            if l[:1] in "+-" and not l.startswith(("+++", "---"))]
    status = [l[1:] for l in diff if l[0] == "+" and CHECKBOX.match(l[1:])]  # the boxes as they are now
    edits = [l for l in diff if not CHECKBOX.match(l[1:]) and not l[1:].startswith("updated:")]
    return status, edits


def scan(jobs, today=None):
    """Compare every watched note with the vault: deleted, moved (same source, other folder) or edited by the owner."""
    if not DIR:
        return
    today = today or dt.date.today()
    by_source = {}
    for root, dirs, files in os.walk(VAULT):
        dirs[:] = [d for d in dirs if not d.startswith(".")]  # skip .stversions, .obsidian, .trash
        for f in files:
            if f.endswith(".md"):
                p = os.path.join(root, f)
                try:
                    m = re.search(r'^source: "([^"]+)"', open(p, encoding="utf-8").read(3000), re.M)
                except OSError:
                    continue
                if m:
                    by_source.setdefault(m.group(1), []).append(os.path.relpath(p, VAULT).replace("\\", "/"))
    for job in jobs:
        index = _load(job)
        notes, keep = index["notes"], {}
        for rel, n in notes.items():
            title = os.path.basename(rel)[11:-3]
            path = os.path.join(VAULT, rel)
            if not os.path.exists(path):
                elsewhere = [r for r in by_source.get(n["source"], []) if r != rel]
                if elsewhere:
                    _signal(job, "moved", f"«{title}» moved from {os.path.dirname(rel)} to {os.path.dirname(elsewhere[0])}")
                    new = elsewhere[0]
                    keep[new] = {**n, "sha": _sha(open(os.path.join(VAULT, new), encoding="utf-8").read())}
                else:
                    _signal(job, "deleted", f"«{title}» ({os.path.dirname(rel)}) deleted by the owner: the note was not wanted")
                continue
            text = open(path, encoding="utf-8").read()
            if _sha(text) != n["sha"]:
                old = n.get("text")
                status, edits = _changes(old, text) if old else ([], ["(previous text not kept)"])
                if status:
                    _signal(job, "status", f"«{title}»: " + "; ".join(status)[:300])
                if edits:
                    _signal(job, "edited", f"«{title}» edited by the owner:\n" + "\n".join(edits)[:800])
                n = {**n, "sha": _sha(text)}
            if dt.date.fromisoformat(n["date"]) >= today - dt.timedelta(days=KEEP_DAYS):
                keep[rel] = {**n, "text": text}  # keep the text so the next edit can be shown as a diff
        index["notes"], index["last_scan"] = keep, today.isoformat()
        _save(job, index)


def daily(jobs):
    """Run scan() at most once a day (called from the watcher loop)."""
    if not DIR:
        return
    last = min((_load(j).get("last_scan", "") for j in jobs), default="")
    if last < dt.date.today().isoformat():
        scan(jobs)


def run_eval(job, use_lessons=True):
    import ideas  # lazy: ideas imports this module
    cases = [json.loads(l) for l in open(_path(job, "eval.jsonl"), encoding="utf-8") if l.strip()]
    passed = 0
    for c in cases:
        i = c["input"]
        prompt = ideas.post_prompt(i["collection"], i["mode"], i["url"], i["caption"])
        if "threads.com" in i["url"]:
            prompt = prompt.replace("an Instagram post", "a Threads post").replace("Instagram post:", "Threads post:")
        try:
            out = json.dumps(ideas.ask_ollama(prompt + (lessons(job) if use_lessons else ""), []), ensure_ascii=False).lower()
        except Exception:
            out = ""
        passed += bool(out) and all(m.lower() in out for m in c.get("must", [])) and not any(m.lower() in out for m in c.get("must_not", []))
    return {"job": job, "score": round(passed / len(cases), 3) if cases else 0, "passed": passed, "n": len(cases)}


if __name__ == "__main__":
    args, sys.argv = sys.argv[1:], sys.argv[:1]  # ideas reads its own flags from sys.argv
    import ideas  # configures DIR/VAULT from config.json
    if args[:1] == ["eval"]:
        print(json.dumps(run_eval(args[1], "--no-lessons" not in args)))
    elif args[:1] == ["signals"]:
        scan(["insta-ideas", "threads-ideas"])
