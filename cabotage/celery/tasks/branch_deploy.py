import datetime
import logging
import re

from flask import current_app
from kubernetes.client.rest import ApiException

from cabotage.server import (
    db,
    github_app,
    kubernetes as kubernetes_ext,
)
from cabotage.server.models.projects import (
    activity_plugin,
    ApplicationEnvironment,
    Configuration,
    Environment,
    Image,
)
from cabotage.server.models.utils import safe_k8s_name
from cabotage.utils.github import find_or_create_pr_comment

logger = logging.getLogger(__name__)

Activity = activity_plugin.activity_cls


def _create_app_env_for_branch_deploy(
    application,
    environment,
    base_environment,
    auto_deploy_branch=None,
):
    """Create ApplicationEnvironment for a branch deploy.

    Configuration objects are copied from the base environment, sharing the same
    Consul/Vault key_slugs. The CabotageEnrollment inheritsFrom grants access
    to the base namespace's secrets.
    """
    base_app_env = ApplicationEnvironment.query.filter_by(
        application_id=application.id,
        environment_id=base_environment.id,
    ).first()

    app_env = ApplicationEnvironment(
        application_id=application.id,
        environment_id=environment.id,
        k8s_identifier=environment.k8s_identifier,
        process_counts=base_app_env.process_counts if base_app_env else {},
        process_pod_classes=base_app_env.process_pod_classes if base_app_env else {},
        auto_deploy_branch=auto_deploy_branch,
    )
    db.session.add(app_env)
    db.session.flush()

    if base_app_env:
        for config in base_app_env.configurations:
            shared_config = Configuration(
                application_id=application.id,
                application_environment_id=app_env.id,
                name=config.name,
                value=config.value,
                secret=config.secret,
                buildtime=config.buildtime,
            )
            shared_config.key_slug = config.key_slug
            shared_config.build_key_slug = config.build_key_slug
            db.session.add(shared_config)
        db.session.flush()

    activity = Activity(
        verb="create",
        object=app_env,
        data={"timestamp": datetime.datetime.utcnow().isoformat()},
    )
    db.session.add(activity)
    return app_env


def _teardown_environment(environment):
    """Delete k8s namespace and all DB records for an ephemeral environment."""
    import kubernetes

    if current_app.config["KUBERNETES_ENABLED"]:
        org = environment.project.organization
        ns_name = safe_k8s_name(org.k8s_identifier, environment.k8s_identifier)
        api_client = kubernetes_ext.kubernetes_client
        core_api = kubernetes.client.CoreV1Api(api_client)
        try:
            core_api.delete_namespace(ns_name, propagation_policy="Foreground")
        except ApiException as exc:
            if exc.status != 404:
                raise

    for app_env in environment.application_environments:
        for config in list(app_env.configurations):
            db.session.delete(config)
        for image in app_env.images.all():
            db.session.delete(image)
        for release in app_env.releases.all():
            db.session.delete(release)
        for deployment in app_env.deployments.all():
            db.session.delete(deployment)
    db.session.flush()
    # Deleting the environment cascades to its application_environments
    db.session.delete(environment)
    db.session.flush()


def _build_images_for_app_envs(app_envs, commit_sha, installation_id):
    """Create Image records and queue builds for a list of ApplicationEnvironments."""
    images = []
    for app_env in app_envs:
        application = app_env.application
        image = Image(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=application.registry_repository_name(app_env),
            image_metadata={
                "sha": commit_sha,
                "installation_id": installation_id,
                "auto_deploy": True,
                "branch_deploy": True,
            },
            build_ref=commit_sha,
        )
        db.session.add(image)
        db.session.flush()
        activity = Activity(
            verb="submit",
            object=image,
            data={"timestamp": datetime.datetime.utcnow().isoformat()},
        )
        db.session.add(activity)
        images.append(image)
    db.session.commit()

    from cabotage.celery.tasks import run_image_build

    for image in images:
        run_image_build.delay(image_id=image.id)


def _app_env_status(app_env):
    """Determine the current status of a service in a branch deploy.

    Returns (emoji, label, log_url_path, updated_at).

    """
    image = app_env.latest_image
    deployment = app_env.latest_deployment

    new_pipeline = image and (not deployment or image.created > deployment.created)

    if new_pipeline:
        if not image.built and not image.error:
            return ("\u23f3", "Building", f"images/{image.id}", image.updated)
        if image.error:
            return ("\u274c", "Build Failed", f"images/{image.id}", image.updated)

        release = app_env.latest_release
        if release and release.created >= image.created:
            if release.error:
                return (
                    "\u274c",
                    "Release Failed",
                    f"releases/{release.id}",
                    release.updated,
                )
            if not release.built:
                return (
                    "\u23f3",
                    "Building Release",
                    f"releases/{release.id}",
                    release.updated,
                )
            return (
                "\u23f3",
                "Awaiting Deploy",
                f"releases/{release.id}",
                release.updated,
            )
        else:
            return (
                "\u23f3",
                "Awaiting Release",
                None,
                image.updated,
            )

    if deployment and deployment.complete:
        return (
            "\u2705",
            "Deployed",
            f"deployments/{deployment.id}",
            deployment.updated,
        )
    if deployment and deployment.error:
        return (
            "\u274c",
            "Deploy Failed",
            f"deployments/{deployment.id}",
            deployment.updated,
        )
    if deployment and not deployment.complete and not deployment.error:
        return (
            "\u23f3",
            "Deploying",
            f"deployments/{deployment.id}",
            deployment.updated,
        )

    return ("\u23f3", "Pending", None, None)


