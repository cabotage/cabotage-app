from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from typing import TYPE_CHECKING

import jwt
import requests

from flask import request

from cabotage.utils.github import github_session


if TYPE_CHECKING:
    from cabotage._types.server import TypedFlask

logger = logging.getLogger(__name__)


class GitHubApp(object):
    def __init__(self, app: TypedFlask | None = None) -> None:
        self.app = app
        self.webhook_secret: str | None = None
        self.app_id: str | None = None
        self.app_private_key_pem: str | None = None
        self._bearer_token: str | None = None
        self._bearer_token_exp: int = -1
        self._bot_login: str | None = None
        self._slug: str | None = None

        if app is not None:
            self.init_app(app)

    def init_app(self, app: TypedFlask) -> None:
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

    def validate_webhook(self) -> bool:
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

    def _token_needs_renewed(self) -> bool:
        return (self._bearer_token_exp - int(time.time())) < 60

    @property
    def bearer_token(self) -> str:
        if self._bearer_token is None or self._token_needs_renewed():
            if self.app_private_key_pem is None:
                raise RuntimeError("GitHub App private key not configured")
            issued = int(time.time())
            payload = {
                "iat": issued,
                "exp": issued + 599,
                "iss": str(self.app_id),
            }
            self._bearer_token = jwt.encode(
                payload, self.app_private_key_pem, algorithm="RS256"
            )
            self._bearer_token_exp = issued + 599
        return self._bearer_token

    def _fetch_app_metadata(self) -> None:
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
    def slug(self) -> str:
        self._fetch_app_metadata()
        assert self._slug
        return self._slug

    @property
    def bot_login(self) -> str:
        self._fetch_app_metadata()
        assert self._bot_login
        return self._bot_login

    @property
    def install_url(self) -> str:
        return f"https://github.com/apps/{self.slug}/installations/new"

    def fetch_installation_access_token(self, installation_id: str | int):
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

    def fetch_installation_repositories(self, installation_id: str | int):
        access_token = self.fetch_installation_access_token(installation_id)
        if access_token is None:
            return None

        try:
            repositories = []
            url = "https://api.github.com/installation/repositories"
            params = {"per_page": 100}
            headers = {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {access_token}",
            }
            while url:
                resp = github_session.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=10,
                )
                resp.raise_for_status()
                repositories.extend(resp.json().get("repositories") or [])
                url = resp.links.get("next", {}).get("url")
                params = None
            return repositories
        except (requests.exceptions.RequestException, ValueError, AttributeError):
            logger.exception(
                "Unable to fetch repositories for GitHub installation %s",
                installation_id,
            )
            return None

    def fetch_installation(self, installation_id: str):
        try:
            resp = github_session.get(
                f"https://api.github.com/app/installations/{installation_id}",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self.bearer_token}",
                },
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.RequestException, ValueError):
            logger.exception(
                "Unable to fetch GitHub installation %s",
                installation_id,
            )
            return None

    def teardown(self, exception: BaseException | None) -> None:
        pass
