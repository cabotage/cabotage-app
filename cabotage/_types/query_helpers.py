from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import TypedDict, NotRequired, Literal

    class Host(TypedDict):
        id: str
        hostname: str
        tls_enabled: NotRequired[bool]
        is_auto_generated: NotRequired[bool]

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

    class ConfigItem(TypedDict, total=False):
        version_id: int
        secret: bool
        buildtime: bool

    type ConfigDiff = Literal[
        "value changed",
        "marked secret",
        "unmarked secret",
        "marked buildtime",
        "unmarked buildtime",
    ]

    class ChangeDetails(TypedDict):
        config: dict[str, list[ConfigDiff]]
        ingress: dict[str, list[str]]
