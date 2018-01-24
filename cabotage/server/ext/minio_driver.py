import os
import secrets

import minio
import minio.error

from flask import current_app
from flask import _app_ctx_stack as stack


class MinioDriver(object):

    def __init__(self, app=None):
        self.app = app
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        self.minio_endpoint = app.config.get('CABOTAGE_MINIO_ENDPOINT', '127.0.0.1:9000')
        self.minio_access_key = app.config.get('CABOTAGE_MINIO_ACCESS_KEY', '')
        self.minio_secret_key = app.config.get('CABOTAGE_MINIO_SECRET_KEY', '')
        self.minio_secure = app.config.get('CABOTAGE_MINIO_SECURE', True)
        self.minio_bucket = app.config.get('CABOTAGE_MINIO_BUCKET', 'cabotage-builds')

        app.teardown_appcontext(self.teardown)

    def connect_minio(self):
        minio_client = minio.Minio(
            self.minio_endpoint,
            access_key=self.minio_access_key,
            secret_key=self.minio_secret_key,
            secure=self.minio_secure,
        )
        return minio_client

    def teardown(self, exception):
        ctx = stack.top
        if hasattr(ctx, 'minio_client'):
            del(ctx.minio_client)

    def create_bucket(self):
        try:
            self.minio_connection.make_bucket(self.minio_bucket)
        except minio.error.BucketAlreadyOwnedByYou:
            pass
        except minio.error.BucketAlreadyExists:
            pass
        except minio.error.ResponseError:
            raise

    def write_object(self, org_slug, proj_slug, app_slug, fileobj):
        fileobj.seek(0, os.SEEK_END)
        file_length = fileobj.tell()
        fileobj.seek(0)
        self.create_bucket()
        path = f'{org_slug}/{proj_slug}/{app_slug}/{secrets.token_urlsafe(8)}.tar.gz'
        etag = self.minio_connection.put_object(
            self.minio_bucket,
            path,
            fileobj,
            file_length,
            'application/tar+gzip',
        )
        return {'etag': etag, 'path': path}

    @property
    def minio_connection(self):
        ctx = stack.top
        if ctx is not None:
            if not hasattr(ctx, 'minio_client'):
                ctx.minio_client = self.connect_minio()
            return ctx.minio_client
