from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING

from celery import shared_task
from flask import current_app
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
from cabotage.server.models.auth import (
    GitHubAppInstallation,
    Organization,
)
from cabotage.server.user.github_installations import (
    merge_repository_metadata,
    reconcile_selected_repository_applications,
    sync_application_repository_metadata,
    sync_installation_repositories,
)
from cabotage.celery.tasks import (
    run_image_build,
    run_omnibus_build,
)
from cabotage.celery.tasks.branch_deploy import (
    create_branch_deploy,
    sync_branch_deploy,
    teardown_branch_deploy,
)
from cabotage.utils.github import (
    cabotage_url,
    github_session,
    matches_watch_paths,
    post_deployment_status_update,
)
from cabotage.celery.tasks.notify import dispatch_autodeploy_notification

if TYPE_CHECKING:
    from uuid import UUID

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
                Application.deleted_at.is_(None),
                ApplicationEnvironment.deleted_at.is_(None),
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
                Application.deleted_at.is_(None),
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
                    Application.deleted_at.is_(None),
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
                    Application.deleted_at.is_(None),
                    ApplicationEnvironment.deleted_at.is_(None),
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
                    Application.deleted_at.is_(None),
                    ApplicationEnvironment.deleted_at.is_(None),
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
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            },
        )
        db.session.add(activity)
        db.session.commit()

        if current_app.config.get("CABOTAGE_OMNIBUS_BUILDS"):
            run_omnibus_build.delay(image_id=image.id)
        else:
            run_image_build.delay(image_id=image.id)

        post_deployment_status_update(
            access_token["token"],
            deployment["statuses_url"],
            "in_progress",
            "Image build commencing.",
        )
        try:
            dispatch_autodeploy_notification(
                "image_building",
                image.id,
                application,
                app_env,
                image_url=cabotage_url(application, f"images/{image.id}"),
                image_metadata=image.image_metadata,
            )
        except Exception:
            logger.warning(
                "Failed to dispatch autodeploy image_building notification",
                exc_info=True,
            )
        return True
    except HookError as exc:
        if access_token and "token" in access_token:
            post_deployment_status_update(
                access_token["token"], deployment["statuses_url"], "error", str(exc)
            )


def process_installation_hook(hook):
    installation_id = hook.payload.get("installation", {}).get("id")
    if installation_id is None:
        return False

    action = hook.payload.get("action")
    if action in ("created", "new_permissions_accepted"):
        _sync_known_installation_metadata(hook.payload["installation"])
        return True
    if action == "deleted":
        GitHubAppInstallation.query.filter_by(installation_id=installation_id).delete()
        (
            Application.query.filter_by(
                github_app_installation_id=installation_id
            ).update(
                {
                    "github_app_installation_id": None,
                    "github_repository_id": None,
                    "github_repository_is_private": False,
                },
                synchronize_session=False,
            )
        )
        return True
    return False


def process_installation_repositories_hook(hook):
    installation_id = hook.payload.get("installation", {}).get("id")
    if installation_id is None:
        return False

    action = hook.payload.get("action")
    if action not in ("added", "removed"):
        return False

    _sync_known_installation_repository_delta(
        hook.payload["installation"],
        action,
        repositories_added=hook.payload.get("repositories_added", []),
        repositories_removed=hook.payload.get("repositories_removed", []),
    )
    if action == "removed":
        removed_ids = [
            repo.get("id")
            for repo in hook.payload.get("repositories_removed", [])
            if repo.get("id") is not None
        ]
        removed_repos = [
            repo.get("full_name")
            for repo in hook.payload.get("repositories_removed", [])
            if repo.get("full_name")
        ]
        if removed_ids or removed_repos:
            (
                Application.query.filter(
                    Application.github_app_installation_id == installation_id,
                    or_(
                        Application.github_repository_id.in_(removed_ids),
                        Application.github_repository.in_(removed_repos),
                    ),
                ).update(
                    {
                        "github_app_installation_id": None,
                        "github_repository_id": None,
                        "github_repository_is_private": False,
                    },
                    synchronize_session=False,
                )
            )
        return True
    if action == "added":
        return True
    return False


