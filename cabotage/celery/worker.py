from cabotage.server import create_app

app = create_app()
celery_app = app.extensions["celery"]
