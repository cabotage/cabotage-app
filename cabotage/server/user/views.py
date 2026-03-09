import collections
import datetime
import json
import re
import time
import threading
import queue

from flask import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
    flash,
)
from flask_security import (
    current_user,
    login_required,
)

import kubernetes

from dxf import DXF
import requests as requests_lib
from requests.exceptions import HTTPError
from sqlalchemy import desc, func
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy_continuum import version_class

from cabotage.server import (
    config_writer,
    db,
    github_app,
    kubernetes as kubernetes_ext,
    vault,
    sock,
)

from cabotage.server.acl import (
    ViewOrganizationPermission,
    ViewProjectPermission,
    ViewApplicationPermission,
    AdministerOrganizationPermission,
    AdministerProjectPermission,
    AdministerApplicationPermission,
)

from cabotage.server.models.auth import (
    Organization,
    User,
)
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.models.projects import (
    DEFAULT_POD_CLASS,
    Application,
    ApplicationEnvironment,
    Configuration,
    Deployment,
    Environment,
    Hook,
    Image,
    Ingress,
    IngressHost,
    IngressPath,
    Project,
    Release,
    pod_classes,
)
from cabotage.server.models.projects import activity_plugin

from cabotage.server.query_helpers import (
    compute_app_status_sets,
    compute_ae_status_sets,
    compute_process_counts,
    extract_latest_variants,
    RelatedObjectResolver,
    split_image_processes,
)
from cabotage.server.models.utils import safe_k8s_name, readable_k8s_hostname, slugify

from cabotage.server.user.forms import (
    AddApplicationToEnvironmentForm,
    ApplicationScaleForm,
    CreateApplicationForm,
    CreateConfigurationForm,
    CreateEnvironmentForm,
    CreateOrganizationForm,
    CreateProjectForm,
    DeleteConfigurationForm,
    DeleteEnvironmentForm,
    EditApplicationEnvironmentSettingsForm,
    EditApplicationSettingsForm,
    EditConfigurationForm,
    EditEnvironmentForm,
    EditProjectSettingsForm,
    IngressSettingsForm,
    IngressHostForm,
    ReleaseDeployForm,
    AddOrganizationUserForm,
)

from cabotage.utils.docker_auth import (
    check_docker_credentials,
    generate_docker_registry_jwt,
    parse_docker_scope,
    docker_access_intersection,
)

from cabotage.celery.tasks import (
    process_github_hook,
    run_deploy,
    run_image_build,
    run_release_build,
)

from cabotage.celery.tasks.deploy import resize_deployment, scale_deployment
from cabotage.utils.build_log_stream import (
    get_redis_client,
    read_log_stream,
    stream_key,
)

_REGEX_META = re.compile(r"[.*+?{}()|\\^$\[\]]")


def _safe_get(model, pk):
    """Look up a model by primary key, returning None on invalid input."""
    try:
        return model.query.get(pk)
    except DataError:
        db.session.rollback()
        return None


Activity = activity_plugin.activity_cls
user_blueprint = Blueprint(
    "user",
    __name__,
)


def _config_k8s_namespace(organization, app_env):
    if app_env.k8s_identifier is not None:
        return safe_k8s_name(
            organization.k8s_identifier, app_env.environment.k8s_identifier
        )
    return organization.k8s_identifier


def _config_k8s_resource_prefix(project, application):
    return safe_k8s_name(project.k8s_identifier, application.k8s_identifier)


def _associate_app_with_environment(application, environment, organization, project):
    """Create ApplicationEnvironment + sentinel config + activity log."""
    app_env = ApplicationEnvironment(
        application_id=application.id,
        environment_id=environment.id,
        k8s_identifier=environment.k8s_identifier,
    )
    db.session.add(app_env)
    db.session.flush()
    sentinel = Configuration(
        application_id=application.id,
        application_environment_id=app_env.id,
        name="CABOTAGE_SENTINEL",
        value="at least one environment variable must exist",
        secret=False,
        buildtime=False,
    )
    try:
        ns = _config_k8s_namespace(organization, app_env)
        prefix = _config_k8s_resource_prefix(project, application)
        key_slugs = config_writer.write_configuration(ns, prefix, sentinel)
    except Exception:
        raise
    sentinel.key_slug = key_slugs["config_key_slug"]
    sentinel.build_key_slug = key_slugs["build_key_slug"]
    db.session.add(sentinel)
    db.session.flush()
    activity = Activity(
        verb="create",
        object=app_env,
        data={
            "user_id": str(current_user.id),
            "timestamp": datetime.datetime.utcnow().isoformat(),
        },
    )
    db.session.add(activity)
    return app_env


def _lookup_app_context(org_slug, project_slug, app_slug, require_admin=False):
    """Resolve org/project/app from slugs and check permissions."""
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    project = Project.query.filter_by(
        organization_id=organization.id, slug=project_slug
    ).first_or_404()
    application = Application.query.filter_by(
        project_id=project.id, slug=app_slug
    ).first_or_404()
    perm = (
        AdministerApplicationPermission if require_admin else ViewApplicationPermission
    )
    if not perm(application.id).can():
        abort(403)
    return organization, project, application


def _default_environment(project):
    """Return the default environment for breadcrumbs, or None."""
    if not project.environments_enabled:
        return None
    return next(
        (e for e in project.project_environments if e.is_default),
        project.project_environments[0] if project.project_environments else None,
    )


def _resolve_app_env(
    application, environment_id=None, env_slug=None, project=None, required=True
):
    """Resolve an ApplicationEnvironment for the given application.

    For env-enabled projects with an env_slug or environment_id, resolves the
    specific enrollment. Otherwise returns the default (implicit) app_env.

    If required=True (default), aborts with 404 when no app_env is found.
    If required=False, returns None instead.
    """
    if environment_id:
        return ApplicationEnvironment.query.filter_by(
            application_id=application.id,
            environment_id=environment_id,
        ).first_or_404()
    if env_slug and project:
        environment = Environment.query.filter_by(
            project_id=project.id,
            slug=env_slug,
        ).first_or_404()
        return ApplicationEnvironment.query.filter_by(
            application_id=application.id,
            environment_id=environment.id,
        ).first_or_404()
    app_env = application.default_app_env
    if app_env is None and required:
        abort(404)
    return app_env


def _create_bare_app_env(application, environment):
    """Create an ApplicationEnvironment without sentinel config or enrollment.

    Used for non-env projects where k8s_identifier=NULL signals legacy paths.
    """
    app_env = ApplicationEnvironment(
        application_id=application.id,
        environment_id=environment.id,
        k8s_identifier=None,
    )
    db.session.add(app_env)
    db.session.flush()
    return app_env


@user_blueprint.route("/organizations")
@login_required
def organizations():
    from sqlalchemy.orm import joinedload

    memberships = (
        OrganizationMember.query.filter(OrganizationMember.user_id == current_user.id)
        .options(
            joinedload(OrganizationMember.organization)
            .joinedload(Organization.projects)
            .joinedload(Project.project_applications),
            joinedload(OrganizationMember.organization).joinedload(
                Organization.members
            ),
        )
        .all()
    )

    # Pre-compute last deploy timestamp per organization
    org_ids = [m.organization_id for m in memberships]
    last_deploy_by_org = {}
    if org_ids:
        rows = (
            db.session.query(
                Project.organization_id,
                func.max(Deployment.created).label("last_deploy"),
            )
            .join(Application, Application.project_id == Project.id)
            .join(
                ApplicationEnvironment,
                ApplicationEnvironment.application_id == Application.id,
            )
            .join(
                Deployment,
                Deployment.application_environment_id == ApplicationEnvironment.id,
            )
            .filter(
                Project.organization_id.in_(org_ids),
                ApplicationEnvironment.k8s_identifier.is_(None),
                Deployment.complete == True,  # noqa: E712
            )
            .group_by(Project.organization_id)
            .all()
        )
        last_deploy_by_org = {row[0]: row[1] for row in rows}

    org_create_form = CreateOrganizationForm()
    return render_template(
        "user/organizations.html",
        organizations=memberships,
        last_deploy_by_org=last_deploy_by_org,
        org_create_form=org_create_form,
    )


@user_blueprint.route("/organizations/<org_slug>")
@login_required
def organization(org_slug):
    from sqlalchemy.orm import joinedload

    organization = (
        Organization.query.filter_by(slug=org_slug)
        .options(
            joinedload(Organization.projects).joinedload(Project.project_applications),
            joinedload(Organization.members).joinedload(OrganizationMember.user),
        )
        .first_or_404()
    )
    if not ViewOrganizationPermission(organization.id).can():
        abort(403)
    project_create_form = CreateProjectForm()
    project_create_form.organization_id.choices = [
        (str(organization.id), organization.name)
    ]
    project_create_form.organization_id.data = str(organization.id)

    app_ids = [app.id for p in organization.projects for app in p.project_applications]
    org_app_count = len(app_ids)
    org_deploy_count = 0
    last_deploy_ts = None
    deployed_app_ids = set()
    errored_app_ids = set()
    building_app_ids = set()

    if app_ids:
        # Deploy count + last deploy timestamp in one query
        deploy_stats = (
            db.session.query(
                func.count(Deployment.id),
                func.max(Deployment.created),
            )
            .join(ApplicationEnvironment)
            .filter(
                Deployment.application_id.in_(app_ids),
                ApplicationEnvironment.k8s_identifier.is_(None),
                Deployment.complete == True,  # noqa: E712
            )
            .one()
        )
        org_deploy_count = deploy_stats[0]
        last_deploy_ts = deploy_stats[1]

        status = compute_app_status_sets(app_ids)
        deployed_app_ids = status["deployed_app_ids"]
        errored_app_ids = status["errored_app_ids"]
        building_app_ids = status["building_app_ids"]

    return render_template(
        "user/organization.html",
        organization=organization,
        project_create_form=project_create_form,
        org_app_count=org_app_count,
        org_deploy_count=org_deploy_count,
        deployed_app_ids=deployed_app_ids,
        errored_app_ids=errored_app_ids,
        building_app_ids=building_app_ids,
        last_deploy_ts=last_deploy_ts,
    )


@user_blueprint.route("/organizations/create", methods=["GET", "POST"])
@login_required
def organization_create():
    user = current_user
    form = CreateOrganizationForm()

    if form.validate_on_submit():
        organization = Organization(name=form.name.data, slug=form.slug.data)
        organization.add_user(user, admin=True)
        db.session.add(organization)
        db.session.flush()
        org_create = Activity(
            verb="create",
            object=organization,
            data={
                "user_id": str(current_user.id),
                "timestamp": datetime.datetime.utcnow().isoformat(),
            },
        )
        db.session.add(org_create)
        db.session.commit()
        return redirect(url_for("user.organization", org_slug=organization.slug))
    return render_template(
        "user/organization_create.html", organization_create_form=form
    )


@user_blueprint.route("/organizations/<org_slug>/projects")
@login_required
def organization_projects(org_slug):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    if not ViewOrganizationPermission(organization.id).can():
        abort(403)
    return render_template("user/organization_projects.html", organization=organization)


@user_blueprint.route(
    "/organizations/<org_slug>/projects/create", methods=["GET", "POST"]
)
@login_required
def organization_project_create(org_slug):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    if not ViewOrganizationPermission(organization.id).can():
        abort(403)

    form = CreateProjectForm()
    form.organization_id.choices = [(str(organization.id), organization.name)]
    form.organization_id.data = str(organization.id)

    if form.validate_on_submit():
        project = Project(
            organization_id=organization.id,
            name=form.name.data,
            slug=form.slug.data,
            environments_enabled=form.environments_enabled.data,
        )
        db.session.add(project)
        db.session.flush()
        # Create default environment
        env_name = "default"
        if form.environments_enabled.data and form.initial_env_name.data:
            env_name = form.initial_env_name.data
        default_env = Environment(
            project_id=project.id,
            name=env_name,
            slug=form.initial_env_slug.data or slugify(env_name),
            is_default=True,
        )
        db.session.add(default_env)
        db.session.flush()
        activity = Activity(
            verb="create",
            object=project,
            data={
                "user_id": str(current_user.id),
                "timestamp": datetime.datetime.utcnow().isoformat(),
            },
        )
        db.session.add(activity)
        db.session.commit()
        return redirect(
            url_for(
                "user.project",
                org_slug=project.organization.slug,
                project_slug=project.slug,
            )
        )
    return render_template(
        "user/organization_project_create.html",
        organization=organization,
        organization_project_create_form=form,
    )


@user_blueprint.route("/projects")
@login_required
def projects():
    from sqlalchemy.orm import joinedload

    user_projects = (
        Project.query.join(Organization)
        .join(Organization.members)
        .filter(OrganizationMember.user_id == current_user.id)
        .options(
            joinedload(Project.organization),
            joinedload(Project.project_applications),
        )
        .all()
    )

    # Pre-compute app status to avoid per-app queries in the template
    app_ids = [app.id for p in user_projects for app in p.project_applications]
    deployed_app_ids = set()
    errored_app_ids = set()

    if app_ids:
        status = compute_app_status_sets(app_ids)
        deployed_app_ids = status["deployed_app_ids"]
        errored_app_ids = status["errored_app_ids"]

    return render_template(
        "user/projects.html",
        projects=user_projects,
        deployed_app_ids=deployed_app_ids,
        errored_app_ids=errored_app_ids,
    )


