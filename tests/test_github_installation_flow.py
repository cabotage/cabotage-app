import re
import time
import uuid
from unittest.mock import patch

import pytest
from flask_security import hash_password

from cabotage.server import db
from cabotage.server.models.auth import GitHubAppInstallation, Organization, User
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.models.projects import Application, Project
from cabotage.server.user import github_installations
from cabotage.server.wsgi import app as _app


@pytest.fixture
def app():
    config_keys = [
        "TESTING",
        "WTF_CSRF_ENABLED",
        "REQUIRE_MFA",
        "GITHUB_APP_CLIENT_ID",
        "GITHUB_APP_CLIENT_SECRET",
        "EXT_PREFERRED_URL_SCHEME",
        "EXT_SERVER_NAME",
    ]
    original_config = {key: _app.config.get(key) for key in config_keys}
    _app.config["TESTING"] = True
    _app.config["WTF_CSRF_ENABLED"] = False
    _app.config["REQUIRE_MFA"] = False
    _app.config["GITHUB_APP_CLIENT_ID"] = "github-client-id"
    _app.config["GITHUB_APP_CLIENT_SECRET"] = "github-client-secret"
    _app.config["EXT_PREFERRED_URL_SCHEME"] = "http"
    _app.config["EXT_SERVER_NAME"] = "localhost"

    from cabotage.server.user.github_oauth import github_oauth_bp

    _app._got_first_request = False
    if "github_oauth" not in _app.blueprints:
        _app.register_blueprint(github_oauth_bp)

    with _app.app_context():
        yield _app

    _app.config.update(original_config)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin_user(app):
    user = User(
        username=f"github-admin-{uuid.uuid4().hex[:8]}",
        email=f"github-admin-{uuid.uuid4().hex[:8]}@example.com",
        password=hash_password("password123"),
        active=True,
        fs_uniquifier=uuid.uuid4().hex,
    )
    db.session.add(user)
    db.session.commit()
    user_id = user.id
    yield user
    db.session.rollback()
    db.session.execute(
        db.text("UPDATE transaction SET user_id = NULL WHERE user_id = :uid"),
        {"uid": user_id},
    )
    User.query.filter_by(id=user_id).delete()
    db.session.commit()


@pytest.fixture
def org(admin_user):
    organization = Organization(
        name=f"GitHub Org {uuid.uuid4().hex[:8]}",
        slug=f"github-org-{uuid.uuid4().hex[:8]}",
    )
    db.session.add(organization)
    db.session.flush()
    organization_id = organization.id
    db.session.add(
        OrganizationMember(
            organization_id=organization_id,
            user_id=admin_user.id,
            admin=True,
        )
    )
    db.session.commit()
    yield organization
    db.session.rollback()
    OrganizationMember.query.filter_by(organization_id=organization_id).delete()
    GitHubAppInstallation.query.filter_by(organization_id=organization_id).delete()
    project_ids = [
        project.id
        for project in Project.query.filter_by(organization_id=organization_id)
    ]
    if project_ids:
        Application.query.filter(Application.project_id.in_(project_ids)).delete(
            synchronize_session=False
        )
        Project.query.filter(Project.id.in_(project_ids)).delete(
            synchronize_session=False
        )
    Organization.query.filter_by(id=organization_id).delete()
    db.session.commit()


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = user.fs_uniquifier
        sess["_fresh"] = True
        sess["fs_cc"] = "set"
        sess["fs_paa"] = time.time()
        sess["identity.id"] = user.id
        sess["identity.auth_type"] = "session"


def _installation(installation_id, account_login):
    return {
        "id": installation_id,
        "account": {
            "id": installation_id + 1000,
            "login": account_login,
            "type": "Organization",
        },
        "repository_selection": "selected",
    }


def test_install_callback_rejects_installation_not_accessible_to_github_user(
    app, client, admin_user, org
):
    _login(client, admin_user)
    state = github_installations.connect_state(
        org, admin_user.id, installation_id=987654
    )

    with (
        patch(
            "cabotage.server.user.github_oauth._fetch_github_user_access_token",
            return_value="user-token",
        ),
        patch(
            "cabotage.server.user.github_oauth._fetch_github_user_installations",
            return_value=[_installation(111111, "allowed-org")],
        ),
    ):
        response = client.get(
            "/auth/github/callback",
            query_string={"state": state, "code": "oauth-code"},
        )

    assert response.status_code == 302
    assert (
        GitHubAppInstallation.query.filter_by(
            organization_id=org.id,
            installation_id=987654,
        ).first()
        is None
    )


