from flask_security.forms import ConfirmRegisterForm, LoginForm, RegisterForm

from flask_wtf import FlaskForm

from wtforms import (
    BooleanField,
    FieldList,
    FileField,
    FormField,
    HiddenField,
    SelectField,
    StringField,
)
from wtforms.validators import (
    DataRequired,
    EqualTo,
    Length,
    Regexp,
    ValidationError,
)

from cabotage.server import db
from cabotage.server.models.auth import Organization
from cabotage.server.models.projects import (
    Application,
    Configuration,
    Project,
)


class ExtendedLoginForm(LoginForm):

    email = StringField('Username or Email Address', [DataRequired()])


class ExtendedRegisterForm(RegisterForm):

    username = StringField(
        'Username',
        validators=[
            DataRequired(),
            Length(min=1, max=64),
        ]
    )

class ExtendedConfirmRegisterForm(ConfirmRegisterForm):

    username = StringField(
        'Username',
        validators=[
            DataRequired(),
            Length(min=1, max=64),
        ]
    )


class CreateOrganizationForm(FlaskForm):

    name = StringField(
        u'Organization Name',
        [DataRequired()],
        description="Friendly and descriptive name for your Organization.",
    )
    slug = StringField(
        u'Organization Slug',
        [DataRequired(),
         Regexp('^[-a-z0-9]+$', message="Invalid Slug! Must match ^[-a-z0-9]+$")],
        description="URL Safe short name for your Organization, must be globally unique.",
    )

    def validate_slug(form, field):
        organization = Organization.query.filter_by(slug=field.data).first()
        if organization is not None:
            raise ValidationError('Organization slugs must be globally unique.')
        return True


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
        [DataRequired(),
         Regexp('^[-a-z0-9]+$', message="Invalid Slug! Must match ^[-a-z0-9]+$")],
        description="URL Safe short name for your Project, must be unique within the Organization.",
    )

    def validate_slug(form, field):
        project = Project.query.filter_by(organization_id=form.organization_id.data).filter_by(slug=field.data).first()
        if project is not None:
            raise ValidationError('Project slugs must be unique within organizations.')
        return True


class DeleteProjectForm(FlaskForm):

    application_id = HiddenField(
        u'Project ID',
        [DataRequired()],
        description="ID of the Project to delete.",
    )
    name = StringField(
        u'Name',
        [DataRequired()],
        description="Name for the Project being deleted.",
    )
    confirm = StringField(
        u'Type the name of the Project.',
        [EqualTo('name', message='Must confirm the *exact* name of the Project!')],
    )


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
        [DataRequired(),
         Regexp('^[-a-z0-9]+$', message="Invalid Slug! Must match ^[-a-z0-9]+$")],
        description="URL Safe short name for your Application, must be unique within the Project.",
    )

    def validate_slug(form, field):
        project = Application.query.filter_by(project_id=form.project_id.data).filter_by(slug=field.data).first()
        if project is not None:
            raise ValidationError('Application slugs must be unique within Projects.')
        return True


class DeleteApplicationForm(FlaskForm):

    application_id = HiddenField(
        u'Application ID',
        [DataRequired()],
        description="ID of the Application to delete.",
    )
    name = StringField(
        u'Name',
        [DataRequired()],
        description="Name for the Application being deleted.",
    )
    confirm = StringField(
        u'Type the name of the Application.',
        [EqualTo('name', message='Must confirm the *exact* name of the Application!')],
    )

class CreateConfigurationForm(FlaskForm):

    application_id = SelectField(
        u'Application',
        [DataRequired()],
        description="Application this Configuration belongs to.",
    )
    name = StringField(
        u'Name',
        [DataRequired(),
         Regexp('^[a-zA-Z_]+[a-zA-Z0-9_]*$', message="Invalid Environment Variable Name! Must match ^[a-zA-Z_]+[a-zA-Z0-9_]*$")],
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
    buildtime = BooleanField(
        u'Expose during Build',
        [],
        description="Set this Enviornment Variable during Image builds.",
    )

    def validate_name(form, field):
        configuration = Configuration.query.filter_by(application_id=form.application_id.data, name=field.data).first()
        if configuration is not None:
            if form.name.data.lower() != configuration.name.lower():
                return True
            raise ValidationError('Configuration names must be unique (case insensitive) within Applications')
        return True


class EditConfigurationForm(FlaskForm):

    application_id = SelectField(
        u'Application',
        [DataRequired()],
        description="Application this Configuration belongs to.",
    )
    name = StringField(
        u'Name',
        [DataRequired(),
         Regexp('^[a-zA-Z_]+[a-zA-Z0-9_]*$', message="Invalid Environment Variable Name! Must match ^[a-zA-Z_]+[a-zA-Z0-9_]*$")],
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
    buildtime = BooleanField(
        u'Expose during Build',
        [],
        description="Set this Enviornment Variable during Image builds.",
    )

    def validate_name(form, field):
        configuration = Configuration.query.filter_by(application_id=form.application_id.data, name=field.data).first()
        if configuration is not None:
            if form.name.data == configuration.name:
                return True
            raise ValidationError('Configuration names cannot be changed! Delete and re-create')
        raise ValidationError('Configurations must be created from the Create Application Configuration form')


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


class ImageBuildSubmitForm(FlaskForm):

    application_id = SelectField(
        u'Application',
        [DataRequired()],
        description="Application this Image is built for.",
    )
    build_file = FileField(
        u'Build File',
        [DataRequired()],
        description="Gzipped Tarball matching {documentation_url}.",
    )


class ReleaseDeployForm(FlaskForm):

    application_id = StringField(
        u'Release ID',
        [DataRequired()],
        description="Release to deploy.",
    )
