"""Seed script for local development.

Runs create_admin first (user + org + bare project), then layers on
realistic data so the UI is fully populated:
  - Extra dev user
  - Multiple projects, apps, environments
  - Configurations, images, releases, deployments, ingress rules
  - Job process history
"""

import datetime
import os
import sys
from typing import TypedDict

from sqlalchemy import text

from sqlalchemy import update

from cabotage.server import create_app, db
from cabotage.server.models import Organization, User
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Configuration,
    Deployment,
    Environment,
    Image,
    Ingress,
    IngressHost,
    IngressPath,
    JobLog,
    Project,
    Release,
)
from cabotage.server.models.resources import PostgresResource, RedisResource


def _make_config(app, app_env, name, value, *, secret=False):
    """Create a Configuration row with a synthetic key_slug."""
    cfg = Configuration(
        application_id=app.id,
        application_environment_id=app_env.id,
        name=name,
        value=value,
        secret=secret,
        key_slug=f"consul:cabotage/seed/{app.slug}/{name}",
    )
    db.session.add(cfg)
    return cfg


def _make_image(app, app_env, *, build_ref="main", processes=None, built=True):
    """Create an Image row.  version is auto-set by the before_insert listener."""
    repo = Image._build_repository_name(
        app.project.organization.k8s_identifier,
        app.project.k8s_identifier,
        app.k8s_identifier,
        app_env.k8s_identifier,
    )
    img = Image(
        application_id=app.id,
        application_environment_id=app_env.id,
        _repository_name=repo,
        build_ref=build_ref,
        built=built,
        processes=processes or {},
        image_metadata={"sha": "abc123deadbeef"},
    )
    db.session.add(img)
    db.session.flush()
    return img


def _make_release(app, app_env, image, configs, *, built=True):
    """Create a Release with image/configuration JSONB snapshots."""
    repo = Image._build_repository_name(
        app.project.organization.k8s_identifier,
        app.project.k8s_identifier,
        app.k8s_identifier,
        app_env.k8s_identifier,
    )
    release = Release(
        application_id=app.id,
        application_environment_id=app_env.id,
        _repository_name=repo,
        image=image.asdict,
        configuration={c.name: c.asdict for c in configs},
        image_changes={"added": [], "removed": [], "changed": [], "unchanged": []},
        configuration_changes={
            "added": [],
            "removed": [],
            "changed": [],
            "unchanged": [],
        },
        platform=app.platform,
        built=built,
    )
    db.session.add(release)
    db.session.flush()
    return release


def _make_deployment(app, app_env, release, *, complete=True):
    """Create a Deployment row from a Release snapshot."""
    dep = Deployment(
        application_id=app.id,
        application_environment_id=app_env.id,
        release=release.asdict,
        complete=complete,
    )
    db.session.add(dep)
    db.session.flush()
    return dep


def _make_ingress(app_env, hostname, path="/", target_process="web"):
    """Create Ingress + IngressHost + IngressPath."""
    ingress = Ingress(
        application_environment_id=app_env.id,
        name="default",
    )
    db.session.add(ingress)
    db.session.flush()

    host = IngressHost(
        ingress_id=ingress.id,
        hostname=hostname,
    )
    db.session.add(host)

    ing_path = IngressPath(
        ingress_id=ingress.id,
        path=path,
        target_process_name=target_process,
    )
    db.session.add(ing_path)
    db.session.flush()
    return ingress


def _enroll_app(app, env):
    """Create an ApplicationEnvironment enrollment."""
    app_env = ApplicationEnvironment(
        application_id=app.id,
        environment_id=env.id,
        k8s_identifier=env.k8s_identifier,
    )
    db.session.add(app_env)
    db.session.flush()
    db.session.refresh(app_env)
    return app_env


# ---------------------------------------------------------------------------
# Web app process definitions
# ---------------------------------------------------------------------------
class ProcessSpec(TypedDict):
    cmd: str
    env: list[tuple[str, str]]


WEB_PROCESSES = {
    "web": {"cmd": "gunicorn app:application --bind 0.0.0.0:8000", "env": []},
    "worker": {"cmd": "celery -A app.celery worker --loglevel=info", "env": []},
    "release": {"cmd": "flask db upgrade", "env": []},
    "job-cleanup": {
        "cmd": "python manage.py cleanup_stale_sessions",
        "env": [("SCHEDULE", "0 */6 * * *")],
    },
    "job-reports": {
        "cmd": "python manage.py generate_daily_report",
        "env": [("SCHEDULE", "30 2 * * *")],
    },
}

WORKER_PROCESSES = {
    "worker": {
        "cmd": "celery -A app.celery worker --loglevel=info --concurrency=4",
        "env": [],
    },
}

DOCS_PROCESSES: dict[str, ProcessSpec] = {
    "web": {"cmd": "nginx -g 'daemon off;'", "env": []},
}

# (age_days, image_secs, release_secs, deploy_secs, deploy_failed)
WEB_PROD_PIPELINE_RUNS = [
    (85, 72, 14, 32, False),
    (75, 68, 12, 28, False),
    (65, 58, 11, 25, True),
    (55, 61, 13, 30, False),
    (45, 55, 10, 22, False),
    (38, 50, 9, 27, False),
    (30, 48, 11, 24, False),
    (22, 45, 8, 20, False),
    (18, 52, 12, 26, True),
    (14, 42, 9, 21, False),
    (10, 40, 8, 19, False),
    (7, 44, 10, 23, False),
    (4, 39, 7, 18, False),
    (2, 41, 9, 20, False),
    (0.25, 38, 8, 17, False),
]


# ---------------------------------------------------------------------------
# PyPI-scale seed scenario (opt-in via SEED_SCALE=pypi)
# ---------------------------------------------------------------------------
WAREHOUSE_PROCESSES: dict[str, ProcessSpec] = {
    "web": {
        "cmd": "gunicorn warehouse.wsgi:application --bind 0.0.0.0:8000",
        "env": [],
    },
    "web-api": {
        "cmd": "gunicorn warehouse.api.wsgi:application --bind 0.0.0.0:8001",
        "env": [],
    },
    "web-uploads": {
        "cmd": "gunicorn warehouse.uploads.wsgi:application --bind 0.0.0.0:8002",
        "env": [],
    },
    "worker": {"cmd": "celery -A warehouse worker --loglevel=info", "env": []},
    "worker-beat": {"cmd": "celery -A warehouse beat --loglevel=info", "env": []},
    "worker-traced": {
        "cmd": "celery -A warehouse worker --loglevel=info -Q traced",
        "env": [],
    },
    "release": {"cmd": "bin/release", "env": []},
}


def _get_or_create_org(
    name: str, slug: str, admin: User, dev_user: User
) -> Organization:
    """Get or create an Organization, ensuring admin/dev are members."""
    org: Organization | None = Organization.query.filter_by(slug=slug).first()
    if org is None:
        org = Organization(name=name, slug=slug)
        db.session.add(org)
        db.session.flush()
        db.session.refresh(org)
    _ensure_org_member(org, admin, admin=True)
    _ensure_org_member(org, dev_user, admin=False)
    return org


