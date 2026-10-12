import datetime
import hashlib
import uuid
from dataclasses import dataclass, field
from typing import cast

from flask import url_for
from sqlalchemy import case, func, select
from sqlalchemy.orm import joinedload, selectinload

from cabotage.server import db
from cabotage.server.models.auth import Organization, User
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Deployment,
    Environment,
    Project,
)
from cabotage.server.query_helpers import compute_ae_status_sets

# Lower rank sorts first: failures, then work in flight, then steady state.
STATUS_RANK = {
    "build-failed": 0,
    "deploy-failed": 0,
    "building": 1,
    "deploying": 2,
    "live": 3,
    "idle": 4,
}
ATTENTION_STATUSES = frozenset({"build-failed", "deploy-failed"})
IN_FLIGHT_STATUSES = frozenset({"building", "deploying"})
ACTIVITY_DAYS = 14
RECENT_DEPLOY_LIMIT = 8
# Above this many organizations the tree starts collapsed, except orgs that
# need attention, so a long membership list stays scannable.
EXPAND_ORG_LIMIT = 6


@dataclass
class EnvStatus:
    ae_id: str
    name: str
    slug: str
    status: str
    url: str
    last_deploy: datetime.datetime | None


@dataclass
class AppRow:
    id: str
    name: str
    slug: str
    url: str
    status: str
    envs: list[EnvStatus]
    last_deploy: datetime.datetime | None
    show_envs: bool


@dataclass
class ProjectRow:
    id: str
    name: str
    slug: str
    url: str
    apps: list[AppRow]


@dataclass
class OrgRow:
    id: str
    name: str
    slug: str
    url: str
    is_member: bool
    is_org_admin: bool
    member_count: int
    color_index: int
    projects: list[ProjectRow]
    status_counts: dict[str, int] = field(default_factory=dict)
    last_deploy: datetime.datetime | None = None
    expanded: bool = True

    @property
    def app_count(self) -> int:
        return sum(len(p.apps) for p in self.projects)

    @property
    def attention_count(self) -> int:
        return sum(self.status_counts.get(s, 0) for s in ATTENTION_STATUSES)

    @property
    def in_flight_count(self) -> int:
        return sum(self.status_counts.get(s, 0) for s in IN_FLIGHT_STATUSES)


@dataclass
class WorkItem:
    org: Organization
    project: Project
    app: AppRow
    env: EnvStatus | None
    status: str
    url: str
    is_member: bool


@dataclass
class RecentDeploy:
    status: str
    created: datetime.datetime
    org: Organization
    project: Project
    app: Application
    env: Environment | None
    auto: bool
    is_member: bool
    url: str


@dataclass
class ActivityDay:
    day: datetime.datetime
    total: int
    failed: int


@dataclass(frozen=True)
class StatusSets:
    """Typed view of ``compute_ae_status_sets`` for the dashboard."""

    build_failed: set[uuid.UUID]
    deploy_failed: set[uuid.UUID]
    building: set[uuid.UUID]
    deploying: set[uuid.UUID]
    completed: set[uuid.UUID]
    last_deploy: dict[uuid.UUID, datetime.datetime]
    deploy_count: int

    @classmethod
    def load(cls, ae_ids: list[uuid.UUID]) -> StatusSets:
        raw = cast("dict[str, object]", compute_ae_status_sets(ae_ids))
        return cls(
            build_failed=cast("set[uuid.UUID]", raw["errored_ae_ids"]),
            deploy_failed=cast("set[uuid.UUID]", raw["deploy_failed_ae_ids"]),
            building=cast("set[uuid.UUID]", raw["building_ae_ids"]),
            deploying=cast("set[uuid.UUID]", raw["deploying_ae_ids"]),
            completed=cast("set[uuid.UUID]", raw["completed_ae_ids"]),
            last_deploy=cast(
                "dict[uuid.UUID, datetime.datetime]", raw["last_deploy_by_ae"]
            ),
            deploy_count=cast("int", raw["deploy_count"]),
        )

    def status(self, ae_id: uuid.UUID) -> str:
        if ae_id in self.build_failed:
            return "build-failed"
        if ae_id in self.deploy_failed:
            return "deploy-failed"
        if ae_id in self.building:
            return "building"
        if ae_id in self.deploying:
            return "deploying"
        if ae_id in self.completed:
            return "live"
        return "idle"


AppEnvs = tuple[Application, list[ApplicationEnvironment]]
ProjectApps = tuple[Project, list[AppEnvs]]
AeContext = tuple[Organization, Project, Application, ApplicationEnvironment]


def _latest(*values: datetime.datetime | None) -> datetime.datetime | None:
    present = [v for v in values if v is not None]
    return max(present) if present else None


