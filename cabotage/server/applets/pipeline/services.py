import datetime
import uuid
from collections.abc import Sequence
from typing import SupportsFloat, TypedDict, cast

from flask import url_for
from sqlalchemy import ColumnElement, Uuid, case, desc, func, or_
from sqlalchemy.orm.attributes import InstrumentedAttribute

from cabotage.server import db
from cabotage.server.applets.pipeline.constants import (
    PIPELINE_HANDOFF,
    PIPELINE_RANGES,
    PIPELINE_STALE,
)
from cabotage.server.models.auth import Organization
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Deployment,
    Environment,
    Image,
    Project,
    Release,
)
from cabotage.utils.datetimes import iso_utc

PIPELINE_STAGES = {
    "image": (Image, Image.built),
    "release": (Release, Release.built),
    "deploy": (Deployment, Deployment.complete),
}
"""Stage -> (model, column marking success); error marks failure for all stages."""


def pipeline_range(range_key: str | None) -> str:
    """The requested range key if it's a known range, else the 30-day default."""
    return range_key if range_key in PIPELINE_RANGES else "30d"


def pipeline_window(
    range_key: str | None,
    since: str | None,
    until: str | None,
) -> tuple[datetime.datetime, datetime.datetime | None]:
    """Start and optional end of the requested window.

    `since`/`until` (inclusive UTC days) narrow a brushed window; otherwise
    the window is the trailing range. Raises OverflowError when the window
    would end past the last representable day.
    """
    try:
        since_day = datetime.date.fromisoformat(since or "")
        until_day = datetime.date.fromisoformat(until or "")
    except ValueError:
        now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
        days = PIPELINE_RANGES[pipeline_range(range_key)]
        return now - datetime.timedelta(days=days), None
    start = datetime.datetime.combine(min(since_day, until_day), datetime.time())
    end = datetime.datetime.combine(max(since_day, until_day), datetime.time())
    return start, end + datetime.timedelta(days=1)


def pipeline_finished(
    stage: str,
    app_env_id: uuid.UUID,
    start: datetime.datetime,
    end: datetime.datetime | None,
) -> tuple[type[Image | Release | Deployment], list[ColumnElement[bool]]]:
    """Model and filters for a stage's finished rows within the window."""
    model, done = PIPELINE_STAGES[stage]
    filters = [
        model.application_environment_id == app_env_id,
        or_(done, model.error),
        model.updated >= start,
    ]
    if end is not None:
        filters.append(model.updated < end)
    return model, filters


def row_duration(
    model: type[Image | Release | Deployment],
) -> ColumnElement[int]:
    """Seconds between a row's creation and its last update."""
    return func.extract("epoch", model.updated - model.created)


def pipeline_run_duration() -> ColumnElement[int]:
    """Deploy-only time, or build and deploy time with shared builds counted once."""
    is_build_pipeline = Deployment.deploy_metadata["auto_deploy"].astext == "true"
    image_release_overlap = func.greatest(
        0.0,
        func.extract(
            "epoch",
            func.least(Image.updated, Release.updated)
            - func.greatest(Image.created, Release.created),
        ),
    )
    return case(
        (
            is_build_pipeline,
            row_duration(Image)
            + row_duration(Release)
            + row_duration(Deployment)
            - image_release_overlap,
        ),
        else_=row_duration(Deployment),
    )


def percentiles(
    duration: ColumnElement[int],
) -> tuple[ColumnElement[float], ColumnElement[float]]:
    """p50/p95 aggregates over a duration expression."""
    return (
        func.percentile_cont(0.5).within_group(duration),
        func.percentile_cont(0.95).within_group(duration),
    )


def seconds(value: SupportsFloat | None) -> float | None:
    return round(float(value), 2) if value is not None else None


class _PipelineStage(TypedDict):
    id: str
    label: str
    status: str
    started: str | None
    finished: str | None
    url: str
    reused: bool


