"""Global account controls: activation, global-admin flag, MFA reset.

Each mutation runs in one transaction that first locks every active global
admin row in ``User.id`` order and then the target row, re-reads them, and
re-checks the self-lockout and last-usable-admin rules before changing
anything. Concurrent console actions therefore serialize on the same locks
and cannot jointly remove the last usable admin.

Callers must obtain the passkey proof (``require_admin_action``) before
calling in here: proof issuance/consumption commits on its own connection and
must never wait behind the row locks taken below.
"""

import datetime
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from flask import current_app
from flask_security.datastore import SQLAlchemyUserDatastore
from sqlalchemy import exists, select

from cabotage.server import db
from cabotage.server.admin_passkey import revoke_admin_access
from cabotage.server.models.auth import User, WebAuthn
from cabotage.server.models.projects import activity_plugin

ACTION_SET_ACTIVE = "admin_console.user.set_active"
ACTION_SET_ADMIN = "admin_console.user.set_admin"
ACTION_RESET_MFA = "admin_console.user.reset_mfa"

# Changes that can leave the platform without a usable admin.
DEACTIVATE = "deactivate"
DEMOTE = "demote"
RESET_MFA = "reset_mfa"

_VERBS = {
    DEACTIVATE: "deactivate",
    DEMOTE: "remove global admin from",
    RESET_MFA: "reset MFA for",
}


class AccountActionRefused(Exception):
    """The change would lock out the actor or the platform."""


@dataclass(frozen=True)
class AccountChange:
    user: User
    changed: bool


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _datastore() -> SQLAlchemyUserDatastore:
    return cast(SQLAlchemyUserDatastore, current_app.extensions["security"].datastore)


def _usable_admin_ids() -> set[uuid.UUID]:
    return set(
        db.session.scalars(
            select(User.id).where(
                User.admin.is_(True),
                User.__table__.c.active.is_(True),
                exists().where(WebAuthn.user_id == User.id),
            )
        )
    )


def refusal(
    change: str,
    target: User,
    actor_id: uuid.UUID,
    usable_admins: set[uuid.UUID],
) -> str | None:
    """Explain why ``change`` must not be applied, or return None."""
    if change not in _VERBS:
        return None
    if target.id == actor_id:
        return f"You can't {_VERBS[change]} your own account from the console."
    if target.id in usable_admins and len(usable_admins) <= 1:
        return (
            f"{target.username or target.email} is the last active global admin "
            "with a passkey."
        )
    return None


def _lock_target(target_id: uuid.UUID) -> User:
    # Deterministic order: all active admins by id, then the target.
    _ = db.session.execute(
        select(User.id)
        .where(User.admin.is_(True), User.__table__.c.active.is_(True))
        .order_by(User.id)
        .with_for_update()
    ).all()
    target = db.session.scalars(
        select(User)
        .where(User.id == target_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if target is None:
        raise LookupError(str(target_id))
    return target


def _guard(change: str, target: User, actor_id: uuid.UUID) -> None:
    reason = refusal(change, target, actor_id, _usable_admin_ids())
    if reason:
        raise AccountActionRefused(reason)


def _record(target: User, actor_id: uuid.UUID, action: str) -> None:
    Activity = activity_plugin.activity_cls
    db.session.add(
        Activity(
            verb="edit",
            object=target,
            data={
                "user_id": str(actor_id),
                "action": action,
                "target_user_id": str(target.id),
                "timestamp": _now_iso(),
            },
        )
    )


def _run(target_id: uuid.UUID, apply: Callable[[User], bool]) -> AccountChange:
    try:
        target = _lock_target(target_id)
        changed = apply(target)
        if changed:
            db.session.commit()
        else:
            db.session.rollback()
        return AccountChange(user=target, changed=changed)
    except Exception:
        db.session.rollback()
        raise


def set_active(
    target_id: uuid.UUID, actor_id: uuid.UUID, active: bool
) -> AccountChange:
    datastore = _datastore()

    def apply(target: User) -> bool:
        if target.active == active:
            return False
        if active:
            _ = datastore.activate_user(target)
            _record(target, actor_id, "admin_activate_user")
            return True
        _guard(DEACTIVATE, target, actor_id)
        _ = datastore.deactivate_user(target)
        # Sign the account out everywhere; reactivation needs a fresh login.
        _ = datastore.set_uniquifier(target)
        _ = datastore.set_token_uniquifier(target)
        revoke_admin_access(target.id)
        _record(target, actor_id, "admin_deactivate_user")
        return True

    return _run(target_id, apply)


def set_admin(target_id: uuid.UUID, actor_id: uuid.UUID, admin: bool) -> AccountChange:
    def apply(target: User) -> bool:
        if target.admin == admin:
            return False
        if not admin:
            _guard(DEMOTE, target, actor_id)
            revoke_admin_access(target.id)
        target.admin = admin
        _record(
            target,
            actor_id,
            "admin_grant_global_admin" if admin else "admin_revoke_global_admin",
        )
        return True

    return _run(target_id, apply)


def reset_mfa(target_id: uuid.UUID, actor_id: uuid.UUID) -> AccountChange:
    datastore = _datastore()

    def apply(target: User) -> bool:
        _guard(RESET_MFA, target, actor_id)
        # Revoke elevation first, then let Flask-Security remove passkeys,
        # TOTP/unified sign-in secrets and recovery codes and rotate the
        # session and token uniquifiers. The password is left untouched.
        revoke_admin_access(target.id)
        _ = datastore.reset_user_access(target)
        _record(target, actor_id, "admin_reset_mfa")
        return True

    return _run(target_id, apply)
