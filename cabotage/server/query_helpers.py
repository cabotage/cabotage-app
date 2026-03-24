"""Shared query helpers for batch-loading status sets and related objects.

These helpers extract duplicated N+1-avoidance patterns from view functions
into reusable functions.
"""

import uuid as _uuid

from sqlalchemy import and_, case, func, or_

from cabotage.server import db
from cabotage.server.models.projects import (
    ApplicationEnvironment,
    Deployment,
    Image,
    Release,
)


def compute_app_status_sets(app_ids):
    """Batch-compute deployed/errored/building status for a list of application IDs.

    Queries via the default ApplicationEnvironment (k8s_identifier IS NULL).

    Returns dict with keys: deployed_app_ids, errored_app_ids, building_app_ids
    """
    deployed_app_ids = set()
    errored_app_ids = set()
    building_app_ids = set()

    if not app_ids:
        return {
            "deployed_app_ids": deployed_app_ids,
            "errored_app_ids": errored_app_ids,
            "building_app_ids": building_app_ids,
        }

    # Apps with any running or completed deployment
    deployed_app_ids = {
        row[0]
        for row in db.session.query(Deployment.application_id)
        .join(ApplicationEnvironment)
        .filter(
            Deployment.application_id.in_(app_ids),
            ApplicationEnvironment.deleted_at.is_(None),
            ApplicationEnvironment.k8s_identifier.is_(None),
            or_(
                Deployment.complete == True,  # noqa: E712
                and_(
                    Deployment.complete == False,  # noqa: E712
                    Deployment.error == False,  # noqa: E712
                ),
            ),
        )
        .distinct()
    }

    # Apps where latest error image version > latest built image version
    error_sub = (
        db.session.query(
            Image.application_id,
            func.max(Image.version).label("v"),
        )
        .join(ApplicationEnvironment)
        .filter(
            Image.application_id.in_(app_ids),
            ApplicationEnvironment.deleted_at.is_(None),
            ApplicationEnvironment.k8s_identifier.is_(None),
            Image.error == True,  # noqa: E712
        )
        .group_by(Image.application_id)
        .subquery()
    )
    built_sub = (
        db.session.query(
            Image.application_id,
            func.max(Image.version).label("v"),
        )
        .join(ApplicationEnvironment)
        .filter(
            Image.application_id.in_(app_ids),
            ApplicationEnvironment.deleted_at.is_(None),
            ApplicationEnvironment.k8s_identifier.is_(None),
            Image.built == True,  # noqa: E712
        )
        .group_by(Image.application_id)
        .subquery()
    )
    errored_app_ids = {
        row[0]
        for row in db.session.query(error_sub.c.application_id)
        .outerjoin(
            built_sub,
            error_sub.c.application_id == built_sub.c.application_id,
        )
        .filter(or_(built_sub.c.v.is_(None), error_sub.c.v > built_sub.c.v))
    }

    # Apps with any in-progress image build
    building_app_ids = {
        row[0]
        for row in db.session.query(Image.application_id)
        .join(ApplicationEnvironment)
        .filter(
            Image.application_id.in_(app_ids),
            ApplicationEnvironment.deleted_at.is_(None),
            ApplicationEnvironment.k8s_identifier.is_(None),
            Image.built == False,  # noqa: E712
            Image.error == False,  # noqa: E712
        )
        .distinct()
    }

    return {
        "deployed_app_ids": deployed_app_ids,
        "errored_app_ids": errored_app_ids,
        "building_app_ids": building_app_ids,
    }


def compute_ae_status_sets(ae_ids):
    """Batch-compute deployment/image status for a list of ApplicationEnvironment IDs.

    Returns dict with keys: deploying_ae_ids, completed_ae_ids, running_ae_ids,
    building_ae_ids, errored_ae_ids, last_deploy_by_ae, deploy_count
    """
    deploying_ae_ids = set()
    completed_ae_ids = set()
    running_ae_ids = set()
    building_ae_ids = set()
    errored_ae_ids = set()
    last_deploy_by_ae = {}
    deploy_count = 0

    if not ae_ids:
        return {
            "deploying_ae_ids": deploying_ae_ids,
            "completed_ae_ids": completed_ae_ids,
            "running_ae_ids": running_ae_ids,
            "building_ae_ids": building_ae_ids,
            "errored_ae_ids": errored_ae_ids,
            "last_deploy_by_ae": last_deploy_by_ae,
            "deploy_count": deploy_count,
        }

    # Latest deployment status per ae (for deploying/running)
    latest_deploy_created_sub = (
        db.session.query(
            Deployment.application_environment_id,
            func.max(Deployment.created).label("max_created"),
        )
        .filter(Deployment.application_environment_id.in_(ae_ids))
        .group_by(Deployment.application_environment_id)
        .subquery()
    )
    latest_deploys = (
        db.session.query(
            Deployment.application_environment_id,
            Deployment.complete,
            Deployment.error,
        )
        .join(
            latest_deploy_created_sub,
            and_(
                Deployment.application_environment_id
                == latest_deploy_created_sub.c.application_environment_id,
                Deployment.created == latest_deploy_created_sub.c.max_created,
            ),
        )
        .all()
    )
    deploying_ae_ids = {r[0] for r in latest_deploys if not r[1] and not r[2]}
    running_ae_ids = {r[0] for r in latest_deploys if r[1] or not r[2]}

    # Completed deploy stats: count + last deploy per ae
    deploy_stats = (
        db.session.query(
            Deployment.application_environment_id,
            func.count(Deployment.id),
            func.max(Deployment.created),
        )
        .filter(
            Deployment.application_environment_id.in_(ae_ids),
            Deployment.complete == True,  # noqa: E712
        )
        .group_by(Deployment.application_environment_id)
        .all()
    )
    completed_ae_ids = {r[0] for r in deploy_stats}
    last_deploy_by_ae = {r[0]: r[2] for r in deploy_stats}
    deploy_count = sum(r[1] for r in deploy_stats)

    # Image stats: one query for error, built, and building checks
    image_stats = (
        db.session.query(
            Image.application_environment_id,
            func.max(
                case((Image.error == True, Image.version), else_=None)  # noqa: E712
            ).label("max_error_v"),
            func.max(
                case((Image.built == True, Image.version), else_=None)  # noqa: E712
            ).label("max_built_v"),
            func.count(
                case(
                    (
                        and_(
                            Image.built == False,  # noqa: E712
                            Image.error == False,  # noqa: E712
                        ),
                        1,
                    )
                )
            ).label("building_count"),
        )
        .filter(Image.application_environment_id.in_(ae_ids))
        .group_by(Image.application_environment_id)
        .all()
    )
    errored_ae_ids = {
        r[0] for r in image_stats if r[1] is not None and (r[2] is None or r[1] > r[2])
    }
    building_ae_ids = {r[0] for r in image_stats if r[3] > 0}

    return {
        "deploying_ae_ids": deploying_ae_ids,
        "completed_ae_ids": completed_ae_ids,
        "running_ae_ids": running_ae_ids,
        "building_ae_ids": building_ae_ids,
        "errored_ae_ids": errored_ae_ids,
        "last_deploy_by_ae": last_deploy_by_ae,
        "deploy_count": deploy_count,
    }