class _PipelineRun(TypedDict):
    status: str
    sha: str | None
    started: str | None
    finished: str | None
    image: _PipelineStage | None
    release: _PipelineStage | None
    deploy: _PipelineStage | None


def pipeline_live_run(
    app_env: ApplicationEnvironment,
    org_slug: str,
    project_slug: str,
    app_slug: str,
    now: datetime.datetime,
) -> _PipelineRun | None:
    """Build the live-run payload for an app environment's pipeline strip.

    Walks back from the newest image/release/deployment to find the chain of
    pipeline objects making up the most recent run. Shared by the /live
    polling endpoint and the overview page's server-rendered initial state.
    """
    # Anchor on the newest pipeline object and walk back along its links;
    # max() keeps the first of equal timestamps, so ties go to the later stage.
    newest = [
        model.query.filter(model.application_environment_id == app_env.id)
        .order_by(desc(model.created))
        .first()
        for model in (Deployment, Release, Image)
    ]
    anchor = max(
        (obj for obj in newest if obj is not None),
        key=lambda obj: obj.created,
        default=None,
    )
    if anchor is None:
        return None

    deploy = anchor if isinstance(anchor, Deployment) else None
    release = anchor if isinstance(anchor, Release) else None
    image = anchor if isinstance(anchor, Image) else None
    if deploy is not None and (deploy.deploy_metadata or {}).get("auto_deploy"):
        release = cast(
            "Release | None",
            Release.query.filter_by(id=(deploy.release or {}).get("id")).first(),
        )
    if release is not None:
        image = cast(
            "Image | None",
            Image.query.filter_by(id=(release.image or {}).get("id")).first(),
        )

    def times(
        obj: Image | Release | Deployment,
    ) -> tuple[datetime.datetime, datetime.datetime]:
        # The Timestamp mixin types created/updated as Column[datetime].
        return cast(datetime.datetime, obj.created), cast(
            datetime.datetime, obj.updated
        )

    def stage(
        obj: Image | Release | Deployment,
        done: bool,
        url: str,
        label: str,
        reused: bool = False,
    ) -> _PipelineStage:
        created, updated = times(obj)
        if obj.error:
            status = "error"
        elif done:
            status = "success"
        elif now - created > PIPELINE_STALE:
            status = "stalled"
        else:
            status = "running"
        return {
            "id": str(obj.id),
            "label": label,
            "status": status,
            "started": iso_utc(created),
            "finished": iso_utc(updated) if done or obj.error else None,
            "url": url,
            "reused": reused,
        }

    # A release can reuse an image that finished building long before it was
    # created (e.g. the only built image for an app that never auto-builds);
    # that image isn't really part of this run and shouldn't anchor its timing.
    image_reused = (
        image is not None
        and release is not None
        and times(release)[0] - times(image)[1] > PIPELINE_HANDOFF
    )

    objs = [obj for obj in (image, release, deploy) if obj is not None]
    timed_objs = [obj for obj in objs if not (obj is image and image_reused)]
    last = objs[-1]
    last_created, last_updated = times(last)
    failed = next((obj for obj in objs if obj.error), None)
    last_done = last.complete if isinstance(last, Deployment) else last.built
    finished: datetime.datetime | None = None
    if failed is not None:
        status, finished = "error", times(failed)[1]
    elif last_done and (
        isinstance(last, Deployment) or now - last_updated > PIPELINE_HANDOFF
    ):
        status, finished = "success", last_updated
    elif not last_done and now - last_created > PIPELINE_STALE:
        status = "stalled"
    else:
        status = "running"

    sha = cast("str | None", image.commit_sha) if image is not None else None
    if sha in (None, "null") and deploy is not None and deploy.release_snapshot:
        sha = cast("str | None", deploy.release_snapshot.commit_sha)

    image_stage = (
        stage(
            image,
            image.built,
            url_for(
                "user.image_detail",
                org_slug=org_slug,
                project_slug=project_slug,
                app_slug=app_slug,
                image_id=image.id,
            ),
            f"Image v{image.version}",
            reused=image_reused,
        )
        if image is not None
        else None
    )
    release_stage = (
        stage(
            release,
            release.built,
            url_for(
                "user.release_detail",
                org_slug=org_slug,
                project_slug=project_slug,
                app_slug=app_slug,
                release_id=release.id,
            ),
            f"Release v{release.version}",
        )
        if release is not None
        else None
    )
    deploy_stage = (
        stage(
            deploy,
            deploy.complete,
            url_for(
                "user.deployment_detail",
                org_slug=org_slug,
                project_slug=project_slug,
                app_slug=app_slug,
                deployment_id=deploy.id,
            ),
            f"Deploy v{release.version}" if release else "Deploy",
        )
        if deploy is not None
        else None
    )
    return {
        "status": status,
        "sha": sha[:7] if sha and sha != "null" else None,
        "started": iso_utc(min(times(obj)[0] for obj in timed_objs)),
        "finished": iso_utc(finished),
        "image": image_stage,
        "release": release_stage,
        "deploy": deploy_stage,
    }


