FROM python:3-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY warpgate_manager ./warpgate_manager

EXPOSE 9090

CMD ["python", "-m", "warpgate_manager.server"]
