import time
import uuid
from unittest.mock import patch

import pytest
from flask import Flask
from flask.testing import FlaskClient
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
from cabotage.server.models.resources import PostgresResource, RedisResource
from cabotage.server.wsgi import app as _app
from tests.admin_passkey_helpers import SigningPasskey
from tests.test_organization_requests import _RequestScopedClient


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
    def test_environment_observe_page_shows_backing_services_panel_when_resources_exist(
        self, client, admin_user, observe_context
    ):
        _login(client, admin_user)
        org = observe_context["org"]
        project = observe_context["project"]
        environment = observe_context["environment"]

        resource = PostgresResource(
            environment_id=environment.id,
            name="observe-db",
            slug="observe-db",
            service_version="18",
            size_class="db.small",
            storage_size=1,
        )
        redis = RedisResource(
            environment_id=environment.id,
            name="observe-redis",
            slug="observe-redis",
            service_version="8",
            size_class="cache.small",
            storage_size=1,
        )
        db.session.add(resource)
        db.session.add(redis)
        db.session.commit()

        resp = client.get(
            f"/projects/{org.slug}/{project.slug}/environments/{environment.slug}/observe"
        )

        assert resp.status_code == 200
        assert b"Backing Services" in resp.data
        assert b"chart-backing-cpu" in resp.data
        assert b'id="filter-service"' in resp.data
        assert b"All services" in resp.data
        assert b"observe-db (postgres)" in resp.data
        assert b"observe-redis (redis)" in resp.data
        assert resp.data.count(b"data-range-selector") >= 2
        assert resp.data.count(b"data-time-nav") >= 2

    def test_environment_observe_page_defaults_to_application_and_service_grouping(
        self, client, admin_user, observe_context
    ):
        _login(client, admin_user)
        org = observe_context["org"]
        project = observe_context["project"]
        environment = observe_context["environment"]

        resource = PostgresResource(
            environment_id=environment.id,
            name="observe-db",
            slug="observe-db",
            service_version="18",
            size_class="db.small",
            storage_size=1,
        )
        db.session.add(resource)
        db.session.commit()

        resp = client.get(
            f"/projects/{org.slug}/{project.slug}/environments/{environment.slug}/observe"
        )

        assert resp.status_code == 200
        assert b'cpu: "application"' in resp.data
        assert b'cpu: "service"' in resp.data

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

    def test_environment_observe_metric_joins_backing_service_pods_only(
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
                f"/projects/{org.slug}/{project.slug}/environments/{environment.slug}/observe/metric?metric=cpu&workload=backing_services&group=service"
            )

        assert resp.status_code == 200
        query = mock_query.call_args[0][0]
        assert "kube_pod_labels" in query
        assert 'label_backing_service="true"' in query
        assert "label_cabotage_io_resource_id" in query
        assert 'label_application!=""' not in query

    def test_environment_observe_metric_filters_backing_services_by_service(
        self, client, admin_user, observe_context
    ):
        _login(client, admin_user)
        org = observe_context["org"]
        project = observe_context["project"]
        environment = observe_context["environment"]

        resource = PostgresResource(
            environment_id=environment.id,
            name="observe-db",
            slug="observe-db",
            service_version="18",
            size_class="db.small",
            storage_size=1,
        )
        db.session.add(resource)
        db.session.commit()

        with patch(
            "cabotage.server.user.views._query_mimir_range", return_value=[]
        ) as mock_query:
            resp = client.get(
                f"/projects/{org.slug}/{project.slug}/environments/{environment.slug}/observe/metric?metric=cpu&workload=backing_services&service={resource.id}"
            )

        assert resp.status_code == 200
        query = mock_query.call_args[0][0]
        assert f'label_cabotage_io_resource_id="{resource.id}"' in query

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

    def test_project_observe_metric_scopes_backing_services_without_application_filter(
        self, client, admin_user, observe_context
    ):
        _login(client, admin_user)
        org = observe_context["org"]
        project = observe_context["project"]
        application = observe_context["application"]

        with patch(
            "cabotage.server.user.views._query_mimir_range", return_value=[]
        ) as mock_query:
            resp = client.get(
                f"/projects/{org.slug}/{project.slug}/observe/metric?metric=cpu&workload=backing_services&application={application.slug}"
            )

        assert resp.status_code == 200
        query = mock_query.call_args[0][0]
        assert "label_backing_service" in query
        assert application.slug not in query

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

    def test_application_observe_metric_rejects_backing_services_workload(
        self, client, admin_user, observe_context
    ):
        _login(client, admin_user)
        org = observe_context["org"]
        project = observe_context["project"]
        environment = observe_context["environment"]
        application = observe_context["application"]

        resp = client.get(
            f"/projects/{org.slug}/{project.slug}/env/{environment.slug}/applications/{application.slug}/observe/metric?metric=cpu&workload=backing_services"
        )

        assert resp.status_code == 400


@pytest.fixture
def infra_client(
    app: Flask, admin_user: User, monkeypatch: pytest.MonkeyPatch
) -> FlaskClient:
    admin_user.admin = True
    db.session.commit()
    monkeypatch.setattr(app, "test_client_class", _RequestScopedClient)
    client = app.test_client()
    key = SigningPasskey.register(admin_user)
    _login(client, admin_user)
    key.enter(client)
    return client