def test_verified_installation_connects_application(app, client, admin_user, org):
    _login(client, admin_user)
    project = Project(name=f"Project {uuid.uuid4().hex[:8]}", organization_id=org.id)
    db.session.add(project)
    db.session.flush()
    application = Application(
        name=f"App {uuid.uuid4().hex[:8]}",
        slug=f"app-{uuid.uuid4().hex[:8]}",
        project_id=project.id,
    )
    db.session.add(application)
    db.session.commit()

    installation_id = 3_000_000_000
    installation = _installation(installation_id, "connected-org")
    state = github_installations.connect_state(
        org,
        admin_user.id,
        installation_id=installation_id,
        application=application,
    )

    with (
        patch(
            "cabotage.server.user.github_oauth._fetch_github_user_access_token",
            return_value="user-token",
        ),
        patch(
            "cabotage.server.user.github_oauth._fetch_github_user_installations",
            return_value=[installation],
        ),
        patch(
            "cabotage.server.user.github_oauth._fetch_github_user_installation_repository_ids",
            return_value=[1],
        ),
        patch(
            "cabotage.server.user.github_installations.github_app.fetch_installation",
            return_value=installation,
        ),
        patch(
            "cabotage.server.user.github_installations.github_app.fetch_installation_repositories",
            return_value=[
                {"id": 1, "full_name": "connected-org/repo", "private": True}
            ],
        ),
    ):
        response = client.get(
            "/auth/github/callback",
            query_string={"state": state, "code": "oauth-code"},
            follow_redirects=True,
        )

    assert response.status_code == 200
    db.session.refresh(application)
    assert application.github_app_installation_id == installation_id
    assert (
        GitHubAppInstallation.query.filter_by(
            organization_id=org.id,
            installation_id=installation_id,
        ).first()
        is not None
    )


def test_verified_installation_without_session_token_redirects(
    app, client, admin_user, org
):
    _login(client, admin_user)

    response = client.get(f"/github/connect/{org.slug}/verified")

    assert response.status_code == 302
    assert response.location.endswith(f"/organizations/{org.slug}/settings")


def test_connect_complete_without_installation_token_redirects(
    app, client, admin_user, org
):
    _login(client, admin_user)

    response = client.post(f"/github/connect/{org.slug}/complete", data={})

    assert response.status_code == 302
    assert response.location.endswith(f"/organizations/{org.slug}/settings")


def test_expired_connect_state_uses_connect_error_path(app, client, admin_user, org):
    _login(client, admin_user)
    state = github_installations.connect_state(org, admin_user.id)

    with patch(
        "cabotage.server.user.github_installations.GITHUB_INSTALL_STATE_MAX_AGE_SECONDS",
        -1,
    ):
        response = client.get(
            "/auth/github/callback",
            query_string={"state": state, "code": "oauth-code"},
        )

    assert response.status_code == 302
    assert response.location.endswith("/organizations")


def test_install_callback_rejects_non_integer_installation_id(
    app, client, admin_user, org
):
    _login(client, admin_user)
    state = github_installations.install_state(org, admin_user.id)

    response = client.get(
        "/github/install/callback",
        query_string={"state": state, "installation_id": "not-an-int"},
    )

    assert response.status_code == 302


def test_install_callback_does_not_use_stale_session_state(
    app, client, admin_user, org
):
    _login(client, admin_user)
    stale_state = github_installations.install_state(org, admin_user.id)
    with client.session_transaction() as sess:
        sess["github_install_state"] = stale_state

    response = client.get(
        "/github/install/callback",
        query_string={"installation_id": "123456"},
    )

    assert response.status_code == 302
    assert response.location.endswith("/organizations")
    assert (
        GitHubAppInstallation.query.filter_by(
            organization_id=org.id,
            installation_id=123456,
        ).first()
        is None
    )


