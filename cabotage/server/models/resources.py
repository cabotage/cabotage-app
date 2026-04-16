from __future__ import annotations

import datetime
import uuid
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from cabotage.server.models.projects import Environment

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy_utils.models import Timestamp

from cabotage.server import Model

from cabotage.server.models.utils import (
    generate_k8s_identifier,
    slugify,
)


# ---------------------------------------------------------------------------
# Size classes
# ---------------------------------------------------------------------------

postgres_size_classes: dict[str, dict[str, dict[str, str]]] = {
    "db.small": {
        "cpu": {"requests": "250m", "limits": "500m"},
        "memory": {"requests": "512Mi", "limits": "512Mi"},
    },
    "db.medium": {
        "cpu": {"requests": "500m", "limits": "1000m"},
        "memory": {"requests": "1Gi", "limits": "1Gi"},
    },
    "db.large": {
        "cpu": {"requests": "1000m", "limits": "2000m"},
        "memory": {"requests": "2Gi", "limits": "2Gi"},
    },
    "db.xlarge": {
        "cpu": {"requests": "2000m", "limits": "4000m"},
        "memory": {"requests": "4Gi", "limits": "4Gi"},
    },
    "db.2xlarge": {
        "cpu": {"requests": "4000m", "limits": "4000m"},
        "memory": {"requests": "8Gi", "limits": "8Gi"},
    },
}

# Memory in bytes per size class, used for postgres parameter tuning
_postgres_memory_bytes: dict[str, int] = {
    "db.small": 536870912,
    "db.medium": 1073741824,
    "db.large": 2147483648,
    "db.xlarge": 4294967296,
    "db.2xlarge": 8589934592,
}

redis_size_classes: dict[str, dict[str, dict[str, str]]] = {
    "cache.small": {
        "cpu": {"requests": "125m", "limits": "250m"},
        "memory": {"requests": "256Mi", "limits": "256Mi"},
    },
    "cache.medium": {
        "cpu": {"requests": "250m", "limits": "500m"},
        "memory": {"requests": "512Mi", "limits": "512Mi"},
    },
    "cache.large": {
        "cpu": {"requests": "500m", "limits": "1000m"},
        "memory": {"requests": "1Gi", "limits": "1Gi"},
    },
    "cache.xlarge": {
        "cpu": {"requests": "1000m", "limits": "2000m"},
        "memory": {"requests": "2Gi", "limits": "2Gi"},
    },
}

DEFAULT_POSTGRES_SIZE_CLASS = "db.medium"
DEFAULT_REDIS_SIZE_CLASS = "cache.medium"

POSTGRES_VERSIONS = ["18"]
REDIS_VERSIONS = ["8"]
DEFAULT_POSTGRES_VERSION = "18"
DEFAULT_REDIS_VERSION = "8"
DEFAULT_REDIS_LEADER_REPLICAS = 3
DEFAULT_REDIS_FOLLOWER_REPLICAS = 3


# ---------------------------------------------------------------------------
# Postgres auto-tuning
# ---------------------------------------------------------------------------


def compute_postgres_parameters(size_class_name):
    """Compute PostgreSQL tuning parameters based on selected size class RAM."""
    mem_bytes = _postgres_memory_bytes[size_class_name]
    mem_mb = mem_bytes // (1024 * 1024)

    shared_buffers_mb = max(128, mem_mb // 4)
    effective_cache_size_mb = (mem_mb * 3) // 4
    max_connections = 100
    work_mem_kb = max(
        4096,
        ((mem_mb - shared_buffers_mb) * 1024) // (max_connections * 3),
    )
    maintenance_work_mem_mb = min(2048, max(64, mem_mb // 20))
    wal_buffers_mb = min(64, max(1, (shared_buffers_mb * 3) // 100))

    return {
        "shared_buffers": f"{shared_buffers_mb}MB",
        "effective_cache_size": f"{effective_cache_size_mb}MB",
        "work_mem": f"{work_mem_kb}kB",
        "maintenance_work_mem": f"{maintenance_work_mem_mb}MB",
        "wal_buffers": f"{wal_buffers_mb}MB",
        "max_connections": str(max_connections),
        "random_page_cost": "1.1",
        "effective_io_concurrency": "200",
        "checkpoint_completion_target": "0.9",
        "default_statistics_target": "100",
    }


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Resource(Model, Timestamp):
    __versioned__: dict = {}
    __tablename__ = "resources"

    def __init__(self, *args, **kwargs):
        if "slug" not in kwargs and "name" in kwargs:
            kwargs["slug"] = slugify(kwargs["name"])
        if "k8s_identifier" not in kwargs and "slug" in kwargs:
            kwargs["k8s_identifier"] = generate_k8s_identifier(kwargs["slug"])
        super().__init__(*args, **kwargs)

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    type: Mapped[str | None] = mapped_column(String(50))
    environment_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("project_environments.id"),
        index=True,
    )
    name: Mapped[str] = mapped_column(Text())
    slug: Mapped[str] = mapped_column(postgresql.CITEXT())
    k8s_identifier: Mapped[str] = mapped_column(String(64))
    service_version: Mapped[str] = mapped_column(String(16))
    size_class: Mapped[str] = mapped_column(String(32))
    storage_size: Mapped[int] = mapped_column(Integer)
    ha_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    provisioning_status: Mapped[str] = mapped_column(
        String(32), default="pending", server_default="pending"
    )
    provisioning_error: Mapped[str | None] = mapped_column(Text())
    connection_info: Mapped[Any | None] = mapped_column(
        postgresql.JSONB(), server_default=text("'{}'::jsonb")
    )
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, index=True)
    version_id: Mapped[int] = mapped_column(Integer)

    environment: Mapped[Environment] = relationship(
        "Environment", back_populates="resources"
    )

    __mapper_args__ = {
        "polymorphic_identity": "resource",
        "polymorphic_on": "type",
        "version_id_col": version_id,
    }

    __table_args__ = (
        UniqueConstraint(
            "environment_id", "slug", name="uq_resources_environment_id_slug"
        ),
        UniqueConstraint(
            "environment_id",
            "k8s_identifier",
            name="uq_resources_env_k8s_identifier",
        ),
    )


class PostgresResource(Resource):
    __versioned__: dict = {}
    __tablename__ = "resources_postgres"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("resources.id"),
        primary_key=True,
    )
    backup_strategy: Mapped[str] = mapped_column(
        String(16), default="daily", server_default="daily"
    )
    postgres_parameters: Mapped[Any | None] = mapped_column(
        postgresql.JSONB(), server_default=text("'{}'::jsonb")
    )

    __mapper_args__ = {
        "polymorphic_identity": "postgres",
    }


class RedisResource(Resource):
    __versioned__: dict = {}
    __tablename__ = "resources_redis"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("resources.id"),
        primary_key=True,
    )
    leader_replicas: Mapped[int] = mapped_column(
        Integer,
        default=DEFAULT_REDIS_LEADER_REPLICAS,
        server_default=str(DEFAULT_REDIS_LEADER_REPLICAS),
    )
    follower_replicas: Mapped[int] = mapped_column(
        Integer,
        default=DEFAULT_REDIS_FOLLOWER_REPLICAS,
        server_default=str(DEFAULT_REDIS_FOLLOWER_REPLICAS),
    )

    __mapper_args__ = {
        "polymorphic_identity": "redis",
    }
