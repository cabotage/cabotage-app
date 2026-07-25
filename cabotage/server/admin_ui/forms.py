from flask_wtf import FlaskForm
from wtforms import BooleanField, HiddenField, StringField
from wtforms.validators import DataRequired


class AdminUserActionForm(FlaskForm):
    """CSRF-only form for user _action posts."""


class AdminOrgActionForm(FlaskForm):
    """CSRF-only form for organization lifecycle _action posts."""


class AdminProjectActionForm(FlaskForm):
    """CSRF-only form for project lifecycle _action posts."""


class AdminOrgMemberActionForm(FlaskForm):
    user_id = HiddenField("User ID", validators=[DataRequired()])


class AdminAddOrgMemberForm(FlaskForm):
    identity = StringField(
        "Email or Username",
        validators=[DataRequired()],
        description="Email address or username of the user to add",
    )
    admin = BooleanField("Organization Admin", default=False)
