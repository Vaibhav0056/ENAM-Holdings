# FINN: FastAPI + PWA. Postgres lives on Neon; nothing stateful lives in the container.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TZ=Asia/Kolkata
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Render injects $PORT (8000 locally). start.sh migrates the schema, seeds companies, then serves.
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\",\"8000\")}/health')" || exit 1
CMD ["sh", "scripts/start.sh"]
