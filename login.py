# One-time Instagram login for ideas.py. Run it yourself in a terminal window:
#   python login.py      (from this folder, inside the venv)
# Accounts that sign in with Facebook have no Instagram password: use import_session.py instead.
# The password is NOT stored: only the session (session.json) is saved, and ideas.py reuses it every day.
import getpass, os
from instagrapi import Client

HERE = os.path.dirname(os.path.abspath(__file__))
cl = Client()
cl.delay_range = [2, 5]
cl.challenge_code_handler = lambda username, choice: input(f"Código de verificación enviado por {choice}: ").strip()
user = input("Usuario de Instagram: ").strip()
pwd = getpass.getpass("Contraseña (no se guarda): ")
code = input("Código 2FA de la app autenticadora (Enter si no usas 2FA): ").strip()
cl.login(user, pwd, verification_code=code)
cl.dump_settings(os.path.join(HERE, "session.json"))
print(f"Sesión guardada en {os.path.join(HERE, 'session.json')}. Ya puedes cerrar esta ventana.")
