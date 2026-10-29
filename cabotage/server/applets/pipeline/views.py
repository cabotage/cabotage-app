import collections
import datetime
from typing import cast

from flask import abort, jsonify, render_template, request
from flask_security import login_required
from sqlalchemy import Uuid, desc, func
from werkzeug.wrappers import Response

from cabotage.server import db
from cabotage.server.acl import ViewOrganizationPermission, ViewProjectPermission
from cabotage.server.applets.pipeline.constants import INFRA_PIPELINE_INACTIVE_LIMIT
from cabotage.server.applets.pipeline.services import (
    PIPELINE_STAGES,
    percentiles,
    pipeline_app_env_inventory,
    pipeline_app_env_inventory_all,
    pipeline_finished,
    pipeline_live_run,
    pipeline_range,
    pipeline_rollup_rows,
    pipeline_rollup_stats,
    pipeline_rollup_summary,
    pipeline_run_duration,
    pipeline_window,
    row_duration,
    seconds,
)
from cabotage.server.models.auth import Organization
from cabotage.server.models.projects import Deployment, Image, Project, Release

# Routes stay on the user blueprint so endpoint names (user.*) are unchanged.
from cabotage.server.user.views import (
    _lookup_app_context,
    _require_admin,
    _resolve_app_env,
    user_blueprint,
)
from cabotage.utils.datetimes import iso_utc


