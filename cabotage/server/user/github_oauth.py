import datetime
import uuid

from authlib.integrations.flask_client import OAuth
from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    request,
    session,
    url_for,
)
from flask_security import login_user

from cabotage.server import db
from cabotage.server.models.auth import GitHubIdentity, User

github_oauth_bp = Blueprint("github_oauth", __name__, url_prefix="/auth/github")
oauth = OAuth()


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
    token = oauth.github.authorize_access_token()
    if token is None:
        flash("GitHub authentication failed.", "error")
        return redirect(url_for("security.login"))

    print(
        "GitHub OAuth token scopes: %s",
        token.get("scope", "(none)"),
    )

    resp = oauth.github.get("user", token=token)
    github_user = resp.json()
    print(
        "GitHub user: id=%s login=%s email=%s",
        github_user.get("id"), github_user.get("login"), github_user.get("email"),
    )

    primary_email = None
    verified_emails = []
    emails_resp = oauth.github.get("user/emails", token=token)
    emails_data = emails_resp.json()
    print(
        "GitHub /user/emails: status=%s body=%s",
        emails_resp.status_code, emails_resp.text,
    )
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
                print(
                    "GitHub org membership check for %s: status=%s body=%s",
                    org, resp.status_code, resp.text,
                )
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

    if identity:
        identity.github_username = github_username
        db.session.commit()
        login_user(identity.user)
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
            login_user(existing_user)
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
                confirmed_at=datetime.datetime.utcnow(),
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
            login_user(user)

    next_url = session.pop("github_oauth_next", "/")
    return redirect(next_url)


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
