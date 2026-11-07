# Create threads_session.json from the cookies of a browser where you are logged into threads.com
# (never stores your password).
#   Vivaldi -> threads.com -> F12 -> Network -> reload -> click the first "www.threads.com" request ->
#   Headers -> Request Headers -> right-click "cookie" -> Copy value
#   python threads_import.py            (asks for it, input hidden)
#   THREADS_COOKIE=... python threads_import.py
import getpass, json, os, re
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"

raw = os.environ.get("THREADS_COOKIE") or getpass.getpass("threads.com cookie header (hidden): ").strip()
cookies = dict(p.strip().split("=", 1) for p in raw.split(";") if "=" in p)
if "sessionid" not in cookies:
    raise SystemExit("No sessionid in what you pasted: copy the whole cookie value of a www.threads.com request.")
s = requests.Session()
s.headers["User-Agent"] = UA
for k, v in cookies.items():
    s.cookies.set(k, v, domain=".threads.com")
r = s.get("https://www.threads.com/saved", timeout=30)
user = (re.search(r"__user=(\d+)", r.text) or [None, "0"])[1]  # the page embeds the viewer id; 0 = logged out
if r.status_code != 200 or user == "0":
    raise SystemExit(f"threads.com treats these cookies as logged out (HTTP {r.status_code}, {len(cookies)} cookies: "
                     f"{', '.join(sorted(cookies))})")
json.dump({"cookies": cookies, "user_agent": UA}, open(os.path.join(HERE, "threads_session.json"), "w", encoding="utf-8"))
print(f"Logged in as user {user}; session saved to threads_session.json")