def _requested_window() -> tuple[datetime.datetime, datetime.datetime | None]:
    """The query string's pipeline window; an unrepresentable end is a 400."""
    try:
        return pipeline_window(
            request.args.get("range"),
            request.args.get("since"),
            request.args.get("until"),
        )
    except OverflowError:
        abort(400)


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/env/<env_slug>/applications/<app_slug>/pipeline",
)
@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/pipeline",
    defaults={"env_slug": None},
)
@login_required
def project_application_pipeline(
    org_slug: str, project_slug: str, app_slug: str, env_slug: str | None = None
):
    _org, project, application = _lookup_app_context(org_slug, project_slug, app_slug)
    app_env = _resolve_app_env(
        application, env_slug=env_slug, project=project, required=False
    )

    return render_template(
        "user/project_application_pipeline.html",
        application=application,
        environment=app_env.environment if app_env else None,
        app_env=app_env,
        current_range=pipeline_range(request.args.get("range")),
    )


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/env/<env_slug>/applications/<app_slug>/pipeline/stats",
)
@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/pipeline/stats",
    defaults={"env_slug": None},
)
@login_required
def project_application_pipeline_stats(
    org_slug: str, project_slug: str, app_slug: str, env_slug: str | None = None
):
    _org, project, application = _lookup_app_context(org_slug, project_slug, app_slug)
    app_env = _resolve_app_env(
        application, env_slug=env_slug, project=project, required=True
    )
    start, end = _requested_window()

    trend = collections.defaultdict(dict)
    stages = {}
    for stage in PIPELINE_STAGES:
        model, filters = pipeline_finished(stage, app_env.id, start, end)
        duration = row_duration(model)
        p50, p95 = percentiles(duration)
        day = func.date_trunc("day", model.updated)
        for row in db.session.query(day, p50, p95).filter(*filters).group_by(day):
            trend[row[0].isoformat()].update(
                {f"p50_{stage}": seconds(row[1]), f"p95_{stage}": seconds(row[2])}
            )
        overall = db.session.query(p50, p95).filter(*filters).one()
        stages[stage] = {"p50": seconds(overall[0]), "p95": seconds(overall[1])}

    _, deploy_filters = pipeline_finished("deploy", app_env.id, start, end)
    day = func.date_trunc("day", Deployment.updated)
    throughput = [
        {"date": row[0].isoformat(), "success": row[1], "failure": row[2]}
        for row in db.session.query(
            day,
            func.count().filter(Deployment.complete),
            func.count().filter(Deployment.error),
        )
        .filter(*deploy_filters)
        .group_by(day)
        .order_by(day)
    ]

    total, succeeded, failed = (
        db.session.query(
            func.count(),
            func.count().filter(Deployment.complete),
            func.count().filter(Deployment.error),
        )
        .filter(*deploy_filters)
        .one()
    )

    run_time = percentiles(pipeline_run_duration())
    run_rows = (
        db.session.query(Deployment)
        .join(Release, Release.id == Deployment.release["id"].astext.cast(Uuid))
        .join(Image, Image.id == Release.image["id"].astext.cast(Uuid))
        .filter(*deploy_filters)
    )
    run_p50, run_p95 = run_rows.with_entities(*run_time).one()
    for row in run_rows.with_entities(day, *run_time).group_by(day):
        trend[cast(datetime.datetime, row[0]).isoformat()].update(
            {"p50_run": seconds(row[1]), "p95_run": seconds(row[2])}
        )

    return jsonify(
        {
            "summary": {
                "runs": total,
                "failed": failed,
                "success_rate": round(succeeded / total * 100, 1) if total else None,
                "run_p50": seconds(run_p50),
                "run_p95": seconds(run_p95),
            },
            "stages": stages,
            "throughput": throughput,
            "duration_trend": [{"date": k, **v} for k, v in sorted(trend.items())],
        }
    )


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/env/<env_slug>/applications/<app_slug>/pipeline/runs",
)
@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/pipeline/runs",
    defaults={"env_slug": None},
)
@login_required
def project_application_pipeline_runs(
    org_slug: str, project_slug: str, app_slug: str, env_slug: str | None = None
):
    _org, project, application = _lookup_app_context(org_slug, project_slug, app_slug)
    app_env = _resolve_app_env(
        application, env_slug=env_slug, project=project, required=True
    )

    limit = max(1, min(request.args.get("limit", 20, type=int), 200))
    offset = max(request.args.get("offset", 0, type=int), 0)
    if offset > 2**63 - 1:
        abort(400)

    start, end = _requested_window()
    query = Deployment.query.filter(
        Deployment.application_environment_id == app_env.id,
        Deployment.updated >= start,
    )
    if end is not None:
        query = query.filter(Deployment.updated < end)
    query = query.order_by(desc(Deployment.created))
    total_count = query.count()
    deployments = query.offset(offset).limit(limit).all()

    # Batch-fetch releases and images to avoid N+1 queries
    release_ids = {(d.release or {}).get("id") for d in deployments} - {None}
    releases: dict[str | None, Release] = {
        str(r.id): r for r in Release.query.filter(Release.id.in_(release_ids))
    }
    image_ids = {(r.image or {}).get("id") for r in releases.values()} - {None}
    images: dict[str | None, Image] = {
        str(i.id): i for i in Image.query.filter(Image.id.in_(image_ids))
    }

    def stage(obj, done):
        if obj is None:
            return None, "pending"
        status = "error" if obj.error else ("success" if done else "pending")
        return seconds(obj.duration_seconds), status

    runs = []
    for d in deployments:
        deploy_metadata = d.deploy_metadata or {}
        pipeline = bool(deploy_metadata.get("auto_deploy"))
        release_pipeline = not pipeline and bool(
            deploy_metadata.get("deploy_after_release")
        )
        rel = releases.get((d.release or {}).get("id"))
        img = images.get((rel.image or {}).get("id")) if rel else None
        image_duration, image_status = stage(img, img and img.built)
        release_duration, release_status = stage(rel, rel and rel.built)
        deploy_duration, deploy_status = stage(d, d.complete)
        if release_pipeline:
            image_duration = None
            total_duration = (
                seconds(release_duration + deploy_duration)
                if release_duration is not None and deploy_duration is not None
                else None
            )
        elif not pipeline:
            image_duration = None
            release_duration = None
            total_duration = deploy_duration
        elif (
            img is not None
            and rel is not None
            and image_duration is not None
            and release_duration is not None
            and deploy_duration is not None
        ):
            overlap = max(
                (
                    min(img.updated, rel.updated) - max(img.created, rel.created)
                ).total_seconds(),
                0.0,
            )
            total_duration = seconds(
                image_duration + release_duration + deploy_duration - overlap
            )
        else:
            total_duration = None
        runs.append(
            {
                "id": str(d.id),
                "image_duration": image_duration,
                "image_status": image_status,
                "release_duration": release_duration,
                "release_status": release_status,
                "deploy_duration": deploy_duration,
                "deploy_status": deploy_status,
                "total_duration": total_duration,
                "trigger_type": d.trigger_type,
                "sha": d.release_snapshot.commit_sha if d.release_snapshot else None,
                "started": iso_utc(
                    cast(
                        datetime.datetime,
                        d.created
                        if not pipeline
                        else (img.created if img else d.created),
                    )
                ),
                "finished": iso_utc(d.updated) if d.complete or d.error else None,
            }
        )

    return jsonify({"runs": runs, "total": total_count})


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/env/<env_slug>/applications/<app_slug>/pipeline/slowest",
)
@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/pipeline/slowest",
    defaults={"env_slug": None},
)
@login_required
def project_application_pipeline_slowest(
    org_slug: str, project_slug: str, app_slug: str, env_slug: str | None = None
):
    _org, project, application = _lookup_app_context(org_slug, project_slug, app_slug)
    app_env = _resolve_app_env(
        application, env_slug=env_slug, project=project, required=True
    )

    stage = request.args.get("stage", "image")
    if stage not in PIPELINE_STAGES:
        stage = "image"
    model, filters = pipeline_finished(stage, app_env.id, *_requested_window())
    rows = (
        model.query.filter(*filters)
        .order_by(desc(model.updated - model.created))
        .limit(10)
        .all()
    )

    items = []
    for r in rows:
        if isinstance(r, Deployment):
            snapshot = r.release_snapshot
        elif isinstance(r, Release):
            snapshot = r.image_snapshot
        else:
            snapshot = r
        items.append(
            {
                "id": str(r.id),
                "duration": seconds(r.duration_seconds),
                "sha": snapshot.commit_sha if snapshot else None,
                "created": iso_utc(r.created),
                "finished": iso_utc(r.updated),
            }
        )

    return jsonify({"stage": stage, "items": items})


