import datetime
import uuid
from typing import Any, TypedDict

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cabotage.server import Model
from cabotage.utils.cost.rates import CostRates, SnapshotRates


class CostRunStats(TypedDict, total=False):
    pods_seen: int
    pods_unallocated: int
    node_hours: float
    nodes_unpriced: int
    node_data_available: bool
    cluster_compute_cost: float
    tenant_compute_cost: float
    overhead_pool: float
    overhead_allocated: float
    fixed_overhead: float
    synthetic: bool
    seed: str
    last_attempt_status: str
    last_attempt_error: str | None
    last_attempt_at: str


class CostCollectionRun(Model):
    __tablename__ = "cost_collection_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    period_start: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    error: Mapped[str | None] = mapped_column(Text())
    rates: Mapped[CostRates | None] = mapped_column(
        postgresql.JSONB(), server_default=text("'{}'::jsonb")
    )
    stats: Mapped[CostRunStats | None] = mapped_column(
        postgresql.JSONB(), server_default=text("'{}'::jsonb")
    )

    line_items: Mapped[list[CostLineItem]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("period_start", name="uq_cost_collection_runs_period_start"),
    )


class CostLineItem(Model):
    __tablename__ = "cost_line_items"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("cost_collection_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    period_start: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
    )
    environment_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("project_environments.id", ondelete="SET NULL"),
    )
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("project_applications.id", ondelete="SET NULL"),
    )
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("resources.id", ondelete="SET NULL"),
    )
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    meter: Mapped[str] = mapped_column(String(16), nullable=False)
    component: Mapped[str] = mapped_column(String(64), nullable=False)
    quantity: Mapped[float] = mapped_column(Float(), nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    usage_quantity: Mapped[float | None] = mapped_column(Float())
    request_quantity: Mapped[float | None] = mapped_column(Float())
    cost_usd: Mapped[Any] = mapped_column(Numeric(14, 6), nullable=False)

    run: Mapped[CostCollectionRun] = relationship(back_populates="line_items")

    __table_args__ = (
        Index(
            "ix_cost_line_items_organization_id_period_start",
            "organization_id",
            "period_start",
        ),
        Index(
            "ix_cost_line_items_project_id_period_start", "project_id", "period_start"
        ),
        Index(
            "ix_cost_line_items_environment_id_period_start",
            "environment_id",
            "period_start",
        ),
        Index(
            "ix_cost_line_items_application_id_period_start",
            "application_id",
            "period_start",
        ),
        Index("ix_cost_line_items_period_start", "period_start"),
        # FK columns not led by another index; ON DELETE actions scan without them.
        Index("ix_cost_line_items_run_id", "run_id"),
        Index("ix_cost_line_items_resource_id", "resource_id"),
    )


class CostNode(Model):
    __tablename__ = "cost_nodes"

    name: Mapped[str] = mapped_column(String(253), primary_key=True)
    instance_type: Mapped[str | None] = mapped_column(String(64))
    capacity_type: Mapped[str | None] = mapped_column(String(32))
    cpu_capacity: Mapped[float | None] = mapped_column(Float())
    memory_gib: Mapped[float | None] = mapped_column(Float())
    last_seen: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))


class CostPriceSnapshot(Model):
    """Rates fetched from the AWS Price List API, layered over the built-in
    defaults by :func:`cabotage.utils.cost.rates.effective_rates`. ``rates``
    holds only the keys that were fetched (a partial ``CostRates``).
    """

    __tablename__ = "cost_price_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    fetched_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    region: Mapped[str] = mapped_column(String(32), nullable=False)
    rates: Mapped[SnapshotRates] = mapped_column(postgresql.JSONB(), nullable=False)

    @classmethod
    def latest(cls) -> CostPriceSnapshot | None:
        snapshot: CostPriceSnapshot | None = cls.query.order_by(
            cls.fetched_at.desc()
        ).first()
        return snapshot
