"""Data for the signed-in dashboard (``main/home.html``).

Every number and status here is read from the database:

- Counts come from the organizations, projects and applications the user can
  see, skipping anything soft-deleted.
- Each app environment's status comes from its newest Image and Deployment
  rows, via ``compute_ae_status_sets``.
- The 14-day chart and the deploy lists come from ``Deployment.created``,
  ``.complete`` and ``.error``.
"""

import datetime
import hashlib
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import cast

from flask import url_for
from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import selectinload

from cabotage.server import db
from cabotage.server.models.auth import Organization, User
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Deployment,
    Project,
)
from cabotage.server.query_helpers import compute_ae_status_sets

# Sort order for statuses: failures first, then work in progress.
STATUS_ORDER = {
    "build-failed": 0,
    "deploy-failed": 0,
    "building": 1,
    "deploying": 2,
    "live": 3,
    "idle": 4,
}
FAILED = frozenset({"build-failed", "deploy-failed"})
IN_PROGRESS = frozenset({"building", "deploying"})

CHART_DAYS = 14
RECENT_DEPLOYS_SHOWN = 8
DEPLOY_LIST_LIMIT = 100
# With more organizations than this, the tree starts collapsed (except
# organizations with failing or in-progress apps) so the page stays short.
EXPAND_ALL_UP_TO = 6


@dataclass
class EnvironmentStatus:
    id: uuid.UUID
    name: str
    slug: str
    status: str
    url: str
    last_deploy: datetime.datetime | None


@dataclass
class AppRow:
    name: str
    slug: str
    url: str
    environments: list[EnvironmentStatus]
    # Projects without environments have a single hidden "default" one.
    show_environments: bool

    @property
    def status(self) -> str:
        """The most urgent status across the app's environments."""
        statuses = [e.status for e in self.environments]
        return min(statuses, key=STATUS_ORDER.__getitem__, default="idle")

    @property
    def last_deploy(self) -> datetime.datetime | None:
        return _newest(e.last_deploy for e in self.environments)


@dataclass
class ProjectRow:
    name: str
    slug: str
    url: str
    apps: list[AppRow]

    @property
    def status_counts(self) -> dict[str, int]:
        return _count_statuses(self.apps)

    @property
    def last_deploy(self) -> datetime.datetime | None:
        return _newest(a.last_deploy for a in self.apps)


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
    expanded: bool = True

    @property
    def apps(self) -> list[AppRow]:
        return [app for project in self.projects for app in project.apps]

    @property
    def app_count(self) -> int:
        return len(self.apps)

    @property
    def status_counts(self) -> dict[str, int]:
        return _count_statuses(self.apps)

    @property
    def failing_count(self) -> int:
        return sum(1 for app in self.apps if app.status in FAILED)

    @property
    def in_progress_count(self) -> int:
        return sum(1 for app in self.apps if app.status in IN_PROGRESS)

    @property
    def last_deploy(self) -> datetime.datetime | None:
        return _newest(a.last_deploy for a in self.apps)


@dataclass
class AppItem:
    """One app (or one of its environments) in a flat list."""

    org: OrgRow
    project: ProjectRow
    app: AppRow
    environment: EnvironmentStatus | None = None

    @property
    def status(self) -> str:
        return self.environment.status if self.environment else self.app.status

    @property
    def url(self) -> str:
        return self.environment.url if self.environment else self.app.url


@dataclass
class DeployRow:
    id: uuid.UUID
    status: str  # "succeeded", "failed" or "running"
    created: datetime.datetime
    automatic: bool
    url: str
    org: OrgRow
    project: ProjectRow
    app: AppRow
    environment: EnvironmentStatus | None


@dataclass
class DayActivity:
    day: datetime.datetime
    succeeded: int
    failed: int
    running: int

    @property
    def total(self) -> int:
        return self.succeeded + self.failed + self.running


