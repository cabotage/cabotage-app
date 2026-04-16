"""Tests for ingress rendering (nginx + tailscale)."""

from cabotage.celery.tasks.deploy import (
    _build_ingress_paths,
    render_ingress_object,
    render_service,
)

# ---------------------------------------------------------------------------
# Fake model objects that mimic Ingress/IngressSnapshot, Host, Path
# ---------------------------------------------------------------------------


class FakeHost:
    def __init__(self, hostname, tls_enabled=True, is_auto_generated=False):
        self.hostname = hostname
        self.tls_enabled = tls_enabled
        self.is_auto_generated = is_auto_generated


class FakePath:
    def __init__(self, path="/", path_type="Prefix", target_process_name="web"):
        self.path = path
        self.path_type = path_type
        self.target_process_name = target_process_name


class FakeIngress:
    def __init__(self, **kwargs):
        self.name = kwargs.get("name", "web")
        self.enabled = kwargs.get("enabled", True)
        self.ingress_class_name = kwargs.get("ingress_class_name", "nginx")
        self.backend_protocol = kwargs.get("backend_protocol", "HTTPS")
        self.proxy_connect_timeout = kwargs.get("proxy_connect_timeout", "10s")
        self.proxy_read_timeout = kwargs.get("proxy_read_timeout", "10s")
        self.proxy_send_timeout = kwargs.get("proxy_send_timeout", "10s")
        self.proxy_body_size = kwargs.get("proxy_body_size", "10M")
        self.client_body_buffer_size = kwargs.get("client_body_buffer_size", "1M")
        self.proxy_request_buffering = kwargs.get("proxy_request_buffering", "on")
        self.session_affinity = kwargs.get("session_affinity", False)
        self.use_regex = kwargs.get("use_regex", False)
        self.allow_annotations = kwargs.get("allow_annotations", False)
        self.extra_annotations = kwargs.get("extra_annotations", {})
        self.cluster_issuer = kwargs.get("cluster_issuer", "letsencrypt")
        self.force_ssl_redirect = kwargs.get("force_ssl_redirect", True)
        self.service_upstream = kwargs.get("service_upstream", True)
        self.tailscale_hostname = kwargs.get("tailscale_hostname", None)
        self.tailscale_funnel = kwargs.get("tailscale_funnel", False)
        self.tailscale_tags = kwargs.get("tailscale_tags", None)
        self.hosts = kwargs.get("hosts", [])
        self.paths = kwargs.get("paths", [])


class FakeRelease:
    """Minimal Release-like object for render_service."""

    class App:
        class Project:
            class Org:
                slug = "myorg"
                k8s_identifier = "myorg-abc123"

            organization = Org()
            slug = "myproject"
            k8s_identifier = "myproject-def456"

        project = Project()
        slug = "myapp"
        k8s_identifier = "myapp-ghi789"

    class AppEnv:
        class Env:
            slug = "production"
            k8s_identifier = "production-xyz"
            uses_environment_namespace = True

        k8s_identifier = "appenv-123"
        environment = Env()

    application = App()
    application_environment = AppEnv()
    processes = {"web": "gunicorn app:app", "worker": "celery -A app worker"}


LABELS = {
    "organization": "myorg",
    "project": "myproject",
    "application": "myapp",
    "app": "test-label",
}
RESOURCE_PREFIX = "proj-abc-app-def"


# ---------------------------------------------------------------------------
# _build_ingress_paths
# ---------------------------------------------------------------------------


