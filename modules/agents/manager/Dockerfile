FROM python:3.12-alpine

RUN apk add --no-cache postgresql-client curl

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# NOTE: runs as root — Docker socket access requires it (same as setup, backup, config)
# SECURITY: documented in M009 F-003, mitigated by Authentik SSO on all routes

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:5000/healthz || exit 1

CMD ["gunicorn", "-k", "gevent", "-w", "1", "-b", "0.0.0.0:5000", "--timeout", "120", "app:create_app()"]
