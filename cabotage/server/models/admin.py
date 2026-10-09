from flask_admin.base import AdminIndexView as _AdminIndexView
from flask_admin.contrib import sqla
from flask_admin.form import SecureForm
from werkzeug.wrappers import Response
from cabotage.server.admin_passkey import has_admin_session, require_admin_session


class AdminIndexView(_AdminIndexView):
    def is_accessible(self) -> bool:
        return has_admin_session()

    def _handle_view(self, name: str, **kwargs: object) -> Response | None:
        return require_admin_session()


class AdminModelView(sqla.ModelView):
    form_base_class = SecureForm

    can_create = False
    can_edit = False
    can_delete = False

    can_view_details = True
    can_set_page_size = True

    def is_accessible(self) -> bool:
        return has_admin_session()

    def _handle_view(self, name: str, **kwargs: object) -> Response | None:
        return require_admin_session()

    def _get_endpoint(self, endpoint: str | None) -> str:
        return f"_{super()._get_endpoint(endpoint)}"


class UserAdminModelView(AdminModelView):
    column_exclude_list = [
        "password",
        "tf_totp_secret",
        "us_totp_secrets",
        "mf_recovery_codes",
        "fs_uniquifier",
        "fs_token_uniquifier",
        "fs_webauthn_user_handle",
    ]
    column_details_exclude_list = column_exclude_list
