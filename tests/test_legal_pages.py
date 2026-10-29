import pytest

from cabotage.server.main.legal import validate_legal_config
from cabotage.server.wsgi import app as _app

LEGAL_PATHS = (
    "/legal/",
    "/legal/terms/",
    "/legal/privacy/",
    "/legal/acceptable-use/",
)
URL_KEYS = (
    "LEGAL_URL",
    "LEGAL_TERMS_URL",
    "LEGAL_PRIVACY_URL",
    "LEGAL_ACCEPTABLE_USE_URL",
)
PSF_URLS = {
    "LEGAL_URL": "https://policies.example.org/hosting/",
    "LEGAL_TERMS_URL": "https://policies.example.org/cabotage/terms/",
    "LEGAL_PRIVACY_URL": "https://policies.example.org/cabotage/privacy/",
    "LEGAL_ACCEPTABLE_USE_URL": "https://policies.example.org/cabotage/aup/",
}


@pytest.fixture
def app():
    keys = ("TESTING", "LEGAL_PROFILE", *URL_KEYS)
    original = {key: _app.config.get(key) for key in keys}
    _app.config["TESTING"] = True
    _app.config.update(dict.fromkeys(("LEGAL_PROFILE", *URL_KEYS)))
    with _app.app_context():
        yield _app
    _app.config.update(original)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.mark.parametrize("path", LEGAL_PATHS)
def test_legal_pages_are_not_served_without_a_profile(client, path):
    assert client.get(path).status_code == 404


def test_no_legal_links_without_configuration(client):
    body = client.get("/about/").get_data(as_text=True)
    assert 'href="/legal/' not in body
    assert ">Legal<" not in body


@pytest.mark.parametrize("path", LEGAL_PATHS)
def test_cabotage_profile_renders_every_page(app, client, path):
    app.config["LEGAL_PROFILE"] = "cabotage"
    assert client.get(path).status_code == 200


def test_cabotage_profile_links_every_page(app, client):
    app.config["LEGAL_PROFILE"] = "cabotage"
    body = client.get("/about/").get_data(as_text=True)
    for path in LEGAL_PATHS:
        assert f'href="{path}"' in body


def test_external_urls_link_out_without_serving_pages(app, client):
    app.config.update(PSF_URLS)
    body = client.get("/about/").get_data(as_text=True)
    for url in PSF_URLS.values():
        assert f'href="{url}"' in body
    assert 'href="/legal/' not in body
    assert client.get("/legal/terms/").status_code == 404


def test_external_urls_only_link_configured_policies(app, client):
    app.config["LEGAL_PRIVACY_URL"] = PSF_URLS["LEGAL_PRIVACY_URL"]
    body = client.get("/about/").get_data(as_text=True)
    assert f'href="{PSF_URLS["LEGAL_PRIVACY_URL"]}"' in body
    assert PSF_URLS["LEGAL_TERMS_URL"] not in body
    assert PSF_URLS["LEGAL_URL"] not in body


def test_validate_rejects_profile_and_urls_together(app):
    app.config["LEGAL_PROFILE"] = "cabotage"
    app.config["LEGAL_TERMS_URL"] = PSF_URLS["LEGAL_TERMS_URL"]
    with pytest.raises(ValueError, match="not both"):
        validate_legal_config(app)


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "/legal/terms/", "policies.example.org/terms"]
)
def test_validate_rejects_non_absolute_http_urls(app, url):
    app.config["LEGAL_PRIVACY_URL"] = url
    with pytest.raises(ValueError, match="CABOTAGE_LEGAL_PRIVACY_URL"):
        validate_legal_config(app)


@pytest.mark.parametrize("profile", ["../cabotage", "Cabotage", "cabotage/terms", 3])
def test_validate_rejects_invalid_profile_names(app, profile):
    app.config["LEGAL_PROFILE"] = profile
    with pytest.raises(ValueError, match="not a valid profile name"):
        validate_legal_config(app)


def test_validate_rejects_profile_without_templates(app):
    app.config["LEGAL_PROFILE"] = "acme-hosting"
    with pytest.raises(ValueError) as exc:
        validate_legal_config(app)
    assert "main/legal/acme-hosting/terms.html" in str(exc.value)
