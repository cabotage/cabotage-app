"""Run one deployment; run_deploy starts this as a Kubernetes Job."""

import sys

from cabotage.celery.tasks.deploy import execute_deployment
from cabotage.server import create_app

app = create_app()

with app.app_context():
    execute_deployment(sys.argv[1])
