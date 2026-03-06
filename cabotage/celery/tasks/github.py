import datetime
import logging

from celery import shared_task
from sqlalchemy import and_, or_
from sqlalchemy.orm.exc import MultipleResultsFound, NoResultFound

from cabotage.server import (
    db,
    github_app,
)
from cabotage.server.models.projects import (
    activity_plugin,
    Environment,
    Hook,
    Image,
    Application,
    ApplicationEnvironment,
    Project,
)
from cabotage.server.models.auth import Organization
from cabotage.celery.tasks import (
    run_image_build,
)
from cabotage.celery.tasks.branch_deploy import (
    create_branch_deploy,
    sync_branch_deploy,
    teardown_branch_deploy,
)
from cabotage.utils.github import github_session, post_deployment_status_update

Activity = activity_plugin.activity_cls
logger = logging.getLogger(__name__)


class HookError(Exception):
    pass


def _resolve_app_env_for_hook(installation_id, repository_name, environment):
    """Resolve an ApplicationEnvironment from GitHub deployment hook data.

    Tries in order:
    1. ApplicationEnvironment.github_environment_name match
    2. Application.github_environment_name match -> default_app_env
    3. Slug-based parsing: project/env/app or project/app -> default_app_env
    """
    # 1. Try matching an ApplicationEnvironment by github_environment_name
    app_env = (
        ApplicationEnvironment.query.join(Application)
        .filter(
            and_(
                ApplicationEnvironment.github_environment_name == environment,
                Application.github_app_installation_id == installation_id,
                Application.github_repository == repository_name,
            )
        )
        .first()
    )
    if app_env:
        return app_env

    # 2. Try matching Application by github_environment_name
    try:
        application = Application.query.filter(
            and_(
                Application.github_app_installation_id == installation_id,
                Application.github_repository == repository_name,
                Application.github_environment_name == environment,
            )
        ).one()
        return application.default_app_env
    except NoResultFound:
        pass
    except MultipleResultsFound:
        print(
            f"multiple apps configured for installation {installation_id} "
            f"on {repository_name} with environment {environment}!"
        )
        return None

    # 3. Fall back to slug-based parsing
    slugs = environment.split("/")
    if len(slugs) == 2:
        project_slug, app_slug = slugs
        application = (
            Application.query.join(Project)
            .filter(
                and_(
                    Project.slug == project_slug,
                    Application.slug == app_slug,
                    Application.github_app_installation_id == installation_id,
                    Application.github_repository == repository_name,
                )
            )
            .first()
        )
        if application:
            return application.default_app_env
    elif len(slugs) == 3:
        project_slug, env_slug, app_slug = slugs
        app_env = (
            ApplicationEnvironment.query.join(Application)
            .join(Environment, ApplicationEnvironment.environment_id == Environment.id)
            .join(Project, Application.project_id == Project.id)
            .filter(
                and_(
                    Project.slug == project_slug,
                    Environment.slug == env_slug,
                    Application.slug == app_slug,
                    Application.github_app_installation_id == installation_id,
                    Application.github_repository == repository_name,
                )
            )
            .first()
        )
        if app_env:
            return app_env
    elif len(slugs) == 4:
        org_slug, project_slug, env_slug, app_slug = slugs
        app_env = (
            ApplicationEnvironment.query.join(Application)
            .join(Environment, ApplicationEnvironment.environment_id == Environment.id)
            .join(Project, Application.project_id == Project.id)
            .join(Organization, Project.organization_id == Organization.id)
            .filter(
                and_(
                    Organization.slug == org_slug,
                    Project.slug == project_slug,
                    Environment.slug == env_slug,
                    Application.slug == app_slug,
                    Application.github_app_installation_id == installation_id,
                    Application.github_repository == repository_name,
                )
            )
            .first()
        )
        if app_env:
            return app_env

    return None


