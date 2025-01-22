import uuid

from flask_security.forms import ConfirmRegisterForm, LoginForm, RegisterForm

from flask_wtf import FlaskForm

from wtforms import (
    BooleanField,
    HiddenField,
    IntegerField,
    SelectField,
    StringField,
    FieldList,
    FormField,
    Form as WTFForm,
)
from wtforms.validators import (
    DataRequired,
    InputRequired,
    EqualTo,
    Length,
    Regexp,
    ValidationError,
)

from cabotage.server.models.auth import Organization
from cabotage.server.models.projects import (
    Application,
    Configuration,
    Project,
)


class ExtendedLoginForm(LoginForm):
    email = StringField("Username or Email Address", [InputRequired()])


class ExtendedRegisterForm(RegisterForm):
    username = StringField(
        "Username",
        validators=[
            InputRequired(),
            Length(min=1, max=64),
        ],
    )


class ExtendedConfirmRegisterForm(ConfirmRegisterForm):
    username = StringField(
        "Username",
        validators=[
            InputRequired(),
            Length(min=1, max=64),
        ],
    )


class CreateOrganizationForm(FlaskForm):
    name = StringField(
        "Organization Name",
        [InputRequired()],
        description="Friendly and descriptive name for your Organization.",
    )
    slug = StringField(
        "Organization Slug",
        [
            InputRequired(),
            Regexp("^[-a-z0-9]+$", message="Invalid Slug! Must match ^[-a-z0-9]+$"),
        ],
        description=(
            "URL Safe short name for your Organization, must be globally unique."
        ),
    )

    def validate_slug(form, field):
        organization = Organization.query.filter_by(slug=field.data).first()
        if organization is not None:
            raise ValidationError("Organization slugs must be globally unique.")
        return True


class CreateProjectForm(FlaskForm):
    organization_id = SelectField(
        "Organization",
        [DataRequired()],
        description="Organization this Project belongs to.",
    )
    name = StringField(
        "Project Name",
        [InputRequired()],
        description="Friendly and descriptive name for your Project.",
    )
    slug = StringField(
        "Project Slug",
        [
            InputRequired(),
            Regexp("^[-a-z0-9]+$", message="Invalid Slug! Must match ^[-a-z0-9]+$"),
        ],
        description=(
            "URL Safe short name for your Project, "
            "must be unique within the Organization."
        ),
    )

    def validate_slug(form, field):
        project = (
            Project.query.filter_by(organization_id=form.organization_id.data)
            .filter_by(slug=field.data)
            .first()
        )
        if project is not None:
            raise ValidationError("Project slugs must be unique within organizations.")
        return True


class DeleteProjectForm(FlaskForm):
    application_id = HiddenField(
        "Project ID",
        [DataRequired()],
        description="ID of the Project to delete.",
    )
    name = StringField(
        "Name",
        [InputRequired()],
        description="Name for the Project being deleted.",
    )
    confirm = StringField(
        "Type the name of the Project.",
        [EqualTo("name", message="Must confirm the *exact* name of the Project!")],
    )


class CreateApplicationForm(FlaskForm):
    organization_id = SelectField(
        "Organization",
        [DataRequired()],
        description="Organization this Application belongs to.",
    )
    project_id = SelectField(
        "Project",
        [DataRequired()],
        description="Project this Application belongs to.",
    )
    name = StringField(
        "Application Name",
        [InputRequired()],
        description="Friendly and descriptive name for your Application.",
    )
    slug = StringField(
        "Application Slug",
        [
            InputRequired(),
            Regexp("^[-a-z0-9]+$", message="Invalid Slug! Must match ^[-a-z0-9]+$"),
        ],
        description=(
            "URL Safe short name for your Application, "
            "must be unique within the Project."
        ),
    )

    def validate_slug(form, field):
        project = (
            Application.query.filter_by(project_id=form.project_id.data)
            .filter_by(slug=field.data)
            .first()
        )
        if project is not None:
            raise ValidationError("Application slugs must be unique within Projects.")
        return True


class DeleteApplicationForm(FlaskForm):
    application_id = HiddenField(
        "Application ID",
        [DataRequired()],
        description="ID of the Application to delete.",
    )
    name = StringField(
        "Name",
        [InputRequired()],
        description="Name for the Application being deleted.",
    )
    confirm = StringField(
        "Type the name of the Application.",
        [EqualTo("name", message="Must confirm the *exact* name of the Application!")],
    )


class CreateConfigurationForm(FlaskForm):
    application_id = SelectField(
        "Application",
        [DataRequired()],
        description="Application this Configuration belongs to.",
    )
    name = StringField(
        "Name",
        [
            InputRequired(),
            Regexp(
                "^[a-zA-Z_]+[a-zA-Z0-9_]*$",
                message=(
                    "Invalid Environment Variable Name! "
                    "Must match ^[a-zA-Z_]+[a-zA-Z0-9_]*$"
                ),
            ),
        ],
        description="Name for the Environment Variable.",
    )
    value = StringField(
        "Value",
        [InputRequired()],
        description="Value for the Environment Variable.",
    )
    secure = BooleanField(
        "Secure",
        [],
        description=(
            "Store this Environment Variable Securely. "
            "It will not be recoverable again via the UI."
        ),
    )
    buildtime = BooleanField(
        "Expose during Build",
        [],
        description="Set this Enviornment Variable during Image builds.",
    )

    def validate_name(form, field):
        configuration = Configuration.query.filter_by(
            application_id=form.application_id.data, name=field.data
        ).first()
        if configuration is not None:
            if form.name.data.lower() != configuration.name.lower():
                return True
            raise ValidationError(
                "Configuration names must be unique (case insensitive) "
                "within Applications"
            )
        return True