def _ensure_org_member(org: Organization, user: User, *, admin: bool) -> None:
    """Idempotently add a user to an organization, same pattern as the
    Acme Corp org.add_user() calls in seed()."""
    existing: OrganizationMember | None = OrganizationMember.query.filter_by(
        organization_id=org.id, user_id=user.id
    ).first()
    if existing is None:
        org.add_user(user, admin=admin)
        db.session.flush()


def _get_or_create_project(
    org: Organization,
    name: str,
    slug: str,
    *,
    environments_enabled: bool,
    deleted: bool = False,
) -> Project:
    proj: Project | None = Project.query.filter_by(
        slug=slug, organization_id=org.id
    ).first()
    if proj is None:
        proj = Project(
            name=name,
            slug=slug,
            organization_id=org.id,
            environments_enabled=environments_enabled,
        )
        db.session.add(proj)
        db.session.flush()
        db.session.refresh(proj)
    if deleted and proj.deleted_at is None:
        proj.deleted_at = datetime.datetime.now(datetime.UTC)
    return proj


def _get_or_create_environment(
    project: Project,
    name: str,
    slug: str,
    *,
    is_default: bool = False,
    sort_order: int = 100,
) -> Environment:
    env: Environment | None = Environment.query.filter_by(
        slug=slug, project_id=project.id
    ).first()
    if env is None:
        env = Environment(
            name=name,
            slug=slug,
            project_id=project.id,
            is_default=is_default,
            sort_order=sort_order,
        )
        db.session.add(env)
        db.session.flush()
        db.session.refresh(env)
    return env


def _get_or_create_default_environment(project: Project) -> Environment:
    """Default environment for a project with environments_enabled=False,
    same pattern as env_docs_default in seed()."""
    return _get_or_create_environment(project, "Default", "default", is_default=True)


def _get_or_create_app(
    project: Project, name: str, slug: str, *, deleted: bool = False
) -> Application:
    app: Application | None = Application.query.filter_by(
        slug=slug, project_id=project.id
    ).first()
    if app is None:
        app = Application(name=name, slug=slug, project_id=project.id)
        db.session.add(app)
        db.session.flush()
        db.session.refresh(app)
    if deleted and app.deleted_at is None:
        app.deleted_at = datetime.datetime.now(datetime.UTC)
    return app


def _get_or_create_app_env(
    app: Application, env: Environment, *, legacy: bool = False
) -> ApplicationEnvironment:
    """Enroll `app` in `env`. `legacy=True` mirrors the Docs Site / Site
    pattern used for projects with environments_enabled=False
    (k8s_identifier=None); otherwise delegates to _enroll_app()."""
    app_env: ApplicationEnvironment | None = ApplicationEnvironment.query.filter_by(
        application_id=app.id, environment_id=env.id
    ).first()
    if app_env is not None:
        return app_env
    if legacy:
        app_env = ApplicationEnvironment(
            application_id=app.id,
            environment_id=env.id,
            k8s_identifier=None,
        )
        db.session.add(app_env)
        db.session.flush()
        db.session.refresh(app_env)
        return app_env
    return _enroll_app(app, env)


def _seed_app_history(
    app: Application,
    app_env: ApplicationEnvironment,
    processes: dict[str, ProcessSpec],
    *,
    build_ref: str = "main",
    count: int = 3,
) -> Image:
    """Create `count` Image rows, `count - 1` Release rows (min 1), and a
    completed Deployment, if none exist yet for this enrollment. Returns the
    latest built Image."""
    latest_img: Image
    if app_env.images.count() == 0:
        images = [
            _make_image(app, app_env, build_ref=build_ref, processes=processes)
            for _ in range(count)
        ]
        latest_img = images[-1]
    else:
        found_img: Image | None = (
            app_env.images.filter_by(built=True).order_by(Image.version.desc()).first()
        )
        if found_img is None:
            raise RuntimeError("Existing seed application has no built image")
        latest_img = found_img

    if app_env.releases.count() == 0:
        releases = [
            _make_release(app, app_env, latest_img, [])
            for _ in range(max(1, count - 1))
        ]
        latest_rel = releases[-1]
        _ = _make_deployment(app, app_env, latest_rel, complete=True)

    return latest_img


def _fast_forward_image_release_versions(
    app_env: ApplicationEnvironment, *, image_target: int, release_target: int
) -> None:
    """Make the latest Image/Release `version` for `app_env` read
    `image_target`/`release_target` without materializing thousands of rows.

    Image.version and Release.version are set by a before_insert listener
    that takes max(version) + 1 scoped to (application_id,
    application_environment_id); there is no DB-level uniqueness or strict
    "+1" constraint on that column (only SQLAlchemy-Continuum's separate
    optimistic-lock `version_id` column is constrained). So after creating a
    modest number of real rows via the normal insert path (so the history
    looks like real, individually-versioned deploys), we issue one raw SQL
    UPDATE per table that adds a fixed offset to every row's version for
    this app_env. That preserves per-row ordering and uniqueness while
    raising the ceiling to the target, e.g. making a brand-new local warehouse
    look like a mature project on image #5790 / release #5824.
    """
    latest_img: Image | None = (
        app_env.images.filter_by(built=True).order_by(Image.version.desc()).first()
    )
    latest_rel: Release | None = (
        app_env.releases.filter_by(built=True).order_by(Release.version.desc()).first()
    )
    if latest_img is None or latest_rel is None:
        return

    image_offset = image_target - latest_img.version
    release_offset = release_target - latest_rel.version
    if image_offset:
        _ = db.session.execute(
            text(
                "UPDATE project_app_images SET version = version + :offset "
                "WHERE application_environment_id = :app_env_id"
            ),
            {"offset": image_offset, "app_env_id": str(app_env.id)},
        )
    if release_offset:
        _ = db.session.execute(
            text(
                "UPDATE project_app_releases SET version = version + :offset "
                "WHERE application_environment_id = :app_env_id"
            ),
            {"offset": release_offset, "app_env_id": str(app_env.id)},
        )
    db.session.flush()
    db.session.expire(latest_img)
    db.session.expire(latest_rel)


def _seed_simple_app(
    project: Project,
    env: Environment,
    name: str,
    slug: str,
    *,
    processes: dict[str, ProcessSpec],
    process_counts: dict[str, int],
    process_pod_classes: dict[str, str] | None = None,
    build_ref: str = "main",
    history_count: int = 3,
    deleted: bool = False,
    ingress_hostname: str | None = None,
) -> tuple[Application, ApplicationEnvironment]:
    """Create (or fetch) a single-app enrollment with image/release/
    deployment history, process counts/pod classes, and an optional
    ingress. Mirrors the Acme Corp Web/Worker/Site app wiring in seed()."""
    app = _get_or_create_app(project, name, slug, deleted=deleted)
    app_env = _get_or_create_app_env(app, env, legacy=not project.environments_enabled)
    _ = _seed_app_history(
        app, app_env, processes, build_ref=build_ref, count=history_count
    )
    app_env.process_counts = process_counts
    if process_pod_classes:
        app_env.process_pod_classes = process_pod_classes
    if ingress_hostname and not app_env.ingresses:
        _ = _make_ingress(app_env, ingress_hostname, path="/", target_process="web")
    return app, app_env


