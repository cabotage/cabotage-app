import collections
import os
import datetime
import time
import threading
import queue

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_security import (
    current_user,
    login_required,
)

import kubernetes

from sqlalchemy import desc
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy_continuum import version_class

from cabotage.server import (
    config_writer,
    db,
    github_app,
    kubernetes as kubernetes_ext,
    minio,
    vault,
    sock,
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
    EditApplicationDeployAutomationForm,
    EditConfigurationForm,
    ImageBuildSubmitForm,
    ReleaseDeployForm,
)

from cabotage.utils.docker_auth import (
    check_docker_credentials,
    generate_docker_credentials,
    generate_docker_registry_jwt,
    parse_docker_scope,
    docker_access_intersection,
)

from cabotage.celery.tasks import (
    is_this_thing_on,
    process_github_hook,
    run_deploy,
    run_image_build,
    run_release_build,
)

from cabotage.celery.tasks.deploy import scale_deployment

Activity = activity_plugin.activity_cls
user_blueprint = Blueprint('user', __name__,)


@user_blueprint.route('/organizations')
@login_required
def organizations():
    user = current_user
    organizations = user.organizations
    return render_template('user/organizations.html', organizations=organizations)


@user_blueprint.route('/organizations/<org_slug>')
@login_required
def organization(org_slug):
    user = current_user
    organization = Organization.query.filter_by(slug=org_slug).first()
    if organization is None:
        abort(404)
    return render_template('user/organization.html', organization=organization)


@user_blueprint.route('/organizations/create', methods=["GET", "POST"])
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
            verb='create',
            object=organization,
            data={
                'user_id': str(current_user.id),
                'timestamp': datetime.datetime.utcnow().isoformat(),
            }
        )
        db.session.add(org_create)
        db.session.commit()
        return redirect(url_for('user.organization', org_slug=organization.slug))
    return render_template('user/organization_create.html', organization_create_form=form)


@user_blueprint.route('/organizations/<org_slug>/projects')
@login_required
def organization_projects(org_slug):
    user = current_user
    organization = Organization.query.filter_by(slug=org_slug).first()
    if organization is None:
        abort(404)
    return render_template('user/organization_projects.html', organization=organization)


@user_blueprint.route('/organizations/<org_slug>/projects/create', methods=["GET", "POST"])
@login_required
def organization_project_create(org_slug):
    user = current_user
    organization = Organization.query.filter_by(slug=org_slug).first()
    if organization is None:
        abort(404)

    form = CreateProjectForm()
    form.organization_id.choices = [(str(organization.id), organization.name)]
    form.organization_id.data = str(organization.id)

    if form.validate_on_submit():
        project = Project(organization_id=organization.id, name=form.name.data, slug=form.slug.data)
        db.session.add(project)
        db.session.flush()
        activity = Activity(
            verb='create',
            object=project,
            data={
                'user_id': str(current_user.id),
                'timestamp': datetime.datetime.utcnow().isoformat(),
            }
        )
        db.session.add(activity)
        db.session.commit()
        return redirect(url_for('user.project', org_slug=project.organization.slug, project_slug=project.slug))
    return render_template('user/organization_project_create.html', organization=organization, organization_project_create_form=form)


@user_blueprint.route('/projects')
@login_required
def projects():
    return render_template('user/projects.html', projects=current_user.projects)


@user_blueprint.route('/projects/<org_slug>/<project_slug>')
@login_required
def project(org_slug, project_slug):
    organization = Organization.query.filter_by(slug=org_slug).first()
    if organization is None:
        abort(404)
    project = Project.query.filter_by(organization_id=organization.id, slug=project_slug).first()
    if project is None:
        abort(404)
    return render_template('user/project.html', project=project)


