
from cabotage.server import (
    db,
    celery,
)
from cabotage.server.models.projects import (
    Hook,
)


class HookError(Exception):
    pass


def process_deployment_hook(hook):
    pass


def process_installation_hook(hook):
    if hook.payload['action'] == 'created':
        pass
    if hook.payload['action'] == 'deleted':
        pass


def process_installation_repositories_hook(hook):
    if hook.payload['action'] == 'created':
        pass
    if hook.payload['action'] == 'deleted':
        pass


@celery.task()
def process_github_hook(hook_id):
    hook = Hook.query.filter_by(id=hook_id).first()
    event = hook.headers['X-Github-Event']
    if event == 'deployment':
        process_deployment_hook(hook)
        hook.processed = True
        db.session.commit()
    if event == 'installation':
        process_installation_hook(hook)
        hook.processed = True
        db.session.commit()
    if event == 'installation_repositories':
        process_installation_repositories_hook(hook)
        hook.processed = True
        db.session.commit()
