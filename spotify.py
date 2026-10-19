"""Follow Música artists on Spotify (Spotify Web API, free).

One-time login:  python spotify.py login
  Uses the Spotify app configured in config.json "spotify" (client_id + its registered redirect_uri) with PKCE, so no
  client secret is needed. Your browser opens Spotify's consent page; you log in and approve there. The resulting token
  (renewable) is kept in spotify_token.json next to this file (git-ignored).

Then ideas.py calls follow_artist(<Spotify artist URL or id>) for each Música post and, when the artist is newly
followed, save_track(<Spotify track URL>) to add the song to Liked Songs.
"""
import base64, hashlib, json, os, secrets, sys, time, urllib.parse, webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8")).get("spotify") or {}
TOKEN = os.path.join(HERE, "spotify_token.json")
SCOPES = "user-follow-read user-follow-modify user-library-modify"


def login():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    redirect = urllib.parse.urlsplit(CFG["redirect_uri"])
    url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": CFG["client_id"], "response_type": "code", "redirect_uri": CFG["redirect_uri"],
        "code_challenge_method": "S256", "code_challenge": challenge, "scope": SCOPES, "state": state})
    got = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
            if urllib.parse.urlsplit(self.path).path != redirect.path or q.get("state") != state:
                self.send_error(404); return
            got.update(q)
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers()
            self.wfile.write("Insta-Ideas: Spotify conectado. Ya puedes cerrar esta pestaña.".encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer((redirect.hostname, redirect.port), H)
    print("Opening Spotify's consent page in your browser…")
    webbrowser.open(url)
    while "code" not in got and "error" not in got:
        srv.handle_request()
    if "error" in got:
        raise SystemExit("Spotify said: " + got["error"])
    r = requests.post("https://accounts.spotify.com/api/token", timeout=20, data={
        "grant_type": "authorization_code", "code": got["code"], "redirect_uri": CFG["redirect_uri"],
        "client_id": CFG["client_id"], "code_verifier": verifier})
    r.raise_for_status()
    save(r.json())
    print("Done: Insta-Ideas can follow artists on Spotify.")


def save(t, old=None):
    t["expires_at"] = time.time() + t.get("expires_in", 3600) - 60
    if old and "refresh_token" not in t:
        t["refresh_token"] = old["refresh_token"]
    json.dump(t, open(TOKEN, "w", encoding="utf-8"))
    return t


def access_token():
    if not os.path.exists(TOKEN):
        return None
    t = json.load(open(TOKEN, encoding="utf-8"))
    if time.time() > t.get("expires_at", 0):
        r = requests.post("https://accounts.spotify.com/api/token", timeout=20, data={
            "grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": CFG["client_id"]})
        r.raise_for_status()
        t = save(r.json(), t)
    return t["access_token"]


def artist_id(url_or_id):
    m = urllib.parse.urlsplit(url_or_id or "")
    if "open.spotify.com" in m.netloc and "/artist/" in m.path:
        return m.path.split("/artist/")[1].split("/")[0]
    return url_or_id if (url_or_id or "").isalnum() and len(url_or_id) == 22 else None


def follow_artist(url_or_id):
    """'nuevo' (followed now), 'ya' (already followed) or None (no Spotify artist / not logged in / error)."""
    aid, tok = artist_id(url_or_id), access_token()
    if not (aid and tok):
        return None
    H = {"Authorization": "Bearer " + tok}
    r = requests.get("https://api.spotify.com/v1/me/following/contains", params={"type": "artist", "ids": aid}, headers=H, timeout=20)
    if not r.ok:
        return None
    if r.json() == [True]:
        return "ya"
    r = requests.put("https://api.spotify.com/v1/me/following", params={"type": "artist", "ids": aid}, headers=H, timeout=20)
    return "nuevo" if r.status_code in (200, 204) else None


def track_id(url):
    m = urllib.parse.urlsplit(url or "")
    return m.path.split("/track/")[1].split("/")[0] if "open.spotify.com" in m.netloc and "/track/" in m.path else None


def save_track(url):
    """Add a Spotify track to the user's Liked Songs; True if it was saved."""
    tid, tok = track_id(url), access_token()
    if not (tid and tok):
        return False
    r = requests.put("https://api.spotify.com/v1/me/tracks", params={"ids": tid},
                     headers={"Authorization": "Bearer " + tok}, timeout=20)
    return r.status_code in (200, 201, 204)


if __name__ == "__main__":
    if sys.argv[1:] == ["login"]:
        login()
    else:
        print(__doc__)
