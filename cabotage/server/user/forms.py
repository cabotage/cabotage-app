from flask_security.forms import ConfirmRegisterForm, LoginForm, RegisterForm

from flask_wtf import FlaskForm

from wtforms import SelectField, StringField
from wtforms.validators import DataRequired, Length

from cabotage.server.models.auth import User


class ExtendedLoginForm(LoginForm):

    username = StringField('Username', [DataRequired()])

    def __init__(self, *args, **kwargs):
        super(ExtendedLoginForm, self).__init__(*args, **kwargs)
        user = User.query.filter_by(username=self.username.data).first()
        if user:
            self.email.data = user.email

class ExtendedRegisterForm(RegisterForm):

    username = StringField(
        'Username',
        validators=[
            DataRequired(),
            Length(min=3, max=40),
        ]
    )

class ExtendedConfirmRegisterForm(ConfirmRegisterForm):

    username = StringField(
        'Username',
        validators=[
            DataRequired(),
            Length(min=3, max=40),
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
