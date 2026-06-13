.PHONY: help build up down clean test

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-12s\033[0m %s\n", $$1, $$2}'

build: ## Build all Docker images
	docker compose build
	docker compose -f compose.manager.yaml build

up: ## Start single proxy
	docker compose up -d

down: ## Stop single proxy
	docker compose down

manager: ## Start manager
	docker compose -f compose.manager.yaml up -d

manager-down: ## Stop manager
	docker compose -f compose.manager.yaml down

clean: ## Remove all containers, volumes, images
	docker compose down -v --rmi local
	docker compose -f compose.manager.yaml down -v --rmi local

lint: ## Lint entrypoint and Python
	shellcheck docker-entrypoint.sh
	cd manager && ruff check server.py

logs: ## Tail all logs
	docker compose logs -f

manager-logs: ## Tail manager logs
	docker compose -f compose.manager.yaml logs -f
