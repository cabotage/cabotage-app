from flask import abort, url_for, redirect
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
