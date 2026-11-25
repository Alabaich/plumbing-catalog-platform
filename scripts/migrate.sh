#!/bin/bash
set -e

echo "=== Running database migrations ==="
docker compose -f docker-compose.prod.yml run --rm api alembic upgrade head
echo "=== Migrations complete ==="