FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TZ=Asia/Kolkata
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Render injects $PORT; default 8000 locally. start.sh seeds the DB on first boot.
EXPOSE 8000
CMD ["sh", "scripts/start.sh"]