def process_installation_target_hook(hook):
    action = hook.payload.get("action")
    if action != "renamed":
        return False

    installation_id = hook.payload.get("installation", {}).get("id")
    if installation_id is None:
        return False

    account = hook.payload.get("account") or {}
    known_installations = GitHubAppInstallation.query.filter_by(
        installation_id=installation_id
    ).all()

    new_login = account.get("login")

    for app_installation in known_installations:
        old_login = app_installation.account_login
        if old_login and new_login and old_login != new_login:
            app_installation.repositories = _rename_cached_repository_owner(
                app_installation.repositories,
                old_login=old_login,
                new_login=new_login,
            )
            _rename_application_repository_owner(
                app_installation,
                old_login=old_login,
                new_login=new_login,
            )
            sync_application_repository_metadata(app_installation)
        app_installation.account_id = account.get("id")
        app_installation.account_login = new_login
        app_installation.account_type = account.get("type")

    return bool(known_installations)


def process_repository_hook(hook):
    installation_id = hook.payload.get("installation", {}).get("id")
    repository = hook.payload.get("repository") or {}
    repository_id = repository.get("id")
    repository_full_name = repository.get("full_name")
    if installation_id is None or repository_id is None or not repository_full_name:
        return False

    known_installations = GitHubAppInstallation.query.filter_by(
        installation_id=installation_id
    ).all()
    repository_metadata = {
        "id": repository_id,
        "full_name": repository_full_name,
        "private": bool(repository.get("private")),
    }

    for app_installation in known_installations:
        previous_names = _update_cached_repository_metadata(
            app_installation,
            repository_metadata,
        )
        if previous_names:
            _rename_application_repositories(
                app_installation.installation_id,
                previous_names=previous_names,
                repo_id=repository_id,
                new_name=repository_full_name,
                private=bool(repository.get("private")),
            )
        else:
            _update_application_repository_privacy(
                app_installation.installation_id,
                repo_id=repository_id,
                repository_name=repository_full_name,
                private=bool(repository.get("private")),
            )

    return bool(known_installations)


def _update_cached_repository_metadata(app_installation, repository_metadata):
    if app_installation.repositories is None:
        return set()

    repository_id = repository_metadata["id"]
    repository_full_name = repository_metadata["full_name"]
    previous_names = set()
    updated = False
    repositories = []
    for repository in app_installation.repositories:
        if not isinstance(repository, dict):
            repositories.append(repository)
            continue

        if (
            repository.get("id") == repository_id
            or repository.get("full_name") == repository_full_name
        ):
            previous_name = repository.get("full_name")
            if previous_name:
                previous_names.add(previous_name)
            repositories.append(repository_metadata)
            updated = True
        else:
            repositories.append(repository)

    if updated:
        app_installation.repositories = sorted(
            repositories,
            key=lambda repo: repo.get("full_name") if isinstance(repo, dict) else "",
        )
        app_installation.repositories_synced_at = datetime.datetime.now(
            datetime.timezone.utc
        ).replace(tzinfo=None)
    return previous_names


def _rename_application_repositories(
    installation_id,
    *,
    previous_names,
    repo_id,
    new_name,
    private,
):
    (
        Application.query.filter(
            Application.github_app_installation_id == installation_id,
            Application.github_repository.in_(previous_names),
        ).update(
            {
                "github_repository_id": repo_id,
                "github_repository": new_name,
                "github_repository_is_private": private,
            },
            synchronize_session=False,
        )
    )


def _update_application_repository_privacy(
    installation_id, *, repo_id, repository_name, private
):
    (
        Application.query.filter(
            Application.github_app_installation_id == installation_id,
            or_(
                Application.github_repository_id == repo_id,
                Application.github_repository == repository_name,
            ),
        ).update(
            {
                "github_repository_id": repo_id,
                "github_repository": repository_name,
                "github_repository_is_private": private,
            },
            synchronize_session=False,
        )
    )


