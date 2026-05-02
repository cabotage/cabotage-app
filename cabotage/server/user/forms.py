import uuid

from flask_security.forms import LoginForm, RegisterFormV2

from flask_wtf import FlaskForm

from wtforms import (
    BooleanField,
    HiddenField,
    IntegerField,
    SelectField,
    StringField,
    TextAreaField,
)
from wtforms.validators import (
    DataRequired,
    InputRequired,
    EqualTo,
    Length,
    Optional,
    Regexp,
    ValidationError,
)

from cabotage.server.models.auth import Organization
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Configuration,
    Environment,
    EnvironmentConfiguration,
    Project,
)
from cabotage.server.models.resources import (
    DEFAULT_REDIS_FOLLOWER_REPLICAS,
    DEFAULT_REDIS_LEADER_REPLICAS,
    POSTGRES_VERSIONS,
    REDIS_VERSIONS,
    Resource,
    postgres_size_classes,
    redis_size_classes,
)
from cabotage.server.models.utils import slugify


class ExtendedLoginForm(LoginForm):
    email = StringField("Username or Email Address", [InputRequired()])


class ExtendedRegisterForm(RegisterFormV2):
    username = StringField(
        "Username",
        validators=[
            InputRequired(),
            Length(min=1, max=64),
            Regexp(
                r"^[^:]+$",
                message="Usernames cannot contain colons.",
            ),
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
    environments_enabled = BooleanField(
        "Enable Environments",
        [],
        default=True,
        description=(
            "Environments let you run the same application with different "
            "configuration in isolated namespaces (e.g., staging and production). "
            "Each environment gets its own config variables, releases, and deploy history."
        ),
    )
    initial_env_name = StringField(
        "Default Environment Name",
        [Optional()],
        default="Production",
        description="Name for the initial default environment.",
    )
    initial_env_slug = StringField(
        "Default Environment Slug",
        [
            Optional(),
            Regexp("^[-a-z0-9]+$", message="Invalid Slug! Must match ^[-a-z0-9]+$"),
        ],
        description="URL-safe identifier for the default environment. Auto-generated from name if left blank.",
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


class EditProjectSettingsForm(FlaskForm):
    project_id = HiddenField("Project ID", [DataRequired()])
    environments_enabled = BooleanField(
        "Enable Environments",
        [],
        description=(
            "Environments let you run the same application with different "
            "configuration in isolated namespaces (e.g., staging and production). "
            "Each environment gets its own config variables, releases, and deploy history."
        ),
    )
    initial_env_name = StringField(
        "Initial Environment Name",
        [Optional()],
        default="Production",
        description="Name for the default environment. All existing applications will be migrated into it.",
    )
    initial_env_slug = StringField(
        "Initial Environment Slug",
        [
            Optional(),
            Regexp("^[-a-z0-9]+$", message="Invalid Slug! Must match ^[-a-z0-9]+$"),
        ],
        description="URL-safe identifier for the default environment. Auto-generated from name if left blank.",
    )
    branch_deploys_enabled = BooleanField(
        "Enable Branch Deploys",
        [],
        description="Automatically create ephemeral environments for pull requests.",
    )
    branch_deploy_base_environment_id = SelectField(
        "Base Environment",
        [Optional()],
        description="Environment to use as a template for branch deploy environments.",
    )


class DeleteProjectForm(FlaskForm):
    project_id = HiddenField(
        "Project ID",
        [DataRequired()],
        description="ID of the Project to delete.",
    )
    name = StringField(
        "Slug",
        [InputRequired()],
        description="Slug of the Project being deleted.",
    )
    confirm = StringField(
        "Type the slug of the Project.",
        [EqualTo("name", message="Must confirm the *exact* slug of the Project!")],
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
    environment_id = SelectField(
        "Environment",
        [Optional()],
        description="Environment to add this application to.",
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
        "Slug",
        [InputRequired()],
        description="Slug of the Application being deleted.",
    )
    confirm = StringField(
        "Type the slug of the Application.",
        [EqualTo("name", message="Must confirm the *exact* slug of the Application!")],
    )


class CreateConfigurationForm(FlaskForm):
    application_id = HiddenField(
        "Application",
        [DataRequired()],
        description="Application this Configuration belongs to.",
    )
    environment_id = HiddenField(
        "Environment",
        description="Environment this Configuration belongs to (optional).",
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
        if field.data and field.data.upper() == "CABOTAGE_SENTINEL":
            raise ValidationError("This name is reserved.")
        app_env_id = None
        env_id = form.environment_id.data or None
        if env_id:
            app_env = ApplicationEnvironment.query.filter_by(
                application_id=form.application_id.data,
                environment_id=env_id,
            ).first()
            if app_env:
                app_env_id = app_env.id
        configuration = Configuration.query.filter_by(
            application_id=form.application_id.data,
            application_environment_id=app_env_id,
            name=field.data,
        ).first()
        if configuration is not None:
            if form.name.data.lower() != configuration.name.lower():
                return True
            raise ValidationError(
                "Configuration names must be unique (case insensitive) "
                "within an environment"
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
    subdirectory = StringField(
        "Subdirectory",
        description="Subdirectory to build out of",
    )
    dockerfile_path = StringField(
        "Dockerfile Path",
        description="Custom path to Dockerfile (e.g. docker/Dockerfile), falls back to Dockerfile.cabotage then Dockerfile",
        filters=[
            (lambda x: x.strip() if (x and isinstance(x, str)) else x),
            (lambda x: x if x else None),
        ],
    )
    procfile_path = StringField(
        "Procfile Path",
        description="Custom path to Procfile (e.g. deploy/Procfile), falls back to Procfile.cabotage then Procfile",
        filters=[
            (lambda x: x.strip() if (x and isinstance(x, str)) else x),
            (lambda x: x if x else None),
        ],
    )
    branch_deploy_watch_paths = TextAreaField(
        "Watch Paths",
        description="Only deploy this app when files matching these patterns change. One .gitignore-style pattern per line (e.g. src/**, Dockerfile). Leave empty to always deploy.",
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
            "Environment name for GitHub deploys, default: cabotage/[application uuid]"
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


class EditConfigurationForm(FlaskForm):
    application_id = HiddenField(
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


class CreateEnvironmentForm(FlaskForm):
    project_id = HiddenField(
        "Project",
        [DataRequired()],
        description="Project this Environment belongs to.",
    )
    name = StringField(
        "Environment Name",
        [InputRequired()],
        description="Friendly name for this Environment (e.g., Staging, Production).",
    )
    slug = StringField(
        "Environment Slug",
        [
            Optional(),
            Regexp("^[-a-z0-9]+$", message="Invalid Slug! Must match ^[-a-z0-9]+$"),
        ],
        description=(
            "URL Safe short name for this Environment, "
            "must be unique within the Project. Auto-generated from name if left blank."
        ),
    )
    is_default = BooleanField(
        "Default Environment",
        [],
        description="Make this the default environment for new applications.",
    )

    def validate_slug(form, field):
        environment = (
            Environment.query.filter_by(project_id=form.project_id.data)
            .filter_by(slug=field.data)
            .first()
        )
        if environment is not None:
            raise ValidationError("Environment slugs must be unique within Projects.")
        return True


class EditEnvironmentForm(FlaskForm):
    environment_id = HiddenField(
        "Environment ID",
        [DataRequired()],
        description="ID of the Environment to edit.",
    )
    name = StringField(
        "Environment Name",
        [InputRequired()],
        description="Friendly name for this Environment.",
    )


class DeleteEnvironmentForm(FlaskForm):
    environment_id = HiddenField(
        "Environment ID",
        [DataRequired()],
        description="ID of the Environment to delete.",
    )
    name = StringField(
        "Slug",
        [InputRequired()],
        description="Slug of the Environment being deleted.",
    )
    confirm = StringField(
        "Type the slug of the Environment.",
        [
            EqualTo(
                "name",
                message="Must confirm the *exact* slug of the Environment!",
            )
        ],
    )


class EditOrganizationForm(FlaskForm):
    organization_id = HiddenField(
        "Organization ID",
        [DataRequired()],
        description="ID of the Organization to edit.",
    )
    name = StringField(
        "Organization Name",
        [InputRequired()],
        description="Friendly name for this Organization.",
    )


class DeleteOrganizationForm(FlaskForm):
    organization_id = HiddenField(
        "Organization ID",
        [DataRequired()],
        description="ID of the Organization to delete.",
    )
    name = StringField(
        "Slug",
        [InputRequired()],
        description="Slug of the Organization being deleted.",
    )
    confirm = StringField(
        "Type the slug of the Organization.",
        [
            EqualTo(
                "name",
                message="Must confirm the *exact* slug of the Organization!",
            )
        ],
    )


class DeleteApplicationEnvironmentForm(FlaskForm):
    app_env_id = HiddenField(
        "Application Environment ID",
        [DataRequired()],
        description="ID of the Application Environment to delete.",
    )
    name = StringField(
        "Slug",
        [InputRequired()],
        description="Slug of the Application being unenrolled.",
    )
    confirm = StringField(
        "Type the slug of the Application.",
        [
            EqualTo(
                "name",
                message="Must confirm the *exact* slug of the Application!",
            )
        ],
    )


class AddApplicationToEnvironmentForm(FlaskForm):
    application_id = SelectField(
        "Application",
        [DataRequired()],
        description="Application to add to this Environment.",
    )
    environment_id = HiddenField(
        "Environment",
        [DataRequired()],
        description="Environment to add the Application to.",
    )


class EditApplicationEnvironmentSettingsForm(FlaskForm):
    app_env_id = HiddenField(
        "Application Environment ID",
        [DataRequired()],
    )
    auto_deploy_branch = StringField(
        "Branch",
        [Optional()],
        description="Branch to auto-deploy for this environment (blank = inherit from app)",
    )
    auto_deploy_wait_for_ci = BooleanField(
        "Wait for CI",
        description="Wait for CI checks to pass before deploying. Uncheck to deploy immediately on push.",
    )
    github_environment_name = StringField(
        "GitHub Environment Name",
        [Optional()],
        description="GitHub environment name for this environment (blank = inherit from app or auto-generated from slugs)",
        filters=[(lambda x: x.strip() if x else x), (lambda x: x if x else None)],
    )
    deployment_timeout = IntegerField(
        "Deployment Timeout",
        [Optional()],
        description="Timeout (in seconds) when waiting for a deployment to complete",
    )
    health_check_path = StringField(
        "HTTP Health Check Path",
        [Optional()],
        description="Path for health check probes",
    )
    health_check_host = StringField(
        "HTTP Health Check Host Header",
        [Optional()],
        description="Host header for health check probes",
        filters=[(lambda x: x.strip() if x else x), (lambda x: x if x else None)],
    )


class IngressSettingsForm(FlaskForm):
    proxy_connect_timeout = StringField(
        "Connect Timeout",
        [
            Optional(),
            Regexp(
                r"^\d+s?$",
                message="Must be a number with optional 's' suffix (e.g. 10s, 60)",
            ),
        ],
        description="Proxy connect timeout (e.g. 10s, 60s)",
        filters=[(lambda x: x.strip() if x else x), (lambda x: x if x else None)],
    )
    proxy_read_timeout = StringField(
        "Read Timeout",
        [
            Optional(),
            Regexp(
                r"^\d+s?$",
                message="Must be a number with optional 's' suffix (e.g. 10s, 60)",
            ),
        ],
        description="Proxy read timeout (e.g. 10s, 60s)",
        filters=[(lambda x: x.strip() if x else x), (lambda x: x if x else None)],
    )
    proxy_send_timeout = StringField(
        "Send Timeout",
        [
            Optional(),
            Regexp(
                r"^\d+s?$",
                message="Must be a number with optional 's' suffix (e.g. 10s, 60)",
            ),
        ],
        description="Proxy send timeout (e.g. 10s, 60s)",
        filters=[(lambda x: x.strip() if x else x), (lambda x: x if x else None)],
    )
    proxy_body_size = StringField(
        "Max Body Size",
        [
            Optional(),
            Regexp(
                r"^\d+[kmKMgG]?$",
                message="Must be a number with optional size unit (e.g. 10M, 1024k, 1G)",
            ),
        ],
        description="Maximum request body size (e.g. 10M, 1024M)",
        filters=[(lambda x: x.strip() if x else x), (lambda x: x if x else None)],
    )
    client_body_buffer_size = StringField(
        "Client Body Buffer",
        [
            Optional(),
            Regexp(
                r"^\d+[kmKMgG]?$",
                message="Must be a number with optional size unit (e.g. 1M, 16k)",
            ),
        ],
        description="Client request body buffer size (e.g. 1M, 16k)",
        filters=[(lambda x: x.strip() if x else x), (lambda x: x if x else None)],
    )
    proxy_request_buffering = SelectField(
        "Request Buffering",
        choices=[("on", "On"), ("off", "Off")],
        description="Enable or disable request buffering",
    )
    session_affinity = BooleanField(
        "Session Affinity",
        description="Enable cookie-based session affinity",
    )
    use_regex = BooleanField(
        "Use Regex Paths",
        description="Enable regex path matching (nginx use-regex annotation)",
    )


class IngressHostForm(FlaskForm):
    hostname = StringField(
        "Hostname",
        [
            DataRequired(),
            Length(max=253),
            Regexp(
                r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)*$",
                message="Must be a valid DNS hostname (lowercase, alphanumeric, hyphens, dots)",
            ),
        ],
        description="Hostname for this ingress rule",
    )


class IngressPathForm(FlaskForm):
    path = StringField(
        "Path",
        [
            DataRequired(),
            Length(max=256),
            Regexp(r"^/", message="Path must start with /"),
        ],
        description="URL path to match",
    )
    path_type = SelectField(
        "Path Type",
        choices=[
            ("Prefix", "Prefix"),
            ("Exact", "Exact"),
            ("ImplementationSpecific", "ImplementationSpecific"),
        ],
        description="How to match the path",
    )
    target_process_name = SelectField(
        "Target Process",
        [DataRequired()],
        description="Process to route traffic to",
    )


class TailscaleIntegrationForm(FlaskForm):
    client_id = StringField(
        "Client ID",
        [InputRequired(), Length(max=255)],
        description="From the OIDC federated identity in Tailscale",
    )


class TailscaleIngressSettingsForm(FlaskForm):
    """Placeholder — tags are now derived from the platform config."""

    pass


class CreateEnvironmentConfigurationForm(FlaskForm):
    project_id = HiddenField(
        "Project",
        [DataRequired()],
        description="Project this Configuration belongs to.",
    )
    environment_id = HiddenField(
        "Environment",
        [DataRequired()],
        description="Environment this Configuration belongs to.",
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
        description="Set this Environment Variable during Image builds.",
    )

    def validate_name(form, field):
        configuration = EnvironmentConfiguration.query.filter_by(
            project_id=form.project_id.data,
            environment_id=form.environment_id.data,
            name=field.data,
        ).first()
        if configuration is not None:
            raise ValidationError(
                "Configuration names must be unique (case insensitive) "
                "within an environment"
            )
        return True


class EditEnvironmentConfigurationForm(FlaskForm):
    environment_configuration_id = HiddenField(
        "Environment Configuration ID",
        [DataRequired()],
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
        description="Set this Environment Variable during Image builds.",
    )

    def validate_name(form, field):
        configuration = EnvironmentConfiguration.query.filter_by(
            id=form.environment_configuration_id.data,
        ).first()
        if configuration is not None:
            if form.name.data == configuration.name:
                return True
            raise ValidationError(
                "Configuration names cannot be changed! Delete and re-create"
            )
        raise ValidationError("Configuration not found")


class DeleteEnvironmentConfigurationForm(FlaskForm):
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


class AddOrganizationUserForm(FlaskForm):
    identity = StringField(
        "Email or GitHub Username",
        [
            InputRequired(),
            Length(min=1, max=255),
        ],
        description="Email address or GitHub username of the user to add",
    )


# ---------------------------------------------------------------------------
# Backing service resource forms
# ---------------------------------------------------------------------------

_BACKUP_STRATEGY_CHOICES = [
    ("daily", "Daily Backups"),
    ("streaming", "Streaming (WAL Archiving)"),
    ("none", "None"),
]


def _validate_unique_resource_slug(form, slug):
    effective_slug = slug or slugify(form.name.data or "")
    if not effective_slug:
        raise ValidationError(
            "Name must contain at least one letter or number, or provide a slug."
        )
    existing = (
        Resource.query.filter_by(
            environment_id=form.environment_id.data,
            slug=effective_slug,
        )
        .filter(Resource.deleted_at.is_(None))
        .first()
    )
    if existing is not None:
        raise ValidationError("Resource slugs must be unique within environments.")
    return True


class CreatePostgresResourceForm(FlaskForm):
    environment_id = HiddenField(
        "Environment",
        [DataRequired()],
        description="Environment this resource belongs to.",
    )
    name = StringField(
        "Database Name",
        [InputRequired()],
        description="Friendly name for this PostgreSQL database.",
    )
    slug = StringField(
        "Database Slug",
        [
            Optional(),
            Regexp("^[-a-z0-9]+$", message="Invalid Slug! Must match ^[-a-z0-9]+$"),
        ],
        description=("URL-safe identifier. Auto-generated from name if left blank."),
    )
    service_version = SelectField(
        "PostgreSQL Version",
        [DataRequired()],
        choices=[(v, f"PostgreSQL {v}") for v in POSTGRES_VERSIONS],
        description="Major PostgreSQL version.",
    )
    size_class = SelectField(
        "Size Class",
        [DataRequired()],
        choices=[(k, k) for k in postgres_size_classes],
        description="CPU and memory allocation for the database.",
    )
    storage_size = IntegerField(
        "Storage Size (GB)",
        [InputRequired()],
        description="Persistent volume size in gigabytes. Maximum 1024 GB (1 TB).",
    )
    ha_enabled = BooleanField(
        "High Availability",
        [],
        description="Deploy with streaming replication for automatic failover.",
    )
    backup_strategy = SelectField(
        "Backup Strategy",
        [DataRequired()],
        choices=_BACKUP_STRATEGY_CHOICES,
        description="How often backups are taken.",
    )

    def validate_slug(form, field):
        return _validate_unique_resource_slug(form, field.data)

    def validate(form, extra_validators=None):
        if not super().validate(extra_validators=extra_validators):
            return False
        try:
            _validate_unique_resource_slug(form, form.slug.data)
        except ValidationError as exc:
            form.slug.errors = [*form.slug.errors, str(exc)]
            return False
        return True

    def validate_storage_size(form, field):
        if field.data is None:
            return True
        if field.data < 1 or field.data > 1024:
            raise ValidationError("Storage size must be between 1 and 1024 GB.")
        return True


class EditPostgresResourceForm(FlaskForm):
    resource_id = HiddenField(
        "Resource ID",
        [DataRequired()],
        description="ID of the resource to edit.",
    )
    current_storage_size = HiddenField("Current Storage Size")
    size_class = SelectField(
        "Size Class",
        [DataRequired()],
        choices=[(k, k) for k in postgres_size_classes],
        description="CPU and memory allocation for the database.",
    )
    storage_size = IntegerField(
        "Storage Size (GB)",
        [InputRequired()],
        description="Persistent volume size in gigabytes. Cannot be reduced.",
    )
    ha_enabled = BooleanField(
        "High Availability",
        [],
        description="Deploy with streaming replication for automatic failover.",
    )
    backup_strategy = SelectField(
        "Backup Strategy",
        [DataRequired()],
        choices=_BACKUP_STRATEGY_CHOICES,
        description="How often backups are taken.",
    )

    def validate_storage_size(form, field):
        if field.data is None:
            return True
        if field.data < 1 or field.data > 1024:
            raise ValidationError("Storage size must be between 1 and 1024 GB.")
        if form.current_storage_size.data:
            current = int(form.current_storage_size.data)
            if field.data < current:
                raise ValidationError(
                    f"Storage size cannot be reduced (currently {current} GB)."
                )
        return True


class DeletePostgresResourceForm(FlaskForm):
    resource_id = HiddenField(
        "Resource ID",
        [DataRequired()],
        description="ID of the resource to delete.",
    )
    name = StringField(
        "Slug",
        [InputRequired()],
        description="Slug of the resource being deleted.",
    )
    confirm = StringField(
        "Type the slug of the database to confirm.",
        [
            EqualTo(
                "name",
                message="Must confirm the *exact* slug of the database!",
            )
        ],
    )


class CreateRedisResourceForm(FlaskForm):
    environment_id = HiddenField(
        "Environment",
        [DataRequired()],
        description="Environment this resource belongs to.",
    )
    name = StringField(
        "Redis Name",
        [InputRequired()],
        description="Friendly name for this Redis instance.",
    )
    slug = StringField(
        "Redis Slug",
        [
            Optional(),
            Regexp("^[-a-z0-9]+$", message="Invalid Slug! Must match ^[-a-z0-9]+$"),
        ],
        description=("URL-safe identifier. Auto-generated from name if left blank."),
    )
    service_version = SelectField(
        "Redis Version",
        [DataRequired()],
        choices=[(v, f"Redis {v}") for v in REDIS_VERSIONS],
        description="Major Redis version.",
    )
    size_class = SelectField(
        "Size Class",
        [DataRequired()],
        choices=[(k, k) for k in redis_size_classes],
        description="CPU and memory allocation for the Redis instance.",
    )
    storage_size = IntegerField(
        "Storage Size (GB)",
        [InputRequired()],
        description="Persistent volume size in gigabytes. Maximum 1024 GB (1 TB).",
    )
    ha_enabled = BooleanField(
        "High Availability",
        [],
        description="Deploy as a Redis cluster with automatic failover.",
    )
    leader_replicas = IntegerField(
        "Leader Replicas",
        [InputRequired()],
        default=DEFAULT_REDIS_LEADER_REPLICAS,
        description="Number of Redis leader nodes when cluster mode is enabled.",
    )
    follower_replicas = IntegerField(
        "Follower Replicas",
        [InputRequired()],
        default=DEFAULT_REDIS_FOLLOWER_REPLICAS,
        description="Number of Redis follower nodes when cluster mode is enabled.",
    )

    def validate_slug(form, field):
        return _validate_unique_resource_slug(form, field.data)

    def validate(form, extra_validators=None):
        if not super().validate(extra_validators=extra_validators):
            return False
        try:
            _validate_unique_resource_slug(form, form.slug.data)
        except ValidationError as exc:
            form.slug.errors = [*form.slug.errors, str(exc)]
            return False
        return True

    def validate_storage_size(form, field):
        if field.data is None:
            return True
        if field.data < 1 or field.data > 1024:
            raise ValidationError("Storage size must be between 1 and 1024 GB.")
        return True

    def validate_leader_replicas(form, field):
        if field.data is None:
            return True
        if field.data < 1 or field.data > 32:
            raise ValidationError("Leader replicas must be between 1 and 32.")
        return True

    def validate_follower_replicas(form, field):
        if field.data is None:
            return True
        if field.data < 1 or field.data > 32:
            raise ValidationError("Follower replicas must be between 1 and 32.")
        return True


class EditRedisResourceForm(FlaskForm):
    resource_id = HiddenField(
        "Resource ID",
        [DataRequired()],
        description="ID of the resource to edit.",
    )
    current_storage_size = HiddenField("Current Storage Size")
    size_class = SelectField(
        "Size Class",
        [DataRequired()],
        choices=[(k, k) for k in redis_size_classes],
        description="CPU and memory allocation for the Redis instance.",
    )
    storage_size = IntegerField(
        "Storage Size (GB)",
        [InputRequired()],
        description="Persistent volume size in gigabytes. Cannot be reduced.",
    )
    leader_replicas = IntegerField(
        "Leader Replicas",
        [InputRequired()],
        default=DEFAULT_REDIS_LEADER_REPLICAS,
        description="Number of Redis leader nodes when cluster mode is enabled.",
    )
    follower_replicas = IntegerField(
        "Follower Replicas",
        [InputRequired()],
        default=DEFAULT_REDIS_FOLLOWER_REPLICAS,
        description="Number of Redis follower nodes when cluster mode is enabled.",
    )

    def validate_storage_size(form, field):
        if field.data is None:
            return True
        if field.data < 1 or field.data > 1024:
            raise ValidationError("Storage size must be between 1 and 1024 GB.")
        if form.current_storage_size.data:
            current = int(form.current_storage_size.data)
            if field.data < current:
                raise ValidationError(
                    f"Storage size cannot be reduced (currently {current} GB)."
                )
        return True

    def validate_leader_replicas(form, field):
        if field.data is None:
            return True
        if field.data < 1 or field.data > 32:
            raise ValidationError("Leader replicas must be between 1 and 32.")
        return True

    def validate_follower_replicas(form, field):
        if field.data is None:
            return True
        if field.data < 1 or field.data > 32:
            raise ValidationError("Follower replicas must be between 1 and 32.")
        return True


class DeleteRedisResourceForm(FlaskForm):
    resource_id = HiddenField(
        "Resource ID",
        [DataRequired()],
        description="ID of the resource to delete.",
    )
    name = StringField(
        "Slug",
        [InputRequired()],
        description="Slug of the resource being deleted.",
    )
    confirm = StringField(
        "Type the slug of the Redis instance to confirm.",
        [
            EqualTo(
                "name",
                message="Must confirm the *exact* slug of the Redis instance!",
            )
        ],
    )