def test_connect_existing_filters_already_connected_installations(
    app, client, admin_user, org
):
    _login(client, admin_user)
    existing = GitHubAppInstallation(
        organization_id=org.id,
        installation_id=333333,
        account_login="existing-org",
        account_type="Organization",
    )
    db.session.add(existing)
    db.session.commit()

    state = github_installations.connect_state(org, admin_user.id)

    with (
        patch(
            "cabotage.server.user.github_oauth._fetch_github_user_access_token",
            return_value="user-token",
        ),
        patch(
            "cabotage.server.user.github_oauth._fetch_github_user_installations",
            return_value=[
                _installation(333333, "existing-org"),
                _installation(444444, "new-org"),
            ],
        ),
    ):
        response = client.get(
            "/auth/github/callback",
            query_string={"state": state, "code": "oauth-code"},
        )

    assert response.status_code == 200
    assert b"<td>new-org</td>" in response.data
    assert b"<td>existing-org</td>" not in response.data
    token = re.search(rb'name="installation" value="([^"]+)"', response.data).group(1)
    payload = github_installations.connect_state_serializer().loads(token.decode())
    assert payload["installation_id"] == 444444
    assert "accessible_repository_ids" not in payload


def test_connect_complete_reauthorizes_selected_installation(
    app, client, admin_user, org
):
    _login(client, admin_user)
    token = github_installations.connect_option(
        org, admin_user.id, _installation(555555, "new-org")
    )

    response = client.post(
        f"/github/connect/{org.slug}/complete",
        data={"installation": token},
    )

    assert response.status_code == 302
    assert response.location.startswith("https://github.com/login/oauth/authorize?")
    assert (
        GitHubAppInstallation.query.filter_by(
            organization_id=org.id,
            installation_id=555555,
        ).first()
        is None
    )


def test_application_settings_rejects_unconnected_installation_id(
    app, client, admin_user, org
):
    _login(client, admin_user)
    project = Project(name=f"Project {uuid.uuid4().hex[:8]}", organization_id=org.id)
    db.session.add(project)
    db.session.flush()
    application = Application(
        name=f"App {uuid.uuid4().hex[:8]}",
        slug=f"app-{uuid.uuid4().hex[:8]}",
        project_id=project.id,
    )
    db.session.add(application)
    db.session.commit()

    forged_installation_id = "999999"
    response = client.post(
        f"/projects/{org.slug}/{project.slug}/applications/{application.slug}/settings",
        data={
            "application_id": str(application.id),
            "github_app_installation_id": forged_installation_id,
            "github_repository": "other-org/private-repo",
            "auto_deploy_branch": "main",
            "subdirectory": "",
            "dockerfile_path": "",
            "procfile_path": "",
            "branch_deploy_watch_paths": "",
            "github_environment_name": "",
            "health_check_path": "",
            "health_check_host": "",
            "deployment_timeout": "300",
        },
    )

    assert response.status_code == 200
    assert (
        b"Select a GitHub installation connected to this organization." in response.data
    )
    db.session.refresh(application)
    assert application.github_app_installation_id is None


def test_application_settings_rejects_non_integer_installation_id_before_db_query(
    app, client, admin_user, org
):
    _login(client, admin_user)
    project = Project(name=f"Project {uuid.uuid4().hex[:8]}", organization_id=org.id)
    db.session.add(project)
    db.session.flush()
    application = Application(
        name=f"App {uuid.uuid4().hex[:8]}",
        slug=f"app-{uuid.uuid4().hex[:8]}",
        project_id=project.id,
    )
    db.session.add(application)
    db.session.commit()

    response = client.post(
        f"/projects/{org.slug}/{project.slug}/applications/{application.slug}/settings",
        data={
            "application_id": str(application.id),
            "github_app_installation_id": "not-an-int",
            "github_repository": "other-org/private-repo",
            "auto_deploy_branch": "main",
            "subdirectory": "",
            "dockerfile_path": "",
            "procfile_path": "",
            "branch_deploy_watch_paths": "",
            "github_environment_name": "production",
            "health_check_path": "",
            "health_check_host": "",
            "deployment_timeout": "300",
        },
    )

    assert response.status_code == 200
    assert b"Select a valid GitHub installation." in response.data
    db.session.refresh(application)
    assert application.github_app_installation_id is None


