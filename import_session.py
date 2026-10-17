# Create session.json from the `sessionid` cookie of a browser where you are logged into instagram.com
# (works for accounts that log in with Facebook, and never stores your password).
#   Browser devtools -> Application/Storage -> Cookies -> https://www.instagram.com -> copy `sessionid`
#   python import_session.py            (asks for it, input hidden)
#   INSTAGRAM_SESSIONID=... python import_session.py
import getpass, os
from instagrapi import Client

HERE = os.path.dirname(os.path.abspath(__file__))
sid = os.environ.get("INSTAGRAM_SESSIONID") or getpass.getpass("sessionid cookie (hidden): ").strip()
cl = Client()
cl.delay_range = [2, 5]
cl.login_by_sessionid(sid)
cl.dump_settings(os.path.join(HERE, "session.json"))
print(f"Logged in as {cl.username}; session saved to session.json")
