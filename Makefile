.DEFAULT_GOAL := help
PYTHON := .venv/bin/python
PIP := .venv/bin/pip

.PHONY: help setup migrate test lint typecheck check demo serve bot worker docker clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Create the virtual environment and install the package with dev extras
	python3 -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install -e '.[dev]'

migrate: ## Apply database migrations
	.venv/bin/alembic upgrade head

test: ## Run the test suite
	$(PYTHON) -m pytest

lint: ## Check formatting and lint rules
	.venv/bin/ruff format --check .
	.venv/bin/ruff check .

typecheck: ## Run mypy
	.venv/bin/mypy

check: lint typecheck test ## Run every check the CI would run

demo: ## Full offline demo: conversations, pipeline, notifications, export
	./scripts/demo.sh

serve: ## Run the admin API on 127.0.0.1:8000
	$(PYTHON) -m talabflow.cli serve

bot: ## Run the chat bot
	$(PYTHON) -m talabflow.cli run-bot

worker: ## Run the notification outbox worker
	$(PYTHON) -m talabflow.cli run-worker

docker: ## Build and start api + bot + worker with compose
	docker compose up --build

clean: ## Remove caches and demo artefacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -f data/demo.sqlite3 data/demo.sqlite3-wal data/demo.sqlite3-shm
	rm -f data/demo-orders.xlsx data/demo-orders.csv