def test_application_settings_rejects_out_of_range_installation_id_before_db_query(
    app, client, admin_user, org
):
    _login(client, admin_user)
    project = Project(name=f"Project {uuid.uuid4().hex[:8]}", organization_id=org.id)
    db.session.add(project)
    db.session.flush()
    application = Application(
        name=f"App {uuid.uuid4().hex[:8]}",
        slug=f"app-{uuid.uuid4().hex[:8]}",
        project_id=project.id,
    )
    db.session.add(application)
    db.session.commit()

    response = client.post(
        f"/projects/{org.slug}/{project.slug}/applications/{application.slug}/settings",
        data={
            "application_id": str(application.id),
            "github_app_installation_id": str(2**80),
            "github_repository": "other-org/private-repo",
            "auto_deploy_branch": "main",
            "subdirectory": "",
            "dockerfile_path": "",
            "procfile_path": "",
            "branch_deploy_watch_paths": "",
            "github_environment_name": "production",
            "health_check_path": "",
            "health_check_host": "",
            "deployment_timeout": "300",
        },
    )

    assert response.status_code == 200
    assert b"Select a valid GitHub installation." in response.data
    db.session.refresh(application)
    assert application.github_app_installation_id is None


def test_application_settings_rejects_repo_for_known_empty_installation(
    app, client, admin_user, org
):
    _login(client, admin_user)
    installation = GitHubAppInstallation(
        organization_id=org.id,
        installation_id=202020,
        account_login="empty-org",
        account_type="Organization",
        repository_selection="selected",
        repositories=[],
    )
    db.session.add(installation)
    project = Project(name=f"Project {uuid.uuid4().hex[:8]}", organization_id=org.id)
    db.session.add(project)
    db.session.flush()
    application = Application(
        name=f"App {uuid.uuid4().hex[:8]}",
        slug=f"app-{uuid.uuid4().hex[:8]}",
        project_id=project.id,
    )
    db.session.add(application)
    db.session.commit()

    response = client.post(
        f"/projects/{org.slug}/{project.slug}/applications/{application.slug}/settings",
        data={
            "application_id": str(application.id),
            "github_app_installation_id": str(installation.installation_id),
            "github_repository": "empty-org/not-authorized",
            "auto_deploy_branch": "main",
            "subdirectory": "",
            "dockerfile_path": "",
            "procfile_path": "",
            "branch_deploy_watch_paths": "",
            "github_environment_name": "",
            "health_check_path": "",
            "health_check_host": "",
            "deployment_timeout": "300",
        },
    )

    assert response.status_code == 200
    assert (
        b"Select a repository available to the chosen GitHub installation."
        in response.data
    )
    db.session.refresh(application)
    assert application.github_repository is None


def test_application_settings_preserves_private_flag_for_preserved_repository(
    app, client, admin_user, org
):
    _login(client, admin_user)
    installation = GitHubAppInstallation(
        organization_id=org.id,
        installation_id=202021,
        account_login="empty-org",
        account_type="Organization",
        repository_selection="selected",
        repositories=[],
    )
    db.session.add(installation)
    project = Project(name=f"Project {uuid.uuid4().hex[:8]}", organization_id=org.id)
    db.session.add(project)
    db.session.flush()
    application = Application(
        name=f"App {uuid.uuid4().hex[:8]}",
        slug=f"app-{uuid.uuid4().hex[:8]}",
        project_id=project.id,
        github_app_installation_id=installation.installation_id,
        github_repository="empty-org/private",
        github_repository_is_private=True,
    )
    db.session.add(application)
    db.session.commit()

    response = client.post(
        f"/projects/{org.slug}/{project.slug}/applications/{application.slug}/settings",
        data={
            "application_id": str(application.id),
            "github_app_installation_id": str(installation.installation_id),
            "github_repository": application.github_repository,
            "auto_deploy_branch": "main",
            "subdirectory": "",
            "dockerfile_path": "",
            "procfile_path": "",
            "branch_deploy_watch_paths": "",
            "github_environment_name": "",
            "health_check_path": "",
            "health_check_host": "",
            "deployment_timeout": "300",
        },
    )

    assert response.status_code == 302
    db.session.refresh(application)
    assert application.github_repository == "empty-org/private"
    assert application.github_repository_is_private is True