@user_blueprint.route("/projects/<org_slug>/<project_slug>")
@login_required
def project(org_slug, project_slug):
    from sqlalchemy.orm import selectinload

    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    project = (
        Project.query.filter_by(organization_id=organization.id, slug=project_slug)
        .options(
            selectinload(Project.project_applications)
            .selectinload(Application.application_environments)
            .selectinload(ApplicationEnvironment.configurations),
            selectinload(Project.project_environments),
        )
        .first_or_404()
    )
    if not ViewProjectPermission(project.id).can():
        abort(403)
    app_create_form = CreateApplicationForm()
    app_create_form.organization_id.choices = [
        (str(organization.id), organization.name)
    ]
    app_create_form.project_id.choices = [(str(project.id), project.name)]
    app_create_form.organization_id.data = str(organization.id)
    app_create_form.project_id.data = str(project.id)
    if project.environments_enabled and len(project.project_environments) > 1:
        sorted_envs = sorted(project.project_environments, key=lambda e: e.sort_order)
        app_create_form.environment_id.choices = [
            (str(e.id), e.name) for e in sorted_envs
        ]
        default_env = next((e for e in sorted_envs if e.is_default), sorted_envs[0])
        app_create_form.environment_id.data = str(default_env.id)
    else:
        app_create_form.environment_id.choices = []

    # Build ae_by_env mapping in Python (avoids duplicate eager-load path)
    ae_ids = []
    ae_by_env = {}
    for app in project.project_applications:
        for ae in app.application_environments:
            ae_ids.append(ae.id)
            ae_by_env.setdefault(ae.environment_id, []).append(ae)

    ae_status = compute_ae_status_sets(ae_ids)
    proj_deploy_count = ae_status["deploy_count"]
    deploying_ae_ids = ae_status["deploying_ae_ids"]
    completed_ae_ids = ae_status["completed_ae_ids"]
    stats_running_ae_ids = ae_status["running_ae_ids"]
    building_ae_ids = ae_status["building_ae_ids"]
    errored_ae_ids = ae_status["errored_ae_ids"]
    last_deploy_by_ae = ae_status["last_deploy_by_ae"]

    unassigned_apps = []
    assigned_app_ids = set()
    env_create_form = None
    has_default = False
    if project.environments_enabled:
        for env_ae_list in ae_by_env.values():
            for ae in env_ae_list:
                assigned_app_ids.add(ae.application_id)
        unassigned_apps = [
            a for a in project.project_applications if a.id not in assigned_app_ids
        ]
        env_create_form = CreateEnvironmentForm()
        env_create_form.project_id.data = str(project.id)
        has_default = any(e.is_default for e in project.project_environments)
    return render_template(
        "user/project.html",
        project=project,
        app_create_form=app_create_form,
        env_create_form=env_create_form,
        has_default=has_default,
        proj_deploy_count=proj_deploy_count,
        unassigned_apps=unassigned_apps,
        deploying_ae_ids=deploying_ae_ids,
        completed_ae_ids=completed_ae_ids,
        stats_running_ae_ids=stats_running_ae_ids,
        building_ae_ids=building_ae_ids,
        errored_ae_ids=errored_ae_ids,
        last_deploy_by_ae=last_deploy_by_ae,
        ae_by_env=ae_by_env,
    )


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/settings", methods=["GET", "POST"]
)
@login_required
def project_settings(org_slug, project_slug):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    project = Project.query.filter_by(
        organization_id=organization.id, slug=project_slug
    ).first_or_404()
    if not AdministerProjectPermission(project.id).can():
        abort(403)

    form = EditProjectSettingsForm(obj=project)
    form.project_id.data = str(project.id)
    form.branch_deploy_base_environment_id.choices = [("", "— select —")] + [
        (str(e.id), e.name) for e in project.project_environments
    ]

    envs = project.project_environments
    non_default_envs = [e for e in envs if not e.is_default]
    can_disable_environments = (
        project.environments_enabled and len(non_default_envs) == 0
    )

    if form.validate_on_submit():
        # A disabled checkbox doesn't submit a value, so the form sees False
        # even though environments are still enabled. Restore the real value
        # when the checkbox was rendered as disabled.
        if project.environments_enabled and not can_disable_environments:
            form.environments_enabled.data = True
        disabling_environments = (
            project.environments_enabled
            and not form.environments_enabled.data
            and can_disable_environments
        )
        if disabling_environments:
            # Delete non-default environments and their enrollments/records.
            # The default env + its app_envs remain (they're implicit).
            for env in non_default_envs:
                for app_env in env.application_environments:
                    for config in list(app_env.configurations):
                        db.session.delete(config)
                    for image in app_env.images.all():
                        db.session.delete(image)
                    for release in app_env.releases.all():
                        db.session.delete(release)
                    for deployment in app_env.deployments.all():
                        db.session.delete(deployment)
                db.session.flush()
                db.session.delete(env)
            db.session.flush()
            # Reset k8s_identifier on default app_envs to NULL (legacy mode)
            default_env = next((e for e in envs if e.is_default), None)
            if default_env:
                for app_env in default_env.application_environments:
                    app_env.k8s_identifier = None
                db.session.flush()
        enabling_environments = (
            not project.environments_enabled and form.environments_enabled.data
        )
        if enabling_environments:
            # Rename the default environment if an initial name was provided.
            initial_name = form.initial_env_name.data
            if initial_name:
                default_env = next(
                    (e for e in project.project_environments if e.is_default),
                    None,
                )
                if default_env:
                    default_env.name = initial_name
                    default_env.slug = form.initial_env_slug.data or slugify(
                        initial_name
                    )
            # Existing app_envs keep k8s_identifier=NULL so all their paths
            # (registry, namespace, consul/vault, build cache) remain unchanged.
            # Copy app-level github_environment_name to each app's default app_env
            for app in project.project_applications:
                if app.github_environment_name is not None:
                    default_ae = app.default_app_env
                    if default_ae and default_ae.github_environment_name is None:
                        default_ae.github_environment_name = app.github_environment_name
        # Branch deploy validation
        if form.branch_deploys_enabled.data:
            if not form.environments_enabled.data:
                flash(
                    "Branch deploys require environments to be enabled.",
                    "error",
                )
                return redirect(
                    url_for(
                        "user.project_settings",
                        org_slug=organization.slug,
                        project_slug=project.slug,
                    )
                )
            if not form.branch_deploy_base_environment_id.data:
                flash(
                    "A base environment must be selected for branch deploys.",
                    "error",
                )
                return redirect(
                    url_for(
                        "user.project_settings",
                        org_slug=organization.slug,
                        project_slug=project.slug,
                    )
                )
        if disabling_environments:
            form.branch_deploys_enabled.data = False
            form.branch_deploy_base_environment_id.data = ""
        if not form.branch_deploys_enabled.data:
            form.branch_deploy_base_environment_id.data = ""

        # Convert empty string to None for the UUID FK
        if not form.branch_deploy_base_environment_id.data:
            form.branch_deploy_base_environment_id.data = None

        form.populate_obj(project)
        env_order = request.form.getlist("env_order")
        if env_order:
            env_map = {str(e.id): e for e in project.project_environments}
            for i, eid in enumerate(env_order):
                if eid in env_map:
                    env_map[eid].sort_order = i
        default_env_id = request.form.get("default_environment_id")
        if default_env_id:
            for env in project.project_environments:
                env.is_default = str(env.id) == default_env_id
        db.session.flush()
        activity = Activity(
            verb="edit",
            object=project,
            data={
                "user_id": str(current_user.id),
                "timestamp": datetime.datetime.utcnow().isoformat(),
            },
        )
        db.session.add(activity)
        db.session.commit()
        return redirect(
            url_for(
                "user.project_settings",
                org_slug=organization.slug,
                project_slug=project.slug,
            )
        )

    return render_template(
        "user/project_settings.html",
        project=project,
        form=form,
        can_disable_environments=can_disable_environments,
    )


@user_blueprint.route("/projects/create", methods=["GET", "POST"])
@login_required
def project_create():
    user = current_user
    form = CreateProjectForm()
    form.organization_id.choices = [
        (str(o.organization_id), o.organization.name) for o in user.organizations
    ]

    if form.validate_on_submit():
        organization = Organization.query.filter_by(
            id=form.organization_id.data
        ).first_or_404()
        if not AdministerOrganizationPermission(organization.id).can():
            abort(403)
        project = Project(
            organization_id=form.organization_id.data,
            name=form.name.data,
            slug=form.slug.data,
            environments_enabled=form.environments_enabled.data,
        )
        db.session.add(project)
        db.session.flush()
        env_name = "default"
        if form.environments_enabled.data and form.initial_env_name.data:
            env_name = form.initial_env_name.data
        default_env = Environment(
            project_id=project.id,
            name=env_name,
            slug=form.initial_env_slug.data or slugify(env_name),
            is_default=True,
        )
        db.session.add(default_env)
        db.session.flush()
        activity = Activity(
            verb="create",
            object=project,
            data={
                "user_id": str(current_user.id),
                "timestamp": datetime.datetime.utcnow().isoformat(),
            },
        )
        db.session.add(activity)
        db.session.commit()
        return redirect(
            url_for(
                "user.project",
                org_slug=project.organization.slug,
                project_slug=project.slug,
            )
        )
    return render_template("user/project_create.html", project_create_form=form)


@user_blueprint.route("/projects/<org_slug>/<project_slug>/environments")
@login_required
def project_environments(org_slug, project_slug):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    project = Project.query.filter_by(
        organization_id=organization.id, slug=project_slug
    ).first_or_404()
    if not ViewProjectPermission(project.id).can():
        abort(403)
    env_create_form = CreateEnvironmentForm()
    env_create_form.project_id.data = str(project.id)
    has_default = any(e.is_default for e in project.project_environments)
    return render_template(
        "user/project_environments.html",
        project=project,
        env_create_form=env_create_form,
        has_default=has_default,
    )


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/environments/create",
    methods=["GET", "POST"],
)
@login_required
def project_environment_create(org_slug, project_slug):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    project = Project.query.filter_by(
        organization_id=organization.id, slug=project_slug
    ).first_or_404()
    if not AdministerProjectPermission(project.id).can():
        abort(403)
    form = CreateEnvironmentForm()
    form.project_id.data = str(project.id)
    has_default = any(e.is_default for e in project.project_environments)
    if form.validate_on_submit():
        slug = form.slug.data or slugify(form.name.data)
        if form.is_default.data:
            for env in project.project_environments:
                env.is_default = False
        environment = Environment(
            project_id=project.id,
            name=form.name.data,
            slug=slug,
            is_default=form.is_default.data,
        )
        db.session.add(environment)
        db.session.flush()
        activity = Activity(
            verb="create",
            object=environment,
            data={
                "user_id": str(current_user.id),
                "timestamp": datetime.datetime.utcnow().isoformat(),
            },
        )
        db.session.add(activity)
        db.session.commit()
        return redirect(
            url_for(
                "user.project_environment",
                org_slug=org_slug,
                project_slug=project_slug,
                env_slug=environment.slug,
            )
        )
    return render_template(
        "user/project_environment_create.html",
        project=project,
        form=form,
        has_default=has_default,
    )


@user_blueprint.route("/projects/<org_slug>/<project_slug>/environments/<env_slug>")
@login_required
def project_environment(org_slug, project_slug, env_slug):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    project = Project.query.filter_by(
        organization_id=organization.id, slug=project_slug
    ).first_or_404()
    if not ViewProjectPermission(project.id).can():
        abort(403)
    environment = Environment.query.filter_by(
        project_id=project.id, slug=env_slug
    ).first_or_404()
    add_app_form = AddApplicationToEnvironmentForm()
    add_app_form.environment_id.data = str(environment.id)
    # Only show applications not already in this environment
    existing_app_ids = {
        ae.application_id for ae in environment.application_environments
    }
    available_apps = [
        a for a in project.project_applications if a.id not in existing_app_ids
    ]
    add_app_form.application_id.choices = [(str(a.id), a.name) for a in available_apps]
    return render_template(
        "user/project_environment.html",
        project=project,
        environment=environment,
        add_app_form=add_app_form,
        available_apps=available_apps,
    )


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/environments/<env_slug>/settings",
    methods=["GET", "POST"],
)
@login_required
def project_environment_settings(org_slug, project_slug, env_slug):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    project = Project.query.filter_by(
        organization_id=organization.id, slug=project_slug
    ).first_or_404()
    if not AdministerProjectPermission(project.id).can():
        abort(403)
    environment = Environment.query.filter_by(
        project_id=project.id, slug=env_slug
    ).first_or_404()
    form = EditEnvironmentForm(obj=environment)
    form.environment_id.data = str(environment.id)
    if form.validate_on_submit():
        environment.name = form.name.data
        db.session.add(environment)
        db.session.commit()
        return redirect(
            url_for(
                "user.project_environment",
                org_slug=org_slug,
                project_slug=project_slug,
                env_slug=environment.slug,
            )
        )
    delete_form = DeleteEnvironmentForm()
    delete_form.environment_id.data = str(environment.id)
    delete_form.name.data = environment.name
    return render_template(
        "user/project_environment_edit.html",
        project=project,
        environment=environment,
        form=form,
        delete_form=delete_form,
    )


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/environments/<env_slug>/delete",
    methods=["POST"],
)
@login_required
def project_environment_delete(org_slug, project_slug, env_slug):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    project = Project.query.filter_by(
        organization_id=organization.id, slug=project_slug
    ).first_or_404()
    if not AdministerProjectPermission(project.id).can():
        abort(403)
    environment = Environment.query.filter_by(
        project_id=project.id, slug=env_slug
    ).first_or_404()
    form = DeleteEnvironmentForm()
    if environment.is_default:
        flash("The default environment cannot be deleted.", "error")
        return redirect(
            url_for(
                "user.project_environment_settings",
                org_slug=org_slug,
                project_slug=project_slug,
                env_slug=env_slug,
            )
        )
    if form.validate_on_submit():
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
        db.session.delete(environment)
        db.session.commit()
        flash(f"Environment {environment.name} deleted.", "success")
        return redirect(
            url_for(
                "user.project_environments",
                org_slug=org_slug,
                project_slug=project_slug,
            )
        )
    abort(400)


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/environments/<env_slug>/add_application",
    methods=["POST"],
)
@login_required
def project_environment_add_application(org_slug, project_slug, env_slug):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    project = Project.query.filter_by(
        organization_id=organization.id, slug=project_slug
    ).first_or_404()
    if not AdministerProjectPermission(project.id).can():
        abort(403)
    environment = Environment.query.filter_by(
        project_id=project.id, slug=env_slug
    ).first_or_404()
    form = AddApplicationToEnvironmentForm()
    form.application_id.choices = [
        (str(a.id), a.name) for a in project.project_applications
    ]
    if form.validate_on_submit():
        application = Application.query.filter_by(
            id=form.application_id.data
        ).first_or_404()
        _associate_app_with_environment(application, environment, organization, project)
        db.session.commit()
        return redirect(
            url_for(
                "user.project_environment",
                org_slug=org_slug,
                project_slug=project_slug,
                env_slug=env_slug,
            )
        )
    abort(400)


