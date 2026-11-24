#!/bin/sh
# Fix ownership of the mounted data folder, then run as the unprivileged "enzo" user.
mkdir -p /app/data /app/backups

# One-time seeding of the Google login files from the stack's .env (base64), only if the file isn't there yet.
# Later token refreshes are written to the file itself, so the .env copy can be deleted after the first start.
seed() { # seed <ENV_VALUE> <filename>
  if [ -n "$1" ] && [ ! -s "/app/data/$2" ]; then
    printf '%s' "$1" | base64 -d > "/app/data/$2" 2>/dev/null && echo "seeded data/$2" || rm -f "/app/data/$2"
  fi
}
seed "$GOOGLE_TOKEN_B64" google_token.json
seed "$GOOGLE_CREDENTIALS_B64" google_credentials.json
seed "$GOOGLE_AUTH_TIME_B64" google_auth_time.json

chown -R enzo:enzo /app/data /app/backups 2>/dev/null || true   # backups: T-28, a new bind mount is created by root
exec gosu enzo "$@"