def test_application_settings_all_repo_cache_does_not_reject_uncached_repository(
    app, client, admin_user, org
):
    _login(client, admin_user)
    installation = GitHubAppInstallation(
        organization_id=org.id,
        installation_id=202022,
        account_login="all-org",
        account_type="Organization",
        repository_selection="all",
        repositories=[{"id": 1, "full_name": "all-org/cached", "private": True}],
    )
    db.session.add(installation)
    project = Project(name=f"Project {uuid.uuid4().hex[:8]}", organization_id=org.id)
    db.session.add(project)
    db.session.flush()
    application = Application(
        name=f"App {uuid.uuid4().hex[:8]}",
        slug=f"app-{uuid.uuid4().hex[:8]}",
        project_id=project.id,
    )
    db.session.add(application)
    db.session.commit()

    response = client.post(
        f"/projects/{org.slug}/{project.slug}/applications/{application.slug}/settings",
        data={
            "application_id": str(application.id),
            "github_app_installation_id": str(installation.installation_id),
            "github_repository": "all-org/not-yet-cached",
            "auto_deploy_branch": "main",
            "subdirectory": "",
            "dockerfile_path": "",
            "procfile_path": "",
            "branch_deploy_watch_paths": "",
            "github_environment_name": "",
            "health_check_path": "",
            "health_check_host": "",
            "deployment_timeout": "300",
        },
    )

    assert response.status_code == 302
    db.session.refresh(application)
    assert application.github_app_installation_id == installation.installation_id
    assert application.github_repository == "all-org/not-yet-cached"
    assert application.github_repository_id is None


def test_application_settings_sets_repository_id_from_selected_repository(
    app, client, admin_user, org
):
    _login(client, admin_user)
    installation = GitHubAppInstallation(
        organization_id=org.id,
        installation_id=202023,
        account_login="selected-org",
        account_type="Organization",
        repository_selection="selected",
        repositories=[
            {"id": 777, "full_name": "selected-org/private", "private": True}
        ],
    )
    db.session.add(installation)
    project = Project(name=f"Project {uuid.uuid4().hex[:8]}", organization_id=org.id)
    db.session.add(project)
    db.session.flush()
    application = Application(
        name=f"App {uuid.uuid4().hex[:8]}",
        slug=f"app-{uuid.uuid4().hex[:8]}",
        project_id=project.id,
    )
    db.session.add(application)
    db.session.commit()

    response = client.post(
        f"/projects/{org.slug}/{project.slug}/applications/{application.slug}/settings",
        data={
            "application_id": str(application.id),
            "github_app_installation_id": str(installation.installation_id),
            "github_repository": "selected-org/private",
            "auto_deploy_branch": "main",
            "subdirectory": "",
            "dockerfile_path": "",
            "procfile_path": "",
            "branch_deploy_watch_paths": "",
            "github_environment_name": "",
            "health_check_path": "",
            "health_check_host": "",
            "deployment_timeout": "300",
        },
    )

    assert response.status_code == 302
    db.session.refresh(application)
    assert application.github_app_installation_id == installation.installation_id
    assert application.github_repository_id == 777
    assert application.github_repository == "selected-org/private"
    assert application.github_repository_is_private is True


def test_refresh_reconciles_applications_for_removed_selected_repositories(
    app, client, admin_user, org
):
    _login(client, admin_user)
    installation_id = 212121
    installation = GitHubAppInstallation(
        organization_id=org.id,
        installation_id=installation_id,
        account_login="selected-org",
        account_type="Organization",
        repository_selection="selected",
        repositories=[
            {"id": 1, "full_name": "selected-org/removed", "private": True},
            {"id": 2, "full_name": "selected-org/kept", "private": True},
        ],
    )
    db.session.add(installation)
    project = Project(name=f"Project {uuid.uuid4().hex[:8]}", organization_id=org.id)
    db.session.add(project)
    db.session.flush()
    removed_app = Application(
        name=f"Removed {uuid.uuid4().hex[:8]}",
        slug=f"removed-{uuid.uuid4().hex[:8]}",
        project_id=project.id,
        github_app_installation_id=installation_id,
        github_repository_id=1,
        github_repository="selected-org/removed",
        github_repository_is_private=True,
    )
    kept_app = Application(
        name=f"Kept {uuid.uuid4().hex[:8]}",
        slug=f"kept-{uuid.uuid4().hex[:8]}",
        project_id=project.id,
        github_app_installation_id=installation_id,
        github_repository_id=2,
        github_repository="selected-org/kept",
        github_repository_is_private=True,
    )
    db.session.add_all([removed_app, kept_app])
    db.session.commit()

    with (
        patch(
            "cabotage.server.user.views.github_app.fetch_installation",
            return_value=_installation(installation_id, "selected-org"),
        ),
        patch(
            "cabotage.server.user.github_installations.github_app.fetch_installation_repositories",
            return_value=[{"id": 2, "full_name": "selected-org/kept", "private": True}],
        ),
    ):
        response = client.post(
            f"/github/installations/{org.slug}/{installation.id}/refresh",
            follow_redirects=True,
        )

    assert response.status_code == 200
    assert b"Disconnected 1 application" in response.data
    db.session.refresh(removed_app)
    db.session.refresh(kept_app)
    assert removed_app.github_app_installation_id is None
    assert removed_app.github_repository_id is None
    assert removed_app.github_repository_is_private is False
    assert kept_app.github_app_installation_id == installation_id
    assert kept_app.github_repository_id == 2
    assert kept_app.github_repository_is_private is True


