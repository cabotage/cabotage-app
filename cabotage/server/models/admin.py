from flask import abort
from flask_admin.base import AdminIndexView as _AdminIndexView
from flask_admin.contrib import sqla
from flask_admin.form import SecureForm
from flask_security import current_user


class AdminIndexView(_AdminIndexView):
    def is_accessible(self):
        return current_user.is_authenticated and current_user.admin

    def _handle_view(self, name, **kwargs):
        if not self.is_accessible():
            abort(403)


class AdminModelView(sqla.ModelView):
    form_base_class = SecureForm

    can_create = False
    can_edit = False
    can_delete = False

    can_view_details = True
    can_set_page_size = True

    def is_accessible(self):
        return current_user.is_authenticated and current_user.admin

    def _handle_view(self, name, **kwargs):
        if not self.is_accessible():
            abort(403)

    def _get_endpoint(self, endpoint):
        return f"_{super()._get_endpoint(endpoint)}"


class UserAdminModelView(AdminModelView):
    _secret_columns = (
        "password",
        "tf_totp_secret",
        "tf_recovery_codes",
        "mf_recovery_codes",
        "us_totp_secrets",
        "us_phone_number",
        "fs_uniquifier",
        "fs_token_uniquifier",
        "fs_webauthn_user_handle",
    )

    column_exclude_list = _secret_columns
    column_details_exclude_list = _secret_columns
    column_export_exclude_list = _secret_columns