@user_blueprint.route("/projects/<org_slug>/<project_slug>/applications/<app_slug>")
@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/env/<env_slug>/applications/<app_slug>"
)
@login_required
def project_application(org_slug, project_slug, app_slug, env_slug=None):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    project = Project.query.filter_by(
        organization_id=organization.id, slug=project_slug
    ).first_or_404()
    application = Application.query.filter_by(
        project_id=project.id, slug=app_slug
    ).first_or_404()
    if not ViewApplicationPermission(application.id).can():
        abort(403)

    environment = None
    environments = []
    if project.environments_enabled:
        # Only show environments this application is enrolled in
        all_app_envs = ApplicationEnvironment.query.filter_by(
            application_id=application.id,
        ).all()
        enrolled_env_ids = {ae.environment_id for ae in all_app_envs}
        environments = sorted(
            [e for e in project.project_environments if e.id in enrolled_env_ids],
            key=lambda e: e.sort_order,
        )
        if env_slug:
            environment = Environment.query.filter_by(
                project_id=project.id, slug=env_slug
            ).first_or_404()
        else:
            environment = next(
                (e for e in environments if e.is_default),
                environments[0] if environments else None,
            )
            if environment:
                return redirect(
                    url_for(
                        "user.project_application",
                        org_slug=org_slug,
                        project_slug=project_slug,
                        app_slug=app_slug,
                        env_slug=environment.slug,
                    )
                )

    # Resolve app_env (may be None for unenrolled apps in env-enabled projects)
    app_env = _resolve_app_env(
        application,
        env_slug=env_slug if env_slug else None,
        project=project,
        required=False,
    )

    pod_class_info = (
        '<table class="table"><tr><th>Class</th><th>CPU</th><th>Mem</th></tr>'
    )
    for pod_class, parameters in pod_classes.items():
        pod_class_info += (
            f'<tr><td>{pod_class}</td><td>{parameters["cpu"]["requests"]}</td>'
            f'<td>{parameters["memory"]["requests"]}</td></tr>'
        )
    pod_class_info += "</table>"

    scale_form = ApplicationScaleForm()
    scale_form.application_id.data = str(application.id)

    config_create_form = CreateConfigurationForm()
    config_create_form.application_id.data = str(application.id)
    if environment:
        config_create_form.environment_id.data = str(environment.id)

    sibling_references = []
    if app_env:
        for ae in app_env.environment.application_environments:
            if ae.application_id == application.id:
                continue
            ingress_list = []
            for ing in ae.ingresses:
                if not ing.enabled:
                    continue
                hosts = ing.hosts
                non_auto = [h for h in hosts if not h.is_auto_generated]
                host = non_auto[0] if non_auto else (hosts[0] if hosts else None)
                if host:
                    ingress_list.append(
                        {
                            "name": ing.name,
                            "hostname": host.hostname,
                            "tls": host.tls_enabled,
                        }
                    )
            if ingress_list:
                sibling_references.append(
                    {
                        "slug": ae.application.slug,
                        "ingresses": ingress_list,
                    }
                )

    # Pre-fetch all data from dynamic relationships once to avoid
    # repeated per-access queries in the template (~50+ calls to
    # src.latest_* each triggering a fresh DB query).
    latest_image = None
    latest_image_built = None
    latest_image_error = None
    latest_image_building = None
    latest_release = None
    latest_release_built = None
    latest_release_building = None
    latest_deployment = None
    latest_deployment_completed = None
    has_releases = False
    image_diff = None
    config_diff = None
    releases = []
    images = []
    deployments = []

    # Pre-resolved related objects (avoid cascading queries for
    # deployment.release_object → release.image_object → image.processes)
    deployed_release = None
    deployed_image = None
    latest_deploy_release = None
    latest_release_image = None
    latest_release_processes = {}
    latest_release_release_commands = {}
    release_by_id = {}
    image_by_id = {}
    release_proc_counts = {}

    if app_env is not None:
        # 3 queries: fetch recent items, extract latest_* variants in Python
        all_images = app_env.images.order_by(Image.version.desc()).limit(50).all()
        all_releases = app_env.releases.order_by(Release.version.desc()).limit(50).all()
        all_deployments = (
            app_env.deployments.order_by(Deployment.created.desc()).limit(50).all()
        )

        images = all_images[:10]
        releases = all_releases[:10]
        deployments = all_deployments[:10]

        # Extract latest_* variants from pre-fetched lists
        variants = extract_latest_variants(all_images, all_releases, all_deployments)
        latest_image = variants["latest_image"]
        latest_image_built = variants["latest_image_built"]
        latest_image_error = variants["latest_image_error"]
        latest_image_building = variants["latest_image_building"]
        latest_release = variants["latest_release"]
        latest_release_built = variants["latest_release_built"]
        latest_release_building = variants["latest_release_building"]
        latest_deployment = variants["latest_deployment"]
        latest_deployment_completed = variants["latest_deployment_completed"]
        has_releases = variants["has_releases"]

        # Pre-resolve Release/Image objects referenced by JSONB foreign keys
        resolver = RelatedObjectResolver(images=all_images, releases=all_releases)
        deployed_release = resolver.get_release(latest_deployment_completed)
        deployed_image = resolver.get_image_for_release(deployed_release)
        latest_deploy_release = resolver.get_release(latest_deployment)
        latest_release_image = resolver.get_image_for_release(latest_release)

        latest_release_processes, latest_release_release_commands, _ = (
            split_image_processes(latest_release_image)
        )

        # Batch-warm caches for template loops
        resolver.warm_caches(all_deployments, all_releases)
        release_by_id, image_by_id = resolver.build_lookup_dicts()
        release_proc_counts = compute_process_counts(releases, resolver)

        # Compute ready_for_deployment diffs inline (avoids re-querying
        # latest_release and latest_image_built inside the model method)
        from cabotage.server.models.projects import DictDiffer

        current = latest_release.asdict if latest_release else {}
        candidate = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            image=(latest_image_built.asdict if latest_image_built else {}),
            configuration={c.name: c.asdict for c in app_env.configurations},
            platform=application.platform,
        ).asdict
        image_diff = DictDiffer(
            candidate.get("image", {}),
            current.get("image", {}),
            ignored_keys=["id", "version_id"],
        )
        config_diff = DictDiffer(
            candidate.get("configuration", {}),
            current.get("configuration", {}),
            ignored_keys=["id", "version_id"],
        )

    return render_template(
        "user/project_application.html",
        application=application,
        app_env=app_env,
        environment=environment,
        environments=environments,
        deploy_form=ReleaseDeployForm(),
        scale_form=scale_form,
        view_releases=version_class(Release)
        .query.filter_by(application_id=application.id)
        .order_by(desc(version_class(Release).version_id))
        .limit(5),
        config_create_form=config_create_form,
        sibling_references=sibling_references,
        releases=releases,
        images=images,
        deployments=deployments,
        DEFAULT_POD_CLASS=DEFAULT_POD_CLASS,
        pod_classes=pod_classes,
        pod_class_info=pod_class_info,
        # Pre-fetched latest_* values (avoid per-access queries)
        latest_image=latest_image,
        latest_image_built=latest_image_built,
        latest_image_error=latest_image_error,
        latest_image_building=latest_image_building,
        latest_release=latest_release,
        latest_release_built=latest_release_built,
        latest_release_building=latest_release_building,
        latest_deployment=latest_deployment,
        latest_deployment_completed=latest_deployment_completed,
        has_releases=has_releases,
        image_diff=image_diff,
        config_diff=config_diff,
        deployed_release=deployed_release,
        deployed_image=deployed_image,
        latest_deploy_release=latest_deploy_release,
        latest_release_image=latest_release_image,
        latest_release_processes=latest_release_processes,
        latest_release_release_commands=latest_release_release_commands,
        release_by_id=release_by_id,
        image_by_id=image_by_id,
        release_proc_counts=release_proc_counts,
    )


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/logs"
)
@login_required
def project_application_logs(org_slug, project_slug, app_slug):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    project = Project.query.filter_by(
        organization_id=organization.id, slug=project_slug
    ).first_or_404()
    application = Application.query.filter_by(
        project_id=project.id, slug=app_slug
    ).first_or_404()
    if not ViewApplicationPermission(application.id).can():
        abort(403)

    return render_template(
        "user/project_application_logs.html",
        application=application,
        org_slug=org_slug,
        project_slug=project_slug,
        app_slug=app_slug,
        environment=_default_environment(project),
    )


@sock.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/logs/live",
    bp=user_blueprint,
)
@login_required
def project_application_livelogs(ws, org_slug, project_slug, app_slug):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    project = Project.query.filter_by(
        organization_id=organization.id, slug=project_slug
    ).first_or_404()
    application = Application.query.filter_by(
        project_id=project.id, slug=app_slug
    ).first_or_404()
    if not ViewApplicationPermission(application.id).can():
        abort(403)

    org_slug = organization.slug
    project_slug = project.slug
    app_slug = application.slug

    api_client = kubernetes_ext.kubernetes_client
    core_api_instance = kubernetes.client.CoreV1Api(api_client)

    labels = {
        "organization": org_slug,
        "project": project_slug,
        "application": app_slug,
    }
    label_selector = ",".join([f"{k}={v}" for k, v in labels.items()])

    db.session.remove()

    q = queue.Queue()
    pod_name_prefix = f"{project_slug}-{app_slug}-"

    def worker(pod_name, stream_handler):
        for line in stream_handler:
            q.put(f"{pod_name}: {line}")

    def update_pods():
        worker_threads = {}
        pod_watch = kubernetes.watch.Watch()
        for response in pod_watch.stream(
            core_api_instance.list_namespaced_pod,
            namespace=org_slug,
            label_selector=label_selector,
        ):
            pod = response["object"]
            create = (
                response["type"] == "ADDED" and pod.status.phase == "Running"
            ) or (
                response["type"] == "MODIFIED"
                and pod.status.phase == "Running"
                and pod.metadata.name not in worker_threads.keys()
            )
            if create:
                w = kubernetes.watch.Watch()
                stream_handler = w.stream(
                    core_api_instance.read_namespaced_pod_log,
                    name=pod.metadata.name,
                    namespace=pod.metadata.namespace,
                    container=pod.metadata.labels["process"],
                    follow=True,
                    _preload_content=False,
                    pretty="true",
                    timestamps=True,
                    tail_lines=10,
                )
                thread = threading.Thread(
                    target=worker,
                    args=(
                        pod.metadata.name.removeprefix(pod_name_prefix),
                        stream_handler,
                    ),
                    daemon=True,
                )
                worker_threads[pod.metadata.name] = thread
                q.put(f"started following {pod.metadata.name}...")
                thread.start()
            if response["type"] == "DELETED":
                if (
                    pod.metadata.name in worker_threads.keys()
                    and not worker_threads[pod.metadata.name].is_alive()
                ):
                    worker_threads[pod.metadata.name].join()
                    q.put(f"{pod.metadata.name} terminated...")
                    del worker_threads[pod.metadata.name]

    update_thread = threading.Thread(target=update_pods, daemon=True)
    update_thread.start()

    while True:
        ws.send(q.get())


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/shell"
)
@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/env/<env_slug>/applications/<app_slug>/shell"
)
@login_required
def project_application_shell(org_slug, project_slug, app_slug, env_slug=None):
    if not current_app.config.get("SHELLZ_ENABLED", False):
        abort(404)
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    project = Project.query.filter_by(
        organization_id=organization.id, slug=project_slug
    ).first_or_404()
    application = Application.query.filter_by(
        project_id=project.id, slug=app_slug
    ).first_or_404()
    if not AdministerApplicationPermission(application.id).can():
        abort(403)

    app_env = _resolve_app_env(application, env_slug=env_slug, project=project)
    environment = app_env.environment if app_env else _default_environment(project)

    # =============================================================================== #
    #  this should be removed when we start a shell pod instead of attaching          #
    # =============================================================================== #
    try:
        [
            k
            for k, v in app_env.process_counts.items()
            if (k.startswith("web") or k.startswith("worker")) and v > 0
        ][0]
    except IndexError:
        abort(404)

    return render_template(
        "user/project_application_shell.html",
        application=application,
        org_slug=org_slug,
        project_slug=project_slug,
        app_slug=app_slug,
        env_slug=env_slug,
        environment=environment,
    )


def _shell_socket(ws, org_slug, project_slug, app_slug, env_slug=None):
    if not current_app.config.get("SHELLZ_ENABLED", False):
        abort(404)
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    project = Project.query.filter_by(
        organization_id=organization.id, slug=project_slug
    ).first_or_404()
    application = Application.query.filter_by(
        project_id=project.id, slug=app_slug
    ).first_or_404()
    if not AdministerApplicationPermission(application.id).can():
        abort(403)

    app_env = _resolve_app_env(application, env_slug=env_slug, project=project)
    namespace = _config_k8s_namespace(organization, app_env)
    process_counts = dict(app_env.process_counts)
    db.session.remove()

    api_client = kubernetes_ext.kubernetes_client
    core_api_instance = kubernetes.client.CoreV1Api(api_client)

    # =============================================================================== #
    #  everything below should be replaced with the creation/monitoring of a new pod  #
    # =============================================================================== #
    try:
        process_name = [
            k
            for k, v in process_counts.items()
            if (k.startswith("web") or k.startswith("worker")) and v > 0
        ][0]
    except IndexError:
        abort(404)
    labels = {
        "organization": org_slug,
        "project": project_slug,
        "application": app_slug,
        "process": process_name,
    }
    if env_slug:
        labels["environment"] = env_slug
    label_selector = ",".join([f"{k}={v}" for k, v in labels.items()])
    pods = core_api_instance.list_namespaced_pod(
        namespace=namespace, label_selector=label_selector
    ).items
    if not pods:
        abort(404)
    pod = pods[0]

    # =============================================================================== #

    resp = kubernetes.stream.stream(
        core_api_instance.connect_get_namespaced_pod_exec,
        pod.metadata.name,
        namespace=pod.metadata.namespace,
        command=[
            "/bin/sh",
            "-c",
            (
                "export CONSUL_TOKEN=$(cat /var/run/secrets/vault/consul-token) && "
                "export VAULT_TOKEN=$(cat /var/run/secrets/vault/vault-token) && "
                "envconsul -config /etc/cabotage/envconsul-shell.hcl /bin/sh"
            ),
        ],
        container=process_name,
        stderr=True,
        stdin=True,
        stdout=True,
        tty=True,
        _preload_content=False,
    )

    last_ping = time.monotonic()
    while resp.is_open():
        resp.update()
        now = time.monotonic()
        if now - last_ping >= 25:
            try:
                resp.sock.ping()
            except Exception:
                break
            last_ping = now
        if data := ws.receive(timeout=0.01):
            if data[0] == "\x00":
                resp.write_stdin(data[1:])
            elif data[0] == "\x01":
                resp.write_channel(kubernetes.stream.ws_client.RESIZE_CHANNEL, data[1:])
        if data := resp.read_stdout(timeout=0.01):
            ws.send("\x00" + data)
        if data := resp.read_stderr(timeout=0.01):
            ws.send("\x00" + data)

    resp.close()
    ws.close()


@sock.route(
    "/projects/<org_slug>/<project_slug>/env/<env_slug>/applications/<app_slug>/shell/socket",
    bp=user_blueprint,
)
@login_required
def project_application_shell_socket_env(
    ws, org_slug, project_slug, app_slug, env_slug
):
    return _shell_socket(ws, org_slug, project_slug, app_slug, env_slug=env_slug)


@sock.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/shell/socket",
    bp=user_blueprint,
)
@login_required
def project_application_shell_socket(ws, org_slug, project_slug, app_slug):
    return _shell_socket(ws, org_slug, project_slug, app_slug)


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/create", methods=["GET", "POST"]
)
@login_required
def project_application_create(org_slug, project_slug):
    form = CreateApplicationForm()
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    project = Project.query.filter_by(
        organization_id=organization.id, slug=project_slug
    ).first_or_404()
    if not AdministerProjectPermission(project.id).can():
        abort(403)

    form.organization_id.choices = [(str(organization.id), organization.name)]
    form.project_id.choices = [(str(project.id), project.name)]
    form.organization_id.data = str(organization.id)
    form.project_id.data = str(project.id)
    if project.environments_enabled and len(project.project_environments) > 1:
        sorted_envs = sorted(project.project_environments, key=lambda e: e.sort_order)
        form.environment_id.choices = [(str(e.id), e.name) for e in sorted_envs]
        if not form.is_submitted():
            default_env = next((e for e in sorted_envs if e.is_default), sorted_envs[0])
            form.environment_id.data = str(default_env.id)
    else:
        form.environment_id.choices = []

    if form.validate_on_submit():
        application = Application(
            project_id=form.project_id.data, name=form.name.data, slug=form.slug.data
        )
        db.session.add(application)
        db.session.flush()

        if project.environments_enabled:
            target_env = None
            if form.environment_id.data:
                target_env = Environment.query.filter_by(
                    id=form.environment_id.data, project_id=project.id
                ).first()
            if not target_env:
                target_env = next(
                    (e for e in project.project_environments if e.is_default),
                    None,
                )
            if target_env:
                _associate_app_with_environment(
                    application, target_env, organization, project
                )
        else:
            # Non-env project: lazily create default environment if needed,
            # then create bare app_env + sentinel config.
            default_env = next(
                (e for e in project.project_environments if e.is_default),
                None,
            )
            if default_env is None:
                default_env = Environment(
                    project_id=project.id,
                    name="default",
                    is_default=True,
                )
                db.session.add(default_env)
                db.session.flush()
            app_env = _create_bare_app_env(application, default_env)
            configuration = Configuration(
                application_id=application.id,
                application_environment_id=app_env.id,
                name="CABOTAGE_SENTINEL",
                value="at least one environment variable must exist",
                secret=False,
                buildtime=False,
            )
            try:
                ns = _config_k8s_namespace(organization, app_env)
                prefix = _config_k8s_resource_prefix(project, application)
                key_slugs = config_writer.write_configuration(ns, prefix, configuration)
            except Exception:
                raise  # No, we should def not do this
            configuration.key_slug = key_slugs["config_key_slug"]
            configuration.build_key_slug = key_slugs["build_key_slug"]
            db.session.add(configuration)
            db.session.flush()
        activity = Activity(
            verb="create",
            object=application,
            data={
                "user_id": str(current_user.id),
                "timestamp": datetime.datetime.utcnow().isoformat(),
            },
        )
        db.session.add(activity)
        db.session.commit()
        return redirect(
            url_for(
                "user.project_application",
                org_slug=project.organization.slug,
                project_slug=project.slug,
                app_slug=application.slug,
            )
        )
    return render_template(
        "user/project_application_create.html",
        project_application_create_form=form,
        org_slug=org_slug,
        project_slug=project_slug,
    )


