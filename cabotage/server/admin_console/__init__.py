"""Full-platform admin console (``/admin``), gated by passkey elevation."""

from cabotage.server.admin_console.views import admin_console_blueprint

__all__ = ("admin_console_blueprint",)