def _rename_cached_repository_owner(repositories, *, old_login, new_login):
    if repositories is None:
        return None

    old_prefix = f"{old_login}/"
    new_prefix = f"{new_login}/"
    renamed_repositories = []
    for repository in repositories:
        if not isinstance(repository, dict):
            renamed_repositories.append(repository)
            continue

        renamed_repository = dict(repository)
        full_name = renamed_repository.get("full_name")
        if isinstance(full_name, str) and full_name.startswith(old_prefix):
            renamed_repository["full_name"] = (
                f"{new_prefix}{full_name[len(old_prefix) :]}"
            )
        renamed_repositories.append(renamed_repository)
    return renamed_repositories


def _rename_application_repository_owner(app_installation, *, old_login, new_login):
    old_prefix = f"{old_login}/"
    new_prefix = f"{new_login}/"
    for application in Application.query.filter_by(
        github_app_installation_id=app_installation.installation_id,
    ).filter(Application.github_repository.startswith(old_prefix)):
        application.github_repository = (
            f"{new_prefix}{application.github_repository[len(old_prefix) :]}"
        )


def _sync_known_installation_repository_delta(
    installation,
    action,
    *,
    repositories_added,
    repositories_removed,
):
    installation_id = installation.get("id")
    if installation_id is None:
        return 0

    account = installation.get("account") or {}
    known_installations = GitHubAppInstallation.query.filter_by(
        installation_id=installation_id
    ).all()

    removed_ids = {
        repo.get("id") for repo in repositories_removed if repo.get("id") is not None
    }
    removed_names = {
        repo.get("full_name") for repo in repositories_removed if repo.get("full_name")
    }

    for app_installation in known_installations:
        app_installation.account_id = account.get("id")
        app_installation.account_login = account.get("login")
        app_installation.account_type = account.get("type")
        app_installation.repository_selection = installation.get("repository_selection")
        if app_installation.repositories is None:
            continue
        if action == "added":
            app_installation.repositories = merge_repository_metadata(
                app_installation.repositories,
                repositories_added,
            )
            app_installation.repositories_synced_at = datetime.datetime.now(
                datetime.timezone.utc
            ).replace(tzinfo=None)
            sync_application_repository_metadata(app_installation)
        elif action == "removed":
            app_installation.repositories = [
                repo
                for repo in app_installation.repositories
                if repo.get("id") not in removed_ids
                and repo.get("full_name") not in removed_names
            ]
            app_installation.repositories_synced_at = datetime.datetime.now(
                datetime.timezone.utc
            ).replace(tzinfo=None)
            reconcile_selected_repository_applications(app_installation)

    return len(known_installations)


def _sync_known_installation_metadata(installation):
    installation_id = installation.get("id")
    if installation_id is None:
        return 0

    account = installation.get("account") or {}
    known_installations = GitHubAppInstallation.query.filter_by(
        installation_id=installation_id
    ).all()

    for app_installation in known_installations:
        previous_selection = app_installation.repository_selection
        app_installation.account_id = account.get("id")
        app_installation.account_login = account.get("login")
        app_installation.account_type = account.get("type")
        app_installation.repository_selection = installation.get("repository_selection")
        if "repositories" in installation:
            app_installation.repositories = merge_repository_metadata(
                [], installation.get("repositories", [])
            )
            app_installation.repositories_synced_at = datetime.datetime.now(
                datetime.timezone.utc
            ).replace(tzinfo=None)
            sync_application_repository_metadata(app_installation)
        elif (
            app_installation.repository_selection == "all"
            and previous_selection != "all"
        ):
            app_installation.repositories = None
            app_installation.repositories_synced_at = None

    return len(known_installations)


