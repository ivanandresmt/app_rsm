FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY alembic.ini .
COPY migrations migrations
COPY app app
RUN useradd -m -u 10001 app && mkdir -p /data/documents && chown -R app /data /srv
USER app
EXPOSE 8000
# api: migrations first (single writer), then serve. worker/mcp override the command.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.api.main:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 20"]
