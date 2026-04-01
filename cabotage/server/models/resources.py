from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Integer, String, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy_utils.models import Timestamp

from cabotage.server import Model


class Resource(Model, Timestamp):
    __versioned__: dict = {}
    __tablename__ = "resources"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    type: Mapped[str | None] = mapped_column(String(50))
    application_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("project_applications.id"),
    )
    version_id: Mapped[int] = mapped_column(Integer)

    __mapper_args__ = {
        "polymorphic_identity": "resource",
        "polymorphic_on": "type",
        "version_id_col": version_id,
    }


class PostgresResource(Resource):
    __versioned__: dict = {}
    __tablename__ = "resources_postgres"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("resources.id"),
        primary_key=True,
    )
    version_id: Mapped[int] = mapped_column(Integer)

    __mapper_args__ = {
        "polymorphic_identity": "postgres",
        "version_id_col": version_id,
    }


class RedisResource(Resource):
    __versioned__: dict = {}
    __tablename__ = "resources_redis"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("resources.id"),
        primary_key=True,
    )
    version_id: Mapped[int] = mapped_column(Integer)

    __mapper_args__ = {
        "polymorphic_identity": "redis",
        "version_id_col": version_id,
    }


class IngressResource(Resource):
    __versioned__: dict = {}
    __tablename__ = "resources_ingress"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("resources.id"),
        primary_key=True,
    )
    version_id: Mapped[int] = mapped_column(Integer)

    __mapper_args__ = {
        "polymorphic_identity": "ingress",
        "version_id_col": version_id,
    }


class CertificateResource(Resource):
    __versioned__: dict = {}
    __tablename__ = "resources_certificate"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("resources.id"),
        primary_key=True,
    )
    version_id: Mapped[int] = mapped_column(Integer)

    __mapper_args__ = {
        "polymorphic_identity": "certificate",
        "version_id_col": version_id,
    }
