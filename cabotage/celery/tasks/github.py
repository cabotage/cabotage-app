import datetime
import io
import os
import tarfile
import tempfile

from pathlib import Path

import requests

from cabotage.server import (
    db,
    celery,
    github_app,
    minio,
)
from cabotage.server.models.projects import (
    activity_plugin,
    Hook,
    Image,
    Application,
)
from cabotage.celery.tasks import (
    run_image_build,
)
from cabotage.utils.github import post_deployment_status_update

Activity = activity_plugin.activity_cls


class HookError(Exception):
    pass


def process_deployment_hook(hook):
    installation_id = hook.payload['installation']['id']
    deployment = hook.payload['deployment']
    environment = deployment['environment']
    repository = hook.payload['repository']
    sender = hook.payload['sender']
    bearer_token = github_app.bearer_token
    access_token = None

    try:
        slugs = environment.split('/')
        if len(slugs) != 2 or slugs[0] != 'cabotage':
            print('not configured for this environment')
            return False
        _, application_id = slugs

        application = Application.query.filter_by(id=application_id).first()
        if application is None:
            print('could not find application')
            return False

        if application.github_app_installation_id != installation_id:
            print('application not configured with installation id')
            return False

        access_token_response = requests.post(
            f'https://api.github.com/installations/{installation_id}/access_tokens',
            headers={
                'Accept': 'application/vnd.github.machine-man-preview+json',
                'Authorization': f'Bearer {bearer_token}',
            }
        )
        if 'token' not in access_token_response.json():
            print(f'Unable to authenticate for {installation_id}')
            print(access_token_response.json())
            raise HookError(f'Unable to authenticate for {installation_id}')

        access_token = access_token_response.json()

        post_deployment_status_update(
            access_token["token"],
            deployment['statuses_url'],
            'pending', 'Deployment is starting!'
        )

        tarball_request = requests.get(
            f'https://api.github.com/repos/{repository["full_name"]}/tarball/{deployment["sha"]}',
            headers={
                'Accept': 'application/vnd.github.machine-man-preview+json',
                'Authorization': f'token {access_token["token"]}',
            },
            stream=True,
        )

        github_tarball_fd, github_tarball_path = tempfile.mkstemp()
        release_tarball_fd, release_tarball_path = tempfile.mkstemp()
        try:
            print('rewriting tarfile... for reasons')
            with open(github_tarball_path, 'wb') as handle:
                for chunk in tarball_request.iter_content(4096):
                    handle.write(chunk)
            with tarfile.open(github_tarball_path, 'r') as github_tarfile:
                with tarfile.open(release_tarball_path, 'w|gz') as release_tarfile:
                    for member in github_tarfile:
                        tar_info = member
                        tar_info.name = f'./{str(Path(*Path(member.name).parts[1:]))}'
                        release_tarfile.addfile(
                            tar_info,
                            github_tarfile.extractfile(member)
                        )
            print('uploading tar to minio')
            with open(release_tarball_path, 'rb') as handle:
                minio_response = minio.write_object(application.project.organization.slug, application.project.slug, application.slug, handle)
            print(f'uploaded tar to {minio_response["path"]}')
        finally:
            os.remove(github_tarball_path)
            os.remove(release_tarball_path)

        image = Image(
            application_id=application.id,
            repository_name=f"cabotage/{application.project.organization.slug}/{application.project.slug}/{application.slug}",
            build_slug=minio_response['path'],
            image_metadata={**deployment, 'installation_id': installation_id, 'auto_deploy': True},
        )
        db.session.add(image)
        db.session.flush()
        activity = Activity(
            verb='submit',
            object=image,
            data={
                'sender': sender,
                'timestamp': datetime.datetime.utcnow().isoformat(),
            }
        )
        db.session.add(activity)
        db.session.commit()

        run_image_build.delay(image_id=image.id)

        post_deployment_status_update(
            access_token["token"],
            deployment['statuses_url'],
            'pending', 'Code retrieved! Image build commencing.'
        )
        return True
    except HookError as exc:
        if access_token and 'token' in access_token:
            post_deployment_status_update(
                access_token["token"],
                deployment['statuses_url'],
                'error', str(exc)
            )


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
        if hook.commit_sha is not None:
            environment = hook.payload.get('deployment', {}).get('environment')
            existing_hooks = (
                Hook.query
                .filter(Hook.commit_sha == hook.commit_sha)
                .filter(Hook.payload['deployment']['environment'].astext == environment)
                .count()
            )
            if existing_hooks > 1:
                return True  # we _should_ mark this deploy as complete
        hook.processed = process_deployment_hook(hook)
        db.session.commit()
    if event == 'installation':
        process_installation_hook(hook)
        hook.processed = True
        db.session.commit()
    if event == 'installation_repositories':
        process_installation_repositories_hook(hook)
        hook.processed = True
        db.session.commit()
