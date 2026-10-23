from flask_wtf import FlaskForm
from wtforms import HiddenField, StringField
from wtforms.validators import AnyOf, InputRequired, Length

_BOOLEAN = ("true", "false")


class SetActiveForm(FlaskForm):
    active = HiddenField(validators=[InputRequired(), AnyOf(_BOOLEAN)])


class SetAdminForm(FlaskForm):
    admin = HiddenField(validators=[InputRequired(), AnyOf(_BOOLEAN)])


class ResetMfaForm(FlaskForm):
    confirm = StringField(
        "Type the username to confirm",
        validators=[InputRequired(), Length(max=255)],
    )
