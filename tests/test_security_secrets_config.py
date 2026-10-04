"""Startup protection against the built-in development secrets."""

import pytest

from cabotage.server.config import validate_security_secrets_config


@pytest.fixture
def config():
    return {
        "DEBUG": False,
        "SECRET_KEY": "test-session-key",
        "SECURITY_PASSWORD_SALT": "test-password-salt",
        "REGISTRY_AUTH_SECRET": "test-registry-secret",
        "SECURITY_TOTP_SECRETS": {1: "test-totp-secret"},
    }


@pytest.mark.parametrize(
    "key, default",
    [
        ("SECRET_KEY", "my_precious"),
        ("SECURITY_PASSWORD_SALT", "my_precious"),
        ("REGISTRY_AUTH_SECRET", "v3rys3cur3"),
    ],
)
def test_rejects_each_scalar_default(config, key, default):
    config[key] = default

    with pytest.raises(ValueError, match=f"CABOTAGE_{key}"):
        validate_security_secrets_config(config)


@pytest.mark.parametrize(
    "secrets",
    [
        {1: "my_precious"},
        {2: "my_precious"},
        {"1": "my_precious"},
        {"2": "my_precious"},
        {1: "my_precious", 2: "test-new-key"},
        {1: "test-new-key", 2: "my_precious"},
    ],
)
def test_rejects_default_totp_secret_under_any_tag(config, secrets):
    config["SECURITY_TOTP_SECRETS"] = secrets

    with pytest.raises(ValueError, match="CABOTAGE_SECURITY_TOTP_SECRETS"):
        validate_security_secrets_config(config)


def test_reports_all_defaults_even_in_testing_mode(config):
    config.update(
        TESTING=True,
        SECRET_KEY="my_precious",
        SECURITY_PASSWORD_SALT="my_precious",
        REGISTRY_AUTH_SECRET="v3rys3cur3",
        SECURITY_TOTP_SECRETS={1: "my_precious"},
    )

    with pytest.raises(ValueError) as exc:
        validate_security_secrets_config(config)

    for key in (
        "CABOTAGE_SECRET_KEY",
        "CABOTAGE_SECURITY_PASSWORD_SALT",
        "CABOTAGE_REGISTRY_AUTH_SECRET",
        "CABOTAGE_SECURITY_TOTP_SECRETS",
    ):
        assert key in str(exc.value)


def test_default_secret_is_allowed_only_while_debug_is_enabled(config):
    config.update(DEBUG=True, SECRET_KEY="my_precious")
    validate_security_secrets_config(config)

    config["DEBUG"] = False
    with pytest.raises(ValueError, match="CABOTAGE_SECRET_KEY"):
        validate_security_secrets_config(config)
