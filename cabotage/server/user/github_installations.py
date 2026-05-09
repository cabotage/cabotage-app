import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from flask import current_app, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer
import requests
from sqlalchemy import or_

from cabotage.server import db, github_app
from cabotage.server.models.auth import GitHubAppInstallation
from cabotage.server.models.projects import Application, Project

GITHUB_INSTALL_STATE_MAX_AGE_SECONDS = 60 * 60


def install_state_serializer():
    return URLSafeTimedSerializer(
        current_app.config["SECRET_KEY"], salt="github-app-install"
    )


def connect_state_serializer():
    return URLSafeTimedSerializer(
        current_app.config["SECRET_KEY"], salt="github-app-connect"
    )


def install_state(organization, user_id, application=None):
    payload = {
        "organization_id": str(organization.id),
        "user_id": str(user_id),
    }
    if application is not None:
        payload["application_id"] = str(application.id)
    return install_state_serializer().dumps(payload)


def connect_state(organization, user_id, *, installation_id=None, application=None):
    payload = {
        "organization_id": str(organization.id),
        "user_id": str(user_id),
    }
    if installation_id is not None:
        payload["installation_id"] = str(installation_id)
    if application is not None:
        payload["application_id"] = str(application.id)
    return connect_state_serializer().dumps(payload)


def connect_option(
    organization,
    user_id,
    installation,
    *,
    application_id=None,
):
    account = installation.get("account") or {}
    payload = {
        "organization_id": str(organization.id),
        "user_id": str(user_id),
        "installation_id": installation.get("id"),
        "account_login": account.get("login"),
        "account_type": account.get("type"),
        "repository_selection": installation.get("repository_selection"),
    }
    if application_id is not None:
        payload["application_id"] = application_id
    return connect_state_serializer().dumps(payload)


def is_connect_state(state):
    try:
        payload = connect_state_serializer().loads(state)
    except BadSignature:
        return False
    if not isinstance(payload, dict):
        return False
    return "organization_id" in payload and "user_id" in payload


def user_authorize_url(state):
    client_id = current_app.config.get("GITHUB_APP_CLIENT_ID")
    if not client_id:
        return None
    scheme = current_app.config["EXT_PREFERRED_URL_SCHEME"]
    server = current_app.config["EXT_SERVER_NAME"]
    redirect_uri = f"{scheme}://{server}{url_for('github_oauth.callback')}"
    return "https://github.com/login/oauth/authorize?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )


def install_url(state):
    configured = current_app.config.get("GITHUB_APP_INSTALL_URL")
    if not configured:
        configured = current_app.config.get("GITHUB_APP_URL")
    if not configured:
        try:
            configured = github_app.install_url
        except (RuntimeError, requests.RequestException, KeyError, ValueError):
            return None

    parts = urlsplit(configured)
    path = parts.path.rstrip("/")
    if "/installations/" not in path:
        path = f"{path}/installations/new"
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["state"] = state
    return urlunsplit(
        (parts.scheme, parts.netloc, path, urlencode(query), parts.fragment)
    )


def installation_choices(organization, selected_id=None):
    installations = sorted(
        organization.github_app_installations,
        key=lambda install: (install.account_login or "", install.installation_id),
    )
    choices = [("", "None")]
    seen = set()
    for installation in installations:
        value = str(installation.installation_id)
        seen.add(value)
        choices.append((value, installation.display_name))
    if selected_id and str(selected_id) not in seen:
        choices.append((str(selected_id), "Unknown installation"))
    return choices


def installation_for_org(organization, installation_id):
    if not installation_id:
        return None
    try:
        installation_id = int(installation_id)
    except (TypeError, ValueError):
        return None
    for installation in organization.github_app_installations:
        if installation.installation_id == installation_id:
            return installation
    return None


def repository_by_name(app_installation, repository_name):
    if not app_installation or not repository_name:
        return None
    for repository in app_installation.repositories or []:
        if repository.get("full_name") == repository_name:
            return repository
    return None


def repository_id(repository):
    if not repository:
        return None
    repo_id = repository.get("id")
    if repo_id is None:
        return None
    try:
        return int(repo_id)
    except (TypeError, ValueError):
        return None


def repository_by_id(app_installation, repo_id):
    if not app_installation or repo_id is None:
        return None
    try:
        repo_id = int(repo_id)
    except (TypeError, ValueError):
        return None
    for repository in app_installation.repositories or []:
        if repository_id(repository) == repo_id:
            return repository
    return None


def repository_options_by_installation(organization):
    options = {}
    for installation in organization.github_app_installations:
        if installation.repositories is None:
            continue
        options[str(installation.installation_id)] = [
            {
                "id": repository.get("id"),
                "name": repository.get("full_name"),
                "private": bool(repository.get("private")),
            }
            for repository in sorted(
                installation.repositories or [],
                key=lambda repo: repo.get("full_name") or "",
            )
            if repository.get("full_name")
        ]
    return options


def repository_metadata(repo):
    full_name = repo.get("full_name")
    if not full_name:
        return None
    return {
        "id": repo.get("id"),
        "full_name": full_name,
        "private": bool(repo.get("private")),
    }


def repository_metadata_key(repo):
    return repo.get("id") or repo.get("full_name")