@user_blueprint.route('/projects/create', methods=["GET", "POST"])
@login_required
def project_create():
    user = current_user
    form = CreateProjectForm()
    form.organization_id.choices = [(str(o.organization_id), o.organization.name) for o in user.organizations]

    if form.validate_on_submit():
        project = Project(organization_id=form.organization_id.data, name=form.name.data, slug=form.slug.data)
        db.session.add(project)
        db.session.flush()
        activity = Activity(
            verb='create',
            object=project,
            data={
                'user_id': str(current_user.id),
                'timestamp': datetime.datetime.utcnow().isoformat(),
            }
        )
        db.session.add(activity)
        db.session.commit()
        return redirect(url_for('user.project', org_slug=project.organization.slug, project_slug=project.slug))
    return render_template('user/project_create.html', project_create_form=form)


@user_blueprint.route('/projects/<org_slug>/<project_slug>/applications/<app_slug>')
@login_required
def project_application(org_slug, project_slug, app_slug):
    organization = Organization.query.filter_by(slug=org_slug).first()
    if organization is None:
        abort(404)
    project = Project.query.filter_by(organization_id=organization.id, slug=project_slug).first()
    if project is None:
        abort(404)
    application = Application.query.filter_by(project_id=project.id, slug=app_slug).first()
    if application is None:
        abort(404)

    pod_class_info = '<table class="table"><tr><th>Class</th><th>CPU</th><th>Mem</th></tr>'
    for pod_class, parameters in pod_classes.items():
        pod_class_info += f'<tr><td>{pod_class}</td><td>{parameters["cpu"]["requests"]}</td><td>{parameters["memory"]["requests"]}</td></tr>'
    pod_class_info += '</table>'

    scale_form = ApplicationScaleForm()
    scale_form.application_id.data = str(application.id)
    return render_template(
        'user/project_application.html',
        application=application,
        deploy_form=ReleaseDeployForm(),
        scale_form=scale_form,
        view_releases=version_class(Release).query.filter_by(application_id=application.id).order_by(desc(version_class(Release).version_id)).limit(5),
        DEFAULT_POD_CLASS=DEFAULT_POD_CLASS,
        pod_classes=pod_classes,
        pod_class_info=pod_class_info,
    )

@user_blueprint.route('/projects/<org_slug>/<project_slug>/applications/<app_slug>/logs')
@login_required
def project_application_logs(org_slug, project_slug, app_slug):
    return render_template('user/project_application_logs.html', org_slug=org_slug, project_slug=project_slug, app_slug=app_slug)


@sock.route('/projects/<org_slug>/<project_slug>/applications/<app_slug>/logs/live', bp=user_blueprint)
@login_required
def project_application_livelogs(ws, org_slug, project_slug, app_slug):
    organization = Organization.query.filter_by(slug=org_slug).first()
    if organization is None:
        abort(404)
    project = Project.query.filter_by(organization_id=organization.id, slug=project_slug).first()
    if project is None:
        abort(404)
    application = Application.query.filter_by(project_id=project.id, slug=app_slug).first()
    if application is None:
        abort(404)

    api_client = kubernetes_ext.kubernetes_client
    core_api_instance = kubernetes.client.CoreV1Api(api_client)

    labels = {
        'organization': organization.slug,
        'project': project.slug,
        'application': application.slug,
    }
    label_selector = ','.join([f'{k}={v}' for k, v in labels.items()])

    q = queue.Queue()

    def worker(pod_name, stream_handler):
        for line in stream_handler:
            q.put(f'{pod_name}: {line}')

    def update_pods():
        worker_threads = {}
        pod_watch = kubernetes.watch.Watch()
        for response in pod_watch.stream(core_api_instance.list_namespaced_pod, namespace=organization.slug, label_selector=label_selector):
            pod = response['object']
            create = (
                (response['type'] == "ADDED" and pod.status.phase == "Running") or
                (response['type'] == "MODIFIED" and pod.status.phase == "Running" and pod.metadata.name not in worker_threads.keys())
            )
            if create:
                w = kubernetes.watch.Watch()
                stream_handler = w.stream(core_api_instance.read_namespaced_pod_log, name=pod.metadata.name, namespace=pod.metadata.namespace, container=pod.metadata.labels['process'], follow=True, _preload_content=False, pretty="true", timestamps=True, tail_lines=10)
                thread = threading.Thread(target=worker, args=(pod.metadata.name.lstrip(f'{organization.slug}-{application.slug}-'), stream_handler), daemon=True)
                worker_threads[pod.metadata.name] = thread
                q.put(f'started following {pod.metadata.name}...')
                thread.start()
            if response['type'] == "DELETED":
                if pod.metadata.name in worker_threads.keys() and not worker_threads[pod.metadata.name].is_alive():
                    worker_threads[pod.metadata.name].join()
                    q.put(f'{pod.metadata.name} terminated...')
                    del(worker_threads[pod.metadata.name])

    update_thread = threading.Thread(target=update_pods, daemon=True)
    update_thread.start()

    while True:
        ws.send(q.get())


