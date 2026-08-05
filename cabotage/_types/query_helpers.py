from __future__ import annotations

from typing import TypedDict, TYPE_CHECKING


if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID
    from cabotage.server.models.projects import Deployment, Image, Release

    class AppStatus(TypedDict):
        deployed_app_ids: set[UUID]
        errored_app_ids: set[UUID]
        building_app_ids: set[UUID]

    class AppEnvStatusSet(TypedDict):
        deploying_ae_ids: set[UUID]
        completed_ae_ids: set[UUID]
        running_ae_ids: set[UUID]
        building_ae_ids: set[UUID]
        errored_ae_ids: set[UUID]
        last_deploy_by_ae: dict[UUID, datetime]
        deploy_count: int

    class ConfigItem(TypedDict):
        version_id: int
        secret: bool
        buildtime: bool

    class Host(TypedDict):
        id: str
        hostname: str
        tls_enabled: bool
        is_auto_generated: bool

    class Path(TypedDict):
        id: str
        path: str
        path_type: str
        target_process_name: str

    class IngressItem(TypedDict, total=False):
        hosts: list[Host]
        paths: list[Path]

        enabled: bool
        ingress_class_name: str
        backend_protocol: str
        proxy_connect_timeout: str
        proxy_read_timeout: str
        proxy_send_timeout: str
        proxy_body_size: str
        client_body_buffer_size: str
        proxy_request_buffering: str
        session_affinity: bool
        use_regex: bool
        allow_annotations: bool
        extra_annotations: dict[str, str]
        cluster_issuer: str
        force_ssl_redirect: bool
        service_upstream: bool
        tailscale_hostname: str
        tailscale_funnel: bool
        tailscale_tags: str

    type ChangeDetail = dict[str, list[str]]

    class ChangeDetails(TypedDict):
        config: ChangeDetail
        ingress: ChangeDetail

    class LatestVariants(TypedDict):
        latest_image: Image | None
        latest_image_built: Image | None
        latest_image_error: Image | None
        latest_image_building: Image | None
        latest_release: Release | None
        latest_release_built: Release | None
        latest_release_building: Release | None
        latest_deployment: Deployment | None
        latest_deployment_completed: Deployment | None
        has_releases: bool