class TestBuildIngressPaths:
    def test_explicit_paths(self):
        ing = FakeIngress(
            paths=[
                FakePath(path="/", target_process_name="web"),
                FakePath(path="/api", path_type="Prefix", target_process_name="web"),
            ]
        )
        paths = _build_ingress_paths(ing, RESOURCE_PREFIX, [])
        assert len(paths) == 2
        assert paths[0].path == "/"
        assert paths[1].path == "/api"
        # All paths use port name "https"
        assert paths[0].backend.service.port.name == "https"
        assert paths[1].backend.service.port.name == "https"

    def test_default_path_when_no_explicit(self):
        ing = FakeIngress(paths=[])
        paths = _build_ingress_paths(ing, RESOURCE_PREFIX, ["web", "worker"])
        assert len(paths) == 1
        assert paths[0].path == "/"
        assert paths[0].path_type == "Prefix"
        # Should target first web process
        assert paths[0].backend.service.name == f"{RESOURCE_PREFIX}-web"

    def test_no_paths_no_web_processes(self):
        ing = FakeIngress(paths=[])
        paths = _build_ingress_paths(ing, RESOURCE_PREFIX, ["worker", "beat"])
        assert paths is None

    def test_no_paths_no_processes(self):
        ing = FakeIngress(paths=[])
        paths = _build_ingress_paths(ing, RESOURCE_PREFIX, [])
        assert paths is None

    def test_backend_service_name(self):
        ing = FakeIngress(paths=[FakePath(target_process_name="web2")])
        paths = _build_ingress_paths(ing, RESOURCE_PREFIX)
        assert paths[0].backend.service.name == f"{RESOURCE_PREFIX}-web2"

    def test_port_always_named_https(self):
        ing = FakeIngress(paths=[FakePath()])
        paths = _build_ingress_paths(ing, RESOURCE_PREFIX)
        assert paths[0].backend.service.port.name == "https"
        assert paths[0].backend.service.port.number is None


# ---------------------------------------------------------------------------
# render_ingress_object — nginx
# ---------------------------------------------------------------------------


class TestRenderNginxIngress:
    def _make_nginx_ingress(self, **overrides):
        defaults = dict(
            ingress_class_name="nginx",
            hosts=[FakeHost("app.example.com")],
            paths=[FakePath()],
        )
        defaults.update(overrides)
        return FakeIngress(**defaults)

    def test_basic_nginx_ingress(self):
        ing = self._make_nginx_ingress()
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS, process_names=["web"])
        assert obj is not None
        assert obj.spec.ingress_class_name == "nginx"
        assert obj.metadata.name == f"{RESOURCE_PREFIX}-web"

    def test_disabled_returns_none(self):
        ing = self._make_nginx_ingress(enabled=False)
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert obj is None

    def test_nginx_annotations(self):
        ing = self._make_nginx_ingress()
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        ann = obj.metadata.annotations
        assert "nginx.ingress.kubernetes.io/backend-protocol" in ann
        assert ann["nginx.ingress.kubernetes.io/backend-protocol"] == "HTTPS"
        assert "cert-manager.io/cluster-issuer" in ann
        assert ann["cert-manager.io/cluster-issuer"] == "letsencrypt"
        assert "nginx.ingress.kubernetes.io/force-ssl-redirect" in ann

    def test_nginx_no_tailscale_annotations(self):
        ing = self._make_nginx_ingress()
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        ann = obj.metadata.annotations
        assert "tailscale.com/proxy-group" not in ann
        assert "tailscale.com/tags" not in ann

    def test_proxy_timeouts(self):
        ing = self._make_nginx_ingress(
            proxy_connect_timeout="30s",
            proxy_read_timeout="60s",
        )
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        ann = obj.metadata.annotations
        assert ann["nginx.ingress.kubernetes.io/proxy-connect-timeout"] == "30s"
        assert ann["nginx.ingress.kubernetes.io/proxy-read-timeout"] == "60s"

    def test_session_affinity(self):
        ing = self._make_nginx_ingress(session_affinity=True)
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert (
            obj.metadata.annotations["nginx.ingress.kubernetes.io/affinity"] == "cookie"
        )

    def test_no_session_affinity_by_default(self):
        ing = self._make_nginx_ingress()
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert "nginx.ingress.kubernetes.io/affinity" not in obj.metadata.annotations

    def test_use_regex(self):
        ing = self._make_nginx_ingress(use_regex=True)
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert (
            obj.metadata.annotations["nginx.ingress.kubernetes.io/use-regex"] == "true"
        )

    def test_extra_annotations_when_allowed(self):
        ing = self._make_nginx_ingress(
            allow_annotations=True,
            extra_annotations={"custom.io/foo": "bar"},
        )
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert obj.metadata.annotations["custom.io/foo"] == "bar"

    def test_extra_annotations_ignored_when_not_allowed(self):
        ing = self._make_nginx_ingress(
            allow_annotations=False,
            extra_annotations={"custom.io/foo": "bar"},
        )
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert "custom.io/foo" not in obj.metadata.annotations

    def test_tls_with_secret_name(self):
        ing = self._make_nginx_ingress(
            hosts=[FakeHost("app.example.com", tls_enabled=True)],
        )
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert len(obj.spec.tls) == 1
        assert obj.spec.tls[0].secret_name == f"{RESOURCE_PREFIX}-web-tls"
        assert "app.example.com" in obj.spec.tls[0].hosts

    def test_no_tls_when_disabled(self):
        ing = self._make_nginx_ingress(
            hosts=[FakeHost("app.example.com", tls_enabled=False)],
        )
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert obj.spec.tls is None

    def test_host_in_rules(self):
        ing = self._make_nginx_ingress(
            hosts=[FakeHost("app.example.com")],
        )
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert len(obj.spec.rules) == 1
        assert obj.spec.rules[0].host == "app.example.com"

    def test_multiple_hosts(self):
        ing = self._make_nginx_ingress(
            hosts=[
                FakeHost("app.example.com"),
                FakeHost("www.example.com"),
            ],
        )
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert len(obj.spec.rules) == 2
        hosts = {r.host for r in obj.spec.rules}
        assert hosts == {"app.example.com", "www.example.com"}

    def test_labels_applied(self):
        ing = self._make_nginx_ingress()
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert obj.metadata.labels["resident-ingress.cabotage.io"] == "true"
        assert obj.metadata.labels["ingress"] == "web"
        assert obj.metadata.labels["organization"] == "myorg"