@user_blueprint.route('/projects/<org_slug>/<project_slug>/applications/create', methods=["GET", "POST"])
@login_required
def project_application_create(org_slug, project_slug):
    user = current_user
    form = CreateApplicationForm()
    organization = Organization.query.filter_by(slug=org_slug).first()
    if organization is None:
        abort(404)
    project = Project.query.filter_by(organization_id=organization.id, slug=project_slug).first()
    if project is None:
        abort(404)

    form.organization_id.choices = [(str(organization.id), organization.name)]
    form.project_id.choices = [(str(project.id), project.name)]
    form.organization_id.data = str(organization.id)
    form.project_id.data = str(project.id)

    if form.validate_on_submit():
        application = Application(project_id=form.project_id.data, name=form.name.data, slug=form.slug.data)
        db.session.add(application)
        db.session.flush()
        activity = Activity(
            verb='create',
            object=application,
            data={
                'user_id': str(current_user.id),
                'timestamp': datetime.datetime.utcnow().isoformat(),
            }
        )
        db.session.add(activity)
        db.session.commit()
        return redirect(url_for('user.project_application', org_slug=project.organization.slug, project_slug=project.slug, app_slug=application.slug))
    return render_template('user/project_application_create.html', project_application_create_form=form, org_slug=org_slug, project_slug=project_slug)


@user_blueprint.route('/projects/<org_slug>/<project_slug>/applications')
@login_required
def project_applications(org_slug, project_slug):
    organization = Organization.query.filter_by(slug=org_slug).first()
    if organization is None:
        abort(404)
    project = Project.query.filter_by(organization_id=organization.id, slug=project_slug).first()
    if project is None:
        abort(404)
    return render_template('user/project_applications.html', project=project)


@user_blueprint.route('/projects/<org_slug>/<project_slug>/applications/<app_slug>/config/<config_id>')
@login_required
def project_application_configuration(org_slug, project_slug, app_slug, config_id):
    organization = Organization.query.filter_by(slug=org_slug).first()
    if organization is None:
        abort(404)
    project = Project.query.filter_by(organization_id=organization.id, slug=project_slug).first()
    if project is None:
        abort(404)
    application = Application.query.filter_by(project_id=project.id, slug=app_slug).first()
    if application is None:
        abort(404)
    configuration = Configuration.query.filter_by(application_id=application.id, id=config_id).first()
    if configuration is None:
        abort(404)

    return render_template('user/project_application_configuration.html', configuration=configuration)


@user_blueprint.route('/projects/<org_slug>/<project_slug>/applications/<app_slug>/config/create', methods=['GET', 'POST'])
@login_required
def project_application_configuration_create(org_slug, project_slug, app_slug):
    organization = Organization.query.filter_by(slug=org_slug).first()
    if organization is None:
        abort(404)
    project = Project.query.filter_by(organization_id=organization.id, slug=project_slug).first()
    if project is None:
        abort(404)
    application = Application.query.filter_by(project_id=project.id, slug=app_slug).first()
    if application is None:
        abort(404)

    form = CreateConfigurationForm()
    form.application_id.choices = [(str(application.id), f'{organization.slug}/{project.slug}: {application.slug}')]
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
        except Exception as exc:
            raise  # No, we should def not do this
        configuration.key_slug = key_slugs['config_key_slug']
        configuration.build_key_slug = key_slugs['build_key_slug']
        if configuration.secret:
            configuration.value = '**secure**'
        db.session.add(configuration)
        db.session.flush()
        activity = Activity(
            verb='create',
            object=configuration,
            data={
                'user_id': str(current_user.id),
                'timestamp': datetime.datetime.utcnow().isoformat(),
            }
        )
        db.session.add(activity)
        db.session.commit()
        return redirect(url_for('user.project_application', org_slug=organization.slug, project_slug=project.slug, app_slug=application.slug))
    return render_template('user/project_application_configuration_create.html', form=form, org_slug=organization.slug, project_slug=project.slug, app_slug=application.slug)