@pytest.mark.parametrize("metric", ["cpu", "memory", "network"])
def test_infra_metric_successful_empty_is_not_an_error(
    infra_client: FlaskClient, metric: str
) -> None:
    with patch(
        "cabotage.server.user.views._query_mimir_range", return_value=[]
    ) as query:
        response = infra_client.get(f"/infra/observe/metric?metric={metric}")

    assert response.status_code == 200
    data = response.get_json()
    assert data["result"] == []
    assert data["step"] == 15
    assert "error" not in data
    assert len(data["queries"]) == (2 if metric == "network" else 3)
    for call in query.call_args_list:
        assert call.kwargs["tenant_id"] == "cabotage-infra"
        assert 'label_cabotage_io_infra="true"' in call.args[0]


@pytest.mark.parametrize(
    ("metric", "components"),
    [
        ("cpu", ["usage", "requests", "limits"]),
        ("memory", ["usage", "requests", "limits"]),
        ("network", ["tx", "rx"]),
    ],
)
def test_infra_metric_upstream_failure_is_unavailable(
    infra_client: FlaskClient, metric: str, components: list[str]
) -> None:
    with patch("cabotage.server.user.views._query_mimir_range", return_value=None):
        response = infra_client.get(f"/infra/observe/metric?metric={metric}")

    assert response.status_code == 502
    data = response.get_json()
    assert data["error"] == "monitoring unavailable"
    assert data["result"] is None
    assert data["failed_queries"] == components
    assert len(data["queries"]) == len(components)


@pytest.mark.parametrize(
    ("metric", "failed_index", "component"),
    [
        ("cpu", 0, "usage"),
        ("cpu", 1, "requests"),
        ("cpu", 2, "limits"),
        ("memory", 0, "usage"),
        ("memory", 1, "requests"),
        ("memory", 2, "limits"),
        ("network", 0, "tx"),
        ("network", 1, "rx"),
    ],
)
def test_infra_metric_rejects_incomplete_aggregate(
    infra_client: FlaskClient, metric: str, failed_index: int, component: str
) -> None:
    count = 2 if metric == "network" else 3
    results = [
        None
        if index == failed_index
        else [{"metric": {}, "values": [[1700000010, "1"]]}]
        for index in range(count)
    ]
    with patch("cabotage.server.user.views._query_mimir_range", side_effect=results):
        response = infra_client.get(f"/infra/observe/metric?metric={metric}&group=pod")

    assert response.status_code == 502
    data = response.get_json()
    assert data["result"] is None
    assert data["failed_queries"] == [component]
    assert len(data["queries"]) == count


@pytest.mark.parametrize("value", ["NaN", "+Inf", "-Inf"])
def test_infra_metric_preserves_missing_measurements_and_reference_series(
    infra_client: FlaskClient, value: str
) -> None:
    usage = {"metric": {}, "values": [[1700000010, value]]}
    reference = {"metric": {}, "values": [[1700000010, "2"]]}
    with patch(
        "cabotage.server.user.views._query_mimir_range",
        side_effect=[[usage], [reference], []],
    ):
        response = infra_client.get("/infra/observe/metric?metric=cpu")

    assert response.status_code == 200
    rows = response.get_json()["result"]
    assert rows[0]["values"][0][1] == value
    assert rows[1]["metric"]["__ref__"] == "requests"


def test_infra_metric_reference_only_response_preserves_missing_usage(
    infra_client: FlaskClient,
) -> None:
    with patch(
        "cabotage.server.user.views._query_mimir_range",
        side_effect=[[], [{"metric": {}, "values": [[1700000010, "2"]]}], []],
    ):
        response = infra_client.get("/infra/observe/metric?metric=memory")

    assert response.status_code == 200
    rows = response.get_json()["result"]
    assert len(rows) == 1
    assert rows[0]["metric"]["__ref__"] == "requests"


def test_infra_metric_keeps_historical_window_and_filters_isolated(
    infra_client: FlaskClient,
) -> None:
    start, end = 1700000100, 1700086500
    with patch(
        "cabotage.server.user.views._query_mimir_range", return_value=[]
    ) as query:
        response = infra_client.get(
            "/infra/observe/metric?metric=cpu&range=24h&group=container"
            f"&start={start}&end={end}&workload_namespace=infra-system"
            "&pod=metrics-0&system_containers=1"
        )

    assert response.status_code == 200
    assert response.get_json()["step"] == 300
    for call in query.call_args_list:
        assert call.args[1:] == (start, end, 300)
        assert call.kwargs["tenant_id"] == "cabotage-infra"
        assert 'namespace="infra-system"' in call.args[0]
        assert 'pod="metrics-0"' in call.args[0]
        assert 'label_cabotage_io_infra="true"' in call.args[0]
        assert 'container!="cabotage-sidecar"' not in call.args[0]


def test_infra_metric_not_configured_is_not_empty(
    app: Flask, infra_client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(app.config, "MIMIR_URL", None)
    with patch("cabotage.server.user.views._query_mimir_range") as query:
        response = infra_client.get("/infra/observe/metric?metric=cpu")

    assert response.status_code == 404
    assert response.get_json()["error"] == "not configured"
    query.assert_not_called()


def test_infra_metric_requires_global_admin(
    client: FlaskClient, admin_user: User
) -> None:
    _login(client, admin_user)
    with patch("cabotage.server.user.views._query_mimir_range") as query:
        response = client.get("/infra/observe/metric?metric=cpu")

    assert response.status_code == 403
    query.assert_not_called()


def test_infra_metric_requires_passkey_elevation(
    client: FlaskClient, admin_user: User
) -> None:
    admin_user.admin = True
    db.session.commit()
    _login(client, admin_user)
    with patch("cabotage.server.user.views._query_mimir_range") as query:
        response = client.get("/infra/observe/metric?metric=cpu")

    assert response.status_code == 302
    query.assert_not_called()
