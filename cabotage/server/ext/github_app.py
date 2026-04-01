import base64
import hashlib
import hmac
import logging
import time

import jwt
import requests

from flask import request

from cabotage.utils.github import github_session

logger = logging.getLogger(__name__)


class GitHubApp(object):
    def __init__(self, app=None):
        self.app = app
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        self.webhook_secret = None
        self.app_id = None
        self.app_private_key_pem = None
        self._bearer_token = None
        self._bearer_token_exp = -1
        self._bot_login = None

        if app.config["GITHUB_WEBHOOK_SECRET"]:
            self.webhook_secret = app.config["GITHUB_WEBHOOK_SECRET"]

        if app.config["GITHUB_APP_ID"]:
            self.app_id = app.config["GITHUB_APP_ID"]
        if app.config["GITHUB_APP_PRIVATE_KEY"]:
            try:
                self.app_private_key_pem = base64.b64decode(
                    app.config["GITHUB_APP_PRIVATE_KEY"]
                ).decode()
            except Exception as exc:
                raise ValueError(f"Unable to decode GITHUB_APP_PRIVATE_KEY: {exc}")

        app.teardown_appcontext(self.teardown)

    def validate_webhook(self):
        if self.webhook_secret is None:
            return True
        signature = request.headers.get("X-Hub-Signature-256")
        if signature is None:
            return False
        return hmac.compare_digest(
            signature.split("=")[1],
            hmac.new(
                self.webhook_secret.encode(), msg=request.data, digestmod=hashlib.sha256
            ).hexdigest(),
        )

    def _token_needs_renewed(self):
        return (self._bearer_token_exp - int(time.time())) < 60

    @property
    def bearer_token(self):
        if self._bearer_token is None or self._token_needs_renewed():
            if self.app_private_key_pem is None:
                raise RuntimeError("GitHub App private key not configured")
            issued = int(time.time())
            payload = {
                "iat": issued,
                "exp": issued + 599,
                "iss": self.app_id,
            }
            self._bearer_token = jwt.encode(
                payload, self.app_private_key_pem, algorithm="RS256"
            )
            self._bearer_token_exp = issued + 599
        return self._bearer_token

    def _fetch_app_metadata(self):
        if self._bot_login is None:
            resp = github_session.get(
                "https://api.github.com/app",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self.bearer_token}",
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            self._slug = data["slug"]
            self._bot_login = f"{self._slug}[bot]"

    @property
    def slug(self):
        self._fetch_app_metadata()
        return self._slug

    @property
    def bot_login(self):
        self._fetch_app_metadata()
        return self._bot_login

    def fetch_installation_access_token(self, installation_id):
        try:
            resp = github_session.post(
                f"https://api.github.com/app/installations/{installation_id}/access_tokens",
                headers={
                    "Accept": "application/vnd.github.machine-man-preview+json",
                    "Authorization": f"Bearer {self.bearer_token}",
                },
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()["token"]
        except (requests.exceptions.RequestException, KeyError, ValueError):
            logger.exception(
                "Unable to fetch access token for installation %s",
                installation_id,
            )
            return None

    def teardown(self, exception):
        pass