def merge_repository_metadata(existing_repos, added_repos):
    merged = {}
    for repo in existing_repos:
        metadata = repository_metadata(repo)
        if metadata is not None:
            merged[repository_metadata_key(metadata)] = metadata
    for repo in added_repos:
        metadata = repository_metadata(repo)
        if metadata is not None:
            merged[repository_metadata_key(metadata)] = metadata
    return sorted(merged.values(), key=lambda repo: repo["full_name"])


def sync_installation_repositories(
    app_installation, *, clear_all_cache_on_failure=True
):
    repositories = github_app.fetch_installation_repositories(
        app_installation.installation_id
    )
    if repositories is None:
        if (
            clear_all_cache_on_failure
            and app_installation.repository_selection == "all"
        ):
            app_installation.repositories = None
            app_installation.repositories_synced_at = None
        return False

    app_installation.repositories = merge_repository_metadata([], repositories)
    app_installation.repositories_synced_at = datetime.datetime.now(
        datetime.timezone.utc
    ).replace(tzinfo=None)
    sync_application_repository_metadata(app_installation)
    return True


def sync_application_repository_metadata(app_installation):
    if app_installation.repositories is None:
        return 0

    repositories_by_id = {
        repo_id: repository
        for repository in app_installation.repositories or []
        if (repo_id := repository_id(repository)) is not None
    }
    repositories_by_name = {
        repository.get("full_name"): repository
        for repository in app_installation.repositories or []
        if repository.get("full_name")
    }
    if not repositories_by_id and not repositories_by_name:
        return 0

    updated = 0
    project_ids = Project.query.with_entities(Project.id).filter_by(
        organization_id=app_installation.organization_id
    )
    applications = (
        Application.query.filter(Application.project_id.in_(project_ids))
        .filter(
            Application.github_app_installation_id == app_installation.installation_id
        )
        .filter(Application.github_repository.isnot(None))
        .all()
    )
    for application in applications:
        repository = repository_by_id(
            app_installation, application.github_repository_id
        ) or repositories_by_name.get(application.github_repository)
        if repository is None:
            continue

        repo_id = repository_id(repository)
        full_name = repository.get("full_name")
        private = bool(repository.get("private"))
        if (
            application.github_repository_id != repo_id
            or application.github_repository != full_name
            or application.github_repository_is_private != private
        ):
            application.github_repository_id = repo_id
            application.github_repository = full_name
            application.github_repository_is_private = private
            updated += 1
    return updated


def user_can_access_installation_repositories(repositories, accessible_repository_ids):
    if accessible_repository_ids is None:
        return True

    accessible_repository_ids = {
        int(repo_id) for repo_id in accessible_repository_ids if repo_id is not None
    }
    repository_ids = {
        int(repo["id"]) for repo in repositories if repo.get("id") is not None
    }
    return repository_ids.issubset(accessible_repository_ids)


def reconcile_selected_repository_applications(app_installation):
    if (
        app_installation.repository_selection != "selected"
        or app_installation.repositories is None
    ):
        return 0

    repository_ids = {
        repository_id(repository)
        for repository in app_installation.repositories
        if repository_id(repository) is not None
    }
    repository_names = {
        repository.get("full_name")
        for repository in app_installation.repositories
        if repository.get("full_name")
    }
    project_ids = Project.query.with_entities(Project.id).filter_by(
        organization_id=app_installation.organization_id
    )
    query = (
        Application.query.filter(Application.project_id.in_(project_ids))
        .filter(
            Application.github_app_installation_id == app_installation.installation_id
        )
        .filter(Application.github_repository.isnot(None))
    )
    if repository_ids:
        query = query.filter(
            or_(
                Application.github_repository_id.is_(None),
                ~Application.github_repository_id.in_(repository_ids),
            )
        )
    if repository_names:
        query = query.filter(
            or_(
                Application.github_repository_id.isnot(None),
                ~Application.github_repository.in_(repository_names),
            )
        )

    return query.update(
        {
            "github_app_installation_id": None,
            "github_repository_id": None,
            "github_repository_is_private": False,
        },
        synchronize_session=False,
    )


def upsert_installation(
    organization,
    installation_id,
    *,
    installed_by_user_id=None,
    accessible_repository_ids=None,
):
    installation = github_app.fetch_installation(installation_id)
    if installation is None:
        return None, False

    repositories = github_app.fetch_installation_repositories(installation_id)
    if repositories is None:
        return None, False
    if not user_can_access_installation_repositories(
        repositories, accessible_repository_ids
    ):
        return None, False

    account = installation.get("account") or {}
    app_installation = GitHubAppInstallation.query.filter_by(
        organization_id=organization.id,
        installation_id=int(installation_id),
    ).first()
    if app_installation is None:
        app_installation = GitHubAppInstallation(installation_id=int(installation_id))
        db.session.add(app_installation)
    app_installation.organization_id = organization.id
    app_installation.account_id = account.get("id")
    app_installation.account_login = account.get("login")
    app_installation.account_type = account.get("type")
    app_installation.repository_selection = installation.get("repository_selection")
    if installed_by_user_id is not None:
        app_installation.installed_by_user_id = installed_by_user_id
    app_installation.repositories = merge_repository_metadata([], repositories)
    app_installation.repositories_synced_at = datetime.datetime.now(
        datetime.timezone.utc
    ).replace(tzinfo=None)
    sync_application_repository_metadata(app_installation)
    reconcile_selected_repository_applications(app_installation)
    return app_installation, True
