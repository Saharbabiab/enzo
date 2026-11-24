"""
One-time Google login for Enzo (Calendar + Gmail).

Before running:
  1. https://console.cloud.google.com → create a project (e.g. "Enzo")
  2. APIs & Services → Library → enable "Google Calendar API" and "Gmail API"
  3. OAuth consent screen → External → add your own Gmail as a Test user
  4. Credentials → Create credentials → OAuth client ID → Application type "Desktop app"
     → Download JSON → save it as  data/google_credentials.json  (in this folder)

Then run:   python google_setup.py
A browser opens; log in and allow access. The token is saved to data/google_token.json
and refreshes itself — you only do this once.
"""

from agent.google_services import run_setup_flow

if __name__ == "__main__":
    print(run_setup_flow())
