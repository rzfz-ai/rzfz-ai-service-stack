FROM python:3.12-alpine
RUN apk add --no-cache curl
WORKDIR /app
COPY server.py .
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=4s --retries=3 \
    CMD curl -f http://localhost:8080/healthz || exit 1
CMD ["python3", "server.py"]
