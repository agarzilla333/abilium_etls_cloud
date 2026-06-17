FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY core/ core/
COPY config/ config/
COPY app.py .

# Cloud Run sets $PORT; default 8080 for local runs.
# --proxy-headers so request.base_url reflects https behind Cloud Run's proxy
# (the OAuth redirect URI is derived from it).
ENV PORT=8080
CMD exec uvicorn app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips="*"
