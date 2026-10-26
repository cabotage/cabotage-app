"""Read-only cost dashboard blueprint.

Always registered; every route 404s unless ``SHOW_COST_ESTIMATES`` is enabled.
"""

from cabotage.server.cost.views import cost_blueprint

__all__ = ["cost_blueprint"]