@user_blueprint.route("/projects/<org_slug>/<project_slug>/applications")
@login_required
def project_applications(org_slug, project_slug):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    project = Project.query.filter_by(
        organization_id=organization.id, slug=project_slug
    ).first_or_404()
    if not ViewProjectPermission(project.id).can():
        abort(403)
    return render_template("user/project_applications.html", project=project)


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/config/<config_id>"
)
@login_required
def project_application_configuration(org_slug, project_slug, app_slug, config_id):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    project = Project.query.filter_by(
        organization_id=organization.id, slug=project_slug
    ).first_or_404()
    application = Application.query.filter_by(
        project_id=project.id, slug=app_slug
    ).first_or_404()
    configuration = Configuration.query.filter_by(
        application_id=application.id, id=config_id
    ).first_or_404()
    if not AdministerApplicationPermission(application.id).can():
        abort(403)

    return render_template(
        "user/project_application_configuration.html",
        configuration=configuration,
        environment=_default_environment(project),
    )


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/config/create",
    methods=["GET", "POST"],
)
@login_required
def project_application_configuration_create(org_slug, project_slug, app_slug):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    project = Project.query.filter_by(
        organization_id=organization.id, slug=project_slug
    ).first_or_404()
    application = Application.query.filter_by(
        project_id=project.id, slug=app_slug
    ).first_or_404()
    if not AdministerApplicationPermission(application.id).can():
        abort(403)

    environment_id = request.form.get("environment_id")
    app_env = _resolve_app_env(application, environment_id=environment_id)

    form = CreateConfigurationForm()
    form.application_id.data = str(application.id)
    form.environment_id.data = environment_id or ""

    if form.validate_on_submit():
        from cabotage.utils.config_templates import has_template_variables

        is_template = has_template_variables(form.value.data)
        if is_template and form.secure.data:
            flash("Template configs cannot be secrets.", "error")
            return render_template(
                "user/project_application_configuration_create.html",
                form=form,
                org_slug=organization.slug,
                project_slug=project.slug,
                app_slug=application.slug,
            )

        configuration = Configuration(
            application_id=form.application_id.data,
            application_environment_id=app_env.id,
            name=form.name.data,
            value=form.value.data,
            secret=form.secure.data,
            buildtime=form.buildtime.data,
        )
        if not is_template:
            try:
                ns = _config_k8s_namespace(organization, app_env)
                prefix = _config_k8s_resource_prefix(project, application)
                key_slugs = config_writer.write_configuration(ns, prefix, configuration)
            except Exception:
                raise  # No, we should def not do this
            configuration.key_slug = key_slugs["config_key_slug"]
            configuration.build_key_slug = key_slugs["build_key_slug"]
            if configuration.secret:
                configuration.value = "**secure**"
        else:
            flash(
                "Template config saved — will resolve at deploy time.",
                "info",
            )
        db.session.add(configuration)
        db.session.flush()
        activity = Activity(
            verb="create",
            object=configuration,
            data={
                "user_id": str(current_user.id),
                "timestamp": datetime.datetime.utcnow().isoformat(),
            },
        )
        db.session.add(activity)
        db.session.commit()
        return redirect(
            url_for(
                "user.project_application",
                org_slug=organization.slug,
                project_slug=project.slug,
                app_slug=application.slug,
                env_slug=(
                    app_env.environment.slug if project.environments_enabled else None
                ),
                _anchor="config",
            )
        )
    return render_template(
        "user/project_application_configuration_create.html",
        form=form,
        org_slug=organization.slug,
        project_slug=project.slug,
        app_slug=application.slug,
    )


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/config/<config_id>/edit",
    methods=["GET", "POST"],
)
@login_required
def project_application_configuration_edit(org_slug, project_slug, app_slug, config_id):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    project = Project.query.filter_by(
        organization_id=organization.id, slug=project_slug
    ).first_or_404()
    application = Application.query.filter_by(
        project_id=project.id, slug=app_slug
    ).first_or_404()
    configuration = Configuration.query.filter_by(
        application_id=application.id, id=config_id
    ).first_or_404()
    if not AdministerApplicationPermission(application.id).can():
        abort(403)

    form = EditConfigurationForm(obj=configuration)
    form.application_id.data = str(configuration.application.id)
    form.name.data = str(configuration.name)
    form.secure.data = configuration.secret

    if form.validate_on_submit():
        from cabotage.utils.config_templates import has_template_variables

        form.populate_obj(configuration)
        is_template = has_template_variables(configuration.value)
        if is_template and configuration.secret:
            flash("Template configs cannot be secrets.", "error")
            return render_template(
                "user/project_application_configuration_edit.html",
                form=form,
                org_slug=organization.slug,
                project_slug=project.slug,
                app_slug=application.slug,
                configuration=configuration,
            )

        if not is_template:
            try:
                app_env = configuration.application_environment
                ns = _config_k8s_namespace(organization, app_env)
                prefix = _config_k8s_resource_prefix(project, application)
                key_slugs = config_writer.write_configuration(ns, prefix, configuration)
            except Exception:
                raise  # No, we should def not do this
            configuration.key_slug = key_slugs["config_key_slug"]
            configuration.build_key_slug = key_slugs["build_key_slug"]
            if configuration.secret:
                configuration.value = "**secure**"
        else:
            configuration.key_slug = None
            configuration.build_key_slug = None
            flash(
                "Template config saved — will resolve at deploy time.",
                "info",
            )
        db.session.flush()
        activity = Activity(
            verb="edit",
            object=configuration,
            data={
                "user_id": str(current_user.id),
                "timestamp": datetime.datetime.utcnow().isoformat(),
            },
        )
        db.session.add(activity)
        db.session.commit()
        _redirect_env_slug = (
            configuration.application_environment.environment.slug
            if project.environments_enabled
            else None
        )
        return redirect(
            url_for(
                "user.project_application",
                org_slug=organization.slug,
                project_slug=project.slug,
                app_slug=application.slug,
                env_slug=_redirect_env_slug,
                _anchor="config",
            )
        )

    if configuration.secret:
        form.value.data = None

    return render_template(
        "user/project_application_configuration_edit.html",
        form=form,
        org_slug=organization.slug,
        project_slug=project.slug,
        app_slug=application.slug,
        configuration=configuration,
    )


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/settings",
    methods=["GET", "POST"],
)
@login_required
def project_application_settings(org_slug, project_slug, app_slug):
    org, project, application = _lookup_app_context(
        org_slug, project_slug, app_slug, require_admin=True
    )

    form = EditApplicationSettingsForm(obj=application)
    form.application_id.choices = [
        (
            str(application.id),
            (
                f"{application.project.organization.slug}/{application.project.slug}: "
                f"{application.slug}"
            ),
        )
    ]
    form.application_id.data = str(application.id)

    if form.validate_on_submit():
        form.populate_obj(application)
        db.session.flush()
        activity = Activity(
            verb="edit",
            object=application,
            data={
                "user_id": str(current_user.id),
                "timestamp": datetime.datetime.utcnow().isoformat(),
            },
        )
        db.session.add(activity)
        db.session.commit()
        environment = _default_environment(project)
        return redirect(
            url_for(
                "user.project_application",
                org_slug=application.project.organization.slug,
                project_slug=application.project.slug,
                app_slug=application.slug,
                env_slug=environment.slug if environment else None,
            )
        )

    return render_template(
        "user/project_application_settings.html",
        application=application,
        form=form,
        app_url=current_app.config.get("GITHUB_APP_URL", "https://github.com"),
        environment=_default_environment(project),
    )


@user_blueprint.route(
    "/application/<application_id>/settings/edit", methods=["GET", "POST"]
)
@login_required
def project_application_settings_legacy(application_id):
    application = Application.query.filter_by(id=application_id).first_or_404()
    if not AdministerApplicationPermission(application.id).can():
        abort(403)
    return redirect(
        url_for(
            "user.project_application_settings",
            org_slug=application.project.organization.slug,
            project_slug=application.project.slug,
            app_slug=application.slug,
        ),
        code=301,
    )


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/env/<env_slug>/applications/<app_slug>/settings",
    methods=["GET", "POST"],
)
@login_required
def project_application_environment_settings(
    org_slug, project_slug, env_slug, app_slug
):
    org, project, application = _lookup_app_context(
        org_slug, project_slug, app_slug, require_admin=True
    )

    environment = Environment.query.filter_by(
        project_id=project.id, slug=env_slug
    ).first_or_404()
    app_env = ApplicationEnvironment.query.filter_by(
        application_id=application.id, environment_id=environment.id
    ).first_or_404()

    form = EditApplicationEnvironmentSettingsForm(obj=app_env)
    form.app_env_id.data = str(app_env.id)

    if form.validate_on_submit():
        app_env.auto_deploy_branch = form.auto_deploy_branch.data or None
        app_env.github_environment_name = form.github_environment_name.data or None
        app_env.deployment_timeout = form.deployment_timeout.data
        app_env.health_check_path = form.health_check_path.data or None
        app_env.health_check_host = form.health_check_host.data or None
        db.session.flush()
        activity = Activity(
            verb="edit",
            object=app_env,
            data={
                "user_id": str(current_user.id),
                "timestamp": datetime.datetime.utcnow().isoformat(),
            },
        )
        db.session.add(activity)
        db.session.commit()
        return redirect(
            url_for(
                "user.project_application",
                org_slug=org_slug,
                project_slug=project_slug,
                app_slug=app_slug,
                env_slug=env_slug,
            )
        )

    return render_template(
        "user/project_application_environment_settings.html",
        application=application,
        app_env=app_env,
        environment=environment,
        form=form,
    )


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/env/<env_slug>/applications/<app_slug>/ingress",
    methods=["GET", "POST"],
)
@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/ingress",
    methods=["GET", "POST"],
    defaults={"env_slug": None},
)
@login_required
def project_application_ingress(org_slug, project_slug, app_slug, env_slug=None):
    if not current_app.config.get("INGRESS_DOMAIN"):
        abort(404)

    org, project, application = _lookup_app_context(
        org_slug, project_slug, app_slug, require_admin=True
    )

    app_env = _resolve_app_env(
        application, env_slug=env_slug, project=project, required=False
    )
    environment = app_env.environment if app_env else None

    ingress_domain = current_app.config["INGRESS_DOMAIN"]

    # Collect available web processes for path target selectors
    web_processes = []
    if app_env:
        procs = set()
        pc = app_env.process_counts or {}
        procs.update(p for p in pc if p.startswith("web"))
        latest_release = app_env.latest_release
        if latest_release and hasattr(latest_release, "processes"):
            procs.update(p for p in latest_release.processes if p.startswith("web"))
        web_processes = sorted(procs)

    def _redirect_back():
        return redirect(
            url_for(
                "user.project_application_ingress",
                org_slug=org_slug,
                project_slug=project_slug,
                app_slug=app_slug,
                env_slug=env_slug,
            )
        )

    def _render_ingress(**extra):
        csrf_form = IngressHostForm()
        return render_template(
            "user/project_application_ingress.html",
            application=application,
            environment=environment,
            app_env=app_env,
            ingress_forms=ingress_forms,
            csrf_form=csrf_form,
            web_processes=web_processes,
            ingress_domain=ingress_domain,
            is_admin=current_user.admin,
            **extra,
        )

    # Build per-ingress forms
    ingress_forms = {}
    if app_env:
        for ing in app_env.ingresses:
            form = IngressSettingsForm(obj=ing, prefix=ing.name)
            ingress_forms[ing.name] = form

    if request.method == "POST" and app_env:
        action = request.form.get("_action")

        _HOSTNAME_RE = re.compile(
            r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)*$"
        )

        # Save ingress (unified: enabled, hosts, paths, settings, annotations)
        if action == "save_ingress":
            ingress_id = request.form.get("_ingress_id")
            ingress = _safe_get(Ingress, ingress_id)
            if not ingress or ingress.application_environment_id != app_env.id:
                return _redirect_back()

            form = IngressSettingsForm(request.form, prefix=ingress.name)
            ingress_forms[ingress.name] = form
            ingress_errors = {}

            if not form.validate():
                # render_field_compact shows field.errors inline
                return _render_ingress(ingress_errors=ingress_errors)

            new_use_regex = form.use_regex.data

            # --- Validate everything before touching the session ---

            # Validate new hostnames
            new_host_indices = set()
            for key in request.form:
                m = re.match(r"_new_host_(\d+)_name", key)
                if m:
                    new_host_indices.add(m.group(1))

            new_hostnames = []
            for idx in sorted(new_host_indices):
                hostname = request.form.get(f"_new_host_{idx}_name", "").strip()
                tls_enabled = f"_new_host_{idx}_tls" in request.form
                if not hostname:
                    continue
                if len(hostname) > 253 or not _HOSTNAME_RE.match(hostname):
                    ingress_errors.setdefault("hosts", []).append(
                        f"Invalid hostname: {hostname}"
                    )
                else:
                    new_hostnames.append((hostname, tls_enabled))

            # Validate use_regex toggle against kept paths
            kept_path_ids = set(request.form.getlist("_existing_path"))
            kept_paths = [p for p in ingress.paths if str(p.id) in kept_path_ids]
            if new_use_regex and not ingress.use_regex:
                for p in kept_paths:
                    try:
                        re.compile(p.path)
                    except re.error as e:
                        ingress_errors.setdefault("paths", []).append(
                            f"Cannot enable regex: path '{p.path}' is not a valid regex ({e})."
                        )
            elif not new_use_regex and ingress.use_regex:
                for p in kept_paths:
                    if _REGEX_META.search(p.path):
                        ingress_errors.setdefault("paths", []).append(
                            f"Cannot disable regex: path '{p.path}' contains regex characters."
                        )

            # Validate new paths
            new_path_indices = set()
            for key in request.form:
                m = re.match(r"_new_path_(\d+)_path", key)
                if m:
                    new_path_indices.add(m.group(1))

            new_paths = []
            for idx in sorted(new_path_indices):
                path_value = request.form.get(f"_new_path_{idx}_path", "").strip()
                path_type = request.form.get(f"_new_path_{idx}_type", "Prefix")
                target = request.form.get(f"_new_path_{idx}_target", "")
                if not path_value:
                    continue
                if not path_value.startswith("/") or len(path_value) > 256:
                    ingress_errors.setdefault("paths", []).append(
                        f"Invalid path: {path_value}"
                    )
                elif new_use_regex:
                    try:
                        re.compile(path_value)
                    except re.error as e:
                        ingress_errors.setdefault("paths", []).append(
                            f"Invalid regex path '{path_value}': {e}"
                        )
                elif _REGEX_META.search(path_value):
                    ingress_errors.setdefault("paths", []).append(
                        f"Path '{path_value}' contains regex characters but regex is disabled."
                    )
                if target and target not in web_processes:
                    ingress_errors.setdefault("paths", []).append(
                        f"Invalid target process: {target}"
                    )
                if not ingress_errors.get("paths"):
                    new_paths.append((path_value, path_type, target))

            if ingress_errors:
                return _render_ingress(ingress_errors=ingress_errors)

            # --- All valid: apply changes ---

            with db.session.no_autoflush:
                # Enabled
                ingress.enabled = "_enabled" in request.form

                # Settings
                ingress.proxy_connect_timeout = form.proxy_connect_timeout.data
                ingress.proxy_read_timeout = form.proxy_read_timeout.data
                ingress.proxy_send_timeout = form.proxy_send_timeout.data
                ingress.proxy_body_size = form.proxy_body_size.data
                ingress.client_body_buffer_size = form.client_body_buffer_size.data
                ingress.proxy_request_buffering = (
                    form.proxy_request_buffering.data or None
                )
                ingress.session_affinity = form.session_affinity.data
                ingress.use_regex = new_use_regex

                # Hosts: diff existing vs form
                kept_host_ids = set(request.form.getlist("_existing_host"))
                for host in list(ingress.hosts):
                    if str(host.id) not in kept_host_ids and not host.is_auto_generated:
                        db.session.delete(host)
                    elif str(host.id) in kept_host_ids:
                        if host.is_auto_generated:
                            host.tls_enabled = True
                        else:
                            host.tls_enabled = (
                                f"_existing_host_tls_{host.id}" in request.form
                            )

                for hostname, tls_enabled in new_hostnames:
                    db.session.add(
                        IngressHost(
                            ingress_id=ingress.id,
                            hostname=hostname,
                            tls_enabled=tls_enabled,
                            is_auto_generated=False,
                        )
                    )

                # Paths: diff existing vs form
                for path in list(ingress.paths):
                    if str(path.id) not in kept_path_ids:
                        db.session.delete(path)

                for path_value, path_type, target in new_paths:
                    db.session.add(
                        IngressPath(
                            ingress_id=ingress.id,
                            path=path_value,
                            path_type=path_type,
                            target_process_name=target,
                        )
                    )

                # Annotations (admin only)
                if current_user.admin:
                    allow = request.form.get("_allow_annotations") == "on"
                    ingress.allow_annotations = allow
                    if allow:
                        annotations = {}
                        for form_key in request.form:
                            if form_key.startswith("_annotation_key_"):
                                idx = form_key[len("_annotation_key_") :]
                                key = request.form[form_key].strip()
                                value = request.form.get(f"_annotation_value_{idx}", "")
                                if key:
                                    annotations[key] = value
                        ingress.extra_annotations = annotations
                    else:
                        ingress.extra_annotations = {}

                activity = Activity(
                    verb="edit",
                    object=ingress,
                    data={
                        "user_id": str(current_user.id),
                        "timestamp": datetime.datetime.utcnow().isoformat(),
                    },
                )
                db.session.add(activity)
                try:
                    db.session.commit()
                    flash(f"Ingress '{ingress.name}' saved.", "success")
                except IntegrityError:
                    db.session.rollback()
                    ingress_errors.setdefault("hosts", []).append(
                        "A hostname or path conflicts with an existing entry."
                    )
                    return _render_ingress(ingress_errors=ingress_errors)
            return _redirect_back()

        # Delete ingress
        if action == "delete_ingress":
            ingress_id = request.form.get("_ingress_id")
            ingress = _safe_get(Ingress, ingress_id)
            if not ingress or ingress.application_environment_id != app_env.id:
                return _redirect_back()
            confirm_name = request.form.get("_confirm_name", "").strip()
            if confirm_name != ingress.name:
                flash("Ingress name does not match. Deletion cancelled.", "error")
                return _redirect_back()
            ingress_name = ingress.name
            activity = Activity(
                verb="delete",
                object=ingress,
                data={
                    "user_id": str(current_user.id),
                    "timestamp": datetime.datetime.utcnow().isoformat(),
                },
            )
            db.session.add(activity)
            db.session.delete(ingress)
            db.session.commit()
            flash(f"Ingress '{ingress_name}' deleted.", "success")
            return _redirect_back()

        # Create new ingress
        if action == "create_ingress":
            new_name = request.form.get("_new_ingress_name", "").strip()
            if new_name and not re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", new_name):
                flash(
                    "Ingress name must be lowercase alphanumeric and hyphens.", "error"
                )
                return _redirect_back()
            if new_name:
                existing = Ingress.query.filter_by(
                    application_environment_id=app_env.id,
                    name=new_name,
                ).first()
                if existing:
                    flash(f"Ingress '{new_name}' already exists.", "error")
                else:
                    ingress = Ingress(
                        application_environment_id=app_env.id,
                        name=new_name,
                        enabled=False,
                    )
                    db.session.add(ingress)
                    db.session.flush()
                    hostname_pairs = [(org.slug, org.k8s_identifier)]
                    if project.environments_enabled and environment:
                        hostname_pairs.append(
                            (environment.slug, environment.k8s_identifier)
                        )
                    hostname_pairs.extend(
                        [
                            (project.slug, project.k8s_identifier),
                            (application.slug, application.k8s_identifier),
                        ]
                    )
                    auto_hostname = (
                        f"{readable_k8s_hostname(*hostname_pairs)}"
                        f"-{new_name}.{ingress_domain}"
                    )
                    host = IngressHost(
                        ingress_id=ingress.id,
                        hostname=auto_hostname,
                        tls_enabled=True,
                        is_auto_generated=True,
                    )
                    db.session.add(host)
                    activity = Activity(
                        verb="create",
                        object=ingress,
                        data={
                            "user_id": str(current_user.id),
                            "timestamp": datetime.datetime.utcnow().isoformat(),
                        },
                    )
                    db.session.add(activity)
                    db.session.commit()
                    flash(f"Ingress '{new_name}' created.", "success")
                return _redirect_back()

    return _render_ingress()


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/config/<config_id>/delete",
    methods=["GET", "POST"],
)
@login_required
def project_application_configuration_delete(
    org_slug, project_slug, app_slug, config_id
):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    project = Project.query.filter_by(
        organization_id=organization.id, slug=project_slug
    ).first_or_404()
    application = Application.query.filter_by(
        project_id=project.id, slug=app_slug
    ).first_or_404()
    configuration = Configuration.query.filter_by(
        application_id=application.id, id=config_id
    ).first_or_404()
    if not AdministerApplicationPermission(application.id).can():
        abort(403)

    if len(configuration.application_environment.configurations) <= 1:
        abort(400)

    if request.method == "GET":
        form = DeleteConfigurationForm(obj=configuration)
    else:
        form = DeleteConfigurationForm()
    form.configuration_id.data = str(configuration.id)
    form.name.data = str(configuration.name)
    form.value.data = str(configuration.value)
    form.secure.data = str(configuration.secret)

    env_slug = (
        configuration.application_environment.environment.slug
        if project.environments_enabled
        else None
    )

    if form.validate_on_submit():
        db.session.delete(configuration)
        db.session.flush()
        activity = Activity(
            verb="delete",
            object=configuration,
            data={
                "user_id": str(current_user.id),
                "timestamp": datetime.datetime.utcnow().isoformat(),
            },
        )
        db.session.add(activity)
        db.session.commit()
        # TODO: Coordinate configuration deletion between db and config_writer
        # try:
        #     config_writer.delete_configuration(
        #         org_slug,
        #         project_slug,
        #         app_slug,
        #         configuration,
        #     )
        # except Exception :
        #     raise
        return redirect(
            url_for(
                "user.project_application",
                org_slug=organization.slug,
                project_slug=project.slug,
                app_slug=application.slug,
                env_slug=env_slug,
                _anchor="config",
            )
        )
    return render_template(
        "user/project_application_configuration_delete.html",
        form=form,
        org_slug=organization.slug,
        project_slug=project.slug,
        app_slug=application.slug,
        configuration=configuration,
    )


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/images"
)
@login_required
def application_images(org_slug, project_slug, app_slug):
    org, project, application = _lookup_app_context(org_slug, project_slug, app_slug)
    page = request.args.get("page", 1, type=int)
    env_slug = request.args.get("env_slug")
    app_env = _resolve_app_env(application, env_slug=env_slug, project=project)
    images = app_env.images.order_by(Image.version.desc()).paginate(page, 20, False)
    return render_template(
        "user/application_images.html",
        page=page,
        application=application,
        images=images.items,
        environment=app_env.environment if project.environments_enabled else None,
        app_env=app_env,
    )


