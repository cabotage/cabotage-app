from flask import abort, render_template, Blueprint, redirect, url_for
from flask_security import current_user, login_required

from cabotage.server import db
from cabotage.server.models.auth import Organization
from cabotage.server.models.projects import Project, Application, Pipeline

from cabotage.server.user.forms import CreateApplicationForm, CreateProjectForm, CreateOrganizationForm, CreatePipelineForm

user_blueprint = Blueprint('user', __name__,)


@user_blueprint.route('/projects')
@login_required
def projects():
    return render_template('user/projects.html', projects=current_user.projects)


@user_blueprint.route('/projects/<org_slug>')
@login_required
def org_projects(org_slug):
    organizations = Organization.query.filter_by(slug=org_slug).all()
    if organizations == []:
        abort(404)
    return render_template('user/org_projects.html', organizations=organizations)


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


@user_blueprint.route('/projects/<org_slug>/<project_slug>/applications/<app_slug>')
@login_required
def project_application(org_slug, project_slug, app_slug):
    organization = Organization.query.filter_by(slug=org_slug).first()
    if organization is None:
        abort(404)
    project = Project.query.filter_by(organization_id=organization.id, slug=project_slug).first()
    if project is None:
        abort(404)
    application = Application.query.filter_by(project_id=project.id, slug=app_slug)
    if application is None:
        abort(404)
    return render_template('user/project_application.html', application=application)


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
        db.session.commit()
        return redirect(url_for('user.project_application', org_slug=project.organization.slug, project_slug=project.slug, app_slug=application.slug))
    return render_template('user/project_application_create.html', project_application_create_form=form, org_slug=org_slug, project_slug=project_slug)


@user_blueprint.route('/projects/<org_slug>/<project_slug>/pipelines/create', methods=["GET", "POST"])
@login_required
def project_pipeline_create(org_slug, project_slug):
    user = current_user
    form = CreatePipelineForm()
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
        application = Pipeline(project_id=form.project_id.data, name=form.name.data, slug=form.slug.data)
        db.session.add(application)
        db.session.commit()
        return redirect(url_for('user.project_pipeline', org_slug=project.organization.slug, project_slug=project.slug, app_slug=application.slug))
    return render_template('user/project_pipeline_create.html', project_pipeline_create_form=form, org_slug=org_slug, project_slug=project_slug)


@user_blueprint.route('/projects/create', methods=["GET", "POST"])
@login_required
def project_create():
    user = current_user
    form = CreateProjectForm()
    form.organization_id.choices = [(str(o.organization_id), o.organization.name) for o in user.organizations]

    if form.validate_on_submit():
        project = Project(organization_id=form.organization_id.data, name=form.name.data, slug=form.slug.data)
        db.session.add(project)
        db.session.commit()
        return redirect(url_for('user.project', org_slug=project.organization.slug, project_slug=project.slug))
    return render_template('user/project_create.html', project_create_form=form)


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
        db.session.commit()
        return redirect(url_for('user.project', org_slug=project.organization.slug, project_slug=project.slug))
    return render_template('user/organization_project_create.html', organization=organization, organization_project_create_form=form)


@user_blueprint.route('/organizations/<org_slug>')
@login_required
def organization(org_slug):
    user = current_user
    organization = Organization.query.filter_by(slug=org_slug).first()
    if organization is None:
        abort(404)
    return render_template('user/organization.html', organization=organization)


@user_blueprint.route('/organizations/<org_slug>/projects')
@login_required
def organization_projects(org_slug):
    user = current_user
    organization = Organization.query.filter_by(slug=org_slug).first()
    if organization is None:
        abort(404)
    return render_template('user/organization_projects.html', organization=organization)


@user_blueprint.route('/organizations')
@login_required
def organizations():
    user = current_user
    organizations = user.organizations
    return render_template('user/organizations.html', organizations=organizations)


@user_blueprint.route('/organizations/create', methods=["GET", "POST"])
@login_required
def organization_create():
    user = current_user
    form = CreateOrganizationForm()

    if form.validate_on_submit():
        organization = Organization(name=form.name.data, slug=form.slug.data)
        organization.add_user(user, admin=True)
        db.session.add(organization)
        db.session.commit()
        return redirect(url_for('user.organization', org_slug=organization.slug))
    return render_template('user/organization_create.html', organization_create_form=form)
