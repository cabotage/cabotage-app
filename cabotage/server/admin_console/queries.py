"""Bounded inventory queries for the admin console.

Every list is paginated or limited. Per-row aggregates are fetched with one
batched query per page (never per row), and nothing here talks to Kubernetes.
Soft-deleted rows are only returned when the caller asks for them, and every
row carries its ``deleted_at`` so templates can mark it.
"""

from __future__ import annotations

import datetime
import math
import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import cast as type_cast

from sqlalchemy import (
    Row,
    Select,
    SQLColumnExpression,
    cast,
    exists,
    func,
    or_,
    select,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Query, contains_eager, joinedload
from sqlalchemy.sql.elements import ColumnElement

from cabotage.server import db
from cabotage.server.models.audit import AuditLog
from cabotage.server.models.auth import (
    GitHubIdentity,
    Organization,
    OrganizationRequest,
    User,
    WebAuthn,
)
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Deployment,
    Environment,
    Project,
    Release,
)

PAGE_SIZE = 50
MAX_PAGE = 2000
MAX_QUERY_LENGTH = 100
SEARCH_GROUP_LIMIT = 8
STATES = ("active", "deleted", "all")
USER_FILTERS = ("all", "admins", "inactive", "no-mfa")

_DELETED_SUFFIX = re.compile(r"--deleted-[0-9a-f]{12}$")


def display_slug(slug: str | None) -> str:
    """Strip the uniqueness suffix soft-delete appends to slugs."""
    if not slug:
        return ""
    return _DELETED_SUFFIX.sub("", slug)


