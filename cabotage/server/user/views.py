import collections
import datetime
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
from requests.exceptions import HTTPError
from sqlalchemy import desc
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
    Project,
    Release,
    pod_classes,
)
from cabotage.server.models.projects import activity_plugin
from cabotage.server.models.utils import safe_k8s_name, slugify

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

from cabotage.celery.tasks.deploy import scale_deployment
from cabotage.utils.build_log_stream import (
    get_redis_client,
    read_log_stream,
    stream_key,
)

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
    user = current_user
    organizations = user.organizations
    return render_template("user/organizations.html", organizations=organizations)


@user_blueprint.route("/organizations/<org_slug>")
@login_required
def organization(org_slug):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    if not ViewOrganizationPermission(organization.id).can():
        abort(403)
    project_create_form = CreateProjectForm()
    project_create_form.organization_id.choices = [
        (str(organization.id), organization.name)
    ]
    project_create_form.organization_id.data = str(organization.id)
    org_app_count = sum(len(p.project_applications) for p in organization.projects)
    org_deploy_count = sum(
        app.deployments.filter_by(complete=True).count()
        for p in organization.projects
        for app in p.project_applications
    )
    return render_template(
        "user/organization.html",
        organization=organization,
        project_create_form=project_create_form,
        org_app_count=org_app_count,
        org_deploy_count=org_deploy_count,
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
    return render_template("user/projects.html", projects=current_user.projects)


@user_blueprint.route("/projects/<org_slug>/<project_slug>")
@login_required
def project(org_slug, project_slug):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    project = Project.query.filter_by(
        organization_id=organization.id, slug=project_slug
    ).first_or_404()
    if not ViewProjectPermission(project.id).can():
        abort(403)
    app_create_form = CreateApplicationForm()
    app_create_form.organization_id.choices = [
        (str(organization.id), organization.name)
    ]
    app_create_form.project_id.choices = [(str(project.id), project.name)]
    app_create_form.organization_id.data = str(organization.id)
    app_create_form.project_id.data = str(project.id)
    proj_deploy_count = sum(
        app.deployments.filter_by(complete=True).count()
        for app in project.project_applications
    )
    unassigned_apps = []
    if project.environments_enabled:
        assigned_app_ids = set()
        for env in project.project_environments:
            for ae in env.application_environments:
                assigned_app_ids.add(ae.application_id)
        unassigned_apps = [
            a for a in project.project_applications if a.id not in assigned_app_ids
        ]
    return render_template(
        "user/project.html",
        project=project,
        app_create_form=app_create_form,
        proj_deploy_count=proj_deploy_count,
        unassigned_apps=unassigned_apps,
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

    envs = project.project_environments
    non_default_envs = [e for e in envs if not e.is_default]
    can_disable_environments = (
        project.environments_enabled and len(non_default_envs) == 0
    )

    if form.validate_on_submit():
        if (
            project.environments_enabled
            and not form.environments_enabled.data
            and not can_disable_environments
        ):
            flash(
                "Cannot disable environments while non-default environments exist.",
                "error",
            )
            return redirect(
                url_for(
                    "user.project_settings",
                    org_slug=organization.slug,
                    project_slug=project.slug,
                )
            )
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
                    default_env.slug = slugify(initial_name)
            # Existing app_envs keep k8s_identifier=NULL so all their paths
            # (registry, namespace, consul/vault, build cache) remain unchanged.
        form.populate_obj(project)
        env_order = request.form.getlist("env_order")
        if env_order:
            env_map = {str(e.id): e for e in project.project_environments}
            for i, eid in enumerate(env_order):
                if eid in env_map:
                    env_map[eid].sort_order = i
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
    return render_template(
        "user/project_environments.html",
        project=project,
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
    if form.validate_on_submit():
        if form.is_default.data:
            for env in project.project_environments:
                env.is_default = False
        environment = Environment(
            project_id=project.id,
            name=form.name.data,
            slug=form.slug.data,
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
        if form.is_default.data and not environment.is_default:
            for env in project.project_environments:
                env.is_default = False
        environment.name = form.name.data
        environment.is_default = form.is_default.data
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

    if app_env is not None:
        releases = app_env.releases.order_by(Release.version.desc()).limit(10).all()
        images = app_env.images.order_by(Image.version.desc()).limit(10).all()
        deployments = (
            app_env.deployments.order_by(Deployment.created.desc()).limit(10).all()
        )
    else:
        releases = []
        images = []
        deployments = []

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
        releases=releases,
        images=images,
        deployments=deployments,
        DEFAULT_POD_CLASS=DEFAULT_POD_CLASS,
        pod_classes=pod_classes,
        pod_class_info=pod_class_info,
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
@login_required
def project_application_shell(org_slug, project_slug, app_slug):
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

    app_env = _resolve_app_env(application)

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
        environment=_default_environment(project),
    )


@sock.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/shell/socket",
    bp=user_blueprint,
)
@login_required
def project_application_shell_socket(ws, org_slug, project_slug, app_slug):
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

    app_env = _resolve_app_env(application)
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
    label_selector = ",".join([f"{k}={v}" for k, v in labels.items()])
    pod = core_api_instance.list_namespaced_pod(
        namespace=org_slug, label_selector=label_selector
    ).items[0]

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
                "envconsul -config /etc/cabotage/envconsul-shell.hcl /bin/bash"
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

    if form.validate_on_submit():
        application = Application(
            project_id=form.project_id.data, name=form.name.data, slug=form.slug.data
        )
        db.session.add(application)
        db.session.flush()

        if project.environments_enabled:
            # Env-enabled: app is a shell until added to environments
            # via the environment page's "Add Application" form.
            pass
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
        configuration = Configuration(
            application_id=form.application_id.data,
            application_environment_id=app_env.id,
            name=form.name.data,
            value=form.value.data,
            secret=form.secure.data,
            buildtime=form.buildtime.data,
        )
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
        form.populate_obj(configuration)
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
    return render_template(
        "user/application_releases.html",
        page=page,
        application=application,
        releases=releases.items,
        environment=app_env.environment if project.environments_enabled else None,
        app_env=app_env,
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
                        if "process_count" in change.keys():
                            scale_deployment(
                                namespace,
                                latest,
                                process_name,
                                change["process_count"]["new_value"],
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