def test_refresh_caches_repositories_when_selection_becomes_all(
    app, client, admin_user, org
):
    _login(client, admin_user)
    installation_id = 222222
    installation = GitHubAppInstallation(
        organization_id=org.id,
        installation_id=installation_id,
        account_login="selected-org",
        account_type="Organization",
        repository_selection="selected",
        repositories=[{"id": 1, "full_name": "selected-org/old", "private": True}],
    )
    db.session.add(installation)
    db.session.commit()
    all_repo_installation = _installation(installation_id, "selected-org")
    all_repo_installation["repository_selection"] = "all"

    with (
        patch(
            "cabotage.server.user.views.github_app.fetch_installation",
            return_value=all_repo_installation,
        ),
        patch(
            "cabotage.server.user.github_installations.github_app.fetch_installation_repositories",
            return_value=[
                {"id": 2, "full_name": "selected-org/current", "private": True}
            ],
        ),
    ):
        response = client.post(
            f"/github/installations/{org.slug}/{installation.id}/refresh",
            follow_redirects=True,
        )

    assert response.status_code == 200
    db.session.refresh(installation)
    assert installation.repository_selection == "all"
    assert installation.repositories == [
        {"id": 2, "full_name": "selected-org/current", "private": True}
    ]
    assert installation.repositories_synced_at is not None


def test_refresh_backfills_application_repository_id(app, client, admin_user, org):
    _login(client, admin_user)
    installation_id = 222224
    installation = GitHubAppInstallation(
        organization_id=org.id,
        installation_id=installation_id,
        account_login="selected-org",
        account_type="Organization",
        repository_selection="selected",
        repositories=[],
    )
    db.session.add(installation)
    project = Project(name=f"Project {uuid.uuid4().hex[:8]}", organization_id=org.id)
    db.session.add(project)
    db.session.flush()
    application = Application(
        name=f"App {uuid.uuid4().hex[:8]}",
        slug=f"app-{uuid.uuid4().hex[:8]}",
        project_id=project.id,
        github_app_installation_id=installation_id,
        github_repository="selected-org/current",
        github_repository_is_private=False,
    )
    db.session.add(application)
    db.session.commit()

    with (
        patch(
            "cabotage.server.user.views.github_app.fetch_installation",
            return_value=_installation(installation_id, "selected-org"),
        ),
        patch(
            "cabotage.server.user.github_installations.github_app.fetch_installation_repositories",
            return_value=[
                {"id": 55, "full_name": "selected-org/current", "private": True}
            ],
        ),
    ):
        response = client.post(
            f"/github/installations/{org.slug}/{installation.id}/refresh",
            follow_redirects=True,
        )

    assert response.status_code == 200
    db.session.refresh(application)
    assert application.github_repository_id == 55
    assert application.github_repository_is_private is True


def test_refresh_clears_stale_repository_cache_when_all_repo_sync_fails(
    app, client, admin_user, org
):
    _login(client, admin_user)
    installation_id = 222223
    installation = GitHubAppInstallation(
        organization_id=org.id,
        installation_id=installation_id,
        account_login="selected-org",
        account_type="Organization",
        repository_selection="selected",
        repositories=[{"id": 1, "full_name": "selected-org/old", "private": True}],
    )
    db.session.add(installation)
    db.session.commit()
    all_repo_installation = _installation(installation_id, "selected-org")
    all_repo_installation["repository_selection"] = "all"

    with (
        patch(
            "cabotage.server.user.views.github_app.fetch_installation",
            return_value=all_repo_installation,
        ),
        patch(
            "cabotage.server.user.github_installations.github_app.fetch_installation_repositories",
            return_value=None,
        ),
    ):
        response = client.post(
            f"/github/installations/{org.slug}/{installation.id}/refresh",
            follow_redirects=True,
        )

    assert response.status_code == 200
    db.session.refresh(installation)
    assert installation.repository_selection == "all"
    assert installation.repositories is None
    assert installation.repositories_synced_at is None


