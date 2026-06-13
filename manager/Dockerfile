FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

EXPOSE 8000

ENV DATA_DIR=/data
VOLUME ["/data"]

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
