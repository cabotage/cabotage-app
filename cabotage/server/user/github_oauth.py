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
    redirect_uri = url_for(
        "github_oauth.callback",
        _external=True,
        _scheme=current_app.config.get("EXT_PREFERRED_URL_SCHEME", "https"),
    )
    session["github_oauth_next"] = request.args.get("next", "/")
    return oauth.github.authorize_redirect(redirect_uri)


@github_oauth_bp.route("/callback")
def callback():
    token = oauth.github.authorize_access_token()
    if token is None:
        flash("GitHub authentication failed.", "error")
        return redirect(url_for("security.login"))

    resp = oauth.github.get("user", token=token)
    github_user = resp.json()

    emails_resp = oauth.github.get("user/emails", token=token)
    primary_email = next(
        (e["email"] for e in emails_resp.json() if e["primary"] and e["verified"]),
        None,
    )
    if not primary_email:
        flash("No verified email found on your GitHub account.", "error")
        return redirect(url_for("security.login"))

    allowed_orgs = current_app.config.get("GITHUB_OAUTH_ALLOWED_ORGS")
    if allowed_orgs:
        org_list = [o.strip().lower() for o in allowed_orgs.split(",") if o.strip()]
        if org_list:
            orgs_resp = oauth.github.get("user/orgs", token=token)
            user_orgs = {o["login"].lower() for o in orgs_resp.json()}
            if not user_orgs.intersection(org_list):
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
        identity.github_access_token = token["access_token"]
        db.session.commit()
        login_user(identity.user)
    else:
        existing_user = User.query.filter(
            db.func.lower(User.email) == primary_email.lower()
        ).first()

        if existing_user:
            gh_identity = GitHubIdentity(
                user_id=existing_user.id,
                github_id=github_id,
                github_username=github_username,
                github_access_token=token["access_token"],
            )
            db.session.add(gh_identity)
            db.session.commit()
            login_user(existing_user)
        else:
            base_username = github_username
            username = base_username
            suffix = 1
            while User.query.filter(
                db.func.lower(User.username) == username.lower()
            ).first():
                username = f"{base_username}-{suffix}"
                suffix += 1

            user = User(
                username=username,
                email=primary_email,
                password="!",
                active=True,
                confirmed_at=datetime.datetime.now(),
                fs_uniquifier=uuid.uuid4().hex,
            )
            db.session.add(user)
            db.session.flush()

            gh_identity = GitHubIdentity(
                user_id=user.id,
                github_id=github_id,
                github_username=github_username,
                github_access_token=token["access_token"],
            )
            db.session.add(gh_identity)
            db.session.commit()
            login_user(user)

    next_url = session.pop("github_oauth_next", "/")
    return redirect(next_url)


def init_github_oauth(app):
    if not app.config.get("GITHUB_OAUTH_CLIENT_ID"):
        return

    oauth.init_app(app)
    oauth.register(
        name="github",
        client_id=app.config["GITHUB_OAUTH_CLIENT_ID"],
        client_secret=app.config["GITHUB_OAUTH_CLIENT_SECRET"],
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
