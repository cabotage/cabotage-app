import json
import re
import uuid

import pytest
from flask import g

from cabotage.server.main.clusters import (
    cluster_context,
    parse_clusters,
    validate_cluster_config,
)
from cabotage.server.models.auth import User
from cabotage.server.wsgi import app as _app

HERE = {
    "name": "psfhosted",
    "url": "http://localhost",
    "region": "us-east-2",
    "color": "teal",
}
PYPI = {"name": "pypi", "url": "https://cabotage.us-east-2.pypi.io", "color": "amber"}


@pytest.fixture
def app():
    keys = ("TESTING", "CLUSTERS", "CLUSTER_NAME", "CLUSTER_SWITCHER_ACCESS")
    original = {key: _app.config.get(key) for key in keys}
    _app.config.update(
        TESTING=True,
        CLUSTERS=None,
        CLUSTER_NAME=None,
        CLUSTER_SWITCHER_ACCESS="admins",
    )
    with _app.app_context():
        yield _app
    _app.config.update(original)


def context_for(app, admin=None):
    """cluster_context() as seen by an anonymous visitor, or a (non-)admin user."""
    with app.test_request_context("/", base_url="http://localhost"):
        if admin is not None:
            g._login_user = User(
                admin=admin, active=True, fs_uniquifier=uuid.uuid4().hex
            )
        return cluster_context()


@pytest.fixture
def client(app):
    return app.test_client()


def page_title(body):
    return body.split("<title>", 1)[1].split("</title>", 1)[0]


def test_env_json_string_is_accepted():
    clusters = parse_clusters(json.dumps([HERE, PYPI]))
    assert [c.name for c in clusters] == ["psfhosted", "pypi"]


@pytest.mark.parametrize(
    "url, origin",
    [
        ("HTTPS://Cabotage.Example.com/", "https://cabotage.example.com"),
        ("https://cabotage.example.com:443", "https://cabotage.example.com"),
        ("http://127.0.0.1:8840", "http://127.0.0.1:8840"),
    ],
)
def test_url_is_normalized_to_its_origin(url, origin):
    (cluster,) = parse_clusters([{"name": "a", "url": url}])
    assert cluster.url == origin


def test_missing_colors_are_assigned_distinctly_by_position():
    clusters = parse_clusters(
        [{"name": f"c{i}", "url": f"https://c{i}.example.com"} for i in range(3)]
    )
    assert len({c.color for c in clusters}) == 3


@pytest.mark.parametrize(
    "raw, message",
    [
        ("{not json", "not valid JSON"),
        ({"name": "a"}, "JSON array"),
        ([{"url": "https://a.example.com"}], "name"),
        ([{"name": "has space", "url": "https://a.example.com"}], "name"),
        ([{"name": "a"}], "missing url"),
        ([{"name": "a", "url": "https://a.example.com/projects"}], "bare http"),
        ([{"name": "a", "url": "ftp://a.example.com"}], "bare http"),
        ([{"name": "a", "url": "https://a.example.com:abc"}], "bare http"),
        ([{"name": "a", "url": "https://a.example.com:70000"}], "bare http"),
        ([{"name": "a", "url": "https://user:pw@a.example.com"}], "bare http"),
        ([{"name": "a", "url": "https://a\\@b.example.com"}], "bare http"),
        ([{"name": "a", "url": "https://exämple.com"}], "bare http"),
        ([{"name": "a", "url": "https://a.example.com", "color": "violet"}], "color"),
        (
            [
                {"name": "a", "url": "https://a.example.com"},
                {"name": "A", "url": "https://b.example.com"},
            ],
            "lists name 'A' twice",
        ),
        (
            [
                {"name": "a", "url": "https://x.example.com"},
                {"name": "b", "url": "https://X.example.com/"},
            ],
            "host",
        ),
    ],
)
def test_ambiguous_configuration_fails(raw, message):
    with pytest.raises((ValueError, TypeError), match=message):
        parse_clusters(raw)


def test_cluster_name_must_be_listed(app):
    app.config.update(CLUSTERS=[PYPI], CLUSTER_NAME="psfhosted")
    with pytest.raises(ValueError, match="CABOTAGE_CLUSTER_NAME"):
        validate_cluster_config(app)


def test_unconfigured_install_renders_no_cluster_identity(client):
    body = client.get("/about/").get_data(as_text=True)
    assert re.search(r"data-topbar\s*>", body)
    assert "cluster-crumb" not in body.split("</header>", 1)[0]
    assert '<link rel="icon"' not in body


def test_everyone_sees_which_cluster_they_are_on(app, client):
    app.config["CLUSTERS"] = [HERE, PYPI]
    body = client.get("/about/").get_data(as_text=True)
    assert page_title(body).strip().startswith("psfhosted · ")
    assert re.search(r'data-topbar\s+data-cluster-color="current"', body)
    assert "cluster-crumb-static" in body
    assert '<link rel="icon"' in body
    # The cluster's color is the default accent when the user hasn't picked one.
    assert "localStorage.getItem('accent-color') || \"teal\"" in body


def test_anonymous_visitors_never_get_the_cluster_list(app, client):
    app.config.update(CLUSTERS=[HERE, PYPI], CLUSTER_SWITCHER_ACCESS="users")
    body = client.get("/about/").get_data(as_text=True)
    assert "cluster-switcher-data" not in body
    # Only the app's own markup; the dev debug toolbar dumps config on its own.
    assert PYPI["url"] not in body.split('<div id="flDebug"', 1)[0]


@pytest.mark.parametrize(
    "access, admin, allowed",
    [
        ("admins", True, True),
        ("admins", False, False),
        ("users", False, True),
        ("users", None, False),
    ],
)
def test_switcher_access(app, access, admin, allowed):
    app.config.update(CLUSTERS=[HERE, PYPI], CLUSTER_SWITCHER_ACCESS=access)
    context = context_for(app, admin=admin)
    assert context["current_cluster"].name == "psfhosted"
    assert (context["cluster_switcher"] is not None) is allowed


def test_unknown_access_level_fails(app):
    app.config["CLUSTER_SWITCHER_ACCESS"] = "everyone"
    with pytest.raises(ValueError, match="CABOTAGE_CLUSTER_SWITCHER_ACCESS"):
        validate_cluster_config(app)


def test_install_finds_itself_by_host(app):
    app.config["CLUSTERS"] = [HERE, PYPI]
    data = context_for(app, admin=True)["cluster_switcher"]
    assert data["current"] == "psfhosted"
    assert [c["url"] for c in data["clusters"]] == [HERE["url"], PYPI["url"]]


def test_cluster_name_overrides_host_match(app):
    app.config.update(CLUSTERS=[HERE, PYPI], CLUSTER_NAME="pypi")
    assert context_for(app, admin=True)["cluster_switcher"]["current"] == "pypi"


def test_unlisted_host_is_not_the_current_cluster(app, client):
    app.config["CLUSTERS"] = [PYPI]
    assert context_for(app, admin=True)["cluster_switcher"]["current"] is None
    assert "pypi" not in page_title(client.get("/about/").get_data(as_text=True))


def test_ping_answers_without_a_session_or_cache(client):
    response = client.get("/_cabotage/ping")
    assert response.status_code == 204
    assert response.headers["Cache-Control"] == "no-store"
