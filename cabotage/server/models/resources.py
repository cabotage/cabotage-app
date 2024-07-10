from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy_utils.models import Timestamp

from cabotage.server import db


class Resource(db.Model, Timestamp):
    __versioned__: dict = {}
    __tablename__ = "resources"

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        nullable=False,
        primary_key=True,
    )
    type = db.Column(db.String(50))
    application_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey("project_applications.id"),
        nullable=False,
    )
    version_id = db.Column(db.Integer, nullable=False)

    __mapper_args__ = {
        "polymorphic_identity": "resource",
        "polymorphic_on": "type",
        "version_id_col": version_id,
    }


class PostgresResource(Resource):
    __versioned__: dict = {}
    __tablename__ = "resources_postgres"

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey("resources.id"),
        nullable=False,
        primary_key=True,
    )
    version_id = db.Column(db.Integer, nullable=False)

    __mapper_args__ = {
        "polymorphic_identity": "postgres",
        "version_id_col": version_id,
    }


class RedisResource(Resource):
    __versioned__: dict = {}
    __tablename__ = "resources_redis"

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey("resources.id"),
        nullable=False,
        primary_key=True,
    )
    version_id = db.Column(db.Integer, nullable=False)

    __mapper_args__ = {
        "polymorphic_identity": "redis",
        "version_id_col": version_id,
    }


class IngressResource(Resource):
    __versioned__: dict = {}
    __tablename__ = "resources_ingress"

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey("resources.id"),
        nullable=False,
        primary_key=True,
    )
    version_id = db.Column(db.Integer, nullable=False)

    __mapper_args__ = {
        "polymorphic_identity": "ingress",
        "version_id_col": version_id,
    }


class CertificateResource(Resource):
    __versioned__: dict = {}
    __tablename__ = "resources_certificate"

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey("resources.id"),
        nullable=False,
        primary_key=True,
    )
    version_id = db.Column(db.Integer, nullable=False)

    __mapper_args__ = {
        "polymorphic_identity": "certificate",
        "version_id_col": version_id,
    }
