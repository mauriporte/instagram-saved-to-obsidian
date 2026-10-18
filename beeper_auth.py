"""Get a Beeper Desktop API token for links.py with OAuth (PKCE), the same way MCP clients connect.
Opens the browser on Beeper Desktop's consent page; approve it there. The token goes to config.json → links.beeper_token.
Run: python beeper_auth.py
"""
import base64, hashlib, http.server, json, os, secrets, threading, urllib.parse, webbrowser
import requests

B = "http://localhost:23373"
PORT = 53682
REDIRECT = f"http://127.0.0.1:{PORT}/callback"
HERE = os.path.dirname(os.path.abspath(__file__))

reg = requests.post(f"{B}/oauth/register", timeout=15, json={
    "client_name": "insta-ideas links", "redirect_uris": [REDIRECT], "grant_types": ["authorization_code"],
    "response_types": ["code"], "token_endpoint_auth_method": "none", "scope": "read write"}).json()
client_id = reg["client_id"]
verifier = secrets.token_urlsafe(64)
challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
state = secrets.token_urlsafe(16)
got = {}


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if q.get("state", [""])[0] == state and "code" in q:
            got["code"] = q["code"][0]
            msg = "Listo: ya puedes cerrar esta pestaña."
        else:
            got["error"] = q.get("error", ["?"])[0]
            msg = "No se autorizó: " + got["error"]
        body = f"<meta charset=utf-8><body style='font-family:sans-serif;background:#111;color:#eee'><h3>{msg}</h3>".encode()
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(body)
        threading.Thread(target=srv.shutdown).start()

    def log_message(self, *a):
        pass


srv = http.server.HTTPServer(("127.0.0.1", PORT), H)
url = f"{B}/oauth/authorize?" + urllib.parse.urlencode({"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
      "scope": "read write", "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
print("Approve the request in the browser / Beeper:", url, flush=True)
webbrowser.open(url)
srv.timeout = 300
srv.serve_forever()
if "code" not in got:
    raise SystemExit("not authorized: " + got.get("error", "timeout"))
tok = requests.post(f"{B}/oauth/token", timeout=15, data={"grant_type": "authorization_code", "code": got["code"],
      "redirect_uri": REDIRECT, "client_id": client_id, "code_verifier": verifier}).json()
access = tok.get("access_token")
if not access:
    raise SystemExit("token exchange failed: " + json.dumps({k: v for k, v in tok.items() if "token" not in k}))
p = os.path.join(HERE, "config.json"); c = json.load(open(p, encoding="utf-8"))
c.setdefault("links", {})["beeper_token"] = access
json.dump(c, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
ok = requests.get(f"{B}/v1/accounts", headers={"Authorization": f"Bearer {access}"}, timeout=15).status_code
print("saved to config.json; /v1/accounts ->", ok, "| expires_in:", tok.get("expires_in"))
