#!/usr/bin/env bash

set -euo pipefail

COMPOSE_FILE="compose.manager.yaml"

echo "Pulling latest code..."
git pull

echo
echo "Building Docker image..."
docker compose -f "$COMPOSE_FILE" build

echo
echo "Restarting services..."
docker compose -f "$COMPOSE_FILE" down
docker compose -f "$COMPOSE_FILE" up -d

echo
echo "Deployment complete"

docker compose -f "$COMPOSE_FILE" ps
