import datetime

import requests

from celery import shared_task
from sqlalchemy import and_
from sqlalchemy.orm.exc import MultipleResultsFound, NoResultFound

from cabotage.server import (
    db,
    github_app,
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
    installation_id = hook.payload["installation"]["id"]
    deployment = hook.payload["deployment"]
    environment = deployment["environment"]
    repository_name = hook.payload["repository"]["full_name"]
    commit_sha = hook.payload["deployment"]["sha"]
    sender = hook.payload["sender"]
    bearer_token = github_app.bearer_token
    access_token = None

    hook.commit_sha = commit_sha

    try:
        try:
            application = Application.query.filter(
                and_(
                    Application.github_app_installation_id == installation_id,
                    Application.github_repository == repository_name,
                    Application.github_environment_name == environment,
                )
            ).one()
        except NoResultFound:
            slugs = environment.split("/")
            if len(slugs) != 2 or slugs[0] != "cabotage":
                print("not configured for this environment")
                return False
            _, application_id = slugs

            application = Application.query.filter_by(id=application_id).first()
            if application is None:
                print("could not find application")
                return False

            if application.github_app_installation_id != installation_id:
                print("application not configured with installation id")
                return False
        except MultipleResultsFound:
            print(
                f"multiple apps configured for installation {installation_id} "
                f"on {repository_name} with environment {environment}!"
            )
            return False

        access_token_response = requests.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers={
                "Accept": "application/vnd.github.machine-man-preview+json",
                "Authorization": f"Bearer {bearer_token}",
            },
            timeout=10
        )
        if "token" not in access_token_response.json():
            print(f"Unable to authenticate for {installation_id}")
            print(access_token_response.json())
            raise HookError(f"Unable to authenticate for {installation_id}")

        access_token = access_token_response.json()

        post_deployment_status_update(
            access_token["token"],
            deployment["statuses_url"],
            "pending",
            "Deployment is starting!",
        )

        image = Image(
            application_id=application.id,
            repository_name=(
                f"cabotage/{application.project.organization.slug}/"
                f"{application.project.slug}/{application.slug}"
            ),
            image_metadata={
                **deployment,
                "installation_id": installation_id,
                "auto_deploy": True,
            },
            build_ref=deployment["sha"],
        )
        db.session.add(image)
        db.session.flush()
        activity = Activity(
            verb="submit",
            object=image,
            data={
                "sender": sender,
                "timestamp": datetime.datetime.utcnow().isoformat(),
            },
        )
        db.session.add(activity)
        db.session.commit()

        run_image_build.delay(image_id=image.id)

        post_deployment_status_update(
            access_token["token"],
            deployment["statuses_url"],
            "pending",
            "Image build commencing.",
        )
        return True
    except HookError as exc:
        if access_token and "token" in access_token:
            post_deployment_status_update(
                access_token["token"], deployment["statuses_url"], "error", str(exc)
            )


def process_installation_hook(hook):
    if hook.payload["action"] == "created":
        pass
    if hook.payload["action"] == "deleted":
        pass


def process_installation_repositories_hook(hook):
    if hook.payload["action"] == "created":
        pass
    if hook.payload["action"] == "deleted":
        pass


def create_deployment(
    access_token=None, application=None, repository_name=None, ref=None
):
    try:
        environment_string = f"cabotage/{application.id}"
        if application.github_environment_name is not None:
            environment_string = application.github_environment_name

        deployment_response = requests.post(
            f"https://api.github.com/repos/{repository_name}/deployments",
            headers={
                "Accept": "application/vnd.github.machine-man-preview+json",
                "Authorization": f'token {access_token["token"]}',
            },
            json={
                "ref": ref,
                "auto_merge": False,
                "environment": environment_string,
            },
            timeout=10
        )
        print(deployment_response.status_code)
        deployment_response.raise_for_status()
    except Exception:
        return False
    return True


