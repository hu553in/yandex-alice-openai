SHELL := /bin/bash
.ONESHELL:
.SHELLFLAGS := -euo pipefail -c

.PHONY: ensure-env
ensure-env:
	if [ ! -f .env ]; then cp .env.example .env; fi

.PHONY: install-deps
install-deps:
	uv sync --all-groups --frozen

.PHONY: lint
lint:
	uv run ruff format
	uv run ruff check --fix

.PHONY: test
test:
	uv run pytest

.PHONY: check-types
check-types:
	uv run ty check .

.PHONY: check
check:
	uv run prek --all-files --hook-stage pre-commit

# Project-specific

.PHONY: start
start: ensure-env
	docker compose up -d --build --wait --remove-orphans

.PHONY: stop
stop: ensure-env
	docker compose down --remove-orphans

.PHONY: restart
restart: stop start
