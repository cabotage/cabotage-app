import logging
import uuid
from typing import Protocol, cast

from celery import shared_task

from cabotage.server.integrations.grafana import (
    GrafanaIntegrationError,
    grafana_configured,
    reconcile_grafana_organization,
)
from cabotage.server.models.grafana import OrganizationGrafanaIntegration

log = logging.getLogger(__name__)


@shared_task()
def reconcile_organization(organization_id: str) -> None:
    """Reconcile a single organization's Grafana org/users/datasources/dashboards."""
    try:
        reconcile_grafana_organization(organization_id)
    except GrafanaIntegrationError as exc:
        # GrafanaIntegrationError messages are already sanitized (built
        # from method/path/status only); never log exc_info/traceback here,
        # which could otherwise surface response bodies or headers.
        log.warning(
            "Grafana reconciliation failed for organization %s: %s",
            organization_id,
            exc,
        )


class _ReconcileOrganizationTask(Protocol):
    """Typed ``.delay`` surface for ``reconcile_organization`` (celery's untyped Proxy confuses static analysis)."""

    def delay(self, organization_id: str) -> None: ...


_reconcile_organization_task = cast(_ReconcileOrganizationTask, reconcile_organization)


def enqueue_reconcile_organization(organization_id: str) -> None:
    """Typed wrapper for ``reconcile_organization.delay(...)``."""
    _reconcile_organization_task.delay(organization_id)


@shared_task()
def reconcile_organizations() -> None:
    """Periodic task: reconcile Grafana for every organization with an integration."""
    if not grafana_configured():
        return

    organization_ids: list[str] = [
        str(cast(uuid.UUID, row[0]))
        for row in OrganizationGrafanaIntegration.query.with_entities(
            OrganizationGrafanaIntegration.organization_id
        ).all()
    ]
    for organization_id in organization_ids:
        enqueue_reconcile_organization(organization_id)
