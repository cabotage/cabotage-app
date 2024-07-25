import base64
import hashlib
import hmac
import time

import requests
import jwt

from flask import request


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
        return hmac.compare_digest(
            request.headers.get("X-Hub-Signature-256").split("=")[1],
            hmac.new(
                self.webhook_secret.encode(), msg=request.data, digestmod=hashlib.sha256
            ).hexdigest(),
        )

    def _token_needs_renewed(self):
        return (self._bearer_token_exp - int(time.time())) < 60

    @property
    def bearer_token(self):
        if self._bearer_token is None or self._token_needs_renewed():
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

    def fetch_installation_access_token(self, installation_id):
        access_token_response = requests.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers={
                "Accept": "application/vnd.github.machine-man-preview+json",
                "Authorization": f"Bearer {self.bearer_token}",
            },
            timeout=10
        )
        if "token" not in access_token_response.json():
            print(f"Unable to authenticate for {installation_id}")
            return None
        return access_token_response.json()["token"]

    def teardown(self, exception):
        pass
