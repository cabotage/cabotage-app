web: gunicorn -c gunicorn.conf -b unix:/var/run/cabotage/cabotage.sock -w "4" --threads "100" cabotage.server.wsgi:app
worker: celery -A cabotage.celery.worker.celery_app worker -l info -E
worker-beat: celery -A cabotage.celery.worker.celery_app beat -l info -S redbeat.RedBeatScheduler
release: python -m flask db upgrade head
