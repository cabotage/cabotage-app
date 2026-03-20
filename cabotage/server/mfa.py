"""MFA enforcement guards and utilities.

Registers before_request hooks that enforce:
- MFA setup for all authenticated users
- Recovery code generation after MFA setup
- Password endpoint blocking for GitHub OAuth users
- Trust-browser checkbox injection for 2FA flows
- Last-method deletion protection
"""

import time

from flask import abort, current_app, flash, redirect, request, session, url_for
from flask_login import current_user
from flask_mail import Message
from flask_security.utils import get_message
from flask_security.webauthn_util import WebauthnUtil


class CabotageWebauthnUtil(WebauthnUtil):
    """Override WebAuthn origin to respect EXT_PREFERRED_URL_SCHEME.

    When behind a reverse proxy that doesn't forward X-Forwarded-Proto,
    request.host_url may report http:// even though the client used https://.
    """

    def origin(self):
        origin = request.host_url.rstrip("/")
        ext_scheme = current_app.config.get("EXT_PREFERRED_URL_SCHEME")
        if ext_scheme and origin.startswith("http://") and ext_scheme == "https":
            origin = "https://" + origin[7:]
        return origin


def get_mfa_status(user):
    """Return (has_totp, num_webauthn, has_mfa) for a user."""
    from cabotage.server.models.auth import WebAuthn

    has_totp = user.tf_primary_method == "authenticator"
    num_webauthn = WebAuthn.query.filter_by(user_id=user.id).count()
    has_mfa = has_totp or num_webauthn > 0
    return has_totp, num_webauthn, has_mfa


def register_mfa_guards(app):
    """Register all MFA-related before_request hooks on the app."""

    @app.before_request
    def require_mfa_setup():
        """Force authenticated users to set up MFA and recovery codes."""
        if (
            not hasattr(current_user, "tf_primary_method")
            or not current_user.is_authenticated
        ):
            return None

        if not app.config.get("REQUIRE_MFA", True):
            return None

        endpoint = request.endpoint
        if endpoint in {"security.logout", "security.static", "static"}:
            return None

        has_totp, num_webauthn, has_mfa = get_mfa_status(current_user)

        # Step 1: No MFA — force setup
        if not has_mfa:
            mfa_setup_endpoints = {
                "user.account_security",
                "user.account_security_qr",
                "security.two_factor_setup",
                "security.two_factor_setup_validate",
                "security.two_factor_token_validation",
                "security.wan_register",
                "security.wan_register_response",
            }
            if endpoint in mfa_setup_endpoints:
                return None
            session["mfa_initial_setup"] = True
            flash("Please set up two-factor authentication to continue.", "warning")
            return redirect(url_for("security.two_factor_setup"))

        # Step 2: Has MFA but no recovery codes — force generation
        if not current_user.mf_recovery_codes:
            if endpoint in (
                "security.mf_recovery_codes",
                "user.account_security_verify_recovery_code",
            ):
                # Ensure freshness for the recovery codes page — user just
                # completed login so fs_paa should be set, but handle the edge case
                if "fs_paa" not in session:
                    session["fs_paa"] = time.time()
                return None
            flash("Please generate recovery codes before continuing.", "warning")
            return redirect(url_for("security.mf_recovery_codes"))

        return None

    @app.before_request
    def block_password_for_github_users():
        """Block password endpoints for GitHub OAuth users and intercept
        password reset requests to send a reminder email instead."""
        # Authenticated GitHub users can't change password
        if current_user.is_authenticated:
            if (
                hasattr(current_user, "github_identity")
                and current_user.github_identity
            ):
                if request.endpoint == "security.change_password":
                    abort(403)

        # Intercept forgot_password POST for GitHub users
        if request.endpoint == "security.forgot_password" and request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            if email:
                from cabotage.server import db
                from cabotage.server.models.auth import User

                user = User.query.filter(db.func.lower(User.email) == email).first()
                if user and user.github_identity:
                    msg = Message(
                        "Password Reset — Sign in with GitHub",
                        sender=(
                            app.config.get("SECURITY_EMAIL_SENDER")
                            or app.config.get("MAIL_DEFAULT_SENDER")
                        ),
                        recipients=[user.email],
                    )
                    msg.body = (
                        f"Hi {user.username},\n\n"
                        f"We received a password reset request for your account, "
                        f"but your account uses GitHub for authentication.\n\n"
                        f"Please sign in using the 'Sign in with GitHub' button instead.\n\n"
                        f"If you did not request this, you can safely ignore this email.\n"
                    )
                    try:
                        from cabotage.server import mail

                        mail.send(msg)
                    except Exception:
                        current_app.logger.exception(
                            "Failed to send GitHub user password reset email"
                        )
                    # Same flash as normal reset to avoid user enumeration
                    flash(*get_message("PASSWORD_RESET_REQUEST", email=email))
                    return redirect(url_for("security.forgot_password"))

        return None

    @app.before_request
    def inject_tf_remember():
        """Set session['tf_remember_login'] from the 'Trust this browser'
        checkbox on 2FA verification forms."""
        if request.method != "POST":
            return None

        tf_endpoints = {
            "security.two_factor_token_validation",
            "security.wan_signin",
            "security.wan_signin_response",
            "security.mf_recovery",
        }
        if request.endpoint not in tf_endpoints:
            return None

        # wan_signin POST carries the checkbox; stash for wan_signin_response
        if request.form.get("remember"):
            session["tf_remember_login"] = True
        elif request.endpoint != "security.wan_signin_response":
            # Don't clear on wan_signin_response — it was set by wan_signin
            session.pop("tf_remember_login", None)

        return None

    @app.before_request
    def guard_last_mfa_method():
        """Prevent deletion of the last MFA method."""
        if (
            not hasattr(current_user, "tf_primary_method")
            or not current_user.is_authenticated
        ):
            return None

        if request.method != "POST":
            return None

        endpoint = request.endpoint
        if endpoint == "security.wan_delete":
            has_totp, num_webauthn, _ = get_mfa_status(current_user)
            total = (1 if has_totp else 0) + num_webauthn
            if total <= 1:
                abort(403)

        elif endpoint == "security.two_factor_setup":
            if request.form.get("setup") == "disable":
                _, num_webauthn, _ = get_mfa_status(current_user)
                total = 1 + num_webauthn  # 1 for the TOTP being disabled
                if total <= 1:
                    abort(403)

        return None
