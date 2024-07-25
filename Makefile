.PHONY: default \
	lint reformat type-check security-check \
	start stop rebuild destroy \
	migrate create-admin \
	routes

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

rebuild: start

stop:
	docker-compose down

destroy:
	docker-compose down --volumes

migrate:
	docker-compose exec cabotage-app python3 -m flask db upgrade

create-admin:
	docker-compose exec cabotage-app python3 -m cabotage.scripts.create_admin

routes:
	docker-compose exec cabotage-app python3 -m flask routes

requirements/%.txt: requirements/%.in
	docker compose run --build --rm base pip-compile --generate-hashes --output-file=$@ $(F) $<

reformat:
	docker compose run --build --rm base black .

lint:
	docker compose run --build --rm base bin/lint

security-check:
	docker compose run --build --rm base bandit -c pyproject.toml -r .

type-check:
	docker compose run --build --rm base mypy --config-file pyproject.toml .
