from flask_security.forms import ConfirmRegisterForm, LoginForm, RegisterForm

from flask_wtf import FlaskForm

from wtforms import BooleanField, SelectField, StringField, FieldList, FormField, HiddenField
from wtforms.validators import DataRequired, Length, ValidationError, EqualTo

from cabotage.server import db
from cabotage.server.models.auth import Organization
from cabotage.server.models.projects import (
    Application,
    Configuration,
    Pipeline,
    Project,
)


class ExtendedLoginForm(LoginForm):

    email = StringField('Username or Email Address', [DataRequired()])


class ExtendedRegisterForm(RegisterForm):

    username = StringField(
        'Username',
        validators=[
            DataRequired(),
            Length(min=3, max=64),
        ]
    )

class ExtendedConfirmRegisterForm(ConfirmRegisterForm):

    username = StringField(
        'Username',
        validators=[
            DataRequired(),
            Length(min=3, max=64),
        ]
    )


class CreateProjectForm(FlaskForm):

    organization_id = SelectField(
        u'Organization',
        [DataRequired()],
        description="Organization this Project belongs to.",
    )
    name = StringField(
        u'Project Name',
        [DataRequired()],
        description="Friendly and descriptive name for your Project.",
    )
    slug = StringField(
        u'Project Slug',
        [DataRequired()],
        description="URL Safe short name for your Project, must be unique within the Organization.",
    )

    def validate_slug(form, field):
        project = Project.query.filter_by(organization_id=form.organization_id.data).filter_by(slug=field.data).first()
        if project is not None:
            raise ValidationError('Project slugs must be unique within organizations.')
        return True


class CreateOrganizationForm(FlaskForm):

    name = StringField(
        u'Organization Name',
        [DataRequired()],
        description="Friendly and descriptive name for your Organization.",
    )
    slug = StringField(
        u'Organization Slug',
        [DataRequired()],
        description="URL Safe short name for your Organization, must be globally unique.",
    )

    def validate_slug(form, field):
        organization = Organization.query.filter_by(slug=field.data).first()
        if organization is not None:
            raise ValidationError('Organization slugs must be globally unique.')
        return True


class CreatePipelineForm(FlaskForm):
    organization_id = SelectField(
        u'Organization',
        [DataRequired()],
        description="Organization this Pipeline belongs to.",
    )
    project_id = SelectField(
        u'Project',
        [DataRequired()],
        description="Project this Pipeline belongs to.",
    )
    name = StringField(
        u'Pipeline Name',
        [DataRequired()],
        description="Friendly and descriptive name for your Pipeline.",
    )
    slug = StringField(
        u'Pipeline Slug',
        [DataRequired()],
        description="URL Safe short name for your Pipeline, must be unique within the Project.",
    )

    def validate_slug(form, field):
        project = Application.query.filter_by(project_id=form.project_id.data).filter_by(slug=field.data).first()
        if project is not None:
            raise ValidationError('Pipeline slugs must be unique within Projects.')
        return True


class CreateApplicationForm(FlaskForm):

    organization_id = SelectField(
        u'Organization',
        [DataRequired()],
        description="Organization this Application belongs to.",
    )
    project_id = SelectField(
        u'Project',
        [DataRequired()],
        description="Project this Application belongs to.",
    )
    name = StringField(
        u'Application Name',
        [DataRequired()],
        description="Friendly and descriptive name for your Application.",
    )
    slug = StringField(
        u'Application Slug',
        [DataRequired()],
        description="URL Safe short name for your Application, must be unique within the Project.",
    )

    def validate_slug(form, field):
        project = Application.query.filter_by(project_id=form.project_id.data).filter_by(slug=field.data).first()
        if project is not None:
            raise ValidationError('Application slugs must be unique within Projects.')
        return True


class CreateConfigurationForm(FlaskForm):

    application_id = SelectField(
        u'Application',
        [DataRequired()],
        description="Application this Configuration belongs to.",
    )
    name = StringField(
        u'Name',
        [DataRequired()],
        description="Name for the Environment Variable.",
    )
    value = StringField(
        u'Value',
        [DataRequired()],
        description="Value for the Environment Variable.",
    )
    secure = BooleanField(
        u'Secure',
        [],
        description="Store this Environment Variable Securely. It will not be recoverable again via the UI.",
    )

    def validate_name(form, field):
        configuration = Configuration.query.filter_by(application_id=form.application_id.data, name=field.data).first()
        if configuration is not None:
            if form.name.data == configuration.name:
                return True
            raise ValidationError('Configuration names must be unique (case insensitive) within Applications')
        return True


class DeleteConfigurationForm(FlaskForm):

    configuration_id = HiddenField(
        u'Configuration ID',
        [DataRequired()],
        description="ID of the Environment Variable to delete.",
    )
    name = StringField(
        u'Name',
        [DataRequired()],
        description="Name for the Environment Variable.",
    )
    value = StringField(
        u'Value',
        [DataRequired()],
        description="Value for the Environment Variable.",
    )
    secure = BooleanField(
        u'Secure',
        [],
        description="Store this Environment Variable Securely. It will not be recoverable again via the UI.",
    )
    confirm = StringField(
        u'Type the name of the Environment Variable.',
        [EqualTo('name', message='Must confirm the *exact* name of the Environment Variable!')],
    )
