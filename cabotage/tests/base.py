from flask_testing import TestCase

from cabotage.server import db, create_app
from cabotage.server.models import User

import testing.postgresql

app = create_app()


class BaseTestCase(TestCase):
    def create_app(self):
        app.config.from_object("cabotage.server.config.TestingConfig")
        app.config["SQLALCHEMY_DATABASE_URI"] = self.postgresql.url()
        return app

    def setUp(self):
        db.engine.execute("CREATE EXTENSION IF NOT EXISTS citext")
        db.engine.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        db.create_all()
        user = User(email="ad@min.com", username="admin", password="admin_user")
        db.session.add(user)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()

    @classmethod
    def setUpClass(cls):
        cls.postgresql = testing.postgresql.Postgresql()

    @classmethod
    def tearDownClass(cls):
        cls.postgresql.stop()
