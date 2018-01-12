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

    organization_id = SelectField(u'Organization', [DataRequired()]) 
    name = StringField(u'Project Name', [DataRequired()])
    slug = StringField(u'Project Slug', [DataRequired()])


class CreateOrganizationForm(FlaskForm):

    name = StringField(u'Organization Name', [DataRequired()])
    slug = StringField(u'Organization Slug', [DataRequired()])


class CreatePipelineForm(FlaskForm):
    organization_id = SelectField(u'Organization', [DataRequired()])
    project_id = SelectField(u'Project', [DataRequired()])
    name = StringField(u'Pipeline Name', [DataRequired()])
    slug = StringField(u'Pipeline Slug', [DataRequired()])


class CreateApplicationForm(FlaskForm):

    organization_id = SelectField(u'Organization', [DataRequired()]) 
    project_id = SelectField(u'Project', [DataRequired()])
    name = StringField(u'Application Name', [DataRequired()])
    slug = StringField(u'Application Slug', [DataRequired()])
