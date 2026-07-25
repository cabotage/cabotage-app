from urllib.parse import urlsplit, urlunsplit

from flask import abort, Blueprint, current_app, jsonify, request
from flask_login import current_user

from cabotage.server import db
from cabotage.server.models.feedback import Feedback, FEEDBACK_KINDS

MAX_MESSAGE_LENGTH = 5000
MAX_URL_LENGTH = 2048

feedback_bp = Blueprint("feedback", __name__)


def init_feedback(app):
    app.register_blueprint(feedback_bp)


def _clamp(value, limit):
    """Normalize to a stripped, truncated string, or None if empty."""
    return (value or "").strip()[:limit] or None


def _clean_page_url(raw):
    """Strip query strings and fragments — they can carry tokens or PII."""
    if not raw:
        return None
    scheme, netloc, path, _, _ = urlsplit(raw[:MAX_URL_LENGTH])
    return urlunsplit((scheme, netloc, path, "", "")) or None


def _clean_view_args(raw):
    if not isinstance(raw, dict):
        return {}
    return {str(key)[:64]: str(value)[:256] for key, value in raw.items()}


def _notify(feedback_id):
    # Deferred import avoids an import cycle at module load. Callers dispatch
    # after commit so workers see the row; a dead broker must not fail the
    # submission.
    from cabotage.celery.tasks.notify import dispatch_feedback_notification

    try:
        dispatch_feedback_notification.delay(str(feedback_id))
    except Exception:
        current_app.logger.warning(
            "Failed to enqueue feedback notification %s", feedback_id
        )


@feedback_bp.route("/feedback", methods=["POST"])
def submit_feedback():
    if not current_app.config.get("FEEDBACK_WIDGET_ENABLED", False):
        abort(404)

    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Feedback message is required."}), 400
    if len(message) > MAX_MESSAGE_LENGTH:
        return jsonify({"error": "Feedback message is too long."}), 400

    kind = data.get("kind")
    feedback = Feedback(
        user_id=current_user.id if current_user.is_authenticated else None,
        email=_clamp(data.get("email"), 255),
        kind=kind if kind in FEEDBACK_KINDS else "other",
        message=message,
        page_url=_clean_page_url(data.get("page_url")),
        page_title=_clamp(data.get("page_title"), 512),
        endpoint=_clamp(data.get("endpoint"), 128),
        view_args=_clean_view_args(data.get("view_args")),
        user_agent=_clamp(request.headers.get("User-Agent"), 512),
        viewport=_clamp(data.get("viewport"), 32),
        theme=_clamp(data.get("theme"), 16),
        remote_addr=_clamp(request.remote_addr, 64),
    )
    db.session.add(feedback)
    db.session.commit()

    _notify(feedback.id)
    return jsonify({"ok": True}), 201
