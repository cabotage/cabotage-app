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
from cabotage.utils.github import (
    github_session,
    matches_watch_paths,
    post_deployment_status_update,
)

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


def _required_contexts_for_branch(access_token, repository_name, branch):
    """Fetch required status check contexts for a branch, excluding our own.

    Queries branch protection rules to get the authoritative list of required
    checks, then filters out any belonging to our own GitHub App. This lets
    GitHub's Deployment API enforce real CI gating while ignoring our own
    in-progress check runs that would otherwise cause 409 Conflicts during
    batch deployments.

    Returns a list of context names. Raises on failure so callers do not
    silently proceed without CI gating.
    """
    resp = github_session.get(
        f"https://api.github.com/repos/{repository_name}/branches/{branch}/protection/required_status_checks",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f'token {access_token["token"]}',
        },
        timeout=10,
    )
    print(
        f"required_status_checks for {repository_name} branch {branch}: "
        f"{resp.status_code} {resp.text}"
    )
    if resp.status_code == 404:
        # Branch is not protected or has no required status checks
        return []
    resp.raise_for_status()
    data = resp.json()
    own_app_id = int(github_app.app_id)
    checks = data.get("checks", [])
    if checks:
        return [c["context"] for c in checks if c.get("app_id") != own_app_id]
    # Fall back to legacy contexts list (no app_id available to filter)
    return data.get("contexts", [])


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
                "Authorization": f'token {access_token["token"]}',
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
            "Authorization": f'token {access_token["token"]}',
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
    access_token=None,
    application=None,
    repository_name=None,
    ref=None,
    app_env=None,
    branch=None,
    transient_environment=False,
    environment_name=None,
    payload=None,
    required_contexts=None,
):
    try:
        environment_string = (
            environment_name or app_env.effective_github_environment_name
        )

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
                "Authorization": f'token {access_token["token"]}',
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
    base_ref = pr["base"]["ref"]
    hook.commit_sha = head_sha

    projects = (
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
        if not any(ae.effective_auto_deploy_branch == base_ref for ae in base_app_envs):
            logger.info(
                "skipping project %s: PR base branch %s does not match any "
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
        elif action == "closed":
            teardown_branch_deploy(project, pr_number)


@shared_task()
def process_github_hook(hook_id):
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
    if event == "pull_request":
        process_pull_request_hook(hook)
        hook.processed = True
        db.session.commit()
