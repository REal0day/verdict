.PHONY: env env-missing up down test

# (Re)generate all secrets in .env — prompts for confirmation.
env:
	./scripts/gen-env.sh

# Only fill in keys that are empty/placeholders — safe for repeated CI runs.
env-missing:
	./scripts/gen-env.sh --only-missing -y

up:
	docker compose up --build

down:
	docker compose down

test:
	cd server && python -m pytest tests/ -q