def seed_pypi_scale(admin: User, dev_user: User) -> None:
    """Seed a PyPI/PSF-scale scenario: ~9 realistic orgs with a mix of
    project/app shapes (with and without environment namespaces,
    single-app and multi-app projects, 1-6 processes per app, varied pod
    classes and replica counts, Postgres/Redis resources, and a handful of
    soft-deleted apps/projects) so the cost UI has a large, varied dataset
    to visualize. Reuses the same helpers/models and idempotent
    `.filter_by(...).first()` guard pattern as the Acme Corp seed above, so
    re-running this with SEED_SCALE=pypi set is a no-op after the first run.
    """

    # ==================================================================
    # Org: pypi — warehouse (the big one), inspector, linehaul, camo,
    # conveyor (soft-deleted), pypi-docs
    # ==================================================================
    org_pypi = _get_or_create_org("PyPI", "pypi", admin, dev_user)

    proj_warehouse = _get_or_create_project(
        org_pypi, "Warehouse", "warehouse", environments_enabled=True
    )
    env_warehouse_prod = _get_or_create_environment(
        proj_warehouse, "Production", "production", is_default=True, sort_order=100
    )
    env_warehouse_staging = _get_or_create_environment(
        proj_warehouse, "Staging", "staging", sort_order=50
    )
    app_warehouse = _get_or_create_app(proj_warehouse, "Warehouse", "warehouse")

    warehouse_prod = _get_or_create_app_env(app_warehouse, env_warehouse_prod)
    now = datetime.datetime.now(datetime.UTC)
    if warehouse_prod.images.count() == 0:
        # ~25 real "about daily" deploys, then fast-forward the version
        # counter so this looks like a mature, high-throughput project.
        for i in range(25):
            build_ref = f"main@{(now - datetime.timedelta(days=25 - i)):%Y%m%d}"
            img = _make_image(
                app_warehouse,
                warehouse_prod,
                build_ref=build_ref,
                processes=WAREHOUSE_PROCESSES,
            )
            rel = _make_release(app_warehouse, warehouse_prod, img, [])
            _make_deployment(app_warehouse, warehouse_prod, rel, complete=True)
    _fast_forward_image_release_versions(
        warehouse_prod, image_target=5790, release_target=5824
    )
    warehouse_prod.process_counts = {
        "web": 31,
        "web-api": 30,
        "web-uploads": 6,
        "worker": 2,
        "worker-beat": 1,
        "worker-traced": 12,
    }
    warehouse_prod.process_pod_classes = {
        "web": "r1.medium",
        "web-api": "r1.xlarge",
        "web-uploads": "r1.large",
        "worker": "r1.medium",
        "worker-beat": "m1.small",
        "worker-traced": "r1.medium",
    }
    if not warehouse_prod.ingresses:
        _ = _make_ingress(warehouse_prod, "pypi.org", path="/", target_process="web")

    warehouse_staging = _get_or_create_app_env(app_warehouse, env_warehouse_staging)
    _ = _seed_app_history(
        app_warehouse,
        warehouse_staging,
        WAREHOUSE_PROCESSES,
        build_ref="develop",
        count=4,
    )
    warehouse_staging.process_counts = {
        "web": 5,
        "web-api": 5,
        "web-uploads": 1,
        "worker": 1,
        "worker-beat": 1,
        "worker-traced": 2,
    }
    warehouse_staging.process_pod_classes = dict(
        warehouse_prod.process_pod_classes or {}
    )

    pg_warehouse_prod: PostgresResource | None = PostgresResource.query.filter_by(
        environment_id=env_warehouse_prod.id, slug="warehouse-db"
    ).first()
    if pg_warehouse_prod is None:
        db.session.add(
            PostgresResource(
                service_version="18",
                environment_id=env_warehouse_prod.id,
                name="Warehouse DB",
                slug="warehouse-db",
                size_class="db.2xlarge",
                storage_size=750,
                ha_enabled=True,
                provisioning_status="ready",
            )
        )
        db.session.flush()

    redis_warehouse_prod = RedisResource.query.filter_by(
        environment_id=env_warehouse_prod.id, slug="warehouse-cache"
    ).first()
    if redis_warehouse_prod is None:
        db.session.add(
            RedisResource(
                service_version="8",
                environment_id=env_warehouse_prod.id,
                name="Warehouse Cache",
                slug="warehouse-cache",
                size_class="cache.large",
                storage_size=4,
                provisioning_status="ready",
            )
        )
        db.session.flush()

    pg_warehouse_staging = PostgresResource.query.filter_by(
        environment_id=env_warehouse_staging.id, slug="warehouse-db"
    ).first()
    if pg_warehouse_staging is None:
        db.session.add(
            PostgresResource(
                service_version="18",
                environment_id=env_warehouse_staging.id,
                name="Warehouse DB (staging)",
                slug="warehouse-db",
                size_class="db.medium",
                storage_size=50,
                ha_enabled=False,
                provisioning_status="ready",
            )
        )
        db.session.flush()

    proj_pypi_docs = _get_or_create_project(
        org_pypi, "PyPI Docs", "pypi-docs", environments_enabled=False
    )
    env_pypi_docs = _get_or_create_default_environment(proj_pypi_docs)
    _ = _seed_simple_app(
        proj_pypi_docs,
        env_pypi_docs,
        "Docs",
        "docs",
        processes=DOCS_PROCESSES,
        process_counts={"web": 1},
        process_pod_classes={"web": "c1.small"},
        ingress_hostname="docs.pypi.org",
    )

    org_inspector = _get_or_create_org("Inspector", "inspector", admin, dev_user)
    proj_inspector = _get_or_create_project(
        org_inspector, "Inspector", "inspector", environments_enabled=False
    )
    env_inspector = _get_or_create_default_environment(proj_inspector)
    _ = _seed_simple_app(
        proj_inspector,
        env_inspector,
        "Inspector",
        "inspector",
        processes={
            "web": {
                "cmd": "gunicorn inspector.wsgi:application --bind 0.0.0.0:8000",
                "env": [],
            },
            "release": {"cmd": "bin/release", "env": []},
        },
        process_counts={"web": 2},
        process_pod_classes={"web": "c1.small"},
        ingress_hostname="inspector.pypi.io",
    )
    pg_inspector = PostgresResource.query.filter_by(
        environment_id=env_inspector.id, slug="inspector-db"
    ).first()
    if pg_inspector is None:
        db.session.add(
            PostgresResource(
                service_version="18",
                environment_id=env_inspector.id,
                name="Inspector DB",
                slug="inspector-db",
                size_class="db.small",
                storage_size=10,
                ha_enabled=False,
                provisioning_status="ready",
            )
        )
        db.session.flush()

    org_linehaul = _get_or_create_org("Linehaul", "linehaul", admin, dev_user)
    proj_linehaul = _get_or_create_project(
        org_linehaul, "Linehaul", "linehaul", environments_enabled=False
    )
    env_linehaul = _get_or_create_default_environment(proj_linehaul)
    _ = _seed_simple_app(
        proj_linehaul,
        env_linehaul,
        "Linehaul",
        "linehaul",
        processes={"worker": {"cmd": "python -m linehaul", "env": []}},
        process_counts={"worker": 1},
        process_pod_classes={"worker": "c1.small"},
        history_count=2,
    )
    redis_linehaul = RedisResource.query.filter_by(
        environment_id=env_linehaul.id, slug="linehaul-cache"
    ).first()
    if redis_linehaul is None:
        db.session.add(
            RedisResource(
                service_version="8",
                environment_id=env_linehaul.id,
                name="Linehaul Cache",
                slug="linehaul-cache",
                size_class="cache.nano",
                storage_size=1,
                provisioning_status="ready",
            )
        )
        db.session.flush()

    org_camo = _get_or_create_org("Camo", "camo", admin, dev_user)
    proj_camo = _get_or_create_project(
        org_camo, "Camo", "camo", environments_enabled=False
    )
    env_camo = _get_or_create_default_environment(proj_camo)
    _ = _seed_simple_app(
        proj_camo,
        env_camo,
        "Camo",
        "camo",
        processes={"web": {"cmd": "camo-server --bind 0.0.0.0:8081", "env": []}},
        process_counts={"web": 3},
        process_pod_classes={"web": "m1.small"},
        ingress_hostname="camo.pypi.io",
    )

    org_conveyor = _get_or_create_org("Conveyor", "conveyor", admin, dev_user)
    proj_conveyor = _get_or_create_project(
        org_conveyor,
        "Conveyor",
        "conveyor",
        environments_enabled=False,
        deleted=True,
    )
    env_conveyor = _get_or_create_default_environment(proj_conveyor)
    _ = _seed_simple_app(
        proj_conveyor,
        env_conveyor,
        "Conveyor",
        "conveyor",
        processes={"worker": {"cmd": "python -m conveyor", "env": []}},
        process_counts={"worker": 1},
        process_pod_classes={"worker": "m1.small"},
        history_count=2,
    )

    # ==================================================================
    # Org: psf — python.org (pythondotorg), psf-membership, donations
    # ==================================================================
    org_psf = _get_or_create_org("Python Software Foundation", "psf", admin, dev_user)

    proj_pydotorg = _get_or_create_project(
        org_psf, "python.org", "pythondotorg", environments_enabled=True
    )
    env_pydotorg_prod = _get_or_create_environment(
        proj_pydotorg, "Production", "production", is_default=True, sort_order=100
    )
    env_pydotorg_staging = _get_or_create_environment(
        proj_pydotorg, "Staging", "staging", sort_order=50
    )
    app_pydotorg_web, pydotorg_web_prod = _seed_simple_app(
        proj_pydotorg,
        env_pydotorg_prod,
        "Web",
        "web",
        processes={
            "web": {
                "cmd": "gunicorn pydotorg.wsgi:application --bind 0.0.0.0:8000",
                "env": [],
            },
            "release": {"cmd": "bin/release", "env": []},
        },
        process_counts={"web": 4},
        process_pod_classes={"web": "c1.medium"},
        history_count=18,
        ingress_hostname="python.org",
    )
    pydotorg_web_staging = _get_or_create_app_env(
        app_pydotorg_web, env_pydotorg_staging
    )
    _ = _seed_app_history(
        app_pydotorg_web,
        pydotorg_web_staging,
        {
            "web": {
                "cmd": "gunicorn pydotorg.wsgi:application --bind 0.0.0.0:8000",
                "env": [],
            },
            "release": {"cmd": "bin/release", "env": []},
        },
        build_ref="develop",
        count=3,
    )
    pydotorg_web_staging.process_counts = {"web": 1}
    pydotorg_web_staging.process_pod_classes = {"web": "c1.small"}

    _ = _seed_simple_app(
        proj_pydotorg,
        env_pydotorg_prod,
        "Worker",
        "worker",
        processes={"worker": {"cmd": "celery -A pydotorg worker", "env": []}},
        process_counts={"worker": 2},
        process_pod_classes={"worker": "m1.medium"},
    )

    pg_pydotorg = PostgresResource.query.filter_by(
        environment_id=env_pydotorg_prod.id, slug="pydotorg-db"
    ).first()
    if pg_pydotorg is None:
        db.session.add(
            PostgresResource(
                service_version="18",
                environment_id=env_pydotorg_prod.id,
                name="python.org DB",
                slug="pydotorg-db",
                size_class="db.large",
                storage_size=50,
                ha_enabled=True,
                provisioning_status="ready",
            )
        )
        db.session.flush()

    redis_pydotorg = RedisResource.query.filter_by(
        environment_id=env_pydotorg_prod.id, slug="pydotorg-cache"
    ).first()
    if redis_pydotorg is None:
        db.session.add(
            RedisResource(
                service_version="8",
                environment_id=env_pydotorg_prod.id,
                name="python.org Cache",
                slug="pydotorg-cache",
                size_class="cache.medium",
                storage_size=2,
                provisioning_status="ready",
            )
        )
        db.session.flush()

    proj_membership = _get_or_create_project(
        org_psf, "PSF Membership", "psf-membership", environments_enabled=False
    )
    env_membership = _get_or_create_default_environment(proj_membership)
    _ = _seed_simple_app(
        proj_membership,
        env_membership,
        "Membership",
        "membership",
        processes={
            "web": {
                "cmd": "gunicorn membership.wsgi:application --bind 0.0.0.0:8000",
                "env": [],
            },
        },
        process_counts={"web": 2},
        process_pod_classes={"web": "m1.medium"},
        ingress_hostname="membership.python.org",
    )
    pg_membership = PostgresResource.query.filter_by(
        environment_id=env_membership.id, slug="membership-db"
    ).first()
    if pg_membership is None:
        db.session.add(
            PostgresResource(
                service_version="18",
                environment_id=env_membership.id,
                name="Membership DB",
                slug="membership-db",
                size_class="db.small",
                storage_size=5,
                ha_enabled=False,
                provisioning_status="ready",
            )
        )
        db.session.flush()

    proj_donations = _get_or_create_project(
        org_psf, "Donations", "donations", environments_enabled=False
    )
    env_donations = _get_or_create_default_environment(proj_donations)
    _ = _seed_simple_app(
        proj_donations,
        env_donations,
        "Donations",
        "donations",
        processes={
            "web": {
                "cmd": "gunicorn donations.wsgi:application --bind 0.0.0.0:8000",
                "env": [],
            },
        },
        process_counts={"web": 2},
        process_pod_classes={"web": "m1.small"},
        ingress_hostname="donate.pypi.org",
    )
    redis_donations = RedisResource.query.filter_by(
        environment_id=env_donations.id, slug="donations-cache"
    ).first()
    if redis_donations is None:
        db.session.add(
            RedisResource(
                service_version="8",
                environment_id=env_donations.id,
                name="Donations Cache",
                slug="donations-cache",
                size_class="cache.small",
                storage_size=1,
                provisioning_status="ready",
            )
        )
        db.session.flush()

    proj_infra = _get_or_create_project(
        org_psf, "Infra Automation", "infra-automation", environments_enabled=False
    )
    env_infra = _get_or_create_default_environment(proj_infra)
    _ = _seed_simple_app(
        proj_infra,
        env_infra,
        "Salt States",
        "salt-states",
        processes={"worker": {"cmd": "salt-master --log-level=info", "env": []}},
        process_counts={"worker": 1},
        process_pod_classes={"worker": "m1.small"},
        history_count=2,
    )

    # ==================================================================
    # Org: pycon — pycon-site, us.pycon.org, regs
    # ==================================================================
    org_pycon = _get_or_create_org("PyCon", "pycon", admin, dev_user)

    proj_pycon_site = _get_or_create_project(
        org_pycon, "PyCon Site", "pycon-site", environments_enabled=True
    )
    env_pycon_prod = _get_or_create_environment(
        proj_pycon_site, "Production", "production", is_default=True, sort_order=100
    )
    env_pycon_staging = _get_or_create_environment(
        proj_pycon_site, "Staging", "staging", sort_order=50
    )
    app_pycon_site, pycon_site_prod = _seed_simple_app(
        proj_pycon_site,
        env_pycon_prod,
        "PyCon Site",
        "site",
        processes={
            "web": {
                "cmd": "gunicorn pycon.wsgi:application --bind 0.0.0.0:8000",
                "env": [],
            },
            "release": {"cmd": "bin/release", "env": []},
        },
        process_counts={"web": 3},
        process_pod_classes={"web": "c1.medium"},
        history_count=12,
        ingress_hostname="pycon.org",
    )
    pycon_site_staging = _get_or_create_app_env(app_pycon_site, env_pycon_staging)
    _ = _seed_app_history(
        app_pycon_site,
        pycon_site_staging,
        {
            "web": {
                "cmd": "gunicorn pycon.wsgi:application --bind 0.0.0.0:8000",
                "env": [],
            },
        },
        build_ref="develop",
        count=2,
    )
    pycon_site_staging.process_counts = {"web": 1}
    pycon_site_staging.process_pod_classes = {"web": "c1.small"}

    proj_us_pycon = _get_or_create_project(
        org_pycon, "US PyCon", "us-pycon-org", environments_enabled=False
    )
    env_us_pycon = _get_or_create_default_environment(proj_us_pycon)
    _ = _seed_simple_app(
        proj_us_pycon,
        env_us_pycon,
        "US PyCon",
        "site",
        processes={
            "web": {
                "cmd": "gunicorn uspycon.wsgi:application --bind 0.0.0.0:8000",
                "env": [],
            },
        },
        process_counts={"web": 2},
        process_pod_classes={"web": "c1.small"},
        ingress_hostname="us.pycon.org",
    )

    proj_regs = _get_or_create_project(
        org_pycon, "Registration", "regs", environments_enabled=False
    )
    env_regs = _get_or_create_default_environment(proj_regs)
    _ = _seed_simple_app(
        proj_regs,
        env_regs,
        "Registration",
        "regs",
        processes={
            "web": {
                "cmd": "gunicorn regs.wsgi:application --bind 0.0.0.0:8000",
                "env": [],
            },
        },
        process_counts={"web": 2},
        process_pod_classes={"web": "m1.medium"},
        ingress_hostname="regs.pycon.org",
    )
    pg_regs: PostgresResource | None = PostgresResource.query.filter_by(
        environment_id=env_regs.id, slug="regs-db"
    ).first()
    if pg_regs is None:
        db.session.add(
            PostgresResource(
                service_version="18",
                environment_id=env_regs.id,
                name="Registration DB",
                slug="regs-db",
                size_class="db.medium",
                storage_size=20,
                ha_enabled=False,
                provisioning_status="ready",
            )
        )
        db.session.flush()

    # ==================================================================
    # Org: docs — docs.python.org builder, bugs (deleted app), discuss,
    # mail, buildbot, packaging (multi-app, one deleted), speed.python.org
    # ==================================================================
    org_docs = _get_or_create_org("Python Docs", "docs", admin, dev_user)

    proj_docs_builder = _get_or_create_project(
        org_docs, "docs.python.org", "docs-builder", environments_enabled=False
    )
    env_docs_builder = _get_or_create_default_environment(proj_docs_builder)
    _ = _seed_simple_app(
        proj_docs_builder,
        env_docs_builder,
        "Builder",
        "builder",
        processes={"worker": {"cmd": "python build_docs.py --watch", "env": []}},
        process_counts={"worker": 1},
        process_pod_classes={"worker": "c1.small"},
        history_count=2,
        ingress_hostname="docs.python.org",
    )

    proj_bugs = _get_or_create_project(
        org_docs, "Bugs", "bugs", environments_enabled=False
    )
    env_bugs = _get_or_create_default_environment(proj_bugs)
    _ = _seed_simple_app(
        proj_bugs,
        env_bugs,
        "Bug Tracker",
        "tracker",
        processes={
            "web": {
                "cmd": "gunicorn bugs.wsgi:application --bind 0.0.0.0:8000",
                "env": [],
            },
        },
        process_counts={"web": 1},
        process_pod_classes={"web": "m1.small"},
        history_count=2,
        deleted=True,
    )

    proj_discuss = _get_or_create_project(
        org_docs, "Discuss", "discuss", environments_enabled=False
    )
    env_discuss = _get_or_create_default_environment(proj_discuss)
    _ = _seed_simple_app(
        proj_discuss,
        env_discuss,
        "Discuss",
        "discuss",
        processes={
            "web": {"cmd": "bundle exec puma -p 8000", "env": []},
            "worker": {"cmd": "bundle exec sidekiq", "env": []},
        },
        process_counts={"web": 3, "worker": 2},
        process_pod_classes={"web": "c1.medium", "worker": "m1.medium"},
        ingress_hostname="discuss.python.org",
    )
    pg_discuss: PostgresResource | None = PostgresResource.query.filter_by(
        environment_id=env_discuss.id, slug="discuss-db"
    ).first()
    if pg_discuss is None:
        db.session.add(
            PostgresResource(
                service_version="18",
                environment_id=env_discuss.id,
                name="Discuss DB",
                slug="discuss-db",
                size_class="db.large",
                storage_size=30,
                ha_enabled=True,
                provisioning_status="ready",
            )
        )
        db.session.flush()
    redis_discuss = RedisResource.query.filter_by(
        environment_id=env_discuss.id, slug="discuss-cache"
    ).first()
    if redis_discuss is None:
        db.session.add(
            RedisResource(
                service_version="8",
                environment_id=env_discuss.id,
                name="Discuss Cache",
                slug="discuss-cache",
                size_class="cache.medium",
                storage_size=2,
                provisioning_status="ready",
            )
        )
        db.session.flush()

    proj_mail = _get_or_create_project(
        org_docs, "Mail", "mail", environments_enabled=False
    )
    env_mail = _get_or_create_default_environment(proj_mail)
    _ = _seed_simple_app(
        proj_mail,
        env_mail,
        "Mailman",
        "mailman",
        processes={"worker": {"cmd": "mailman start --nodaemon", "env": []}},
        process_counts={"worker": 1},
        process_pod_classes={"worker": "m1.small"},
        history_count=2,
    )

    proj_buildbot = _get_or_create_project(
        org_docs, "Buildbot", "buildbot", environments_enabled=False
    )
    env_buildbot = _get_or_create_default_environment(proj_buildbot)
    _ = _seed_simple_app(
        proj_buildbot,
        env_buildbot,
        "Buildbot",
        "buildbot",
        processes={
            "web": {"cmd": "buildbot-worker start --nodaemon", "env": []},
            "worker": {"cmd": "buildbot start --nodaemon", "env": []},
        },
        process_counts={"web": 1, "worker": 2},
        process_pod_classes={"web": "m1.small", "worker": "c1.medium"},
        ingress_hostname="buildbot.python.org",
    )

    proj_packaging = _get_or_create_project(
        org_docs, "Packaging", "packaging", environments_enabled=False
    )
    env_packaging = _get_or_create_default_environment(proj_packaging)
    _ = _seed_simple_app(
        proj_packaging,
        env_packaging,
        "Packaging Guide",
        "guide",
        processes={"web": {"cmd": "mkdocs serve --dev-addr 0.0.0.0:8000", "env": []}},
        process_counts={"web": 1},
        process_pod_classes={"web": "c1.small"},
        history_count=2,
        ingress_hostname="packaging.python.org",
    )
    _ = _seed_simple_app(
        proj_packaging,
        env_packaging,
        "Packaging Index Bot",
        "index-bot",
        processes={"worker": {"cmd": "python -m packaging_index_bot", "env": []}},
        process_counts={"worker": 1},
        process_pod_classes={"worker": "m1.small"},
        history_count=2,
        deleted=True,
    )

    proj_speed = _get_or_create_project(
        org_docs, "speed.python.org", "speed", environments_enabled=False
    )
    env_speed = _get_or_create_default_environment(proj_speed)
    _ = _seed_simple_app(
        proj_speed,
        env_speed,
        "Speed",
        "speed",
        processes={
            "web": {
                "cmd": "gunicorn speed.wsgi:application --bind 0.0.0.0:8000",
                "env": [],
            },
        },
        process_counts={"web": 1},
        process_pod_classes={"web": "m1.small"},
        history_count=2,
        ingress_hostname="speed.python.org",
    )

    # ==================================================================
    # Org: sandbox — a personal/sandbox org with tiny or idle apps
    # ==================================================================
    org_sandbox = _get_or_create_org("Sandbox", "sandbox", admin, dev_user)

    proj_scratch = _get_or_create_project(
        org_sandbox, "Scratch", "scratch", environments_enabled=False
    )
    env_scratch = _get_or_create_default_environment(proj_scratch)
    _ = _seed_simple_app(
        proj_scratch,
        env_scratch,
        "Idle App",
        "idle-app",
        processes={"web": {"cmd": "python -m http.server 8000", "env": []}},
        process_counts={"web": 1},
        process_pod_classes={"web": "m1.small"},
        history_count=1,
    )

    proj_experiments = _get_or_create_project(
        org_sandbox, "Experiments", "experiments", environments_enabled=False
    )
    env_experiments = _get_or_create_default_environment(proj_experiments)
    _ = _seed_simple_app(
        proj_experiments,
        env_experiments,
        "Old Experiment",
        "old-experiment",
        processes={"worker": {"cmd": "python -m old_experiment", "env": []}},
        process_counts={"worker": 1},
        process_pod_classes={"worker": "m1.small"},
        history_count=1,
        deleted=True,
    )

    db.session.commit()


