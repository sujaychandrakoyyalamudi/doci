.PHONY: setup api web test check build local evaluate

setup:
	uv sync --all-extras
	npm --prefix frontend ci

api:
	uv run uvicorn doci.main:app --reload --host 127.0.0.1 --port 8000

web:
	npm --prefix frontend run dev

test:
	uv run pytest -q

check:
	uv run ruff check backend
	uv run ruff format --check backend
	npm --prefix frontend run build

build:
	docker build -t doci:local .

local:
	docker compose up --build

evaluate:
	@test -n "$(DATASET)" || (echo "Usage: make evaluate DATASET=private-data/evaluation.json"; exit 1)
	uv run python -m doci.evaluation --dataset "$(DATASET)"