@user_blueprint.route("/applications/<application_id>/images")
@login_required
def application_images_legacy(application_id):
    application = Application.query.filter_by(id=application_id).first_or_404()
    if not ViewApplicationPermission(application.id).can():
        abort(403)
    return redirect(
        url_for(
            "user.application_images",
            org_slug=application.project.organization.slug,
            project_slug=application.project.slug,
            app_slug=application.slug,
        ),
        code=301,
    )


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/images/<image_id>"
)
@login_required
def image_detail(org_slug, project_slug, app_slug, image_id):
    org, project, application = _lookup_app_context(org_slug, project_slug, app_slug)
    image = Image.query.filter_by(
        id=image_id, application_id=application.id
    ).first_or_404()
    if image.error:
        image.image_build_log = f"{image.image_build_log}\n**Error!**"
    secret = current_app.config["REGISTRY_AUTH_SECRET"]
    docker_pull_credentials = image.docker_pull_credentials(secret)
    return render_template(
        "user/image_detail.html",
        image=image,
        docker_pull_credentials=docker_pull_credentials,
    )


@user_blueprint.route("/image/<image_id>")
@login_required
def image_detail_legacy(image_id):
    image = Image.query.filter_by(id=image_id).first_or_404()
    if not ViewApplicationPermission(image.application.id).can():
        abort(403)
    return redirect(
        url_for(
            "user.image_detail",
            org_slug=image.application.project.organization.slug,
            project_slug=image.application.project.slug,
            app_slug=image.application.slug,
            image_id=image_id,
        ),
        code=301,
    )


def _stream_redis_build_logs(ws, build_type, build_job_id, job_label):
    """Stream build logs from Redis over a WebSocket."""
    redis_client = get_redis_client(current_app.config["CELERY_BROKER_URL"])
    log_key = stream_key(build_type, build_job_id)
    ws.send(f"Job Pod {job_label}")
    for line in read_log_stream(redis_client, log_key):
        if line is None:
            continue
        ws.send(f"  {line}")
    ws.send("=================END OF LOGS=================")


def _stream_image_build_logs(ws, image):
    """Stream image build logs over a websocket, handling error state."""
    if image.error:
        ws.send(f"Job Pod imagebuild-{image.build_job_id}")
        if image.image_build_log:
            for line in image.image_build_log.split("\n"):
                ws.send(f"  {line}")
        if image.error_detail:
            ws.send(f"  Error: {image.error_detail}")
        ws.send("=================END OF LOGS=================")
        return
    build_job_id = image.build_job_id
    image_build_log = image.image_build_log

    if image_build_log is not None:
        ws.send(f"Job Pod imagebuild-{build_job_id}")
        for line in image_build_log.split("\n"):
            ws.send(f"  {line}")
        ws.send("=================END OF LOGS=================")
        return

    db.session.remove()

    _stream_redis_build_logs(
        ws,
        "image",
        build_job_id,
        f"imagebuild-{build_job_id}",
    )


@sock.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/images/<image_id>/livelogs",
    bp=user_blueprint,
)
@login_required
def image_build_livelogs(ws, org_slug, project_slug, app_slug, image_id):
    org, project, application = _lookup_app_context(org_slug, project_slug, app_slug)
    image = Image.query.filter_by(
        id=image_id, application_id=application.id
    ).first_or_404()
    _stream_image_build_logs(ws, image)


@sock.route("/image/<image_id>/livelogs", bp=user_blueprint)
@login_required
def image_build_livelogs_legacy(ws, image_id):
    image = Image.query.filter_by(id=image_id).first_or_404()
    if not ViewApplicationPermission(image.application.id).can():
        abort(403)
    _stream_image_build_logs(ws, image)


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/releases"
)
@login_required
def application_releases(org_slug, project_slug, app_slug):
    org, project, application = _lookup_app_context(org_slug, project_slug, app_slug)
    page = request.args.get("page", 1, type=int)
    env_slug = request.args.get("env_slug")
    app_env = _resolve_app_env(application, env_slug=env_slug, project=project)
    releases = app_env.releases.order_by(Release.version.desc()).paginate(
        page, 20, False
    )

    # Pre-resolve Image objects to avoid N+1 on release.image_object per row
    resolver = RelatedObjectResolver(releases=releases.items)
    resolver.warm_caches([], releases.items)
    _, image_by_id = resolver.build_lookup_dicts()
    release_proc_counts = compute_process_counts(releases.items, resolver)

    return render_template(
        "user/application_releases.html",
        page=page,
        application=application,
        releases=releases.items,
        environment=app_env.environment if project.environments_enabled else None,
        app_env=app_env,
        image_by_id=image_by_id,
        release_proc_counts=release_proc_counts,
    )


@user_blueprint.route("/applications/<application_id>/releases")
@login_required
def application_releases_legacy(application_id):
    application = Application.query.filter_by(id=application_id).first_or_404()
    if not ViewApplicationPermission(application.id).can():
        abort(403)
    return redirect(
        url_for(
            "user.application_releases",
            org_slug=application.project.organization.slug,
            project_slug=application.project.slug,
            app_slug=application.slug,
        ),
        code=301,
    )


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/releases/<release_id>"
)
@login_required
def release_detail(org_slug, project_slug, app_slug, release_id):
    org, project, application = _lookup_app_context(org_slug, project_slug, app_slug)
    release = Release.query.filter_by(
        id=release_id, application_id=application.id
    ).first_or_404()
    secret = current_app.config["REGISTRY_AUTH_SECRET"]
    docker_pull_credentials = release.docker_pull_credentials(secret)
    image_pull_secrets = release.image_pull_secrets(
        secret,
        registry_urls=[
            current_app.config["REGISTRY_PULL"],
            current_app.config["REGISTRY_BUILD"],
        ],
    )
    return render_template(
        "user/release_detail.html",
        release=release,
        deploy_form=ReleaseDeployForm(),
        docker_pull_credentials=docker_pull_credentials,
        image_pull_secrets=image_pull_secrets,
    )


@user_blueprint.route("/release/<release_id>")
@login_required
def release_detail_legacy(release_id):
    release = Release.query.filter_by(id=release_id).first_or_404()
    if not ViewApplicationPermission(release.application.id).can():
        abort(403)
    return redirect(
        url_for(
            "user.release_detail",
            org_slug=release.application.project.organization.slug,
            project_slug=release.application.project.slug,
            app_slug=release.application.slug,
            release_id=release_id,
        ),
        code=301,
    )


def _stream_release_build_logs(ws, release):
    """Stream release build logs over a websocket, handling error state."""
    if release.error:
        ws.send(f"Job Pod releasebuild-{release.build_job_id}")
        if release.release_build_log:
            for line in release.release_build_log.split("\n"):
                ws.send(f"  {line}")
        if release.error_detail:
            ws.send(f"  Error: {release.error_detail}")
        ws.send("=================END OF LOGS=================")
        return
    build_job_id = release.build_job_id
    release_build_log = release.release_build_log

    if release_build_log is not None:
        ws.send(f"Job Pod releasebuild-{build_job_id}")
        for line in release_build_log.split("\n"):
            ws.send(f"  {line}")
        ws.send("=================END OF LOGS=================")
        return

    db.session.remove()

    _stream_redis_build_logs(
        ws,
        "release",
        build_job_id,
        f"releasebuild-{build_job_id}",
    )


@sock.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/releases/<release_id>/livelogs",
    bp=user_blueprint,
)
@login_required
def release_build_livelogs(ws, org_slug, project_slug, app_slug, release_id):
    org, project, application = _lookup_app_context(org_slug, project_slug, app_slug)
    release = Release.query.filter_by(
        id=release_id, application_id=application.id
    ).first_or_404()
    _stream_release_build_logs(ws, release)


