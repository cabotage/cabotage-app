from flask_security.forms import ConfirmRegisterForm, LoginForm, RegisterForm

from wtforms import StringField
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