@shared_task()
def sync_github_app_installations():
    synced = 0
    disconnected = 0
    failed = 0
    for app_installation in GitHubAppInstallation.query.all():
        installation = github_app.fetch_installation(app_installation.installation_id)
        if installation is None:
            failed += 1
            continue

        account = installation.get("account") or {}
        app_installation.account_id = account.get("id")
        app_installation.account_login = account.get("login")
        app_installation.account_type = account.get("type")
        app_installation.repository_selection = installation.get("repository_selection")
        if sync_installation_repositories(
            app_installation,
            clear_all_cache_on_failure=False,
        ):
            disconnected += reconcile_selected_repository_applications(app_installation)
            synced += 1
        else:
            failed += 1

    db.session.commit()
    return {"synced": synced, "failed": failed, "disconnected": disconnected}


def _required_contexts_for_branch(access_token, repository_name, branch):
    """Fetch required status check contexts for a branch, excluding our own.

    Queries both legacy branch protection rules and repository rulesets to get
    the authoritative list of required checks, then filters out any belonging
    to our own GitHub App.

    Returns a list of context names.
    """
    if github_app.app_id is None:
        raise HookError("GitHub App ID not configured")
    own_app_id = int(github_app.app_id)
    required = []

    # Try legacy branch protection API first.
    resp = github_session.get(
        f"https://api.github.com/repos/{repository_name}/branches/{branch}/protection/required_status_checks",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"token {access_token['token']}",
        },
        timeout=10,
    )
    print(
        f"required_status_checks for {repository_name} branch {branch}: "
        f"{resp.status_code} {resp.text}"
    )
    if resp.status_code != 404:
        resp.raise_for_status()
        data = resp.json()
        checks = data.get("checks", [])
        if checks:
            required = [c["context"] for c in checks if c.get("app_id") != own_app_id]
        else:
            required = data.get("contexts", [])

    # Also query the repository rulesets API, which covers rules configured
    # via GitHub's newer rulesets feature (not returned by the legacy API).
    rules_resp = github_session.get(
        f"https://api.github.com/repos/{repository_name}/rules/branches/{branch}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"token {access_token['token']}",
        },
        timeout=10,
    )
    print(
        f"branch rules for {repository_name} branch {branch}: "
        f"{rules_resp.status_code} {rules_resp.text}"
    )
    if rules_resp.status_code != 404:
        rules_resp.raise_for_status()
        existing = set(required)
        for rule in rules_resp.json():
            if rule.get("type") != "required_status_checks":
                continue
            for check in rule.get("parameters", {}).get("required_status_checks", []):
                context = check.get("context")
                integration_id = check.get("integration_id")
                if not context:
                    continue
                if integration_id and int(integration_id) == own_app_id:
                    continue
                if context not in existing:
                    required.append(context)
                    existing.add(context)

    return required


def _all_required_checks_passed(
    access_token, repository_name, commit_sha, required_contexts
):
    """Check whether all required status checks have passed for a commit.

    Queries both the combined status API (for commit statuses) and the check
    runs API (for GitHub Actions), then verifies every context in
    required_contexts has a successful result.

    Returns True if all required contexts have passed, False otherwise.
    """
    if not required_contexts:
        return True

    passed = set()

    # Check runs (GitHub Actions)
    page = 1
    while True:
        resp = github_session.get(
            f"https://api.github.com/repos/{repository_name}/commits/{commit_sha}/check-runs",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"token {access_token['token']}",
            },
            params={"per_page": 100, "page": page},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        for run in data.get("check_runs", []):
            if run.get("conclusion") == "success":
                passed.add(run["name"])
        if len(data.get("check_runs", [])) < 100:
            break
        page += 1

    # Commit statuses (legacy status API)
    resp = github_session.get(
        f"https://api.github.com/repos/{repository_name}/commits/{commit_sha}/status",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"token {access_token['token']}",
        },
        timeout=10,
    )
    resp.raise_for_status()
    for status in resp.json().get("statuses", []):
        if status.get("state") == "success":
            passed.add(status["context"])

    missing = set(required_contexts) - passed
    if missing:
        logger.info(
            "skipping deployment for %s@%s: required checks not yet passed: %s",
            repository_name,
            commit_sha,
            ", ".join(sorted(missing)),
        )
        return False
    return True


