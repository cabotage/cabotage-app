import re

import requests
from celery import shared_task
from dxf import DXF
from flask import current_app

from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Deployment,
    Release,
)
from cabotage.utils.docker_auth import generate_docker_registry_jwt

# Release tags of this many recent successful deployments survive pruning so
# they stay available as rollback targets.
ROLLBACK_RETAINED_DEPLOYMENTS = 10


def natsort(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _rollback_release_aliases(app_env: ApplicationEnvironment) -> frozenset[str]:
    deployments = (
        app_env.deployments.filter_by(complete=True, error=False)
        .order_by(Deployment.created.desc())
        .limit(ROLLBACK_RETAINED_DEPLOYMENTS)
        .all()
    )
    release_ids = {d.release.get("id") for d in deployments if d.release}
    if not release_ids:
        return frozenset()
    versions = Release.query.with_entities(Release.version).filter(
        Release.id.in_(release_ids)
    )
    return frozenset(f"release-{version}" for (version,) in versions)


def _prune_repository(
    repository_name: str, dry_run: bool = False, retain: frozenset[str] = frozenset()
):
    def auth(dxf, response):
        dxf.token = generate_docker_registry_jwt(
            access=[
                {
                    "type": "repository",
                    "name": repository_name,
                    "actions": ["*"],
                }
            ]
        )

    registry = current_app.config["REGISTRY_BUILD"]
    registry_secure = current_app.config["REGISTRY_SECURE"]
    _tlsverify = False
    if registry_secure:
        _tlsverify = current_app.config["REGISTRY_VERIFY"]
        if _tlsverify == "True":
            _tlsverify = True
    client = DXF(
        host=registry,
        repo=repository_name,
        auth=auth,
        insecure=(not registry_secure),
        tlsverify=_tlsverify,
    )
    try:
        aliases = client.list_aliases()
        image_aliases = sorted(
            [a for a in aliases if a.startswith("image-") and a != "image-buildcache"],
            key=natsort,
        )
        release_aliases = sorted(
            [
                a
                for a in aliases
                if a.startswith("release-") and a != "release-buildcache"
            ],
            key=natsort,
        )
        for a in image_aliases:
            if a in image_aliases[: -5 or None]:
                try:
                    print(f"deleting {repository_name}:{a}")
                    if not dry_run:
                        client.del_alias(a)
                except requests.exceptions.HTTPError as e:
                    print(e)
            else:
                print(f"retaining {repository_name}:{a}")
        for a in release_aliases:
            if a in release_aliases[: -5 or None] and a not in retain:
                try:
                    print(f"deleting {repository_name}:{a}")
                    if not dry_run:
                        client.del_alias(a)
                except requests.exceptions.HTTPError as e:
                    print(e)
            else:
                print(f"retaining {repository_name}:{a}")
    except requests.exceptions.HTTPError as e:
        print(e)


@shared_task()
def prune_images(dry_run=False):
    for app in reversed(Application.query.all()):
        if app.project.organization is not None:
            app_env: ApplicationEnvironment
            for app_env in app.application_environments:
                _prune_repository(
                    app.registry_repository_name(app_env),
                    dry_run=dry_run,
                    retain=_rollback_release_aliases(app_env),
                )
