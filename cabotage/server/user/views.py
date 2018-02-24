import os
import datetime

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

from sqlalchemy import desc
from sqlalchemy_continuum import version_class

from cabotage.server import config_writer
from cabotage.server import minio
from cabotage.server import db
from cabotage.server import vault
from cabotage.server.models.auth import Organization
from cabotage.server.models.projects import (
    Project,
    Application,
    Configuration,
    Image,
    Release
)
from cabotage.server.models.projects import activity_plugin

from cabotage.server.user.forms import (
    CreateApplicationForm,
    CreateConfigurationForm,
    CreateOrganizationForm,
    CreateProjectForm,
    DeleteConfigurationForm,
    EditConfigurationForm,
    ImageBuildSubmitForm,
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
    run_image_build,
    run_release_build,
)

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
    return render_template(
        'user/project_application.html',
        application=application,
        view_releases=version_class(Release).query.filter_by(application_id=application.id).order_by(desc(version_class(Release).version_id)).limit(5),
    )


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
        )
        try:
            key_slug = config_writer.write_configuration(
                org_slug,
                project_slug,
                app_slug,
                configuration,
            )
        except Exception as exc:
            raise  # No, we should def not do this
        configuration.key_slug = key_slug
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
            key_slug = config_writer.write_configuration(
                org_slug,
                project_slug,
                app_slug,
                configuration,
            )
        except Exception as exc:
            raise  # No, we should def not do this
        configuration.key_slug = key_slug
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
    secret = current_app.config['CABOTAGE_REGISTRY_AUTH_SECRET']
    docker_pull_credentials = image.docker_pull_credentials(secret)
    return render_template('user/image_detail.html', image=image, docker_pull_credentials=docker_pull_credentials)


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
    secret = current_app.config['CABOTAGE_REGISTRY_AUTH_SECRET']
    docker_pull_credentials = release.docker_pull_credentials(secret)
    return render_template('user/release_detail.html', release=release, docker_pull_credentials=docker_pull_credentials)


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
    return redirect(url_for('user.project_application', org_slug=application.project.organization.slug, project_slug=application.project.slug, app_slug=application.slug))


@user_blueprint.route('/docker/auth')
def docker_auth():
    secret = current_app.config['CABOTAGE_REGISTRY_AUTH_SECRET']
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
        return redirect(url_for('user.project_application', org_slug=organization.slug, project_slug=project.slug, app_slug=application.slug))
    return render_template('user/application_images_build_submit.html', form=form, application=application)

@user_blueprint.route('/signing-cert', methods=['GET'])
def signing_certi():
    cert = vault.signing_cert
    return render_template('user/signing_cert.html', signing_certificate=cert)
