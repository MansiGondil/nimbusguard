.PHONY: install dev backend frontend test lint format evaluate docker up down

install:            ## Install backend + frontend dependencies
	python3 -m venv .venv && .venv/bin/pip install -r backend/requirements-dev.txt
	cd frontend && npm ci

backend:            ## Run the API + simulator in demo mode (http://localhost:8000)
	cd backend && NG_TIME_SCALE=10 NG_PREDICTION_LOOKBACK_MINUTES=10 NG_PERSIST_INTERVAL_SECONDS=15 \
	  NG_ANALYSIS_INTERVAL_SECONDS=10 ../.venv/bin/uvicorn app.main:app --reload --port 8000

frontend:           ## Run the dashboard dev server (http://localhost:5173)
	cd frontend && npm run dev

test:               ## Run the full backend test suite
	cd backend && ../.venv/bin/pytest -v

lint:
	cd backend && ../.venv/bin/ruff check . && ../.venv/bin/ruff format --check .
	cd frontend && npm run typecheck

format:
	cd backend && ../.venv/bin/ruff format . && ../.venv/bin/ruff check --fix .

evaluate:           ## Score anomaly detection on labelled simulated faults
	cd backend && ../.venv/bin/python -m scripts.evaluate_detector --episodes 60

docker:             ## Single container (SQLite + in-memory cache): http://localhost:8000
	docker build -t nimbusguard . && docker run --rm -p 8000:8000 -e NG_TIME_SCALE=10 nimbusguard

up:                 ## Full stack: TimescaleDB, Redis, API, worker, Caddy TLS
	test -f .env || cp .env.example .env
	docker compose up --build -d

down:
	docker compose down