def _preview_url(app_env):
    """Return the https:// URL for the first auto-generated ingress host, or None."""
    for ingress in app_env.ingresses:
        if not ingress.enabled:
            continue
        for host in ingress.hosts:
            if host.is_auto_generated and host.tls_enabled:
                return f"https://{host.hostname}"
    return None


def _render_pr_comment_body(environment):
    """Render the markdown body for a branch deploy PR comment."""
    project = environment.project
    org_slug = project.organization.slug
    project_slug = project.slug

    scheme = current_app.config["EXT_PREFERRED_URL_SCHEME"]
    server = current_app.config["EXT_SERVER_NAME"]
    base_url = f"{scheme}://{server}"

    lines = [
        f"**Branch Deploy** for `{environment.slug}` " f"in **{project.name}**",
        "",
        "| Service | Status | Preview | Updated (UTC) |",
        "| :--- | :--- | :--- | :--- |",
    ]

    for app_env in environment.application_environments:
        app = app_env.application
        emoji, label, log_path, updated_at = _app_env_status(app_env)

        if log_path:
            app_url = f"{base_url}/projects/{org_slug}/{project_slug}/applications/{app.slug}/{log_path}"
            status = f"{emoji} {label} ([View Logs]({app_url}))"
        else:
            status = f"{emoji} {label}"

        preview = ""
        url = _preview_url(app_env)
        if url and label == "Deployed":
            preview = f"[Open]({url})"

        if updated_at:
            ts = updated_at.strftime("%b %-d, %Y at %-I:%M %p")
        else:
            ts = ""

        lines.append(f"| {app.slug} | {status} | {preview} | {ts} |")

    return "\n".join(lines)


def update_pr_comment(environment):
    """Create or update the PR comment for a branch deploy environment."""
    match = re.match(r"pr-(\d+)", environment.slug)
    if not match:
        return

    pr_number = int(match.group(1))
    app_env = next(iter(environment.application_environments), None)
    if not app_env:
        return

    application = app_env.application
    repository_name = application.github_repository
    installation_id = application.github_app_installation_id
    if not repository_name or not installation_id:
        return

    try:
        access_token = github_app.fetch_installation_access_token(installation_id)
        body = _render_pr_comment_body(environment)
        find_or_create_pr_comment(access_token, repository_name, pr_number, body)
    except Exception:
        logger.exception(
            "Failed to update PR comment for %s#%s", repository_name, pr_number
        )


def maybe_update_pr_comment_for_app_env(app_env):
    """Update the PR comment if this app_env belongs to a branch deploy environment."""
    env = app_env.environment
    if env.forked_from_environment_id is not None:
        update_pr_comment(env)


def create_branch_deploy(project, pr_number, head_sha, installation_id, head_ref=None):
    """Create an ephemeral environment for a PR and build images for all enrolled apps."""
    base_env = project.branch_deploy_base_environment
    env_slug = f"pr-{pr_number}"

    existing = Environment.query.filter_by(project_id=project.id, slug=env_slug).first()
    if existing:
        _build_images_for_app_envs(
            existing.application_environments, head_sha, installation_id
        )
        update_pr_comment(existing)
        return

    environment = Environment(
        project_id=project.id,
        name=f"PR #{pr_number}",
        slug=env_slug,
        ephemeral=True,
        forked_from_environment_id=base_env.id,
        sort_order=999,
    )
    db.session.add(environment)
    db.session.flush()

    activity = Activity(
        verb="create",
        object=environment,
        data={"timestamp": datetime.datetime.utcnow().isoformat()},
    )
    db.session.add(activity)

    new_app_envs = []
    for app in project.project_applications:
        base_app_env = ApplicationEnvironment.query.filter_by(
            application_id=app.id,
            environment_id=base_env.id,
        ).first()
        if not base_app_env:
            continue
        app_env = _create_app_env_for_branch_deploy(
            app,
            environment,
            base_env,
            auto_deploy_branch=head_ref,
        )
        new_app_envs.append(app_env)

    db.session.commit()
    _build_images_for_app_envs(new_app_envs, head_sha, installation_id)
    update_pr_comment(environment)


def sync_branch_deploy(project, pr_number, head_sha, installation_id):
    """Build new images for an existing branch deploy environment."""
    env_slug = f"pr-{pr_number}"
    environment = Environment.query.filter_by(
        project_id=project.id, slug=env_slug
    ).first()
    if not environment:
        logger.info(
            "no ephemeral environment %s for project %s, skipping synchronize",
            env_slug,
            project.id,
        )
        return
    _build_images_for_app_envs(
        environment.application_environments, head_sha, installation_id
    )
    update_pr_comment(environment)


def teardown_branch_deploy(project, pr_number):
    """Tear down an ephemeral environment for a closed PR."""
    env_slug = f"pr-{pr_number}"
    environment = Environment.query.filter_by(
        project_id=project.id, slug=env_slug
    ).first()
    if not environment:
        return

    _post_teardown_comment(environment, pr_number)
    _teardown_environment(environment)
    db.session.commit()
    logger.info(
        "torn down ephemeral environment %s for project %s", env_slug, project.id
    )


def _post_teardown_comment(environment, pr_number):
    app_env = next(iter(environment.application_environments), None)
    if not app_env:
        return

    application = app_env.application
    repository_name = application.github_repository
    installation_id = application.github_app_installation_id
    if not repository_name or not installation_id:
        return

    try:
        access_token = github_app.fetch_installation_access_token(installation_id)
        body = (
            f"**Branch Deploy** for `{environment.slug}` "
            f"in **{environment.project.name}**\n\n"
            "Environment has been destroyed."
        )
        find_or_create_pr_comment(access_token, repository_name, pr_number, body)
    except Exception:
        logger.exception(
            "Failed to post teardown comment for %s#%s", repository_name, pr_number
        )
