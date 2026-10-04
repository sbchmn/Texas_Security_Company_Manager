#!/bin/sh
# Continuous binary-log shipping. An hourly dump alone only reaches a one-hour recovery
# point; the accepted target is much tighter, so the write-ahead log is copied off the data
# volume as MySQL produces it.
set -eu

MYSQL_HOST="${MYSQL_HOST:-db}"
MYSQL_PORT="${MYSQL_PORT:-3306}"
BACKUP_USER="${MYSQL_BACKUP_USER:-root}"
: "${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD or MYSQL_BACKUP_PASSWORD must be set}"
export MYSQL_PWD="${MYSQL_BACKUP_PASSWORD:-$MYSQL_ROOT_PASSWORD}"
LOG=/backups/backup.log

mkdir -p /backups/binlog
while true; do
  # --stop-never leaves the client attached and streaming; --raw copies the binary log files
  # unchanged so mysqlbinlog can replay them into a restored server.
  if mysqlbinlog -h "$MYSQL_HOST" -P "$MYSQL_PORT" -u "$BACKUP_USER" \
      --read-from-remote-server --raw --stop-never --include-globals \
      --result-file=/backups/binlog/ 2>>"$LOG"; then
    echo "$(date -u +%FT%TZ) binlog stream ended cleanly" >> "$LOG"
  else
    echo "$(date -u +%FT%TZ) binlog stream failed; retrying in 10s" >> "$LOG"
  fi
  sleep 10
done
