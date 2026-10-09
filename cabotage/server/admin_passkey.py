"""Native WebAuthn elevation, separate from ordinary account authentication."""

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import cast
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from flask import (
    Blueprint,
    Flask,
    abort,
    current_app,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask import Response as FlaskResponse
from flask_login import user_logged_in, user_logged_out
from flask_login.utils import current_user, login_required
from flask_security.signals import wan_deleted
from flask_wtf.csrf import generate_csrf, validate_csrf
from sqlalchemy import ColumnElement, delete, insert, select, update
from webauthn import generate_authentication_options, options_to_json
from webauthn import verify_authentication_response
from webauthn.helpers import parse_authentication_credential_json
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    PublicKeyCredentialDescriptor,
    UserVerificationRequirement,
)
from wtforms.validators import ValidationError
from werkzeug.wrappers import Response

from cabotage.server import db, security
from cabotage.server.models.admin_security import AdminChallenge, AdminGrant
from cabotage.server.models.auth import OrganizationRequest, User, WebAuthn

blueprint = Blueprint("admin_passkey", __name__, url_prefix="/admin/passkey")
ENTRY_LIFETIME = timedelta(minutes=15)
ELEVATED_POST_ENDPOINTS = {
    "github_oauth.callback",
    "slack_oauth.callback",
    "discord_oauth.callback",
    "user.project_application_shell",
}
ACTION_LIFETIME = timedelta(minutes=2)
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _admin() -> User:
    if not current_user.is_authenticated:
        abort(401)
    user = db.session.get(User, cast(User, current_user).id, populate_existing=True)
    if user is None or not user.active or not user.admin:
        abort(403)
    return user


def _csrf() -> None:
    # Explicit even if an unrelated caller has been exempted from CSRFProtect.
    if current_app.config.get("WTF_CSRF_ENABLED", True):
        try:
            validate_csrf(
                request.headers.get("X-CSRFToken") or request.form.get("csrf_token")
            )
        except ValidationError:
            abort(400, "Invalid CSRF token")


def _binding() -> str:
    binding = session.get("admin_binding")
    if not isinstance(binding, str):
        binding = secrets.token_urlsafe(32)
        session["admin_binding"] = binding
    return binding


def _grant_id() -> str | None:
    value = session.get("admin_grant")
    return value if isinstance(value, str) else None


def _grant_expiration(
    grant_id: str | None, binding: str | None, user_id: UUID
) -> datetime | None:
    if not grant_id or not binding:
        return None
    # A separate, short transaction avoids identity-map and long-lived stream snapshots.
    with db.engine.connect() as connection:
        return connection.execute(
            select(AdminGrant.expires_at)
            .join(User, User.id == AdminGrant.user_id)
            .join(WebAuthn, WebAuthn.id == AdminGrant.credential_id)
            .where(
                AdminGrant.id == grant_id,
                AdminGrant.user_id == user_id,
                AdminGrant.session_binding == binding,
                AdminGrant.uniquifier == User.fs_uniquifier,
                AdminGrant.expires_at > _now(),
                User.admin.is_(True),
                User.__table__.c.active.is_(True),
                WebAuthn.user_id == User.id,
            )
        ).scalar_one_or_none()


def has_admin_session() -> bool:
    user = cast(User, current_user)
    return user.is_authenticated and (
        _grant_expiration(_grant_id(), session.get("admin_binding"), user.id)
        is not None
    )


def _epoch(value: datetime) -> float:
    return value.replace(tzinfo=timezone.utc).timestamp()


def admin_access_state() -> dict[str, object]:
    """Display metadata only; every protected request still checks its own grant."""
    user = cast(User, current_user)
    expires_at = (
        _grant_expiration(_grant_id(), session.get("admin_binding"), user.id)
        if user.is_authenticated
        else None
    )
    return {
        "active": expires_at is not None,
        "expires_at": _epoch(expires_at) if expires_at is not None else None,
        "server_time": _epoch(_now()),
    }


