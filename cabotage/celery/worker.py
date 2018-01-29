import os
from cabotage.server import celery, create_app

app = create_app()
app.app_context().push()