def seed():
    app = create_app()

    if not app.config["DEBUG"]:
        print(
            "Warning: this command should only be run in development/test environments"
        )
        sys.exit(1)

    with app.app_context():
        # ── Run create_admin logic first ──────────────────────────────
        # Check if admin already exists (idempotent)
        admin = User.query.filter_by(username="admin").first()
        if admin is None:
            print("Running create_admin bootstrap...")
            admin = User(  # nosec
                email="ad@min.com",
                password="admin",
                username="admin",
                admin=True,
                active=True,
                fs_uniquifier="admin",
            )
            db.session.add(admin)
            db.session.flush()

            org = Organization(name="Acme Corp", slug="acme-corp")
            org.add_user(admin, admin=True)
            db.session.add(org)
            db.session.flush()
            db.session.refresh(org)
        else:
            print("Admin user already exists, skipping bootstrap.")
            org = Organization.query.filter_by(slug="acme-corp").first()
            if org is None:
                print("ERROR: admin exists but Acme Corp org not found!")
                sys.exit(1)

        # ── Extra dev user ────────────────────────────────────────────
        dev_user = User.query.filter_by(username="dev").first()
        if dev_user is None:
            dev_user = User(  # nosec
                email="dev@acme.corp",
                password="dev",
                username="dev",
                admin=False,
                active=True,
                fs_uniquifier="dev",
            )
            db.session.add(dev_user)
            db.session.flush()
            org.add_user(dev_user, admin=False)
            db.session.flush()
            print("Created dev user (dev@acme.corp / dev)")

        # ==============================================================
        # Project 1: My API (environments enabled)
        # ==============================================================
        proj_api = Project.query.filter_by(
            slug="my-api", organization_id=org.id
        ).first()
        if proj_api is None:
            proj_api = Project(
                name="My API",
                slug="my-api",
                organization_id=org.id,
                environments_enabled=True,
            )
            db.session.add(proj_api)
            db.session.flush()
            db.session.refresh(proj_api)

        # Environments
        env_prod = Environment.query.filter_by(
            slug="production", project_id=proj_api.id
        ).first()
        if env_prod is None:
            env_prod = Environment(
                name="Production",
                slug="production",
                project_id=proj_api.id,
                is_default=True,
                sort_order=100,
            )
            db.session.add(env_prod)
            db.session.flush()
            db.session.refresh(env_prod)

        env_staging = Environment.query.filter_by(
            slug="staging", project_id=proj_api.id
        ).first()
        if env_staging is None:
            env_staging = Environment(
                name="Staging",
                slug="staging",
                project_id=proj_api.id,
                is_default=False,
                sort_order=50,
            )
            db.session.add(env_staging)
            db.session.flush()
            db.session.refresh(env_staging)

        # ── App: Web ──────────────────────────────────────────────────
        app_web = Application.query.filter_by(
            slug="web", project_id=proj_api.id
        ).first()
        if app_web is None:
            app_web = Application(name="Web", slug="web", project_id=proj_api.id)
            db.session.add(app_web)
            db.session.flush()
            db.session.refresh(app_web)

        # Enroll Web in Production
        web_prod = ApplicationEnvironment.query.filter_by(
            application_id=app_web.id, environment_id=env_prod.id
        ).first()
        if web_prod is None:
            web_prod = _enroll_app(app_web, env_prod)

        # Enroll Web in Staging
        web_staging = ApplicationEnvironment.query.filter_by(
            application_id=app_web.id, environment_id=env_staging.id
        ).first()
        if web_staging is None:
            web_staging = _enroll_app(app_web, env_staging)

        # Configs for Web/Production
        if not web_prod.configurations:
            configs_web_prod = [
                _make_config(
                    app_web, web_prod, "DATABASE_URL", "postgresql://db:5432/myapi"
                ),
                _make_config(
                    app_web,
                    web_prod,
                    "SECRET_KEY",
                    "super-secret-prod-key",
                    secret=True,
                ),
                _make_config(app_web, web_prod, "LOG_LEVEL", "warning"),
                _make_config(app_web, web_prod, "CABOTAGE_SENTINEL", "1"),
            ]
            db.session.flush()
        else:
            configs_web_prod = list(web_prod.configurations)

        # Configs for Web/Staging
        if not web_staging.configurations:
            configs_web_staging = [
                _make_config(
                    app_web,
                    web_staging,
                    "DATABASE_URL",
                    "postgresql://db:5432/myapi_staging",
                ),
                _make_config(
                    app_web, web_staging, "SECRET_KEY", "staging-secret", secret=True
                ),
                _make_config(app_web, web_staging, "LOG_LEVEL", "debug"),
                _make_config(app_web, web_staging, "CABOTAGE_SENTINEL", "1"),
            ]
            db.session.flush()
        else:
            configs_web_staging = list(web_staging.configurations)

        # Pipeline runs for Web/Production (backdated after commit)
        new_runs = WEB_PROD_PIPELINE_RUNS if web_prod.images.count() == 0 else []
        pipeline_runs = []
        for age_days, *durations, failed in new_runs:
            img = _make_image(
                app_web, web_prod, build_ref="main", processes=WEB_PROCESSES
            )
            rel = _make_release(app_web, web_prod, img, configs_web_prod)
            dep = _make_deployment(app_web, web_prod, rel, complete=not failed)
            dep.deploy_metadata = {"auto_deploy": True}
            if failed:
                dep.error = True
                dep.error_detail = "Readiness probe failed"
            pipeline_runs.append((age_days, zip((img, rel, dep), durations)))

        # Process counts for Web/Production
        web_prod.process_counts = {"web": 2, "worker": 1}

        # Ingress for Web/Production
        if not web_prod.ingresses:
            _make_ingress(web_prod, "api.acme.corp", path="/", target_process="web")

        # Backing services for Production (so cost/backing-service views have
        # something real to show)
        pg_resource = PostgresResource.query.filter_by(
            environment_id=env_prod.id, slug="main-db"
        ).first()
        if pg_resource is None:
            pg_resource = PostgresResource(
                service_version="18",
                environment_id=env_prod.id,
                name="Main DB",
                slug="main-db",
                size_class="db.small",
                storage_size=10,
                ha_enabled=True,
                provisioning_status="ready",
            )
            db.session.add(pg_resource)
            db.session.flush()

        redis_resource: RedisResource | None = RedisResource.query.filter_by(
            environment_id=env_prod.id, slug="app-cache"
        ).first()
        if redis_resource is None:
            redis_resource = RedisResource(
                service_version="8",
                environment_id=env_prod.id,
                name="App Cache",
                slug="app-cache",
                size_class="cache.small",
                storage_size=1,
                provisioning_status="ready",
            )
            db.session.add(redis_resource)
            db.session.flush()

        # Images for Web/Staging (1 image)
        if web_staging.images.count() == 0:
            staging_img = _make_image(
                app_web, web_staging, build_ref="develop", processes=WEB_PROCESSES
            )
        else:
            staging_img = (
                web_staging.images.filter_by(built=True)
                .order_by(Image.version.desc())
                .first()
            )

        # Release + Deployment for Web/Staging
        if web_staging.releases.count() == 0:
            staging_rel = _make_release(
                app_web, web_staging, staging_img, configs_web_staging
            )
            _make_deployment(app_web, web_staging, staging_rel, complete=True)

        web_staging.process_counts = {"web": 1, "worker": 1}

        # ── App: Worker ───────────────────────────────────────────────
        app_worker = Application.query.filter_by(
            slug="worker", project_id=proj_api.id
        ).first()
        if app_worker is None:
            app_worker = Application(
                name="Worker", slug="worker", project_id=proj_api.id
            )
            db.session.add(app_worker)
            db.session.flush()
            db.session.refresh(app_worker)

        # Enroll Worker in Production only
        worker_prod = ApplicationEnvironment.query.filter_by(
            application_id=app_worker.id, environment_id=env_prod.id
        ).first()
        if worker_prod is None:
            worker_prod = _enroll_app(app_worker, env_prod)

        # Configs for Worker/Production
        if not worker_prod.configurations:
            configs_worker_prod = [
                _make_config(
                    app_worker,
                    worker_prod,
                    "DATABASE_URL",
                    "postgresql://db:5432/myapi",
                ),
                _make_config(
                    app_worker, worker_prod, "REDIS_URL", "redis://redis:6379/0"
                ),
                _make_config(app_worker, worker_prod, "CABOTAGE_SENTINEL", "1"),
            ]
            db.session.flush()
        else:
            configs_worker_prod = list(worker_prod.configurations)

        # Image, Release, Deployment for Worker/Production
        if worker_prod.images.count() == 0:
            worker_img = _make_image(
                app_worker, worker_prod, build_ref="main", processes=WORKER_PROCESSES
            )
        else:
            worker_img = (
                worker_prod.images.filter_by(built=True)
                .order_by(Image.version.desc())
                .first()
            )

        if worker_prod.releases.count() == 0:
            worker_rel = _make_release(
                app_worker, worker_prod, worker_img, configs_worker_prod
            )
            _make_deployment(app_worker, worker_prod, worker_rel, complete=True)

        worker_prod.process_counts = {"worker": 2}

        # ==============================================================
        # Project 2: Docs Site (environments disabled)
        # ==============================================================
        proj_docs = Project.query.filter_by(slug="docs", organization_id=org.id).first()
        if proj_docs is None:
            proj_docs = Project(
                name="Docs Site",
                slug="docs",
                organization_id=org.id,
                environments_enabled=False,
            )
            db.session.add(proj_docs)
            db.session.flush()
            db.session.refresh(proj_docs)

        # Default environment for docs (needed even when environments_enabled=False)
        env_docs_default = Environment.query.filter_by(
            slug="default", project_id=proj_docs.id
        ).first()
        if env_docs_default is None:
            env_docs_default = Environment(
                name="Default",
                slug="default",
                project_id=proj_docs.id,
                is_default=True,
            )
            db.session.add(env_docs_default)
            db.session.flush()
            db.session.refresh(env_docs_default)

        # App: Site
        app_site = Application.query.filter_by(
            slug="site", project_id=proj_docs.id
        ).first()
        if app_site is None:
            app_site = Application(name="Site", slug="site", project_id=proj_docs.id)
            db.session.add(app_site)
            db.session.flush()
            db.session.refresh(app_site)

        # Enroll Site — legacy style (k8s_identifier=None for non-env projects)
        site_env = ApplicationEnvironment.query.filter_by(
            application_id=app_site.id, environment_id=env_docs_default.id
        ).first()
        if site_env is None:
            site_env = ApplicationEnvironment(
                application_id=app_site.id,
                environment_id=env_docs_default.id,
                k8s_identifier=None,
            )
            db.session.add(site_env)
            db.session.flush()
            db.session.refresh(site_env)

        # Configs for Site
        if not site_env.configurations:
            configs_site = [
                _make_config(app_site, site_env, "BASE_URL", "https://docs.acme.corp"),
                _make_config(app_site, site_env, "CABOTAGE_SENTINEL", "1"),
            ]
            db.session.flush()
        else:
            configs_site = list(site_env.configurations)

        # Image, Release, Deployment for Site
        if site_env.images.count() == 0:
            site_img = _make_image(
                app_site, site_env, build_ref="main", processes=DOCS_PROCESSES
            )
        else:
            site_img = (
                site_env.images.filter_by(built=True)
                .order_by(Image.version.desc())
                .first()
            )

        if site_env.releases.count() == 0:
            site_rel = _make_release(app_site, site_env, site_img, configs_site)
            _make_deployment(app_site, site_env, site_rel, complete=True)

        site_env.process_counts = {"web": 1}

        # Ingress for Site
        if not site_env.ingresses:
            _make_ingress(site_env, "docs.acme.corp", path="/", target_process="web")

        # ── Job history for Web/Production ─────────────────────────────
        existing_job_logs = JobLog.query.filter_by(
            application_id=app_web.id,
            application_environment_id=web_prod.id,
        ).count()
        if existing_job_logs == 0:
            now = datetime.datetime.now(datetime.UTC)
            for proc_name, interval_min, count in [
                ("job-cleanup", 360, 12),
                ("job-reports", 1440, 7),
            ]:
                for i in range(count):
                    scheduled = now - datetime.timedelta(
                        minutes=interval_min * (count - i)
                    )
                    started = scheduled + datetime.timedelta(seconds=3)
                    duration = 25 + (i * 7) % 30
                    completed = started + datetime.timedelta(seconds=duration)
                    succeeded = i != 2  # one failure
                    db.session.add(
                        JobLog(
                            application_id=app_web.id,
                            application_environment_id=web_prod.id,
                            process_name=proc_name,
                            job_name=f"seed-{app_web.slug}-{proc_name}-{29577000 + i}",
                            namespace=f"seed-{web_prod.id}",
                            schedule_timestamp=scheduled,
                            start_time=started,
                            completion_time=completed,
                            duration_seconds=duration,
                            succeeded=succeeded,
                            pods_active=0,
                            pods_succeeded=1 if succeeded else 0,
                            pods_failed=0 if succeeded else 1,
                            release_version=1,
                            deployment_id="seed-deploy",
                            labels={
                                "organization": org.slug,
                                "project": proj_api.slug,
                                "application": app_web.slug,
                                "process": proc_name,
                            },
                            resources={
                                "requests": {"cpu": "500m", "memory": "1Gi"},
                                "limits": {"cpu": "1", "memory": "1536Mi"},
                            },
                        )
                    )
            db.session.flush()
            print("Created job history for Web/Production (job-cleanup, job-reports)")

        web_prod.process_counts = {
            **web_prod.process_counts,
            "job-cleanup": 1,
            "job-reports": 1,
        }

        # ── Commit everything ─────────────────────────────────────────
        db.session.commit()

        # Backdate pipeline runs with core UPDATEs; the ORM's onupdate would
        # otherwise reset `updated` to now().
        now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
        for age_days, stages in pipeline_runs:
            started = now - datetime.timedelta(days=age_days)
            for obj, secs in stages:
                model = type(obj)
                _ = db.session.execute(
                    update(model)
                    .where(model.id == obj.id)
                    .values(
                        created=started,
                        updated=started + datetime.timedelta(seconds=secs),
                    )
                )
                started += datetime.timedelta(seconds=secs)
        db.session.commit()

        if os.environ.get("SEED_SCALE") == "pypi":
            seed_pypi_scale(admin, dev_user)

        # ── Synthetic cost history (every organization, not just Acme) ───
        # CostCollectionRun is one row per cluster-wide hour shared by every
        # organization's CostLineItem rows for that hour, so this must loop
        # over every Organization and call seed_cost_history once each —
        # seed_cost_history itself is safe to call per-org (it reuses the
        # shared hour's run rather than deleting/recreating it).
        from cabotage.utils.cost.demo import finalize_platform_stats, seed_cost_history

        all_orgs: list[Organization] = Organization.query.filter(
            Organization.deleted_at.is_(None)
        ).all()
        cost_items_by_org: dict[str, int] = {}
        for organization in all_orgs:
            cost_items_by_org[organization.slug] = seed_cost_history(
                organization, days=90
            )
        cost_items = sum(cost_items_by_org.values())
        # Backfill cluster-wide stats (cluster_compute_cost,
        # overhead_allocated, etc.) once every organization's own line
        # items exist, so the admin platform-scope report's reconciliation
        # math holds across the whole demo window, not just real-collector
        # hours.
        _ = finalize_platform_stats(all_orgs, days=90)
        print()
        print("Seed complete!")
        print("  Organizations: 1 (Acme Corp)")
        print("  Users: admin (ad@min.com), dev (dev@acme.corp)")
        print("  Projects: My API (2 envs, 2 apps), Docs Site (1 app)")
        print("  Images / Releases / Deployments created")
        print("  Ingress: api.acme.corp, docs.acme.corp")
        print(
            f"  Cost history: {cost_items} line items over 90 days "
            f"across {len(cost_items_by_org)} organization(s)"
        )
        if os.environ.get("SEED_SCALE") == "pypi":
            print(
                "  PyPI-scale orgs also seeded (SEED_SCALE=pypi): pypi, "
                "inspector, linehaul, camo, conveyor, psf, pycon, docs, sandbox"
            )
        print()
        print("Login at http://localhost:5000 with admin/admin or dev/dev")


if __name__ == "__main__":
    seed()