class RelatedObjectResolver:
    """Caches Release/Image lookups from JSONB foreign keys.

    Avoids cascading queries like deployment.release_object → release.image_object.
    """

    def __init__(self, images=None, releases=None):
        self._release_cache = {}
        self._image_cache = {i.id: i for i in (images or [])}
        self._all_releases = releases or []

    def warm_caches(self, deployments, releases):
        """Pre-resolve all Release/Image references from deployments and releases."""
        for d in deployments:
            self._get_release(d)
        for r in releases:
            self._get_image_for_release(r)

    def build_lookup_dicts(self):
        """Return (release_by_id, image_by_id) dicts keyed by string UUID."""
        release_by_id = {str(k): v for k, v in self._release_cache.items() if v}
        image_by_id = {str(k): v for k, v in self._image_cache.items() if v}
        return release_by_id, image_by_id

    def get_release(self, deploy):
        """Get the Release object referenced by a Deployment's JSONB field."""
        return self._get_release(deploy)

    def get_image_for_release(self, rel):
        """Get the Image object referenced by a Release's JSONB field."""
        return self._get_image_for_release(rel)

    def _get_release(self, deploy):
        if not deploy or not deploy.release:
            return None
        rid = deploy.release.get("id")
        if not rid:
            return None
        rid = _uuid.UUID(rid) if isinstance(rid, str) else rid
        if rid not in self._release_cache:
            found = next((r for r in self._all_releases if r.id == rid), None)
            if found is None:
                found = Release.query.get(rid)
            self._release_cache[rid] = found
        return self._release_cache[rid]

    def _get_image_for_release(self, rel):
        if not rel or not rel.image:
            return None
        iid = rel.image.get("id")
        if not iid:
            return None
        iid = _uuid.UUID(iid) if isinstance(iid, str) else iid
        if iid not in self._image_cache:
            self._image_cache[iid] = Image.query.get(iid)
        return self._image_cache[iid]


def extract_latest_variants(images, releases, deployments):
    """Extract latest_* variants from pre-fetched lists.

    Returns dict with keys: latest_image, latest_image_built, latest_image_error,
    latest_image_building, latest_release, latest_release_built, latest_release_building,
    latest_deployment, latest_deployment_completed, has_releases
    """
    return {
        "latest_image": images[0] if images else None,
        "latest_image_built": next((i for i in images if i.built), None),
        "latest_image_error": next((i for i in images if i.error), None),
        "latest_image_building": next(
            (i for i in images if not i.built and not i.error), None
        ),
        "latest_release": releases[0] if releases else None,
        "latest_release_built": next((r for r in releases if r.built), None),
        "latest_release_building": next(
            (r for r in releases if not r.built and not r.error), None
        ),
        "latest_deployment": deployments[0] if deployments else None,
        "latest_deployment_completed": next(
            (d for d in deployments if d.complete), None
        ),
        "has_releases": len(releases) > 0,
    }


def compute_process_counts(releases, resolver):
    """Compute service process count per release (excludes release/postdeploy commands).

    Returns {str(release_id): int}.
    """
    release_proc_counts = {}
    for r in releases:
        img = resolver.get_image_for_release(r)
        if r.built and img and img.processes:
            release_proc_counts[str(r.id)] = sum(
                1
                for k in img.processes
                if not k.startswith("release") and not k.startswith("postdeploy")
            )
    return release_proc_counts


def split_image_processes(image):
    """Split image.processes into (service_procs, release_cmds, postdeploy_cmds).

    Mirrors Release.processes / release_commands / postdeploy_commands without
    triggering an image_object query.
    """
    if not image or not image.processes:
        return {}, {}, {}
    all_procs = image.processes
    service_procs = {
        k: v
        for k, v in all_procs.items()
        if not (k.startswith("release") or k.startswith("postdeploy"))
    }
    release_cmds = {k: v for k, v in all_procs.items() if k.startswith("release")}
    postdeploy_cmds = {k: v for k, v in all_procs.items() if k.startswith("postdeploy")}
    return service_procs, release_cmds, postdeploy_cmds
