import re

import requests

from celery import shared_task
from dxf import DXF
from flask import current_app

from cabotage.server.models.projects import Application
from cabotage.utils.docker_auth import generate_docker_registry_jwt


def natsort(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


@shared_task()
def prune_images(dry_run=False):
    for app in reversed(Application.query.all()):
        if app.project.organization is not None:
            organization_slug = app.project.organization.slug
            project_slug = app.project.slug
            application_slug = app.slug
            repository_name = (
                f"cabotage/{organization_slug}/{project_slug}/{application_slug}"
            )

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
                    [
                        a
                        for a in aliases
                        if a.startswith("image-") and a != "image-buildcache"
                    ],
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
                            pass
                    else:
                        print(f"retaining {repository_name}:{a}")
                for a in release_aliases:
                    if a in release_aliases[: -5 or None]:
                        try:
                            print(f"deleting {repository_name}:{a}")
                            if not dry_run:
                                client.del_alias(a)
                        except requests.exceptions.HTTPError as e:
                            print(e)
                            pass
                    else:
                        print(f"retaining {repository_name}:{a}")
            except requests.exceptions.HTTPError as e:
                print(e)
                pass
