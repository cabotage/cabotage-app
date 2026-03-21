"""Seed script for local development.

Runs create_admin first (user + org + bare project), then layers on
realistic data so the UI is fully populated:
  - Extra dev user
  - Multiple projects, apps, environments
  - Configurations, images, releases, deployments, ingress rules
  - Job process history
"""

import datetime

from cabotage.server import create_app, db
from cabotage.server.models import Organization, User
from cabotage.server.models.auth import Billing
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

DOCS_PROCESSES = {
    "web": {"cmd": "nginx -g 'daemon off;'", "env": []},
}


def seed():
    app = create_app()

    if not app.config["DEBUG"]:
        print(
            "Warning: this command should only be run in development/test environments"
        )
        exit(1)

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
                exit(1)

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

        # Images for Web/Production (3 images)
        if web_prod.images.count() == 0:
            _make_image(app_web, web_prod, build_ref="main", processes=WEB_PROCESSES)
            _make_image(app_web, web_prod, build_ref="main", processes=WEB_PROCESSES)
            latest_web_prod_img = _make_image(
                app_web, web_prod, build_ref="main", processes=WEB_PROCESSES
            )
        else:
            latest_web_prod_img = (
                web_prod.images.filter_by(built=True)
                .order_by(Image.version.desc())
                .first()
            )

        # Releases for Web/Production (2 releases)
        if web_prod.releases.count() == 0:
            _make_release(app_web, web_prod, latest_web_prod_img, configs_web_prod)
            latest_web_prod_rel = _make_release(
                app_web, web_prod, latest_web_prod_img, configs_web_prod
            )
        else:
            latest_web_prod_rel = (
                web_prod.releases.filter_by(built=True)
                .order_by(Release.version.desc())
                .first()
            )

        # Deployment for Web/Production
        if web_prod.deployments.count() == 0:
            _make_deployment(app_web, web_prod, latest_web_prod_rel, complete=True)

        # Process counts for Web/Production
        web_prod.process_counts = {"web": 2, "worker": 1}

        # Ingress for Web/Production
        if not web_prod.ingresses:
            _make_ingress(web_prod, "api.acme.corp", path="/", target_process="web")

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
            now = datetime.datetime.utcnow()
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


        # ==============================================================
        # Billing: Acme Corp on Indie plan with fake Stripe IDs
        # ==============================================================
        billing = Billing.query.filter_by(org_id=org.id).first()
        if billing is None:
            billing = Billing(
                org_id=org.id,
                stripe_customer_id="cus_seed_acme_corp_001",
                stripe_sub_id="sub_seed_acme_corp_001",
                stripe_sub_status="active",
                stripe_sub_plan="indie",
            )
            db.session.add(billing)
            db.session.flush()
            print("Created billing record (Indie plan, active)")
        else:
            print("Billing record already exists, skipping.")

        # ── Commit everything ─────────────────────────────────────────
        db.session.commit()
        print()
        print("Seed complete!")
        print("  Organizations: 1 (Acme Corp)")
        print("  Users: admin (ad@min.com), dev (dev@acme.corp)")
        print("  Projects: My API (2 envs, 2 apps), Docs Site (1 app)")
        print("  Images / Releases / Deployments created")
        print("  Ingress: api.acme.corp, docs.acme.corp")
        print("  Billing: Indie plan (active)")
        print()
        print("Login at http://localhost:5000 with admin/admin or dev/dev")


if __name__ == "__main__":
    seed()