def _resource_url(
    org: Organization,
    project: Project | None = None,
    application: Application | None = None,
    environment: Environment | None = None,
) -> str:
    if project is None:
        return url_for("user.organization", org_slug=org.slug)
    if application is None:
        return url_for("user.project", org_slug=org.slug, project_slug=project.slug)
    return url_for(
        "user.project_application",
        org_slug=org.slug,
        project_slug=project.slug,
        app_slug=application.slug,
        env_slug=environment.slug if environment is not None else None,
    )


def _build_tree(
    orgs: list[Organization],
) -> tuple[list[tuple[Organization, list[ProjectApps]]], dict[uuid.UUID, AeContext]]:
    """Order active projects, apps and app environments, indexing the latter."""
    tree: list[tuple[Organization, list[ProjectApps]]] = []
    ae_index: dict[uuid.UUID, AeContext] = {}
    for org in orgs:
        projects: list[ProjectApps] = []
        for project in sorted(org.active_projects, key=lambda p: p.name.lower()):
            env_order = {e.id: e.sort_order for e in project.project_environments}
            apps: list[AppEnvs] = []
            for application in sorted(
                project.active_applications, key=lambda a: a.name.lower()
            ):
                app_envs = sorted(
                    (
                        ae
                        for ae in application.application_environments
                        if ae.deleted_at is None and ae.environment.deleted_at is None
                    ),
                    key=lambda ae: env_order.get(ae.environment_id, 0),
                )
                apps.append((application, app_envs))
                for ae in app_envs:
                    ae_index[ae.id] = (org, project, application, ae)
            projects.append((project, apps))
        tree.append((org, projects))
    return tree, ae_index