@sock.route("/release/<release_id>/livelogs", bp=user_blueprint)
@login_required
def release_build_livelogs_legacy(ws, release_id):
    release = Release.query.filter_by(id=release_id).first_or_404()
    if not ViewApplicationPermission(release.application.id).can():
        abort(403)
    _stream_release_build_logs(ws, release)


def _stream_deployment_logs(ws, deployment):
    """Stream deploy logs for a deployment over a websocket."""
    deployment_id = deployment.id
    job_id = deployment.job_id
    deploy_log = deployment.deploy_log

    if deploy_log is not None:
        ws.send(f"Job Pod deployment-{job_id}")
        for line in deploy_log.split("\n"):
            ws.send(f"  {line}")
        ws.send("=================END OF LOGS=================")
        return

    db.session.remove()

    if current_app.config["KUBERNETES_ENABLED"]:
        if job_id is None:
            for _ in range(60):
                time.sleep(0.5)
                dep = Deployment.query.filter_by(id=deployment_id).first()
                if dep is None:
                    ws.send("=================END OF LOGS=================")
                    return
                if dep.job_id is not None:
                    job_id = dep.job_id
                    break
                if dep.deploy_log is not None:
                    ws.send(f"Job Pod deployment-{dep.job_id}")
                    for line in dep.deploy_log.split("\n"):
                        ws.send(f"  {line}")
                    ws.send("=================END OF LOGS=================")
                    return
                db.session.remove()
            else:
                ws.send("=================END OF LOGS=================")
                return

        _stream_redis_build_logs(
            ws,
            "deploy",
            job_id,
            f"deployment-{job_id}",
        )
    else:
        ws.send("=================END OF LOGS=================")


@sock.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/deployments/<deployment_id>/livelogs",
    bp=user_blueprint,
)
@login_required
def deployment_livelogs(ws, org_slug, project_slug, app_slug, deployment_id):
    org, project, application = _lookup_app_context(org_slug, project_slug, app_slug)
    deployment = Deployment.query.filter_by(
        id=deployment_id, application_id=application.id
    ).first_or_404()
    _stream_deployment_logs(ws, deployment)


@sock.route("/deployment/<deployment_id>/livelogs", bp=user_blueprint)
@login_required
def deployment_livelogs_legacy(ws, deployment_id):
    deployment = Deployment.query.filter_by(id=deployment_id).first_or_404()
    if not ViewApplicationPermission(deployment.application.id).can():
        abort(403)
    _stream_deployment_logs(ws, deployment)


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/deployments/<deployment_id>"
)
@login_required
def deployment_detail(org_slug, project_slug, app_slug, deployment_id):
    org, project, application = _lookup_app_context(org_slug, project_slug, app_slug)
    deployment = Deployment.query.filter_by(
        id=deployment_id, application_id=application.id
    ).first_or_404()
    return render_template("user/deployment_detail.html", deployment=deployment)


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/deployments/<deployment_id>/logs"
)
@login_required
def deployment_logs_view(org_slug, project_slug, app_slug, deployment_id):
    org, project, application = _lookup_app_context(org_slug, project_slug, app_slug)
    deployment = Deployment.query.filter_by(
        id=deployment_id, application_id=application.id
    ).first_or_404()

    loki_configured = bool(current_app.config.get("LOKI_URL"))
    app_env = deployment.application_environment
    process_names = sorted(app_env.process_counts or {}) if app_env else []

    # Determine if this deployment's pods are still live
    if not deployment.complete and not deployment.error:
        deployment_is_live = True
    elif deployment.complete and app_env:
        latest_completed = (
            Deployment.query.filter_by(
                application_environment_id=app_env.id, complete=True
            )
            .order_by(Deployment.created.desc())
            .first()
        )
        deployment_is_live = latest_completed and latest_completed.id == deployment.id
    else:
        deployment_is_live = False

    return render_template(
        "user/deployment_logs.html",
        deployment=deployment,
        application=application,
        loki_configured=loki_configured,
        process_names=process_names,
        deployment_is_live=deployment_is_live,
    )


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/deployments/<deployment_id>/logs/query"
)
@login_required
def deployment_logs_query(org_slug, project_slug, app_slug, deployment_id):
    org, project, application = _lookup_app_context(org_slug, project_slug, app_slug)
    deployment = Deployment.query.filter_by(
        id=deployment_id, application_id=application.id
    ).first_or_404()

    app_env = deployment.application_environment
    if not app_env:
        return jsonify({"error": "not configured"}), 404

    namespace = _compute_observe_namespace(application, app_env)
    process_names = sorted(app_env.process_counts or {})
    selectors = [
        f'namespace="{namespace}"',
        f'deployment="{deployment_id}"',
    ]
    return _loki_query_response(selectors, process_names)


@user_blueprint.route("/deployment/<deployment_id>")
@login_required
def deployment_detail_legacy(deployment_id):
    deployment = Deployment.query.filter_by(id=deployment_id).first_or_404()
    if not ViewApplicationPermission(deployment.application.id).can():
        abort(403)
    return redirect(
        url_for(
            "user.deployment_detail",
            org_slug=deployment.application.project.organization.slug,
            project_slug=deployment.application.project.slug,
            app_slug=deployment.application.slug,
            deployment_id=deployment_id,
        ),
        code=301,
    )


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/releases/create",
    methods=["POST"],
)
@login_required
def application_release_create(org_slug, project_slug, app_slug):
    org, project, application = _lookup_app_context(org_slug, project_slug, app_slug)

    environment_id = request.form.get("environment_id")
    app_env = _resolve_app_env(application, environment_id=environment_id)

    release = application.create_release(app_env=app_env)
    db.session.add(release)
    db.session.flush()
    activity = Activity(
        verb="edit",
        object=release,
        data={
            "user_id": str(current_user.id),
            "timestamp": datetime.datetime.utcnow().isoformat(),
        },
    )
    db.session.add(activity)
    db.session.commit()
    run_release_build.delay(release_id=release.id)
    return redirect(
        url_for(
            "user.release_detail",
            org_slug=org_slug,
            project_slug=project_slug,
            app_slug=app_slug,
            release_id=release.id,
        )
    )


@user_blueprint.route(
    "/applications/<application_id>/release/create", methods=["GET", "POST"]
)
@login_required
def application_release_create_legacy(application_id):
    application = Application.query.filter_by(id=application_id).first_or_404()
    if not ViewApplicationPermission(application.id).can():
        abort(403)
    return redirect(
        url_for(
            "user.application_release_create",
            org_slug=application.project.organization.slug,
            project_slug=application.project.slug,
            app_slug=application.slug,
        ),
        code=307,
    )


@user_blueprint.route("/guide")
@login_required
def guide():
    return render_template(
        "user/guide.html",
        github_app_url=current_app.config.get("GITHUB_APP_URL", "https://github.com"),
    )


@user_blueprint.route("/docker/auth")
def docker_auth():
    secret = current_app.config["REGISTRY_AUTH_SECRET"]
    password = request.authorization.password
    scope_params = request.args.getlist("scope")
    scope = " ".join(scope_params) if scope_params else "registry:catalog:*"
    requested_access = parse_docker_scope(scope)
    max_age = None
    if "push" in [
        action for access in requested_access for action in access["actions"]
    ]:
        max_age = 600
    granted_access = check_docker_credentials(password, secret=secret, max_age=max_age)
    if not granted_access:
        return jsonify({"error": "unauthorized"}), 401
    access = docker_access_intersection(granted_access, requested_access)
    return jsonify({"token": generate_docker_registry_jwt(access=access)})


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/images/fromsource",
    methods=["POST"],
)
@login_required
def application_images_build_fromsource(org_slug, project_slug, app_slug):
    org, project, application = _lookup_app_context(
        org_slug, project_slug, app_slug, require_admin=True
    )

    environment_id = request.form.get("environment_id")
    app_env = _resolve_app_env(application, environment_id=environment_id)

    image = Image(
        application_id=application.id,
        application_environment_id=app_env.id,
        _repository_name=application.registry_repository_name(app_env),
        build_ref=app_env.effective_auto_deploy_branch,
    )
    db.session.add(image)
    db.session.flush()
    activity = Activity(
        verb="fromsource",
        object=image,
        data={
            "user_id": str(current_user.id),
            "timestamp": datetime.datetime.utcnow().isoformat(),
        },
    )
    db.session.add(activity)
    db.session.commit()
    run_image_build.delay(image_id=image.id, buildkit=True)
    return redirect(
        url_for(
            "user.image_detail",
            org_slug=org_slug,
            project_slug=project_slug,
            app_slug=app_slug,
            image_id=image.id,
        )
    )


@user_blueprint.route(
    "/applications/<application_id>/images/fromsource", methods=["POST"]
)
@login_required
def application_images_build_fromsource_legacy(application_id):
    application = Application.query.filter_by(id=application_id).first_or_404()
    if not AdministerApplicationPermission(application.id).can():
        abort(403)
    return redirect(
        url_for(
            "user.application_images_build_fromsource",
            org_slug=application.project.organization.slug,
            project_slug=application.project.slug,
            app_slug=application.slug,
        ),
        code=307,
    )


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/clearcache",
    methods=["POST"],
)
@login_required
def application_clear_cache(org_slug, project_slug, app_slug):
    org, project, application = _lookup_app_context(
        org_slug, project_slug, app_slug, require_admin=True
    )

    app_env = application.default_app_env
    repository_name = application.registry_repository_name(app_env)

    api_client = kubernetes_ext.kubernetes_client
    core_api_instance = kubernetes.client.CoreV1Api(api_client)
    batch_api_instance = kubernetes.client.BatchV1Api(api_client)
    image = application.images.first()
    if image is not None and current_app.config["KUBERNETES_ENABLED"]:
        from cabotage.celery.tasks.deploy import run_job
        from cabotage.celery.tasks.build import fetch_image_build_cache_volume_claim

        buildkit_image = current_app.config["BUILDKIT_IMAGE"]

        volume_claim = fetch_image_build_cache_volume_claim(core_api_instance, image)
        job_object = kubernetes.client.V1Job(
            metadata=kubernetes.client.V1ObjectMeta(
                name=f"clear-cache-{volume_claim.metadata.name}"[:63],
                labels={
                    "organization": image.application.project.organization.slug,
                    "project": image.application.project.slug,
                    "application": image.application.slug,
                    "process": "clear-cache",
                    "resident-job.cabotage.io": "true",
                },
            ),
            spec=kubernetes.client.V1JobSpec(
                active_deadline_seconds=1800,
                backoff_limit=0,
                parallelism=1,
                completions=1,
                template=kubernetes.client.V1PodTemplateSpec(
                    metadata=kubernetes.client.V1ObjectMeta(
                        labels={
                            "organization": image.application.project.organization.slug,  # noqa: E501
                            "project": image.application.project.slug,
                            "application": image.application.slug,
                            "process": "clear-cache",
                            "ca-admission.cabotage.io": "true",
                            "resident-pod.cabotage.io": "true",
                        },
                        annotations={
                            "container.apparmor.security.beta.kubernetes.io/clear-cache": "unconfined",  # noqa: E501
                        },
                    ),
                    spec=kubernetes.client.V1PodSpec(
                        restart_policy="Never",
                        security_context=kubernetes.client.V1PodSecurityContext(
                            fs_group=1000,
                            fs_group_change_policy="OnRootMismatch",
                        ),
                        containers=[
                            kubernetes.client.V1Container(
                                name="clear-cache",
                                image=buildkit_image,
                                command=["buildctl-daemonless.sh"],
                                args=["prune", "--all"],
                                env=[
                                    kubernetes.client.V1EnvVar(
                                        name="BUILDKITD_FLAGS",
                                        value="--oci-worker-no-process-sandbox",  # noqa: E501
                                    ),
                                ],
                                security_context=kubernetes.client.V1SecurityContext(
                                    seccomp_profile=kubernetes.client.V1SeccompProfile(
                                        type="Unconfined",
                                    ),
                                    run_as_user=1000,
                                    run_as_group=1000,
                                ),
                                volume_mounts=[
                                    kubernetes.client.V1VolumeMount(
                                        mount_path="/home/user/.local/share/buildkit",
                                        name="build-cache",
                                    ),
                                ],
                            ),
                        ],
                        volumes=[
                            kubernetes.client.V1Volume(
                                name="build-cache",
                                persistent_volume_claim=kubernetes.client.V1PersistentVolumeClaimVolumeSource(
                                    claim_name=volume_claim.metadata.name
                                ),
                            ),
                        ],
                    ),
                ),
            ),
        )

        job_complete, job_logs = run_job(
            core_api_instance, batch_api_instance, "default", job_object
        )

    def auth(dxf, response):
        dxf.token = generate_docker_registry_jwt(
            access=[{"type": "repository", "name": repository_name, "actions": ["*"]}]
        )

    registry = current_app.config["REGISTRY_BUILD"]
    registry_secure = current_app.config["REGISTRY_SECURE"]
    _tlsverify = False
    if registry_secure:
        _tlsverify = current_app.config["REGISTRY_VERIFY"]
        if _tlsverify == "True":
            _tlsverify = True
    client = DXF(
        host=registry,
        repo=repository_name,
        auth=auth,
        insecure=(not registry_secure),
        tlsverify=_tlsverify,
    )

    try:
        client.get_alias("image-buildcache")
        try:
            client.del_alias("image-buildcache")
        except (
            HTTPError
        ) as e:  # Exception based error handling vs returning a None :upsidedownsmile:
            if e.response.status_code == 404:
                pass  # Suppose there could be a race that the get_alias didn't find
            elif e.response.status_code == 405:
                pass  # The registry may not be configured to allow deletes
            else:
                raise
    except (
        HTTPError
    ) as e:  # Exception based error handling vs just returning a None :upsidedownsmile:
        if e.response.status_code == 404:
            pass
        else:
            raise
    try:
        client.get_alias("release-buildcache")
        try:
            client.del_alias("release-buildcache")
        except (
            HTTPError
        ) as e:  # Exception based error handling vs returning a None :upsidedownsmile:
            if e.response.status_code == 404:
                pass  # Suppose there could be a race that the get_alias didn't find
            elif e.response.status_code == 405:
                pass  # The registry may not be configured to allow deletes
            else:
                raise
    except (
        HTTPError
    ) as e:  # Exception based error handling vs just returning a None :upsidedownsmile:
        if e.response.status_code == 404:
            pass
        else:
            raise

    _env = _default_environment(project)
    return redirect(
        url_for(
            "user.project_application",
            org_slug=application.project.organization.slug,
            project_slug=application.project.slug,
            app_slug=application.slug,
            env_slug=_env.slug if _env else None,
        )
    )