@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/env/<env_slug>/applications/<app_slug>/pipeline/live",
)
@user_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/pipeline/live",
    defaults={"env_slug": None},
)
@login_required
def project_application_pipeline_live(
    org_slug: str, project_slug: str, app_slug: str, env_slug: str | None = None
) -> Response:
    _org, project, application = _lookup_app_context(org_slug, project_slug, app_slug)
    app_env = _resolve_app_env(
        application, env_slug=env_slug, project=project, required=True
    )
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    run = pipeline_live_run(app_env, org_slug, project_slug, app_slug, now)
    return jsonify({"server_time": iso_utc(now), "run": run})


@user_blueprint.route("/projects/<org_slug>/<project_slug>/pipeline")
@login_required
def project_pipeline(org_slug: str, project_slug: str) -> str:
    organization = (
        Organization.query.filter_by(slug=org_slug)
        .filter(Organization.deleted_at.is_(None))
        .first_or_404()
    )
    project = (
        Project.query.filter_by(organization_id=organization.id, slug=project_slug)
        .filter(Project.deleted_at.is_(None))
        .first_or_404()
    )
    if not ViewProjectPermission(project.id).can():
        abort(403)
    return render_template(
        "user/project_pipeline.html",
        organization=organization,
        project=project,
        current_range=pipeline_range(request.args.get("range")),
    )


@user_blueprint.route("/projects/<org_slug>/<project_slug>/pipeline/rollup")
@login_required
def project_pipeline_rollup(org_slug: str, project_slug: str) -> Response:
    organization = (
        Organization.query.filter_by(slug=org_slug)
        .filter(Organization.deleted_at.is_(None))
        .first_or_404()
    )
    project = (
        Project.query.filter_by(organization_id=organization.id, slug=project_slug)
        .filter(Project.deleted_at.is_(None))
        .first_or_404()
    )
    if not ViewProjectPermission(project.id).can():
        abort(403)

    start, end = _requested_window()
    inventory = pipeline_app_env_inventory(organization, project=project)
    stats = pipeline_rollup_stats(
        [meta["app_env_id"] for meta in inventory], start, end
    )
    return jsonify({"rows": pipeline_rollup_rows(inventory, stats)})


@user_blueprint.route("/organizations/<org_slug>/pipeline")
@login_required
def organization_pipeline(org_slug: str) -> str:
    organization = (
        Organization.query.filter_by(slug=org_slug)
        .filter(Organization.deleted_at.is_(None))
        .first_or_404()
    )
    if not ViewOrganizationPermission(organization.id).can():
        abort(403)
    return render_template(
        "user/organization_pipeline.html",
        organization=organization,
        current_range=pipeline_range(request.args.get("range")),
    )


@user_blueprint.route("/organizations/<org_slug>/pipeline/rollup")
@login_required
def organization_pipeline_rollup(org_slug: str) -> Response:
    organization = (
        Organization.query.filter_by(slug=org_slug)
        .filter(Organization.deleted_at.is_(None))
        .first_or_404()
    )
    if not ViewOrganizationPermission(organization.id).can():
        abort(403)

    start, end = _requested_window()
    inventory = pipeline_app_env_inventory(organization)
    app_env_ids = [meta["app_env_id"] for meta in inventory]
    stats = pipeline_rollup_stats(app_env_ids, start, end)
    summary = pipeline_rollup_summary(app_env_ids, start, end)
    return jsonify({"rows": pipeline_rollup_rows(inventory, stats), "summary": summary})


@user_blueprint.route("/infra/pipeline")
@login_required
def infra_pipeline() -> str:
    _require_admin()
    return render_template(
        "user/infra_pipeline.html",
        current_range=pipeline_range(request.args.get("range")),
    )


@user_blueprint.route("/infra/pipeline/rollup")
@login_required
def infra_pipeline_rollup() -> Response:
    _require_admin()

    start, end = _requested_window()
    inventory = pipeline_app_env_inventory_all()
    app_env_ids = [meta["app_env_id"] for meta in inventory]
    stats = pipeline_rollup_stats(app_env_ids, start, end)
    summary = pipeline_rollup_summary(app_env_ids, start, end)

    active = [meta for meta in inventory if stats[meta["app_env_id"]]["runs"] > 0]
    inactive = [meta for meta in inventory if stats[meta["app_env_id"]]["runs"] == 0]
    inactive_total = len(inactive)
    include_inactive = (
        request.args.get("include_inactive") == "1"
        or inactive_total <= INFRA_PIPELINE_INACTIVE_LIMIT
    )
    shown = active + inactive if include_inactive else active

    return jsonify(
        {
            "rows": pipeline_rollup_rows(shown, stats),
            "summary": summary,
            "inactive_total": inactive_total,
            "inactive_included": include_inactive,
        }
    )