@user_blueprint.route('/projects/<org_slug>/<project_slug>/applications/<app_slug>/config/<config_id>/edit', methods=['GET', 'POST'])
@login_required
def project_application_configuration_edit(org_slug, project_slug, app_slug, config_id):
    organization = Organization.query.filter_by(slug=org_slug).first()
    if organization is None:
        abort(404)
    project = Project.query.filter_by(organization_id=organization.id, slug=project_slug).first()
    if project is None:
        abort(404)
    application = Application.query.filter_by(project_id=project.id, slug=app_slug).first()
    if application is None:
        abort(404)
    configuration = Configuration.query.filter_by(application_id=application.id, id=config_id).first()
    if configuration is None:
        abort(404)

    form = EditConfigurationForm(obj=configuration)
    form.application_id.choices = [(str(configuration.application.id), f'{organization.slug}/{project.slug}: {application.slug}')]
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
        except Exception as exc:
            raise  # No, we should def not do this
        configuration.key_slug = key_slugs['config_key_slug']
        configuration.build_key_slug = key_slugs['build_key_slug']
        if configuration.secret:
            configuration.value = '**secure**'
        db.session.flush()
        activity = Activity(
            verb='edit',
            object=configuration,
            data={
                'user_id': str(current_user.id),
                'timestamp': datetime.datetime.utcnow().isoformat(),
            }
        )
        db.session.add(activity)
        db.session.commit()
        return redirect(url_for('user.project_application', org_slug=organization.slug, project_slug=project.slug, app_slug=application.slug))

    if configuration.secret:
        form.value.data = None

    return render_template('user/project_application_configuration_edit.html', form=form, org_slug=organization.slug, project_slug=project.slug, app_slug=application.slug, configuration=configuration)


@user_blueprint.route('/application/<application_id>/deploy_automation/edit', methods=['GET', 'POST'])
@login_required
def project_application_deployment_automation(application_id):
    application = Application.query.filter_by(id=application_id).first()
    if application is None:
        abort(404)
    form = EditApplicationDeployAutomationForm(obj=application)
    form.application_id.choices = [(str(application.id), f'{application.project.organization.slug}/{application.project.slug}: {application.slug}')]
    form.application_id.data = str(application.id)

    if form.validate_on_submit():
        form.populate_obj(application)
        db.session.flush()
        activity = Activity(
            verb='edit',
            object=application,
            data={
                'user_id': str(current_user.id),
                'timestamp': datetime.datetime.utcnow().isoformat(),
            }
        )
        db.session.add(activity)
        db.session.commit()
        return redirect(url_for('user.project_application', org_slug=application.project.organization.slug, project_slug=application.project.slug, app_slug=application.slug))

    return render_template('user/project_application_deploy_automation.html', form=form, app_url=current_app.config.get('GITHUB_APP_URL', 'https://github.com'))


