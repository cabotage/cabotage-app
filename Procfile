web: gunicorn -c gunicorn.conf -b unix:/var/run/cabotage/cabotage.sock -w "4" --threads "100" cabotage.server.wsgi:app
worker: celery -A cabotage.celery.worker.celery_app worker -E
worker-beat: celery -A cabotage.celery.worker.celery_app beat -S redbeat.RedBeatScheduler
release: python -m flask db upgrade head