@user_blueprint.route("/applications/<application_id>/clearcache", methods=["POST"])
@login_required
def application_clear_cache_legacy(application_id):
    application = Application.query.filter_by(id=application_id).first_or_404()
    if not AdministerApplicationPermission(application.id).can():
        abort(403)
    return redirect(
        url_for(
            "user.application_clear_cache",
            org_slug=application.project.organization.slug,
            project_slug=application.project.slug,
            app_slug=application.slug,
        ),
        code=307,
    )


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/scale",
    methods=["POST"],
)
@login_required
def application_scale(org_slug, project_slug, app_slug):
    org, project, application = _lookup_app_context(
        org_slug, project_slug, app_slug, require_admin=True
    )

    environment_id = request.form.get("environment_id")
    app_env = _resolve_app_env(application, environment_id=environment_id)

    form = ApplicationScaleForm()
    form.application_id.data = str(application.id)
    if form.validate_on_submit():
        scaled = collections.defaultdict(dict)
        for key, value in request.form.items():
            if key.startswith("process-count-"):
                process_name = key[len("process-count-") :]
                if app_env.process_counts.get(process_name, 0) != int(value):
                    scaled[process_name]["process_count"] = {
                        "old_value": app_env.process_counts.get(process_name, 0),
                        "new_value": int(value),
                    }
                    app_env.process_counts[process_name] = int(value)
                    flag_modified(app_env, "process_counts")
            if key.startswith("process-pod-class-"):
                process_name = key[len("process-pod-class-") :]
                if app_env.process_pod_classes.get(process_name, 0) != value:
                    scaled[process_name]["pod_class"] = {
                        "old_value": app_env.process_pod_classes.get(
                            process_name, DEFAULT_POD_CLASS
                        ),
                        "new_value": value,
                    }
                    app_env.process_pod_classes[process_name] = value
                    flag_modified(app_env, "process_pod_classes")
        if scaled:
            activity = Activity(
                verb="scale",
                object=application,
                data={
                    "user_id": str(current_user.id),
                    "timestamp": datetime.datetime.utcnow().isoformat(),
                    "changes": scaled,
                },
            )
            db.session.add(app_env)
            db.session.add(activity)
            db.session.commit()

            if current_app.config["KUBERNETES_ENABLED"]:
                from cabotage.celery.tasks.deploy import k8s_namespace as _k8s_ns

                latest = app_env.latest_release_built
                if latest:
                    namespace = _k8s_ns(latest)
                    for process_name, change in scaled.items():
                        if "process_count" in change:
                            scale_deployment(
                                namespace,
                                latest,
                                process_name,
                                change["process_count"]["new_value"],
                            )
                        if "pod_class" in change:
                            resize_deployment(
                                namespace,
                                latest,
                                process_name,
                                change["pod_class"]["new_value"],
                            )
    else:
        return jsonify(form.errors), 400
    return redirect(
        url_for(
            "user.project_application",
            org_slug=application.project.organization.slug,
            project_slug=application.project.slug,
            app_slug=application.slug,
            env_slug=app_env.environment.slug if project.environments_enabled else None,
        )
    )


@user_blueprint.route("/application/<application_id>/scale", methods=["POST"])
@login_required
def application_scale_legacy(application_id):
    application = Application.query.filter_by(id=application_id).first_or_404()
    if not AdministerApplicationPermission(application.id).can():
        abort(403)
    return redirect(
        url_for(
            "user.application_scale",
            org_slug=application.project.organization.slug,
            project_slug=application.project.slug,
            app_slug=application.slug,
        ),
        code=307,
    )


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/releases/<release_id>/deploy",
    methods=["POST"],
)
@login_required
def release_deploy(org_slug, project_slug, app_slug, release_id):
    org, project, application = _lookup_app_context(
        org_slug, project_slug, app_slug, require_admin=True
    )
    release = Release.query.filter_by(
        id=release_id, application_id=application.id
    ).first_or_404()
    deployment = Deployment(
        application_id=release.application.id,
        application_environment_id=release.application_environment_id,
        release=release.asdict,
    )
    db.session.add(deployment)
    db.session.flush()
    activity = Activity(
        verb="deploy",
        object=deployment,
        data={
            "user_id": str(current_user.id),
            "timestamp": datetime.datetime.utcnow().isoformat(),
        },
    )
    db.session.add(activity)
    db.session.commit()
    if current_app.config["KUBERNETES_ENABLED"]:
        deployment_id = deployment.id
        run_deploy.delay(deployment_id=deployment.id)
        deployment = Deployment.query.filter_by(id=deployment_id).first_or_404()
    else:
        from cabotage.celery.tasks.deploy import fake_deploy_release

        fake_deploy_release(deployment)
        deployment.complete = True
        db.session.commit()
    return redirect(
        url_for(
            "user.deployment_detail",
            org_slug=org_slug,
            project_slug=project_slug,
            app_slug=app_slug,
            deployment_id=deployment.id,
        )
    )


@user_blueprint.route("/release/<release_id>/deploy", methods=["POST"])
@login_required
def release_deploy_legacy(release_id):
    release = Release.query.filter_by(id=release_id).first_or_404()
    if not AdministerApplicationPermission(release.application.id).can():
        abort(403)
    return redirect(
        url_for(
            "user.release_deploy",
            org_slug=release.application.project.organization.slug,
            project_slug=release.application.project.slug,
            app_slug=release.application.slug,
            release_id=release_id,
        ),
        code=307,
    )


@user_blueprint.route("/signing-cert", methods=["GET"])
def signing_cert():
    cert = vault.signing_cert
    raw = request.args.get("raw", None)
    if raw is not None:
        response = make_response(cert, 200)
        response.mimetype = "text/plain"
        return response
    return render_template("user/signing_cert.html", signing_certificate=cert)


@user_blueprint.route("/github/hooks", methods=["POST"])
def github_hooks():
    if github_app.validate_webhook():
        hook = Hook(headers=dict(request.headers), payload=request.json)
        db.session.add(hook)
        db.session.commit()
        process_github_hook.delay(hook_id=hook.id)
        return jsonify({"hook_id": hook.id})
    abort(403)


@user_blueprint.route("/organizations/<org_slug>/users/add", methods=["GET", "POST"])
@login_required
def organization_add_user(org_slug):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    if not AdministerOrganizationPermission(organization.id).can():
        abort(403)

    form = AddOrganizationUserForm()
    all_users = User.query.all() if current_user.admin else None

    if form.validate_on_submit():
        if user := User.query.filter_by(email=form.email.data).first():
            organization.add_user(user)
            db.session.commit()
            flash("User has been added to the organization.", "success")
            return redirect(url_for("user.organization", org_slug=org_slug))
        else:
            flash("User with this email address does not exist.", "error")
    return render_template(
        "user/organization_add_user.html",
        organization=organization,
        form=form,
        all_users=all_users,
    )


@user_blueprint.route("/organizations/<org_slug>/users/remove", methods=["POST"])
@login_required
def organization_remove_user(org_slug):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    if not AdministerOrganizationPermission(organization.id).can():
        abort(403)

    user_id = request.form.get("user_id")
    if user := User.query.get(user_id):
        org_user_count = len(organization.members)
        if org_user_count <= 1:
            flash(
                "Cannot remove the last user from an organization. Add someone else first!",
                "warning",
            )
        else:
            organization.remove_user(user)
            db.session.commit()
            flash(
                f"User {user.email} removed from organization {organization.name}.",
                "success",
            )
    else:
        flash("User not found.", "error")
    return redirect(url_for("user.organization", org_slug=org_slug))


@user_blueprint.route("/organizations/<org_slug>/users/promote", methods=["POST"])
@login_required
def organization_promote_user(org_slug):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    if not AdministerOrganizationPermission(organization.id).can():
        abort(403)

    user_id = request.form.get("user_id")
    if user := User.query.get(user_id):
        if member := OrganizationMember.query.filter_by(
            user_id=user.id, organization_id=organization.id
        ).first():
            member.admin = True
            db.session.commit()
            flash(
                f"User {user.email} promoted to admin in {organization.name}.",
                "success",
            )
    else:
        flash("User not found.", "error")
    return redirect(url_for("user.organization", org_slug=org_slug))


@user_blueprint.route("/organizations/<org_slug>/users/demote", methods=["POST"])
@login_required
def organization_demote_user(org_slug):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    if not AdministerOrganizationPermission(organization.id).can():
        abort(403)

    user_id = request.form.get("user_id")
    if user := User.query.get(user_id):
        if member := OrganizationMember.query.filter_by(
            user_id=user.id, organization_id=organization.id
        ).first():
            member.admin = False
            db.session.commit()
            flash(
                f"User {user.email} demoted to member in {organization.name}.",
                "success",
            )
    else:
        flash("User not found.", "error")
    return redirect(url_for("user.organization", org_slug=org_slug))


def _mimir_connection():
    """Return (mimir_url, verify) tuple, or (None, None) if not configured."""
    mimir_url = current_app.config.get("MIMIR_URL")
    if not mimir_url:
        return None, None
    verify = current_app.config.get("MIMIR_VERIFY")
    if verify is not None:
        if isinstance(verify, str) and verify.lower() == "false":
            verify = False
    else:
        verify = True
    return mimir_url, verify


def _query_mimir_range(query, start, end, step):
    """Query Mimir's Prometheus-compatible query_range endpoint.

    Returns the parsed ``data.result`` list, or None on any error.
    """
    mimir_url, verify = _mimir_connection()
    if not mimir_url:
        return None
    try:
        resp = requests_lib.get(
            f"{mimir_url}/prometheus/api/v1/query_range",
            params={
                "query": query,
                "start": start,
                "end": end,
                "step": step,
            },
            verify=verify,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "success":
            return data.get("data", {}).get("result", [])
        return None
    except Exception:
        return None


def _compute_observe_namespace(application, app_env):
    """Compute the k8s namespace for an application's environment."""
    org_k8s = application.project.organization.k8s_identifier
    if app_env and app_env.k8s_identifier is not None:
        return safe_k8s_name(org_k8s, app_env.environment.k8s_identifier)
    return org_k8s


def _compute_observe_prefix(application):
    """Compute the k8s resource prefix (project-app) for pod matching."""
    return safe_k8s_name(
        application.project.k8s_identifier,
        application.k8s_identifier,
    )


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/env/<env_slug>/applications/<app_slug>/observe",
)
@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/observe",
    defaults={"env_slug": None},
)
@login_required
def project_application_observe(org_slug, project_slug, app_slug, env_slug=None):
    org, project, application = _lookup_app_context(org_slug, project_slug, app_slug)
    app_env = _resolve_app_env(
        application, env_slug=env_slug, project=project, required=False
    )
    environment = app_env.environment if app_env else None

    mimir_configured = bool(current_app.config.get("MIMIR_URL"))
    process_names = sorted(app_env.process_counts or {}) if app_env else []

    return render_template(
        "user/project_application_observe.html",
        application=application,
        environment=environment,
        app_env=app_env,
        mimir_configured=mimir_configured,
        current_range=request.args.get("range", "1h"),
        process_names=process_names,
    )


_OBSERVE_METRICS = {"cpu", "memory", "requests", "latency", "errors"}
_OBSERVE_GROUPS = {"total", "process", "pod", "status"}


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/env/<env_slug>/applications/<app_slug>/observe/metric",
)
@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/observe/metric",
    defaults={"env_slug": None},
)
@login_required
def project_application_observe_metric(org_slug, project_slug, app_slug, env_slug=None):
    metric = request.args.get("metric")
    if metric not in _OBSERVE_METRICS:
        return jsonify({"error": "invalid metric"}), 400

    group = request.args.get("group", "total")
    if group not in _OBSERVE_GROUPS:
        group = "total"

    org, project, application = _lookup_app_context(org_slug, project_slug, app_slug)
    app_env = _resolve_app_env(
        application, env_slug=env_slug, project=project, required=False
    )

    mimir_url = current_app.config.get("MIMIR_URL")
    if not mimir_url or not app_env:
        return jsonify({"error": "not configured"}), 404

    namespace = _compute_observe_namespace(application, app_env)
    prefix = _compute_observe_prefix(application)
    escaped_prefix = _REGEX_META.sub(r"\\\g<0>", prefix)

    # Optional process filter
    process_filter = request.args.get("process", "")
    if process_filter:
        escaped_process = _REGEX_META.sub(r"\\\g<0>", process_filter)
        labels = (
            f'namespace="{namespace}", pod=~"{escaped_prefix}-{escaped_process}-.*"'
        )
    else:
        labels = f'namespace="{namespace}", pod=~"{escaped_prefix}-.*"'

    # Build exact traefik service labels from ingress config.
    # nginx-ingress format: {ns}-{prefix}-{ingress.name}-{prefix}-{process}-{port}@kubernetesingressnginx
    traefik_svc_names = set()
    for ingress in app_env.ingresses:
        if not ingress.enabled:
            continue
        for path in ingress.paths:
            if process_filter and path.target_process_name != process_filter:
                continue
            traefik_svc_names.add(
                f"{namespace}-{prefix}-{ingress.name}-{prefix}-{path.target_process_name}-8000@kubernetesingressnginx"
            )
        if not ingress.paths:
            if not process_filter or process_filter == "web":
                traefik_svc_names.add(
                    f"{namespace}-{prefix}-{ingress.name}-{prefix}-web-8000@kubernetesingressnginx"
                )
    if len(traefik_svc_names) == 1:
        traefik_svc = f'service="{next(iter(traefik_svc_names))}"'
    elif traefik_svc_names:
        joined = "|".join(
            _REGEX_META.sub(r"\\\g<0>", s) for s in sorted(traefik_svc_names)
        )
        traefik_svc = f'service=~"{joined}"'
    elif process_filter:
        # Filtered process has no ingresses — no traefik metrics available
        traefik_svc = None
    else:
        traefik_svc = f'service="{namespace}-{prefix}-web-{prefix}-web-8000@kubernetesingressnginx"'

    range_param = request.args.get("range", "1h")
    range_map = {"1h": 3600, "6h": 21600, "24h": 86400}
    step_map = {"1h": 15, "6h": 60, "24h": 300}
    duration = range_map.get(range_param, 3600)
    step = step_map.get(range_param, 15)

    # Align start/end to step boundaries for clean bucket alignment
    end = int(time.time()) // step * step
    start = end - duration

    result = None
    queries = []

    by_clause = {
        "pod": "by (pod)",
        "process": "by (process)",
        "total": "",
        "status": "",
    }[group]
    # cAdvisor metrics lack a "process" label; extract it from the pod name
    process_re = f"{escaped_prefix}-(.*)-[a-z0-9]+-[a-z0-9]+"

    # Use step-aligned rate window so CPU isn't a rolling average
    rate_window = f"{max(step, 30)}s"

    if metric == "cpu":
        if group == "process":
            q = (
                f"sum by (process) (label_replace("
                f"sum by (pod) (rate(container_cpu_usage_seconds_total{{{labels}}}[{rate_window}]))"
                f', "process", "$1", "pod", "{process_re}"))'
            )
        else:
            q = f"sum(rate(container_cpu_usage_seconds_total{{{labels}}}[{rate_window}])) {by_clause}"
        queries.append(q)
        result = _query_mimir_range(q, start, end, step)
    elif metric == "memory":
        if group == "process":
            q = (
                f"sum by (process) (label_replace("
                f"sum by (pod) (container_memory_working_set_bytes{{{labels}}})"
                f', "process", "$1", "pod", "{process_re}"))'
            )
        else:
            q = f"sum(container_memory_working_set_bytes{{{labels}}}) {by_clause}"
        queries.append(q)
        result = _query_mimir_range(q, start, end, step)
    elif metric in ("requests", "errors", "latency") and traefik_svc is None:
        # Process has no ingresses — no HTTP metrics
        result = None
    elif metric == "requests":
        # Use 60s minimum bucket for clean per-minute counts
        req_step = max(step, 60)
        req_start = end - duration
        result = []
        for code_class in ["2", "3", "4", "5"]:
            q = (
                f"sum(increase(traefik_service_requests_total"
                f'{{{traefik_svc}, code=~"{code_class}.."}}[{req_step}s]))'
            )
            queries.append(q)
            qr = _query_mimir_range(q, req_start, end, req_step)
            if qr:
                for series in qr:
                    series["metric"]["code"] = f"{code_class}xx"
                result.extend(qr)
        result = result if result else None
    elif metric == "errors":
        # Use 60s minimum bucket for clean per-minute rates
        err_step = max(step, 60)
        err_start = end - duration
        if group == "status":
            # Per status code class error rates (4xx and 5xx separately)
            result = []
            for code_class, label in [("4", "4xx"), ("5", "5xx")]:
                q = (
                    f"sum(increase(traefik_service_requests_total"
                    f'{{{traefik_svc}, code=~"{code_class}.."}}[{err_step}s]))'
                    f" / sum(increase(traefik_service_requests_total"
                    f"{{{traefik_svc}}}[{err_step}s]))"
                )
                queries.append(q)
                qr = _query_mimir_range(q, err_start, end, err_step)
                if qr:
                    for series in qr:
                        series["metric"]["label"] = label
                    result.extend(qr)
            result = result if result else None
        else:
            # Total error rate (5xx only)
            q = (
                f"sum(increase(traefik_service_requests_total"
                f'{{{traefik_svc}, code=~"5.."}}[{err_step}s]))'
                f" / sum(increase(traefik_service_requests_total"
                f"{{{traefik_svc}}}[{err_step}s]))"
            )
            queries.append(q)
            result = _query_mimir_range(q, err_start, end, err_step)
            if result:
                for series in result:
                    series["metric"]["label"] = "error rate"
    elif metric == "latency":
        result = []
        for quantile in [0.5, 0.9, 0.95, 0.99]:
            q = (
                f"histogram_quantile({quantile}, sum(rate("
                f"traefik_router_request_duration_seconds_bucket"
                f"{{{traefik_svc}}}[{rate_window}])) by (le))"
            )
            queries.append(q)
            qr = _query_mimir_range(q, start, end, step)
            if qr:
                for series in qr:
                    series["metric"]["quantile"] = f"p{int(quantile * 100)}"
                result.extend(qr)
        result = result if result else None

    return jsonify({"result": result, "queries": queries})


