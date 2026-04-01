import datetime
import threading

from flask import request as flask_request
from flask_login import user_logged_out
from flask_security import (
    tf_code_confirmed,
    tf_disabled,
    tf_profile_changed,
    user_authenticated,
    wan_deleted,
    wan_registered,
)

_queue = threading.local()


def _enqueue(user, verb, action=None):
    if not hasattr(_queue, "events"):
        _queue.events = []
    remote_addr = None
    try:
        remote_addr = flask_request.remote_addr
    except RuntimeError:
        pass
    _queue.events.append(
        {
            "user_id": str(user.id),
            "verb": verb,
            "action": action,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "remote_addr": remote_addr,
        }
    )


def _on_authenticated(sender, user, **kwargs):
    _enqueue(user, "login")


def _on_logged_out(sender, user, **kwargs):
    if user:
        _enqueue(user, "logout")


def _on_mfa(action):
    def handler(sender, user=None, **kwargs):
        if user:
            _enqueue(user, "edit", action=action)

    return handler


def init_audit(app):
    from cabotage.server import db
    from cabotage.server.models.auth import User
    from cabotage.server.models.projects import activity_plugin

    Activity = activity_plugin.activity_cls

    @app.after_request
    def commit_audit_events(response):
        events = getattr(_queue, "events", None)
        if not events:
            return response
        _queue.events = []
        try:
            for evt in events:
                user = db.session.get(User, evt["user_id"])
                if not user:
                    continue
                data = {
                    "user_id": evt["user_id"],
                    "timestamp": evt["timestamp"],
                }
                if evt["action"]:
                    data["action"] = evt["action"]
                if evt.get("remote_addr"):
                    data["remote_addr"] = evt["remote_addr"]
                a = Activity(verb=evt["verb"], object=user, data=data)
                db.session.add(a)
            db.session.commit()
        except Exception:
            db.session.rollback()
        return response

    # Connect signals (weak=False prevents GC of closures)
    user_authenticated.connect(_on_authenticated, weak=False)
    user_logged_out.connect(_on_logged_out, weak=False)
    tf_code_confirmed.connect(_on_mfa("totp_setup"), weak=False)
    tf_disabled.connect(_on_mfa("totp_disabled"), weak=False)
    tf_profile_changed.connect(_on_mfa("totp_profile_changed"), weak=False)
    wan_registered.connect(_on_mfa("webauthn_registered"), weak=False)
    wan_deleted.connect(_on_mfa("webauthn_deleted"), weak=False)