@user_blueprint.route('/projects/<org_slug>/<project_slug>/applications/<app_slug>/config/<config_id>/delete', methods=['GET', 'POST'])
@login_required
def project_application_configuration_delete(org_slug, project_slug, app_slug, config_id):
    organization = Organization.query.filter_by(slug=org_slug).first()
    if organization is None:
        abort(404)
    project = Project.query.filter_by(organization_id=organization.id, slug=project_slug).first()
    if project is None:
        abort(404)
    application = Application.query.filter_by(project_id=project.id, slug=app_slug).first()
    if application is None:
        abort(404)
    configuration = Configuration.query.filter_by(application_id=application.id, id=config_id).first()
    if configuration is None:
        abort(404)

    if request.method == 'GET':
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
            verb='delete',
            object=configuration,
            data={
                'user_id': str(current_user.id),
                'timestamp': datetime.datetime.utcnow().isoformat(),
            }
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
        except Exception as exc:
            pass  # No, we should def not do this
        return redirect(url_for('user.project_application', org_slug=organization.slug, project_slug=project.slug, app_slug=application.slug))
    return render_template('user/project_application_configuration_delete.html', form=form, org_slug=organization.slug, project_slug=project.slug, app_slug=application.slug, configuration=configuration)


@user_blueprint.route('/applications/<application_id>/images')
@login_required
def application_images(application_id):
    application = Application.query.filter_by(id=application_id).first()
    if application is None:
        abort(404)
    page = request.args.get('page', 1, type=int)
    images = application.images.order_by(Image.version.desc()).paginate(page, 20, False)
    return render_template('user/application_images.html', page=page, application=application, images=images.items)


@user_blueprint.route('/image/<image_id>')
@login_required
def image_detail(image_id):
    image = Image.query.filter_by(id=image_id).first()
    if image is None:
        abort(404)
    if image.error:
        image.image_build_log = f"{image.image_build_log}\n**Error!**"
    secret = current_app.config['REGISTRY_AUTH_SECRET']
    docker_pull_credentials = image.docker_pull_credentials(secret)
    return render_template('user/image_detail.html', image=image, docker_pull_credentials=docker_pull_credentials)


@sock.route('/image/<image_id>/livelogs', bp=user_blueprint)
@login_required
def image_build_livelogs(ws, image_id):
    image = Image.query.filter_by(id=image_id).first()
    if image is None or image.error:
        abort(404)
    if image.image_build_log is not None:
        for line in image.image_build_log.split('\n'):
            ws.send(line)
        ws.send('=================END OF LOGS=================')

    api_client = kubernetes_ext.kubernetes_client
    core_api_instance = kubernetes.client.CoreV1Api(api_client)
    batch_api_instance = kubernetes.client.BatchV1Api(api_client)

    job_name, namespace = (f'imagebuild-{image.build_job_id}', 'default')

    job_object = None
    while job_object is None:
        try:
            job_object = batch_api_instance.read_namespaced_job(job_name, namespace)
        except kubernetes.client.exceptions.ApiException as exc:
            print(f'pod not ready yet... {exc}')
        time.sleep(.25)

    label_selector = ','.join([f'{k}={v}' for k, v in job_object.metadata.labels.items()])
    try:
        pods = core_api_instance.list_namespaced_pod(namespace, label_selector=label_selector)
    except kubernetes.client.exceptions.ApiException as exc:
        print(f'Encountered exception: {exc}')
        return False

    if len(pods.items) != 1:
        print(f'Found too many pods!')
        return False

    pod = pods.items[0]
    while True:
        pod = core_api_instance.read_namespaced_pod(pod.metadata.name, pod.metadata.namespace)
        if pod.status.phase == 'Running':
            break
        time.sleep(1)
    w = kubernetes.watch.Watch()
    for line in w.stream(core_api_instance.read_namespaced_pod_log, name=pod.metadata.name, namespace=namespace, container=job_object.metadata.labels['process'], follow=True, _preload_content=False, pretty="true"):
        ws.send(line)

    ws.send('=================END OF LOGS=================')

@user_blueprint.route('/applications/<application_id>/releases')
@login_required
def application_releases(application_id):
    application = Application.query.filter_by(id=application_id).first()
    if application is None:
        abort(404)
    page = request.args.get('page', 1, type=int)
    releases = application.releases.order_by(Release.version.desc()).paginate(page, 20, False)
    return render_template('user/application_releases.html', page=page, application=application, releases=releases.items)


