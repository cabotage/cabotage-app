import base64
import hashlib
import hmac


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

    def teardown(self, exception):
        ctx = stack.top
