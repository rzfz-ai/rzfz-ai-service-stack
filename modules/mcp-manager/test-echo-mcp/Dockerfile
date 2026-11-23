FROM python:3.12-alpine@sha256:e6dae589c1c05976188fd3bd019afd091445fc533ed2ea29001beddc7368a9c8
RUN apk upgrade --no-cache && apk add --no-cache curl
WORKDIR /app
COPY server.py .
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=4s --retries=3 \
    CMD curl -f http://localhost:8080/healthz || exit 1
CMD ["python3", "server.py"]
