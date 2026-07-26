import datetime
import uuid

from authlib.integrations.flask_client import OAuth
from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_security import current_user, login_user
from flask_security.tf_plugin import tf_verify_validity_token
from flask_wtf import FlaskForm
from itsdangerous import BadData
import requests

from cabotage.server import db
from cabotage.server.acl import (
    AdministerApplicationPermission,
    AdministerOrganizationPermission,
)
from cabotage.server.mfa import get_mfa_status
from sqlalchemy.exc import DataError

from cabotage.server.models.auth import GitHubIdentity, Organization, User
from cabotage.server.models.projects import Application, activity_plugin
from cabotage.server.user import github_installations

github_oauth_bp = Blueprint("github_oauth", __name__, url_prefix="/auth/github")
oauth = OAuth()
Activity = activity_plugin.activity_cls


@github_oauth_bp.route("/login")
def login():
    scheme = current_app.config["EXT_PREFERRED_URL_SCHEME"]
    server = current_app.config["EXT_SERVER_NAME"]
    path = url_for("github_oauth.callback")
    redirect_uri = f"{scheme}://{server}{path}"
    session["github_oauth_next"] = request.args.get("next", "/")
    return oauth.github.authorize_redirect(redirect_uri)


@github_oauth_bp.route("/callback")
def callback():
    state = request.args.get("state")
    if state and _is_github_installation_connect_state(state):
        return _connect_installation_callback(state)

    token = oauth.github.authorize_access_token()
    if token is None:
        flash("GitHub authentication failed.", "error")
        return redirect(url_for("security.login"))

    resp = oauth.github.get("user", token=token)
    github_user = resp.json()

    primary_email = None
    verified_emails = []
    emails_resp = oauth.github.get("user/emails", token=token)
    emails_data = emails_resp.json()
    if isinstance(emails_data, list):
        verified_emails = [e["email"] for e in emails_data if e.get("verified")]
        primary_email = next(
            (e["email"] for e in emails_data if e.get("primary") and e.get("verified")),
            None,
        )
    if not primary_email:
        primary_email = github_user.get("email")
    if not primary_email:
        flash("No verified email found on your GitHub account.", "error")
        return redirect(url_for("security.login"))
    if primary_email not in verified_emails:
        verified_emails.append(primary_email)

    allowed_orgs = current_app.config.get("GITHUB_OAUTH_ALLOWED_ORGS")
    if allowed_orgs:
        org_list = [o.strip().lower() for o in allowed_orgs.split(",") if o.strip()]
        if org_list:
            is_member = False
            for org in org_list:
                resp = oauth.github.get(f"user/memberships/orgs/{org}", token=token)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("state") == "active":
                        is_member = True
                        break
            if not is_member:
                flash(
                    "Your GitHub account is not a member of an allowed organization.",
                    "error",
                )
                return redirect(url_for("security.login"))

    github_id = github_user["id"]
    github_username = github_user["login"]

    identity = GitHubIdentity.query.filter_by(github_id=github_id).first()

    user = None
    if identity:
        identity.github_username = github_username
        db.session.commit()
        user = identity.user
    else:
        existing_user = User.query.filter(
            db.func.lower(User.email).in_([e.lower() for e in verified_emails])
        ).first()

        if existing_user:
            gh_identity = GitHubIdentity(
                user_id=existing_user.id,
                github_id=github_id,
                github_username=github_username,
            )
            db.session.add(gh_identity)
            db.session.commit()
            user = existing_user
        else:
            registerable = current_app.config.get("SECURITY_REGISTERABLE", True)
            github_oauth_only = current_app.config.get("GITHUB_OAUTH_ONLY", False)
            if not registerable and not github_oauth_only:
                flash("Account registration is currently closed.", "error")
                return redirect(url_for("security.login"))

            username = f"github:{github_id}:{github_username}"

            user = User(
                username=username,
                email=primary_email,
                password="!",  # nosec B106 - unusable password for OAuth-only users
                active=True,
                confirmed_at=datetime.datetime.now(datetime.timezone.utc),
                fs_uniquifier=uuid.uuid4().hex,
            )
            db.session.add(user)
            db.session.flush()

            gh_identity = GitHubIdentity(
                user_id=user.id,
                github_id=github_id,
                github_username=github_username,
            )
            db.session.add(gh_identity)
            db.session.commit()

    next_url = session.pop("github_oauth_next", "/")
    return _complete_oauth_login(user, next_url)