class EditApplicationSettingsForm(FlaskForm):
    application_id = SelectField(
        "Application",
        [DataRequired()],
        description="Application this Configuration belongs to.",
    )
    github_repository = StringField(
        "GitHub Repository",
        description="GitHub Repository to deploy from",
        render_kw={"placeholder": "org_name/repo_name"},
    )
    github_repository_is_private = BooleanField(
        "Private Repository",
        description=(
            "This is a private GitHub repository "
            "(requires a valid GitHub Application Installation ID)"
        ),
    )
    auto_deploy_branch = StringField(
        "Branch",
        description="GitHub Repository branch to auto-deploy from",
    )
    github_app_installation_id = StringField(
        "GitHub Application Installation ID",
        description="Application Installation ID from GitHub",
        filters=[
            (lambda x: x.strip() if (x and isinstance(x, str)) else x),
            (lambda x: x if x else None),
        ],
    )
    github_environment_name = StringField(
        "GitHub Environment Name",
        description=(
            "Environment name for GitHub deploys, "
            "default: cabotage/[application uuid]"
        ),
        filters=[(lambda x: x.strip() if x else x), (lambda x: x if x else None)],
    )

    def validate_github_environment_name(form, field):
        if field.data is None:
            return True
        app = (
            Application.query.filter_by(
                github_app_installation_id=form.github_app_installation_id.data
            )
            .filter_by(github_repository=form.github_repository.data)
            .filter_by(github_environment_name=field.data)
            .first()
        )
        if app is not None and app.id != uuid.UUID(form.application_id.data):
            raise ValidationError(
                "Environment names must be unique within "
                "GitHub App Installations and Repositories."
            )
        return True

    deployment_timeout = IntegerField(
        "Deployment Timeout",
        description=("Timeout (in seconds) when waiting for a deployment to complete"),
    )

    health_check_path = StringField(
        "HTTP Health Check Path",
        description=(
            "Path that probes should hit with a simple HTTP request "
            "to determine health. "
            "It should respond quickly with a simple HTTP 200 OK. "
            "Requires a new release to take effect."
        ),
    )
    health_check_host = StringField(
        "HTTP Health Check Host Header",
        description=(
            "Host header that probes should use when request to determine health. "
            "Requires a new release to take effect."
        ),
    )


class HostEntryForm(WTFForm):
    hostname = StringField("hostname")
    tls = BooleanField("tls")


class IngressEntryForm(WTFForm):
    process_name = StringField("process")
    enabled = BooleanField("Enable Ingress")
    additional_hosts = FieldList(FormField(HostEntryForm))


class EditIngressForm(FlaskForm):
    application_id = SelectField(
        "Application",
        [DataRequired()],
        description="Application these Ingresses belong to.",
    )
    ingresses = FieldList(FormField(IngressEntryForm))


class EditConfigurationForm(FlaskForm):
    application_id = SelectField(
        "Application",
        [DataRequired()],
        description="Application this Configuration belongs to.",
    )
    name = StringField(
        "Name",
        [
            DataRequired(),
            Regexp(
                "^[a-zA-Z_]+[a-zA-Z0-9_]*$",
                message=(
                    "Invalid Environment Variable Name! "
                    "Must match ^[a-zA-Z_]+[a-zA-Z0-9_]*$"
                ),
            ),
        ],
        description="Name for the Environment Variable.",
    )
    value = StringField(
        "Value",
        [InputRequired()],
        description="Value for the Environment Variable.",
    )
    secure = BooleanField(
        "Secure",
        [],
        description=(
            "Store this Environment Variable Securely. "
            "It will not be recoverable again via the UI."
        ),
    )
    buildtime = BooleanField(
        "Expose during Build",
        [],
        description="Set this Enviornment Variable during Image builds.",
    )

    def validate_name(form, field):
        configuration = Configuration.query.filter_by(
            application_id=form.application_id.data, name=field.data
        ).first()
        if configuration is not None:
            if form.name.data == configuration.name:
                return True
            raise ValidationError(
                "Configuration names cannot be changed! Delete and re-create"
            )
        raise ValidationError(
            (
                "Configurations must be created from the "
                "Create Application Configuration form"
            )
        )


class DeleteConfigurationForm(FlaskForm):
    configuration_id = HiddenField(
        "Configuration ID",
        [DataRequired()],
        description="ID of the Environment Variable to delete.",
    )
    name = StringField(
        "Name",
        [DataRequired()],
        description="Name for the Environment Variable.",
    )
    value = StringField(
        "Value",
        [DataRequired()],
        description="Value for the Environment Variable.",
    )
    secure = BooleanField(
        "Secure",
        [],
        description=(
            "Store this Environment Variable Securely. "
            "It will not be recoverable again via the UI."
        ),
    )
    confirm = StringField(
        "Type the name of the Environment Variable.",
        [
            EqualTo(
                "name",
                message="Must confirm the *exact* name of the Environment Variable!",
            )
        ],
    )


class ReleaseDeployForm(FlaskForm):
    release_id = StringField(
        "Release ID",
        [InputRequired()],
        description="Release to deploy.",
    )


class ApplicationScaleForm(FlaskForm):
    application_id = StringField(
        "Application ID",
        [DataRequired()],
        description="Application to scale.",
    )
