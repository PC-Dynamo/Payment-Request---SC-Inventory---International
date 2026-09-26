FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=UTC
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY server/requirements.txt /app/server/requirements.txt
RUN pip install --no-cache-dir -r /app/server/requirements.txt

COPY server/ /app/server/
COPY Outputs/ /app/Outputs/

EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=8s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

# One worker: the refresh state lives in memory and the ledger is SQLite.
CMD ["gunicorn","-w","1","--threads","8","-b","0.0.0.0:8080","--chdir","/app/server","--timeout","1800","app:app"]
