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
    EnvironmentConfigSubscription,
    EnvironmentConfiguration,
    Image,
    Ingress,
    IngressHost,
    IngressPath,
)
from cabotage.server.models.utils import readable_k8s_hostname, safe_k8s_name
from cabotage.utils.github import find_or_create_pr_comment

logger = logging.getLogger(__name__)

Activity = activity_plugin.activity_cls


def _create_app_env_for_branch_deploy(
    application,
    environment,
    base_environment,
    auto_deploy_branch=None,
    env_config_map=None,
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

        # Copy environment config subscriptions, pointing to the new
        # environment's copies of the EnvironmentConfigurations.
        if env_config_map:
            for sub in base_app_env.environment_config_subscriptions:
                new_config_id = env_config_map.get(sub.environment_configuration_id)
                if new_config_id is not None:
                    db.session.add(
                        EnvironmentConfigSubscription(
                            application_environment_id=app_env.id,
                            environment_configuration_id=new_config_id,
                        )
                    )
            db.session.flush()

        # Copy ingresses with auto-generated hostnames for the new environment
        ingress_domain = current_app.config.get("INGRESS_DOMAIN")
        if ingress_domain:
            from cabotage.celery.tasks.deploy import _ingress_hostname_pairs

            hostname_pairs = _ingress_hostname_pairs(app_env)
            hostname_prefix = readable_k8s_hostname(*hostname_pairs)
            for base_ing in base_app_env.ingresses:
                new_ing = Ingress(
                    application_environment_id=app_env.id,
                    name=base_ing.name,
                    enabled=base_ing.enabled,
                    ingress_class_name=base_ing.ingress_class_name,
                    backend_protocol=base_ing.backend_protocol,
                    proxy_connect_timeout=base_ing.proxy_connect_timeout,
                    proxy_read_timeout=base_ing.proxy_read_timeout,
                    proxy_send_timeout=base_ing.proxy_send_timeout,
                    proxy_body_size=base_ing.proxy_body_size,
                    client_body_buffer_size=base_ing.client_body_buffer_size,
                    proxy_request_buffering=base_ing.proxy_request_buffering,
                    session_affinity=base_ing.session_affinity,
                    use_regex=base_ing.use_regex,
                    cluster_issuer=base_ing.cluster_issuer,
                    force_ssl_redirect=base_ing.force_ssl_redirect,
                    service_upstream=base_ing.service_upstream,
                )
                db.session.add(new_ing)
                db.session.flush()
                # Auto-generated hostname for the preview environment
                auto_hostname = f"{hostname_prefix}-{base_ing.name}.{ingress_domain}"
                db.session.add(
                    IngressHost(
                        ingress_id=new_ing.id,
                        hostname=auto_hostname,
                        tls_enabled=True,
                        is_auto_generated=True,
                    )
                )
                # Copy paths
                for base_path in base_ing.paths:
                    db.session.add(
                        IngressPath(
                            ingress_id=new_ing.id,
                            path=base_path.path,
                            path_type=base_path.path_type,
                            target_process_name=base_path.target_process_name,
                        )
                    )
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

    from cabotage.celery.tasks.build import build_cache_pvc_name

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

        # Clean up build cache PVCs
        for app_env in environment.application_environments:
            pvc_name = build_cache_pvc_name(app_env)
            try:
                core_api.delete_namespaced_persistent_volume_claim(
                    pvc_name, "default", propagation_policy="Foreground"
                )
                logger.info("Deleted build cache PVC %s", pvc_name)
            except ApiException as exc:
                if exc.status != 404:
                    logger.warning(
                        "Failed to delete build cache PVC %s: %s", pvc_name, exc
                    )

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
    from cabotage.celery.tasks.github import (
        create_deployment as create_github_deployment,
    )

    token = github_app.fetch_installation_access_token(installation_id)
    access_token = {"token": token} if token else None

    images = []
    for app_env in app_envs:
        application = app_env.application
        metadata = {
            "sha": commit_sha,
            "installation_id": installation_id,
            "auto_deploy": True,
            "branch_deploy": True,
        }

        if access_token and application.github_repository:
            statuses_url = create_github_deployment(
                access_token=access_token,
                application=application,
                repository_name=application.github_repository,
                ref=commit_sha,
                app_env=app_env,
                transient_environment=True,
            )
            if statuses_url:
                metadata["statuses_url"] = statuses_url

        image = Image(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=application.registry_repository_name(app_env),
            image_metadata=metadata,
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
            return ("\u23f3", "Building Image", f"images/{image.id}", image.updated)
        if image.error:
            return ("\u274c", "Image Build Failed", f"images/{image.id}", image.updated)

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

    # Copy environment-level configurations from the base environment,
    # sharing the same Consul/Vault key_slugs.
    env_config_map = {}  # base config id -> new config id
    for env_config in base_env.environment_configurations:
        if env_config.deleted:
            continue
        new_env_config = EnvironmentConfiguration(
            project_id=project.id,
            environment_id=environment.id,
            name=env_config.name,
            value=env_config.value,
            secret=env_config.secret,
            buildtime=env_config.buildtime,
        )
        new_env_config.key_slug = env_config.key_slug
        new_env_config.build_key_slug = env_config.build_key_slug
        db.session.add(new_env_config)
        db.session.flush()
        env_config_map[env_config.id] = new_env_config.id
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
            env_config_map=env_config_map,
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
