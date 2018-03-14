import base64
import hashlib
import hmac
import time

import jwt

from flask import current_app
from flask import request
from flask import _app_ctx_stack as stack


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

        if app.config['GITHUB_WEBHOOK_SECRET']:
            self.webhook_secret = app.config['GITHUB_WEBHOOK_SECRET']

        if app.config['GITHUB_APP_ID']:
            self.app_id = app.config['GITHUB_APP_ID']
        if app.config['GITHUB_APP_PRIVATE_KEY']:
            try:
                self.private_key_pem = base64.b64decode(app.config['GITHUB_APP_PRIVATE_KEY'])
            except Exception as exc:
                raise ValueError(f'Unable to decode GITHUB_APP_PRIVATE_KEY: {exc}')

        app.teardown_appcontext(self.teardown)

    def validate_webhook(self):
        if self.webhook_secret is None:
            return True
        return hmac.compare_digest(
            request.headers.get('X-Hub-Signature').split('=')[1],
            hmac.new(self.webhook_secret.encode(), msg=request.data, digestmod=hashlib.sha1).hexdigest()
        )

    def _token_needs_renewed(self):
        return (self._bearer_token_exp - int(time.time())) > 60

    @property
    def bearer_token(self):
        if self._bearer_token is None or self._token_needs_renewed():
            issued = int(time.time())
            payload = {
                'iat': issued,
                'exp': issued + 600,
                'iss': self.app_id,
            }
            self._bearer_token = jwt.encode(payload, self.private_key_pem, algorithm='RS256')
            self._bearer_token_exp = issued + 600
        return self._bearer_token

    def teardown(self, exception):
        ctx = stack.top