def _complete_oauth_login(user, next_url):
    """Handle MFA check and login for OAuth users.

    If the user has MFA configured and no valid trust cookie, sets up
    Flask-Security's 2FA session state and redirects to the challenge.
    Otherwise logs in directly.
    """
    has_totp, num_webauthn, has_mfa = get_mfa_status(user)

    if has_mfa:
        tf_fresh = tf_verify_validity_token(user.fs_uniquifier)
        if not tf_fresh or current_app.config.get(
            "SECURITY_TWO_FACTOR_ALWAYS_VALIDATE"
        ):
            session["tf_user_id"] = user.fs_uniquifier
            next_param = {"next": next_url} if next_url and next_url != "/" else {}
            if has_totp and num_webauthn > 0:
                session["tf_select"] = True
                return redirect(url_for("security.tf_select", **next_param))
            elif has_totp:
                session["tf_state"] = "ready"
                return redirect(
                    url_for("security.two_factor_token_validation", **next_param)
                )
            else:
                session["tf_state"] = "ready"
                return redirect(url_for("security.wan_signin", **next_param))

    login_user(user)
    db.session.commit()
    return redirect(next_url)


def _is_github_installation_connect_state(state):
    return github_installations.is_connect_state(state)


def _connect_installation_callback(state):
    if not current_user.is_authenticated:
        flash("Please sign in before connecting GitHub installations.", "error")
        return redirect(url_for("security.login"))

    try:
        payload = github_installations.connect_state_serializer().loads(
            state, max_age=github_installations.GITHUB_INSTALL_STATE_MAX_AGE_SECONDS
        )
    except BadData:
        flash("The GitHub connect link expired or could not be verified.", "danger")
        return redirect(url_for("user.organizations"))

    if payload.get("user_id") != str(current_user.id):
        flash("The GitHub connect link belongs to a different user.", "danger")
        return redirect(url_for("user.organizations"))

    organization = Organization.query.filter_by(
        id=payload.get("organization_id")
    ).first_or_404()
    if not AdministerOrganizationPermission(organization.id).can():
        return redirect(url_for("user.organizations"))

    access_token = _fetch_github_user_access_token(request.args.get("code"))
    if access_token is None:
        flash("Cabotage could not authorize your GitHub account.", "danger")
        return redirect(
            url_for("user.organization_settings", org_slug=organization.slug)
        )

    installations = _fetch_github_user_installations(access_token)
    if installations is None:
        flash("Cabotage could not load your GitHub App installations.", "danger")
        return redirect(
            url_for("user.organization_settings", org_slug=organization.slug)
        )

    requested_installation_id = payload.get("installation_id")
    if requested_installation_id:
        try:
            requested_installation_id = int(requested_installation_id)
        except (TypeError, ValueError):
            flash("The GitHub connect link expired or could not be verified.", "danger")
            return redirect(
                url_for("user.organization_settings", org_slug=organization.slug)
            )

        requested_installation = next(
            (
                installation
                for installation in installations
                if installation.get("id") == requested_installation_id
            ),
            None,
        )
        if requested_installation is None:
            flash(
                "Your GitHub account is not authorized to connect that installation.",
                "danger",
            )
            return redirect(
                url_for("user.organization_settings", org_slug=organization.slug)
            )

        accessible_repository_ids = _fetch_github_user_installation_repository_ids(
            access_token, requested_installation_id
        )
        if accessible_repository_ids is None:
            flash(
                "Cabotage could not verify your repository access for that installation.",
                "danger",
            )
            return redirect(
                url_for("user.organization_settings", org_slug=organization.slug)
            )

        return _complete_verified_installation_connection(
            organization,
            payload,
            accessible_repository_ids=accessible_repository_ids,
        )

    existing_installation_ids = {
        installation.installation_id
        for installation in organization.github_app_installations
    }
    installation_options = []
    for installation in installations:
        installation_id = installation.get("id")
        account = installation.get("account") or {}
        if not installation_id or int(installation_id) in existing_installation_ids:
            continue
        installation_options.append(
            {
                "account_login": account.get("login") or "Unknown",
                "repository_selection": installation.get("repository_selection"),
                "token": github_installations.connect_option(
                    organization,
                    current_user.id,
                    installation,
                ),
            }
        )

    return render_template(
        "user/github_installation_connect.html",
        organization=organization,
        installations=installation_options,
        csrf_form=FlaskForm(),
    )


def _safe_get(model, pk):
    try:
        return model.query.get(pk)
    except DataError:
        db.session.rollback()
        return None