def dashboard_context(user: User, show_all: bool) -> dict[str, object]:
    memberships: dict[uuid.UUID, bool] = {
        org_id: admin
        for org_id, admin in db.session.execute(
            select(OrganizationMember.organization_id, OrganizationMember.admin)
            .join(Organization, OrganizationMember.organization_id == Organization.id)
            .where(
                OrganizationMember.user_id == user.id,
                Organization.deleted_at.is_(None),
            )
        )
    }
    platform_org_count = (
        db.session.scalar(
            select(func.count(Organization.id)).where(Organization.deleted_at.is_(None))
        )
        or 0
        if user.admin
        else 0
    )

    org_query = select(Organization).where(Organization.deleted_at.is_(None))
    if not show_all:
        org_query = org_query.where(Organization.id.in_(list(memberships)))
    orgs = list(
        db.session.scalars(
            org_query.options(
                selectinload(Organization.projects)
                .selectinload(Project.project_applications)
                .selectinload(Application.application_environments)
                .joinedload(ApplicationEnvironment.environment),
                selectinload(Organization.projects).selectinload(
                    Project.project_environments
                ),
            ).order_by(func.lower(Organization.name))
        )
    )
    member_counts: dict[uuid.UUID, int] = {
        org_id: count
        for org_id, count in db.session.execute(
            select(OrganizationMember.organization_id, func.count())
            .where(OrganizationMember.organization_id.in_([o.id for o in orgs]))
            .group_by(OrganizationMember.organization_id)
        )
    }

    tree, ae_index = _build_tree(orgs)
    ae_ids = list(ae_index)
    sets = StatusSets.load(ae_ids)

    org_rows: list[OrgRow] = []
    attention: list[WorkItem] = []
    in_flight: list[WorkItem] = []
    for org, projects in tree:
        is_member = org.id in memberships
        status_counts: dict[str, int] = {}
        org_last: datetime.datetime | None = None
        project_rows: list[ProjectRow] = []
        for project, apps in projects:
            app_rows: list[AppRow] = []
            for application, app_envs in apps:
                envs = [
                    EnvStatus(
                        ae_id=str(ae.id),
                        name=ae.environment.name,
                        slug=ae.environment.slug,
                        status=sets.status(ae.id),
                        url=_resource_url(
                            org,
                            project,
                            application,
                            ae.environment if project.environments_enabled else None,
                        ),
                        last_deploy=sets.last_deploy.get(ae.id),
                    )
                    for ae in app_envs
                ]
                app_status = min(
                    (e.status for e in envs),
                    key=STATUS_RANK.__getitem__,
                    default="idle",
                )
                row = AppRow(
                    id=str(application.id),
                    name=application.name,
                    slug=application.slug,
                    url=_resource_url(org, project, application),
                    status=app_status,
                    envs=envs,
                    last_deploy=_latest(*(e.last_deploy for e in envs)),
                    show_envs=project.environments_enabled,
                )
                app_rows.append(row)
                status_counts[app_status] = status_counts.get(app_status, 0) + 1
                org_last = _latest(org_last, row.last_deploy)
                for env in envs:
                    item = WorkItem(
                        org=org,
                        project=project,
                        app=row,
                        env=env if project.environments_enabled else None,
                        status=env.status,
                        url=env.url,
                        is_member=is_member,
                    )
                    if env.status in ATTENTION_STATUSES:
                        attention.append(item)
                    elif env.status in IN_FLIGHT_STATUSES:
                        in_flight.append(item)
            project_rows.append(
                ProjectRow(
                    id=str(project.id),
                    name=project.name,
                    slug=project.slug,
                    url=_resource_url(org, project),
                    apps=app_rows,
                )
            )
        org_rows.append(
            OrgRow(
                id=str(org.id),
                name=org.name or org.slug,
                slug=org.slug,
                url=_resource_url(org),
                is_member=is_member,
                is_org_admin=memberships.get(org.id, False),
                member_count=member_counts.get(org.id, 0),
                color_index=org.id.int % 6,
                projects=project_rows,
                status_counts=status_counts,
                last_deploy=org_last,
            )
        )

    collapse = len(org_rows) > EXPAND_ORG_LIMIT
    for org_row in org_rows:
        org_row.expanded = not collapse or bool(
            org_row.attention_count or org_row.in_flight_count
        )

    # Members' own orgs lead even in the platform-wide view.
    org_rows.sort(key=lambda r: not r.is_member)

    today = datetime.datetime.now(datetime.UTC).replace(
        tzinfo=None, hour=0, minute=0, second=0, microsecond=0
    )
    since = today - datetime.timedelta(days=ACTIVITY_DAYS - 1)
    daily: dict[datetime.datetime, tuple[int, int]] = {}
    recent_rows: list[Deployment] = []
    if ae_ids:
        day = func.date_trunc("day", Deployment.created)
        daily = {
            d: (total, failed)
            for d, total, failed in db.session.execute(
                select(
                    day,
                    func.count(Deployment.id),
                    func.count(case((Deployment.error.is_(True), 1))),
                )
                .where(
                    Deployment.application_environment_id.in_(ae_ids),
                    Deployment.created >= since,
                )
                .group_by(day)
            )
        }
        recent_rows = list(
            db.session.scalars(
                select(Deployment)
                .where(Deployment.application_environment_id.in_(ae_ids))
                .options(joinedload(Deployment.application_environment))
                .order_by(Deployment.created.desc())
                .limit(RECENT_DEPLOY_LIMIT)
            )
        )

    activity = [
        ActivityDay(day, *daily.get(day, (0, 0)))
        for day in (
            since + datetime.timedelta(days=offset) for offset in range(ACTIVITY_DAYS)
        )
    ]

    recent: list[RecentDeploy] = []
    for dep in recent_rows:
        org, project, application, ae = ae_index[dep.application_environment_id]
        if dep.error:
            status = "failed"
        elif dep.complete:
            status = "succeeded"
        else:
            status = "running"
        metadata = cast("dict[str, object]", dep.deploy_metadata or {})
        recent.append(
            RecentDeploy(
                status=status,
                # sqlalchemy_utils' Timestamp types `created` as a Column.
                created=cast("datetime.datetime", dep.created),
                org=org,
                project=project,
                app=application,
                env=ae.environment if project.environments_enabled else None,
                auto=bool(metadata.get("auto_deploy")),
                is_member=org.id in memberships,
                url=url_for(
                    "user.deployment_detail",
                    org_slug=org.slug,
                    project_slug=project.slug,
                    app_slug=application.slug,
                    deployment_id=dep.id,
                ),
            )
        )

    status_totals: dict[str, int] = {}
    for org_row in org_rows:
        for status, count in org_row.status_counts.items():
            status_totals[status] = status_totals.get(status, 0) + count

    attention.sort(key=lambda i: (i.org.slug, i.project.slug, i.app.slug))
    in_flight.sort(key=lambda i: STATUS_RANK[i.status])

    # Changes whenever any rendered status changes; the page polls while
    # work is in flight and swaps live regions when this differs.
    signature = hashlib.sha1(
        "|".join(
            f"{ae_id}:{sets.status(ae_id)}" for ae_id in sorted(ae_ids, key=str)
        ).encode()
        + str(recent_rows[0].id if recent_rows else "").encode(),
        usedforsecurity=False,
    ).hexdigest()

    return {
        "org_rows": org_rows,
        "org_count": len(org_rows),
        "member_org_count": len(memberships),
        "platform_org_count": platform_org_count,
        "other_org_count": sum(1 for r in org_rows if not r.is_member),
        "project_count": sum(len(r.projects) for r in org_rows),
        "app_count": sum(r.app_count for r in org_rows),
        "deploy_count": sets.deploy_count,
        "status_totals": status_totals,
        "attention": attention,
        "in_flight": in_flight,
        "activity": activity,
        "activity_max": max((a.total for a in activity), default=0),
        "activity_total": sum(a.total for a in activity),
        "activity_failed": sum(a.failed for a in activity),
        "recent": recent,
        "status_signature": signature,
    }