def process_push_hook(hook):
    installation_id = hook.payload["installation"]["id"]
    repository_name = hook.payload["repository"]["full_name"]
    branch_names = [hook.payload["ref"].lstrip("refs/heads/")]
    commit_sha = hook.payload["after"]

    hook.commit_sha = commit_sha

    applications = Application.query.filter(
        and_(
            Application.auto_deploy_branch.in_(branch_names),
            Application.github_app_installation_id == installation_id,
            Application.github_repository == repository_name,
        )
    ).all()
    if len(applications) == 0:
        print(
            f"could not find application! "
            f"installation_id: {installation_id}, "
            f"repository_name: {repository_name}, "
            f"branches: {branch_names}"
        )
        return False


def process_check_suite_hook(hook):
    installation_id = hook.payload["installation"]["id"]
    repository_name = hook.payload["repository"]["full_name"]
    branch_names = [hook.payload["check_suite"]["head_branch"]]
    commit_sha = hook.payload["check_suite"]["head_sha"]
    bearer_token = github_app.bearer_token
    access_token = None

    hook.commit_sha = commit_sha

    if hook.payload["check_suite"]["conclusion"] == "success":
        pushes = (
            Hook.query.filter(Hook.commit_sha == hook.commit_sha)
            .filter(Hook.headers["X-Github-Event"].astext == "push")
            .count()
        )
        if pushes == 0:
            return False
        applications = Application.query.filter(
            and_(
                Application.auto_deploy_branch.in_(branch_names),
                Application.github_app_installation_id == installation_id,
                Application.github_repository == repository_name,
            )
        ).all()
        if len(applications) == 0:
            print(
                f"could not find application! "
                f"installation_id: {installation_id}, "
                f"repository_name: {repository_name}, "
                f"branches: {branch_names}"
            )
            return False

        access_token_response = requests.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers={
                "Accept": "application/vnd.github.machine-man-preview+json",
                "Authorization": f"Bearer {bearer_token}",
            },
            timeout=10
        )
        if "token" not in access_token_response.json():
            print(f"Unable to authenticate for {installation_id}")
            print(access_token_response.json())
            raise HookError(f"Unable to authenticate for {installation_id}")

        access_token = access_token_response.json()

        try:
            push_event = (
                Hook.query.filter(Hook.commit_sha == commit_sha)
                .filter(Hook.headers.op("->>")("X-Github-Event") == "push")
                .one()
            )
        except NoResultFound:
            print(
                f"ignoring check_suite without push for {repository_name}@{commit_sha}"
            )

        if push_event.deployed:
            print(
                "skipping auto-deploy for previously deployed "
                f"{repository_name}@{commit_sha}"
            )
            return False

        results = []
        for application in applications:
            print(f"deploying {repository_name}@{commit_sha} to {application.id}")
            deployment_result = create_deployment(
                access_token=access_token,
                application=application,
                repository_name=repository_name,
                ref=commit_sha,
            )
            results.append(deployment_result)

        if all(results):
            push_event.deployed = True
            db.session.commit()


@shared_task()
def process_github_hook(hook_id):
    hook = Hook.query.filter_by(id=hook_id).first()
    event = hook.headers["X-Github-Event"]
    if event == "deployment":
        if hook.commit_sha is not None:
            installation_id = hook.payload.get("installation", {}).get("id")
            environment = hook.payload.get("deployment", {}).get("environment")
            existing_hooks = (
                Hook.query.filter(Hook.commit_sha == hook.commit_sha)
                .filter(Hook.payload["installation"]["id"].astext == installation_id)
                .filter(Hook.payload["deployment"]["environment"].astext == environment)
                .count()
            )
            if existing_hooks > 1:
                return True  # we _should_ mark this deploy as complete
        hook.processed = process_deployment_hook(hook)
        db.session.commit()
    if event == "push":
        process_push_hook(hook)
        hook.processed = True
        db.session.commit()
    if event == "check_suite":
        process_check_suite_hook(hook)
        hook.processed = True
        db.session.commit()
    if event == "installation":
        process_installation_hook(hook)
        hook.processed = True
        db.session.commit()
    if event == "installation_repositories":
        process_installation_repositories_hook(hook)
        hook.processed = True
        db.session.commit()