@dataclass(frozen=True)
class EnvironmentStatuses:
    """Status lookups for a set of app environments, from compute_ae_status_sets."""

    build_failed: set[uuid.UUID]
    deploy_failed: set[uuid.UUID]
    building: set[uuid.UUID]
    deploying: set[uuid.UUID]
    deployed: set[uuid.UUID]
    last_deploy: dict[uuid.UUID, datetime.datetime]
    completed_deploys: int

    @classmethod
    def load(cls, app_environment_ids: list[uuid.UUID]) -> EnvironmentStatuses:
        sets = cast("dict[str, object]", compute_ae_status_sets(app_environment_ids))
        return cls(
            build_failed=cast("set[uuid.UUID]", sets["errored_ae_ids"]),
            deploy_failed=cast("set[uuid.UUID]", sets["deploy_failed_ae_ids"]),
            building=cast("set[uuid.UUID]", sets["building_ae_ids"]),
            deploying=cast("set[uuid.UUID]", sets["deploying_ae_ids"]),
            deployed=cast("set[uuid.UUID]", sets["completed_ae_ids"]),
            last_deploy=cast(
                "dict[uuid.UUID, datetime.datetime]", sets["last_deploy_by_ae"]
            ),
            completed_deploys=cast("int", sets["deploy_count"]),
        )

    def status_of(self, app_environment_id: uuid.UUID) -> str:
        if app_environment_id in self.build_failed:
            return "build-failed"
        if app_environment_id in self.deploy_failed:
            return "deploy-failed"
        if app_environment_id in self.building:
            return "building"
        if app_environment_id in self.deploying:
            return "deploying"
        if app_environment_id in self.deployed:
            return "live"
        return "idle"


def _newest(
    values: Iterable[datetime.datetime | None],
) -> datetime.datetime | None:
    present = [v for v in values if v is not None]
    return max(present) if present else None