def create_deployment(
    access_token: dict,
    repository_name: str,
    ref: str,
    application: Application | None = None,
    app_env: ApplicationEnvironment | None = None,
    branch: str | None = None,
    transient_environment: bool = False,
    environment_name: str | None = None,
    payload: dict | None = None,
    required_contexts: list | None = None,
):
    try:
        if environment_name:
            environment_string = environment_name
        elif app_env is not None:
            environment_string = app_env.effective_github_environment_name
        else:
            raise ValueError("Either environment_name or app_env must be provided")

        deploy_payload = {
            "ref": ref,
            "auto_merge": False,
            "environment": environment_string,
        }
        if payload is not None:
            deploy_payload["payload"] = payload

        if transient_environment:
            deploy_payload["transient_environment"] = True
            deploy_payload["production_environment"] = False
            # Skip required contexts for transient (branch deploy) environments
            deploy_payload["required_contexts"] = []
        elif branch:
            # Use pre-fetched required contexts if available, otherwise fetch.
            if required_contexts is None:
                required_contexts = _required_contexts_for_branch(
                    access_token, repository_name, branch
                )
            deploy_payload["required_contexts"] = required_contexts
        elif required_contexts is not None:
            deploy_payload["required_contexts"] = required_contexts

        deployment_response = github_session.post(
            f"https://api.github.com/repos/{repository_name}/deployments",
            headers={
                "Accept": "application/vnd.github.machine-man-preview+json",
                "Authorization": f"token {access_token['token']}",
            },
            json=deploy_payload,
            timeout=10,
        )
        deployment_response.raise_for_status()
        statuses_url = deployment_response.json()["statuses_url"]
        post_deployment_status_update(
            access_token["token"],
            statuses_url,
            "pending",
            "Deployment created.",
        )
    except Exception:
        logger.exception(
            "failed to create deployment for %s ref=%s",
            repository_name,
            ref,
        )
        return None
    return statuses_url