def _local_url(value: str | None) -> str:
    if (
        value
        and value.startswith("/")
        and not value.startswith("//")
        and "\\" not in value
    ):
        parsed = urlsplit(value)
        if not parsed.scheme and not parsed.netloc:
            return value
    return "/admin/"


def require_admin_session() -> Response | None:
    _ = _admin()
    if has_admin_session():
        return None
    if request.method not in SAFE_METHODS and (
        request.is_json or request.headers.get("X-Admin-Fetch") == "1"
    ):
        _csrf()
        return _verification_response(_context("entry"))
    return redirect(url_for("admin_passkey.entry", next=_local_url(request.full_path)))


def request_payload() -> dict[str, object]:
    """Canonical body; retain repeated fields and exclude only proof/CSRF transport."""
    files: dict[str, list[dict[str, str | None]]] = {}
    for key in sorted(request.files):
        files[key] = []
        for upload in request.files.getlist(key):
            position = upload.stream.tell()
            digest = hashlib.sha256()
            try:
                _ = upload.stream.seek(0)
                while chunk := upload.stream.read(65536):
                    digest.update(chunk)
            finally:
                _ = upload.stream.seek(position)
            files[key].append(
                {
                    "filename": upload.filename,
                    "content_type": upload.content_type,
                    "sha256": digest.hexdigest(),
                }
            )
    if request.is_json:
        return {"json": request.get_json()}
    if request.mimetype in {
        "application/x-www-form-urlencoded",
        "multipart/form-data",
    }:
        return {
            "form": {
                key: request.form.getlist(key)
                for key in sorted(request.form)
                if key not in {"_admin_action", "csrf_token"}
            },
            "files": files,
        }
    return {"body": request.get_data(as_text=True)}


