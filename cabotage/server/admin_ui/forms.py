from flask_wtf import FlaskForm
from wtforms import BooleanField, HiddenField, StringField
from wtforms.validators import DataRequired


class AdminActionForm(FlaskForm):
    """CSRF-only form for `_action` posts (user, org, and project actions)."""


class AdminOrgMemberActionForm(FlaskForm):
    user_id = HiddenField("User ID", validators=[DataRequired()])


class AdminAddOrgMemberForm(FlaskForm):
    identity = StringField(
        "Email or Username",
        validators=[DataRequired()],
        description="Email address or username of the user to add",
    )
    admin = BooleanField("Organization Admin", default=False)