@user_blueprint.route('/release/<release_id>')
@login_required
def release_detail(release_id):
    release = Release.query.filter_by(id=release_id).first()
    if release is None:
        abort(404)
    secret = current_app.config['REGISTRY_AUTH_SECRET']
    docker_pull_credentials = release.docker_pull_credentials(secret)
    image_pull_secrets = release.image_pull_secrets(secret, registry_urls=[current_app.config['REGISTRY_PULL'], current_app.config['REGISTRY_BUILD']])
    return render_template('user/release_detail.html', release=release, docker_pull_credentials=docker_pull_credentials, image_pull_secrets=image_pull_secrets)


@user_blueprint.route('/deployment/<deployment_id>')
@login_required
def deployment_detail(deployment_id):
    deployment = Deployment.query.filter_by(id=deployment_id).first()
    if deployment is None:
        abort(404)
    return render_template('user/deployment_detail.html', deployment=deployment)


@user_blueprint.route('/applications/<application_id>/release/create', methods=['GET', 'POST'])
@login_required
def application_release_create(application_id):
    application = Application.query.filter_by(id=application_id).first()
    if application is None:
        abort(404)

    release = application.create_release()
    db.session.add(release)
    db.session.flush()
    activity = Activity(
        verb='edit',
        object=release,
        data={
            'user_id': str(current_user.id),
            'timestamp': datetime.datetime.utcnow().isoformat(),
        }
    )
    db.session.add(activity)
    db.session.commit()
    run_release_build.delay(release_id=release.id)
    return redirect(url_for('user.release_detail', release_id=release.id))


@user_blueprint.route('/docker/auth')
def docker_auth():
    secret = current_app.config['REGISTRY_AUTH_SECRET']
    username, password = request.authorization.username, request.authorization.password
    scope = request.args.get('scope', 'registry:catalog:*')
    requested_access = parse_docker_scope(scope)
    max_age = None
    if 'push' in [action for access in requested_access for action in access['actions']]:
        max_age = 600
    granted_access = check_docker_credentials(password, secret=secret, max_age=max_age)
    if not granted_access:
        return jsonify({"error": "unauthorized"}), 401
    access = docker_access_intersection(granted_access, requested_access)
    return jsonify({'token': generate_docker_registry_jwt(access=access)})

@user_blueprint.route('/applications/<application_id>/images/submit', methods=['GET', 'POST'])
@login_required
def application_images_build_submit(application_id):
    application = Application.query.filter_by(id=application_id).first()
    if application is None:
        abort(404)
    project = application.project
    organization = application.project.organization

    form = ImageBuildSubmitForm()
    form.application_id.choices = [(str(application.id), f'{organization.slug}/{project.slug}: {application.slug}')]
    form.application_id.data = str(application.id)

    if form.validate_on_submit():
        organization_slug = organization.slug
        project_slug = project.slug
        application_slug = application.slug
        repository_name = f"cabotage/{organization_slug}/{project_slug}/{application_slug}"

        fileobj = request.files['build_file']
        if fileobj:
            minio_response = minio.write_object(organization_slug, project_slug, application_slug, fileobj)
            image = Image(
                application_id=application.id,
                repository_name=repository_name,
                build_slug=minio_response['path'],
            )
            db.session.add(image)
            db.session.flush()
            activity = Activity(
                verb='submit',
                object=image,
                data={
                    'user_id': str(current_user.id),
                    'timestamp': datetime.datetime.utcnow().isoformat(),
                }
            )
            db.session.add(activity)
            db.session.commit()
            run_image_build.delay(image_id=image.id)
            return redirect(url_for('user.image_detail', image_id=image.id))
        return redirect(url_for('user.project_application', org_slug=organization.slug, project_slug=project.slug, app_slug=application.slug))
    return render_template('user/application_images_build_submit.html', form=form, application=application)