# ---------------------------------------------------------------------------
# render_ingress_object — tailscale
# ---------------------------------------------------------------------------


class TestRenderTailscaleIngress:
    def _make_ts_ingress(self, **overrides):
        defaults = dict(
            name="ts-web",
            ingress_class_name="tailscale",
            hosts=[FakeHost("my-app")],
            paths=[FakePath()],
        )
        defaults.update(overrides)
        return FakeIngress(**defaults)

    def test_basic_tailscale_ingress(self):
        ing = self._make_ts_ingress()
        obj = render_ingress_object(
            ing,
            RESOURCE_PREFIX,
            LABELS,
            org_k8s_identifier="myorg-abc123",
        )
        assert obj is not None
        assert obj.spec.ingress_class_name == "tailscale"

    def test_disabled_returns_none(self):
        ing = self._make_ts_ingress(enabled=False)
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert obj is None

    def test_no_nginx_annotations(self):
        ing = self._make_ts_ingress()
        obj = render_ingress_object(
            ing,
            RESOURCE_PREFIX,
            LABELS,
            org_k8s_identifier="myorg-abc123",
        )
        ann = obj.metadata.annotations
        assert "nginx.ingress.kubernetes.io/backend-protocol" not in ann
        assert "cert-manager.io/cluster-issuer" not in ann

    def test_proxy_group_annotation(self):
        ing = self._make_ts_ingress()
        obj = render_ingress_object(
            ing,
            RESOURCE_PREFIX,
            LABELS,
            org_k8s_identifier="myorg-abc123",
        )
        ann = obj.metadata.annotations
        assert ann["tailscale.com/proxy-group"] == "ingress-myorg-abc123"

    def test_no_proxy_group_without_org_identifier(self):
        ing = self._make_ts_ingress()
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        ann = obj.metadata.annotations
        assert "tailscale.com/proxy-group" not in ann

    def test_tailscale_tags_annotation(self):
        ing = self._make_ts_ingress(tailscale_tags="tag:web,tag:prod")
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert obj.metadata.annotations["tailscale.com/tags"] == "tag:web,tag:prod"

    def test_no_tags_when_not_set(self):
        ing = self._make_ts_ingress(tailscale_tags=None)
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert "tailscale.com/tags" not in obj.metadata.annotations

    def test_org_default_tags_fallback(self):
        ing = self._make_ts_ingress(tailscale_tags=None)
        obj = render_ingress_object(
            ing, RESOURCE_PREFIX, LABELS, org_default_tags="tag:cabotage"
        )
        assert obj.metadata.annotations["tailscale.com/tags"] == "tag:cabotage"

    def test_ingress_tags_override_org_default(self):
        ing = self._make_ts_ingress(tailscale_tags="tag:custom")
        obj = render_ingress_object(
            ing, RESOURCE_PREFIX, LABELS, org_default_tags="tag:cabotage"
        )
        assert obj.metadata.annotations["tailscale.com/tags"] == "tag:custom"

    def test_tls_always_on(self):
        """Tailscale ingresses always include all hosts in TLS, regardless of tls_enabled."""
        ing = self._make_ts_ingress(
            hosts=[FakeHost("my-app", tls_enabled=False)],
        )
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert len(obj.spec.tls) == 1
        assert "my-app" in obj.spec.tls[0].hosts

    def test_tls_no_secret_name(self):
        """Tailscale handles TLS automatically — no secret_name needed."""
        ing = self._make_ts_ingress()
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert obj.spec.tls[0].secret_name is None

    def test_host_not_in_rules(self):
        """Tailscale ingress rules should have host=None to avoid FQDN mismatch."""
        ing = self._make_ts_ingress(
            hosts=[FakeHost("my-app")],
        )
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert len(obj.spec.rules) == 1
        assert obj.spec.rules[0].host is None

    def test_host_in_tls(self):
        """The hostname goes into tls.hosts for MagicDNS name."""
        ing = self._make_ts_ingress(
            hosts=[FakeHost("my-app")],
        )
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert obj.spec.tls[0].hosts == ["my-app"]

    def test_labels_applied(self):
        ing = self._make_ts_ingress()
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert obj.metadata.labels["resident-ingress.cabotage.io"] == "true"
        assert obj.metadata.labels["ingress"] == "ts-web"

    def test_backend_port_uses_named_https(self):
        """Tailscale backends use port name 'https' for HTTPS detection."""
        ing = self._make_ts_ingress()
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        path = obj.spec.rules[0].http.paths[0]
        assert path.backend.service.port.name == "https"


# ---------------------------------------------------------------------------
# render_service
# ---------------------------------------------------------------------------


class TestRenderService:
    def test_service_name(self):
        svc = render_service(FakeRelease(), "web")
        # resource_prefix is safe_k8s_name(project.k8s_identifier, app.k8s_identifier)
        assert "web" in svc.metadata.name

    def test_port_named_https(self):
        svc = render_service(FakeRelease(), "web")
        assert len(svc.spec.ports) == 1
        assert svc.spec.ports[0].name == "https"
        assert svc.spec.ports[0].port == 8000
        assert svc.spec.ports[0].target_port == 8000

    def test_labels(self):
        svc = render_service(FakeRelease(), "web")
        assert svc.metadata.labels["resident-service.cabotage.io"] == "true"
        assert svc.metadata.labels["process"] == "web"

    def test_selector(self):
        svc = render_service(FakeRelease(), "web")
        assert "process" in svc.spec.selector
        assert svc.spec.selector["process"] == "web"