def _pipeline_finished_many(
    model: type[Image | Release | Deployment],
    done: ColumnElement[bool] | InstrumentedAttribute[bool],
    app_env_ids: list[uuid.UUID],
    start: datetime.datetime,
    end: datetime.datetime | None,
) -> list[ColumnElement[bool]]:
    """Filters for a stage's finished rows across several app environments."""
    filters = [
        model.application_environment_id.in_(app_env_ids),
        or_(done, model.error),
        model.updated >= start,
    ]
    if end is not None:
        filters.append(model.updated < end)
    return filters


class _PipelineRollupMeta(TypedDict):
    app_env_id: uuid.UUID
    project_slug: str
    project_name: str
    app_slug: str
    app_name: str
    env_slug: str
    env_name: str
    url: str


class _InfraPipelineMeta(_PipelineRollupMeta):
    org_slug: str
    org_name: str


class _PipelineRollupAgg(TypedDict):
    runs: int
    failed: int
    success_rate: float | None
    p50: float | None
    p95: float | None
    last_status: str | None
    last_at: str | None
    sparkline: list[dict[str, object]]


def pipeline_app_env_inventory(
    organization: Organization,
    project: Project | None = None,
) -> list[_PipelineRollupMeta]:
    """List active application environments for a project, or for every
    project in an organization, to drive the pipeline rollup tables.

    Every active (application, environment) enrollment is included, even
    ones with no runs, so the table doubles as an inventory.
    """
    query = (
        db.session.query(ApplicationEnvironment, Application, Environment, Project)
        .join(Application, Application.id == ApplicationEnvironment.application_id)
        .join(Environment, Environment.id == ApplicationEnvironment.environment_id)
        .join(Project, Project.id == Application.project_id)
        .filter(
            ApplicationEnvironment.deleted_at.is_(None),
            Application.deleted_at.is_(None),
            Environment.deleted_at.is_(None),
            Project.deleted_at.is_(None),
        )
    )
    if project is not None:
        query = query.filter(Application.project_id == project.id)
    else:
        query = query.filter(Project.organization_id == organization.id)
    query = query.order_by(
        Project.slug, Application.slug, Environment.sort_order, Environment.slug
    )

    inventory: list[_PipelineRollupMeta] = []
    for app_env, application, environment, proj in query:
        inventory.append(
            {
                "app_env_id": app_env.id,
                "project_slug": proj.slug,
                "project_name": proj.name,
                "app_slug": application.slug,
                "app_name": application.name,
                "env_slug": environment.slug,
                "env_name": environment.name,
                "url": url_for(
                    "user.project_application_pipeline",
                    org_slug=organization.slug,
                    project_slug=proj.slug,
                    app_slug=application.slug,
                    env_slug=app_env.env_slug_for_paths,
                ),
            }
        )
    return inventory


