#!/bin/sh
# Hourly logical dump of the application schema, kept in the mysql_backups volume.
# Runs inside the mysql image because that is where mysqldump already exists.
set -eu

MYSQL_HOST="${MYSQL_HOST:-db}"
MYSQL_PORT="${MYSQL_PORT:-3306}"
BACKUP_USER="${MYSQL_BACKUP_USER:-root}"
: "${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD or MYSQL_BACKUP_PASSWORD must be set}"
# MYSQL_PWD keeps the secret out of the container process list, unlike -p<password>.
export MYSQL_PWD="${MYSQL_BACKUP_PASSWORD:-$MYSQL_ROOT_PASSWORD}"
INTERVAL="${BACKUP_INTERVAL_SECONDS:-3600}"
KEEP="${BACKUP_KEEP:-72}"
LOG=/backups/backup.log
# --databases takes names, so a second schema on this server is not covered by accident. The
# signing profile puts DocuSeal in its own database here; listing it in
# MYSQL_BACKUP_EXTRA_DATABASES is what brings it into the same dump. Deliberately unquoted at the
# mysqldump call: it is a word list, and a name that does not exist must fail the dump rather
# than quietly produce a backup that omits a system of record.
DATABASES="${MYSQL_DATABASE:-tscm} ${MYSQL_BACKUP_EXTRA_DATABASES:-}"

mkdir -p /backups/dumps
while true; do
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  target="/backups/dumps/${MYSQL_DATABASE:-tscm}-${stamp}.sql.gz"
  # --no-tablespaces avoids needing PROCESS; --triggers is required for the tenant and audit
  # guard triggers installed by the migrations to come back with the data; --source-data=2
  # records the exact binary-log position so the shipped logs can be replayed from it.
  if mysqldump -h "$MYSQL_HOST" -P "$MYSQL_PORT" -u "$BACKUP_USER" \
      --single-transaction --quick --no-tablespaces --routines --triggers --events \
      --source-data=2 \
      --databases $DATABASES 2>>"$LOG" | gzip > "$target.tmp" \
     && [ -s "$target.tmp" ] \
     && zcat "$target.tmp" | tail -n 3 | grep -q "Dump completed"; then
    mv "$target.tmp" "$target"
    position=$(zcat "$target" | grep -m1 "^-- CHANGE REPLICATION SOURCE TO" || true)
    echo "$(date -u +%FT%TZ) wrote $target :: $position" >> "$LOG"
    ls -1t /backups/dumps/*.sql.gz | tail -n +"$((KEEP + 1))" | while read -r stale; do
      rm -f "$stale"; echo "$(date -u +%FT%TZ) pruned $stale" >> "$LOG"
    done
  else
    rm -f "$target.tmp"
    echo "$(date -u +%FT%TZ) FAILED: dump did not complete" >> "$LOG"
  fi
  sleep "$INTERVAL"
done
