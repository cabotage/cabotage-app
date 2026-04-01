"""Celery task to reconcile alerts with Alertmanager.

Polls the Alertmanager v2 API for all currently active alerts, upserts them
into the local alerts table, and marks any locally-firing alerts that are
no longer present in Alertmanager as resolved.
"""

import logging
from datetime import UTC, datetime

import requests
from celery import shared_task
from flask import current_app

from cabotage.server import db
from cabotage.server.models.projects import Alert
from cabotage.server.alerting.ingest import (
    _record_activity,
    parse_alertmanager_timestamp,
    upsert_alert,
)

log = logging.getLogger(__name__)


@shared_task()
def reconcile_alerts():
    alertmanager_url = current_app.config.get("ALERTMANAGER_URL")
    if not alertmanager_url:
        return

    verify = current_app.config.get("ALERTMANAGER_VERIFY")
    if verify is None:
        verify = True

    secret = current_app.config.get("ALERTMANAGER_WEBHOOK_SECRET")
    headers = {}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"

    try:
        resp = requests.get(
            f"{alertmanager_url.rstrip('/')}/api/v2/alerts",
            headers=headers,
            verify=verify,
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException:
        log.exception("Failed to fetch alerts from Alertmanager")
        return

    active_alerts = resp.json()

    # Track which fingerprints are still active in Alertmanager
    seen_fingerprints = set()

    for alert_data in active_alerts:
        labels = alert_data.get("labels", {})
        fingerprint = alert_data.get("fingerprint", "")

        starts_at = parse_alertmanager_timestamp(alert_data.get("startsAt"))
        if starts_at:
            seen_fingerprints.add((fingerprint, starts_at))

        # The v2 API nests status as {"state": "active|suppressed|..."}
        status = alert_data.get("status", {})
        if isinstance(status, dict):
            state = status.get("state", "active")
        else:
            state = status
        am_status = "firing" if state == "active" else state

        upsert_alert(
            fingerprint=fingerprint,
            status=am_status,
            alertname=labels.get("alertname", "unknown"),
            labels=labels,
            annotations=alert_data.get("annotations", {}),
            starts_at=starts_at,
            ends_at=parse_alertmanager_timestamp(alert_data.get("endsAt")),
            generator_url=alert_data.get("generatorURL"),
        )

    # Resolve any locally-firing alerts that are no longer in Alertmanager
    firing_alerts = Alert.query.filter_by(status="firing").all()
    now = datetime.now(UTC).replace(tzinfo=None)
    resolved_count = 0
    for alert in firing_alerts:
        if (alert.fingerprint, alert.starts_at) not in seen_fingerprints:
            alert.status = "resolved"
            alert.ends_at = now
            _record_activity("resolved", alert, alert.application)
            resolved_count += 1

    db.session.commit()
    log.info(
        "Reconciled alerts: %d active from Alertmanager, %d resolved",
        len(active_alerts),
        resolved_count,
    )
