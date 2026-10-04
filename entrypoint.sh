#!/bin/sh
set -eu
# Multi-replica deployments set RUN_MIGRATIONS=0 and run the migration in a release phase or
# a one-shot service, so two containers never apply the same schema at the same moment.
if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
  python manage.py migrate --noinput
fi
if [ "${DJANGO_CHECK_DEPLOY:-0}" = "1" ]; then
  python manage.py check --deploy --fail-level WARNING
fi
# Platform deploys without a shell (App Platform) create the first organization owner with
# this one-shot; idempotent, so leave it on only for the initial rollout.
if [ "${RUN_BOOTSTRAP:-0}" = "1" ]; then
  python manage.py bootstrap_admin
fi
# Static files are collected during the image build (see Dockerfile), not at boot.
exec gunicorn config.wsgi:application \
  --bind "0.0.0.0:${PORT:-8000}" \
  --workers "${WEB_CONCURRENCY:-3}" \
  --timeout "${GUNICORN_TIMEOUT:-120}" \
  --graceful-timeout "${GUNICORN_GRACEFUL_TIMEOUT:-30}" \
  --access-logfile - \
  --error-logfile -