def process_deployment_hook(hook):
    installation_id = hook.payload["installation"]["id"]
    deployment = hook.payload["deployment"]

    # Only process deployments created by this app's bot
    if deployment["creator"]["login"] != github_app.bot_login:
        print(
            f"ignoring deployment created by {deployment['creator']['login']} "
            f"(not {github_app.bot_login})"
        )
        return False

    environment = deployment["environment"]
    repository_name = hook.payload["repository"]["full_name"]
    commit_sha = hook.payload["deployment"]["sha"]
    sender = hook.payload["sender"]
    bearer_token = github_app.bearer_token
    access_token = None

    hook.commit_sha = commit_sha

    try:
        app_env = _resolve_app_env_for_hook(
            installation_id, repository_name, environment
        )
        if app_env is None:
            print("not configured for this environment")
            return False
        application = app_env.application

        access_token_response = github_session.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers={
                "Accept": "application/vnd.github.machine-man-preview+json",
                "Authorization": f"Bearer {bearer_token}",
            },
            timeout=10,
        )
        if "token" not in access_token_response.json():
            print(f"Unable to authenticate for {installation_id}")
            print(access_token_response.json())
            raise HookError(f"Unable to authenticate for {installation_id}")

        access_token = access_token_response.json()

        post_deployment_status_update(
            access_token["token"],
            deployment["statuses_url"],
            "in_progress",
            "Deployment is starting!",
        )

        image = Image(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=application.registry_repository_name(app_env),
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
            "in_progress",
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
    access_token=None,
    application=None,
    repository_name=None,
    ref=None,
    app_env=None,
):
    try:
        environment_string = app_env.effective_github_environment_name

        deployment_response = github_session.post(
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
            timeout=10,
        )
        post_deployment_status_update(
            access_token["token"],
            deployment_response.json()["statuses_url"],
            "pending",
            "Deployment created.",
        )
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

    env_matches = (
        ApplicationEnvironment.query.join(Application)
        .join(Project, Application.project_id == Project.id)
        .join(Environment, ApplicationEnvironment.environment_id == Environment.id)
        .filter(
            and_(
                Environment.ephemeral.is_(False),
                or_(
                    ApplicationEnvironment.auto_deploy_branch.in_(branch_names),
                    and_(
                        ApplicationEnvironment.auto_deploy_branch.is_(None),
                        Application.auto_deploy_branch.in_(branch_names),
                    ),
                ),
                Application.github_app_installation_id == installation_id,
                Application.github_repository == repository_name,
            )
        )
        .all()
    )
    if len(env_matches) == 0:
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
        env_matches = (
            ApplicationEnvironment.query.join(Application)
            .join(Project, Application.project_id == Project.id)
            .join(Environment, ApplicationEnvironment.environment_id == Environment.id)
            .filter(
                and_(
                    Environment.ephemeral.is_(False),
                    or_(
                        ApplicationEnvironment.auto_deploy_branch.in_(branch_names),
                        and_(
                            ApplicationEnvironment.auto_deploy_branch.is_(None),
                            Application.auto_deploy_branch.in_(branch_names),
                        ),
                    ),
                    Application.github_app_installation_id == installation_id,
                    Application.github_repository == repository_name,
                )
            )
            .all()
        )
        if len(env_matches) == 0:
            print(
                f"could not find application! "
                f"installation_id: {installation_id}, "
                f"repository_name: {repository_name}, "
                f"branches: {branch_names}"
            )
            return False

        access_token_response = github_session.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers={
                "Accept": "application/vnd.github.machine-man-preview+json",
                "Authorization": f"Bearer {bearer_token}",
            },
            timeout=10,
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
        for app_env in env_matches:
            print(
                f"deploying {repository_name}@{commit_sha} to "
                f"{app_env.application.id} env {app_env.environment.slug}"
            )
            deployment_result = create_deployment(
                access_token=access_token,
                application=app_env.application,
                repository_name=repository_name,
                ref=commit_sha,
                app_env=app_env,
            )
            results.append(deployment_result)

        if all(results):
            push_event.deployed = True
            db.session.commit()


def process_pull_request_hook(hook):
    action = hook.payload["action"]
    if action not in ("opened", "reopened", "synchronize", "closed"):
        return

    installation_id = hook.payload["installation"]["id"]
    repository_name = hook.payload["repository"]["full_name"]
    pr = hook.payload["pull_request"]

    # Skip PRs from forks
    head_repo = (pr.get("head", {}).get("repo") or {}).get("full_name")
    base_repo = (pr.get("base", {}).get("repo") or {}).get("full_name")
    if head_repo != base_repo:
        logger.info(
            "ignoring pull_request from fork %s (base: %s)",
            head_repo,
            base_repo,
        )
        return

    # Skip PRs opened by bot accounts
    pr_author = pr.get("user", {})
    if pr_author.get("type") == "Bot" or (pr_author.get("login") or "").endswith(
        "[bot]"
    ):
        logger.info("ignoring pull_request from bot %s", pr_author.get("login"))
        return

    pr_number = pr["number"]
    head_sha = pr["head"]["sha"]
    head_ref = pr["head"]["ref"]
    hook.commit_sha = head_sha

    projects = (
        Project.query.join(Application)
        .filter(
            Application.github_app_installation_id == installation_id,
            Application.github_repository == repository_name,
            Project.branch_deploys_enabled.is_(True),
            Project.branch_deploy_base_environment_id.isnot(None),
        )
        .distinct()
        .all()
    )
    if not projects:
        return

    for project in projects:
        if action in ("opened", "reopened"):
            create_branch_deploy(
                project, pr_number, head_sha, installation_id, head_ref
            )
        elif action == "synchronize":
            sync_branch_deploy(project, pr_number, head_sha, installation_id)
        elif action == "closed":
            teardown_branch_deploy(project, pr_number)


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
    if event == "pull_request":
        process_pull_request_hook(hook)
        hook.processed = True
        db.session.commit()