def test_connect_rejects_when_app_repository_access_cannot_be_verified(
    app, client, admin_user, org
):
    _login(client, admin_user)
    installation_id = 232323
    installation = _installation(installation_id, "sync-failed-org")
    state = github_installations.connect_state(
        org, admin_user.id, installation_id=installation_id
    )

    with (
        patch(
            "cabotage.server.user.github_oauth._fetch_github_user_access_token",
            return_value="user-token",
        ),
        patch(
            "cabotage.server.user.github_oauth._fetch_github_user_installations",
            return_value=[installation],
        ),
        patch(
            "cabotage.server.user.github_oauth._fetch_github_user_installation_repository_ids",
            return_value=[],
        ),
        patch(
            "cabotage.server.user.github_installations.github_app.fetch_installation",
            return_value=installation,
        ),
        patch(
            "cabotage.server.user.github_installations.github_app.fetch_installation_repositories",
            return_value=None,
        ),
    ):
        response = client.get(
            "/auth/github/callback",
            query_string={"state": state, "code": "oauth-code"},
            follow_redirects=True,
        )

    assert response.status_code == 200
    assert b"could not verify the GitHub App installation" in response.data
    assert (
        GitHubAppInstallation.query.filter_by(
            organization_id=org.id,
            installation_id=installation_id,
        ).first()
        is None
    )


def test_connect_rejects_when_user_cannot_access_all_app_repositories(
    app, client, admin_user, org
):
    _login(client, admin_user)
    installation_id = 232324
    installation = _installation(installation_id, "limited-org")
    state = github_installations.connect_state(
        org, admin_user.id, installation_id=installation_id
    )

    with (
        patch(
            "cabotage.server.user.github_oauth._fetch_github_user_access_token",
            return_value="user-token",
        ),
        patch(
            "cabotage.server.user.github_oauth._fetch_github_user_installations",
            return_value=[installation],
        ),
        patch(
            "cabotage.server.user.github_oauth._fetch_github_user_installation_repository_ids",
            return_value=[1],
        ),
        patch(
            "cabotage.server.user.github_installations.github_app.fetch_installation",
            return_value=installation,
        ),
        patch(
            "cabotage.server.user.github_installations.github_app.fetch_installation_repositories",
            return_value=[
                {"id": 1, "full_name": "limited-org/visible", "private": True},
                {"id": 2, "full_name": "limited-org/hidden", "private": True},
            ],
        ),
    ):
        response = client.get(
            "/auth/github/callback",
            query_string={"state": state, "code": "oauth-code"},
            follow_redirects=True,
        )

    assert response.status_code == 200
    assert b"could not verify the GitHub App installation" in response.data
    assert (
        GitHubAppInstallation.query.filter_by(
            organization_id=org.id,
            installation_id=installation_id,
        ).first()
        is None
    )


def test_application_connect_clears_inaccessible_selected_repository(
    app, client, admin_user, org
):
    _login(client, admin_user)
    project = Project(name=f"Project {uuid.uuid4().hex[:8]}", organization_id=org.id)
    db.session.add(project)
    db.session.flush()
    application = Application(
        name=f"App {uuid.uuid4().hex[:8]}",
        slug=f"app-{uuid.uuid4().hex[:8]}",
        project_id=project.id,
        github_repository="selected-org/missing",
        github_repository_is_private=True,
    )
    db.session.add(application)
    db.session.commit()

    installation_id = 242424
    installation = _installation(installation_id, "selected-org")
    state = github_installations.connect_state(
        org,
        admin_user.id,
        installation_id=installation_id,
        application=application,
    )

    with (
        patch(
            "cabotage.server.user.github_oauth._fetch_github_user_access_token",
            return_value="user-token",
        ),
        patch(
            "cabotage.server.user.github_oauth._fetch_github_user_installations",
            return_value=[installation],
        ),
        patch(
            "cabotage.server.user.github_oauth._fetch_github_user_installation_repository_ids",
            return_value=[1],
        ),
        patch(
            "cabotage.server.user.github_installations.github_app.fetch_installation",
            return_value=installation,
        ),
        patch(
            "cabotage.server.user.github_installations.github_app.fetch_installation_repositories",
            return_value=[{"id": 1, "full_name": "selected-org/kept", "private": True}],
        ),
    ):
        response = client.get(
            "/auth/github/callback",
            query_string={"state": state, "code": "oauth-code"},
            follow_redirects=True,
        )

    assert response.status_code == 200
    assert b"repository is not available to that installation" in response.data
    db.session.refresh(application)
    assert application.github_app_installation_id is None
    assert application.github_repository == "selected-org/missing"
    assert application.github_repository_is_private is False


