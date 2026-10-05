FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server/ ./server/
COPY tests/ ./tests/
ENV DATA_DIR=/data PORT=8080
EXPOSE 8080
# persistent profile + keys live in /data — mount a volume when available
CMD ["sh", "-c", "uvicorn server.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