def pipeline_app_env_inventory_all() -> list[_InfraPipelineMeta]:
    """List every active application environment across every organization,
    to drive the platform-wide infra pipeline rollup table.

    Mirrors `pipeline_app_env_inventory` but spans organizations, in a
    single query (never one query per org).
    """
    query = (
        db.session.query(
            ApplicationEnvironment, Application, Environment, Project, Organization
        )
        .join(Application, Application.id == ApplicationEnvironment.application_id)
        .join(Environment, Environment.id == ApplicationEnvironment.environment_id)
        .join(Project, Project.id == Application.project_id)
        .join(Organization, Organization.id == Project.organization_id)
        .filter(
            ApplicationEnvironment.deleted_at.is_(None),
            Application.deleted_at.is_(None),
            Environment.deleted_at.is_(None),
            Project.deleted_at.is_(None),
            Organization.deleted_at.is_(None),
        )
        .order_by(
            Organization.slug,
            Project.slug,
            Application.slug,
            Environment.sort_order,
            Environment.slug,
        )
    )

    inventory: list[_InfraPipelineMeta] = []
    for app_env, application, environment, proj, org in query:
        inventory.append(
            {
                "app_env_id": app_env.id,
                "org_slug": org.slug,
                "org_name": org.name,
                "project_slug": proj.slug,
                "project_name": proj.name,
                "app_slug": application.slug,
                "app_name": application.name,
                "env_slug": environment.slug,
                "env_name": environment.name,
                "url": url_for(
                    "user.project_application_pipeline",
                    org_slug=org.slug,
                    project_slug=proj.slug,
                    app_slug=application.slug,
                    env_slug=app_env.env_slug_for_paths,
                ),
            }
        )
    return inventory


def pipeline_rollup_stats(
    app_env_ids: list[uuid.UUID],
    start: datetime.datetime,
    end: datetime.datetime | None,
) -> dict[uuid.UUID, _PipelineRollupAgg]:
    """Aggregate run counts, run-time percentiles, last-run status, and a
    per-day deploy sparkline for each application environment.

    Uses a fixed number of GROUP BY queries regardless of how many
    application environments are in scope (never one query per row).
    """
    rows: dict[uuid.UUID, _PipelineRollupAgg] = {
        ae_id: {
            "runs": 0,
            "failed": 0,
            "success_rate": None,
            "p50": None,
            "p95": None,
            "last_status": None,
            "last_at": None,
            "sparkline": [],
        }
        for ae_id in app_env_ids
    }
    if not app_env_ids:
        return rows

    aid = Deployment.application_environment_id
    deploy_filters = _pipeline_finished_many(
        Deployment, Deployment.complete, app_env_ids, start, end
    )

    # Deploy totals per app env: "success rate" mirrors the app page's
    # definition of deploys that went live.
    for ae_id, total, succeeded, failed in (
        db.session.query(
            aid,
            func.count(),
            func.count().filter(Deployment.complete),
            func.count().filter(Deployment.error),
        )
        .filter(*deploy_filters)
        .group_by(aid)
    ):
        row = rows[ae_id]
        row["runs"] = total
        row["failed"] = failed
        row["success_rate"] = round(succeeded / total * 100, 1) if total else None

    # Deploys per day, for the row sparkline.
    day = func.date_trunc("day", Deployment.updated)
    for ae_id, bucket, count, day_failed in (
        db.session.query(
            aid,
            day,
            func.count(),
            func.count().filter(Deployment.error),
        )
        .filter(*deploy_filters)
        .group_by(aid, day)
        .order_by(day)
    ):
        rows[ae_id]["sparkline"].append(
            {
                "date": cast(datetime.datetime, bucket).date().isoformat(),
                "count": count,
                "failed": day_failed,
            }
        )

    run_p50, run_p95 = percentiles(pipeline_run_duration())
    run_rows = (
        db.session.query(aid, run_p50, run_p95)
        .select_from(Deployment)
        .join(Release, Release.id == Deployment.release["id"].astext.cast(Uuid))
        .join(Image, Image.id == Release.image["id"].astext.cast(Uuid))
        .filter(*deploy_filters)
        .group_by(aid)
    )
    for ae_id, p50, p95 in run_rows:
        rows[ae_id]["p50"] = seconds(p50)
        rows[ae_id]["p95"] = seconds(p95)

    # Last run: the most recently finished stage object per app env, across
    # all three stages (one DISTINCT ON query per stage, never per row).
    latest: dict[uuid.UUID, tuple[datetime.datetime, bool]] = {}
    for model, done in PIPELINE_STAGES.values():
        stage_aid = model.application_environment_id
        stage_filters = _pipeline_finished_many(model, done, app_env_ids, start, end)
        stage_rows = (
            db.session.query(stage_aid, model.updated, model.error)
            .filter(*stage_filters)
            .order_by(stage_aid, model.updated.desc())
            .distinct(stage_aid)
        )
        for ae_id, updated, error in stage_rows:
            prior = latest.get(ae_id)
            if prior is None or updated > prior[0]:
                latest[ae_id] = (updated, error)
    for ae_id, (updated, error) in latest.items():
        rows[ae_id]["last_status"] = "fail" if error else "ok"
        rows[ae_id]["last_at"] = iso_utc(updated)

    return rows