def test_application_connect_sets_private_flag_from_selected_repository(
    app, client, admin_user, org
):
    _login(client, admin_user)
    project = Project(name=f"Project {uuid.uuid4().hex[:8]}", organization_id=org.id)
    db.session.add(project)
    db.session.flush()
    application = Application(
        name=f"App {uuid.uuid4().hex[:8]}",
        slug=f"app-{uuid.uuid4().hex[:8]}",
        project_id=project.id,
        github_repository="selected-org/private",
        github_repository_is_private=False,
    )
    db.session.add(application)
    db.session.commit()

    installation_id = 252525
    installation = _installation(installation_id, "selected-org")
    state = github_installations.connect_state(
        org,
        admin_user.id,
        installation_id=installation_id,
        application=application,
    )

    with (
        patch(
            "cabotage.server.user.github_oauth._fetch_github_user_access_token",
            return_value="user-token",
        ),
        patch(
            "cabotage.server.user.github_oauth._fetch_github_user_installations",
            return_value=[installation],
        ),
        patch(
            "cabotage.server.user.github_oauth._fetch_github_user_installation_repository_ids",
            return_value=[1],
        ),
        patch(
            "cabotage.server.user.github_installations.github_app.fetch_installation",
            return_value=installation,
        ),
        patch(
            "cabotage.server.user.github_installations.github_app.fetch_installation_repositories",
            return_value=[
                {"id": 1, "full_name": "selected-org/private", "private": True}
            ],
        ),
    ):
        response = client.get(
            "/auth/github/callback",
            query_string={"state": state, "code": "oauth-code"},
            follow_redirects=True,
        )

    assert response.status_code == 200
    db.session.refresh(application)
    assert application.github_app_installation_id == installation_id
    assert application.github_repository_id == 1
    assert application.github_repository_is_private is True


def test_repository_options_omit_unknown_but_include_known_empty_installations(
    app, org
):
    unknown = GitHubAppInstallation(
        organization_id=org.id,
        installation_id=303030,
        account_login="unknown-org",
        account_type="Organization",
        repositories=None,
    )
    known_empty = GitHubAppInstallation(
        organization_id=org.id,
        installation_id=404040,
        account_login="empty-org",
        account_type="Organization",
        repositories=[],
    )
    db.session.add_all([unknown, known_empty])
    db.session.commit()

    options = github_installations.repository_options_by_installation(org)

    assert str(unknown.installation_id) not in options
    assert options[str(known_empty.installation_id)] == []


def test_installation_choices_do_not_display_installation_ids(app, org):
    installation = GitHubAppInstallation(
        organization_id=org.id,
        installation_id=303030,
        account_login="connected-org",
        account_type="Organization",
    )
    db.session.add(installation)
    db.session.commit()

    choices = github_installations.installation_choices(org, selected_id=404040)

    assert ("", "None") in choices
    assert (str(installation.installation_id), "connected-org") in choices
    assert ("404040", "Unknown installation") in choices
    assert all("303030" not in label for _, label in choices)
    assert all("404040" not in label for _, label in choices)


def test_application_settings_get_does_not_sync_github_repositories(
    app, client, admin_user, org
):
    _login(client, admin_user)
    db.session.add(
        GitHubAppInstallation(
            organization_id=org.id,
            installation_id=101010,
            account_login="connected-org",
            account_type="Organization",
            repositories=None,
        )
    )
    project = Project(name=f"Project {uuid.uuid4().hex[:8]}", organization_id=org.id)
    db.session.add(project)
    db.session.flush()
    application = Application(
        name=f"App {uuid.uuid4().hex[:8]}",
        slug=f"app-{uuid.uuid4().hex[:8]}",
        project_id=project.id,
    )
    db.session.add(application)
    db.session.commit()

    with patch(
        "cabotage.server.user.github_installations.sync_installation_repositories",
        side_effect=AssertionError("settings GET should not sync repositories"),
    ):
        response = client.get(
            f"/projects/{org.slug}/{project.slug}/applications/{application.slug}/settings"
        )

    assert response.status_code == 200
