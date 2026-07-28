#!/usr/bin/env bash
# openGardener nightly backup.
# Makes a consistent SQLite backup (safe even while the logger is writing,
# thanks to the .backup command + WAL), compresses it, copies to the NAS,
# and rotates old copies.
#
# Configure NAS_DEST below, then schedule via cron or a systemd timer:
#   crontab -e
#   15 3 * * *  /home/ben/backup_opengardener.sh >> /home/ben/backup.log 2>&1
#
# WAL note: sqlite3 .backup produces a single consistent file even with the
# logger actively writing, so no need to stop the service.

set -euo pipefail

DB=~/opengardener.db
STAGE=~/backups
NAS_DEST="/mnt/nas/opengardener"    # <-- set to your mounted NAS path
KEEP_LOCAL=7                        # keep this many local daily backups
KEEP_NAS=30                         # keep this many on the NAS

mkdir -p "$STAGE"
stamp=$(date +%Y%m%d-%H%M%S)
out="$STAGE/opengardener-$stamp.db"

# consistent backup via the online .backup API (not a raw cp)
sqlite3 "$DB" ".backup '$out'"
gzip -f "$out"
gzfile="$out.gz"
echo "$(date -Is) backup created: $gzfile ($(du -h "$gzfile" | cut -f1))"

# copy to NAS if the destination is mounted/reachable
if [ -d "$NAS_DEST" ]; then
  cp "$gzfile" "$NAS_DEST/"
  echo "$(date -Is) copied to NAS: $NAS_DEST/"
  # rotate NAS copies
  ls -1t "$NAS_DEST"/opengardener-*.db.gz 2>/dev/null | tail -n +$((KEEP_NAS+1)) | xargs -r rm -f
else
  echo "$(date -Is) WARNING: NAS dest $NAS_DEST not present; kept local only"
fi

# rotate local copies
ls -1t "$STAGE"/opengardener-*.db.gz 2>/dev/null | tail -n +$((KEEP_LOCAL+1)) | xargs -r rm -f
echo "$(date -Is) rotation done"