class _PipelineRollupSummary(TypedDict):
    runs: int
    failed: int
    change_failure_rate: float | None
    run_p50: float | None
    run_p95: float | None
    active_app_envs: int


def pipeline_rollup_summary(
    app_env_ids: list[uuid.UUID],
    start: datetime.datetime,
    end: datetime.datetime | None,
) -> _PipelineRollupSummary:
    """Deploys in range, change failure rate, and pooled run-time percentiles
    across every application environment in scope.

    This is computed over all matching runs together, not averaged from the
    per-row medians, so it reflects the organization's actual run mix.
    """
    if not app_env_ids:
        return {
            "runs": 0,
            "failed": 0,
            "change_failure_rate": None,
            "run_p50": None,
            "run_p95": None,
            "active_app_envs": 0,
        }

    filters = _pipeline_finished_many(
        Deployment, Deployment.complete, app_env_ids, start, end
    )
    total, failed, active_app_envs = (
        db.session.query(
            func.count(),
            func.count().filter(Deployment.error),
            func.count(func.distinct(Deployment.application_environment_id)),
        )
        .filter(*filters)
        .one()
    )
    run_p50, run_p95 = percentiles(pipeline_run_duration())
    p50, p95 = (
        db.session.query(run_p50, run_p95)
        .select_from(Deployment)
        .join(Release, Release.id == Deployment.release["id"].astext.cast(Uuid))
        .join(Image, Image.id == Release.image["id"].astext.cast(Uuid))
        .filter(*filters)
        .one()
    )
    return {
        "runs": total,
        "failed": failed,
        "change_failure_rate": round(failed / total * 100, 1) if total else None,
        "run_p50": seconds(p50),
        "run_p95": seconds(p95),
        "active_app_envs": active_app_envs,
    }


def pipeline_rollup_rows(
    inventory: Sequence[_PipelineRollupMeta],
    stats: dict[uuid.UUID, _PipelineRollupAgg],
) -> list[dict[str, object]]:
    """Combine inventory metadata with aggregated stats into JSON-ready rows."""
    rows: list[dict[str, object]] = []
    for meta in inventory:
        agg = stats[meta["app_env_id"]]
        row: dict[str, object] = dict(meta)
        row["app_env_id"] = str(meta["app_env_id"])
        row.update(agg)
        rows.append(row)
    return rows
