FROM python:3.14-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN addgroup --system app && adduser --system --ingroup app app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Settings intentionally fail closed without production secrets. collectstatic only
# needs deterministic, non-sensitive placeholders during image construction; these
# values are scoped to this RUN instruction and are not kept in the image environment.
# /app/media is created here so the named volume Docker mounts on top of it inherits
# ownership by the runtime user instead of root.
RUN SECRET_KEY=build-only-not-a-runtime-secret-00000000000000000000 \
    ALLOWED_HOSTS=localhost \
    REDIS_URL=redis://localhost:6379/0 \
    CLAMAV_HOST=clamav \
    python manage.py collectstatic --noinput \
    && mkdir -p /app/media \
    && chown -R app:app /app
USER app
EXPOSE 8000
# Liveness only: /readyz also needs MySQL and Redis, which belong to the orchestration layer.
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
  CMD python -c "import sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=4).status==200 else 1)"
CMD ["./entrypoint.sh"]
