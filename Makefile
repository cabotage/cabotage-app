.PHONY: default lint reformat start stop rebuild destroy migrate create-admin

default:
	@echo "Call a specific subcommand:"
	@echo
	@$(MAKE) -pRrq -f $(lastword $(MAKEFILE_LIST)) : 2>/dev/null\
	| awk -v RS= -F: '/^# File/,/^# Finished Make data base/ {if ($$1 !~ "^[#.]") {print $$1}}'\
	| sort\
	| egrep -v -e '^[^[:alnum:]]' -e '^$@$$'
	@echo
	@exit 1

lint:
	tox -e black
	tox -e ruff

reformat:
	tox -e reformat

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
