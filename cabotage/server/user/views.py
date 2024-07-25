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
)
from flask_security import (
    current_user,
    login_required,
)

import backoff
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
)
from cabotage.server.models.projects import (
    DEFAULT_POD_CLASS,
    Application,
    Configuration,
    Deployment,
    Hook,
    Image,
    Project,
    Release,
    pod_classes,
)
from cabotage.server.models.projects import activity_plugin

from cabotage.server.user.forms import (
    ApplicationScaleForm,
    CreateApplicationForm,
    CreateConfigurationForm,
    CreateOrganizationForm,
    CreateProjectForm,
    DeleteConfigurationForm,
    EditApplicationSettingsForm,
    EditConfigurationForm,
    ReleaseDeployForm,
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

Activity = activity_plugin.activity_cls
user_blueprint = Blueprint(
    "user",
    __name__,
)


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
    return render_template("user/organization.html", organization=organization)


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
            organization_id=organization.id, name=form.name.data, slug=form.slug.data
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

    return render_template("user/project.html", project=project)


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


@user_blueprint.route("/projects/<org_slug>/<project_slug>/applications/<app_slug>")
@login_required
def project_application(org_slug, project_slug, app_slug):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    project = Project.query.filter_by(
        organization_id=organization.id, slug=project_slug
    ).first_or_404()
    application = Application.query.filter_by(
        project_id=project.id, slug=app_slug
    ).first_or_404()
    if not ViewApplicationPermission(application.id).can():
        abort(403)

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
    return render_template(
        "user/project_application.html",
        application=application,
        deploy_form=ReleaseDeployForm(),
        scale_form=scale_form,
        view_releases=version_class(Release)
        .query.filter_by(application_id=application.id)
        .order_by(desc(version_class(Release).version_id))
        .limit(5),
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
        org_slug=org_slug,
        project_slug=project_slug,
        app_slug=app_slug,
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

    api_client = kubernetes_ext.kubernetes_client
    core_api_instance = kubernetes.client.CoreV1Api(api_client)

    labels = {
        "organization": organization.slug,
        "project": project.slug,
        "application": application.slug,
    }
    label_selector = ",".join([f"{k}={v}" for k, v in labels.items()])

    q = queue.Queue()

    def worker(pod_name, stream_handler):
        for line in stream_handler:
            q.put(f"{pod_name}: {line}")

    def update_pods():
        worker_threads = {}
        pod_watch = kubernetes.watch.Watch()
        for response in pod_watch.stream(
            core_api_instance.list_namespaced_pod,
            namespace=organization.slug,
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
                        pod.metadata.name.removeprefix(
                            f"{project.slug}-{application.slug}-"
                        ),
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

    return render_template(
        "user/project_application_shell.html",
        org_slug=org_slug,
        project_slug=project_slug,
        app_slug=app_slug,
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

    api_client = kubernetes_ext.kubernetes_client
    core_api_instance = kubernetes.client.CoreV1Api(api_client)

    # =============================================================================== #
    #  everything below should be replaced with the creation/monitoring of a new pod  #
    # =============================================================================== #
    labels = {
        "organization": organization.slug,
        "project": project.slug,
        "application": application.slug,
    }
    label_selector = ",".join([f"{k}={v}" for k, v in labels.items()])
    pod = core_api_instance.list_namespaced_pod(
        namespace=organization.slug, label_selector=label_selector
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
        container="web",
        stderr=True,
        stdin=True,
        stdout=True,
        tty=True,
        _preload_content=False,
    )

    while resp.is_open():
        resp.update()
        if data := ws.receive(timeout=0.01):
            print((data,))
            if data[0] == "\x00":
                resp.write_stdin(data[1:])
            elif data[0] == "\x01":
                resp.write_channel(kubernetes.stream.ws_client.RESIZE_CHANNEL, data[1:])
            else:
                print((data[0],))
        if data := resp.read_stdout(timeout=0.01):
            print((data,))
            ws.send("\x00" + data)
        if data := resp.read_stderr(timeout=0.01):
            print((data,))
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
        "user/project_application_configuration.html", configuration=configuration
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

    form = CreateConfigurationForm()
    form.application_id.choices = [
        (str(application.id), f"{organization.slug}/{project.slug}: {application.slug}")
    ]
    form.application_id.data = str(application.id)

    if form.validate_on_submit():
        configuration = Configuration(
            application_id=form.application_id.data,
            name=form.name.data,
            value=form.value.data,
            secret=form.secure.data,
            buildtime=form.buildtime.data,
        )
        try:
            key_slugs = config_writer.write_configuration(
                org_slug,
                project_slug,
                app_slug,
                configuration,
            )
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
    form.application_id.choices = [
        (
            str(configuration.application.id),
            f"{organization.slug}/{project.slug}: {application.slug}",
        )
    ]
    form.application_id.data = str(configuration.application.id)
    form.name.data = str(configuration.name)
    form.secure.data = configuration.secret

    if form.validate_on_submit():
        form.populate_obj(configuration)
        try:
            key_slugs = config_writer.write_configuration(
                org_slug,
                project_slug,
                app_slug,
                configuration,
            )
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
        return redirect(
            url_for(
                "user.project_application",
                org_slug=organization.slug,
                project_slug=project.slug,
                app_slug=application.slug,
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
    "/application/<application_id>/settings/edit", methods=["GET", "POST"]
)
@login_required
def project_application_settings(application_id):
    application = Application.query.filter_by(id=application_id).first_or_404()
    if not AdministerApplicationPermission(application.id).can():
        abort(403)

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
        return redirect(
            url_for(
                "user.project_application",
                org_slug=application.project.organization.slug,
                project_slug=application.project.slug,
                app_slug=application.slug,
            )
        )

    return render_template(
        "user/project_application_settings.html",
        form=form,
        app_url=current_app.config.get("GITHUB_APP_URL", "https://github.com"),
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

    if request.method == "GET":
        form = DeleteConfigurationForm(obj=configuration)
    else:
        form = DeleteConfigurationForm()
    form.configuration_id.data = str(configuration.id)
    form.name.data = str(configuration.name)
    form.value.data = str(configuration.value)
    form.secure.data = str(configuration.secret)

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
        try:
            config_writer.delete_configuration(
                org_slug,
                project_slug,
                app_slug,
                configuration,
            )
        except Exception :
            raise # TODO: Coordinate configuration deletion between db and config_writer
        return redirect(
            url_for(
                "user.project_application",
                org_slug=organization.slug,
                project_slug=project.slug,
                app_slug=application.slug,
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


@user_blueprint.route("/applications/<application_id>/images")
@login_required
def application_images(application_id):
    application = Application.query.filter_by(id=application_id).first_or_404()
    if not ViewApplicationPermission(application.id).can():
        abort(403)
    page = request.args.get("page", 1, type=int)
    images = application.images.order_by(Image.version.desc()).paginate(page, 20, False)
    return render_template(
        "user/application_images.html",
        page=page,
        application=application,
        images=images.items,
    )


@user_blueprint.route("/image/<image_id>")
@login_required
def image_detail(image_id):
    image = Image.query.filter_by(id=image_id).first_or_404()
    if not ViewApplicationPermission(image.application.id).can():
        abort(403)
    if image.error:
        image.image_build_log = f"{image.image_build_log}\n**Error!**"
    secret = current_app.config["REGISTRY_AUTH_SECRET"]
    docker_pull_credentials = image.docker_pull_credentials(secret)
    return render_template(
        "user/image_detail.html",
        image=image,
        docker_pull_credentials=docker_pull_credentials,
    )


@sock.route("/image/<image_id>/livelogs", bp=user_blueprint)
@login_required
def image_build_livelogs(ws, image_id):
    image = Image.query.filter_by(id=image_id).first_or_404()
    if image.error:
        abort(404)
    if not ViewApplicationPermission(image.application.id).can():
        abort(403)
    if image.image_build_log is not None:
        ws.send(f"Job Pod imagebuild-{image.build_job_id}")
        for line in image.image_build_log.split("\n"):
            ws.send(f"  {line}")
        ws.send("=================END OF LOGS=================")

    api_client = kubernetes_ext.kubernetes_client
    core_api_instance = kubernetes.client.CoreV1Api(api_client)
    batch_api_instance = kubernetes.client.BatchV1Api(api_client)

    job_name, namespace = (f"imagebuild-{image.build_job_id}", "default")

    @backoff.on_exception(
        backoff.constant,
        kubernetes.client.exceptions.ApiException,
        max_tries=20,
        interval=0.25,
    )
    def fetch_job_object():
        return batch_api_instance.read_namespaced_job(job_name, namespace)

    try:
        job_object = fetch_job_object()
    except kubernetes.client.exceptions.ApiException:
        ws.send("=================END OF LOGS=================")
        return

    label_selector = ",".join(
        [f"{k}={v}" for k, v in job_object.spec.template.metadata.labels.items()]
    )
    try:
        pods = core_api_instance.list_namespaced_pod(
            namespace, label_selector=label_selector
        )
    except kubernetes.client.exceptions.ApiException as exc:
        ws.send("=================END OF LOGS=================")
        print(f"Encountered exception: {exc}")
        return False

    if len(pods.items) != 1:
        ws.send("=================END OF LOGS=================")
        print("Found too many pods!")
        return False

    pod = pods.items[0]
    while True:
        pod = core_api_instance.read_namespaced_pod(
            pod.metadata.name, pod.metadata.namespace
        )
        if pod.status.phase == "Running":
            break
        time.sleep(1)
    ws.send(f"Job Pod imagebuild-{image.build_job_id}")
    w = kubernetes.watch.Watch()
    for line in w.stream(
        core_api_instance.read_namespaced_pod_log,
        name=pod.metadata.name,
        namespace=namespace,
        container=job_object.metadata.labels["process"],
        follow=True,
        _preload_content=False,
        pretty="true",
    ):
        ws.send(f"  {line}")

    ws.send("=================END OF LOGS=================")


@user_blueprint.route("/applications/<application_id>/releases")
@login_required
def application_releases(application_id):
    application = Application.query.filter_by(id=application_id).first_or_404()
    if not ViewApplicationPermission(application.id).can():
        abort(403)
    page = request.args.get("page", 1, type=int)
    releases = application.releases.order_by(Release.version.desc()).paginate(
        page, 20, False
    )
    return render_template(
        "user/application_releases.html",
        page=page,
        application=application,
        releases=releases.items,
    )


@user_blueprint.route("/release/<release_id>")
@login_required
def release_detail(release_id):
    release = Release.query.filter_by(id=release_id).first_or_404()
    if not ViewApplicationPermission(release.application.id).can():
        abort(403)
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
        docker_pull_credentials=docker_pull_credentials,
        image_pull_secrets=image_pull_secrets,
    )


@sock.route("/release/<release_id>/livelogs", bp=user_blueprint)
@login_required
def release_build_livelogs(ws, release_id):
    release = Release.query.filter_by(id=release_id).first_or_404()
    if release.error:
        abort(404)
    if not ViewApplicationPermission(release.application.id).can():
        abort(403)
    if release.release_build_log is not None:
        ws.send(f"Job Pod releasebuild-{release.build_job_id}")
        for line in release.release_build_log.split("\n"):
            ws.send(f"  {line}")
        ws.send("=================END OF LOGS=================")

    api_client = kubernetes_ext.kubernetes_client
    core_api_instance = kubernetes.client.CoreV1Api(api_client)
    batch_api_instance = kubernetes.client.BatchV1Api(api_client)

    job_name, namespace = (f"releasebuild-{release.build_job_id}", "default")

    @backoff.on_exception(
        backoff.constant,
        kubernetes.client.exceptions.ApiException,
        max_tries=20,
        interval=0.25,
    )
    def fetch_job_object():
        return batch_api_instance.read_namespaced_job(job_name, namespace)

    try:
        job_object = fetch_job_object()
    except kubernetes.client.exceptions.ApiException:
        ws.send("=================END OF LOGS=================")
        return

    label_selector = ",".join(
        [f"{k}={v}" for k, v in job_object.spec.template.metadata.labels.items()]
    )
    try:
        pods = core_api_instance.list_namespaced_pod(
            namespace, label_selector=label_selector
        )
    except kubernetes.client.exceptions.ApiException as exc:
        ws.send("=================END OF LOGS=================")
        print(f"Encountered exception: {exc}")
        return False

    if len(pods.items) != 1:
        ws.send("=================END OF LOGS=================")
        print("Found too many pods!")
        return False

    pod = pods.items[0]
    while True:
        pod = core_api_instance.read_namespaced_pod(
            pod.metadata.name, pod.metadata.namespace
        )
        if pod.status.phase == "Running":
            break
        time.sleep(1)
    ws.send(f"Job Pod releasebuild-{release.build_job_id}")
    w = kubernetes.watch.Watch()
    for line in w.stream(
        core_api_instance.read_namespaced_pod_log,
        name=pod.metadata.name,
        namespace=namespace,
        container=job_object.metadata.labels["process"],
        follow=True,
        _preload_content=False,
        pretty="true",
    ):
        ws.send(f"  {line}")

    ws.send("=================END OF LOGS=================")


@sock.route("/deployment/<deployment_id>/livelogs", bp=user_blueprint)
@login_required
def deployment_livelogs(ws, deployment_id):
    deployment = Deployment.query.filter_by(id=deployment_id).first_or_404()
    if not ViewApplicationPermission(deployment.application.id).can():
        abort(403)

    if deployment.deploy_log is not None:
        ws.send(f"Job Pod deployment-{deployment.job_id}")
        for line in deployment.deploy_log.split("\n"):
            ws.send(f"  {line}")
        ws.send("=================END OF LOGS=================")

    api_client = kubernetes_ext.kubernetes_client
    core_api_instance = kubernetes.client.CoreV1Api(api_client)
    batch_api_instance = kubernetes.client.BatchV1Api(api_client)

    job_name, namespace = (
        f"deployment-{deployment.job_id}",
        deployment.application.project.organization.slug,
    )

    @backoff.on_exception(
        backoff.constant,
        kubernetes.client.exceptions.ApiException,
        max_tries=20,
        interval=0.25,
    )
    def fetch_job_object():
        return batch_api_instance.read_namespaced_job(job_name, namespace)

    try:
        job_object = fetch_job_object()
    except kubernetes.client.exceptions.ApiException:
        ws.send("=================END OF LOGS=================")
        return

    label_selector = ",".join(
        [f"{k}={v}" for k, v in job_object.spec.template.metadata.labels.items()]
    )
    try:
        pods = core_api_instance.list_namespaced_pod(
            namespace, label_selector=label_selector
        )
    except kubernetes.client.exceptions.ApiException as exc:
        ws.send("=================END OF LOGS=================")
        print(f"Encountered exception: {exc}")
        return False

    if len(pods.items) != 1:
        ws.send("=================END OF LOGS=================")
        print("Found too many pods!")
        return False

    pod = pods.items[0]
    while True:
        try:
            pod = core_api_instance.read_namespaced_pod(
                pod.metadata.name, pod.metadata.namespace
            )
            if pod.status.phase == "Running":
                break
            time.sleep(0.25)
        except kubernetes.client.exceptions.ApiException as exc:
            ws.send("=================END OF LOGS=================")
            print(f"Encountered exception: {exc}")
            return False

    ws.send(f"Job Pod deployment-{deployment.job_id}")
    w = kubernetes.watch.Watch()
    for line in w.stream(
        core_api_instance.read_namespaced_pod_log,
        name=pod.metadata.name,
        namespace=namespace,
        container=job_object.metadata.labels["process"],
        follow=True,
        _preload_content=False,
        pretty="true",
    ):
        ws.send(f"  {line}")

    ws.send("=================END OF LOGS=================")


@user_blueprint.route("/deployment/<deployment_id>")
@login_required
def deployment_detail(deployment_id):
    deployment = Deployment.query.filter_by(id=deployment_id).first_or_404()
    if not ViewApplicationPermission(deployment.application.id).can():
        abort(403)
    return render_template("user/deployment_detail.html", deployment=deployment)


@user_blueprint.route(
    "/applications/<application_id>/release/create", methods=["GET", "POST"]
)
@login_required
def application_release_create(application_id):
    application = Application.query.filter_by(id=application_id).first_or_404()
    if not ViewApplicationPermission(application.id).can():
        abort(403)

    release = application.create_release()
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
    return redirect(url_for("user.release_detail", release_id=release.id))


@user_blueprint.route("/docker/auth")
def docker_auth():
    secret = current_app.config["REGISTRY_AUTH_SECRET"]
    password = request.authorization.password
    scope = request.args.get("scope", "registry:catalog:*")
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
    "/applications/<application_id>/images/fromsource", methods=["POST"]
)
@login_required
def application_images_build_fromsource(application_id):
    application = Application.query.filter_by(id=application_id).first_or_404()
    if not AdministerApplicationPermission(application.id).can():
        abort(403)
    project = application.project
    organization = application.project.organization

    organization_slug = organization.slug
    project_slug = project.slug
    application_slug = application.slug
    repository_name = f"cabotage/{organization_slug}/{project_slug}/{application_slug}"

    image = Image(
        application_id=application.id,
        repository_name=repository_name,
        build_ref=application.auto_deploy_branch,
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
    return redirect(url_for("user.image_detail", image_id=image.id))


@user_blueprint.route("/applications/<application_id>/clearcache", methods=["POST"])
@login_required
def application_clear_cache(application_id):
    application = Application.query.filter_by(id=application_id).first_or_404()
    if not AdministerApplicationPermission(application.id).can():
        abort(403)

    project = application.project
    organization = application.project.organization

    organization_slug = organization.slug
    project_slug = project.slug
    application_slug = application.slug
    repository_name = f"cabotage/{organization_slug}/{project_slug}/{application_slug}"

    api_client = kubernetes_ext.kubernetes_client
    core_api_instance = kubernetes.client.CoreV1Api(api_client)
    batch_api_instance = kubernetes.client.BatchV1Api(api_client)
    image = application.images.first()
    if image is not None and current_app.config["KUBERNETES_ENABLED"]:
        from cabotage.celery.tasks.deploy import run_job
        from cabotage.celery.tasks.build import fetch_image_build_cache_volume_claim

        volume_claim = fetch_image_build_cache_volume_claim(core_api_instance, image)
        job_object = kubernetes.client.V1Job(
            metadata=kubernetes.client.V1ObjectMeta(
                name=f"clear-cache-{volume_claim.metadata.name}",
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
                    ),
                    spec=kubernetes.client.V1PodSpec(
                        restart_policy="Never",
                        security_context=kubernetes.client.V1PodSecurityContext(
                            fs_group=1000,
                        ),
                        containers=[
                            kubernetes.client.V1Container(
                                name="clear-cache",
                                image="busybox",
                                command=["find", "/build-cache", "-delete"],
                                security_context=kubernetes.client.V1SecurityContext(
                                    seccomp_profile=kubernetes.client.V1SeccompProfile(
                                        type="Unconfined",
                                    ),
                                    run_as_user=1000,
                                    run_as_group=1000,
                                ),
                                volume_mounts=[
                                    kubernetes.client.V1VolumeMount(
                                        mount_path="/build-cache",
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

    return redirect(
        url_for(
            "user.project_application",
            org_slug=application.project.organization.slug,
            project_slug=application.project.slug,
            app_slug=application.slug,
        )
    )


@user_blueprint.route("/application/<application_id>/scale", methods=["POST"])
@login_required
def application_scale(application_id):
    application = Application.query.filter_by(id=application_id).first_or_404()
    if not AdministerApplicationPermission(application.id).can():
        abort(403)
    form = ApplicationScaleForm()
    form.application_id.data = str(application.id)
    if form.validate_on_submit():
        scaled = collections.defaultdict(dict)
        for key, value in request.form.items():
            if key.startswith("process-count-"):
                process_name = key[len("process-count-") :]
                if application.process_counts.get(process_name, 0) != int(value):
                    scaled[process_name]["process_count"] = {
                        "old_value": application.process_counts.get(process_name, 0),
                        "new_value": int(value),
                    }
                    application.process_counts[process_name] = int(value)
                    flag_modified(application, "process_counts")
            if key.startswith("process-pod-class-"):
                if application.process_pod_classes.get(process_name, 0) != value:
                    scaled[process_name]["pod_class"] = {
                        "old_value": application.process_pod_classes.get(
                            process_name, DEFAULT_POD_CLASS
                        ),
                        "new_value": value,
                    }
                    application.process_pod_classes[process_name] = value
                    flag_modified(application, "process_pod_classes")
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
            db.session.add(application)
            db.session.add(activity)
            db.session.commit()

            if current_app.config["KUBERNETES_ENABLED"]:
                for process_name, change in scaled.items():
                    if "process_count" in change.keys():
                        scale_deployment(
                            application.project.organization.slug,
                            application.latest_release,
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
        )
    )


@user_blueprint.route("/release/<release_id>/deploy", methods=["POST"])
@login_required
def release_deploy(release_id):
    release = Release.query.filter_by(id=release_id).first_or_404()
    if not AdministerApplicationPermission(release.application.id).can():
        abort(403)
    deployment = Deployment(
        application_id=release.application.id,
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
    return redirect(url_for("user.deployment_detail", deployment_id=deployment.id))


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
