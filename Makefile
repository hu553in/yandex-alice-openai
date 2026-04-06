SHELL := /bin/bash
.ONESHELL:
.SHELLFLAGS := -euo pipefail -c

.PHONY: ensure_env
ensure_env:
	if [ ! -f .env ]; then cp .env.example .env; fi

.PHONY: install_deps
install_deps:
	uv sync --all-groups --frozen

.PHONY: sync_deps
sync_deps:
	uv sync --all-groups

.PHONY: check_deps_updates
check_deps_updates:
	uv tree --outdated --depth=1 | grep latest

.PHONY: check_deps_vuln
check_deps_vuln:
	uv run pysentry-rs .

.PHONY: lint
lint:
	uv run ruff format
	uv run ruff check --fix

.PHONY: test
test:
	uv run pytest

.PHONY: check_types
check_types:
	uv run ty check .

.PHONY: check
check:
	uv run prek --all-files --hook-stage pre-commit

# Project-specific

.PHONY: start
start: ensure_env
	docker compose up -d --build --wait --remove-orphans

.PHONY: stop
stop: ensure_env
	docker compose down --remove-orphans

.PHONY: restart
restart: stop start