def _query_mimir_instant(query):
    """Query Mimir's Prometheus-compatible instant query endpoint.

    Returns the parsed ``data.result`` list, or None on any error.
    """
    mimir_url, verify = _mimir_connection()
    if not mimir_url:
        return None
    try:
        resp = requests_lib.get(
            f"{mimir_url}/prometheus/api/v1/query",
            params={"query": query},
            verify=verify,
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "success":
            return data.get("data", {}).get("result", [])
        return None
    except Exception:
        return None


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/env/<env_slug>/applications/<app_slug>/live-stats",
)
@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/live-stats",
    defaults={"env_slug": None},
)
@login_required
def project_application_live_stats(org_slug, project_slug, app_slug, env_slug=None):
    org, project, application = _lookup_app_context(org_slug, project_slug, app_slug)
    app_env = _resolve_app_env(
        application, env_slug=env_slug, project=project, required=False
    )

    mimir_url = current_app.config.get("MIMIR_URL")
    if not mimir_url or not app_env:
        return jsonify({"error": "not configured"}), 404

    namespace = _compute_observe_namespace(application, app_env)
    prefix = _compute_observe_prefix(application)
    escaped_prefix = _REGEX_META.sub(r"\\\g<0>", prefix)

    # Pod status from Kubernetes API (source of truth)
    pods_total = 0
    pods_ready = 0
    pods_by_phase = {}
    running_pod_names = []
    processes = (
        {}
    )  # {process_name: {"total": N, "ready": N, "pending": N, "crashed": N}}
    try:
        api_client = kubernetes_ext.kubernetes_client
        core_api = kubernetes.client.CoreV1Api(api_client)
        label_selector = (
            f"organization={application.project.organization.slug},"
            f"project={application.project.slug},"
            f"application={application.slug}"
        )
        pod_list = core_api.list_namespaced_pod(
            namespace=namespace,
            label_selector=label_selector,
        )
        for pod in pod_list.items:
            # Skip terminating pods (deletionTimestamp is set)
            if pod.metadata.deletion_timestamp is not None:
                continue
            phase = pod.status.phase or "Unknown"
            pods_by_phase[phase] = pods_by_phase.get(phase, 0) + 1
            pods_total += 1
            if phase == "Running":
                running_pod_names.append(pod.metadata.name)
            is_ready = False
            is_crashed = False
            if pod.status.conditions:
                for cond in pod.status.conditions:
                    if cond.type == "Ready" and cond.status == "True":
                        is_ready = True
                        pods_ready += 1
                        break
            # Detect crash: check container statuses for CrashLoopBackOff/Error
            if not is_ready and pod.status.container_statuses:
                for cs in pod.status.container_statuses:
                    if cs.state and cs.state.waiting:
                        reason = cs.state.waiting.reason or ""
                        if reason in ("CrashLoopBackOff", "Error", "ImagePullBackOff"):
                            is_crashed = True
                            break
                    if cs.state and cs.state.terminated:
                        is_crashed = True
                        break
            proc = (pod.metadata.labels or {}).get("process", "unknown")
            if proc not in processes:
                processes[proc] = {"total": 0, "ready": 0, "pending": 0, "crashed": 0}
            processes[proc]["total"] += 1
            if is_ready:
                processes[proc]["ready"] += 1
            elif is_crashed:
                processes[proc]["crashed"] += 1
            else:
                processes[proc]["pending"] += 1
    except (kubernetes.client.ApiException, Exception):
        current_app.logger.debug("Failed to list pods for live stats", exc_info=True)

    end = int(time.time())
    start = end - 3600  # 1 hour
    step = 60  # 1-minute resolution

    # History: use prefix regex (includes all pods over the hour)
    cpu_history = []
    mem_history = []

    cpu_series = _query_mimir_range(
        f"sum(rate(container_cpu_usage_seconds_total"
        f'{{namespace="{namespace}", pod=~"{escaped_prefix}-.*"}}[{step}s]))',
        start,
        end,
        step,
    )
    if cpu_series and len(cpu_series) > 0:
        for ts, val in cpu_series[0].get("values", []):
            cpu_history.append([ts, round(float(val) * 1000, 1)])

    mem_series = _query_mimir_range(
        f"sum(container_memory_working_set_bytes"
        f'{{namespace="{namespace}", pod=~"{escaped_prefix}-.*"}})',
        start,
        end,
        step,
    )
    if mem_series and len(mem_series) > 0:
        for ts, val in mem_series[0].get("values", []):
            mem_history.append([ts, float(val)])

    # Current values: scoped to running pods from k8s
    cpu_val = None
    mem_val = None

    if running_pod_names:
        pod_regex = "|".join(
            _REGEX_META.sub(r"\\\g<0>", name) for name in running_pod_names
        )
        cpu_result = _query_mimir_instant(
            f"sum(rate(container_cpu_usage_seconds_total"
            f'{{namespace="{namespace}", pod=~"{pod_regex}"}}[5m]))'
        )
        if cpu_result and len(cpu_result) > 0 and cpu_result[0].get("value"):
            cpu_val = round(float(cpu_result[0]["value"][1]) * 1000, 1)

        mem_result = _query_mimir_instant(
            f"sum(container_memory_working_set_bytes"
            f'{{namespace="{namespace}", pod=~"{pod_regex}"}})'
        )
        if mem_result and len(mem_result) > 0 and mem_result[0].get("value"):
            mem_val = float(mem_result[0]["value"][1])

    return jsonify(
        {
            "cpu": cpu_val,
            "cpu_history": cpu_history,
            "memory": mem_val,
            "memory_history": mem_history,
            "pods": pods_total,
            "pods_ready": pods_ready,
            "pods_by_phase": pods_by_phase,
            "processes": processes,
        }
    )


# ---------------------------------------------------------------------------
# Loki log viewer
# ---------------------------------------------------------------------------


def _loki_connection():
    """Return (loki_url, verify) tuple, or (None, None) if not configured."""
    loki_url = current_app.config.get("LOKI_URL")
    if not loki_url:
        return None, None
    verify = current_app.config.get("LOKI_VERIFY")
    if verify is not None:
        if isinstance(verify, str) and verify.lower() == "false":
            verify = False
    else:
        verify = True
    return loki_url, verify


def _query_loki(query, start, end, limit=500, direction="backward"):
    """Query Loki's query_range endpoint.

    *start* and *end* are unix epoch **nanoseconds** (int or str).
    Returns the parsed ``data.result`` list, or None on any error.
    """
    loki_url, verify = _loki_connection()
    if not loki_url:
        return None
    try:
        resp = requests_lib.get(
            f"{loki_url}/loki/api/v1/query_range",
            params={
                "query": query,
                "start": str(start),
                "end": str(end),
                "limit": limit,
                "direction": direction,
            },
            verify=verify,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "success":
            return data.get("data", {}).get("result", [])
        return None
    except Exception:
        current_app.logger.debug("Loki query failed", exc_info=True)
        return None


def _escape_logql_line_filter(text):
    """Escape special characters for a LogQL line-filter string literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "")


def _build_log_selectors(namespace, project_slug=None, app_slug=None, env_slug=None):
    """Build LogQL label selectors for a given scope.

    Returns a list of selector strings like ``'namespace="foo"'``.
    Does NOT perform ACL checks — callers must handle that.
    """
    selectors = [
        f'namespace="{namespace}"',
    ]
    if project_slug:
        selectors.append(f'project="{project_slug}"')
    if app_slug:
        selectors.append(f'application="{app_slug}"')
    if env_slug:
        selectors.append(f'environment="{env_slug}"')
    return selectors


def _loki_query_response(selectors, process_names):
    """Shared Loki query logic.  Returns a Flask JSON response.

    *selectors* is a list of LogQL label matchers (strings).
    *process_names* is used to build a container-name filter.
    """
    loki_url, _ = _loki_connection()
    if not loki_url:
        return jsonify({"error": "not configured"}), 404

    search = request.args.get("search", "").strip()
    process = request.args.get("process", "").strip()
    time_range = request.args.get("range", "1h")
    limit = min(request.args.get("limit", 500, type=int), 5000)
    end_ns = request.args.get("end", None)
    start_param = request.args.get("start", None)

    range_seconds = {"1h": 3600, "6h": 21600, "24h": 86400}.get(time_range, 3600)

    if end_ns is not None:
        try:
            end_ns = int(end_ns)
        except (ValueError, TypeError):
            end_ns = None

    if end_ns is None:
        end_ns = int(time.time() * 1_000_000_000)

    if start_param is not None:
        try:
            start_ns = int(start_param)
        except (ValueError, TypeError):
            start_ns = end_ns - (range_seconds * 1_000_000_000)
    else:
        start_ns = end_ns - (range_seconds * 1_000_000_000)

    # Build LogQL query — filter to user process containers
    all_selectors = list(selectors)
    if process:
        all_selectors.append(f'pod_container_name="{process}"')
    elif process_names:
        # Loki regex matchers are fully anchored, so exact alternation is safe
        all_selectors.append(f'pod_container_name=~"{"|".join(process_names)}"')
    else:
        all_selectors.append('pod_container_name!~"cabotage-.*"')

    logql = "{" + ", ".join(all_selectors) + "}"

    hide_probes = request.args.get("hide_probes", "")
    if hide_probes:
        logql += r" !~ `\bkube-probe/\d+\.\d+\b`"

    if search:
        escaped = _escape_logql_line_filter(search)
        logql += f' |= "{escaped}"'

    direction = request.args.get("direction", "backward")
    if direction not in ("backward", "forward"):
        direction = "backward"

    streams = _query_loki(logql, start_ns, end_ns, limit=limit, direction=direction)
    if streams is None:
        return jsonify({"error": "query failed"}), 502

    # Flatten streams into a sorted list of log entries
    entries = []
    for stream in streams:
        labels = stream.get("stream", {})
        proc = labels.get("process", "")
        pod = labels.get("pod_name", "")
        for ts_ns, line in stream.get("values", []):
            # Unwrap container runtime log wrappers
            message = line
            log_stream = ""
            # CRI format: "<timestamp> <stream> <flags> <message>"
            # e.g. "2026-03-07T20:00:57.030Z stdout F actual log line"
            if len(line) > 36 and line[0] == "2" and line[4] == "-":
                parts = line.split(" ", 3)
                if len(parts) == 4 and parts[1] in ("stdout", "stderr"):
                    log_stream = parts[1]
                    message = parts[3]
            else:
                # Docker JSON format: {"log":"...", "stream":"...", "time":"..."}
                try:
                    parsed = json.loads(line)
                    if isinstance(parsed, dict) and "log" in parsed:
                        message = parsed["log"]
                        log_stream = parsed.get("stream", "")
                except (json.JSONDecodeError, TypeError):
                    pass
            if message.endswith("\n"):
                message = message[:-1]
            entries.append(
                {
                    "ts": ts_ns,
                    "process": proc,
                    "pod": pod,
                    "stream": log_stream,
                    "message": message,
                }
            )

    # Sort oldest first (chronological)
    entries.sort(key=lambda e: e["ts"])

    # Trim to limit
    entries = entries[:limit]

    return jsonify({"entries": entries})


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/env/<env_slug>/applications/<app_slug>/logs",
)
@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/logs",
    defaults={"env_slug": None},
)
@login_required
def project_application_logs_view(org_slug, project_slug, app_slug, env_slug=None):
    org, project, application = _lookup_app_context(org_slug, project_slug, app_slug)
    app_env = _resolve_app_env(
        application, env_slug=env_slug, project=project, required=False
    )
    environment = app_env.environment if app_env else None

    loki_configured = bool(current_app.config.get("LOKI_URL"))
    process_names = sorted(app_env.process_counts or {}) if app_env else []

    return render_template(
        "user/project_application_logs_view.html",
        application=application,
        environment=environment,
        app_env=app_env,
        loki_configured=loki_configured,
        process_names=process_names,
    )


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/env/<env_slug>/applications/<app_slug>/logs/query",
)
@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/logs/query",
    defaults={"env_slug": None},
)
@login_required
def project_application_logs_query(org_slug, project_slug, app_slug, env_slug=None):
    org, project, application = _lookup_app_context(org_slug, project_slug, app_slug)
    app_env = _resolve_app_env(
        application, env_slug=env_slug, project=project, required=False
    )

    if not app_env:
        return jsonify({"error": "not configured"}), 404

    namespace = _compute_observe_namespace(application, app_env)
    process_names = sorted(app_env.process_counts or {})
    env_slug_val = app_env.environment.slug if app_env.environment else ""
    selectors = _build_log_selectors(
        namespace,
        project_slug=application.project.slug,
        app_slug=application.slug,
        env_slug=env_slug_val,
    )
    return _loki_query_response(selectors, process_names)


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/logs",
)
@login_required
def project_logs_view(org_slug, project_slug):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    project = Project.query.filter_by(
        organization_id=organization.id, slug=project_slug
    ).first_or_404()
    if not ViewProjectPermission(project.id).can():
        abort(403)

    loki_configured = bool(current_app.config.get("LOKI_URL"))

    # Collect process names from all app_envs in the project
    process_names_set = set()
    for app in project.project_applications:
        for ae in app.application_environments:
            for proc in ae.process_counts or {}:
                process_names_set.add(proc)
    process_names = sorted(process_names_set)

    return render_template(
        "user/project_logs.html",
        organization=organization,
        project=project,
        loki_configured=loki_configured,
        process_names=process_names,
    )


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/logs/query",
)
@login_required
def project_logs_query(org_slug, project_slug):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    project = Project.query.filter_by(
        organization_id=organization.id, slug=project_slug
    ).first_or_404()
    if not ViewProjectPermission(project.id).can():
        abort(403)

    # Project logs may span multiple namespaces (org-level + env-specific),
    # so use a regex on the org k8s identifier prefix.
    selectors = [
        f'namespace=~"{_REGEX_META.sub(lambda m: chr(92) + m.group(), organization.k8s_identifier)}.*"',
        f'project="{project.slug}"',
    ]

    # Collect process names from all app_envs
    process_names_set = set()
    for app in project.project_applications:
        for ae in app.application_environments:
            for proc in ae.process_counts or {}:
                process_names_set.add(proc)
    process_names = sorted(process_names_set)

    return _loki_query_response(selectors, process_names)
