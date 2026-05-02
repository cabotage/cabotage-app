import hmac
import logging

from flask import Blueprint, abort, current_app, jsonify, request

from cabotage.server import db
from cabotage.server.alerting.ingest import parse_alertmanager_timestamp, upsert_alert

log = logging.getLogger(__name__)

alerting_blueprint = Blueprint("alerting", __name__)


def _validate_bearer_token():
    secret = current_app.config.get("ALERTMANAGER_WEBHOOK_SECRET")
    if not secret:
        log.warning("ALERTMANAGER_WEBHOOK_SECRET not configured, rejecting request")
        return False

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False

    token = auth_header[len("Bearer ") :]
    return hmac.compare_digest(token, secret)


@alerting_blueprint.route("/alertmanager/webhooks", methods=["POST"])
def alertmanager_webhook():
    if not _validate_bearer_token():
        abort(403)

    payload = request.get_json(silent=True)
    if not payload:
        abort(400)

    alerts_data = payload.get("alerts", [])
    if not alerts_data:
        return jsonify({"status": "ok", "alerts_processed": 0})

    group_key = payload.get("groupKey")
    receiver = payload.get("receiver")

    alerts_processed = 0
    dispatch_ids = []
    for alert_data in alerts_data:
        labels = alert_data.get("labels", {})
        processed, dispatch_id = upsert_alert(
            fingerprint=alert_data.get("fingerprint", ""),
            status=alert_data.get("status", "unknown"),
            alertname=labels.get("alertname", "unknown"),
            labels=labels,
            annotations=alert_data.get("annotations", {}),
            starts_at=parse_alertmanager_timestamp(alert_data.get("startsAt")),
            ends_at=parse_alertmanager_timestamp(alert_data.get("endsAt")),
            generator_url=alert_data.get("generatorURL"),
            group_key=group_key,
            receiver=receiver,
        )
        if processed:
            alerts_processed += 1
        if dispatch_id:
            dispatch_ids.append(dispatch_id)

    db.session.commit()

    # Dispatch notifications after commit so workers can see the data
    from cabotage.celery.tasks.notify import dispatch_alert_notification

    for alert_id in dispatch_ids:
        dispatch_alert_notification.delay(str(alert_id))

    log.info(
        "Processed alertmanager webhook: %d alerts (%s)",
        alerts_processed,
        payload.get("status", "unknown"),
    )

    return jsonify({"status": "ok", "alerts_processed": alerts_processed})
