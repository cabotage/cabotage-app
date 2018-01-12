from flask import abort, render_template, Blueprint
from flask_security import current_user, login_required

from cabotage.server import db
from cabotage.server.models.auth import Organization
from cabotage.server.models.projects import Project

from cabotage.server.user.forms import CreateProjectForm, CreateOrganizationForm

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
def project_view(org_slug, project_slug):
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
        db.session.commit()
        return 'created project', 201
    return render_template('user/project_create.html', project_create_form=form)


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
        return 'created organization!', 200
    return render_template('user/organization_create.html', organization_create_form=form)
