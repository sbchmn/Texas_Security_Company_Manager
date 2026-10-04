#!/bin/sh
# Creates the database and user the signing profile's DocuSeal container connects as, inside the
# application's own MySQL server. One server, two schemas: DocuSeal gets no grant on the
# application's tables, and the application gets none on its own.
#
# This is a .sh, not a .sql, on purpose: the MySQL entrypoint pipes .sql files straight into the
# client with no variable expansion, so the names below would be the literal text
# "${DOCUSEAL_DATABASE}". Executable files in that directory run with the service environment and
# are silenced, which keeps the password out of the container log.
#
# It calls the client directly rather than using docker_process_sql, which is a shell function
# defined inside docker-entrypoint.sh and therefore not visible to a file the entrypoint executes:
# doing it the other way fails with "docker_process_sql: command not found", exit 127, and the
# whole initialisation aborts (observed). Root already has its MYSQL_ROOT_PASSWORD applied by the
# time these files run — connecting without it fails "Access denied for user 'root'@'localhost'
# (using password: NO)", also observed — so MYSQL_PWD carries it, which keeps it out of the
# container's process list exactly as it does in mysql-backup.sh.
#
# Files here run only while the data directory is empty. An installation that starts the signing
# profile later applies the same four statements once, by hand, over the socket:
#
#   docker compose -f compose.yaml exec db mysql -uroot -p
#
# and pastes the CREATE DATABASE / CREATE USER / GRANT / FLUSH block from this file, substituting
# the values from .env. Skipping it is not quiet, but the symptom misleads: with no such account
# the container crash-loops on
#   ActiveRecord::ConnectionNotEstablished: trilogy_auth_recv: TRILOGY_PROTOCOL_VIOLATION
# which reads like a TLS or auth-plugin problem and is really a missing user — observed against
# the same image, server version and network as a successful boot, where the account was the only
# difference. Confirm with "SELECT user,host FROM mysql.user WHERE user='docuseal'" before chasing
# certificates.
#
# The password goes into a SQL string literal, so it must contain no single quote — and it also
# appears inside DocuSeal's DATABASE_URL, where / and @ need URL-encoding. Pick a password that
# satisfies both, or encode deliberately.
set -eu

db="${DOCUSEAL_DATABASE:-docuseal}"
user="${DOCUSEAL_USER:-docuseal}"
pass="${DOCUSEAL_PASSWORD:-change-me}"
: "${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD must be set for the signing initialisation}"
export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"

mysql --protocol=socket --user=root <<EOSQL
CREATE DATABASE IF NOT EXISTS \`$db\` CHARACTER SET utf8mb4;
CREATE USER IF NOT EXISTS '$user'@'%' IDENTIFIED BY '$pass';
GRANT ALL PRIVILEGES ON \`$db\`.* TO '$user'@'%';
FLUSH PRIVILEGES;
EOSQL
