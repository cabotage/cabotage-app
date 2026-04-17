import time
import uuid
from unittest.mock import patch

import pytest
from flask_security import hash_password

from cabotage.server import db
from cabotage.server.models.auth import Organization, User
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Environment,
    Project,
)
from cabotage.server.wsgi import app as _app


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = user.fs_uniquifier
        sess["_fresh"] = True
        sess["fs_cc"] = "set"
        sess["fs_paa"] = time.time()
        sess["identity.id"] = user.id
        sess["identity.auth_type"] = "session"


@pytest.fixture
def app():
    _app.config["TESTING"] = True
    _app.config["WTF_CSRF_ENABLED"] = False
    _app.config["REQUIRE_MFA"] = False
    _app.config["MIMIR_URL"] = "https://mimir.example.test"
    with _app.app_context():
        yield _app
    _app.config["REQUIRE_MFA"] = True
    _app.config["MIMIR_URL"] = None


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin_user(app):
    user = User(
        username=f"observe-admin-{uuid.uuid4().hex[:8]}",
        email=f"observe-admin-{uuid.uuid4().hex[:8]}@example.com",
        password=hash_password("password123"),
        active=True,
        fs_uniquifier=uuid.uuid4().hex,
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def observe_context(admin_user):
    org = Organization(name="Observe Org", slug=f"observe-org-{uuid.uuid4().hex[:8]}")
    db.session.add(org)
    db.session.flush()
    db.session.add(
        OrganizationMember(organization_id=org.id, user_id=admin_user.id, admin=True)
    )

    project = Project(name="Observe Project", organization_id=org.id)
    db.session.add(project)
    db.session.flush()

    environment = Environment(name="production", project_id=project.id)
    db.session.add(environment)
    db.session.flush()

    application = Application(
        name="Observe App",
        slug=f"observe-app-{uuid.uuid4().hex[:8]}",
        project_id=project.id,
    )
    db.session.add(application)
    db.session.flush()

    app_env = ApplicationEnvironment(
        application_id=application.id,
        environment_id=environment.id,
        k8s_identifier=environment.k8s_identifier,
    )
    db.session.add(app_env)
    db.session.commit()

    return {
        "org": org,
        "project": project,
        "environment": environment,
        "application": application,
        "app_env": app_env,
    }


class TestObserveMetricQueries:
    def test_application_observe_metric_does_not_join_kube_pod_labels(
        self, client, admin_user, observe_context
    ):
        _login(client, admin_user)
        org = observe_context["org"]
        project = observe_context["project"]
        environment = observe_context["environment"]
        application = observe_context["application"]

        with patch(
            "cabotage.server.user.views._query_mimir_range", return_value=[]
        ) as mock_query:
            resp = client.get(
                f"/projects/{org.slug}/{project.slug}/env/{environment.slug}/applications/{application.slug}/observe/metric?metric=cpu"
            )

        assert resp.status_code == 200
        query = mock_query.call_args[0][0]
        assert "kube_pod_labels" not in query

    def test_environment_observe_metric_joins_application_pods_only(
        self, client, admin_user, observe_context
    ):
        _login(client, admin_user)
        org = observe_context["org"]
        project = observe_context["project"]
        environment = observe_context["environment"]

        with patch(
            "cabotage.server.user.views._query_mimir_range", return_value=[]
        ) as mock_query:
            resp = client.get(
                f"/projects/{org.slug}/{project.slug}/environments/{environment.slug}/observe/metric?metric=cpu"
            )

        assert resp.status_code == 200
        query = mock_query.call_args[0][0]
        assert "kube_pod_labels" in query
        assert 'label_application!=""' in query
        assert f'namespace="{environment.k8s_namespace}"' in query

    def test_project_observe_metric_joins_application_pods_only(
        self, client, admin_user, observe_context
    ):
        _login(client, admin_user)
        org = observe_context["org"]
        project = observe_context["project"]

        with patch(
            "cabotage.server.user.views._query_mimir_range", return_value=[]
        ) as mock_query:
            resp = client.get(
                f"/projects/{org.slug}/{project.slug}/observe/metric?metric=cpu"
            )

        assert resp.status_code == 200
        query = mock_query.call_args[0][0]
        assert "kube_pod_labels" in query
        assert 'label_application!=""' in query
        assert "pod=~" in query

    def test_organization_observe_metric_joins_application_pods_only(
        self, client, admin_user, observe_context
    ):
        _login(client, admin_user)
        org = observe_context["org"]

        with patch(
            "cabotage.server.user.views._query_mimir_range", return_value=[]
        ) as mock_query:
            resp = client.get(f"/organizations/{org.slug}/observe/metric?metric=cpu")

        assert resp.status_code == 200
        query = mock_query.call_args[0][0]
        assert "kube_pod_labels" in query
        assert 'label_application!=""' in query
        assert "pod=~" in query
