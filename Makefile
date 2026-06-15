.PHONY: help build up down cluster cluster-down clean logs cluster-logs

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-12s\033[0m %s\n", $$1, $$2}'

build: ## Build Docker image
	docker compose build

up: ## Start single proxy
	docker compose up -d

down: ## Stop single proxy
	docker compose down

cluster: ## Start cluster with N proxies (scale via: make cluster N=5)
	docker compose -f compose.cluster.yaml up -d --scale warpgate=$(or $(N),3)

cluster-down: ## Stop cluster
	docker compose -f compose.cluster.yaml down

clean: ## Remove all containers, volumes, images
	docker compose down -v --rmi local
	docker compose -f compose.cluster.yaml down -v --rmi local

logs: ## Tail single proxy logs
	docker compose logs -f

cluster-logs: ## Tail cluster logs
	docker compose -f compose.cluster.yaml logs -f
