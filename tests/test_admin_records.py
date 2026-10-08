"""Legacy database records must not render authentication secrets."""

import uuid

import pytest
from flask import url_for
from flask.testing import FlaskClient
from sqlalchemy import inspect

from cabotage.server import db
from cabotage.server.models.auth import User
from tests.admin_passkey_helpers import SigningPasskey
from tests.test_organization_requests import (
    _login,
    admin_user as admin_user,
    app as app,
    client as client,
    regular_user as regular_user,
)


def _record_url(client: FlaskClient, surface: str, user: User) -> str:
    with client.application.test_request_context():
        if surface == "details":
            return url_for("_user.details_view", id=user.id)
        return url_for("_user.index_view")


@pytest.mark.parametrize("surface", ["list", "details"])
def test_user_records_hide_authentication_secrets(
    client: FlaskClient, admin_user: User, regular_user: User, surface: str
) -> None:
    marker = uuid.uuid4().hex
    secrets = {
        "password": f"password-sentinel-{marker}",
        "tf_totp_secret": f"totp-sentinel-{marker}",
        "us_totp_secrets": f"unified-totp-sentinel-{marker}",
        "fs_uniquifier": f"session-sentinel-{marker}",
        "fs_webauthn_user_handle": f"webauthn-sentinel-{marker}",
    }
    if "fs_token_uniquifier" in inspect(User).columns:
        secrets["fs_token_uniquifier"] = f"token-sentinel-{marker}"
    for field, value in secrets.items():
        setattr(regular_user, field, value)
    recovery_code = f"recovery-sentinel-{marker}"
    regular_user.mf_recovery_codes = [recovery_code]
    db.session.commit()

    passkey = SigningPasskey.register(admin_user)
    _login(client, admin_user)
    passkey.enter(client)
    url = _record_url(client, surface, regular_user)
    # Follow actual pagination so the seeded user must be rendered even when
    # the shared test database contains more users than a single page holds.
    pages = (User.query.count() + 19) // 20 if surface == "list" else 1
    for page in range(pages):
        response = (
            client.get(url, query_string={"page": page, "page_size": 20})
            if surface == "list"
            else client.get(url)
        )
        assert response.status_code == 200
        for value in [*secrets.values(), recovery_code]:
            assert value.encode() not in response.data
        if regular_user.username.encode() in response.data:
            assert regular_user.email.encode() in response.data
            break
    else:
        pytest.fail("The seeded user's nonsecret fields were not rendered")


@pytest.mark.parametrize("surface", ["list", "details"])
def test_user_records_require_signed_admin_access(
    client: FlaskClient, admin_user: User, regular_user: User, surface: str
) -> None:
    url = _record_url(client, surface, regular_user)
    _login(client, regular_user)
    assert client.get(url).status_code == 403
    _login(client, admin_user)
    assert client.get(url).status_code == 302
