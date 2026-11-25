#!/bin/bash
set -e

PREV_TAG=$(git rev-parse HEAD~1)
echo "=== Rolling back to $PREV_TAG ==="

git checkout $PREV_TAG

docker compose -f docker-compose.prod.yml build api remix-app
docker compose -f docker-compose.prod.yml up -d --no-deps api remix-app

echo "=== Rolled back successfully ==="
echo "Remember to fix the issue before deploying again."
echo "Run 'git checkout main' when ready to continue."