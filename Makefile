.PHONY: default \
	lint reformat type-check security-check \
	start stop rebuild destroy \
	migrate create-admin seed \
	routes migrations lock test

default:
	@echo "Call a specific subcommand:"
	@echo
	@$(MAKE) -pRrq -f $(lastword $(MAKEFILE_LIST)) : 2>/dev/null\
	| awk -v RS= -F: '/^# File/,/^# Finished Make data base/ {if ($$1 !~ "^[#.]") {print $$1}}'\
	| sort\
	| egrep -v -e '^[^[:alnum:]]' -e '^$@$$'
	@echo
	@exit 1

start:
	docker-compose up --build --detach
	@$(MAKE) seed

rebuild: start

stop:
	docker-compose down

destroy:
	docker-compose down --volumes

migrations:
	docker-compose exec cabotage-app python3 -m flask db revision --autogenerate -m "$(filter-out $@,$(MAKECMDGOALS))"

migrate:
	docker-compose exec cabotage-app python3 -m flask db upgrade

create-admin:
	docker-compose exec cabotage-app python3 -m cabotage.scripts.create_admin

seed: migrate
	docker-compose exec cabotage-app python3 -m cabotage.scripts.seed_localdev

routes:
	docker-compose exec cabotage-app python3 -m flask routes

lock:
	uv lock

test:
	docker compose exec db psql -U postgres -tc "SELECT 1 FROM pg_database WHERE datname = 'cabotage_test'" | grep -q 1 || \
		docker compose exec db psql -U postgres -c "CREATE DATABASE cabotage_test" && \
		docker compose exec db psql -U postgres -d cabotage_test -c "CREATE EXTENSION IF NOT EXISTS citext; CREATE EXTENSION IF NOT EXISTS pgcrypto;"
	docker compose run --rm \
		-e CABOTAGE_SQLALCHEMY_DATABASE_URI=postgresql://postgres@db/cabotage_test \
		-e CABOTAGE_TESTING=True \
		-e FLASK_APP=cabotage.server.wsgi \
		base sh -c "uv pip install pytest && python3 -m flask db upgrade && python3 -m pytest tests/ -v $(ARGS)"

reformat:
	docker compose run --build --rm base black .

lint:
	docker compose run --build --rm base bin/lint

security-check:
	docker compose run --build --rm base bandit -c pyproject.toml -r .

type-check:
	docker compose run --build --rm base mypy --config-file pyproject.toml .
