#!/bin/sh
# Create the Docker secrets used by compose.yml (one value per file).
#   db_password                     random, generated here
#   md_client_id, md_client_secret,
#   md_username, md_password        MangaDex personal API client; left EMPTY = account features off
# Usage: ./scripts/init-secrets.sh [secrets dir] [uid:gid of the app user]
set -eu
DIR="${1:-${MDM_SECRETS_PATH:-./secrets}}"
OWNER="${2:-${PUID:-1000}:${PGID:-1000}}"
mkdir -p "$DIR"
if [ ! -s "$DIR/db_password" ]; then
  openssl rand -base64 32 | tr -d '\n' > "$DIR/db_password"
  echo "created $DIR/db_password"
fi
for f in md_client_id md_client_secret md_username md_password; do
  [ -e "$DIR/$f" ] || { : > "$DIR/$f"; echo "created empty $DIR/$f"; }
done
chmod 400 "$DIR"/*
if [ "$(id -u)" = 0 ]; then chown "$OWNER" "$DIR"/*; else
  echo "note: the files must be readable by $OWNER (sudo chown $OWNER $DIR/*)"; fi
