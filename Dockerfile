FROM python:3.14-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN addgroup --system app && adduser --system --ingroup app app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Settings intentionally fail closed without production secrets. collectstatic only
# needs deterministic, non-sensitive placeholders during image construction; these
# values are scoped to this RUN instruction and are not persisted in the image.
RUN SECRET_KEY=build-only-not-a-runtime-secret-00000000000000000000 \
    ALLOWED_HOSTS=localhost \
    REDIS_URL=redis://localhost:6379/0 \
    CLAMAV_HOST=clamav \
    python manage.py collectstatic --noinput \
    && chown -R app:app /app
USER app
EXPOSE 8000
CMD ["./entrypoint.sh"]