def _complete_verified_installation_connection(
    organization, payload, *, accessible_repository_ids
):
    app_installation, repositories_synced = github_installations.upsert_installation(
        organization,
        payload.get("installation_id"),
        installed_by_user_id=current_user.id,
        accessible_repository_ids=accessible_repository_ids,
    )
    if app_installation is None:
        flash("Cabotage could not verify the GitHub App installation.", "danger")
        return redirect(
            url_for("user.organization_settings", org_slug=organization.slug)
        )

    application = None
    application_repository_disconnected = False
    application_id = payload.get("application_id")
    if application_id:
        application = _safe_get(Application, application_id)
        if (
            application is None
            or application.deleted_at is not None
            or application.project.organization_id != organization.id
            or not AdministerApplicationPermission(application.id).can()
        ):
            flash("The GitHub install link does not match this application.", "danger")
            return redirect(
                url_for("user.organization_settings", org_slug=organization.slug)
            )
        application.github_app_installation_id = app_installation.installation_id
        selected_repository = github_installations.repository_by_name(
            app_installation, application.github_repository
        )
        if (
            repositories_synced
            and application.github_repository
            and app_installation.repository_selection == "selected"
            and app_installation.repositories is not None
            and selected_repository is None
        ):
            application.github_app_installation_id = None
            application.github_repository_id = None
            application.github_repository_is_private = False
            application_repository_disconnected = True
        elif selected_repository is not None:
            application.github_repository_id = github_installations.repository_id(
                selected_repository
            )
            application.github_repository_is_private = bool(
                selected_repository.get("private")
            )

    db.session.flush()
    db.session.add(
        Activity(
            verb="edit",
            object=organization,
            data={
                "user_id": str(current_user.id),
                "action": "github_app_install",
                "installation_id": app_installation.installation_id,
                "account_login": app_installation.account_login,
                "application_id": str(application.id)
                if application is not None
                else None,
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            },
        )
    )
    db.session.commit()

    if application is not None:
        if application_repository_disconnected:
            flash(
                "GitHub App installed, but this application's repository is not "
                "available to that installation. Choose an accessible repository.",
                "warning",
            )
        else:
            flash("GitHub App installed and connected to this application.", "success")
        return redirect(
            url_for(
                "user.project_application_settings",
                org_slug=organization.slug,
                project_slug=application.project.slug,
                app_slug=application.slug,
                env_slug=payload.get("env_slug"),
            )
        )

    flash("GitHub App installation connected.", "success")
    return redirect(url_for("user.organization_settings", org_slug=organization.slug))


def _fetch_github_user_access_token(code):
    if not code:
        return None
    scheme = current_app.config["EXT_PREFERRED_URL_SCHEME"]
    server = current_app.config["EXT_SERVER_NAME"]
    redirect_uri = f"{scheme}://{server}{url_for('github_oauth.callback')}"
    try:
        resp = requests.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": current_app.config["GITHUB_APP_CLIENT_ID"],
                "client_secret": current_app.config["GITHUB_APP_CLIENT_SECRET"],
                "code": code,
                "redirect_uri": redirect_uri,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("access_token")
    except (requests.RequestException, ValueError, KeyError):
        current_app.logger.exception("Unable to fetch GitHub user access token")
        return None


def _fetch_github_user_installations(access_token):
    try:
        installations = []
        url = "https://api.github.com/user/installations"
        params = {"per_page": 100}
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {access_token}",
        }
        while url:
            resp = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
            installations.extend(resp.json().get("installations") or [])
            url = resp.links.get("next", {}).get("url")
            params = None
        return installations
    except (requests.RequestException, ValueError, AttributeError):
        current_app.logger.exception("Unable to fetch GitHub user installations")
        return None


def _fetch_github_user_installation_repository_ids(access_token, installation_id):
    try:
        repository_ids = []
        url = (
            f"https://api.github.com/user/installations/{installation_id}/repositories"
        )
        params = {"per_page": 100}
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {access_token}",
        }
        while url:
            resp = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
            repository_ids.extend(
                repo["id"]
                for repo in resp.json().get("repositories") or []
                if repo.get("id") is not None
            )
            url = resp.links.get("next", {}).get("url")
            params = None
        return repository_ids
    except (requests.RequestException, ValueError, AttributeError, KeyError):
        current_app.logger.exception(
            "Unable to fetch GitHub user repositories for installation %s",
            installation_id,
        )
        return None


def init_github_oauth(app):
    if not app.config.get("GITHUB_APP_CLIENT_ID"):
        return

    oauth.init_app(app)
    oauth.register(  # nosec B106 - access_token_url is a URL, not a password
        name="github",
        client_id=app.config["GITHUB_APP_CLIENT_ID"],
        client_secret=app.config["GITHUB_APP_CLIENT_SECRET"],
        access_token_url="https://github.com/login/oauth/access_token",
        authorize_url="https://github.com/login/oauth/authorize",
        api_base_url="https://api.github.com/",
        client_kwargs={"scope": "user:email read:org"},
    )
    app.register_blueprint(github_oauth_bp)

    @app.context_processor
    def github_oauth_context():
        return {
            "github_oauth_enabled": True,
            "github_oauth_only": bool(app.config.get("GITHUB_OAUTH_ONLY")),
        }
