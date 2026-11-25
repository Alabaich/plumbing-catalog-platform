.PHONY: up down restart deploy rollback migrate logs ps build remix

# Start all containers
up:
	docker compose -f docker-compose.prod.yml up -d

# Stop all containers
down:
	docker compose -f docker-compose.prod.yml down

# Restart specific service: make restart s=api
restart:
	docker compose -f docker-compose.prod.yml up -d --no-deps $(s)

# Full deploy
deploy:
	bash scripts/deploy.sh

# Rollback
rollback:
	bash scripts/rollback.sh

# Run migrations
migrate:
	bash scripts/migrate.sh

# View logs: make logs s=api
logs:
	docker compose -f docker-compose.prod.yml logs -f $(s)

# Show running containers
ps:
	docker compose -f docker-compose.prod.yml ps

# Build all images
build:
	docker compose -f docker-compose.prod.yml build --no-cache

# Build specific service: make build s=api
build-one:
	docker compose -f docker-compose.prod.yml build --no-cache $(s)

# Pull latest and restart
update:
	git pull origin main
	docker compose -f docker-compose.prod.yml up -d

# Rebuild and restart Remix app only
remix:
	docker compose -f docker-compose.prod.yml up -d --no-deps --build remix-app

rebuild-core:
	docker compose -f docker-compose.prod.yml up -d --build remix-app api