def process_push_hook(hook):
    installation_id = hook.payload["installation"]["id"]
    repository_name = hook.payload["repository"]["full_name"]
    branch_names = [hook.payload["ref"].removeprefix("refs/heads/")]
    commit_sha = hook.payload["after"]

    hook.commit_sha = commit_sha

    env_matches = (
        ApplicationEnvironment.query.join(Application)
        .join(Project, Application.project_id == Project.id)
        .join(Environment, ApplicationEnvironment.environment_id == Environment.id)
        .filter(
            and_(
                Environment.ephemeral.is_(False),
                Application.deleted_at.is_(None),
                ApplicationEnvironment.deleted_at.is_(None),
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

    # Deploy immediately for apps that don't wait for CI.
    skip_ci_matches = [ae for ae in env_matches if not ae.auto_deploy_wait_for_ci]
    if skip_ci_matches:
        bearer_token = github_app.bearer_token
        access_token_response = github_session.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers={
                "Accept": "application/vnd.github.machine-man-preview+json",
                "Authorization": f"Bearer {bearer_token}",
            },
            timeout=10,
        )
        if "token" in access_token_response.json():
            access_token = access_token_response.json()

            # Extract changed files for watch path filtering.
            changed_files = set()
            for commit in hook.payload.get("commits", []):
                changed_files.update(commit.get("added", []))
                changed_files.update(commit.get("modified", []))
                changed_files.update(commit.get("removed", []))

            for app_env in skip_ci_matches:
                watch_paths = app_env.application.branch_deploy_watch_paths
                if (
                    watch_paths
                    and changed_files
                    and not matches_watch_paths(changed_files, watch_paths)
                ):
                    continue
                print(
                    f"deploying (skip CI) {repository_name}@{commit_sha} to "
                    f"{app_env.application.id} env {app_env.environment.slug}"
                )
                create_deployment(
                    access_token=access_token,
                    application=app_env.application,
                    repository_name=repository_name,
                    ref=commit_sha,
                    app_env=app_env,
                    required_contexts=[],
                )


def process_check_suite_hook(hook):
    installation_id = hook.payload["installation"]["id"]
    repository_name = hook.payload["repository"]["full_name"]
    head_branch = hook.payload["check_suite"]["head_branch"]
    branch_names = [head_branch]
    commit_sha = hook.payload["check_suite"]["head_sha"]
    bearer_token = github_app.bearer_token
    access_token = None

    hook.commit_sha = commit_sha

    # Ignore check suites created by our own app (e.g. from our check runs)
    # to avoid an infinite deploy loop.
    suite_app_id = hook.payload["check_suite"].get("app", {}).get("id")
    if suite_app_id and str(suite_app_id) == str(github_app.app_id):
        return False

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
                    Application.deleted_at.is_(None),
                    ApplicationEnvironment.deleted_at.is_(None),
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

        push_event = (
            Hook.query.filter(Hook.commit_sha == commit_sha)
            .filter(Hook.headers.op("->>")("X-Github-Event") == "push")
            .order_by(Hook.created.desc())
            .first()
        )
        if push_event is None:
            print(
                f"ignoring check_suite without push for {repository_name}@{commit_sha}"
            )
            return False

        if push_event.deployed:
            print(
                "skipping auto-deploy for previously deployed "
                f"{repository_name}@{commit_sha}"
            )
            return False

        # Check that all required status checks have passed before
        # attempting to create deployments. If not, bail out so a later
        # check_suite webhook (when more suites complete) can retry.
        required_contexts = _required_contexts_for_branch(
            access_token, repository_name, head_branch
        )
        if not _all_required_checks_passed(
            access_token, repository_name, commit_sha, required_contexts
        ):
            return False

        # Mark deployed *before* creating deployments to prevent races
        # between concurrent check_suite webhooks for the same SHA.
        push_event.deployed = True
        db.session.commit()

        # Extract changed files from the push event payload to filter
        # apps by watch paths.
        changed_files = set()
        for commit in push_event.payload.get("commits", []):
            changed_files.update(commit.get("added", []))
            changed_files.update(commit.get("modified", []))
            changed_files.update(commit.get("removed", []))

        for app_env in env_matches:
            # Skip apps that already deployed on push (no CI wait).
            if not app_env.auto_deploy_wait_for_ci:
                continue
            watch_paths = app_env.application.branch_deploy_watch_paths
            if (
                watch_paths
                and changed_files
                and not matches_watch_paths(changed_files, watch_paths)
            ):
                print(
                    f"skipping {repository_name}@{commit_sha} for "
                    f"{app_env.application.id} env {app_env.environment.slug}: "
                    f"no changes in watch paths"
                )
                continue
            print(
                f"deploying {repository_name}@{commit_sha} to "
                f"{app_env.application.id} env {app_env.environment.slug}"
            )
            create_deployment(
                access_token=access_token,
                application=app_env.application,
                repository_name=repository_name,
                ref=commit_sha,
                app_env=app_env,
                branch=head_branch,
                required_contexts=required_contexts,
            )


def _base_ref_chains_to_auto_deploy_branch(
    access_token, repository_name, base_ref, auto_deploy_branches, max_depth=10
):
    """Check if base_ref transitively reaches an auto-deploy branch.

    Walks the chain of open pull requests via the GitHub API: if base_ref
    is the head branch of an open PR whose own base branch is an
    auto-deploy branch (or chains to one), returns True.
    """
    owner = repository_name.split("/")[0]
    visited = set()
    to_check = [base_ref]

    for _ in range(max_depth):
        if not to_check:
            return False
        current = to_check.pop(0)
        if current in visited:
            continue
        visited.add(current)

        resp = github_session.get(
            f"https://api.github.com/repos/{repository_name}/pulls",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"token {access_token['token']}",
            },
            params={"state": "open", "head": f"{owner}:{current}"},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(
                "failed to query open PRs for %s head=%s: %s",
                repository_name,
                current,
                resp.status_code,
            )
            return False

        for pr in resp.json():
            pr_base = pr["base"]["ref"]
            if pr_base in auto_deploy_branches:
                return True
            if pr_base not in visited:
                to_check.append(pr_base)

    return False


def process_pull_request_hook(hook: Hook) -> None:
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
    base_ref = pr["base"]["ref"]
    hook.commit_sha = head_sha

    projects: list[Project] = (
        Project.query.join(Application)
        .filter(
            Application.github_app_installation_id == installation_id,
            Application.github_repository == repository_name,
            Application.deleted_at.is_(None),
            Project.branch_deploys_enabled.is_(True),
            Project.branch_deploy_base_environment_id.isnot(None),
        )
        .distinct()
        .all()
    )
    if not projects:
        return

    for project in projects:
        # Only process PRs that target the same branch as an app in the
        # preview base environment is configured to auto-deploy from.
        base_env = project.branch_deploy_base_environment
        base_app_envs = (
            ApplicationEnvironment.query.filter_by(
                environment_id=base_env.id,
            )
            .join(Application)
            .filter(
                Application.github_app_installation_id == installation_id,
                Application.github_repository == repository_name,
                Application.deleted_at.is_(None),
            )
            .all()
        )
        # Teardown doesn't depend on branch validation — always honor
        # closed events so stacked PRs closed out of order get cleaned up.
        if action == "closed":
            teardown_branch_deploy(project, pr_number)
            continue

        auto_deploy_branches = {ae.effective_auto_deploy_branch for ae in base_app_envs}
        if base_ref not in auto_deploy_branches:
            # base_ref is not a direct auto-deploy branch; check if it
            # chains to one via stacked PRs (e.g. frontend -> backend -> main).
            bearer_token = github_app.bearer_token
            access_token_response = github_session.post(
                f"https://api.github.com/app/installations/{installation_id}/access_tokens",
                headers={
                    "Accept": "application/vnd.github.machine-man-preview+json",
                    "Authorization": f"Bearer {bearer_token}",
                },
                timeout=10,
            )
            if "token" not in access_token_response.json():
                logger.warning(
                    "unable to authenticate for stacked PR check on %s",
                    repository_name,
                )
                continue
            access_token = access_token_response.json()
            if not _base_ref_chains_to_auto_deploy_branch(
                access_token,
                repository_name,
                base_ref,
                auto_deploy_branches,
            ):
                logger.info(
                    "skipping project %s: PR base branch %s does not chain to any "
                    "auto_deploy_branch in base environment %s",
                    project.slug,
                    base_ref,
                    base_env.slug,
                )
                continue
        if action in ("opened", "reopened"):
            create_branch_deploy(
                project, pr_number, head_sha, installation_id, head_ref
            )
        elif action == "synchronize":
            sync_branch_deploy(project, pr_number, head_sha, installation_id)


@shared_task()
def process_github_hook(hook_id: UUID):
    hook = Hook.query.filter_by(id=hook_id).first()
    event = hook.headers["X-Github-Event"]
    if event == "deployment":
        commit_sha = hook.payload.get("deployment", {}).get("sha")
        if commit_sha:
            installation_id = str(hook.payload.get("installation", {}).get("id", ""))
            environment = hook.payload.get("deployment", {}).get("environment")
            # Find the earliest hook for this SHA + installation + environment.
            # Only the first one should proceed; later duplicates are skipped.
            first_hook = (
                Hook.query.filter(Hook.headers["X-Github-Event"].astext == "deployment")
                .filter(Hook.payload["deployment"]["sha"].astext == commit_sha)
                .filter(Hook.payload["installation"]["id"].astext == installation_id)
                .filter(Hook.payload["deployment"]["environment"].astext == environment)
                .order_by(Hook.created)
                .first()
            )
            if first_hook and first_hook.id != hook.id:
                hook.processed = True
                db.session.commit()
                return True
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
    if event == "installation_target":
        process_installation_target_hook(hook)
        hook.processed = True
        db.session.commit()
    if event == "repository":
        process_repository_hook(hook)
        hook.processed = True
        db.session.commit()
    if event == "pull_request":
        process_pull_request_hook(hook)
        hook.processed = True
        db.session.commit()
