"""
sessions.py — see and sign out devices logged in to the Enzo web app.

    python sessions.py list
    python sessions.py revoke <id>     # e.g. a lost phone
    python sessions.py revoke-all      # sign out everything
"""

import sys
from datetime import datetime

import auth


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def main(argv: list[str]) -> None:
    cmd = argv[0] if argv else "list"
    if cmd == "list":
        rows = auth.list_sessions()
        if not rows:
            print("No devices signed in.")
        for s in rows:
            print(f"{s['id']}  {s['device']:<30}  ip {s.get('ip', ''):<16} "
                  f"last seen {_when(s['last_seen'])}  expires {_when(s['expires'])}")
    elif cmd == "revoke" and len(argv) == 2:
        print("Signed out." if auth.revoke_session(argv[1]) else "No session with that id.")
    elif cmd == "revoke-all":
        print(f"Signed out {auth.revoke_all()} device(s).")
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