def _count_statuses(apps: list[AppRow]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for app in apps:
        counts[app.status] = counts.get(app.status, 0) + 1
    return counts


def _active_environments(application: Application) -> list[ApplicationEnvironment]:
    """The app's live environments, in the project's environment order."""
    order = {e.id: e.sort_order for e in application.project.project_environments}
    return sorted(
        (
            ae
            for ae in application.application_environments
            if ae.deleted_at is None and ae.environment.deleted_at is None
        ),
        key=lambda ae: order.get(ae.environment_id, 0),
    )


def _load_memberships(user: User) -> dict[uuid.UUID, bool]:
    """Organization id -> whether the user is an admin of it."""
    rows = db.session.execute(
        select(OrganizationMember.organization_id, OrganizationMember.admin)
        .join(Organization, OrganizationMember.organization_id == Organization.id)
        .where(
            OrganizationMember.user_id == user.id,
            Organization.deleted_at.is_(None),
        )
    )
    return {org_id: is_admin for org_id, is_admin in rows}


def _load_organizations(org_ids: list[uuid.UUID] | None) -> list[Organization]:
    """Live organizations with projects, apps and environments preloaded.

    ``None`` loads every organization (the platform admin view).
    """
    query = select(Organization).where(Organization.deleted_at.is_(None))
    if org_ids is not None:
        query = query.where(Organization.id.in_(org_ids))
    query = query.options(
        selectinload(Organization.projects).selectinload(Project.project_environments),
        selectinload(Organization.projects)
        .selectinload(Project.project_applications)
        .selectinload(Application.application_environments)
        .selectinload(ApplicationEnvironment.environment),
    ).order_by(func.lower(Organization.name))
    return list(db.session.scalars(query))


def _count_members(org_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    rows = db.session.execute(
        select(OrganizationMember.organization_id, func.count())
        .where(OrganizationMember.organization_id.in_(org_ids))
        .group_by(OrganizationMember.organization_id)
    )
    return {org_id: count for org_id, count in rows}


def _build_org_rows(
    orgs: list[Organization],
    memberships: dict[uuid.UUID, bool],
    member_counts: dict[uuid.UUID, int],
    statuses: EnvironmentStatuses,
) -> list[OrgRow]:
    org_rows = []
    for org in orgs:
        project_rows = []
        for project in sorted(org.active_projects, key=lambda p: p.name.lower()):
            app_rows = []
            for app in sorted(
                project.active_applications, key=lambda a: a.name.lower()
            ):
                environments = [
                    EnvironmentStatus(
                        id=ae.id,
                        name=ae.environment.name,
                        slug=ae.environment.slug,
                        status=statuses.status_of(ae.id),
                        url=url_for(
                            "user.project_application",
                            org_slug=org.slug,
                            project_slug=project.slug,
                            app_slug=app.slug,
                            env_slug=(
                                ae.environment.slug
                                if project.environments_enabled
                                else None
                            ),
                        ),
                        last_deploy=statuses.last_deploy.get(ae.id),
                    )
                    for ae in _active_environments(app)
                ]
                app_rows.append(
                    AppRow(
                        name=app.name,
                        slug=app.slug,
                        url=url_for(
                            "user.project_application",
                            org_slug=org.slug,
                            project_slug=project.slug,
                            app_slug=app.slug,
                        ),
                        environments=environments,
                        show_environments=project.environments_enabled,
                    )
                )
            project_rows.append(
                ProjectRow(
                    name=project.name,
                    slug=project.slug,
                    url=url_for(
                        "user.project", org_slug=org.slug, project_slug=project.slug
                    ),
                    apps=app_rows,
                )
            )
        org_rows.append(
            OrgRow(
                id=str(org.id),
                name=org.name or org.slug,
                slug=org.slug,
                url=url_for("user.organization", org_slug=org.slug),
                is_member=org.id in memberships,
                is_org_admin=memberships.get(org.id, False),
                member_count=member_counts.get(org.id, 0),
                color_index=org.id.int % 6,
                projects=project_rows,
            )
        )

    collapse = len(org_rows) > EXPAND_ALL_UP_TO
    for row in org_rows:
        row.expanded = not collapse or bool(row.failing_count or row.in_progress_count)
    # The user's own organizations come first, even in the admin view.
    org_rows.sort(key=lambda r: not r.is_member)
    return org_rows


def _environment_index(
    org_rows: list[OrgRow],
) -> dict[uuid.UUID, AppItem]:
    """App environment id -> where it sits in the tree."""
    return {
        env.id: AppItem(org, project, app, env if app.show_environments else None)
        for org in org_rows
        for project in org.projects
        for app in project.apps
        for env in app.environments
    }


def _daily_deploys(
    app_environment_ids: list[uuid.UUID], since: datetime.datetime
) -> dict[datetime.datetime, tuple[int, int, int]]:
    """Day -> (succeeded, failed, running) deploys started that day."""
    if not app_environment_ids:
        return {}
    day = func.date_trunc("day", Deployment.created)
    succeeded = and_(Deployment.complete.is_(True), Deployment.error.is_(False))
    running = and_(Deployment.complete.is_(False), Deployment.error.is_(False))
    rows = db.session.execute(
        select(
            day,
            func.count(case((succeeded, 1))),
            func.count(case((Deployment.error.is_(True), 1))),
            func.count(case((running, 1))),
        )
        .where(
            Deployment.application_environment_id.in_(app_environment_ids),
            Deployment.created >= since,
        )
        .group_by(day)
    )
    return {d: (ok, failed, run) for d, ok, failed, run in rows}


def _newest_deploys(
    environments: dict[uuid.UUID, AppItem], limit: int
) -> list[DeployRow]:
    if not environments:
        return []
    deployments = db.session.scalars(
        select(Deployment)
        .where(Deployment.application_environment_id.in_(list(environments)))
        .order_by(Deployment.created.desc())
        .limit(limit)
    )
    rows = []
    for deployment in deployments:
        where = environments[deployment.application_environment_id]
        if deployment.error:
            status = "failed"
        elif deployment.complete:
            status = "succeeded"
        else:
            status = "running"
        metadata = cast("dict[str, object]", deployment.deploy_metadata or {})
        rows.append(
            DeployRow(
                id=deployment.id,
                status=status,
                # sqlalchemy_utils' Timestamp declares `created` as a Column.
                created=cast("datetime.datetime", deployment.created),
                automatic=bool(metadata.get("auto_deploy")),
                url=url_for(
                    "user.deployment_detail",
                    org_slug=where.org.slug,
                    project_slug=where.project.slug,
                    app_slug=where.app.slug,
                    deployment_id=deployment.id,
                ),
                org=where.org,
                project=where.project,
                app=where.app,
                environment=where.environment,
            )
        )
    return rows


def dashboard_context(user: User, show_all: bool) -> dict[str, object]:
    """Template variables for the dashboard.

    ``show_all`` (platform admins only) includes organizations the user is not
    a member of.
    """
    memberships = _load_memberships(user)
    orgs = _load_organizations(None if show_all else list(memberships))
    app_environment_ids = [
        ae.id
        for org in orgs
        for project in org.active_projects
        for app in project.active_applications
        for ae in _active_environments(app)
    ]
    statuses = EnvironmentStatuses.load(app_environment_ids)
    org_rows = _build_org_rows(
        orgs, memberships, _count_members([o.id for o in orgs]), statuses
    )
    environments = _environment_index(org_rows)

    app_items = [
        AppItem(org, project, app)
        for org in org_rows
        for project in org.projects
        for app in project.apps
    ]
    app_items.sort(key=lambda i: (STATUS_ORDER[i.status], i.org.slug, i.app.slug))
    failing = [item for item in environments.values() if item.status in FAILED]
    failing.sort(key=lambda i: (i.org.slug, i.project.slug, i.app.slug))
    in_progress = [i for i in environments.values() if i.status in IN_PROGRESS]
    in_progress.sort(key=lambda i: STATUS_ORDER[i.status])

    today = datetime.datetime.now(datetime.UTC).replace(
        tzinfo=None, hour=0, minute=0, second=0, microsecond=0
    )
    first_day = today - datetime.timedelta(days=CHART_DAYS - 1)
    daily = _daily_deploys(app_environment_ids, first_day)
    chart = [
        DayActivity(day, *daily.get(day, (0, 0, 0)))
        for day in (first_day + datetime.timedelta(days=n) for n in range(CHART_DAYS))
    ]
    deploys = _newest_deploys(environments, DEPLOY_LIST_LIMIT)

    status_totals = _count_statuses([item.app for item in app_items])
    platform_org_count = (
        db.session.scalar(
            select(func.count(Organization.id)).where(Organization.deleted_at.is_(None))
        )
        if user.admin
        else 0
    )
    # Changes whenever a status or the newest deploy changes. The page polls
    # while work is in progress and only re-renders when this differs.
    status_signature = hashlib.sha1(
        "|".join(
            f"{env_id}:{item.status}"
            for env_id, item in sorted(environments.items(), key=lambda kv: str(kv[0]))
        ).encode()
        + (str(deploys[0].id) if deploys else "").encode(),
        usedforsecurity=False,
    ).hexdigest()

    return {
        "org_rows": org_rows,
        "org_count": len(org_rows),
        "member_org_count": len(memberships),
        "other_org_count": sum(1 for r in org_rows if not r.is_member),
        "platform_org_count": platform_org_count,
        "project_count": sum(len(r.projects) for r in org_rows),
        "app_count": len(app_items),
        "status_totals": status_totals,
        "failing_app_count": sum(status_totals.get(s, 0) for s in FAILED),
        "app_items": app_items,
        "failing": failing,
        "in_progress": in_progress,
        "completed_deploys": statuses.completed_deploys,
        "chart": chart,
        "chart_peak": max((d.total for d in chart), default=0),
        "chart_succeeded": sum(d.succeeded for d in chart),
        "chart_failed": sum(d.failed for d in chart),
        "chart_started": sum(d.total for d in chart),
        "deploys": deploys,
        "recent_deploys": deploys[:RECENT_DEPLOYS_SHOWN],
        "status_signature": status_signature,
    }
