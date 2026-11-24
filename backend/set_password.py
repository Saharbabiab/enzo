"""
set_password.py — set (or change) the password for the Enzo web app.

    python set_password.py

Stores only a salted hash (ENZO_PASSWORD_HASH) in .env — never the password.
Changing the password does NOT sign out devices; use `python sessions.py revoke-all` for that.
Restart the backend afterwards.
"""

import getpass
from pathlib import Path

from dotenv import set_key

from auth import hash_password

ENV = Path(__file__).parent / ".env"


def main() -> None:
    pw = getpass.getpass("New Enzo password (min 10 chars): ")
    if len(pw) < 10:
        print("Too short — use at least 10 characters (a few words works well).")
        return
    if getpass.getpass("Again: ") != pw:
        print("They don't match — nothing changed.")
        return
    ENV.touch(exist_ok=True)
    set_key(str(ENV), "ENZO_PASSWORD_HASH", hash_password(pw), quote_mode="never")
    print("✅ Password saved (hash only) in .env. Restart the backend to use it.")


if __name__ == "__main__":
    main()