def _contains(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _as_uuid(term: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(term)
    except ValueError:
        return None


def _state_filter[T](
    query: Query[T], column: SQLColumnExpression[datetime.datetime | None], state: str
) -> Query[T]:
    if state == "active":
        return query.filter(column.is_(None))
    if state == "deleted":
        return query.filter(column.is_not(None))
    return query


@dataclass
class Page[T]:
    items: list[T]
    page: int
    total: int
    per_page: int = PAGE_SIZE

    @property
    def pages(self) -> int:
        return max(1, math.ceil(self.total / self.per_page))

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def first_index(self) -> int:
        return 0 if not self.items else (self.page - 1) * self.per_page + 1

    @property
    def last_index(self) -> int:
        return (self.page - 1) * self.per_page + len(self.items)


def _paginate[T](
    query: Query[T], page: int, order_by: Sequence[SQLColumnExpression[object]]
) -> tuple[list[T], int]:
    total = query.order_by(None).count()
    items = (
        query.order_by(*order_by).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    )
    return items, total


# ── Deployment state ────────────────────────────────────────────────────────


@dataclass
class DeploymentRow:
    id: uuid.UUID
    application_id: uuid.UUID
    application_environment_id: uuid.UUID
    created: datetime.datetime | None
    complete: bool
    error: bool
    version: int | None
    environment_name: str | None
    environment_slug: str | None

    @property
    def status(self) -> str:
        if self.error:
            return "failed"
        if self.complete:
            return "deployed"
        return "deploying"


def _deployment_columns() -> list[SQLColumnExpression[object]]:
    return [
        Deployment.id,
        Deployment.application_id,
        Deployment.application_environment_id,
        Deployment.created,
        Deployment.complete,
        Deployment.error,
        Release.version,
        Environment.name.label("environment_name"),
        Environment.slug.label("environment_slug"),
    ]


def _deployment_base(
    columns: list[SQLColumnExpression[object]],
) -> Select[tuple[object, ...]]:
    return (
        select(*columns)
        .select_from(Deployment)
        .join(
            ApplicationEnvironment,
            ApplicationEnvironment.id == Deployment.application_environment_id,
        )
        .join(Environment, Environment.id == ApplicationEnvironment.environment_id)
        .outerjoin(
            Release,
            Release.id
            == cast(Deployment.release["id"].astext, postgresql.UUID(as_uuid=True)),
        )
    )


def _deployment_row(row: Row[tuple[object, ...]]) -> DeploymentRow:
    data = row._mapping
    return DeploymentRow(
        id=type_cast(uuid.UUID, data["id"]),
        application_id=type_cast(uuid.UUID, data["application_id"]),
        application_environment_id=type_cast(
            uuid.UUID, data["application_environment_id"]
        ),
        created=type_cast(datetime.datetime | None, data["created"]),
        complete=type_cast(bool, data["complete"]),
        error=type_cast(bool, data["error"]),
        version=type_cast(int | None, data["version"]),
        environment_name=type_cast(str | None, data["environment_name"]),
        environment_slug=type_cast(str | None, data["environment_slug"]),
    )


def latest_deployment_by_application(
    application_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, DeploymentRow]:
    """Newest deployment per application across its live environments."""
    ids = list(application_ids)
    if not ids:
        return {}
    statement = (
        _deployment_base(_deployment_columns())
        .where(
            Deployment.application_id.in_(ids),
            ApplicationEnvironment.deleted_at.is_(None),
        )
        .distinct(Deployment.application_id)
        .order_by(
            Deployment.application_id, Deployment.created.desc(), Deployment.id.desc()
        )
    )
    return {
        type_cast(uuid.UUID, row._mapping["application_id"]): _deployment_row(row)
        for row in db.session.execute(statement)
    }


def latest_deployment_by_app_env(
    app_env_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, DeploymentRow]:
    ids = list(app_env_ids)
    if not ids:
        return {}
    statement = (
        _deployment_base(_deployment_columns())
        .where(Deployment.application_environment_id.in_(ids))
        .distinct(Deployment.application_environment_id)
        .order_by(
            Deployment.application_environment_id,
            Deployment.created.desc(),
            Deployment.id.desc(),
        )
    )
    return {
        type_cast(
            uuid.UUID, row._mapping["application_environment_id"]
        ): _deployment_row(row)
        for row in db.session.execute(statement)
    }


# ── Overview ────────────────────────────────────────────────────────────────


@dataclass
class Overview:
    organizations: int
    projects: int
    applications: int
    users: int
    pending_requests: list[OrganizationRequest]
    pending_request_total: int
    usable_admins: int
    admins_without_passkeys: list[User] = field(default_factory=list)
    admins_without_passkeys_total: int = 0


def overview() -> Overview:
    missing_passkeys = type_cast(Query[User], User.query).filter(
        User.admin.is_(True),
        User.__table__.c.active.is_(True),
        ~exists().where(WebAuthn.user_id == User.id),
    )
    counts = db.session.execute(
        select(
            select(func.count(Organization.id))
            .where(Organization.deleted_at.is_(None))
            .scalar_subquery(),
            select(func.count(Project.id))
            .where(Project.deleted_at.is_(None))
            .scalar_subquery(),
            select(func.count(Application.id))
            .where(Application.deleted_at.is_(None))
            .scalar_subquery(),
            select(func.count(User.id))
            .where(User.__table__.c.active.is_(True))
            .scalar_subquery(),
            select(func.count(OrganizationRequest.id))
            .where(OrganizationRequest.status == OrganizationRequest.STATUS_PENDING)
            .scalar_subquery(),
        )
    ).one()
    pending = (
        type_cast(Query[OrganizationRequest], OrganizationRequest.query)
        .options(joinedload(OrganizationRequest.requester))
        .filter(OrganizationRequest.status == OrganizationRequest.STATUS_PENDING)
        .order_by(OrganizationRequest.created_at.asc(), OrganizationRequest.id)
        .limit(5)
        .all()
    )
    return Overview(
        organizations=counts[0],
        projects=counts[1],
        applications=counts[2],
        users=counts[3],
        pending_requests=pending,
        pending_request_total=counts[4],
        usable_admins=db.session.scalar(
            select(func.count(User.id)).where(
                User.admin.is_(True),
                User.__table__.c.active.is_(True),
                exists().where(WebAuthn.user_id == User.id),
            )
        )
        or 0,
        admins_without_passkeys=missing_passkeys.order_by(
            func.lower(User.username), User.id
        )
        .limit(5)
        .all(),
        admins_without_passkeys_total=missing_passkeys.count(),
    )


def pending_request_count() -> int:
    return db.session.execute(
        select(func.count(OrganizationRequest.id)).where(
            OrganizationRequest.status == OrganizationRequest.STATUS_PENDING
        )
    ).scalar_one()


# ── Organizations ───────────────────────────────────────────────────────────


@dataclass
class OrganizationStats:
    projects: int = 0
    applications: int = 0
    members: int = 0
    last_deploy: datetime.datetime | None = None


def organization_stats(
    organization_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, OrganizationStats]:
    stats = {org_id: OrganizationStats() for org_id in organization_ids}
    if not organization_ids:
        return stats
    for org_id, count in db.session.execute(
        select(Project.organization_id, func.count(Project.id))
        .where(
            Project.organization_id.in_(organization_ids),
            Project.deleted_at.is_(None),
        )
        .group_by(Project.organization_id)
    ):
        stats[org_id].projects = count
    for org_id, count in db.session.execute(
        select(Project.organization_id, func.count(Application.id))
        .join(Application, Application.project_id == Project.id)
        .where(
            Project.organization_id.in_(organization_ids),
            Project.deleted_at.is_(None),
            Application.deleted_at.is_(None),
        )
        .group_by(Project.organization_id)
    ):
        stats[org_id].applications = count
    for org_id, count in db.session.execute(
        select(OrganizationMember.organization_id, func.count())
        .where(OrganizationMember.organization_id.in_(organization_ids))
        .group_by(OrganizationMember.organization_id)
    ):
        stats[org_id].members = count
    for org_id, last_deploy in db.session.execute(
        select(Project.organization_id, func.max(Deployment.created))
        .join(Application, Application.project_id == Project.id)
        .join(Deployment, Deployment.application_id == Application.id)
        .where(Project.organization_id.in_(organization_ids))
        .group_by(Project.organization_id)
    ):
        stats[org_id].last_deploy = last_deploy
    return stats


def _organization_search(query: Query[Organization], term: str) -> Query[Organization]:
    pattern = _contains(term)
    clauses: list[ColumnElement[bool]] = [
        Organization.name.ilike(pattern, escape="\\"),
        Organization.slug.ilike(pattern, escape="\\"),
        Organization.k8s_identifier.ilike(pattern, escape="\\"),
    ]
    if (as_id := _as_uuid(term)) is not None:
        clauses.append(Organization.id == as_id)
    return query.filter(or_(*clauses))


def organizations_page(
    term: str, state: str, page: int
) -> Page[tuple[Organization, OrganizationStats]]:
    query = _state_filter(
        type_cast(Query[Organization], Organization.query),
        Organization.deleted_at,
        state,
    )
    if term:
        query = _organization_search(query, term)
    items, total = _paginate(
        query,
        page,
        (
            Organization.deleted_at.is_not(None),
            func.lower(Organization.name),
            Organization.id,
        ),
    )
    stats = organization_stats([org.id for org in items])
    return Page(items=[(org, stats[org.id]) for org in items], page=page, total=total)


@dataclass
class OrganizationDetail:
    organization: Organization
    projects: list[Project]
    project_stats: dict[uuid.UUID, ProjectStats]
    members: list[OrganizationMember]
    requests: list[OrganizationRequest]


def organization_detail(organization_id: uuid.UUID) -> OrganizationDetail | None:
    organization = db.session.get(Organization, organization_id)
    if organization is None:
        return None
    projects = (
        type_cast(Query[Project], Project.query)
        .filter(Project.organization_id == organization.id)
        .order_by(Project.deleted_at.is_not(None), func.lower(Project.name), Project.id)
        .limit(200)
        .all()
    )
    members = (
        type_cast(Query[OrganizationMember], OrganizationMember.query)
        .join(User, User.id == OrganizationMember.user_id)
        .options(contains_eager(OrganizationMember.user))
        .filter(OrganizationMember.organization_id == organization.id)
        .order_by(OrganizationMember.admin.desc(), func.lower(User.username), User.id)
        .limit(200)
        .all()
    )
    requests = (
        type_cast(Query[OrganizationRequest], OrganizationRequest.query)
        .options(
            joinedload(OrganizationRequest.requester),
            joinedload(OrganizationRequest.reviewer),
        )
        .filter(OrganizationRequest.organization_id == organization.id)
        .order_by(OrganizationRequest.created_at.desc(), OrganizationRequest.id)
        .limit(5)
        .all()
    )
    return OrganizationDetail(
        organization=organization,
        projects=projects,
        project_stats=project_stats([p.id for p in projects]),
        members=members,
        requests=requests,
    )


# ── Projects ────────────────────────────────────────────────────────────────


@dataclass
class ProjectStats:
    applications: int = 0
    environments: int = 0
    last_deploy: datetime.datetime | None = None


def project_stats(project_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, ProjectStats]:
    stats = {project_id: ProjectStats() for project_id in project_ids}
    if not project_ids:
        return stats
    for project_id, count in db.session.execute(
        select(Application.project_id, func.count(Application.id))
        .where(
            Application.project_id.in_(project_ids),
            Application.deleted_at.is_(None),
        )
        .group_by(Application.project_id)
    ):
        stats[project_id].applications = count
    for project_id, count in db.session.execute(
        select(Environment.project_id, func.count(Environment.id))
        .where(
            Environment.project_id.in_(project_ids),
            Environment.deleted_at.is_(None),
        )
        .group_by(Environment.project_id)
    ):
        stats[project_id].environments = count
    for project_id, last_deploy in db.session.execute(
        select(Application.project_id, func.max(Deployment.created))
        .join(Deployment, Deployment.application_id == Application.id)
        .where(Application.project_id.in_(project_ids))
        .group_by(Application.project_id)
    ):
        stats[project_id].last_deploy = last_deploy
    return stats


def _project_query() -> Query[Project]:
    return (
        type_cast(Query[Project], Project.query)
        .join(Organization, Organization.id == Project.organization_id)
        .options(contains_eager(Project.organization))
    )


def _project_search(query: Query[Project], term: str) -> Query[Project]:
    pattern = _contains(term)
    clauses: list[ColumnElement[bool]] = [
        Project.name.ilike(pattern, escape="\\"),
        Project.slug.ilike(pattern, escape="\\"),
        Organization.slug.ilike(pattern, escape="\\"),
        Organization.name.ilike(pattern, escape="\\"),
    ]
    if (as_id := _as_uuid(term)) is not None:
        clauses.append(Project.id == as_id)
    return query.filter(or_(*clauses))


def projects_page(
    term: str, state: str, page: int
) -> Page[tuple[Project, ProjectStats]]:
    query = _state_filter(_project_query(), Project.deleted_at, state)
    if term:
        query = _project_search(query, term)
    items, total = _paginate(
        query,
        page,
        (
            Project.deleted_at.is_not(None),
            func.lower(Organization.slug),
            func.lower(Project.slug),
            Project.id,
        ),
    )
    stats = project_stats([project.id for project in items])
    return Page(
        items=[(project, stats[project.id]) for project in items],
        page=page,
        total=total,
    )


@dataclass
class ProjectDetail:
    project: Project
    applications: list[Application]
    latest: dict[uuid.UUID, DeploymentRow]
    environments: list[Environment]


def project_detail(project_id: uuid.UUID) -> ProjectDetail | None:
    project = (
        type_cast(Query[Project], Project.query)
        .options(joinedload(Project.organization))
        .filter(Project.id == project_id)
        .one_or_none()
    )
    if project is None:
        return None
    applications = (
        type_cast(Query[Application], Application.query)
        .filter(Application.project_id == project.id)
        .order_by(
            Application.deleted_at.is_not(None),
            func.lower(Application.name),
            Application.id,
        )
        .limit(200)
        .all()
    )
    environments = (
        type_cast(Query[Environment], Environment.query)
        .filter(Environment.project_id == project.id)
        .order_by(
            Environment.deleted_at.is_not(None),
            Environment.sort_order,
            func.lower(Environment.name),
            Environment.id,
        )
        .limit(100)
        .all()
    )
    return ProjectDetail(
        project=project,
        applications=applications,
        latest=latest_deployment_by_application(
            [app.id for app in applications if app.deleted_at is None]
        ),
        environments=environments,
    )


# ── Applications ────────────────────────────────────────────────────────────


def _application_query() -> Query[Application]:
    return (
        type_cast(Query[Application], Application.query)
        .join(Project, Project.id == Application.project_id)
        .join(Organization, Organization.id == Project.organization_id)
        .options(
            contains_eager(Application.project).contains_eager(Project.organization)
        )
    )


def _application_search(query: Query[Application], term: str) -> Query[Application]:
    pattern = _contains(term)
    clauses: list[ColumnElement[bool]] = [
        Application.name.ilike(pattern, escape="\\"),
        Application.slug.ilike(pattern, escape="\\"),
        Application.github_repository.ilike(pattern, escape="\\"),
        Project.slug.ilike(pattern, escape="\\"),
        Organization.slug.ilike(pattern, escape="\\"),
    ]
    if (as_id := _as_uuid(term)) is not None:
        clauses.append(Application.id == as_id)
    return query.filter(or_(*clauses))


def applications_page(
    term: str, state: str, page: int
) -> Page[tuple[Application, DeploymentRow | None]]:
    query = _state_filter(_application_query(), Application.deleted_at, state)
    if term:
        query = _application_search(query, term)
    items, total = _paginate(
        query,
        page,
        (
            Application.deleted_at.is_not(None),
            func.lower(Organization.slug),
            func.lower(Project.slug),
            func.lower(Application.slug),
            Application.id,
        ),
    )
    latest = latest_deployment_by_application(
        [app.id for app in items if app.deleted_at is None]
    )
    return Page(
        items=[(app, latest.get(app.id)) for app in items], page=page, total=total
    )


@dataclass
class AppEnvRow:
    app_env: ApplicationEnvironment
    environment: Environment
    latest: DeploymentRow | None


@dataclass
class ApplicationDetail:
    application: Application
    environments: list[AppEnvRow]


def application_detail(application_id: uuid.UUID) -> ApplicationDetail | None:
    application = (
        type_cast(Query[Application], Application.query)
        .options(joinedload(Application.project).joinedload(Project.organization))
        .filter(Application.id == application_id)
        .one_or_none()
    )
    if application is None:
        return None
    app_envs = (
        type_cast(Query[ApplicationEnvironment], ApplicationEnvironment.query)
        .join(Environment, Environment.id == ApplicationEnvironment.environment_id)
        .options(contains_eager(ApplicationEnvironment.environment))
        .filter(ApplicationEnvironment.application_id == application.id)
        .order_by(
            ApplicationEnvironment.deleted_at.is_not(None),
            Environment.sort_order,
            func.lower(Environment.name),
            ApplicationEnvironment.id,
        )
        .limit(100)
        .all()
    )
    latest = latest_deployment_by_app_env([ae.id for ae in app_envs])
    return ApplicationDetail(
        application=application,
        environments=[
            AppEnvRow(app_env=ae, environment=ae.environment, latest=latest.get(ae.id))
            for ae in app_envs
        ],
    )


# ── Users ───────────────────────────────────────────────────────────────────


def usable_admin_ids() -> set[uuid.UUID]:
    """Active global admins holding at least one passkey credential."""
    return set(
        db.session.scalars(
            select(User.id).where(
                User.admin.is_(True),
                User.__table__.c.active.is_(True),
                exists().where(WebAuthn.user_id == User.id),
            )
        )
    )


@dataclass
class UserStats:
    passkeys: int = 0
    organizations: int = 0


def user_stats(user_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, UserStats]:
    stats = {user_id: UserStats() for user_id in user_ids}
    if not user_ids:
        return stats
    for user_id, count in db.session.execute(
        select(WebAuthn.user_id, func.count(WebAuthn.id))
        .where(WebAuthn.user_id.in_(user_ids))
        .group_by(WebAuthn.user_id)
    ):
        stats[user_id].passkeys = count
    for user_id, count in db.session.execute(
        select(OrganizationMember.user_id, func.count())
        .join(Organization, Organization.id == OrganizationMember.organization_id)
        .where(
            OrganizationMember.user_id.in_(user_ids),
            Organization.deleted_at.is_(None),
        )
        .group_by(OrganizationMember.user_id)
    ):
        stats[user_id].organizations = count
    return stats


def _user_query() -> Query[User]:
    # github_identity is a backref declared on GitHubIdentity.
    github_identity = getattr(User, "github_identity")
    return (
        type_cast(Query[User], User.query)
        .outerjoin(GitHubIdentity, GitHubIdentity.user_id == User.id)
        .options(contains_eager(github_identity))
    )


def _user_search(query: Query[User], term: str) -> Query[User]:
    pattern = _contains(term)
    clauses: list[ColumnElement[bool]] = [
        type_cast(ColumnElement[str], User.email).ilike(pattern, escape="\\"),
        User.username.ilike(pattern, escape="\\"),
        GitHubIdentity.github_username.ilike(pattern, escape="\\"),
    ]
    if (as_id := _as_uuid(term)) is not None:
        clauses.append(User.id == as_id)
    return query.filter(or_(*clauses))


def _user_filter(query: Query[User], user_filter: str) -> Query[User]:
    if user_filter == "admins":
        return query.filter(User.admin.is_(True))
    if user_filter == "inactive":
        return query.filter(User.__table__.c.active.is_(False))
    if user_filter == "no-mfa":
        return query.filter(
            or_(
                type_cast(ColumnElement[str | None], User.tf_primary_method).is_(None),
                type_cast(ColumnElement[str | None], User.tf_primary_method)
                != "authenticator",
            ),
            ~exists().where(WebAuthn.user_id == User.id),
        )
    return query


def users_page(term: str, user_filter: str, page: int) -> Page[tuple[User, UserStats]]:
    query = _user_filter(_user_query(), user_filter)
    if term:
        query = _user_search(query, term)
    items, total = _paginate(
        query,
        page,
        (
            User.__table__.c.active.is_(False),
            User.admin.is_(False),
            func.lower(User.username),
            User.id,
        ),
    )
    stats = user_stats([user.id for user in items])
    return Page(
        items=[(user, stats[user.id]) for user in items], page=page, total=total
    )


@dataclass
class UserDetail:
    user: User
    passkeys: list[WebAuthn]
    memberships: list[OrganizationMember]
    events: list[AuditLog]
    requests: list[OrganizationRequest]
    usable_admins: set[uuid.UUID] = field(default_factory=set)

    @property
    def has_totp(self) -> bool:
        return (
            type_cast(str | None, type_cast(object, self.user.tf_primary_method))
            == "authenticator"
        )

    @property
    def has_mfa(self) -> bool:
        return self.has_totp or bool(self.passkeys)

    @property
    def recovery_codes(self) -> int:
        return len(
            type_cast(list[str] | None, type_cast(object, self.user.mf_recovery_codes))
            or []
        )

    @property
    def is_usable_admin(self) -> bool:
        return self.user.id in self.usable_admins


def user_detail(user_id: uuid.UUID) -> UserDetail | None:
    user = _user_query().filter(User.id == user_id).one_or_none()
    if user is None:
        return None
    passkeys = list(
        db.session.scalars(
            select(WebAuthn)
            .where(WebAuthn.user_id == user.id)
            .order_by(WebAuthn.create_datetime)
        )
    )
    memberships = (
        type_cast(Query[OrganizationMember], OrganizationMember.query)
        .join(Organization, Organization.id == OrganizationMember.organization_id)
        .options(contains_eager(OrganizationMember.organization))
        .filter(OrganizationMember.user_id == user.id)
        .order_by(Organization.deleted_at.is_not(None), func.lower(Organization.slug))
        .limit(200)
        .all()
    )
    events = (
        type_cast(Query[AuditLog], AuditLog.query)
        .filter(AuditLog.object_type == "User", AuditLog.object_id == user.id)
        .order_by(AuditLog.id.desc())
        .limit(15)
        .all()
    )
    requests = (
        type_cast(Query[OrganizationRequest], OrganizationRequest.query)
        .filter(OrganizationRequest.requester_user_id == user.id)
        .order_by(OrganizationRequest.created_at.desc())
        .limit(5)
        .all()
    )
    return UserDetail(
        user=user,
        passkeys=passkeys,
        memberships=memberships,
        events=events,
        requests=requests,
        usable_admins=usable_admin_ids(),
    )


# ── Requests ────────────────────────────────────────────────────────────────


def organization_requests(status: str) -> list[OrganizationRequest]:
    query = type_cast(Query[OrganizationRequest], OrganizationRequest.query).options(
        joinedload(OrganizationRequest.requester),
        joinedload(OrganizationRequest.reviewer),
        joinedload(OrganizationRequest.organization),
    )
    if status == "pending":
        query = query.filter(
            OrganizationRequest.status == OrganizationRequest.STATUS_PENDING
        )
        query = query.order_by(OrganizationRequest.created_at.asc())
    else:
        query = query.order_by(OrganizationRequest.created_at.desc())
    return query.limit(200).all()


# ── Search ──────────────────────────────────────────────────────────────────


@dataclass
class SearchResults:
    organizations: list[Organization]
    projects: list[Project]
    applications: list[Application]
    users: list[User]

    @property
    def empty(self) -> bool:
        return not (
            self.organizations or self.projects or self.applications or self.users
        )


def search(term: str) -> SearchResults:
    limit = SEARCH_GROUP_LIMIT
    return SearchResults(
        organizations=_organization_search(
            type_cast(Query[Organization], Organization.query), term
        )
        .order_by(Organization.deleted_at.is_not(None), func.lower(Organization.name))
        .limit(limit)
        .all(),
        projects=_project_search(_project_query(), term)
        .order_by(Project.deleted_at.is_not(None), func.lower(Project.slug))
        .limit(limit)
        .all(),
        applications=_application_search(_application_query(), term)
        .order_by(Application.deleted_at.is_not(None), func.lower(Application.slug))
        .limit(limit)
        .all(),
        users=_user_search(_user_query(), term)
        .order_by(
            User.__table__.c.active.is_(False),
            func.lower(User.username),
        )
        .limit(limit)
        .all(),
    )