def _digest(action: str, target: str, payload: dict[str, object]) -> str:
    encoded = json.dumps(
        [
            action,
            target,
            payload,
            request.method,
            request.path,
            sorted((key, request.args.getlist(key)) for key in request.args),
            request_payload(),
        ],
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _action_summary(action: str, target: str) -> dict[str, str]:
    summary = cast(dict[str, str] | None, getattr(g, "admin_action_summary", None))
    if summary is not None:
        return summary
    endpoint = action.rsplit(".", 1)[-1]
    title = endpoint.replace("_", " ").capitalize()
    consequence = (
        "Submit this exact request using admin access. "
        "The submitted values are not displayed here."
    )
    if endpoint == "organization_settings":
        changes = {
            "save_org": (
                "Update organization",
                "Save the submitted organization settings.",
            ),
            "save_tailscale": (
                "Update Tailscale integration",
                "Replace this organization's Tailscale credentials.",
            ),
            "delete_tailscale": (
                "Remove Tailscale integration",
                "Remove this organization's integration and its Tailscale operators.",
            ),
        }
        title, consequence = changes.get(
            request.form.get("_action", ""), (title, consequence)
        )
    elif endpoint == "release_deploy":
        title = "Deploy release"
        consequence = (
            "Start a deployment of this release. "
            "Running application processes may be replaced."
        )
    elif endpoint == "application_scale":
        title = "Scale application"
        consequence = (
            "Change process replica counts using the submitted scaling settings."
        )
    elif endpoint == "project_application_shell":
        title = "Open application shell"
        consequence = (
            "Open an interactive shell with access to this application's "
            "runtime and environment."
        )
    elif endpoint in {
        "organization_delete",
        "project_delete",
        "project_application_delete",
    }:
        title, consequence = {
            "organization_delete": (
                "Delete organization",
                "Delete this organization and every project, application, "
                "environment and backing service in it. Queue removal of its "
                "application Kubernetes resources and any Tailscale operator. "
                "This cannot be undone here.",
            ),
            "project_delete": (
                "Delete project",
                "Delete every application, environment and backing service in "
                "this project, and queue removal of its application Kubernetes "
                "resources. Other projects remain. This cannot be undone here.",
            ),
            "project_application_delete": (
                "Delete application",
                "Delete this application in every environment and queue removal "
                "of its Kubernetes resources. Other applications and shared "
                "environments remain. This cannot be undone here.",
            ),
        }[endpoint]
    elif endpoint.endswith("_delete"):
        consequence = (
            "Delete the selected resource. Its dependent resources may also be "
            "removed; this cannot be undone here."
        )
    elif "config" in endpoint:
        consequence = (
            "Change configuration for this resource. "
            "Secret values are intentionally omitted from this review."
        )
    elif action in {
        "github_oauth.callback",
        "slack_oauth.callback",
        "discord_oauth.callback",
    }:
        title = f"Connect {action.split('_', 1)[0].capitalize()}"
        consequence = (
            "Complete the pending integration authorization "
            "for the selected organization."
        )
    elif endpoint in {
        "organization_add_user",
        "organization_remove_user",
        "organization_promote_user",
        "organization_demote_user",
    }:
        title, consequence = {
            "organization_add_user": (
                "Add organization member",
                "Give the selected account member access to this organization.",
            ),
            "organization_remove_user": (
                "Remove organization member",
                "Remove the selected account's membership in this organization.",
            ),
            "organization_promote_user": (
                "Grant organization admin",
                "Allow the selected member to administer this organization.",
            ),
            "organization_demote_user": (
                "Remove organization admin",
                "Keep membership but remove organization administration rights.",
            ),
        }[endpoint]
    elif endpoint in {"organization_request_approve", "organization_request_deny"}:
        if endpoint == "organization_request_approve":
            title = "Approve organization request"
            consequence = (
                "Create the proposed organization and grant its requester "
                "organization admin access."
            )
        else:
            title = "Deny organization request"
            consequence = (
                "Reject this organization request. No organization or "
                "membership will be created."
            )
    arguments = request.view_args or {}
    labels = [
        f"{label}: {arguments[key]}"
        for key, label in (
            ("org_slug", "Organization"),
            ("project_slug", "Project"),
            ("env_slug", "Environment"),
            ("app_slug", "Application"),
            ("release_id", "Release"),
            ("config_id", "Configuration"),
            ("resource_slug", "Resource"),
        )
        if arguments.get(key) is not None
    ]
    if endpoint in {"organization_request_approve", "organization_request_deny"}:
        try:
            request_id = UUID(str(arguments.get("request_id", "")))
        except ValueError:
            request_id = None
        org_request = (
            db.session.get(OrganizationRequest, request_id)
            if request_id is not None
            else None
        )
        if org_request is not None:
            requester = org_request.requester
            labels.extend(
                (
                    f"Proposed organization: {org_request.name} ({org_request.slug})",
                    f"Requester: {requester.username or requester.email}",
                    f"Request: {org_request.id}",
                )
            )
    if action in {"slack_oauth.callback", "discord_oauth.callback"}:
        org_slug = session.get(f"{action.split('.', 1)[0]}_org_slug")
        if isinstance(org_slug, str):
            labels.append(f"Organization: {org_slug}")
    if endpoint in {
        "organization_remove_user",
        "organization_promote_user",
        "organization_demote_user",
    }:
        try:
            member_id = UUID(request.form.get("user_id", ""))
        except ValueError:
            member_id = None
        member = db.session.get(User, member_id) if member_id is not None else None
        if member is not None:
            labels.append(f"Account: {member.username or member.email}")
    elif endpoint == "organization_add_user":
        # Only disclose an account resolved from the database. Unknown identity
        # input might contain sensitive text and is never echoed into the review.
        member = db.session.scalar(
            select(User).where(
                User.__table__.c.email == request.form.get("identity", "").strip()
            )
        )
        if member is not None:
            labels.append(
                f"Account: {member.username or member.email} ({member.email})"
            )
    return {
        "title": title,
        "target": " · ".join(labels) or target,
        "consequence": consequence,
        "confirm_label": "Confirm action",
    }


def _context(
    kind: str, action: str = "", target: str = "", request_id: str | None = None
) -> dict[str, object]:
    replay: dict[str, str] | None = None
    if (
        kind == "action"
        and request.headers.get("X-Admin-Fetch") != "1"
        and not request.files
    ):
        body = request.get_data(as_text=True)
        content_type = request.content_type or ""
        if request.form:
            body = urlencode(list(request.form.items(multi=True)))
            content_type = "application/x-www-form-urlencoded"
        replay = {
            "url": request.full_path.rstrip("?"),
            "method": request.method,
            "body": body,
            "content_type": content_type,
        }
    return {
        "kind": kind,
        "action": action,
        "target": target,
        "summary": _action_summary(action, target) if kind == "action" else None,
        "request_label": f"{request.method} {request.path}",
        "options_url": url_for("admin_passkey.options"),
        "verify_url": url_for("admin_passkey.verify"),
        "return_url": _local_url(request.args.get("next")),
        "request_id": request_id,
        "replay": replay,
    }


def _verification_response(context: dict[str, object]) -> Response:
    if request.is_json or request.headers.get("X-Admin-Fetch") == "1":
        response = jsonify(admin_verification=context)
        response.status_code = 428
    else:
        response = make_response(
            render_template("admin_console/verify.html", admin_verification=context),
            428,
        )
    response.headers["Cache-Control"] = "no-store"
    return response


def require_admin_action(
    action: str, target: str, payload: dict[str, object]
) -> Response | None:
    """Consume exactly one server-issued proof for this request, before any mutation."""
    _csrf()
    user = _admin()
    if not has_admin_session():
        return require_admin_session()
    if request.method in SAFE_METHODS:
        abort(405)
    digest = _digest(action, target, payload)
    if getattr(g, "admin_action_digest", None) == digest:
        return None
    token = request.headers.get("X-Admin-Action") or request.form.get("_admin_action")
    if token:
        # Commit consumption independently: application rollback cannot revive a proof.
        with db.engine.begin() as connection:
            consumed = connection.execute(
                update(AdminChallenge)
                .where(
                    AdminChallenge.id == token,
                    AdminChallenge.user_id == user.id,
                    AdminChallenge.uniquifier == user.fs_uniquifier,
                    AdminChallenge.session_binding == session.get("admin_binding"),
                    AdminChallenge.grant_id == _grant_id(),
                    AdminChallenge.request_digest == digest,
                    AdminChallenge.expires_at > _now(),
                    AdminChallenge.verified_at.is_not(None),
                    AdminChallenge.used_at.is_(None),
                )
                .values(used_at=_now())
                .returning(AdminChallenge.id)
            ).scalar_one_or_none()
        if consumed is None:
            abort(
                403,
                "The action proof expired, was used, or does not match this request",
            )
        g.admin_action_digest = digest
        return None
    challenge_id = secrets.token_urlsafe(32)
    with db.engine.begin() as connection:
        _ = connection.execute(
            insert(AdminChallenge).values(
                id=challenge_id,
                user_id=user.id,
                uniquifier=user.fs_uniquifier,
                session_binding=_binding(),
                grant_id=_grant_id(),
                challenge=secrets.token_bytes(32),
                request_digest=digest,
                origin=security.webauthn_util.origin(),
                rp_id=request.host.split(":")[0],
                expires_at=_now() + ACTION_LIFETIME,
            )
        )
    return _verification_response(_context("action", action, target, challenge_id))


def require_elevated_request() -> Response | None:
    """One exact HTTP-request proof shared by all native permission checks."""
    return require_admin_action(request.endpoint or request.path, request.path, {})


def revoke_admin_access(user_id: UUID) -> None:
    """Revoke in the caller's transaction, alongside account/security changes."""
    _ = db.session.execute(
        delete(AdminChallenge).where(AdminChallenge.user_id == user_id)
    )
    _ = db.session.execute(delete(AdminGrant).where(AdminGrant.user_id == user_id))


@blueprint.get("/")
@login_required
def entry() -> Response:
    _ = _admin()
    if has_admin_session():
        return redirect(_local_url(request.args.get("next")))
    response = make_response(
        render_template(
            "admin_console/verify.html", admin_verification=_context("entry")
        )
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@blueprint.get("/status")
def status() -> Response:
    response = jsonify(admin_access_state())
    response.headers["Cache-Control"] = "no-store"
    return response


@blueprint.post("/options")
@login_required
def options() -> Response:
    _csrf()
    user = _admin()
    data = request.get_json()
    if not isinstance(data, dict):
        abort(400)
    challenge_id = data.get("request_id")
    if challenge_id is not None and not isinstance(challenge_id, str):
        abort(400)
    credentials = db.session.scalars(
        select(WebAuthn).where(WebAuthn.user_id == user.id)
    ).all()
    if not credentials:
        response = jsonify(
            error="Register a user-verifying passkey in Account security first."
        )
        response.status_code = 403
        return response
    if challenge_id:
        if not has_admin_session():
            abort(403)
        challenge = db.session.get(AdminChallenge, challenge_id)
        if (
            challenge is None
            or challenge.user_id != user.id
            or challenge.uniquifier != user.fs_uniquifier
            or challenge.session_binding != _binding()
            or challenge.grant_id != _grant_id()
            or challenge.expires_at <= _now()
            or challenge.attempted_at is not None
        ):
            abort(403)
    else:
        challenge = AdminChallenge(
            id=secrets.token_urlsafe(32),
            user_id=user.id,
            uniquifier=user.fs_uniquifier,
            session_binding=_binding(),
            challenge=secrets.token_bytes(32),
            origin=security.webauthn_util.origin(),
            rp_id=request.host.split(":")[0],
            expires_at=_now() + ACTION_LIFETIME,
        )
        db.session.add(challenge)
        db.session.commit()
    public_key = generate_authentication_options(
        rp_id=challenge.rp_id,
        challenge=challenge.challenge,
        timeout=120000,
        allow_credentials=[
            PublicKeyCredentialDescriptor(
                id=cast(bytes, cast(object, cred.credential_id))
            )
            for cred in credentials
        ],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    response = jsonify(
        request_id=challenge.id, publicKey=json.loads(options_to_json(public_key))
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@blueprint.post("/verify")
@login_required
def verify() -> Response:
    _csrf()
    user = _admin()
    data = request.get_json()
    if not isinstance(data, dict):
        abort(400)
    challenge_id = data.get("request_id")
    if not isinstance(challenge_id, str):
        abort(400)
    with db.engine.begin() as connection:
        row = (
            connection.execute(
                update(AdminChallenge)
                .where(
                    AdminChallenge.id == challenge_id,
                    AdminChallenge.user_id == user.id,
                    AdminChallenge.uniquifier == user.fs_uniquifier,
                    AdminChallenge.session_binding == _binding(),
                    AdminChallenge.expires_at > _now(),
                    AdminChallenge.attempted_at.is_(None),
                )
                .values(attempted_at=_now())
                .returning(AdminChallenge.__table__)
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        abort(403, "The challenge expired or was already used")
    if row["grant_id"] is not None and (
        row["grant_id"] != _grant_id() or not has_admin_session()
    ):
        abort(403)
    if (
        row["origin"] != security.webauthn_util.origin()
        or row["rp_id"] != request.host.split(":")[0]
    ):
        abort(403)
    try:
        credential = parse_authentication_credential_json(
            cast(str | dict[str, object], data.get("credential", {}))
        )
        registered = db.session.scalar(
            select(WebAuthn)
            .where(
                WebAuthn.user_id == user.id,
                cast(ColumnElement[bytes], WebAuthn.credential_id) == credential.raw_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if registered is None:
            abort(403)
        if (
            credential.response.user_handle is not None
            and credential.response.user_handle
            != (user.fs_webauthn_user_handle or "").encode()
        ):
            abort(403)
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=row["challenge"],
            expected_origin=row["origin"],
            expected_rp_id=row["rp_id"],
            credential_public_key=cast(bytes, cast(object, registered.public_key)),
            credential_current_sign_count=cast(
                int, cast(object, registered.sign_count)
            ),
            require_user_verification=True,
        )
    except WebAuthnException, ValueError, TypeError:
        db.session.rollback()
        abort(403, "Passkey verification failed")
    # Flask-Security's legacy mixin annotates instance values as Column objects.
    setattr(registered, "sign_count", verified.new_sign_count)
    setattr(registered, "lastuse_datetime", _now())
    if row["grant_id"] is None:
        _ = db.session.execute(
            delete(AdminChallenge).where(AdminChallenge.expires_at <= _now())
        )
        _ = db.session.execute(
            delete(AdminGrant).where(AdminGrant.expires_at <= _now())
        )
        # Re-entering never refreshes an existing grant; it creates a new revocable one.
        old_grant = _grant_id()
        if old_grant:
            _ = db.session.execute(delete(AdminGrant).where(AdminGrant.id == old_grant))
        grant = AdminGrant(
            id=secrets.token_urlsafe(32),
            user_id=user.id,
            credential_id=registered.id,
            uniquifier=user.fs_uniquifier,
            session_binding=_binding(),
            expires_at=_now() + ENTRY_LIFETIME,
        )
        db.session.add(grant)
        _ = db.session.execute(
            delete(AdminChallenge).where(AdminChallenge.id == challenge_id)
        )
        db.session.commit()
        session["admin_grant"] = grant.id
        return jsonify(verified=True, admin_access=admin_access_state())
    _ = db.session.execute(
        update(AdminChallenge)
        .where(AdminChallenge.id == challenge_id)
        .values(
            verified_at=_now(),
            credential_id=registered.id,
        )
    )
    db.session.commit()
    return jsonify(
        verified=True,
        action_token=challenge_id,
        expires_at=_epoch(row["expires_at"]),
        server_time=_epoch(_now()),
    )


@blueprint.post("/end")
@login_required
def end() -> Response:
    _csrf()
    _revoke_session_grants(current_app, cast(User, current_user))
    if request.is_json or request.headers.get("X-Admin-Fetch") == "1":
        return jsonify(admin_access=admin_access_state())
    return redirect(url_for("admin_passkey.entry"))


def _revoke_session_grants(sender: Flask, user: User, **extra: object) -> None:
    old_grant = _grant_id()
    if old_grant:
        _ = db.session.execute(delete(AdminGrant).where(AdminGrant.id == old_grant))
    binding = cast(str | None, session.get("admin_binding"))
    if binding:
        _ = db.session.execute(
            delete(AdminChallenge).where(AdminChallenge.session_binding == binding)
        )
    db.session.commit()
    session.pop("admin_grant", None)
    session.pop("admin_binding", None)
    session.pop("admin_shell_ticket", None)


def _revoke_credential_grants(sender: Flask, user: User, **extra: object) -> None:
    # The native deletion view commits this with the credential removal. Do not
    # change its last-MFA policy or require elevation to manage one's own account.
    revoke_admin_access(user.id)
    session.pop("admin_grant", None)
    session.pop("admin_binding", None)
    session.pop("admin_shell_ticket", None)


def register_admin_guards(app: Flask) -> None:
    _ = user_logged_out.connect(_revoke_session_grants, sender=app, weak=False)
    _ = user_logged_in.connect(_revoke_session_grants, sender=app, weak=False)
    _ = wan_deleted.connect(_revoke_credential_grants, sender=app, weak=False)
    cast(dict[str, object], app.jinja_env.globals)["admin_access_state"] = (
        admin_access_state
    )

    @app.after_request
    def private_admin_response(response: FlaskResponse) -> FlaskResponse:
        converted = (
            request.headers.get("X-Admin-Navigate") == "1"
            and response.status_code in {301, 302, 303, 307, 308}
            and response.headers.get("Location")
        )
        if converted:
            # Any form the band intercepts, approved or membership-authorized:
            # fetch cannot follow external redirects (GitHub), and following
            # same-origin ones would load the page twice and consume flashes.
            response = jsonify(admin_redirect=response.headers["Location"])
        if (
            converted
            or request.blueprint in {"admin_passkey", "admin_console"}
            or getattr(g, "admin_elevated", False)
            or getattr(g, "admin_action_digest", None)
        ):
            response.headers["Cache-Control"] = "no-store"
        return response


@dataclass(frozen=True)
class _StreamGrantCheck:
    identity: tuple[UUID, str, str | None, str | None, str | None]
    checked_at: float
    expires_at: datetime | None


def elevated_stream_valid() -> bool:
    """Check revocation at most one second apart, and absolute expiry on every tick."""
    if not getattr(g, "admin_elevated", False):
        return True
    user = cast(User, current_user)
    if not user.is_authenticated:
        return False
    grant_id = _grant_id()
    binding = cast(str | None, session.get("admin_binding"))
    identity = (
        user.id,
        cast(str, cast(object, user.fs_uniquifier)),
        cast(str | None, session.get("_user_id")),
        grant_id,
        binding,
    )
    # Keep this on the request itself, never an application or browser-session cache.
    cached = cast(
        _StreamGrantCheck | None, request.environ.get("cabotage.admin_stream_grant")
    )
    checked_at = monotonic()
    if (
        cached is None
        or cached.identity != identity
        or checked_at - cached.checked_at >= 1.0
    ):
        cached = _StreamGrantCheck(
            identity, checked_at, _grant_expiration(grant_id, binding, user.id)
        )
        request.environ["cabotage.admin_stream_grant"] = cached
    return cached.expires_at is not None and cached.expires_at > _now()


def confirm_elevated_get() -> Response:
    """A side-effecting legacy GET becomes a confirmed POST for elevated callers."""
    context = _context("action", request.endpoint or request.path, request.path)
    context["replay"] = {
        "url": request.full_path.rstrip("?"),
        "method": "POST",
        "body": urlencode({"csrf_token": generate_csrf()}),
        "content_type": "application/x-www-form-urlencoded",
    }
    return _verification_response(context)


def issue_shell_ticket(socket_url: str) -> None:
    """Issue one socket capability only after the shell page's confirmed POST."""
    if not getattr(g, "admin_elevated", False):
        return
    if request.method != "POST" or not getattr(g, "admin_action_digest", None):
        abort(403)
    user = cast(User, current_user)
    token = secrets.token_urlsafe(32)
    with db.engine.begin() as connection:
        _ = connection.execute(
            insert(AdminChallenge).values(
                id=token,
                user_id=user.id,
                uniquifier=user.fs_uniquifier,
                session_binding=_binding(),
                grant_id=_grant_id(),
                challenge=b"",
                request_digest=hashlib.sha256(
                    ("shell:" + socket_url).encode()
                ).hexdigest(),
                origin=security.webauthn_util.origin(),
                rp_id=request.host.split(":")[0],
                expires_at=_now() + ACTION_LIFETIME,
                attempted_at=_now(),
                verified_at=_now(),
            )
        )
    session["admin_shell_ticket"] = token


def consume_shell_ticket() -> bool:
    token = cast(str | None, session.get("admin_shell_ticket"))
    if (
        not token
        or not has_admin_session()
        or request.headers.get("Origin") != security.webauthn_util.origin()
    ):
        return False
    user = cast(User, current_user)
    with db.engine.begin() as connection:
        consumed = connection.execute(
            update(AdminChallenge)
            .where(
                AdminChallenge.id == token,
                AdminChallenge.user_id == user.id,
                AdminChallenge.grant_id == _grant_id(),
                AdminChallenge.session_binding == session.get("admin_binding"),
                AdminChallenge.request_digest
                == hashlib.sha256(("shell:" + request.path).encode()).hexdigest(),
                AdminChallenge.expires_at > _now(),
                AdminChallenge.used_at.is_(None),
                AdminChallenge.verified_at.is_not(None),
            )
            .values(used_at=_now())
            .returning(AdminChallenge.id)
        ).scalar_one_or_none()
    return consumed is not None
