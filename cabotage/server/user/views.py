import os
import datetime

from flask import (
    Blueprint,
    abort,
    flash,
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
    Container,
    Release
)
from cabotage.server.models.projects import activity_plugin

from cabotage.server.user.forms import (
    CreateApplicationForm,
    CreateConfigurationForm,
    CreateContainerForm,
    CreateOrganizationForm,
    CreateProjectForm,
    DeleteConfigurationForm,
)

from cabotage.utils.docker_auth import generate_docker_registry_jwt

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

    form = CreateConfigurationForm(obj=configuration)
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


@user_blueprint.route('/projects/<org_slug>/<project_slug>/applications/<app_slug>/container/<container_id>')
@login_required
def project_application_container(org_slug, project_slug, app_slug, container_id):
    organization = Organization.query.filter_by(slug=org_slug).first()
    if organization is None:
        abort(404)
    project = Project.query.filter_by(organization_id=organization.id, slug=project_slug).first()
    if project is None:
        abort(404)
    application = Application.query.filter_by(project_id=project.id, slug=app_slug).first()
    if application is None:
        abort(404)
    container = Container.query.filter_by(application_id=application.id, id=container_id).first()
    if container is None:
        abort(404)

    return render_template('user/project_application_container.html', container=container)


@user_blueprint.route('/projects/<org_slug>/<project_slug>/applications/<app_slug>/container/create', methods=['GET', 'POST'])
@login_required
def project_application_container_create(org_slug, project_slug, app_slug):
    organization = Organization.query.filter_by(slug=org_slug).first()
    if organization is None:
        abort(404)
    project = Project.query.filter_by(organization_id=organization.id, slug=project_slug).first()
    if project is None:
        abort(404)
    application = Application.query.filter_by(project_id=project.id, slug=app_slug).first()
    if application is None:
        abort(404)

    form = CreateContainerForm()
    form.application_id.choices = [(str(application.id), f'{organization.slug}/{project.slug}: {application.slug}')]
    form.application_id.data = str(application.id)

    if form.validate_on_submit():
        container = Container(
            application_id=form.application_id.data,
            container_repository=form.container_repository.data,
            container_tag=form.container_tag.data,
        )
        db.session.add(container)
        db.session.flush()
        activity = Activity(
            verb='create',
            object=container,
            data={
                'user_id': str(current_user.id),
                'timestamp': datetime.datetime.utcnow().isoformat(),
            }
        )
        db.session.add(activity)
        db.session.commit()
        return redirect(url_for('user.project_application', org_slug=organization.slug, project_slug=project.slug, app_slug=application.slug))
    return render_template('user/project_application_container_create.html', form=form, org_slug=organization.slug, project_slug=project.slug, app_slug=application.slug)


@user_blueprint.route('/projects/<org_slug>/<project_slug>/applications/<app_slug>/container/<container_id>/edit', methods=['GET', 'POST'])
@login_required
def project_application_container_edit(org_slug, project_slug, app_slug, container_id):
    organization = Organization.query.filter_by(slug=org_slug).first()
    if organization is None:
        abort(404)
    project = Project.query.filter_by(organization_id=organization.id, slug=project_slug).first()
    if project is None:
        abort(404)
    application = Application.query.filter_by(project_id=project.id, slug=app_slug).first()
    if application is None:
        abort(404)
    container = Container.query.filter_by(application_id=application.id, id=container_id).first()
    if container is None:
        abort(404)

    form = CreateContainerForm(obj=container)
    form.application_id.choices = [(str(container.application.id), f'{organization.slug}/{project.slug}: {application.slug}')]
    form.application_id.data = str(container.application.id)

    if form.validate_on_submit():
        form.populate_obj(container)
        db.session.flush()
        activity = Activity(
            verb='edit',
            object=container,
            data={
                'user_id': str(current_user.id),
                'timestamp': datetime.datetime.utcnow().isoformat(),
            }
        )
        db.session.add(activity)
        db.session.commit()
        return redirect(url_for('user.project_application', org_slug=organization.slug, project_slug=project.slug, app_slug=application.slug))
    return render_template('user/project_application_container_edit.html', form=form, org_slug=organization.slug, project_slug=project.slug, app_slug=application.slug, container=container)


@user_blueprint.route('/projects/<org_slug>/<project_slug>/applications/<app_slug>/release/create', methods=['GET', 'POST'])
@login_required
def project_application_release_create(org_slug, project_slug, app_slug):
    organization = Organization.query.filter_by(slug=org_slug).first()
    if organization is None:
        abort(404)
    project = Project.query.filter_by(organization_id=organization.id, slug=project_slug).first()
    if project is None:
        abort(404)
    application = Application.query.filter_by(project_id=project.id, slug=app_slug).first()
    if application is None:
        abort(404)

    application.create_release()
    db.session.flush()
    activity = Activity(
        verb='edit',
        object=application.release,
        data={
            'user_id': str(current_user.id),
            'timestamp': datetime.datetime.utcnow().isoformat(),
        }
    )
    db.session.add(activity)
    db.session.commit()
    return redirect(url_for('user.project_application', org_slug=organization.slug, project_slug=project.slug, app_slug=application.slug))


@user_blueprint.route('/build/submit', methods=['GET', 'POST'])
@login_required
def build_submit():
    if request.method == 'POST':
        # TODO: check for org/project/application access
        # TODO: convert to WTForm and Template
        if 'file' not in request.files:
            flash('No file part')
            return redirect(request.url)
        if 'organization' not in request.form or 'project' not in request.form or 'application' not in request.form:
            flash('Must supply organization, project, and application')
            return redirect(request.url)
        org_slug = request.form['organization']
        proj_slug = request.form['project']
        app_slug = request.form['application']
        fileobj = request.files['file']
        if fileobj:
            response = minio.write_object(org_slug, proj_slug, app_slug, fileobj)
            return f'{response}, {generate_docker_registry_jwt()}, {vault.signing_public_key}'
    return '''
    <!doctype html>
    <title>Upload new File</title>
    <h1>Upload new File</h1>
    <form method=post enctype=multipart/form-data>
      <p>Organization<input type=text name=organization>
      <p>Project<input type=text name=project>
      <p>Application<input type=text name=application>
      <p><input type=file name=file>
         <input type=submit value=Upload>
    </form>
    '''
