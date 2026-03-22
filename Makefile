UV := uv

.PHONY: sync lock format lint typecheck test run worker compose-up compose-down

sync:
	$(UV) sync --all-extras

lock:
	$(UV) lock

format:
	$(UV) run ruff format .

lint:
	$(UV) run ruff check .

typecheck:
	$(UV) run mypy src tests

test:
	$(UV) run pytest

run:
	$(UV) run alice-api

worker:
	$(UV) run alice-worker

compose-up:
	docker compose up --build

compose-down:
	docker compose down --remove-orphans