@user_blueprint.route('/applications/<application_id>/images/fromsource', methods=['POST'])
@login_required
def application_images_build_fromsource(application_id):
    application = Application.query.filter_by(id=application_id).first()
    if application is None:
        abort(404)
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
        verb='fromsource',
        object=image,
        data={
            'user_id': str(current_user.id),
            'timestamp': datetime.datetime.utcnow().isoformat(),
        }
    )
    db.session.add(activity)
    db.session.commit()
    run_image_build.delay(image_id=image.id, buildkit=True)
    return redirect(url_for('user.image_detail', image_id=image.id))


@user_blueprint.route('/application/<application_id>/scale', methods=['POST'])
@login_required
def application_scale(application_id):
    application = Application.query.filter_by(id=application_id).first()
    if application is None:
        abort(404)
    form = ApplicationScaleForm()
    form.application_id.data = str(application.id)
    if form.validate_on_submit():
        scaled = collections.defaultdict(dict)
        for key, value in request.form.items():
            if key.startswith('process-count-'):
                process_name = key[len('process-count-'):]
                if application.process_counts.get(process_name, 0) != int(value):
                    scaled[process_name]['process_count'] = {
                        'old_value': application.process_counts.get(process_name, 0),
                        'new_value': int(value),
                    }
                    application.process_counts[process_name] = int(value)
                    flag_modified(application, "process_counts")
            if key.startswith('process-pod-class-'):
                if application.process_pod_classes.get(process_name, 0) != value:
                    scaled[process_name]['pod_class'] = {
                        'old_value': application.process_pod_classes.get(process_name, DEFAULT_POD_CLASS),
                        'new_value': value,
                    }
                    application.process_pod_classes[process_name] = value
                    flag_modified(application, "process_pod_classes")
        if scaled:
            activity = Activity(
                verb='scale',
                object=application,
                data={
                    'user_id': str(current_user.id),
                    'timestamp': datetime.datetime.utcnow().isoformat(),
                    'changes': scaled,
                }
            )
            db.session.add(application)
            db.session.add(activity)
            db.session.commit()

            if current_app.config['KUBERNETES_ENABLED']:
                for process_name, change in scaled.items():
                    if 'process_count' in change.keys():
                        scale_deployment(application.project.organization.slug, application.latest_release, process_name, change['process_count']['new_value'])
    else:
        return jsonify(form.errors), 400
    return redirect(url_for('user.project_application', org_slug=application.project.organization.slug, project_slug=application.project.slug, app_slug=application.slug))

@user_blueprint.route('/release/<release_id>/deploy', methods=['POST'])
@login_required
def release_deploy(release_id):
    release = Release.query.filter_by(id=release_id).first()
    if release is None:
        abort(404)
    deployment = Deployment(
        application_id=release.application.id,
        release=release.asdict,
    )
    db.session.add(deployment)
    db.session.flush()
    activity = Activity(
        verb='deploy',
        object=deployment,
        data={
            'user_id': str(current_user.id),
            'timestamp': datetime.datetime.utcnow().isoformat(),
        }
    )
    db.session.add(activity)
    db.session.commit()
    if current_app.config['KUBERNETES_ENABLED']:
        deployment_id = deployment.id
        run_deploy.delay(deployment_id=deployment.id)
        deployment = Deployment.query.filter_by(id=deployment_id).first()
    else:
        from cabotage.celery.tasks.deploy import fake_deploy_release
        fake_deploy_release(deployment)
        deployment.complete = True
        db.session.commit()
    return redirect(url_for('user.deployment_detail', deployment_id=deployment.id))

@user_blueprint.route('/signing-cert', methods=['GET'])
def signing_cert():
    cert = vault.signing_cert
    return render_template('user/signing_cert.html', signing_certificate=cert)

@user_blueprint.route('/github/hooks', methods=['POST'])
def github_hooks():
    if github_app.validate_webhook():
        hook = Hook(headers=dict(request.headers), payload=request.json)
        db.session.add(hook)
        db.session.commit()
        process_github_hook.delay(hook_id=hook.id)
        return jsonify({'hook_id': hook.id})
    abort(403)
