#!/bin/bash
set -e

echo "=== Pulling latest code ==="
git pull origin main

echo "=== Building new images ==="
docker compose -f docker-compose.prod.yml build --no-cache api remix-app

echo "=== Running DB migrations ==="
bash scripts/migrate.sh

echo "=== Rolling restart: API ==="
docker compose -f docker-compose.prod.yml up -d --no-deps api

echo "Waiting for API health check..."
sleep 10
curl -sf http://localhost:8000/health || { echo "API FAILED - rolling back"; bash scripts/rollback.sh; exit 1; }

echo "=== Rolling restart: Remix app ==="
docker compose -f docker-compose.prod.yml up -d --no-deps remix-app

echo "=== Deploy complete ==="
docker compose -f docker-compose.prod.yml ps