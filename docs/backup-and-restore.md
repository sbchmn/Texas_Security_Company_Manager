# Backup and restore

**Status:** designed and shipped in the Compose topology; **not yet exercised end to end**.
`docs/discovery-decisions.md` accepts an RPO of 5 minutes and an RTO of 1 hour as service
targets, and `docs/implementation-readiness.md` gates any recovery *claim* on restore evidence.
This file is the procedure for producing that evidence. Until a restore test has been run and
recorded below, the deployment has backups and no proven recovery.

## What runs

```bash
docker compose -f compose.yaml --profile backups up -d
```

| Service | Output | Cadence |
| --- | --- | --- |
| `db-backup` | `/backups/dumps/tscm-<UTC timestamp>.sql.gz` | every `BACKUP_INTERVAL_SECONDS` (default 3600) |
| `binlog-backup` | `/backups/binlog/binlog.NNNNNN` | continuous (`mysqlbinlog --read-from-remote-server --raw --stop-never`) |

Both write to the `mysql_backups` volume, log to `/backups/backup.log`, and use
`MYSQL_BACKUP_USER` / `MYSQL_BACKUP_PASSWORD` when set (they default to the root account
created at first initialisation). A dump is only kept if it finished — the loop checks for
mysqldump's `Dump completed` marker before renaming the `.tmp` file — and `BACKUP_KEEP`
(default 72) bounds retention.

## Recovery point actually achieved

- **Datadir intact, data corrupted or dropped:** replay from the last dump plus the shipped
  binary logs. The recovery point is bounded by binary-log lag, which is normally seconds —
  this is the case the 5-minute target is about.
- **`mysql` volume lost entirely:** the recovery point is the last completed dump **and** the
  last binary log file copied to `mysql_backups`, so it is at most one dump interval unless
  `mysql_backups` is a different failure domain from `mysql`. It is currently the same Docker
  host: **an hourly dump on the same disk does not meet a 5-minute RPO for a whole-host
  failure.** Off-host copies (object storage or a second node) are required before the target
  can be claimed.
- **Provider/account loss (App Platform):** the managed tier's own snapshots apply; this
  procedure covers the Compose topology only.

Recovery *time* (target 1 hour) is dominated by restore plus verification, so rehearse it.

## Restore procedure

Restore overwrites everything in the running database. Confirm the target with
`docker compose exec db printenv MYSQL_DATABASE` first, and prefer restoring into a scratch
database to validate before replacing production.

```bash
# 1. Stop the writers so nothing commits during the restore.
docker compose -f compose.yaml stop web worker

# 2. Keep the current state, in case the chosen point in time turns out to be wrong.
docker compose -f compose.yaml exec db sh -c \
  'mysqldump --single-transaction --no-tablespaces -uroot -p"$MYSQL_ROOT_PASSWORD" tscm | gzip > /tmp/pre-restore.sql.gz'

# 3. Load the newest dump into a scratch schema. The dump was taken with --databases, so the
#    CREATE DATABASE/USE lines are rewritten to keep the live schema untouched.
latest=$(docker compose -f compose.yaml exec -T db-backup sh -c 'ls -1t /backups/dumps/*.sql.gz | head -1')
echo "recorded restore point: $(docker compose -f compose.yaml exec -T db-backup sh -c "grep -F $(basename $latest) /backups/backup.log | tail -1")"
docker compose -f compose.yaml exec -T db sh -c \
  'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "CREATE DATABASE IF NOT EXISTS tscm_restore CHARACTER SET utf8mb4"'
docker compose -f compose.yaml exec -T db-backup sh -c "gunzip -c $latest" \
  | sed -e 's/^CREATE DATABASE.*$//' -e 's/^USE `tscm`;$/USE tscm_restore;/' \
  | docker compose -f compose.yaml exec -T db sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" tscm_restore'

# 4. Replay the shipped binary logs from the position the dump recorded (step 3 echoes it),
#    up to the moment you want to recover to. Without --raw, mysqlbinlog writes SQL.
docker compose -f compose.yaml exec -T binlog-backup sh -c \
  'mysqlbinlog --no-defaults --read-from-remote-server -h db -uroot -p"$MYSQL_ROOT_PASSWORD" \
     --start-position=<POSITION> --stop-datetime="<YYYY-MM-DD HH:MM:SS>" \
     binlog.<FIRST> binlog.<LAST>' \
  | docker compose -f compose.yaml exec -T db sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" tscm_restore'
```

The replay runs against the local copies in `/backups/binlog`; `--raw` is the *shipping*
mode used by `binlog-backup.sh` and must not be used here, because it writes binary files
instead of SQL.

Then validate before promoting the result:

```sql
SELECT COUNT(*) FROM core_auditevent;             -- must be non-zero and match the expected span
SELECT COUNT(*) FROM core_punch;                  -- and compare against the last known count
SELECT COUNT(*) FROM information_schema.triggers
 WHERE trigger_schema='tscm_restore';             -- expect the guards from migrations 0012/0018/0019
SELECT MAX(occurred_at) FROM core_auditevent;     -- this is your achieved recovery point
```

If the trigger count is short, the target schema was created on a server without
`log-bin-trust-function-creators=1`, the same option the bundled MySQL uses: the dump carries
the trigger definitions, but the restore user must be allowed to create them.

Promote by repointing the application rather than overwriting the live schema, so the old
data is still there if the chosen point in time turns out to be wrong:

```bash
# 5. Point MYSQL_DATABASE at tscm_restore, restart, verify, then retire the old schema later.
docker compose -f compose.yaml up -d --force-recreate web worker
docker compose -f compose.yaml exec web python manage.py check
curl -fsS https://localhost/readyz
```

`/readyz` returns 200 only when MySQL and Redis answer, so it is the fastest post-restore gate.
`manage.py check` catches a schema that restored without its tenant guard triggers.

## Media

Personnel documents and brand logos are **not** in the database. They live in the `media`
volume (or an S3-compatible bucket when `AWS_STORAGE_BUCKET_NAME` is set) and need their own
copy:

```bash
docker run --rm -v <project>_media:/from -v "$PWD":/to alpine \
  tar -czf /to/media-$(date -u +%Y%m%dT%H%M%SZ).tar.gz -C /from .
```

A restore that recovers the database but not the volume leaves every document row pointing at
a missing file — the application reports the download as unavailable rather than inventing it.

## Restore test log

Run at least quarterly, and on any host or storage change. Record the observed times so
future reviews can judge the RTO claim instead of assuming it.

| Date | Dump used | Binlog replayed | Elapsed to ready | Row counts verified | Notes |
| --- | --- | --- | --- | --- | --- |
| — | — | — | — | — | no restore has been performed yet |
